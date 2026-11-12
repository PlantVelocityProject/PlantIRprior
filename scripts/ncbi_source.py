"""Native NCBI SRA/BioSample discovery via cached, rate-limited E-utilities."""
import json
import re
import xml.etree.ElementTree as ET

from discovery_io import digest
from discovery_metadata import TAXA,identity,identifiers,clean
from ena_source import EnaClient,MetadataLinkError

BASE='https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'


def xml(raw):
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)',raw,re.I):raise ValueError('DTD/entities in NCBI response')
    try:root=ET.fromstring(raw)
    except ET.ParseError as exc:raise ValueError('invalid NCBI XML')from exc
    if root.tag=='ERROR' or root.find('.//ERROR')is not None:raise ValueError('NCBI returned XML error')
    return root


class NcbiClient(EnaClient):
    page_size=500

    def __init__(self,batch,base=BASE,**kwargs):
        kwargs.setdefault('delay',.4)
        super().__init__(batch,base=base,provider='NCBI',**kwargs)
        self.packages={}

    def search_ids(self,db,term):
        result=[];count=None;start=0
        while count is None or start<count:
            raw,receipt=self.fetch('esearch.fcgi',{'db':db,'term':term,'retmode':'json',
                'retstart':start,'retmax':self.page_size,'tool':'PlantIRagent'})
            data=json.loads(raw);page=data.get('esearchresult')
            if data.get('error')or not isinstance(page,dict)or page.get('errorlist'):
                raise ValueError('NCBI search returned an error or invalid result')
            warning=page.get('warninglist',{})
            if warning.get('quotedphrasesnotfound')or warning.get('phrasesignored'):
                raise ValueError('NCBI did not recognize a query phrase: '+json.dumps(warning))
            number=page.get('count','');ids=page.get('idlist')
            if not isinstance(number,str)or not number.isascii()or not number.isdecimal():
                raise ValueError('invalid NCBI result count')
            if not isinstance(ids,list)or any(not isinstance(i,str)or not re.fullmatch('[0-9]+',i)for i in ids):
                raise ValueError('invalid NCBI UID list')
            if count is None:count=int(number)
            if int(number)!=count:raise ValueError('NCBI index count changed during paging; retry in a new batch')
            if len(ids)!=min(self.page_size,count-start)or set(ids)&set(result)or len(ids)!=len(set(ids)):
                raise ValueError('incomplete or duplicated NCBI search page')
            result.extend(ids);start+=len(ids)
            if count==0:break
        return result

    def search(self,species,accessions=(),projects=(),allow_missing=False):
        if species not in TAXA:raise ValueError('unsupported NCBI species')
        if any(not re.fullmatch('SRR[0-9]+',a)for a in accessions):raise ValueError('NCBI native discovery requires SRR')
        if any(not re.fullmatch(r'(?:PRJ(?:NA|EB|DB)[0-9]+|[SED]RP[0-9]+)',p)for p in projects):
            raise ValueError('invalid NCBI project selector')
        term=('txid{}[Organism:exp] AND (PACBIO_SMRT[Platform] OR '
              'OXFORD_NANOPORE[Platform]) AND TRANSCRIPTOMIC[Source]').format(TAXA[species])
        if accessions:term+=' AND ('+' OR '.join(a+'[ACCN]'for a in sorted(set(accessions)))+')'
        if projects:term+=' AND ('+' OR '.join(p+'[All Fields]'for p in sorted(set(projects)))+')'
        ids=self.search_ids('sra',term);records={}
        for start in range(0,len(ids),100):
            raw,receipt=self.fetch('efetch.fcgi',{'db':'sra','id':','.join(ids[start:start+100]),
                'retmode':'xml','tool':'PlantIRagent'})
            root=xml(raw)
            if root.tag!='EXPERIMENT_PACKAGE_SET':raise ValueError('NCBI did not return SRA experiment packages')
            packages=root.findall('EXPERIMENT_PACKAGE')
            if len(packages)!=len(ids[start:start+100]):raise ValueError('incomplete NCBI experiment package response')
            for package in packages:
                experiment=package.find('EXPERIMENT');sample=package.find('SAMPLE');study=package.find('STUDY')
                if experiment is None or sample is None or study is None:raise ValueError('missing NCBI linked package object')
                platform=experiment.find('PLATFORM');platforms=[n.tag for n in platform]if platform is not None else[]
                library=experiment.find('DESIGN/LIBRARY_DESCRIPTOR')
                if library is None:raise ValueError('missing NCBI library descriptor')
                if platforms not in [['PACBIO_SMRT'],['OXFORD_NANOPORE']]or clean(library.findtext('LIBRARY_SOURCE')).upper()!='TRANSCRIPTOMIC':continue
                project_ids=sorted(x for x in identifiers(study)if re.fullmatch('PRJ(?:NA|EB|DB)[0-9]+',x))
                for run in package.findall('RUN_SET/RUN'):
                    accession=run.get('accession','')
                    if not re.fullmatch('SRR[0-9]+',accession):continue
                    if accessions and accession not in accessions:continue
                    row={'run_accession':accession,'experiment_accession':experiment.get('accession',''),
                         'sample_accession':sample.get('accession',''),'secondary_sample_accession':'',
                         'study_accession':';'.join(project_ids),
                         'secondary_study_accession':study.get('accession',''),
                         'tax_id':clean(sample.findtext('SAMPLE_NAME/TAXON_ID')),
                         'scientific_name':clean(sample.findtext('SAMPLE_NAME/SCIENTIFIC_NAME')),
                         'instrument_platform':platforms[0],'library_source':clean(library.findtext('LIBRARY_SOURCE')),
                         'library_strategy':clean(library.findtext('LIBRARY_STRATEGY')),
                         'library_construction_protocol':clean(library.findtext('LIBRARY_CONSTRUCTION_PROTOCOL')),
                         'experiment_title':clean(experiment.findtext('TITLE')),
                         'library_name':clean(library.findtext('LIBRARY_NAME')),
                         'ncbi_package_sha256':digest(ET.tostring(package))}
                    if accession in records and row!=records[accession]:raise ValueError('conflicting NCBI run records')
                    records[accession]=row;self.packages[identity(row)]=(package,run,receipt)
        missing=set(accessions)-set(records)
        if missing and not allow_missing:raise ValueError('requested runs not returned by NCBI: '+', '.join(sorted(missing)))
        return [records[a]for a in sorted(records)]

    def bundle(self,row):
        entry=self.packages.get(identity(row))
        if entry is None:raise ValueError('NCBI bundle requires a verified search record')
        package,run,receipt=entry;sample=package.find('SAMPLE')
        sample_ids=sorted(x for x in identifiers(sample)if re.fullmatch('SAMN[0-9]+',x))
        if len(sample_ids)!=1:raise MetadataLinkError('NCBI package has no unique linked BioSample')
        sample_accession=sample_ids[0];ids=self.search_ids('biosample',sample_accession+'[Accession]')
        if len(ids)!=1:raise MetadataLinkError('NCBI BioSample search is not unique')
        raw,bio_receipt=self.fetch('efetch.fcgi',{'db':'biosample','id':ids[0],'retmode':'xml','tool':'PlantIRagent'})
        biosamples=xml(raw).findall('.//BioSample')
        if len(biosamples)!=1 or biosamples[0].get('accession')!=sample_accession:
            raise MetadataLinkError('NCBI BioSample does not match SRA linked accession')
        result={'portal':row,'source':'NCBI','run_xml':ET.tostring(run,encoding='unicode'),
                'experiment_xml':ET.tostring(package.find('EXPERIMENT'),encoding='unicode'),
                'sample_xml':ET.tostring(sample,encoding='unicode'),'biosample_xml':raw.decode('utf-8-sig'),
                'biosample_accession':sample_accession,
                'urls':{k:receipt['url']for k in ('portal','run','experiment','sample')},
                'timestamps':{k:receipt['fetched_at']for k in ('portal','run','experiment','sample')}}
        result['urls']['biosample']=bio_receipt['url'];result['timestamps']['biosample']=bio_receipt['fetched_at']
        return result
