"""Native, read-only NGDC GSA discovery using public HTML, RunInfo and Qtrans.

RunInfo is GSA's own CSV export, not an INSDC mirror.  Qtrans paths are accepted
only when the public file-list service returns the matching file itself.
"""
import csv
from html.parser import HTMLParser
import io
import json
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit

from discovery_metadata import CORE_FIELDS, TAXA, UNKNOWN, classify_protocol, clean, identity
from download_reads import SPECIES
from ena_source import EnaClient
from treatment_metadata import summarize_treatment, TREATMENT_NORMALIZATION_VERSION

BASE = 'https://ngdc.cncb.ac.cn/gsa/'
QTRANS_BASE = 'https://qtp.cncb.ac.cn/qtrans/api/v2/'
PLATFORMS = ('OXFORD_NANOPORE', 'PACBIO_SMRT')
CSV_REQUIRED = {'Run', 'Center', 'Experiment', 'BioSample', 'BioProject', 'ScientificName',
                'TaxID', 'Platform', 'LibrarySource', 'Submission', 'FileName', 'FileSize',
                'FileType', 'Download_path'}
RUN_SECTIONS = {'测序反应', 'runs', 'sequencing runs', 'sequencing run'}
RUN_HEADERS = {'run编号', 'run accession', 'run accession number', 'run id', 'run'}


class _HTML(HTMLParser):
    """Keep table cells, including nested tables, without interpreting scripts."""
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tables=[]; self.active=[]; self.rows=[]; self.experiments=[]; self.skip=0
        self.table_frames=[];self.run_accessions=set()
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag in {'script','style'}:self.skip+=1
        if tag=='table':
            self.tables.append(attrs.get('id',''))
            section=''
            if self.active and self.active[-1]['cells']:
                label=self.active[-1]['cells'][0]
                if label['tag']=='th':section=clean(' '.join(label['parts'])).casefold().rstrip(':')
            self.table_frames.append({'is_run':section in RUN_SECTIONS})
        if tag=='tr':self.active.append({'table':self.tables[-1] if self.tables else '',
            'frame':self.table_frames[-1] if self.table_frames else {},'cells':[], 'cell':None})
        if tag in {'th','td'} and self.active:
            cell={'tag':tag,'parts':[]};self.active[-1]['cells'].append(cell);self.active[-1]['cell']=cell
        if tag=='input' and 'checkbox_class' in attrs.get('class','').split():
            acc=attrs.get('data-value','')
            if not re.fullmatch(r'CRX[0-9]+',acc):raise ValueError('non-native NGDC experiment in search results')
            self.experiments.append(acc)

    def handle_endtag(self, tag):
        if tag in {'script','style'}:self.skip=max(0,self.skip-1)
        if tag in {'th','td'} and self.active:self.active[-1]['cell']=None
        if tag=='tr' and self.active:
            row=self.active.pop()
            cells=[(c['tag'],clean(' '.join(c['parts'])))for c in row['cells']]
            self.rows.append((row['table'],cells))
            if cells and all(tag=='th'for tag,value in cells) and cells[0][1].casefold().rstrip(':') in RUN_HEADERS:
                row['frame']['is_run']=True
            if (row['frame'].get('is_run') and cells and cells[0][0]=='td'
                    and re.fullmatch(r'CRR[0-9]+',cells[0][1])):
                self.run_accessions.add(cells[0][1])
        if tag=='table' and self.tables:
            self.tables.pop();self.table_frames.pop()

    def handle_data(self, value):
        if self.skip:return
        for row in self.active:
            if row['cell'] is not None:row['cell']['parts'].append(value)


def _html(raw):
    try:text=raw.decode('utf-8-sig') if isinstance(raw,bytes) else raw
    except UnicodeError as exc:raise ValueError('invalid UTF-8 NGDC HTML')from exc
    if not isinstance(text,str) or not re.search(r'<(?:html|table|div)\b',text,re.I):
        raise ValueError('NGDC did not return record HTML')
    if re.search(r'正在进行安全检查|安全验证|<title>\s*(?:security|access denied|502|500)|captcha',text,re.I):
        raise ValueError('NGDC returned an access check or service error')
    return text,_HTML(text)


