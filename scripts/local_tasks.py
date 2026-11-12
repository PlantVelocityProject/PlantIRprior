"""Durable, bounded IR update requests; scientific state remains in IRagent."""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import subprocess
import threading
import sqlite3

from delivery_store import DeliveryStore,capture_plan,file_hash,runtime_plan
from task_contract import ContractError

from task_contract import error_record, normalize_task_receipt, phase_for

ACTIVE = {'queued', 'running', 'unknown'}
SPECIES = {'ath', 'gmx', 'osa', 'sly', 'zma'}
DEFAULTS = dict(max_samples=10, discovery_limit=100, skip_discovery=False,
                discovery_source='auto', download_backend='auto', download_timeout=900,
                download_retries=2, accession=[])


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value):
        raise ValueError('invalid request/task identifier')
    return value


def authorized(uid, allowed):
    return uid in allowed


def json_object(text, max_depth=64):
    def finite_float(token):
        value=float(token)
        if not math.isfinite(value):raise ValueError('nonfinite JSON number')
        return value
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result
    value = json.loads(text, object_pairs_hook=pairs, parse_float=finite_float,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON number')))
    if not isinstance(value, dict):
        raise ValueError('JSON object required')
    pending=[(value,0)]
    while pending:
        item,depth=pending.pop()
        if depth>max_depth:raise ValueError('JSON nesting exceeds supported receipt depth')
        children=item.values() if isinstance(item,dict) else item if isinstance(item,list) else ()
        pending.extend((child,depth+1)for child in children)
    return value


def atomic_json(path, value):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=str(path.parent))
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, sort_keys=True, ensure_ascii=False, allow_nan=False)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, str(path))
        fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def normalize_params(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS) - {'species','publish_only'}:
        raise ValueError('unsupported update parameters')
    result = dict(DEFAULTS, **value)
    species = result.get('species')
    if not isinstance(species, list) or not species or any(not isinstance(s, str) or s not in SPECIES for s in species):
        raise ValueError('species must contain supported species codes')
    result['species'] = sorted(set(species))
    for key, lower, upper in [('max_samples', 1, 10), ('discovery_limit', 1, 100),
                              ('download_timeout', 1, 7200), ('download_retries', 0, 3)]:
        if type(result[key]) is not int or not lower <= result[key] <= upper:
            raise ValueError(key + ' is outside the permitted integer budget')
    for key in ('skip_discovery','publish_only'):
        if type(result.get(key,False)) is not bool:raise ValueError(key+' must be boolean')
    if result['discovery_source'] not in ('auto', 'ncbi', 'ena', 'ddbj', 'ngdc'):
        raise ValueError('unsupported metadata source')
    if result['download_backend'] not in ('auto', 'kingfisher', 'qtransc', 'ena-https'):
        raise ValueError('unsupported download backend')
    accessions = result['accession']
    if (not isinstance(accessions, list) or len(accessions) > 100 or
            any(not isinstance(a, str) or not re.fullmatch(r'(?:SRR|ERR|DRR|CRR)[0-9]+', a) for a in accessions)):
        raise ValueError('invalid accession selection')
    result['accession'] = sorted(set(accessions))
    if result.get('publish_only') and result['accession']:
        raise ValueError('publication uses complete reference groups; accession selection is not supported')
    if not result.get('publish_only'):result.pop('publish_only',None)
    return result


def normalize_delivery_params(value):
    if not isinstance(value,dict)or set(value)-{'source_task','species','targets'}:raise ValueError('unsupported delivery parameters')
    source=value.get('source_task');species=value.get('species',[]);targets=value.get('targets')
    if source is not None:identifier(source)
    if not isinstance(species,list)or any(not isinstance(code,str)or code not in SPECIES for code in species):raise ValueError('unsupported delivery species')
    if bool(source)==bool(species):raise ValueError('choose source_task or species')
    if not isinstance(targets,list)or not targets or any(target not in ('resources','website')for target in targets):raise ValueError('choose resources and/or website')
    return dict(source_task=source,species=sorted(set(species)),targets=sorted(set(targets)))



