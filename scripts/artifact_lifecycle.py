"""Journaled, receipt-bound pruning of reproducible intermediate files."""
from contextlib import closing, contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3

from download_reads import check_cancel, child_path, now, sha256

KINDS = {'fastq', 'alignment_fastq', 'bam'}


@contextmanager
def directory_lock(root, shared=False):
    """Use the same directory locks as stage executors, without lock files."""
    descriptor = os.open(str(root), os.O_RDONLY)
    try:
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('directory is in use: ' + str(root))
        yield descriptor
    finally:
        os.close(descriptor)


def readonly(db):
    return sqlite3.connect(Path(db).absolute().as_uri() + '?mode=ro', uri=True)


def task(db, stage, target, accession, species):
    """Read original successful receipts; never rewrite them during pruning."""
    if not Path(db).is_file():
        return None
    with closing(readonly(db)) as connection:
        connection.row_factory = sqlite3.Row
        if stage == 'evidence':
            row = connection.execute('SELECT * FROM evidence WHERE accession=? AND namespace=?',
                                     (accession, str(target))).fetchone()
            if row is None:
                return None
            result = dict(row); payload = json.loads(result['payload'])
            result['files'] = payload['files']
        else:
            namespace = target.parent.parent if stage == 'raw' else target.parent
            row = connection.execute('SELECT * FROM downloads WHERE accession=? AND species=? AND raw_root=?',
                                     (accession, species, str(namespace))).fetchone()
            if row is None:
                return None
            result = dict(row); payload = json.loads(result['files_json'])
            result['files'] = payload if isinstance(payload, list) else payload['files']
        return result