def _pairs(parsed):
    result={}
    for _,cells in parsed.rows:
        if len(cells)==2 and cells[0][0]=='th' and cells[1][0]=='td':
            result.setdefault(cells[0][1].casefold().rstrip(':'),[]).append(cells[1][1])
    return result


def _values(pairs, *names):
    return [v for name in names for v in pairs.get(name.casefold(),[]) if v.casefold() not in UNKNOWN]


def _download_paths(row):
    submission=row.get('Submission','')
    if not re.fullmatch(r'CRA[0-9]+',submission):
        raise ValueError('missing_or_invalid_submission_accession')
    paths=[]
    for value in row.get('Download_path','').split('|'):
        if not value:continue
        parsed=urlsplit(value)
        if (parsed.scheme not in {'ftp','https'} or parsed.hostname not in {'download.big.ac.cn','download.cncb.ac.cn'}
                or parsed.query or parsed.fragment or not re.fullmatch(r'/gsa[0-9]*/CRA[0-9]+/CRR[0-9]+/[^/]+',parsed.path)
                or '..' in PurePosixPath(parsed.path).parts):
            raise ValueError('unverified NGDC download URL')
        if PurePosixPath(parsed.path).parent.parent.name!=submission:
            raise ValueError('download_submission_directory_conflict')
        paths.append(parsed.path)
    return paths


def _file_issues(row):
    """Validate CSV byte counts and whether the files can enter FASTQ processing.

    Qtrans's rounded human-readable size is deliberately not interpreted as an
    exact byte count. The precise size evidence here is the official CSV only.
    """
    files=row.get('FileName','').split('|');sizes=row.get('FileSize','').split('|')
    issues=[]
    if (not all(files) or len(sizes)!=len(files)
            or any(not re.fullmatch(r'[0-9]+',s) or int(s)<=0 for s in sizes)):
        issues.append('FileSize: missing_or_invalid_positive_byte_counts')
    formats=[v.strip().casefold()for v in row.get('FileType','').split('|')]
    if len(formats)==1:formats*=len(files)
    if len(formats)!=len(files):
        issues.append('RemotePaths: unsupported_file_format');return issues
    for name,kind in zip(files,formats):
        lower=name.casefold()
        unsupported=bool(re.search(r'\.(?:bam|cram|sra|fast5|pod5|h5|hdf5|tar|zip|7z)(?:\.gz)?$'
                                   r'|\.(?:bz2|bzip2|bz|xz|zst|tgz)$',lower))
        known_fastq=kind in {'fastq','fq','fastq.gz','fq.gz'}
        named_fastq=lower.endswith(('.fastq','.fq','.fastq.gz','.fq.gz'))
        unspecified=kind in UNKNOWN or kind in {'unspecified','other'}
        if unsupported or (not known_fastq and not (unspecified and named_fastq)):
            issues.append('RemotePaths: unsupported_file_format')
    return issues


