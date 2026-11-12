"""Read-only ENA Portal/Browser adapter with verifiable response caching."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from discovery_io import atomic_json, atomic_write, digest
from discovery_metadata import TAXA, RUN, identity

BASE = 'https://www.ebi.ac.uk/ena/'
FIELDS = ('run_accession,experiment_accession,sample_accession,secondary_sample_accession,'
          'study_accession,secondary_study_accession,tax_id,scientific_name,instrument_platform,'
          'library_strategy,library_source,library_construction_protocol,experiment_title,'
          'library_name,first_public,last_updated')


class MetadataLinkError(ValueError):
    """A run exists, but its experiment/sample identifier needs review."""


def response_key(url,method='GET',request_body='',content_type=''):
    if not isinstance(url,str)or method not in {'GET','POST'}:
        raise ValueError('invalid cached HTTP request')
    if method=='GET':return digest(url.encode())
    if not isinstance(request_body,str)or not isinstance(content_type,str):
        raise ValueError('invalid cached POST request')
    return digest((url+'\nPOST\n'+content_type+'\n'+request_body).encode())


def verify_response_cache(batch):
    """Recheck a batch's source bytes before reusing normalized candidates."""
    folder = Path(batch) / 'responses'
    for path in sorted(folder.glob('*.json')):
        receipt = json.loads(path.read_text())
        url = receipt.get('url')
        if response_key(url,receipt.get('method','GET'),receipt.get('request_body',''),receipt.get('content_type','')) != path.stem:
            raise ValueError('invalid response cache receipt: ' + str(path))
        body = folder / (path.stem + '.body')
        if not body.is_file() or digest(body.read_bytes()) != receipt.get('sha256'):
            raise ValueError('response cache hash mismatch: ' + str(body))


