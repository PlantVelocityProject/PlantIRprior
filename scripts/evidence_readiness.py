"""Read-only scientific evidence inspection and disposable dependency-aware cache.

The cache records positive validated facts, never workflow state or permission to
repair data. Callers own cohort membership, publication recovery and cleanup.
"""
from collections.abc import Mapping
import csv
from dataclasses import asdict, dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
import sqlite3

from download_reads import check_cancel, child_path, now, sha256, species_code
from evidence_admission import imported_signature, load_admission, validate_cohort
from evidence_contract import native_signature, validate_parameters, validate_rows,DEFAULT_PARAMETERS
from evidence_identity import MIGRATIONS, canonical_software, software_policy


@dataclass(frozen=True)
class EvidenceDecision:
    state: str
    reason: str
    kind: str
    signature: object = None
    identity: object = None
    differences: tuple = ()

    def to_dict(self):
        return asdict(self)

    @property
    def message(self):
        if self.differences:
            return '; '.join(str(v) for v in self.differences)[:1800]
        return {'ready':'registered evidence satisfies the scientific contract',
                'missing':'no registered evidence in the selected reference namespace',
                'unchecked':'evidence contents have not been verified'}.get(self.state, self.reason)


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _same_facts(left, right):
    # A current request is an interpretation of validated source facts. Keep it
    # in the receipt, but always reapply the matcher rather than reparse TSVs
    # when another stage needs the same facts without a new extraction request.
    return (isinstance(left, dict) and isinstance(right, dict)
            and {k:v for k,v in left.items() if k != 'request'}
            == {k:v for k,v in right.items() if k != 'request'})


def _match_request(decision, request):
    if decision.kind != 'native' or request is None:
        return decision
    signature, differences = decision.signature, []
    for key,value in validate_parameters(request['parameters']).items():
        actual = signature['parameters'].get(key)
        if actual != value:
            differences.append('{} expected {}, found {}'.format(key,value,actual))
    if canonical_software(signature['software']) != canonical_software(request['software']):
        differences.append('actual extraction software source identity differs from current request')
    if not differences:
        return decision
    differences.append('explicit rebuild or matching scientific configuration required')
    return EvidenceDecision('incompatible','EVIDENCE_INCOMPATIBLE',decision.kind,
                            signature,decision.identity,tuple(differences))


def _safe(path):
    """Reject symlinks in the root as well as every managed path component."""
    path = Path(path).absolute()
    for parent in reversed((path,) + tuple(path.parents)):
        if parent.is_symlink():
            raise ValueError('symlink in managed evidence/reference path: ' + str(parent))
    return path


def _stamp(path):
    stat = _safe(path).stat()
    if not Path(path).is_file():
        raise ValueError('managed evidence/reference path is not a file: ' + str(path))
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise ValueError('invalid registered evidence hash')
    return value


@lru_cache(maxsize=1)
def current_software():
    """Probe the actual selected scientific environment, never a stored report."""
    from evidence_validation import identity
    return software_policy(identity())


class _Request(Mapping):
    def __init__(self, parameters, software):
        self.parameters, self.software = parameters, software
    def __iter__(self):
        return iter(('parameters', 'software'))
    def __len__(self):
        return 2
    def __getitem__(self, key):
        if key == 'parameters': return self.parameters
        if key == 'software':
            if self.software is None: self.software = current_software()
            return software_policy(self.software)
        raise KeyError(key)


def request_for(config_path, parameters=None, software=None):
    """Read effective parameters now; defer the runtime probe until needed."""
    if parameters is None:
        config = json.loads(Path(config_path).read_text())
        if(not isinstance(config,dict)or set(config)-{'schema_version','parameters'}
                or type(config.get('schema_version'))is not int or config['schema_version']!=1):
            raise ValueError('unsupported evidence configuration schema')
        configured=config.get('parameters',{})
        if not isinstance(configured,dict)or set(configured)-set(DEFAULT_PARAMETERS):
            raise ValueError('unknown evidence parameters')
        parameters = dict(DEFAULT_PARAMETERS,**configured)
    return _Request(validate_parameters(parameters), software)