class NgdcClient(EnaClient):
    def __init__(self,batch,base=BASE,offline=False,timeout=45,retries=2,page_size=100,
                 qtrans_base=QTRANS_BASE,sample_base=None,**kwargs):
        super().__init__(batch,base=base,offline=offline,timeout=timeout,retries=retries,provider='NGDC',**kwargs)
        if not isinstance(page_size,int) or page_size<1:raise ValueError('invalid NGDC page size')
        self.page_size=page_size
        common=dict(offline=offline,timeout=timeout,retries=retries,provider='NGDC',
                    retry_delay=self.retry_delay,delay=self.delay)
        self.qtrans=EnaClient(batch,base=qtrans_base,**common)
        self.samples=EnaClient(batch,base=sample_base or self.base.rsplit('/gsa/',1)[0]+'/biosample/',**common)

    def _export(self,params):
        raw,receipt=self.fetch('search/getRunInfo',params)
        try:
            text=raw.decode('utf-8-sig');reader=csv.DictReader(io.StringIO(text))
            if not reader.fieldnames or not CSV_REQUIRED<=set(reader.fieldnames):raise ValueError('missing NGDC CSV schema')
            rows=list(reader)
        except (UnicodeError,csv.Error)as exc:raise ValueError('invalid NGDC RunInfo CSV')from exc
        for row in rows:
            if any(not isinstance(v,str)for v in row.values()):raise ValueError('malformed NGDC CSV row')
            row['run_accession']=row['Run']
            self.portal_receipts[identity(row)]=receipt
        return rows

    def search(self,species,accessions=(),projects=(),allow_missing=False):
        if species not in SPECIES:raise ValueError('unsupported NGDC species')
        if any(not re.fullmatch(r'CRR[0-9]+',x)for x in accessions):raise ValueError('NGDC requires native CRR runs')
        if any(not re.fullmatch(r'PRJCA[0-9]+',x)for x in projects):raise ValueError('NGDC requires PRJCA projects')
        seen={}
        def admit(rows):
            for row in rows:
                acc=row['Run']
                if not re.fullmatch(r'CRR[0-9]+',acc) or row['Center']!='NGDC':
                    raise ValueError('non-native run returned by NGDC')
                if accessions and acc not in accessions:continue
                name=row['ScientificName'].casefold();expected=SPECIES[species].casefold()
                if not (name==expected or name.startswith(expected+' ')) or re.search(r'\bhybrid\b|\bmixed\b|\s[x×]\s',name):
                    if accessions and allow_missing:continue
                    raise ValueError('NGDC returned wrong species')
                if row['TaxID'] in {str(x)for x in TAXA.values()} and row['TaxID']!=str(TAXA[species]):
                    raise ValueError('NGDC taxon conflicts with requested species')
                if row['Platform'] not in PLATFORMS or row['LibrarySource']!='TRANSCRIPTOMIC':
                    raise ValueError('NGDC returned non-transcriptomic or unsupported platform run')
                if projects and row['BioProject'] not in projects:continue
                if acc in seen and seen[acc]!=row:raise ValueError('conflicting NGDC run records')
                seen[acc]=row
        if accessions:
            for acc in sorted(set(accessions)):
                raw,_=self.fetch('search',{'searchTerm':acc});text,parsed=_html(raw)
                refs={x for x in _values(_pairs(parsed),'实验编号','experiment accession','experiment')
                      if re.fullmatch(r'CRX[0-9]+',x)}
                if len(refs)!=1:
                    if not refs and ('queryResult' in text or 'Total Items:' in text or 'No items found' in text):continue
                    raise ValueError('NGDC run lookup has missing or conflicting experiment links')
                admit(self._export({'searchTerm':next(iter(refs)), 'accessionSelectedList':json.dumps(sorted(refs))}))
        else:
            term='"{}"[organism] AND "NGDC"[center]'.format(SPECIES[species])
            if projects:term+=' AND ('+' OR '.join('"{}"[projectAcc]'.format(x)for x in sorted(set(projects)))+')'
            for platform in PLATFORMS:
                params={'searchTerm':term,'platformSelected':'true','platformSelectedItem':platform,
                        'sourceSelected':'true','sourceSelectedItem':'TRANSCRIPTOMIC',
                        'pageSize':self.page_size,'from':1}
                raw,_=self.fetch('search',params);text,parsed=_html(raw)
                totals={int(x)for x in re.findall(r'Total Items:\s*(?:&nbsp;|\s)*([0-9]+)',text)}
                if not totals:totals={int(x)for x in re.findall(r"totalDatas['\"]?\s*(?:value=|=\s*)['\"]([0-9]+)",text)}
                if len(totals)!=1:raise ValueError('NGDC search has no unambiguous result count')
                total=totals.pop();ids=parsed.experiments
                if len(ids)!=len(set(ids)):raise ValueError('duplicate NGDC experiment on first page')
                if (not ids and total)or len(ids)>total:raise ValueError('NGDC search returned an incomplete page')
                if not total:continue
                # The public UI has unstable tie ordering across pages. Use its
                # official complete export, rather than dropping repeated rows.
                # RunInfo can also contain ancestor taxa, so verify the exact
                # requested scope and its unique experiment count against the UI.
                exported=self._export(params);expected_name=SPECIES[species].casefold()
                scoped=[r for r in exported if r['Center']=='NGDC'and r['Platform']==platform
                        and r['LibrarySource']=='TRANSCRIPTOMIC'
                        and(not projects or r['BioProject']in projects)
                        and(r['ScientificName'].casefold()==expected_name
                            or r['ScientificName'].casefold().startswith(expected_name+' '))]
                experiments={r['Experiment']for r in scoped}
                if len(experiments)!=total or not set(ids)<=experiments:
                    raise ValueError('incomplete NGDC complete export or changed experiment count')
                admit(scoped)
        if accessions and not allow_missing and set(accessions)-set(seen):
            raise ValueError('requested runs not returned by NGDC: '+', '.join(sorted(set(accessions)-set(seen))))
        return [seen[x]for x in sorted(seen)]

    def bundle(self,row):
        receipt=self.portal_receipts.get(identity(row))
        if not receipt:raise ValueError('NGDC bundle requires a verified search row')
        result={'source':'NGDC','portal':row,'urls':{'portal':receipt['url']},
                'timestamps':{'portal':receipt['fetched_at']},'remote_records':[],
                'remote_listings':[],'issues':[]}
        for kind,client,path,params in [
            ('experiment',self,'search',{'searchTerm':row.get('Experiment','')}),
            ('sample',self.samples,'browse/'+row.get('BioSample',''),None)]:
            pattern=r'CRX[0-9]+' if kind=='experiment' else r'SAMC[0-9]+'
            if not re.fullmatch(pattern,row.get('Experiment' if kind=='experiment' else 'BioSample','')):
                result[kind+'_html']='';result['issues'].append(kind+': missing linked identifier');continue
            raw,ref=client.fetch(path,params);text,_=_html(raw)
            result[kind+'_html']=text;result['urls'][kind]=ref['url'];result['timestamps'][kind]=ref['fetched_at']
        try:paths=_download_paths(row)
        except ValueError as exc:paths=[];result['issues'].append('RemotePaths: '+str(exc))
        file_issues=_file_issues(row);result['issues'].extend(file_issues)
        if file_issues:paths=[]
        for directory in sorted({str(PurePosixPath(x).parent)for x in paths}):
            raw,ref=self.qtrans.fetch('file/list',{'path':directory})
            try:listing=json.loads(raw)
            except (ValueError,UnicodeError)as exc:raise ValueError('Qtrans returned invalid JSON')from exc
            if (not isinstance(listing,dict) or listing.get('code')!=200 or not isinstance(listing.get('data'),list)
                    or any(not isinstance(x,dict)for x in listing['data'])):
                raise ValueError('Qtrans returned an access or service error')
            result['remote_records'].extend(listing['data'])
            result['remote_listings'].append({'directory':directory,'records':listing['data'],'receipt':ref})
        if len(result['remote_listings'])==1:
            ref=result['remote_listings'][0]['receipt']
            result['urls']['remote']=ref['url'];result['timestamps']['remote']=ref['fetched_at']
        return result


