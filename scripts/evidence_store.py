"""Journaled publication of flat per-sample evidence and shared registration."""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from download_reads import child_path, now, sha256


def atomic_json(path, value):
    """Durably prepare a sibling file before atomic replacement."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class EvidenceState:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(path))) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS evidence (
                accession TEXT, namespace TEXT, fingerprint TEXT, status TEXT,
                attempts INTEGER, error TEXT, payload TEXT, updated_at TEXT,
                PRIMARY KEY(accession, namespace))''')

    def read(self, accession, namespace):
        with closing(sqlite3.connect(str(self.path))) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM evidence WHERE accession=? AND namespace=?',
                             (accession, str(namespace))).fetchone()
            if row is None:
                return None
            value = dict(row); value['payload'] = json.loads(value['payload'])
            return value

    def write(self, accession, namespace, fingerprint, status, attempts, payload, error=''):
        with closing(sqlite3.connect(str(self.path))) as db, db:
            db.execute('INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?,?,?,?)',
                       (accession, str(namespace), fingerprint, status, attempts, error,
                        json.dumps(payload, allow_nan=False), now()))


def registration(target, identity):
    path = child_path(target, 'registration.json')
    if not path.exists():
        return dict(identity, schema_version='2.0', registered_at=now(),
                    coordinate_system='1-based inclusive',
                    validation={'reference_match': 'exact_introns_and_supporting_transcripts'},
                    extraction_provenance={'origin': 'native', 'software_version': None, 'effective_parameters': None},
                    file_sha256={})
    value = json.loads(path.read_text())
    for key in ['species_code', 'reference_id', 'annotation_id', 'reference_sha256']:
        if value.get(key) != identity[key]:
            raise ValueError('existing evidence registration has conflicting reference identity: ' + key)
    if value.get('coordinate_system') != '1-based inclusive' or value.get('validation', {}).get('reference_match') != 'exact_introns_and_supporting_transcripts':
        raise ValueError('existing evidence registration was not reference-validated')
    files = value.get('file_sha256')
    if not isinstance(files, dict) or any(not isinstance(k, str) or Path(k).name != k or
                                         not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v)
                                         for k, v in files.items()):
        raise ValueError('invalid file hashes in evidence registration')
    return value


def existing_files(target, names, record, control):
    """Only explicitly registered matching files may be replaced."""
    old = {}
    for name in names:
        path = child_path(target, name)
        if path.exists():
            expected = record['file_sha256'].get(name)
            if not expected or not path.is_file() or sha256(path, control) != expected:
                raise ValueError('unregistered or modified evidence file; preserve it before retrying: ' + str(path))
            old[name] = expected
    return old


def ensure_stage(stage, row, names):
    if not stage.exists():
        return
    if any(stage.iterdir()) and not row:
        raise ValueError('unregistered evidence partial directory: ' + str(stage))
    allowed = set(names) | {'request.json'}
    for path in stage.rglob('*'):
        relative = path.relative_to(stage)
        child_path(stage, relative)
        parts = relative.parts
        if len(parts) == 1 and parts[0] in allowed and path.is_file():
            continue
        # The extractor may be killed while preparing its own temporary pair.
        if re.fullmatch(r'\.(?:plantvelo-irprior|plantirref)-[A-Za-z0-9_-]+', parts[0]):
            if len(parts) == 1 and path.is_dir():
                continue
            if len(parts) == 2 and parts[1] in ('result.tsv', 'run.json') and path.is_file():
                continue
        raise ValueError('unknown file in evidence partial directory: ' + str(path))


def clear_stage(stage, row, names):
    ensure_stage(stage, row, names)
    if stage.exists():
        shutil.rmtree(str(stage))
    stage.mkdir(parents=True)


def matching_file(path, digest):
    return path.is_file() and sha256(path) == digest


def publish(target, stage, payload):
    """Finish a saved publication without re-running scientific computation."""
    identity, files, old = payload['identity'], payload['files'], payload['old_files']
    target.mkdir(parents=True, exist_ok=True)
    record = registration(target, identity)
    for name, digest in files.items():
        destination = child_path(target, name)
        source = child_path(stage, name)
        registered = record['file_sha256'].get(name)
        if registered not in (None, old.get(name), digest):
            raise ValueError('registration changed during pending evidence publication: ' + name)
        if matching_file(destination, digest):
            continue
        if destination.exists() and (name not in old or not matching_file(destination, old[name])):
            raise ValueError('modified evidence target during publication: ' + name)
        if not matching_file(source, digest):
            raise ValueError('pending evidence file cannot be verified: ' + name)
        with source.open('rb') as handle:
            os.fsync(handle.fileno())
        os.replace(source, destination)
        sync_directory(target)
        sync_directory(stage)
    record['file_sha256'].update(files)
    record['reference_metadata_path'] = identity['reference_metadata_path']
    record['registered_at'] = now()
    native = [name for name in record['file_sha256'] if name.endswith('.intron_prior.tsv')]
    mixed = any(name + '.run.json' not in record['file_sha256'] for name in native)
    # Preserve historical source details when new reports join that batch.
    provenance = dict(record.get('extraction_provenance', {}))
    provenance.update(origin=('mixed_validated' if record.get('admission') else 'mixed') if mixed else 'native',
                      software_version=None, effective_parameters=None)
    record['extraction_provenance'] = provenance
    atomic_json(child_path(target, 'registration.json'), record)