class TaskStore:
    def __init__(self, project, launcher, activity):
        self.project = Path(project).resolve()
        self.base = self.project / 'state/agent'
        self.tasks = self.base / 'tasks'
        self.tasks.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.tasks.is_symlink(): raise ValueError('symlink task directory')
        self.records = self.tasks / 'records'
        self.records.mkdir(mode=0o700, exist_ok=True)
        self.launcher, self.activity = launcher, activity
        self.check_capacity = lambda: None
        self.lock = threading.RLock()

    def task_path(self, task_id):
        return self.records / (identifier(task_id) + '.json')

    def receipt_path(self, task_id, number):
        return self.tasks / (identifier(task_id) + '.a' + str(number) + '.result.json')

    def load(self, task_id):
        path = self.task_path(task_id)
        if path.is_symlink() or not path.is_file(): raise ValueError('unknown or unsafe task: ' + task_id)
        return json_object(path.read_text())

    def save(self, task):
        atomic_json(self.task_path(task['id']), task)

    def fingerprint(self, params):
        if 'job_id'in params:
            with DeliveryStore(self.project/'state/updates.sqlite3',create=False)as store:plan=runtime_plan(store.get(params['job_id'])['plan'],self.project)
            if not set(params['targets'])<=set(plan['targets']):raise ValueError('delivery target expansion')
            if file_hash(plan['config_path'])!=plan['config_sha256']:raise ValueError('delivery configuration changed')
            result=dict(job_id=params['job_id'],targets=params['targets'],config_sha256=plan['config_sha256'])
            image=self.project/'containers/images/plantiragent-v1.sif'
            if image.exists():
                result['image_checksum']=file_hash(Path(str(image)+'.sha256'))
                stat=image.stat();result['image_stat']=[stat.st_size,stat.st_mtime_ns,stat.st_ino]
            return result
        paths = [self.project / 'configs' / (name + '.json') for name in
                 ('alignment', 'evidence', 'aggregation', 'classification', 'prior_resources', 'evidence_identity_migrations')]
        defaults = json_object((self.project / 'configs/prior_resources.json').read_text())['defaults']
        for code in params['species']:
            selected = defaults[code]
            paths.append(self.project / 'storage/references' / code / selected['reference_id'] / selected['annotation_id'] / 'reference.json')
        image = self.project / 'containers/images/plantiragent-v1.sif'
        if image.exists():
            paths.append(Path(str(image) + '.sha256'))
        result = {str(p.relative_to(self.project)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        if image.exists():
            stat = image.stat(); result['image_stat'] = [stat.st_size, stat.st_mtime_ns, stat.st_ino]
        return result

    def command(self, params, batch_id=None, preview=False):
        if 'job_id'in params:
            args=['bash',str(self.project/'scripts/run_delivery.sh'),'--job-id',params['job_id'],'--json']
            for target in params['targets']:args+=['--target',target]
            if preview:args.append('--dry-run')
            return args
        args = ['bash', str(self.project / 'scripts/run_update.sh'), '--json']
        for species in params['species']: args += ['--species', species]
        for accession in params['accession']: args += ['--accession', accession]
        for name in ('max_samples', 'discovery_limit', 'discovery_source', 'download_backend', 'download_timeout', 'download_retries'):
            args += ['--' + name.replace('_', '-'), str(params[name])]
        if params['skip_discovery']: args.append('--skip-discovery')
        if params.get('publish_only'):args.append('--publish-only')
        if batch_id: args += ['--batch-id', batch_id]
        if preview: args.append('--dry-run')
        return args

    def status(self, task_id, tail=0):
        with self.lock:
            task = self.load(task_id)
            latest = task['attempts'][-1]
            receipt = self.receipt_path(task_id, latest['number'])
            result = None
            invalid = None
            try:
                if receipt.is_symlink(): raise ValueError('symlinked terminal receipt')
                if receipt.exists():
                    if not receipt.is_file() or receipt.stat().st_size > 4*1024*1024:
                        raise ValueError('invalid or oversized terminal receipt')
                    result = normalize_task_receipt(json_object(receipt.read_text()),task_id,task.get('batch_id'),latest['number'],operation=task.get('kind','update'),job_id=task.get('job_id'))
            except (OSError,ValueError,RuntimeError,KeyError,TypeError) as exc:
                invalid = str(exc)
            errors = result['errors'] if result else []
            if result:
                execution=result['execution']
            else:
                try: active=self.activity(latest['unit'])
                except (OSError,RuntimeError,ValueError,subprocess.SubprocessError): active='unknown'
                execution={'active':dict(state='running',outcome=None),
                           'inactive':dict(state='stopped',outcome='failed' if invalid else 'interrupted')}.get(
                               active,dict(state='unknown',outcome=None))
                if invalid:errors=[error_record('RECEIPT_INVALID','receipt',invalid)]
                elif active=='inactive':errors=[error_record('RECEIPT_MISSING','receipt','task unit is inactive without a terminal receipt')]
            answer = dict(id=task_id, batch_id=task.get('batch_id'), operation=task.get('kind','update'),job_id=task.get('job_id'), phase=phase_for(execution), execution=execution,
                          attempt=latest['number'], params=task['params'], unit=latest['unit'], created_at=task['created_at'],
                          sample_budget_per_attempt=0 if task['params'].get('publish_only')else task['params'].get('max_samples',0),
                          total_attempt_budget=0 if task['params'].get('publish_only')else len(task['attempts']) * task['params'].get('max_samples',0),
                          receipt=str(receipt), result=result.get('result') if result else None,
                          result_replayed=result.get('result_replayed') if result else None,errors=errors,
                          error=result.get('error') if result else invalid or task.get('dispatch_error'),
                          returncode=result.get('returncode') if result else None)
            if tail:
                if type(tail) is not int or not 0 <= tail <= 8192: raise ValueError('tail outside permitted range')
                path = self.tasks / (task_id + '.a' + str(latest['number']) + '.stderr.log')
                if path.exists():
                    with path.open('rb') as stream:
                        stream.seek(max(0, path.stat().st_size-tail)); answer['stderr_tail'] = stream.read(tail).decode('utf-8', errors='replace')
            return answer

    def assert_idle(self):
        for path in self.records.glob('*.json'):
            task = json_object(path.read_text())
            if self.status(task['id'])['phase'] in ACTIVE:
                raise RuntimeError('another task is running or unconfirmed: ' + task['id'])

    def dispatch(self, task):
        self.check_capacity()
        number = len(task['attempts']) + 1
        tag = hashlib.sha256(task['id'].encode()).hexdigest()[:24]
        unit = 'plantir-task-' + tag + '-a' + str(number) + '.service'
        task['attempts'].append(dict(number=number, unit=unit, submitted_at=now()))
        self.save(task)  # Admission precedes systemd dispatch; a lost reply reuses this identity.
        try: self.launcher(task, number)
        except (OSError, RuntimeError) as exc:
            task['dispatch_error'] = str(exc); self.save(task)
            # Dispatch may have succeeded before the transport failed. Never auto-retry it.
        return self.status(task['id'])

    def start(self, request_id, params):
        with self.lock:
            task_id = identifier(request_id); params = normalize_params(params)
            path = self.task_path(task_id)
            if path.exists() or path.is_symlink():
                task = self.load(task_id)
                if task['params'] != params: raise ValueError('request ID already binds a different selection')
                return self.status(task_id)
            self.assert_idle()
            task = dict(id=task_id, kind='update', params=params, batch_id='la-' + hashlib.sha256(task_id.encode()).hexdigest()[:32],
                        fingerprint=self.fingerprint(params), created_at=now(), attempts=[], resumes={})
            return self.dispatch(task)

    def resume(self, task_id, request_id):
        with self.lock:
            identifier(request_id); task = self.load(task_id); status = self.status(task_id)
            if request_id in task['resumes'] or status['phase'] == 'complete': return status
            if status['phase'] in ACTIVE: raise RuntimeError('task is active or execution state is unknown')
            if task.get('kind','update')=='update':
                result=status.get('result')or{}
                saved=self.scientific_checkpoint(task.get('batch_id'))
                science=result.get('science_outcome',saved.get('science_outcome'))
                job=result.get('delivery_job_id',saved.get('delivery_job_id'))
                delivery_errors=any(e.get('stage')in ('delivery','prior_sync','website_sync')for e in result.get('errors',[])if isinstance(e,dict))
                if science=='success' or (science is None and delivery_errors):
                    exc=ContractError('DELIVERY_ONLY_REPAIR_REQUIRED','scientific work is complete or recovery scope is unproven; use deliver instead of resuming science; job='+str(job))
                    exc.job_id=job
                    raise exc
            if len(task['attempts']) >= 4: raise ValueError('four attempts exhausted; inspect the failure before further work')
            self.assert_idle()
            if self.fingerprint(task['params']) != task['fingerprint']: raise ValueError('configuration/reference/image identity changed')
            task['resumes'][request_id] = len(task['attempts']) + 1
            return self.dispatch(task)

    def scientific_checkpoint(self,batch_id):
        path=self.project/'state/updates.sqlite3'
        if not batch_id or not path.is_file():return {}
        db=None
        try:
            db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
            row=db.execute('SELECT payload FROM cycles WHERE id=?',(batch_id,)).fetchone()
            return json_object(row[0])if row else{}
        except (ValueError,sqlite3.Error):return {}
        finally:
            if db is not None:db.close()

    def deliver(self,request_id,selection):
        with self.lock:
            task_id=identifier(request_id);selection=normalize_delivery_params(selection)
            path=self.task_path(task_id)
            if path.exists()or path.is_symlink():
                task=self.load(task_id)
                if task.get('kind')!='delivery'or task.get('delivery_request')!=selection:raise ValueError('request ID already binds another operation or selection')
                return self.status(task_id)
            self.assert_idle();self.check_capacity()
            if selection['source_task']:
                source=self.load(selection['source_task'])
                if source.get('kind','update')=='delivery':job_id=source['job_id']
                else:
                    result=self.status(source['id']).get('result')or{}
                    checkpoint=self.scientific_checkpoint(source.get('batch_id'))
                    job_id=result.get('delivery_job_id')or checkpoint.get('delivery_job_id')
                if not job_id:raise ValueError('source has no pinned delivery plan; explicitly select species to create a current-release plan')
                with DeliveryStore(self.project/'state/updates.sqlite3',create=False)as store:plan=store.get(job_id)['plan']
                if not set(selection['targets'])<=set(plan['targets']):raise ValueError('cannot add targets to the source delivery plan')
            else:
                plan=capture_plan(selection['species'],self.project/'releases',self.project/'storage/references',
                    self.project/'plantvelo/data',self.project/'website',self.project/'configs/prior_resources.json',targets=selection['targets'],project_root=self.project)
                with DeliveryStore(self.project/'state/updates.sqlite3')as store:job_id=store.register(plan)
            params=dict(job_id=job_id,targets=selection['targets'])
            task=dict(id=task_id,kind='delivery',params=params,delivery_request=selection,job_id=job_id,batch_id=None,
                fingerprint=self.fingerprint(params),created_at=now(),attempts=[],resumes={})
            return self.dispatch(task)
