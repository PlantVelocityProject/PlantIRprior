#!/usr/bin/env python3
"""Validated, recoverable FASTQ downloads. Python 3.8+, standard library only."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import zlib

from qtrans_download import BLOCKED_EXIT, TIMEOUT_EXIT, DownloadBlocked, QtransDownload

PROJECT = Path(__file__).resolve().parents[1]
SPECIES = {'ath': 'Arabidopsis thaliana', 'gmx': 'Glycine max', 'osa': 'Oryza sativa',
           'sly': 'Solanum lycopersicum', 'zma': 'Zea mays'}
METHODS = ('aws-http', 'prefetch', 'ena-ftp', 'ena-ascp', 'aws-cp', 'gcp-cp')
PROXY_ENV_VARS = {
    prefix + name
    for prefix in ('', 'singularityenv_', 'apptainerenv_')
    for name in ('http_proxy', 'https_proxy', 'ftp_proxy', 'all_proxy', 'no_proxy')
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def positive_int(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return result


def nonnegative_int(value):
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError('must be nonnegative')
    return result


def nonnegative_float(value):
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError('must be finite and nonnegative')
    return result


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('--samples', type=Path, default=PROJECT / 'metadata/samples_info.tsv', help='TSV with Accession and Species')
    p.add_argument('--accession', action='append', default=[], help='select a run; repeat for multiple runs')
    p.add_argument('--species', action='append', default=[], help='species code or scientific name; repeatable')
    p.add_argument('--raw-dir', type=Path, default=PROJECT / 'storage/raw')
    p.add_argument('--log-dir', type=Path, default=PROJECT / 'logs/download')
    p.add_argument('--state-db', type=Path, default=PROJECT / 'state/downloads.sqlite3')
    p.add_argument('--evidence-dir', type=Path, default=PROJECT / 'storage/evidence')
    p.add_argument('--references-dir', type=Path, default=PROJECT / 'storage/references')
    p.add_argument('--reference-id')
    p.add_argument('--annotation-id')
    p.add_argument('--evidence-config', type=Path, default=PROJECT / 'configs/evidence.json')
    p.add_argument('--evidence-cache-db', type=Path)
    p.add_argument('--backend', choices=('auto', 'kingfisher', 'qtransc', 'ena-https'), default='auto')
    p.add_argument('--kingfisher', default=os.environ.get('KINGFISHER_BIN', 'kingfisher'))
    p.add_argument('--qtransc', default=os.environ.get('QTRANSC_BIN', 'qtransc'))
    p.add_argument('--methods', nargs='+', choices=METHODS, default=['aws-http', 'prefetch'])
    p.add_argument('--remote-path', action='append', default=[], help='qtransc path for one selected CRR; repeat for multiple files')
    p.add_argument('--jobs', type=positive_int, default=os.environ.get('MAX_JOBS', '3'))
    p.add_argument('--retries', type=nonnegative_int, default=2, help='additional attempts after the first failure')
    p.add_argument('--retry-delay', type=nonnegative_float, default=5)
    p.add_argument('--timeout', type=nonnegative_float, default=0, help='seconds per backend attempt including Qtrans transfer; 0 disables transfer timeout')
    p.add_argument('--ignore-evidence', action='store_true', help='download raw data even when compatible registered evidence exists')
    p.add_argument('--redownload', action='store_true', help='materialize raw data again, bypass evidence/downstream skips')
    p.add_argument('--dry-run', action='store_true', help='read-only plan; do not run download programs')
    p.add_argument('--json', action='store_true', help='structured summary on stdout')
    return p


def species_code(value):
    normalized = value.strip().replace('_', ' ').casefold()
    for code, name in SPECIES.items():
        if normalized in (code, name.casefold()):
            return code
    raise ValueError('unsupported species: ' + value)


def remote_paths(value):
    if not value.strip():
        return []
    paths = json.loads(value) if value.lstrip().startswith('[') else value.split(';')
    if not isinstance(paths, list) or not all(isinstance(v, str) for v in paths):
        raise ValueError('RemotePaths must be a JSON string array or semicolon-separated paths')
    return [v.strip() for v in paths if v.strip()]


@dataclass
class Job:
    accession: str
    species: str
    backend: str
    remote: list = field(default_factory=list)
    protocol: str = ''

    @property
    def fingerprint(self):
        # Archive transport may change on retry; published byte receipts remain authoritative.
        transport = 'kingfisher' if self.backend == 'ena-https' else self.backend
        value = [self.accession, self.species, transport, self.remote]
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def read_jobs(args):
    jobs, seen = [], set()
    with args.samples.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f, delimiter='\t')
        if not reader.fieldnames or not {'Accession', 'Species'} <= set(reader.fieldnames):
            raise ValueError('sample table requires Accession and Species columns')
        for line, row in enumerate(reader, 2):
            if None in row or any(v is None for v in row.values()):
                raise ValueError('malformed sample row at line {}'.format(line))
            acc = row['Accession'].strip()
            if not re.fullmatch(r'(?:SRR|ERR|DRR|CRR)[0-9]+', acc):
                raise ValueError('invalid run accession at line {}: {}'.format(line, acc))
            if acc in seen:
                raise ValueError('duplicate accession: ' + acc)
            seen.add(acc)
            backend = 'qtransc' if acc.startswith('CRR') else 'kingfisher'
            jobs.append(Job(acc, species_code(row['Species']), backend, remote_paths(row.get('RemotePaths', '')), row.get('Protocol', '').strip()))
    if not jobs:
        raise ValueError('sample table is empty')
    requested = set(args.accession)
    if requested - seen:
        raise ValueError('accessions absent from sample table: ' + ', '.join(sorted(requested - seen)))
    selected_species = {species_code(v) for v in args.species}
    jobs = [j for j in jobs if (not requested or j.accession in requested)
            and (not selected_species or j.species in selected_species)]
    if requested and {j.accession for j in jobs} != requested:
        raise ValueError('accession and species selectors conflict')
    if args.remote_path:
        if len(jobs) != 1 or jobs[0].backend != 'qtransc':
            raise ValueError('--remote-path requires exactly one selected CRR')
        jobs[0].remote = list(args.remote_path)
    for job in jobs:
        if args.backend == 'ena-https' and job.backend == 'kingfisher':
            job.backend = 'ena-https'
        if args.backend != 'auto' and job.backend != args.backend:
            raise ValueError('backend {} is incompatible with {}'.format(args.backend, job.accession))
        names = []
        for path in job.remote:
            remote = PurePosixPath(path)
            if not re.match(r'^/gsa[0-9]*/', path) or '..' in remote.parts or '\x00' in path or not remote.name:
                raise ValueError('invalid qtransc remote path: ' + path)
            names.append(remote.name)
        if len(names) != len(set(names)):
            raise ValueError('qtransc remote paths have duplicate filenames: ' + job.accession)
    return jobs


def check_cancel(control):
    if control is not None and control.cancelled.is_set():
        raise RuntimeError('interrupted')


def sha256(path, control=None):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
            check_cancel(control)
            digest.update(block)
    return digest.hexdigest()


def child_path(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('unsafe local path: ' + str(relative))
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('symlink in managed output path: ' + str(current))
    return current



class State:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(path), timeout=30)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS downloads (
                accession TEXT, species TEXT, raw_root TEXT, fingerprint TEXT,
                status TEXT, attempts INTEGER, error TEXT, files_json TEXT, updated_at TEXT,
                PRIMARY KEY (accession,species,raw_root))''')

    def read(self, job, raw):
        with closing(sqlite3.connect(str(self.path), timeout=30)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM downloads WHERE accession=? AND species=? AND raw_root=?',
                             (job.accession, job.species, str(raw))).fetchone()
            if row is None:
                return None
            record = dict(row)
            payload = json.loads(record.pop('files_json'))
            record['files'] = payload if isinstance(payload, list) else payload['files']
            record['previous_files'] = [] if isinstance(payload, list) else payload.get('previous_files', [])
            return record

    def write(self, job, raw, status, attempts, error='', files=None, previous_files=None):
        with closing(sqlite3.connect(str(self.path), timeout=30)) as db, db:
            db.execute('INSERT OR REPLACE INTO downloads VALUES (?,?,?,?,?,?,?,?,?)',
                       (job.accession, job.species, str(raw), job.fingerprint, status, attempts,
                        error, json.dumps({'files': files or [], 'previous_files': previous_files or []}), now()))


class Control:
    def __init__(self):
        self.cancelled = threading.Event()
        self.download_blocked = threading.Event()
        self.signum = None
        self.lock_fd = None
        self.extra_lock_fds = ()
        self.cleanup_lock = threading.Lock()

    @property
    def pass_fds(self):
        return tuple(dict.fromkeys(([self.lock_fd] if self.lock_fd is not None else []) + list(self.extra_lock_fds)))

    def signal(self, signum, _frame):
        self.signum = signum
        self.cancelled.set()


def stop_process(process, cooperative=False):
    if cooperative:
        # The supervisor stops its own Qtrans task. While cleanup is pending,
        # it still holds the inherited directory lock.
        try: process.terminate()
        except ProcessLookupError: return
        try: process.wait(timeout=15)
        except subprocess.TimeoutExpired: pass
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def invoke(command, work, log, job, timeout, control, cooperative=False):
    if control.cancelled.is_set():
        raise RuntimeError('interrupted')
    # All download backends connect directly, including inside a proxied update.
    env = {name: value for name, value in os.environ.items() if name.lower() not in PROXY_ENV_VARS}
    env.update(IRAGENT_ACCESSION=job.accession, IRAGENT_SPECIES=job.species)
    with log.open('ab') as f:
        f.write(('\n{} command {}\n'.format(now(), json.dumps(command))).encode())
        f.flush()
        process = subprocess.Popen(command, cwd=str(work), stdout=f, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True,
                                   pass_fds=control.pass_fds)
        start = time.monotonic()
        try:
            while process.poll() is None:
                if control.cancelled.is_set():
                    raise RuntimeError('interrupted')
                if timeout and time.monotonic() - start >= timeout:
                    raise RuntimeError('backend timeout after {} seconds'.format(timeout))
                control.cancelled.wait(0.05)
            if cooperative and process.returncode == BLOCKED_EXIT:
                raise DownloadBlocked('Qtrans service or stop confirmation unavailable; see ' + str(log))
            if cooperative and process.returncode == TIMEOUT_EXIT:
                raise RuntimeError('Qtrans download timeout; see ' + str(log))
            if process.returncode:
                raise RuntimeError('backend exited with code {}'.format(process.returncode))
        finally:
            if process.poll() is None:
                stop_process(process, cooperative)


def validate_fastq(path, control=None):
    with path.open('rb') as f:
        compressed = f.read(2) == b'\x1f\x8b'
    opener = gzip.open if compressed else open
    count = 0
    with opener(str(path), 'rb') as f:
        while True:
            check_cancel(control)
            name = f.readline()
            if not name:
                break
            seq, plus, quality = f.readline(), f.readline(), f.readline()
            seq, quality = seq.rstrip(b'\r\n'), quality.rstrip(b'\r\n')
            if not name.startswith(b'@') or not plus.startswith(b'+') or not seq or len(seq) != len(quality):
                raise ValueError('invalid or truncated four-line FASTQ: ' + str(path))
            count += 1
    if not count:
        raise ValueError('empty FASTQ: ' + str(path))


def stage_receipts(work, job, control=None):
    files = [p for p in work.rglob('*') if p.is_file()]
    if job.backend == 'qtransc':
        fastqs = []
        for remote in job.remote:
            name = PurePosixPath(remote).name
            matches = [p for p in files if p.name == name]
            if len(matches) != 1:
                raise ValueError('expected exactly one downloaded file: ' + name)
            fastqs.append(matches[0])
    else:
        fastqs = [p for p in files if str(p).lower().endswith(('.fastq', '.fq', '.fastq.gz', '.fq.gz'))]
    if job.backend == 'ena-https':
        archive = json.loads(child_path(work,'ena-download.json').read_text())
        expected = {item['name'] for item in archive['files']}
        if (archive.get('accession') != job.accession or archive.get('status') != 'verified'
                or {str(p.relative_to(work)) for p in fastqs} != expected):
            raise ValueError('FASTQ set differs from verified ENA file report')
    if not fastqs:
        raise ValueError('backend returned success but produced no FASTQ files')
    for path in fastqs:
        child_path(work, path.relative_to(work))
        validate_fastq(path, control)
    receipts = []
    for path in files:
        child_path(work, path.relative_to(work))
        receipts.append({'path': str(path.relative_to(work)), 'size_bytes': path.stat().st_size,
                         'sha256': sha256(path, control), 'kind': 'fastq' if path in fastqs else 'auxiliary'})
    return receipts


def valid_receipts(files, target, control=None, required_kind='fastq'):
    if not files or not any(item.get('kind') == required_kind for item in files):
        return False
    try:
        actual = {str(p.relative_to(target)) for p in target.rglob('*') if p.is_file() or p.is_symlink()}
        if actual != {item['path'] for item in files}:
            return False
        for item in files:
            path = child_path(target, item['path'])
            if not path.is_file() or path.stat().st_size != item['size_bytes'] or sha256(path, control) != item['sha256']:
                return False
        return True
    except (OSError, KeyError, ValueError):
        return False


def ensure_owned_target(target, record):
    if not target.exists():
        return
    if not target.is_dir():
        raise ValueError('output path is not a directory: ' + str(target))
    actual = {str(p.relative_to(target)) for p in target.rglob('*') if p.is_file() or p.is_symlink()}
    expected = {item['path'] for item in record['files'] + record.get('previous_files', [])} if record else set()
    if actual - expected or (actual and not record):
        raise ValueError('unregistered files in output directory; preserve or relocate them before retrying: ' + str(target))
    for relative in actual:
        child_path(target, relative)


def promote(work, target, record):
    ensure_owned_target(target, record)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = work.parent / ('.previous-' + target.name)
    if backup.exists() and target.exists():
        raise ValueError('both previous and current output directories exist; preserve them before recovery: ' + str(target))
    if target.exists():
        target.rename(backup)
    try:
        work.rename(target)
    except BaseException:
        if backup.exists():
            backup.rename(target)
        raise
    return backup


def cleanup_backup(backup, files):
    if backup.exists():
        ensure_owned_target(backup, {'files': files})
        shutil.rmtree(str(backup))


def result(job, status, error='', attempts=0):
    value = {'accession': job.accession, 'species': job.species, 'backend': job.backend,
             'status': status, 'attempts': attempts}
    if error:
        value['error'] = error
    return value


def download(job, args, state, control):
    from artifact_lifecycle import (authorized_files, journal, kept_receipts_valid,
                                    retire_journals, valid_proof)
    if control.download_blocked.is_set():
        return result(job, 'deferred', 'shared download service is blocked')
    previous = state.read(job, args.raw_dir)
    old_files = previous['files'] if previous else []
    attempts = 0
    prepared = False
    publishing = bool(previous and previous['status'] == 'publishing')
    try:
        target = child_path(args.raw_dir, Path(job.species) / job.accession)
        work = child_path(args.raw_dir, Path('.partial') / job.species / job.accession)
        backup = child_path(args.raw_dir, Path('.partial') / job.species / ('.previous-' + job.accession))
        same_request = previous and previous['fingerprint'] == job.fingerprint
        if same_request and previous['status'] == 'complete' and not args.redownload:
            pruned = journal(args.state_db, target, 'fastq')
            if pruned and pruned['request_fingerprint'] == job.fingerprint and pruned['status'] in ('pruning', 'pruned'):
                consumer = pruned['payload']['consumer']
                qc = child_path(Path(consumer['target']), 'alignment.qc.json')
                protocol_matches = not job.protocol or json.loads(qc.read_text()).get('protocol') == job.protocol
                omitted = authorized_files(args.state_db, target, 'fastq', job.fingerprint, control)
                if protocol_matches and omitted and kept_receipts_valid(old_files, target, omitted, control):
                    return result(job, 'skipped_downstream')
        if previous and previous['status'] == 'complete':
            # A crash after committing a replacement can leave its old backup.
            # Reconcile it before another request overwrites its ownership receipt,
            # even when the current output has since disappeared or changed.
            cleanup_backup(backup, previous['previous_files'])
        if not args.redownload and same_request and previous['status'] in ('complete', 'publishing') and valid_receipts(old_files, target, control):
            if publishing:
                retire_journals(args.state_db, target)
                state.write(job, args.raw_dir, 'complete', previous['attempts'], files=old_files,
                            previous_files=previous['previous_files'])
            cleanup_backup(backup, previous['previous_files'])
            return result(job, 'reused')
        if publishing:
            if not same_request or not valid_receipts(old_files, work, control):
                raise ValueError('pending publication cannot be verified; preserve existing files before recovery')
            promote(work, target, previous)
            retire_journals(args.state_db, target)
            state.write(job, args.raw_dir, 'complete', previous['attempts'], files=old_files,
                        previous_files=previous['previous_files'])
            cleanup_backup(backup, previous['previous_files'])
            return result(job, 'reused')
        ensure_owned_target(target, previous)
        if job.backend == 'qtransc' and not job.remote:
            raise ValueError('CRR requires explicit remote paths (--remote-path or RemotePaths column)')
        binary = sys.executable if job.backend == 'ena-https' else args.qtransc if job.backend == 'qtransc' else args.kingfisher
        binary = str(Path(binary).expanduser().resolve()) if '/' in binary else shutil.which(binary)
        if not binary or not os.path.isfile(binary) or not os.access(binary, os.X_OK):
            raise ValueError('download executable not found or not executable: ' + str(binary or job.backend))
        if work.exists() and any(work.iterdir()) and (not previous or previous['fingerprint'] != job.fingerprint):
            raise ValueError('partial directory belongs to a different or unregistered request: ' + str(work))
        work.mkdir(parents=True, exist_ok=True)
        prepared = True
        if job.backend == 'kingfisher' and ((work/'.ena-metadata').exists() or (work/'ena-download.json').exists()):
            raise ValueError('HTTPS-managed staging is incomplete; resume with --backend ena-https')
        log = args.log_dir / job.species / (job.accession + '.log')
        log.parent.mkdir(parents=True, exist_ok=True)
        error = ''
        for attempts in range(1, args.retries + 2):
            if control.cancelled.is_set():
                break
            state.write(job, args.raw_dir, 'running', attempts, files=old_files)
            with log.open('a') as f:
                f.write('\n{} attempt {}\n'.format(now(), attempts))
            try:
                if job.backend == 'ena-https':
                    invoke([binary, '-B', str(Path(__file__).with_name('ena_https_download.py')),
                            '--accession',job.accession,'--directory',str(work)],
                           work,log,job,args.timeout,control)
                elif job.backend == 'kingfisher':
                    invoke([binary, 'get', '-r', job.accession, '-m'] + args.methods,
                           work, log, job, args.timeout, control)
                else:
                    command = [sys.executable, '-B', str(Path(__file__).with_name('qtrans_download.py')),
                               '--binary', binary, '--directory', str(work), '--raw-root', str(args.raw_dir),
                               '--timeout', str(args.timeout)]
                    for remote in job.remote: command.extend(['--remote', remote])
                    invoke(command, work, log, job, 0, control, cooperative=True)
                if control.cancelled.is_set():
                    raise RuntimeError('interrupted')
                files = stage_receipts(work, job, control)
                check_cancel(control)
                # Commit publication intent before any rename. A restart can
                # reconcile verified staging or final files without redownloading.
                state.write(job, args.raw_dir, 'publishing', attempts, files=files, previous_files=old_files)
                publishing = True
                promote(work, target, previous)
                retire_journals(args.state_db, target)
                state.write(job, args.raw_dir, 'complete', attempts, files=files, previous_files=old_files)
                cleanup_backup(backup, old_files)
                return result(job, 'downloaded', attempts=attempts)
            except (OSError, ValueError, RuntimeError, EOFError, zlib.error, sqlite3.Error) as exc:
                error = str(exc)
                with log.open('a') as f:
                    f.write('FAILED: {}\n'.format(error))
                if isinstance(exc, DownloadBlocked):
                    control.download_blocked.set()
                    break
                if publishing:
                    # Preserve the durable intent instead of replacing it with
                    # an old/empty receipt if finalization or cleanup failed.
                    return result(job, 'failed', error, attempts)
                if control.cancelled.is_set():
                    break
                if attempts <= args.retries:
                    control.cancelled.wait(args.retry_delay)
        if control.cancelled.is_set():
            state.write(job, args.raw_dir, 'interrupted', attempts, 'interrupted', old_files)
            return result(job, 'interrupted', 'interrupted', attempts)
        state.write(job, args.raw_dir, 'failed', attempts, error, old_files)
        return result(job, 'failed', error, attempts)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        # A rejected preflight must not grant ownership of unrelated staging files.
        status = 'interrupted' if control.cancelled.is_set() else 'failed'
        if prepared and not publishing:
            state.write(job, args.raw_dir, status, attempts, str(exc), old_files)
        return result(job, status, str(exc), attempts)


def display(args, results, dry_run=False, download_blocked=False):
    names = ('downloaded', 'reused', 'skipped_evidence', 'skipped_downstream', 'planned', 'failed', 'interrupted', 'deferred')
    counts = {name: sum(r['status'] == name for r in results) for name in names}
    summary = {'dry_run': dry_run, 'selected': len(results), 'counts': counts, 'results': results}
    if download_blocked: summary['download_blocked'] = True
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print('{} {} {}{}'.format(r['status'].upper(), r['species'], r['accession'],
                                     (': ' + r['error'] if 'error' in r else '') +
                                     (' [evidence unchecked]' if r.get('evidence', {}).get('state') == 'unchecked' else '')))
        print('Summary: ' + ', '.join('{}={}'.format(k, v) for k, v in counts.items()))
    return summary


def run(args):
    for name in ('samples', 'raw_dir', 'log_dir', 'state_db', 'evidence_dir', 'references_dir', 'evidence_config'):
        setattr(args, name, getattr(args, name).expanduser().resolve())
    jobs = read_jobs(args)
    results, pending = [], []
    # Keep this import local: readiness shares managed-file helpers above.
    if not args.dry_run and not (args.ignore_evidence or args.redownload):
        from evidence_readiness import build_reference, inspect_evidence, request_for, ReadinessCache
        from align_reads import reference_for
        cache = ReadinessCache(args.evidence_cache_db)
        references = {}
        defaults = json.loads((PROJECT / 'configs/prior_resources.json').read_text())['defaults']
        try:
            request = None
            for job in jobs:
                try:
                    target = defaults[job.species]
                    reference_id = args.reference_id or target['reference_id']
                    annotation_id = args.annotation_id or target['annotation_id']
                    namespace = args.evidence_dir / job.species / reference_id / annotation_id
                    # An absent selected namespace cannot justify skipping a download.
                    # Evidence belonging to another target never blocks a fresh run.
                    if not namespace.exists() and not namespace.is_symlink():
                        pending.append(job)
                        continue
                    if job.species not in references:
                        from argparse import Namespace
                        selection = Namespace(references_dir=args.references_dir,
                                              reference_id=reference_id, annotation_id=annotation_id)
                        folder, metadata = reference_for(selection, job.species)
                        references[job.species] = build_reference(folder, metadata)
                    reference = references[job.species]
                    if request is None:
                        request = request_for(args.evidence_config)
                    decision = inspect_evidence(namespace,
                        {'Accession': job.accession, 'Species': job.species, 'Protocol': job.protocol},
                        reference, request=request, cache=cache)
                    if decision.state == 'missing':
                        pending.append(job)
                    else:
                        value = result(job, 'skipped_evidence' if decision.state == 'ready' else 'failed',
                                       '' if decision.state == 'ready' else decision.message)
                        value['evidence'] = decision.to_dict()
                        results.append(value)
                except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as exc:
                    results.append(result(job, 'failed', str(exc)))
        finally:
            cache.close()
    else:
        pending = jobs
    if args.dry_run:
        for job in pending:
            error = 'CRR requires remote paths' if job.backend == 'qtransc' and not job.remote else ''
            value = result(job, 'failed' if error else 'planned', error)
            value['evidence'] = {'state': 'unchecked', 'reason': 'EVIDENCE_UNCHECKED'}
            results.append(value)
        summary = display(args, sorted(results, key=lambda r: r['accession']), True)
        return 1 if summary['counts']['failed'] else 0
    if not pending:
        display(args, sorted(results, key=lambda r: r['accession']))
        return 1 if any(r['status'] == 'failed' for r in results) else 0
    args.raw_dir.mkdir(parents=True, exist_ok=True)
    # Lock the Linux directory itself: no separate .done/lock metadata file.
    descriptor = os.open(str(args.raw_dir), os.O_RDONLY)
    control, handlers = Control(), {}
    control.lock_fd = descriptor
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DownloadBlocked('another downloader is already using this raw directory')
        selected_qtrans = {(remote, str(args.raw_dir/'.partial'/job.species/job.accession/PurePosixPath(remote).name))
                           for job in pending if job.backend == 'qtransc' for remote in job.remote}
        QtransDownload('', args.raw_dir, [], 0, control.cancelled).check_raw_writers(args.raw_dir, selected_qtrans)
        state = State(args.state_db)
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, control.signal)
        with ThreadPoolExecutor(max_workers=args.jobs) as executor:
            futures = {executor.submit(download, job, args, state, control): job for job in pending}
            for future in as_completed(futures):
                job = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append(result(job, 'failed', str(exc)))
        summary = display(args, sorted(results, key=lambda r: r['accession']),
                          download_blocked=control.download_blocked.is_set())
        if control.signum:
            return 128 + control.signum
        return 1 if summary['counts']['failed'] or summary['counts']['interrupted'] else 0
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        os.close(descriptor)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return run(args)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        if args.json:
            value = {'error': str(exc)}
            if isinstance(exc, DownloadBlocked): value['download_blocked'] = True
            print(json.dumps(value, ensure_ascii=False))
        else:
            print('ERROR: ' + str(exc), file=sys.stderr)
        return 2 if isinstance(exc, (OSError, ValueError)) else 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
