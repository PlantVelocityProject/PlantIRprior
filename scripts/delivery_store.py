"""Immutable delivery plans and short checkpoints in the existing update database."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from task_contract import ContractError

OUTPUTS = {'high_confidence_IR.tsv','intron_evidence.tsv.gz','provenance.json','qc.json'}
SHA = re.compile(r'[a-f0-9]{64}\Z')
JOB = re.compile(r'dj-[a-f0-9]{40}\Z')
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')


def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def file_hash(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(8*1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def read_json(path):
    def unique(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate JSON key: '+key)
            result[key]=value
        return result
    return json.loads(Path(path).read_text(),object_pairs_hook=unique,
        parse_constant=lambda token:(_ for _ in ()).throw(ValueError('nonfinite JSON number')))


def validate_descriptor(value):
    fields={'species','reference_id','annotation_id','release_id','generation','cohort_hash','files','reference_sha256'}
    if not isinstance(value,dict) or set(value)!=fields:raise ValueError('invalid release descriptor fields')
    if any(not isinstance(value[k],str) or not IDENTIFIER.fullmatch(value[k]) for k in ('species','reference_id','annotation_id')):
        raise ValueError('invalid release namespace')
    if type(value['generation']) is not int or value['generation']<1:raise ValueError('invalid release generation')
    if not isinstance(value['release_id'],str) or not re.fullmatch(r'g[0-9]{6}-[a-f0-9]{10}',value['release_id']) or not value['release_id'].startswith('g{:06d}-'.format(value['generation'])):
        raise ValueError('invalid release identity')
    for key,fields in [('files',OUTPUTS),('reference_sha256',{'genome','annotation'})]:
        if not isinstance(value[key],dict) or set(value[key])!=fields:raise ValueError('incomplete release hashes')
        if any(not isinstance(v,str) or not SHA.fullmatch(v)for v in value[key].values()):raise ValueError('invalid release hashes')
    if not isinstance(value['cohort_hash'],str) or not SHA.fullmatch(value['cohort_hash']):raise ValueError('invalid release cohort')
    return value


def validate_plan(plan):
    fields={'schema_version','kind','protocol_version','config','config_sha256','config_path','release_dir',
            'references_dir','resources_dir','website_dir','targets','resources','skipped_resources','website_releases','missing_website'}
    if not isinstance(plan,dict) or set(plan)!=fields or type(plan['schema_version'])is not int or plan['schema_version']!=1 or plan['kind']!='delivery_plan' or type(plan['protocol_version'])is not int or plan['protocol_version']!=1:
        raise ValueError('unsupported delivery plan')
    from prior_sync import configuration
    defaults=configuration(plan['config'])
    if not isinstance(plan['config_sha256'],str) or not SHA.fullmatch(plan['config_sha256']):raise ValueError('invalid config identity')
    for key in ('config_path','release_dir','references_dir','resources_dir','website_dir'):
        if not isinstance(plan[key],str) or not Path(plan[key]).is_absolute():raise ValueError('delivery paths must be absolute')
    targets=plan['targets']
    if not isinstance(targets,list) or not targets or any(t not in ('resources','website') for t in targets) or targets!=sorted(set(targets)):
        raise ValueError('invalid delivery targets')
    for key in ('resources','website_releases'):
        if not isinstance(plan[key],dict):raise ValueError('invalid release target map')
        for code,descriptor in plan[key].items():
            if descriptor is None:
                if key!='resources' or code not in defaults:raise ValueError('invalid missing resource')
                continue
            validate_descriptor(descriptor)
            if descriptor['species']!=code or defaults.get(code)!={k:descriptor[k]for k in ('reference_id','annotation_id')}:
                raise ValueError('release descriptor/default reference mismatch')
    if not isinstance(plan['skipped_resources'],dict):raise ValueError('invalid skipped resources')
    for code,namespace in plan['skipped_resources'].items():
        if not isinstance(namespace,dict) or set(namespace)!={'reference_id','annotation_id'} or not isinstance(code,str) or not IDENTIFIER.fullmatch(code) or any(not isinstance(v,str) or not IDENTIFIER.fullmatch(v)for v in namespace.values()):raise ValueError('invalid skipped namespace')
        if code in plan['resources']:raise ValueError('resource cannot be both selected and skipped')
    missing=plan['missing_website']
    if not isinstance(missing,list) or any(not isinstance(v,str)for v in missing) or missing!=sorted(set(missing)):
        raise ValueError('invalid missing website releases')
    if 'website' in targets and set(plan['website_releases'])|set(missing)!=set(defaults):raise ValueError('incomplete website version vector')
    if set(plan['website_releases'])&set(missing):raise ValueError('website release cannot be ready and missing')
    if 'resources' not in targets and (plan['resources'] or plan['skipped_resources']):raise ValueError('unauthorized resource targets')
    if 'website' not in targets and (plan['website_releases'] or missing):raise ValueError('unauthorized website targets')
    return plan


def job_identity(plan):
    validate_plan(plan)
    return 'dj-'+hashlib.sha256(encoded(plan).encode()).hexdigest()[:40]


def capture_descriptor(code,selected,release_dir,references_dir):
    from download_reads import child_path
    namespace=Path(code)/selected['reference_id']/selected['annotation_id']
    folder=child_path(Path(release_dir),namespace)
    pointer=child_path(folder,'current.json')
    if not pointer.is_file():return None
    record=read_json(pointer)
    # Validate the release id before using it as a path.
    release_id=record.get('release_id')
    if not isinstance(release_id,str) or not re.fullmatch(r'g[0-9]{6}-[a-f0-9]{10}',release_id):raise ValueError('invalid current release')
    published=child_path(folder,release_id)
    if not isinstance(record.get('files'),dict) or set(record['files'])!=OUTPUTS:raise ValueError('incomplete current release')
    for name,expected in record['files'].items():
        if file_hash(child_path(published,name))!=expected:raise ValueError('published source hash mismatch: '+name)
    provenance=read_json(child_path(published,'provenance.json'))
    reference=read_json(child_path(Path(references_dir),namespace/'reference.json'))
    descriptor=dict(species=code,**selected,release_id=release_id,generation=record.get('generation'),
        cohort_hash=record.get('cohort_hash'),files=record['files'],
        reference_sha256={k:reference['files'][k]['sha256']for k in ('genome','annotation')})
    validate_descriptor(descriptor)
    ref=provenance['reference']
    if (ref['species']!=code or ref['reference_id']!=selected['reference_id'] or ref['annotation_id']!=selected['annotation_id']
            or any(ref['files'][k]['sha256']!=descriptor['reference_sha256'][k]for k in ('genome','annotation'))
            or provenance['release_id']!=release_id or provenance['cohort_hash']!=descriptor['cohort_hash']):raise ValueError('published provenance identity mismatch')
    return descriptor


PATH_FIELDS=('config_path','release_dir','references_dir','resources_dir','website_dir')
CANONICAL_ROOT=Path('/workspace/PlantIRagent')


def runtime_plan(plan,project_root):
    """Resolve immutable mount-relative project paths for the current process."""
    value=deepcopy(plan)
    for key in PATH_FIELDS:
        path=Path(value[key])
        try:relative=path.relative_to(CANONICAL_ROOT)
        except ValueError:continue
        value[key]=str(Path(project_root).resolve()/relative)
    return value


def capture_plan(species,release_dir,references_dir,resources_dir,website_dir,config_path,targets=None,groups=None,project_root=None):
    from prior_sync import configuration
    config_path=Path(config_path).expanduser().resolve();config=read_json(config_path);defaults=configuration(config)
    targets=sorted(set(targets or ['resources','website']))
    if any(t not in ('resources','website')for t in targets):raise ValueError('invalid delivery target')
    codes=sorted(set(species) if species else defaults)
    resources={};skipped={};website={};missing=[];cache={}
    def source(code):
        if code not in cache:cache[code]=capture_descriptor(code,defaults[code],release_dir,references_dir)
        return cache[code]
    if 'resources'in targets:
        for code in codes:
            selected=groups.get(code)if groups else None
            if code not in defaults or (selected and tuple(selected)!=(defaults[code]['reference_id'],defaults[code]['annotation_id'])):
                if selected:skipped[code]=dict(reference_id=selected[0],annotation_id=selected[1])
                else:raise ValueError('unconfigured resource species: '+code)
            else:resources[code]=source(code)
    if 'website'in targets:
        for code in sorted(defaults):
            descriptor=source(code)
            if descriptor is None:missing.append(code)
            else:website[code]=descriptor
    plan=dict(schema_version=1,kind='delivery_plan',protocol_version=1,config=config,
        config_sha256=file_hash(config_path),config_path=str(config_path),
        release_dir=str(Path(release_dir).expanduser().resolve()),references_dir=str(Path(references_dir).expanduser().resolve()),
        resources_dir=str(Path(resources_dir).expanduser().resolve()),website_dir=str(Path(website_dir).expanduser().resolve()),
        targets=targets,resources=resources,skipped_resources=skipped,website_releases=website,missing_website=missing)
    if project_root is not None:
        root=Path(project_root).resolve()
        for key in PATH_FIELDS:
            try:relative=Path(plan[key]).relative_to(root)
            except ValueError:continue
            plan[key]=str(CANONICAL_ROOT/relative)
    return validate_plan(plan)


class DeliveryStore:
    def __init__(self,path,connection=None,create=True):
        self.owns=connection is None
        if connection is None:
            path=Path(path).resolve()
            if create:path.parent.mkdir(parents=True,exist_ok=True)
            self.db=sqlite3.connect(str(path)if create else path.as_uri()+'?mode=ro',uri=not create)
        else:self.db=connection
        if create:
            self.db.execute('CREATE TABLE IF NOT EXISTS delivery_jobs(id TEXT PRIMARY KEY,plan TEXT NOT NULL,status TEXT NOT NULL,target_state TEXT NOT NULL,updated_at TEXT NOT NULL)')
            if self.owns:self.db.commit()

    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    def close(self):
        if self.owns:self.db.close()

    def register(self,plan):
        name=job_identity(plan)
        with self.db if self.owns else nullcontext():
            row=self.db.execute('SELECT plan FROM delivery_jobs WHERE id=?',(name,)).fetchone()
            if row:
                if row[0]!=encoded(plan):raise ValueError('delivery plan identity collision')
                return name
            states={}
            if 'website'in plan['targets']:
                for previous_plan,previous_state in self.db.execute('SELECT plan,target_state FROM delivery_jobs ORDER BY updated_at DESC'):
                    previous=json.loads(previous_plan);progress=json.loads(previous_state)
                    if (previous.get('website_dir')==plan['website_dir'] and previous.get('config_sha256')==plan['config_sha256']
                            and progress.get('website',{}).get('status')=='succeeded'):
                        cache=progress['website'].get('result',{}).get('verification_cache')
                        if isinstance(cache,dict):
                            states['website']=dict(status='pending',result=dict(verification_cache=cache))
                            break
            self.db.execute('INSERT INTO delivery_jobs VALUES (?,?,?,?,?)',
                (name,encoded(plan),'pending',encoded(states),datetime.now(timezone.utc).isoformat()))
        return name

    def get(self,name):
        if not isinstance(name,str) or not JOB.fullmatch(name):raise ValueError('invalid delivery job identifier')
        row=self.db.execute('SELECT id,plan,status,target_state,updated_at FROM delivery_jobs WHERE id=?',(name,)).fetchone()
        if row is None:raise ValueError('unknown delivery job')
        value=dict(zip(('id','plan','status','target_state','updated_at'),row))
        value['plan']=json.loads(value['plan']);value['target_state']=json.loads(value['target_state'])
        if job_identity(value['plan'])!=name:raise ValueError('delivery plan was modified')
        if not isinstance(value['target_state'],dict):raise ValueError('invalid delivery checkpoints')
        return value

    def checkpoint(self,name,target,state,result):
        if state not in {'pending','running','succeeded','failed','skipped','superseded'}:raise ValueError('invalid delivery checkpoint')
        with self.db if self.owns else nullcontext():
            value=self.get(name)
            plan=value['plan'];allowed={'resources/'+code for code in set(plan['resources'])|set(plan['skipped_resources'])}
            if 'website'in plan['targets']:allowed.add('website')
            if target not in allowed:raise ValueError('target not authorized by delivery plan')
            states=value['target_state'];states[target]=dict(status=state,result=result)
            statuses=[v['status']for v in states.values()]
            overall='running'if 'running'in statuses else 'partial'if any(s in {'failed','superseded'}for s in statuses)else 'pending'
            if allowed and set(states)==allowed and all(s in {'succeeded','skipped'}for s in statuses):overall='complete'
            self.db.execute('UPDATE delivery_jobs SET status=?,target_state=?,updated_at=? WHERE id=?',
                (overall,encoded(states),datetime.now(timezone.utc).isoformat(),name))
