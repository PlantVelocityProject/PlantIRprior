#!/usr/bin/env python3
"""Download official ENA FASTQ files over HTTPS with size/MD5 checks and Range recovery."""
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.request import Request,urlopen
from urllib.parse import urlsplit

from discovery_io import atomic_json,sync_directory
from download_reads import child_path
from ena_source import EnaClient

CHUNK_SIZE=4*1024*1024


def fetch_range(item,start,end,open_url):
    """Fetch one bounded buffer; retry transient connection errors only."""
    for attempt in range(4):
        try:
            request=Request(item['url'],headers={'Accept-Encoding':'identity',
                'User-Agent':'PlantIRagent-ENA-HTTPS/1.0','Range':'bytes={}-{}'.format(start,end)})
            with open_url(request,timeout=45)as response:
                expected='bytes {}-{}/{}'.format(start,end,item['size_bytes'])
                if response.status!=206 or response.headers.get('Content-Range')!=expected:
                    raise ValueError('unexpected ENA Content-Range during parallel download')
                data=response.read(end-start+2)
                if len(data)!=end-start+1:raise OSError('incomplete ENA range')
                return data
        except (OSError,HTTPException):
            if attempt==3:raise
            time.sleep(min(4,2**attempt))


def prefetch_tail(item,partial,offset,open_url):
    # At most four pending 4 MiB results. Commit only the contiguous prefix;
    # interrupted later ranges are safely requested again on the next attempt.
    with ThreadPoolExecutor(max_workers=4)as pool,partial.open('ab')as handle:
        pending=deque();next_start=offset
        while next_start<item['size_bytes'] or pending:
            while next_start<item['size_bytes'] and len(pending)<4:
                end=min(next_start+CHUNK_SIZE,item['size_bytes'])-1
                pending.append((end,pool.submit(fetch_range,item,next_start,end,open_url)))
                next_start=end+1
            end,future=pending.popleft();data=future.result()
            handle.write(data);handle.flush();os.fsync(handle.fileno());offset=end+1
            del data,future
            print('ENA HTTPS {} bytes={}/{}'.format(item['name'],offset,item['size_bytes']),file=sys.stderr,flush=True)
    return offset


def md5(path):
    result=hashlib.md5()
    with path.open('rb')as handle:
        for block in iter(lambda:handle.read(8*1024*1024),b''):result.update(block)
    return result.hexdigest()


def transfer(item,directory,open_url):
    name=item['name'];target=child_path(directory,name)
    partial=child_path(directory,name+'.http-part');checkpoint=child_path(directory,name+'.http.json')
    if target.exists():
        if target.stat().st_size==item['size_bytes'] and md5(target)==item['md5']:return
        aria=child_path(directory,name+'.aria2')
        if not aria.is_file():raise ValueError('existing FASTQ does not match ENA checksum; preserve it before retrying: '+name)
        # The parent downloader owns this staging directory. An aria2 file is
        # preallocated/out-of-order and cannot be treated as a sequential prefix.
        target.unlink();aria.unlink()
    if checkpoint.exists():
        if json.loads(checkpoint.read_text())!=item:raise ValueError('HTTPS partial belongs to another archive identity')
    elif partial.exists():raise ValueError('unregistered HTTPS partial: '+str(partial))
    else:atomic_json(checkpoint,item)
    offset=partial.stat().st_size if partial.exists()else 0
    if offset==item['size_bytes'] and md5(partial)==item['md5']:
        os.replace(partial,target);checkpoint.unlink();sync_directory(directory);return
    if offset>=item['size_bytes']:
        with partial.open('wb'):pass
        offset=0
    print('ENA HTTPS {} resume_offset={}'.format(item['url'],offset),file=sys.stderr,flush=True)
    while offset<item['size_bytes']:
        end=min(offset+CHUNK_SIZE,item['size_bytes'])-1
        headers={'Accept-Encoding':'identity','User-Agent':'PlantIRagent-ENA-HTTPS/1.0',
                 'Range':'bytes={}-{}'.format(offset,end)}
        with open_url(Request(item['url'],headers=headers),timeout=45)as response:
            if response.status==206:
                expected='bytes {}-{}/{}'.format(offset,end,item['size_bytes'])
                if response.headers.get('Content-Range')!=expected:raise ValueError('unexpected ENA Content-Range')
            elif response.status==200:
                offset=0;end=item['size_bytes']-1
            else:raise ValueError('unexpected ENA file HTTP status: '+str(response.status))
            with partial.open('ab'if offset else'wb')as handle:
                total=offset
                while True:
                    block=response.read(1024*1024)
                    if not block:break
                    total+=len(block)
                    if total>end+1:raise ValueError('ENA response exceeds requested range')
                    handle.write(block)
                handle.flush();os.fsync(handle.fileno())
        if total!=end+1:raise ValueError('ENA range download is incomplete')
        offset=total
        print('ENA HTTPS {} bytes={}/{}'.format(name,offset,item['size_bytes']),file=sys.stderr,flush=True)
        if response.status==206 and offset<item['size_bytes']:
            offset=prefetch_tail(item,partial,offset,open_url)
    if partial.stat().st_size!=item['size_bytes']:raise ValueError('ENA download is incomplete')
    if md5(partial)!=item['md5']:raise ValueError('ENA file MD5 mismatch')
    os.replace(partial,target);checkpoint.unlink();sync_directory(directory)