def journal(db, target, kind):
    if not Path(db).is_file():
        return None
    with closing(readonly(db)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_lifecycle'").fetchone():
            return None
        connection.row_factory = sqlite3.Row
        row = connection.execute('SELECT * FROM artifact_lifecycle WHERE sample_root=? AND kind=?',
                                 (str(target), kind)).fetchone()
        if row is None:
            return None
        result = dict(row); result['payload'] = json.loads(result['payload'])
        return result


def write_journal(db, target, kind, fingerprint, status, payload):
    with closing(sqlite3.connect(str(db))) as connection, connection:
        connection.execute('''CREATE TABLE IF NOT EXISTS artifact_lifecycle (
            sample_root TEXT, kind TEXT, request_fingerprint TEXT, status TEXT,
            payload TEXT, updated_at TEXT, PRIMARY KEY(sample_root,kind))''')
        connection.execute('INSERT OR REPLACE INTO artifact_lifecycle VALUES (?,?,?,?,?,?)',
                           (str(target), kind, fingerprint, status, json.dumps(payload), now()))


def retire_journals(db, target):
    """A new publication restores bytes; old deletion intents no longer apply."""
    if not Path(db).is_file():
        return
    with closing(sqlite3.connect(str(db))) as connection, connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_lifecycle'").fetchone():
            connection.execute("UPDATE artifact_lifecycle SET status='restored',updated_at=? WHERE sample_root=?",
                               (now(), str(target)))


def alignment_proof(db, target, accession, species, reference_metadata):
    row = task(db, 'alignment', target, accession, species)
    if not row or row['status'] != 'complete':
        raise ValueError('alignment is not durably complete')
    names = {'genome.sorted.bam', 'genome.sorted.bam.bai', 'alignment.qc.json'}
    files = {r['path']: r['sha256'] for r in row['files'] if r['path'] in names}
    if set(files) != names:
        raise ValueError('alignment receipts do not contain BAM, index and QC')
    qc = json.loads(child_path(target, 'alignment.qc.json').read_text())
    return {'stage': 'alignment', 'db': str(db), 'target': str(target), 'accession': accession,
            'species': species, 'fingerprint': row['fingerprint'], 'files': files,
            'reference_metadata': str(reference_metadata), 'references': qc['provenance']['reference'],
            'reference_id': qc['reference_id'], 'annotation_id': qc['annotation_id']}


def evidence_proof(db, target, accession, species):
    row = task(db, 'evidence', target, accession, species)
    if not row or row['status'] != 'complete':
        raise ValueError('evidence is not durably complete')
    report_name = accession + '.intron_prior.tsv.run.json'
    report = json.loads(child_path(target, report_name).read_text())
    record = json.loads(child_path(target, 'registration.json').read_text())
    return {'stage': 'evidence', 'db': str(db), 'target': str(target), 'accession': accession,
            'species': species, 'fingerprint': row['fingerprint'], 'files': row['files'],
            'reference_metadata': str((target / record['reference_metadata_path']).resolve()),
            'references': report['agent']['provenance']['reference'],
            'reference_id': record['reference_id'], 'annotation_id': record['annotation_id']}


def valid_proof(proof, control=None, seen=None):
    """Validate live downstream evidence, following at most alignment→evidence."""
    try:
        stage, target = proof['stage'], Path(proof['target'])
        marker = (proof['db'], str(target), proof['fingerprint'])
        seen = set() if seen is None else set(seen)
        if marker in seen or stage not in ('alignment', 'evidence'):
            return False
        seen.add(marker)
        row = task(proof['db'], stage, target, proof['accession'], proof['species'])
        if not row or row['status'] != 'complete' or row['fingerprint'] != proof['fingerprint']:
            return False
        meta = json.loads(Path(proof['reference_metadata']).read_text())
        if meta['assembly']['name'] != proof['reference_id'] or meta['annotation']['name'] != proof['annotation_id']:
            return False
        for key, entry in proof['references'].items():
            if meta['files'][key]['sha256'] != entry['sha256'] or sha256(Path(entry['path']), control) != entry['sha256']:
                return False
        receipts = row['files'] if stage == 'evidence' else {r['path']: r['sha256'] for r in row['files']}
        if any(receipts.get(name) != digest for name, digest in proof['files'].items()):
            return False
        omitted = {}
        if stage == 'alignment':
            omitted = authorized_files(proof['db'], target, 'bam', proof['fingerprint'], control, seen)
        for name, digest in proof['files'].items():
            path = child_path(target, name)
            if path.is_file():
                if sha256(path, control) != digest:
                    return False
            elif omitted.get(name, {}).get('sha256') != digest:
                return False
        if stage == 'evidence':
            record = json.loads(child_path(target, 'registration.json').read_text())
            if record['species_code'] != proof['species'] or record['reference_id'] != proof['reference_id'] or record['annotation_id'] != proof['annotation_id']:
                return False
            if record['validation']['reference_match'] != 'exact_introns_and_supporting_transcripts':
                return False
            if any(record['file_sha256'].get(n) != h for n, h in proof['files'].items()):
                return False
            if any(record['reference_sha256'][k] != v['sha256'] for k, v in proof['references'].items()):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
        return False


def authorized_files(db, target, kind, fingerprint, control=None, seen=None):
    row = journal(db, target, kind)
    if not row or row['request_fingerprint'] != fingerprint or row['status'] not in ('pruning', 'pruned'):
        return {}
    consumer = row['payload']['consumer']
    producer = task(db, 'raw' if kind == 'fastq' else 'alignment', target, consumer['accession'], consumer['species'])
    if not producer or producer['status'] != 'complete' or producer['fingerprint'] != fingerprint:
        return {}
    receipts = {entry['path']: entry for entry in producer['files']}
    if any(receipts.get(entry['path']) != entry for entry in row['payload']['files']):
        return {}
    if not valid_proof(row['payload']['consumer'], control, seen):
        return {}
    return {entry['path']: entry for entry in row['payload']['files']}


def prune(db, target, kind, fingerprint, files, consumer, control=None):
    """Caller must hold the producer's directory lock; never delete by glob."""
    if kind not in KINDS or not files:
        return {'status': 'nothing_to_prune', 'files': 0, 'bytes': 0}
    producer = task(db, 'raw' if kind == 'fastq' else 'alignment', target,
                    consumer['accession'], consumer['species'])
    if not producer or producer['status'] != 'complete' or producer['fingerprint'] != fingerprint:
        raise ValueError('cannot prune an unregistered or incomplete input generation')
    receipts = {entry['path']: entry for entry in producer['files']}
    for entry in files:
        if receipts.get(entry['path']) != entry:
            raise ValueError('cleanup file does not match original task receipt: ' + entry['path'])
        if kind == 'fastq' and entry['kind'] != 'fastq':
            raise ValueError('cleanup requires a registered FASTQ receipt')
        if kind == 'bam' and entry['path'] not in ('genome.sorted.bam', 'genome.sorted.bam.bai'):
            raise ValueError('unexpected BAM cleanup file')
        if kind == 'alignment_fastq' and entry['path'] not in ('full_length.fq', 'unclassified.fq', 'rescued.fq'):
            raise ValueError('unexpected preprocessed FASTQ cleanup file')
    if not valid_proof(consumer, control):
        raise ValueError('downstream result cannot be verified; inputs retained')
    # Confirm the consumer used exactly these bytes before granting deletion.
    report_path = ('alignment.qc.json' if kind == 'fastq' else consumer['accession'] + '.intron_prior.tsv.run.json')
    if kind != 'alignment_fastq':
        report = json.loads(child_path(Path(consumer['target']), report_path).read_text())
        provenance = report['provenance'] if kind == 'fastq' else report['agent']['provenance']
        consumed = provenance['inputs']
        consumed = consumed if isinstance(consumed, list) else list(consumed.values())
        consumed = {e['path']: e['sha256'] for e in consumed}
        if any(consumed.get(str(child_path(target, e['path']))) != e['sha256'] for e in files):
            raise ValueError('downstream result did not consume the selected input files')
    old = journal(db, target, kind)
    previous = {e['path']: e for e in old['payload']['files']} if old and old['request_fingerprint'] == fingerprint and old['status'] in ('pruning', 'pruned') else {}
    # Preflight every file before the first unlink. Missing files need an earlier
    # recorded intent; ordinary accidental deletion is not silently adopted.
    for entry in files:
        path = child_path(target, entry['path'])
        if path.exists():
            if not path.is_file() or path.stat().st_size != entry['size_bytes'] or sha256(path, control) != entry['sha256']:
                raise ValueError('modified intermediate retained: ' + str(path))
        elif previous.get(entry['path']) != entry:
            raise ValueError('missing intermediate has no cleanup authorization: ' + str(path))
    payload = {'files': files, 'consumer': consumer, 'reason': 'downstream_complete',
               'requested_at': old['payload'].get('requested_at', now()) if old else now()}
    write_journal(db, target, kind, fingerprint, 'pruning', payload)
    for entry in files:
        check_cancel(control)
        path = child_path(target, entry['path'])
        if path.exists():
            if sha256(path, control) != entry['sha256']:
                raise ValueError('intermediate changed before deletion: ' + str(path))
            path.unlink()
            descriptor = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    payload['completed_at'] = now()
    write_journal(db, target, kind, fingerprint, 'pruned', payload)
    return {'status': 'pruned', 'files': len(files), 'bytes': sum(e['size_bytes'] for e in files)}


def kept_receipts_valid(files, target, omitted, control=None):
    expected = {entry['path']: entry for entry in files}
    actual = {str(p.relative_to(target)) for p in target.rglob('*') if p.is_file() or p.is_symlink()}
    if actual - set(expected):
        return False
    for name, entry in expected.items():
        path = child_path(target, name)
        if not path.exists() and omitted.get(name) == entry:
            continue
        if not path.is_file() or path.stat().st_size != entry['size_bytes'] or sha256(path, control) != entry['sha256']:
            return False
    return True