def build_reference(folder, meta, control=None):
    """Construct one lazy, hash-verified reference context for a selected group.

    Missing samples and metadata previews need no scientific dependencies. The
    fixed environment's GTF parser is loaded only when rows need a catalogue.
    """
    folder = Path(folder)
    files = {key:{'path':str(child_path(folder, meta['files'][key]['path'])),
                  'sha256':meta['files'][key]['sha256']} for key in ('genome', 'annotation')}
    verified, catalogue = {}, {}
    def verify_reference():
        current = {k:_stamp(v['path']) for k,v in files.items()}
        if verified.get('stamps') != current:
            for entry in files.values():
                if sha256(Path(entry['path']), control) != _digest(entry['sha256']):
                    raise ValueError('reference hash mismatch: ' + entry['path'])
            if current != {k:_stamp(v['path']) for k,v in files.items()}:
                raise ValueError('reference changed during verification')
            verified['stamps'] = current
            catalogue.clear()
    def load_catalogue():
        if not catalogue:
            verify_reference()
            from plantvelo.irprior import load_introns_from_gtf
            for intron in load_introns_from_gtf(files['annotation']['path']):
                check_cancel(control)
                catalogue[intron.intron_uid] = {'gene_id':intron.gene_id,'intron_id':intron.intron_id,
                    'intron_uid':intron.intron_uid,'chromosome':intron.chrom,'start':str(intron.start0+1),
                    'end':str(intron.end0),'strand':intron.strand,
                    'supporting_transcripts':intron.supporting_transcripts,'anchor_count':str(intron.anchor_count),
                    'exon_overlap_warning':intron.exon_overlap_warning}
            if not catalogue: raise ValueError('reference annotation contains no introns')
        return catalogue
    return {'species':meta['species_code'],'reference_id':meta['assembly']['name'],
            'annotation_id':meta['annotation']['name'],'files':files,
            'metadata_path':str(folder/'reference.json'),'metadata':meta,
            '_verify_reference':verify_reference,
            'intron_lookup':lambda uid:load_catalogue().get(uid),
            'expected_rows':lambda:len(load_catalogue())}


_CACHE_SCHEMA = '''CREATE TABLE IF NOT EXISTS evidence_readiness_checks (
    namespace TEXT NOT NULL,accession TEXT NOT NULL,dependencies TEXT NOT NULL,
    result TEXT NOT NULL,checked_at TEXT NOT NULL,PRIMARY KEY(namespace,accession))'''


