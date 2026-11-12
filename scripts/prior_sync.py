"""Recoverable publication of five-column PlantVelo prior resources."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
from copy import deepcopy
from contextlib import nullcontext

from discovery_io import atomic_write, sync_directory
from download_reads import child_path, sha256

FIELDS = ['gene_id','chromosome','start','end','strand']
OUTPUTS = {'high_confidence_IR.tsv','intron_evidence.tsv.gz','provenance.json','qc.json'}
PENDING = '.prior-sync.pending.json'
STAGE = '.prior-sync'
DESCRIPTOR_FIELDS = {'species','reference_id','annotation_id','release_id',
                     'generation','cohort_hash','files','reference_sha256'}


def validate_descriptor(species,descriptor,selected):
    """Validate the frozen plan without resolving a different release target."""
    from task_contract import ContractError
    if not isinstance(descriptor,dict) or set(descriptor)!=DESCRIPTOR_FIELDS:
        raise ContractError('DELIVERY_PLAN_MISMATCH','invalid prior release descriptor fields')
    if (descriptor['species']!=species or not isinstance(species,str)
            or not re.fullmatch('[a-z0-9][a-z0-9_-]*',species)
            or any(not identifier(descriptor[k])for k in ('reference_id','annotation_id'))
            or selected!={k:descriptor[k]for k in ('reference_id','annotation_id')}):
        raise ContractError('DELIVERY_PLAN_MISMATCH','prior default reference configuration changed')
    generation=descriptor['generation'];release_id=descriptor['release_id']
    if (type(generation)is not int or generation<1 or not isinstance(release_id,str)
            or not re.fullmatch(r'g[0-9]{6}-[a-f0-9]{10}',release_id)
            or release_id.split('-')[0]!='g{:06d}'.format(generation)
            or not isinstance(descriptor['cohort_hash'],str)
            or not re.fullmatch('[a-f0-9]{64}',descriptor['cohort_hash'])):
        raise ContractError('DELIVERY_PLAN_MISMATCH','invalid frozen prior generation/cohort identity')
    for field,keys in [('files',OUTPUTS),('reference_sha256',{'genome','annotation'})]:
        values=descriptor[field]
        if (not isinstance(values,dict)or set(values)!=keys
                or any(not isinstance(v,str)or not re.fullmatch('[a-f0-9]{64}',v)for v in values.values())):
            raise ContractError('DELIVERY_PLAN_MISMATCH','invalid frozen prior '+field)
    return descriptor


def release_generation(entry):
    value=entry.get('release_id') if isinstance(entry,dict) else None
    if not isinstance(value,str)or not re.fullmatch(r'g[0-9]{6}-[a-f0-9]{10}',value):return None
    return int(value[1:7])


def check_target_version(entry,descriptor):
    """Generations are ordered only inside the same reference namespace."""
    from task_contract import ContractError
    if entry is None:return
    if any(entry[k]!=descriptor[k]for k in ('reference_id','annotation_id')):
        raise ContractError('DELIVERY_PLAN_MISMATCH','managed prior reference namespace changed')
    generation=release_generation(entry)
    if generation is None:
        raise ContractError('DELIVERY_PLAN_MISMATCH','managed prior has unknown release identity')
    if generation>descriptor['generation']:
        raise ContractError('DELIVERY_SUPERSEDED','managed prior already has a newer release; create a current delivery plan')
    if generation==descriptor['generation'] and entry['release_id']!=descriptor['release_id']:
        raise ContractError('DELIVERY_PLAN_MISMATCH','managed prior generation has a different release identity')


def check_current(species,release_dir,descriptor):
    from task_contract import ContractError
    namespace=Path(species)/descriptor['reference_id']/descriptor['annotation_id']
    path=child_path(Path(release_dir),namespace/'current.json')
    if not path.is_file():
        raise ContractError('MISSING_RELEASE','planned prior current release is missing')
    pointer=json_object(path)
    generation=release_generation(pointer)
    if (generation is None or type(pointer.get('generation'))is not int
            or pointer['generation']!=generation):
        raise ContractError('DELIVERY_PLAN_MISMATCH','invalid current prior release identity')
    if generation>descriptor['generation']:
        raise ContractError('DELIVERY_SUPERSEDED','current prior release advanced; create a current delivery plan')
    if any(pointer.get(k)!=descriptor[k]for k in ('release_id','generation','cohort_hash','files')):
        raise ContractError('DELIVERY_PLAN_MISMATCH','current prior release identity or hashes changed')


def encoded(value):
    return (json.dumps(value,sort_keys=True,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode()


def json_object(path):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate JSON key: '+key)
            result[key]=value
        return result
    value=json.loads(Path(path).read_text(),object_pairs_hook=pairs)
    if not isinstance(value,dict):raise ValueError('JSON object required: '+str(path))
    return value


def identifier(value):
    return isinstance(value,str) and bool(value) and value not in {'.','..'} and not any(c in value for c in '/\\\x00\r\n')


def configuration(value):
    if (not isinstance(value,dict) or set(value)!={'schema_version','defaults'}
            or type(value['schema_version'])is not int or value['schema_version']!=1
            or not isinstance(value['defaults'],dict)):
        raise ValueError('invalid prior resource configuration')
    for species,entry in value['defaults'].items():
        if (not re.fullmatch('[a-z0-9][a-z0-9_-]*',species) or not isinstance(entry,dict)
                or set(entry)!={'reference_id','annotation_id'} or not all(identifier(v)for v in entry.values())):
            raise ValueError('invalid default prior reference')
    return value['defaults']


def intervals(data):
    reader=csv.DictReader(io.StringIO(data.decode('utf-8')),delimiter='\t')
    if reader.fieldnames!=FIELDS:raise ValueError('prior resource requires exactly five columns')
    rows=set()
    for row in reader:
        if None in row or any(v is None for v in row.values()):raise ValueError('malformed prior resource row')
        if (not row['gene_id'] or not row['chromosome'] or row['strand']not in {'+','-'}
                or not re.fullmatch('[0-9]+',row['start'])or not re.fullmatch('[0-9]+',row['end'])
                or not 1<=int(row['start'])<=int(row['end'])):
            raise ValueError('invalid prior resource interval')
        key=tuple(row[k]for k in FIELDS)
        if key in rows:raise ValueError('duplicate prior resource interval')
        rows.add(key)
    if not rows:raise ValueError('empty high-confidence prior resource')
    return rows


def release_source(species,release_dir,references_dir,selected,previous=None,descriptor=None):
    from prior_format import load_builtin_ir_prior
    from plantvelo.irprior import load_introns_from_gtf
    reference_id,annotation_id=selected['reference_id'],selected['annotation_id']
    namespace=Path(species)/reference_id/annotation_id
    folder=child_path(Path(release_dir),namespace)
    if descriptor is None:
        pointer_path=child_path(folder,'current.json')
        if not pointer_path.exists():return None
        pointer=json_object(pointer_path)
    else:
        pointer=validate_descriptor(species,descriptor,selected)
    release_id=pointer.get('release_id')
    if not isinstance(release_id,str)or not re.fullmatch(r'g[0-9]{6}-[a-f0-9]{10}',release_id):
        raise ValueError('invalid published prior release ID')
    generation=pointer.get('generation');cohort=pointer.get('cohort_hash')
    if (type(generation)is not int or generation<1 or release_id.split('-')[0]!='g{:06d}'.format(generation)
            or not isinstance(cohort,str)or not re.fullmatch('[a-f0-9]{64}',cohort)):
        raise ValueError('invalid published prior generation/cohort identity')
    if not isinstance(pointer.get('files'),dict) or set(pointer['files'])!=OUTPUTS:
        raise ValueError('incomplete published prior artifacts')
    published=child_path(folder,release_id)
    for name,expected in pointer['files'].items():
        path=child_path(published,name)
        if (not isinstance(expected,str)or not re.fullmatch('[a-f0-9]{64}',expected)
                or not path.is_file()or sha256(path)!=expected):
            raise ValueError('published prior artifact hash mismatch: '+name)
    provenance=json_object(child_path(published,'provenance.json'))
    qc=json_object(child_path(published,'qc.json'))
    if not isinstance(qc.get('plantvelo_validation'),dict) or not isinstance(provenance.get('reference'),dict):
        raise ValueError('invalid published prior provenance/QC objects')
    if (provenance.get('release_id')!=release_id or provenance.get('generation')!=pointer.get('generation')
            or provenance.get('cohort_hash')!=pointer.get('cohort_hash')
            or qc.get('generation')!=pointer.get('generation') or qc.get('cohort_hash')!=pointer.get('cohort_hash')
            or qc.get('plantvelo_validation',{}).get('status')!='passed'):
        raise ValueError('published prior provenance/QC mismatch')
    refroot=child_path(Path(references_dir),namespace)
    meta=json_object(child_path(refroot,'reference.json'))
    reference=provenance['reference']
    if (reference['species']!=species or reference['reference_id']!=reference_id
            or reference['annotation_id']!=annotation_id or meta['species_code']!=species
            or meta['assembly']['name']!=reference_id or meta['annotation']['name']!=annotation_id):
        raise ValueError('prior resource reference namespace mismatch')
    hashes={name:meta['files'][name]['sha256']for name in ('genome','annotation')}
    if any(reference['files'][name]['sha256']!=value for name,value in hashes.items()):
        raise ValueError('published prior reference hash mismatch')
    if descriptor is not None and hashes!=descriptor['reference_sha256']:
        raise ValueError('frozen prior reference hash mismatch')
    if descriptor is not None:
        for name,expected in hashes.items():
            fixed=child_path(refroot,meta['files'][name]['path'])
            if not fixed.is_file()or sha256(fixed)!=expected:
                raise ValueError('fixed prior reference hash mismatch: '+name)
    source=child_path(published,'high_confidence_IR.tsv');data=source.read_bytes();rows=intervals(data)
    loaded=load_builtin_ir_prior(str(source),species)
    if (loaded.sha256!=pointer['files'][source.name]or len(loaded.records)!=len(rows)
            or len(rows)!=qc.get('high_confidence_rows')
            or len(rows)!=qc['plantvelo_validation'].get('matched_introns')):
        raise ValueError('published prior loader/QC row mismatch')
    annotation=child_path(refroot,meta['files']['annotation']['path'])
    if not annotation.is_file()or sha256(annotation)!=hashes['annotation']:
        raise ValueError('fixed prior annotation hash mismatch')
    entry=dict(selected,reference_sha256=hashes,release_id=release_id,
               sha256=pointer['files'][source.name],rows=len(rows))
    from prior_protocol import validate_entry
    validate_entry(species,entry)
    if previous is not None and all(previous[k]==entry[k] for k in ('reference_id','annotation_id','release_id')) and previous!=entry:
        raise ValueError('immutable published prior release identity changed')
    if previous!=entry:
        # Model changed/new resources once; repeated checks retain the verified
        # identity without rebuilding the complete annotation catalogue.
        expected={(i.gene_id,i.chrom,str(i.start0+1),str(i.end0),i.strand)for i in load_introns_from_gtf(str(annotation))}
        if not rows<=expected:raise ValueError('published prior intervals differ from fixed annotation')
    return source,entry


def file_hash(path):
    if path.is_symlink():raise ValueError('symlink in managed prior resource')
    if not path.exists():return None
    if not path.is_file():raise ValueError('non-file in managed prior resource')
    return sha256(path)


def check_stage(stage,owned):
    if not stage.exists():return
    if stage.is_symlink()or not stage.is_dir():raise ValueError('unsafe prior sync staging directory')
    for path in stage.iterdir():
        if path.is_symlink()or not path.is_file():raise ValueError('unknown prior sync staging path')
        if not owned:raise ValueError('unregistered prior sync staging files')
        if path.name in {'prior.tsv','priors.json'}:continue
        if re.fullmatch(r'\.(?:prior\.tsv|priors\.json)-[A-Za-z0-9_]{8}',path.name):
            path.unlink();continue
        raise ValueError('unknown file in prior sync staging: '+path.name)


def journal(data_dir,payload):
    record=dict(payload,identity=hashlib.sha256(encoded(payload)).hexdigest())
    atomic_write(data_dir/PENDING,encoded(record),mode=0o600)


def recover(data_dir,release_dir,defaults,references_dir=None):
    from prior_protocol import validate_entry,read_registry
    from task_contract import ContractError
    marker=child_path(data_dir,PENDING)
    if not marker.exists():return None
    record=json_object(marker);payload={k:v for k,v in record.items()if k!='identity'}
    expected_keys={'schema_version','kind','species','source','entry','before_file','before_registry','registry'}
    if (set(payload) not in (expected_keys,expected_keys|{'descriptor'}) or payload.get('schema_version')!=1
            or payload.get('kind')!='plantvelo_prior_sync'
            or record.get('identity')!=hashlib.sha256(encoded(payload)).hexdigest()):
        raise ValueError('invalid prior resource pending journal; preserve it before recovery')
    species,entry=payload['species'],payload['entry'];validate_entry(species,entry)
    selected={k:entry[k]for k in ('reference_id','annotation_id')}
    if defaults.get(species)!=selected:raise ValueError('pending prior sync default reference changed')
    source=child_path(Path(release_dir),Path(species)/entry['reference_id']/entry['annotation_id']/entry['release_id']/'high_confidence_IR.tsv')
    if str(source)!=payload['source']:raise ValueError('pending prior sync source changed')
    if not source.is_file()or sha256(source)!=entry['sha256']:raise ValueError('pending prior sync source hash mismatch')
    for value in (payload['before_file'],payload['before_registry']):
        if value is not None and (not isinstance(value,str)or not re.fullmatch('[a-f0-9]{64}',value)):
            raise ValueError('invalid pending prior before hash')
    registry=payload['registry']
    if (set(registry)!={'schema_version','species'}or registry['schema_version']!=1
            or not isinstance(registry['species'],dict)or registry['species'].get(species)!=entry):
        raise ValueError('invalid pending prior registry')
    for code,item in registry['species'].items():validate_entry(code,item)
    target=child_path(data_dir,species+'.tsv');catalogue=child_path(data_dir,'priors.json')
    # An old full-registry journal must never revert another completed writer.
    current=read_registry(data_dir,required=False)
    if current is not None:
        for code,item in current['species'].items():
            planned=registry['species'].get(code)
            if planned is None:continue
            if all(item[k]==planned[k]for k in ('reference_id','annotation_id')):
                newer,older=release_generation(item),release_generation(planned)
                if newer is not None and older is not None and newer>older:
                    raise ContractError('DELIVERY_SUPERSEDED','pending prior registry is older than the delivered target; preserve the journal')
    registry_data=encoded(registry);after_registry=hashlib.sha256(registry_data).hexdigest()
    current_file,current_registry=file_hash(target),file_hash(catalogue)
    if current_file not in {payload['before_file'],entry['sha256']}:
        raise ValueError('managed prior resource was modified during recovery')
    if current_registry not in {payload['before_registry'],after_registry}:
        raise ValueError('managed prior registry was modified during recovery')
    if 'descriptor' in payload:
        if references_dir is None:raise ValueError('frozen prior recovery requires reference directory')
        descriptor=validate_descriptor(species,payload['descriptor'],selected)
        prepared=release_source(species,release_dir,references_dir,selected,descriptor=descriptor)
        if prepared[1]!=entry:raise ValueError('pending frozen prior release identity changed')
    def check_preimage(expected_file):
        if file_hash(target)!=expected_file:
            raise ValueError('managed prior resource was modified during recovery')
        if file_hash(catalogue)!=current_registry:
            raise ValueError('managed prior registry was modified during recovery')
    check_preimage(current_file)
    stage=child_path(data_dir,STAGE);check_stage(stage,True);stage.mkdir(exist_ok=True)
    if current_file!=entry['sha256']:
        atomic_write(stage/'prior.tsv',source.read_bytes(),mode=0o644)
        if sha256(stage/'prior.tsv')!=entry['sha256']:raise ValueError('prior changed while staging')
        check_preimage(current_file)
        os.replace(stage/'prior.tsv',target);sync_directory(data_dir);sync_directory(stage)
    if current_registry!=after_registry:
        atomic_write(stage/'priors.json',registry_data,mode=0o644)
        check_preimage(entry['sha256'])
        os.replace(stage/'priors.json',catalogue);sync_directory(data_dir);sync_directory(stage)
    if sha256(target)!=entry['sha256']or sha256(catalogue)!=after_registry:
        raise ValueError('prior sync commit hash mismatch')
    # Remove staging before the journal: a crash here is still replayable.
    check_stage(stage,True)
    for path in stage.iterdir():path.unlink()
    stage.rmdir();sync_directory(data_dir)
    marker.unlink();sync_directory(data_dir)
    return {'status':'synchronized','species':species,'release_id':entry['release_id'],
            'sha256':entry['sha256'],'rows':entry['rows'],'recovered':True}


def sync_species(species,release_dir,references_dir,data_dir,config,dry_run=False,descriptor=None):
    defaults=configuration(config)
    if descriptor is not None:
        descriptor=deepcopy(validate_descriptor(species,descriptor,defaults.get(species)))
    if species not in defaults:return {'status':'not_configured','species':species}
    release_dir=Path(release_dir).expanduser().absolute();references_dir=Path(references_dir).expanduser().absolute()
    data_dir=Path(data_dir).expanduser().absolute()
    if data_dir.is_symlink():raise ValueError('symlink resource directory')
    if dry_run:
        selected=defaults[species];pointer=child_path(release_dir,Path(species)/selected['reference_id']/selected['annotation_id']/'current.json')
        if descriptor is not None:
            from prior_protocol import resource_lock,read_registry
            with resource_lock(data_dir)if data_dir.exists()else nullcontext():
                registry=read_registry(data_dir,required=False)or {'species':{}}
                old_entry=registry['species'].get(species)
                check_target_version(old_entry,descriptor)
                check_current(species,release_dir,descriptor)
                if old_entry is not None and file_hash(child_path(data_dir,species+'.tsv'))!=old_entry['sha256']:
                    raise ValueError('managed prior resource hash was modified')
                release_source(species,release_dir,references_dir,selected,previous=old_entry,descriptor=descriptor)
                check_current(species,release_dir,descriptor)
        return {'status':'planned','species':species,'data_dir':str(data_dir),'published':pointer.is_file()}
    data_dir.mkdir(parents=True,exist_ok=True)
    from prior_protocol import resource_lock,read_registry
    recovered=None
    with resource_lock(data_dir,exclusive=True):
        recovered=recover(data_dir,release_dir,defaults,references_dir=references_dir)
        check_stage(child_path(data_dir,STAGE),False)
        catalogue=child_path(data_dir,'priors.json');target=child_path(data_dir,species+'.tsv')
        registry=read_registry(data_dir,required=False)or {'schema_version':1,'species':{}}
        before_file=file_hash(target);before_registry=file_hash(catalogue);old_entry=registry['species'].get(species)
        if descriptor is not None:
            check_target_version(old_entry,descriptor)
            check_current(species,release_dir,descriptor)
        if old_entry is not None and before_file!=old_entry['sha256']:
            raise ValueError('managed prior resource hash was modified')
        prepared=release_source(species,release_dir,references_dir,defaults[species],previous=old_entry,descriptor=descriptor)
        if prepared is None:return {'status':'no_release','species':species}
        source,entry=prepared
        if file_hash(target)!=before_file:
            raise ValueError('managed prior resource hash was modified during validation')
        if file_hash(catalogue)!=before_registry:
            raise ValueError('managed prior registry was modified during validation')
        if descriptor is not None:check_current(species,release_dir,descriptor)
        if old_entry is None and before_file is not None and intervals(target.read_bytes())!=intervals(source.read_bytes()):
            raise ValueError('unmanaged prior resource differs from the published interval set')
        if old_entry==entry and before_file==entry['sha256']:
            if recovered and recovered['species']==species and recovered['release_id']==entry['release_id']:return recovered
            return {'status':'up_to_date','species':species,'release_id':entry['release_id'],'sha256':entry['sha256'],'rows':entry['rows']}
        updated=dict(registry,species=dict(registry['species'],**{species:entry}))
        payload={'schema_version':1,'kind':'plantvelo_prior_sync','species':species,'source':str(source),
                 'entry':entry,'before_file':before_file,'before_registry':before_registry,'registry':updated}
        if descriptor is not None:
            check_current(species,release_dir,descriptor)
            payload['descriptor']=descriptor
        journal(data_dir,payload)
        result=recover(data_dir,release_dir,defaults,references_dir=references_dir);result['recovered']=False
        if descriptor is not None:check_current(species,release_dir,descriptor)
        return result


def sync_for_namespace(species,reference_id,annotation_id,release_dir,references_dir,data_dir,config,dry_run=False,descriptor=None):
    defaults=configuration(config)
    if descriptor is not None:
        validate_descriptor(species,descriptor,{'reference_id':reference_id,'annotation_id':annotation_id})
    if defaults.get(species)!={'reference_id':reference_id,'annotation_id':annotation_id}:
        return {'status':'not_default_reference','species':species,'reference_id':reference_id,'annotation_id':annotation_id}
    return sync_species(species,release_dir,references_dir,data_dir,config,dry_run=dry_run,descriptor=descriptor)