class EnaClient:
    def __init__(self, batch, base=BASE, offline=False, timeout=45, retries=2, retry_delay=2, delay=0.15, provider='ENA'):
        self.batch = Path(batch); self.base = base.rstrip('/') + '/'; self.offline = offline
        self.timeout, self.retries, self.retry_delay, self.delay = timeout, retries, retry_delay, delay
        self.last_request = 0
        self.portal_receipts = {}
        self.provider = provider

    def fetch(self, path, params=None,*,method='GET',json_body=None):
        if method not in {'GET','POST'}or (json_body is not None and method!='POST'):
            raise ValueError('unsupported metadata HTTP request')
        url = self.base + path + ('?' + urlencode(params) if params else '')
        payload=None;content_type='';request_body=''
        if method=='POST':
            if json_body is not None:
                request_body=json.dumps(json_body,sort_keys=True,separators=(',',':'),allow_nan=False)
                content_type='application/json'
            else:
                url=self.base+path;request_body=urlencode(params or {})
                content_type='application/x-www-form-urlencoded'
            payload=request_body.encode()
        key = response_key(url,method,request_body,content_type); folder = self.batch / 'responses'
        body, receipt = folder / (key + '.body'), folder / (key + '.json')
        if receipt.exists():
            metadata = json.loads(receipt.read_text())
            if (metadata.get('url') != url or metadata.get('method','GET')!=method or not body.is_file()
                    or (method=='POST'and(metadata.get('request_body')!=request_body or metadata.get('content_type')!=content_type))):
                raise ValueError('invalid response cache receipt: ' + str(receipt))
            data = body.read_bytes()
            if digest(data) != metadata.get('sha256'):
                raise ValueError('response cache hash mismatch: ' + str(body))
            return data, metadata
        if self.offline:
            raise RuntimeError('offline response is not cached: ' + url)
        for attempt in range(self.retries + 1):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                headers={'User-Agent': 'PlantIRagent-discovery/1.0', 'Accept-Encoding': 'identity'}
                if content_type:headers['Content-Type']=content_type
                request = Request(url,data=payload,headers=headers,method=method)
                with urlopen(request, timeout=self.timeout) as response:
                    data = response.read(64 * 1024 * 1024 + 1)
                if len(data) > 64 * 1024 * 1024:
                    raise ValueError(self.provider+' response exceeds 64 MiB')
                metadata = {'url': url, 'sha256': digest(data),
                            'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
                if method=='POST':metadata.update(method=method,request_body=request_body,content_type=content_type)
                atomic_write(body, data); atomic_json(receipt, metadata)
                return data, metadata
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                if isinstance(exc, HTTPError): exc.close()
                if isinstance(exc, HTTPError) and exc.code not in {408, 429, 500, 502, 503, 504}:
                    raise RuntimeError('{} request failed: {}: {}'.format(self.provider,url, exc)) from exc
                if attempt == self.retries:
                    raise RuntimeError('{} request failed after {} attempts: {}: {}'.format(self.provider,attempt + 1, url, exc)) from exc
                wait = min(30, self.retry_delay * 2 ** attempt)
                if isinstance(exc, HTTPError):
                    retry_after = exc.headers.get('Retry-After', '')
                    if retry_after.isdigit(): wait = min(60, int(retry_after))
                time.sleep(wait)

    def search(self, species, accessions=(), projects=(), allow_missing=False,native_only=False):
        if species not in TAXA: raise ValueError('unsupported species: ' + species)
        if any(not RUN.fullmatch(a) or a.startswith('CRR') for a in accessions):
            raise ValueError('ENA discovery accepts only SRR/ERR/DRR accessions')
        if native_only and any(not a.startswith('ERR')for a in accessions):raise ValueError('ENA native discovery requires ERR')
        if any(not re.fullmatch(r'(?:PRJ(?:NA|EB|DB)[0-9]+|[SED]RP[0-9]+)', p) for p in projects):
            raise ValueError('invalid ENA project accession')
        query = ('tax_tree({}) AND (instrument_platform="PACBIO_SMRT" OR instrument_platform="OXFORD_NANOPORE") '
                 'AND (library_source="TRANSCRIPTOMIC" OR library_strategy="RNA-Seq")').format(TAXA[species])
        if accessions: query += ' AND (' + ' OR '.join('run_accession="{}"'.format(a) for a in sorted(set(accessions))) + ')'
        elif native_only:query+=' AND run_accession="ERR*"'
        if projects:
            query += ' AND (' + ' OR '.join('(study_accession="{0}" OR secondary_study_accession="{0}")'.format(p)
                                            for p in sorted(set(projects))) + ')'
        raw, receipt = self.fetch('portal/api/search', {'result': 'read_run', 'query': query,
                                               'fields': FIELDS, 'format': 'json', 'limit': 0})
        try:
            rows = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ValueError('ENA search returned invalid JSON') from exc
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise ValueError('ENA search did not return a record array')
        seen = {}
        for row in rows:
            if any(not isinstance(v, str) for v in row.values()): raise ValueError('invalid ENA field type')
            acc = row.get('run_accession', '')
            if not RUN.fullmatch(acc) or acc.startswith('CRR'): raise ValueError('invalid ENA run accession')
            if native_only and not acc.startswith('ERR'):continue
            if accessions and acc not in accessions: raise ValueError('ENA returned an unrequested run')
            if acc in seen and seen[acc] != row: raise ValueError('conflicting ENA records for ' + acc)
            seen[acc] = row
            self.portal_receipts[identity(row)] = receipt
        if accessions and not allow_missing and set(accessions) - set(seen):
            raise ValueError('requested runs not returned by ENA species/RNA/platform query: ' +
                             ', '.join(sorted(set(accessions) - set(seen))))
        return [seen[a] for a in sorted(seen)]

    def bundle(self, row):
        result = {'portal': row, 'urls': {}, 'timestamps': {}}
        for kind, key, pattern in [('run', 'run_accession', r'[SED]RR[0-9]+'),
                                   ('experiment', 'experiment_accession', r'[SED]RX[0-9]+'),
                                   ('sample', 'sample_accession', r'(?:SAM(?:N|EA|D)[0-9]+|[SED]RS[0-9]+)')]:
            accession = row.get(key, '')
            if not re.fullmatch(pattern, accession): raise MetadataLinkError('invalid linked ENA ' + kind + ': ' + accession)
            raw, receipt = self.fetch('browser/api/xml/' + accession)
            try: result[kind + '_xml'] = raw.decode('utf-8-sig')
            except UnicodeError as exc: raise ValueError('invalid UTF-8 ENA XML') from exc
            result['urls'][kind] = receipt['url']; result['fetched_at'] = receipt['fetched_at']
            result['timestamps'][kind] = receipt['fetched_at']
        receipt = self.portal_receipts.get(identity(row))
        if not receipt: raise ValueError('bundle requires a run returned by a verified search')
        result['urls']['portal'] = receipt['url']; result['timestamps']['portal'] = receipt['fetched_at']
        return result