class ReadinessCache:
    """Optional existing SQLite database or borrowed connection; short writes."""
    def __init__(self, path=None, connection=None, readonly=False):
        self.readonly, self.memory = readonly, {}
        self.path, self.connection = Path(path) if path is not None else None, connection
        self._owned = False
        if self.connection is None and self.path is not None and not readonly:
            self.connection = sqlite3.connect(str(self.path), timeout=30)
            self._owned = True
        if self.connection is not None and not readonly:
            # DDL outside a transaction commits itself; inside a borrowed
            # transaction it must remain part of the caller's transaction.
            self.connection.execute(_CACHE_SCHEMA)

    def close(self):
        if self._owned and self.connection is not None:
            self.connection.close(); self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def lookup(self, namespace, accession, dependencies):
        if self.readonly: return None
        key = (str(namespace), accession)
        try:
            if self.connection is not None:
                row = self.connection.execute('SELECT dependencies,result FROM evidence_readiness_checks WHERE namespace=? AND accession=?', key).fetchone()
                if row is None or not _same_facts(json.loads(row[0]), dependencies): return None
                result = json.loads(row[1])
            else:
                value = self.memory.get(key)
                if value is None or not _same_facts(value[0], dependencies): return None
                result = value[1]
            if not isinstance(result, dict) or result.get('state') != 'ready' or result.get('kind') not in ('native','validated_import','legacy'):
                return None
            if not isinstance(result.get('signature'), dict) or not isinstance(result.get('identity'), dict): return None
            if result['signature'].get('kind') != result['kind'] or result.get('differences') or result.get('reason'):
                return None
            if (result['identity'].get('namespace') != key[0]
                    or result['identity'].get('accession') != accession): return None
            _digest(result['identity'].get('sha256'))
            if result['kind'] == 'native':
                validate_parameters(result['signature'].get('parameters'))
                canonical_software(result['signature'].get('software'))
                _digest(result['identity'].get('report_sha256'))
            elif result['kind'] == 'validated_import':
                if result['signature'].get('historical_parameters_verified') is not False: return None
                _digest(result['signature'].get('audit_id'))
            result = dict(result, differences=tuple(result.get('differences', ())))
            return EvidenceDecision(**result)
        except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
            return None

    def remember(self, namespace, accession, dependencies, result):
        if self.readonly or result.state != 'ready': return
        key = (str(namespace), accession)
        if self.connection is None:
            self.memory[key] = (json.loads(_encoded(dependencies)), result.to_dict())
        else:
            try:
                # SAVEPOINT gives a short standalone transaction or nests
                # safely without committing a transaction owned by the caller.
                self.connection.execute('SAVEPOINT evidence_readiness_cache_write')
                try:
                    self.connection.execute('INSERT OR REPLACE INTO evidence_readiness_checks VALUES (?,?,?,?,?)',
                        (*key, _encoded(dependencies), _encoded(result.to_dict()), now()))
                    self.connection.execute('RELEASE evidence_readiness_cache_write')
                except sqlite3.Error:
                    self.connection.execute('ROLLBACK TO evidence_readiness_cache_write')
                    self.connection.execute('RELEASE evidence_readiness_cache_write')
                    raise
            except sqlite3.Error:
                # A disposable performance cache cannot invalidate scientific facts.
                pass


_PROCESS_CACHE = ReadinessCache()


def _projection(record, name):
    projection = {key:record.get(key) for key in ('species_code','reference_id','annotation_id',
        'reference_metadata_path','reference_sha256','coordinate_system','validation','admission')}
    projection['file_sha256'] = {n:record.get('file_sha256',{}).get(n) for n in (name,name+'.run.json')}
    return projection


def _registration(folder):
    path = _safe(child_path(folder, 'registration.json'))
    if not path.exists(): return None
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not isinstance(value.get('file_sha256'), dict):
        raise ValueError('malformed evidence registration')
    return value


def _reference_metadata(folder, record, reference):
    relative = record.get('reference_metadata_path')
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError('invalid registered reference metadata path')
    # Historical relative paths legitimately walk from evidence to references.
    candidate = folder / relative
    _safe(candidate)
    path = candidate.resolve()
    _safe(path)
    meta = json.loads(path.read_text())
    if (meta['species_code'] != reference['species'] or meta['assembly']['name'] != reference['reference_id']
            or meta['annotation']['name'] != reference['annotation_id']
            or any(meta['files'][k]['sha256'] != reference['files'][k]['sha256'] for k in ('genome','annotation'))):
        raise ValueError('registered reference metadata identity mismatch')
    for key in ('genome','annotation'):
        actual = _safe(child_path(path.parent, meta['files'][key]['path']))
        if actual.resolve() != _safe(reference['files'][key]['path']).resolve():
            raise ValueError('registered reference metadata points to another physical reference')
    return path, meta