def normalize_ngdc_bundle(bundle,species,protocols):
    portal=bundle['portal'];acc=portal.get('Run',portal.get('run_accession',''))
    if species not in SPECIES or not re.fullmatch(r'CRR[0-9]+',acc):raise ValueError('unsupported native NGDC run/species')
    row={};fields={};issues=list(bundle.get('issues',[]));expt=portal.get('Experiment','');sample=portal.get('BioSample','')
    def ev(kind,path,value):
        return {'source':'NGDC','url':bundle.get('urls',{}).get(kind,''),
                'record':{'experiment':expt,'sample':sample}.get(kind,acc),'path':path,'raw_value':value,
                'fetched_at':bundle.get('timestamps',{}).get(kind)}
    def put(name,value,entries,reason=None):
        row[name]=value;fields[name]={'normalized_value':value,'evidence':entries,
            'status':'missing' if value=='unknown' else 'reported','reason':reason or ('not_reported'if value=='unknown'else'explicit_record_value')}
    def problem(name,reason):issues.append(name+': '+reason)
    put('Accession',acc,[ev('portal','Run',acc)])
    for field,key,pattern in [('BioSample','BioSample',r'SAMC[0-9]+'),('BioProject','BioProject',r'PRJCA[0-9]+')]:
        value=portal.get(key,'');valid=bool(re.fullmatch(pattern,value))
        put(field,value if valid else 'unknown',[ev('portal',key,value)])
        if not valid:problem(field,'missing_or_invalid_linked_identifier')
    parsed={}
    for kind in ['experiment','sample']:
        text=bundle.get(kind+'_html','')
        if text:_,parsed[kind]=_html(text)
        else:problem(kind,'missing_public_record');parsed[kind]=_HTML('')
    xp=_pairs(parsed['experiment']);sp=_pairs(parsed['sample'])
    def verify(pairs,names,expected,field):
        vals=_values(pairs,*names)
        if not vals or set(vals)!={expected}:problem(field,'missing_or_conflicting_linked_record')
    verify(xp,['实验编号','experiment accession','experiment'],expt,'Accession')
    verify(xp,['样本编号','biosample','sample accession'],sample,'BioSample')
    verify(sp,['样本编号','biosample','sample accession'],sample,'BioSample')
    verify(xp,['项目编号','bioproject','project accession'],portal.get('BioProject',''),'BioProject')
    verify(sp,['项目编号','bioproject','project accession'],portal.get('BioProject',''),'BioProject')
    submissions=_values(xp,'GSA编号','gsa accession','submission')
    if submissions and set(submissions)!={portal.get('Submission','')}:
        problem('RemotePaths','experiment_submission_mismatch')
    if acc not in parsed['experiment'].run_accessions:problem('Accession','experiment_run_link_mismatch')
    expected=SPECIES[species];names=[portal.get('ScientificName','')]+_values(xp,'物种名称','organism','scientific name')+_values(sp,'物种名称','organism','scientific name')
    if (len(names)<3 or any(not (n.casefold()==expected.casefold()or n.casefold().startswith(expected.casefold()+' '))for n in names)
            or any(re.search(r'\bhybrid\b|\bmixed\b|\s[x×]\s',n,re.I)for n in names)
            or not portal.get('TaxID','').isdigit()
            or (portal.get('TaxID')in {str(t)for t in TAXA.values()}and portal['TaxID']!=str(TAXA[species]))):
        problem('Species','missing_or_conflicting_taxon')
    put('Species',expected,[ev('portal','TaxID/ScientificName',[portal.get('TaxID',''),names[0]]),ev('sample','物种名称',_values(sp,'物种名称','organism','scientific name'))])
    platform=portal.get('Platform','')
    verify(xp,['测序平台','platform'],platform,'Platform')
    if platform not in PLATFORMS:problem('Platform','unsupported_or_missing_platform')
    put('Platform',platform or 'unknown',[ev('portal','Platform',platform),ev('experiment','测序平台',_values(xp,'测序平台','platform'))])
    library={};last=None
    for _,cells in parsed['experiment'].rows:
        values=[x[1]for x in cells]
        if any(v in {'文库构建方法','Library construction protocol'}for v in values):last=values
        elif last and cells and all(x[0]=='td'for x in cells)and len(cells)==len(last):
            for k,v in zip(last,values):library.setdefault(k,[]).append(v)
            last=None
    src=_values(library,'样品来源','Library source')
    if portal.get('LibrarySource')!='TRANSCRIPTOMIC' or set(src)!={'TRANSCRIPTOMIC'}:problem('Protocol','non_transcriptomic_or_conflicting_library_source')
    descriptions=_values(library,'文库构建方法','Library construction protocol')
    entries=[ev('portal',k,portal.get(k,''))for k in ['Title','LibraryName']]
    entries += [ev('experiment','文库构建方法',v)for v in descriptions]
    protocol,reason=classify_protocol(' '.join(str(x['raw_value'])for x in entries),platform,protocols)
    if not descriptions:problem('Protocol','missing_library_construction_description')
    if reason:problem('Protocol',reason)
    put('Protocol',protocol,entries,reason)
    attributes={}
    for table,cells in parsed['sample'].rows:
        if table=='attribute_table'and len(cells)==2:
            attributes.setdefault(cells[0][1].casefold().rstrip(':'),[]).append(cells[1][1])
    def attr(field,names):
        values=list(dict.fromkeys(_values(attributes,*names)))
        value=values[0]if len(values)==1 else'unknown'
        put(field,value,[ev('sample','attribute_table/'+n,v)for n in names for v in _values(attributes,n)])
        if len(values)>1:problem(field,'conflicting_structured_values')
    attr('Tissue',['组织','tissue','组织类型','organ','organism part'])
    attr('Ecotype',['栽培品种','品种','生态型','cultivar','ecotype','variety','strain'])
    attr('Genotype',['基因型','genotype','genetic background'])
    treatment_names=['处理方法','生长时期','发育阶段','取样时间','培养方法','培养温度','treatment','age','growth stage','developmental stage','time','time point','growth conditions']
    treatment=[(n,v)for n in treatment_names for v in _values(attributes,n)]
    summary, reason = summarize_treatment('; '.join(n+': '+v for n,v in treatment))
    put('Treatment',summary,[ev('sample','attribute_table/'+n,v)for n,v in treatment],reason)
    for n in treatment_names:
        if len(set(_values(attributes,n)))>1:problem('Treatment','conflicting_structured_values')
    put('Database','NGDC',[ev('portal','Center',portal.get('Center',''))])
    if portal.get('Center')!='NGDC':problem('Database','non_native_record')
    try:paths=_download_paths(portal)
    except ValueError as exc:paths=[];problem('RemotePaths',str(exc))
    file_issues=_file_issues(portal);issues.extend(file_issues)
    files=portal.get('FileName','').split('|');verified=[];records=bundle.get('remote_records',[])
    file_evidence=[]
    if not paths or len(paths)!=len(files):problem('RemotePaths','missing_download_URL_or_filename')
    else:
        for name,path in zip(files,paths):
            if PurePosixPath(path).name!=name or PurePosixPath(path).parent.name!=acc:
                problem('RemotePaths','download_filename_or_run_directory_conflict');continue
            matches=[x for x in records if x.get('name')==name and x.get('path')==path and x.get('type')=='file'and x.get('disabled')is False]
            if len(matches)!=1:problem('RemotePaths','missing_or_conflicting_official_Qtrans_file');continue
            sources=[(listing,index) for listing in bundle.get('remote_listings',[])
                     if listing.get('directory')==str(PurePosixPath(path).parent)
                     for index,record in enumerate(listing.get('records',[])) if record==matches[0]]
            if len(sources)!=1:
                problem('RemotePaths','missing_or_conflicting_Qtrans_response_receipt');continue
            listing,index=sources[0];receipt=listing.get('receipt',{})
            if not receipt.get('url') or not receipt.get('fetched_at'):
                problem('RemotePaths','missing_Qtrans_response_receipt');continue
            file_evidence.append({'source':'NGDC','url':receipt['url'],'record':acc,
                'path':'$.data[{}]'.format(index),'raw_value':matches[0],
                'fetched_at':receipt['fetched_at']})
            verified.append(path)
    if paths and len(verified)==len(paths) and not file_issues:
        put('RemotePaths',';'.join(verified),file_evidence+[
            ev('portal','Download_path',portal.get('Download_path','')),
            ev('portal','Submission',portal.get('Submission','')),
            ev('portal','FileSize',portal.get('FileSize','')),
            ev('portal','FileType',portal.get('FileType',''))])
    return {'accession':acc,'aliases':[acc],'row':{k:row[k]for k in CORE_FIELDS+(['RemotePaths']if'RemotePaths'in row else[])},
            'fields':fields,'issues':list(dict.fromkeys(issues)),
            'source_fingerprint':identity({k:bundle.get(k)for k in ['portal','experiment_html','sample_html','remote_records']}),
            'normalization_version':TREATMENT_NORMALIZATION_VERSION}