def download_run(accession,directory,client=None,open_url=urlopen):
    if not re.fullmatch('[SED]RR[0-9]+',accession):raise ValueError('ENA HTTPS requires an INSDC run accession')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    client=client or EnaClient(directory/'.ena-metadata')
    raw,receipt=client.fetch('portal/api/filereport',{'accession':accession,'result':'read_run',
        'fields':'run_accession,fastq_ftp,fastq_md5,fastq_bytes','format':'json'})
    rows=json.loads(raw)
    if not isinstance(rows,list) or len(rows)!=1 or rows[0].get('run_accession')!=accession:
        raise ValueError('ENA file report does not uniquely identify requested run')
    row=rows[0]
    urls=row.get('fastq_ftp','').split(';');hashes=row.get('fastq_md5','').split(';');sizes=row.get('fastq_bytes','').split(';')
    if len(urls)!=len(hashes)or len(urls)!=len(sizes):raise ValueError('inconsistent ENA file report arrays')
    files=[];names=set()
    for value,checksum,size in zip(urls,hashes,sizes):
        url=value if '://'in value else'https://'+value
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname not in {'ftp.sra.ebi.ac.uk','ftp.ebi.ac.uk'} or parsed.port not in {None,443}:
            raise ValueError('untrusted ENA archive URL: '+url)
        name=Path(parsed.path).name
        if (not re.fullmatch('[A-Za-z0-9_.-]+',name)or not name.endswith(('.fastq.gz','.fq.gz'))
                or name in names or not re.fullmatch('[a-fA-F0-9]{32}',checksum)
                or not size.isascii()or not size.isdecimal()or int(size)<=0):
            raise ValueError('invalid ENA FASTQ file descriptor')
        names.add(name);files.append({'name':name,'url':url,'md5':checksum.lower(),'size_bytes':int(size)})
    actual={str(p.relative_to(directory)) for p in directory.rglob('*')
            if p.name.lower().endswith(('.fastq','.fq','.fastq.gz','.fq.gz'))}
    if actual-names:raise ValueError('unexpected FASTQ in staging; preserve or relocate it before retrying: '+', '.join(sorted(actual-names)))
    for item in files:transfer(item,directory,open_url)
    result={'accession':accession,'source':'ENA HTTPS','metadata':receipt,'files':files,'status':'verified'}
    atomic_json(directory/'ena-download.json',result);return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--accession',required=True)
    p.add_argument('--directory',type=Path,required=True);args=p.parse_args()
    try:result=download_run(args.accession,args.directory);code=0
    except (OSError,HTTPException,ValueError,RuntimeError,KeyError,TypeError)as exc:result={'status':'failed','error':str(exc)};code=1
    print(json.dumps(result,ensure_ascii=False));return code


if __name__=='__main__':sys.exit(main())