def _dependencies(folder, sample, reference, record, request, allow_legacy, paths, meta_path, meta):
    from evidence_identity import __file__ as identity_file
    from evidence_admission import __file__ as admission_file
    from evidence_contract import __file__ as contract_file
    implementations = [Path(__file__),Path(identity_file),Path(admission_file),Path(contract_file),MIGRATIONS]
    return {'registration':_projection(record, sample['Accession']+'.intron_prior.tsv'),
        'sample':{k:v for k,v in sample.items() if k != 'RemotePaths'},
        'reference':{'species':reference['species'],'reference_id':reference['reference_id'],
            'annotation_id':reference['annotation_id'],'files':reference['files'],'metadata':meta,
            'metadata_stamp':_stamp(meta_path),
            'physical_stamps':{k:_stamp(v['path']) for k,v in reference['files'].items()}},
        'request':None if request is None else {'parameters':validate_parameters(request['parameters']),
                                             'software':software_policy(request['software'])},
        'files':{str(p):_stamp(p) for p in paths},'allow_legacy':bool(allow_legacy),
        'policy':{p.name:sha256(p) for p in implementations}}


def inspect_evidence(namespace, sample, reference, request=None, cache=None, control=None,
                     verify=True, allow_legacy=False):
    """Validate one sample in exactly the selected namespace; never repair it."""
    if not verify: return EvidenceDecision('unchecked', '', 'unknown')
    folder, accession = Path(namespace).absolute(), sample['Accession']
    kind, identity = 'unknown', None
    owned_adapter = None
    def fail(message, state='invalid', reason='EVIDENCE_INVALID'):
        return EvidenceDecision(state,reason,kind,identity=identity,differences=(str(message)[:1800],))
    try:
        check_cancel(control)
        _safe(folder)
        if not re.fullmatch(r'(?:SRR|ERR|DRR|CRR)[0-9]+', accession):
            return fail('invalid sample accession')
        if reference is None:
            return fail('selected reference context is required',state='incompatible',reason='EVIDENCE_INCOMPATIBLE')
        for marker in ('admission_pending.json', 'baseline_reset.json'):
            pending = _safe(child_path(folder,marker))
            if pending.exists():
                return fail('saved evidence recovery must finish: '+marker,state='pending',reason='EVIDENCE_RECOVERY_REQUIRED')
        name = accession+'.intron_prior.tsv'
        source, report_path = child_path(folder,name), child_path(folder,name+'.run.json')
        record = _registration(folder)
        if record is None:
            kind = 'absent'
            if source.exists() or report_path.exists(): return fail('unregistered evidence files; preserve files before retrying')
            return EvidenceDecision('missing','','absent')
        expected_identity = {'species_code':species_code(reference['species']),
            'reference_id':reference['reference_id'],'annotation_id':reference['annotation_id']}
        if any(record.get(k) != v for k,v in expected_identity.items()):
            return fail('evidence registration namespace mismatch',state='incompatible',reason='EVIDENCE_INCOMPATIBLE')
        digest = record['file_sha256'].get(name)
        report_digest = record['file_sha256'].get(name+'.run.json')
        kind = 'native' if report_digest is not None else ('validated_import' if record.get('admission') else 'legacy')
        if digest is None:
            kind = 'absent'
            if source.exists() or report_path.exists() or report_digest is not None:
                return fail('unregistered or incomplete evidence files; preserve files before retrying')
            return EvidenceDecision('missing','','absent')
        _digest(digest)
        if report_digest is not None: _digest(report_digest)
        elif report_path.exists(): return fail('unregistered native report; preserve it before retrying')
        identity = {'namespace':str(folder),'accession':accession,'sha256':digest,'report_sha256':report_digest}
        if record.get('coordinate_system') != '1-based inclusive' or record.get('validation',{}).get('reference_match') != 'exact_introns_and_supporting_transcripts':
            return fail('evidence registration is not reference-validated')
        if any(record.get('reference_sha256',{}).get(k) != reference['files'][k]['sha256'] for k in ('genome','annotation')):
            return fail('evidence registration reference hash mismatch',state='incompatible',reason='EVIDENCE_INCOMPATIBLE')
        if species_code(sample['Species']) != species_code(reference['species']):
            return fail('sample metadata species differs from selected reference',state='incompatible',reason='EVIDENCE_INCOMPATIBLE')
        meta_path, meta = _reference_metadata(folder, record, reference)
        if kind == 'legacy' and not allow_legacy:
            return fail('historical evidence requires formal admission; run validate_imported_evidence.py',reason='EVIDENCE_ADMISSION_REQUIRED')
        paths = [source]
        if report_digest is not None: paths.append(report_path)
        elif record.get('admission'):
            receipt = record['admission'];_digest(receipt['sha256'])
            admission_path = child_path(folder,receipt['path']);paths.append(admission_path)
            identity['admission'] = receipt
        dependencies = _dependencies(folder,sample,reference,record,request,allow_legacy,paths,meta_path,meta)
        adapter = cache if cache is not None else _PROCESS_CACHE
        if not isinstance(adapter, ReadinessCache):
            if hasattr(adapter,'db'): adapter=ReadinessCache(connection=adapter.db)
            elif isinstance(adapter,sqlite3.Connection): adapter=ReadinessCache(connection=adapter)
            else: adapter=ReadinessCache(path=adapter)
            owned_adapter = adapter
        cached = adapter.lookup(folder,accession,dependencies)
        def unchanged():
            latest = _registration(folder)
            if latest is None or _projection(latest,name) != _projection(record,name): return False
            latest_meta_path,latest_meta = _reference_metadata(folder,latest,reference)
            return dependencies == _dependencies(folder,sample,reference,latest,request,allow_legacy,paths,latest_meta_path,latest_meta)
        if cached is not None:
            if not unchanged(): return fail('evidence changed during cached verification')
            return _match_request(cached,request)
        verify_reference = reference.get('_verify_reference')
        if verify_reference: verify_reference()
        else:
            for entry in reference['files'].values():
                if sha256(Path(entry['path']),control) != _digest(entry['sha256']):
                    return fail('physical reference file hash mismatch')
        for path in paths:
            expected = (digest if path == source else report_digest if path == report_path else record['admission']['sha256'])
            if sha256(path,control) != expected: return fail('registered evidence file hash mismatch: '+path.name)
        report = None
        if kind == 'native':
            report = json.loads(report_path.read_text());signature = native_signature(report,sample,reference)
        elif kind == 'validated_import':
            audit = load_admission(folder,record,reference)
            if audit['samples'].get(accession,{}).get('sha256') != digest:
                return fail('historical source is not covered by formal admission')
            signature = imported_signature(audit)
        else:
            if not allow_legacy:
                return fail('historical evidence requires formal admission; run validate_imported_evidence.py',reason='EVIDENCE_ADMISSION_REQUIRED')
            signature = {'kind':'legacy','parameters':'unknown','software':'unknown'}
        expected_rows = reference['expected_rows']
        if callable(expected_rows): expected_rows = expected_rows()
        with source.open(newline='') as handle:
            reader = csv.DictReader(handle,delimiter='\t')
            class CheckedRows:
                fieldnames = reader.fieldnames
                def __iter__(self):
                    for row in reader:
                        check_cancel(control)
                        yield row
            result = validate_rows(CheckedRows(),sample,reference['intron_lookup'],expected_rows,kind,report)
        if not unchanged(): return fail('evidence or reference changed during verification')
        decision = EvidenceDecision('ready','',kind,signature,identity)
        adapter.remember(folder,accession,dependencies,decision)
        return _match_request(decision,request)
    except ImportError as exc:
        return fail('trusted scientific runtime unavailable: '+str(exc),state='unchecked')
    except (OSError,ValueError,KeyError,TypeError,AttributeError,sqlite3.Error) as exc:
        return fail(str(exc))
    finally:
        if owned_adapter is not None: owned_adapter.close()


def check_group(signatures, proposed_native=None, allow_legacy_mixing=False):
    values = list(signatures)
    if proposed_native is not None: values.append(proposed_native)
    try:
        validate_cohort(values,allow_legacy_mixing)
        return {'state':'ready','reason':'','message':'evidence cohort admission satisfied'}
    except (ValueError,KeyError,TypeError,AttributeError) as exc:
        return {'state':'incompatible','reason':'EVIDENCE_COHORT_INCOMPATIBLE','message':str(exc)[:1800]}
