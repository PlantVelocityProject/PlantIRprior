#!/usr/bin/env python3
"""Reference-aware, recoverable long-read alignment. Python 3.8+, Linux."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import csv
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import shlex
import sqlite3
import subprocess
import sys
import time
import zlib

from download_reads import (PROJECT, Control, State, check_cancel, child_path, cleanup_backup,
                            ensure_owned_target, now,
                            nonnegative_float, nonnegative_int, positive_int, promote,
                            sha256, species_code, stop_process, valid_receipts, validate_fastq)
from artifact_lifecycle import (alignment_proof, authorized_files, directory_lock, journal,
                                kept_receipts_valid, prune, retire_journals, task, valid_proof)

ROUTES = {'iso-seq', 'ont-cdna', 'ont-pcr-cdna', 'ont-direct-rna', 'flep', 'flep2'}
OUTPUT_FILES = {'genome.sorted.bam', 'genome.sorted.bam.bai', 'genome.sorted.flagstat.txt',
                'alignment.qc.json', 'full_length.fq', 'unclassified.fq', 'rescued.fq',
                'pychopper_stats.tsv', 'pychopper_report.pdf'}


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    for flag, default in [('samples', 'metadata/samples_info.tsv'), ('raw-dir', 'storage/raw'),
                          ('references-dir', 'storage/references'), ('alignment-dir', 'storage/alignments'),
                          ('log-dir', 'logs/alignment'), ('state-db', 'state/alignments.sqlite3'),
                          ('evidence-dir', 'storage/evidence'), ('config', 'configs/alignment.json'),
                          ('download-state-db', 'state/downloads.sqlite3')]:
        p.add_argument('--' + flag, type=Path, default=PROJECT / default)
    p.add_argument('--evidence-config', type=Path, default=PROJECT / 'configs/evidence.json')
    p.add_argument('--evidence-cache-db', type=Path)
    p.add_argument('--accession', action='append', default=[])
    p.add_argument('--species', action='append', default=[])
    p.add_argument('--reference-id', help='assembly name; combine with --species when needed')
    p.add_argument('--annotation-id')
    p.add_argument('--protocol', help='override Protocol for all selected samples using a configured key')
    for name in ['threads', 'sort-threads', 'pychopper-threads', 'jobs']:
        p.add_argument('--' + name, type=positive_int)
    p.add_argument('--retries', type=nonnegative_int, default=0)
    p.add_argument('--retry-delay', type=nonnegative_float, default=5)
    p.add_argument('--timeout', type=nonnegative_float, default=0,
                   help='seconds for preprocessing+alignment, and per validation call; 0 disables')
    p.add_argument('--ignore-evidence', action='store_true')
    p.add_argument('--keep-fastq', action='store_true', help='retain raw and Pychopper FASTQs after successful alignment')
    p.add_argument('--force', action='store_true', help='bypass evidence skipping and completed-result reuse')
    p.add_argument('--dry-run', action='store_true', help='metadata plan only, without dependencies or raw input checks')
    p.add_argument('--json', action='store_true')
    return p


@dataclass
class Alignment:
    accession: str
    species: str
    protocol: str
    settings: dict
    reference: Path
    metadata: dict
    fingerprint: str = ''
    provenance: dict = field(default_factory=dict)

    @property
    def backend(self):
        return 'pbmm2' if self.settings['route'] == 'iso-seq' else 'minimap2'

    @property
    def namespace(self):
        return Path(self.species) / self.metadata['assembly']['name'] / self.metadata['annotation']['name']


def load_config(args):
    config = json.loads(args.config.read_text())
    if config.get('schema_version') != 1 or not isinstance(config.get('protocols'), dict):
        raise ValueError('unsupported alignment config schema')
    for key, settings in config['protocols'].items():
        if not isinstance(settings, dict) or settings.get('route') not in ROUTES:
            raise ValueError('invalid alignment route: ' + key)
        if set(settings) - {'route', 'max_intron', 'pychopper_kit'}:
            raise ValueError('unknown settings for protocol ' + key)
        if settings['route'] in ('flep', 'flep2'):
            positive_int(settings.get('max_intron', 12000))
        if settings['route'] == 'ont-pcr-cdna' and settings.get('pychopper_kit', 'PCS109') not in ('PCS109', 'PCS110', 'PCS111'):
            raise ValueError('unsupported pychopper kit for ' + key)
    resources = config.get('resources', {})
    for name in ['threads', 'sort_threads', 'pychopper_threads', 'jobs']:
        if getattr(args, name) is None:
            setattr(args, name, positive_int(resources.get(name, 1)))
    return config


def reference_for(args, code):
    candidates = []
    for path in sorted((args.references_dir / code).glob('*/*/reference.json')):
        meta = json.loads(path.read_text())
        assembly, annotation = meta['assembly']['name'], meta['annotation']['name']
        if args.reference_id and assembly != args.reference_id:
            continue
        if args.annotation_id and annotation != args.annotation_id:
            continue
        if path.parent.parent.name != assembly or path.parent.name != annotation or meta['species_code'] != code:
            raise ValueError('reference directory and metadata disagree: ' + str(path))
        candidates.append((path.parent, meta))
    if len(candidates) != 1:
        raise ValueError('expected exactly one reference for {}; found {}; select --reference-id/--annotation-id'.format(code, len(candidates)))
    return candidates[0]


def read_jobs(args, config):
    rows, seen = [], set()
    with args.samples.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f, delimiter='\t')
        if not reader.fieldnames or not {'Accession', 'Species', 'Protocol'} <= set(reader.fieldnames):
            raise ValueError('sample table requires Accession, Species and Protocol columns')
        for line, row in enumerate(reader, 2):
            if None in row or any(v is None for v in row.values()):
                raise ValueError('malformed sample row at line {}'.format(line))
            accession = row['Accession'].strip()
            if not re.fullmatch(r'(?:SRR|ERR|DRR|CRR)[0-9]+', accession) or accession in seen:
                raise ValueError('invalid or duplicate accession: ' + accession)
            seen.add(accession)
            rows.append((accession, species_code(row['Species']), args.protocol or row['Protocol'].strip()))
    if not rows:
        raise ValueError('sample table is empty')
    requested = set(args.accession)
    if requested - seen:
        raise ValueError('accessions absent from sample table: ' + ', '.join(sorted(requested - seen)))
    codes = {species_code(v) for v in args.species}
    rows = [r for r in rows if (not requested or r[0] in requested) and (not codes or r[1] in codes)]
    if requested and requested != {r[0] for r in rows}:
        raise ValueError('accession and species selectors conflict')
    jobs, references = [], {}
    for accession, code, protocol in rows:
        if protocol not in config['protocols']:
            raise ValueError('unsupported Protocol: ' + protocol)
        settings = dict(config['protocols'][protocol])
        if code not in references:
            references[code] = reference_for(args, code)
        ref, meta = references[code]
        jobs.append(Alignment(accession, code, protocol, settings, ref, meta))
    return jobs


def inputs_for(args, job):
    folder = child_path(args.raw_dir, Path(job.species) / job.accession)
    files = []
    for path in sorted(folder.rglob('*')) if folder.is_dir() else []:
        if path.is_file() and path.name.lower().endswith(('.fq', '.fastq', '.fq.gz', '.fastq.gz', '.gz')):
            child_path(folder, path.relative_to(folder))
            files.append(path)
    if not files:
        raise ValueError('no FASTQ files found for {} in {}'.format(job.accession, folder))
    return files


def tool_environment(job):
    names = ['samtools', job.backend]
    if job.settings['route'] == 'ont-pcr-cdna':
        names.extend(['pychopper', 'nhmmscan'])
    tools = {}
    for name in names:
        path = shutil.which(name)
        if not path:
            raise ValueError('required executable not found in PATH: ' + name)
        tools[name] = {'path': str(Path(path).absolute())}
    return tools


def identify_tool(name, tool, log, job, args, control):
    tool['executable_sha256'] = sha256(Path(tool['path']), control)
    if name == 'nhmmscan':
        tool['version'] = capture([tool['path'], '-h'], log, job, args, control).strip()
        return
    if name != 'pychopper':
        tool['version'] = capture([tool['path'], '--version'], log, job, args, control).strip()
        return
    # Pychopper does not implement --version. Probe help and, for a standard
    # Python console entry, ask that executable's own interpreter for package
    # metadata and source hashes (the adapter may run in a different environment).
    help_text = capture([tool['path'], '-h'], log, job, args, control)
    tool['help_sha256'] = hashlib.sha256(help_text.encode()).hexdigest()
    tool['version'] = None
    with Path(tool['path']).open('rb') as f:
        shebang = f.readline(4096).decode('utf-8', errors='replace').strip()
    interpreter = shlex.split(shebang[2:]) if shebang.startswith('#!') else []
    if interpreter and ('python' in Path(interpreter[0]).name or
                        (Path(interpreter[0]).name == 'env' and any('python' in v for v in interpreter[1:]))):
        probe = ('import hashlib,json; from importlib import metadata; '
                 'ds=list(metadata.distributions(name="pychopper")); '
                 'd=ds[0] if ds else None; '
                 'fs=sorted((str(p),str(d.locate_file(p))) for p in (d.files or []) '
                 'if str(p).startswith("pychopper/") and not str(p).endswith(".pyc")) if d else []; '
                 'h=hashlib.sha256(); '
                 '[(h.update(n.encode()),h.update(open(p,"rb").read())) for n,p in fs]; '
                 'print(json.dumps({"version":d.version if d else None,"package_sha256":h.hexdigest() if fs else None}))')
        tool.update(json.loads(capture(interpreter + ['-c', probe], log, job, args, control)))


def ensure_stage_owned(stage, job, previous):
    if not stage.exists() or not any(stage.iterdir()):
        return
    if not previous:
        raise ValueError('unregistered partial directory: ' + str(stage))
    backup = stage / 'output' / ('.previous-' + job.accession)
    if backup.exists():
        if backup.is_symlink():
            raise ValueError('symlink in previous output directory: ' + str(backup))
        ensure_owned_target(backup, {'files': previous['previous_files']})
    prefix = 'output/' + job.accession + '/'
    files = {'samples.txt', 'inputs/' + job.accession + '.fastq', 'reference/genome.fa',
             'output/logs/' + job.accession + '.alignment.log',
             'output/logs/' + job.accession + '.pychopper.log'}
    files.update(prefix + name for name in ['.alignment.lock', 'genome.sorted.bam', 'genome.sorted.bam.bai',
                                          'genome.sorted.flagstat.txt', 'alignment.qc.json', 'full_length.fq',
                                          'unclassified.fq', 'rescued.fq', 'pychopper_stats.tsv', 'pychopper_report.pdf'])
    directories = {'inputs', 'reference', 'output', 'output/logs', 'output/' + job.accession}
    for path in stage.rglob('*'):
        if path == backup or backup in path.parents:
            continue
        relative = str(path.relative_to(stage))
        temporary = relative.startswith(prefix) and re.fullmatch(
            r'(?:genome\.sorted\.tmp\.[0-9]+\.bam(?:\.bai)?|genome\.sorted\.(?:bam\.bai|flagstat\.txt)\.tmp\.[0-9]+|genome\.sorted\.sort\.[0-9]+\.bam)',
            relative[len(prefix):])
        if path.is_symlink():
            # This reserved link is only unlinked/recreated; never follow it for
            # cleanup. Its old target may legitimately differ from a new request.
            if relative != 'reference/genome.fa':
                raise ValueError('unregistered symlink in partial directory: ' + str(path))
        elif path.is_dir():
            if relative not in directories:
                raise ValueError('unregistered subdirectory in partial directory: ' + str(path))
        elif relative not in files and not temporary:
            raise ValueError('unregistered file in partial directory: ' + str(path))


def command_label(command):
    name = Path(command[0]).name
    return name + ' ' + command[1] if name == 'samtools' and len(command) > 1 else name


def capture(command, log, job, args, control):
    """Small command output only (versions, BAM header and read counts)."""
    check_cancel(control)
    with log.open('ab') as err:
        err.write(('\n{} command {}\n'.format(now(), json.dumps(command))).encode()); err.flush()
        with tempfile_output(log.parent) as output:
            process = subprocess.Popen(command, stdout=output, stderr=err, start_new_session=True,
                                       pass_fds=control.pass_fds)
            start = time.monotonic()
            try:
                while process.poll() is None:
                    check_cancel(control)
                    if args.timeout and time.monotonic() - start >= args.timeout:
                        raise RuntimeError('command timeout: ' + command_label(command))
                    control.cancelled.wait(0.05)
                if process.returncode:
                    raise RuntimeError('command exited with code {}: {}'.format(process.returncode, command_label(command)))
                output.seek(0)
                return output.read().decode('utf-8', errors='replace')
            finally:
                if process.poll() is None:
                    stop_process(process)


def tempfile_output(directory):
    import tempfile
    return tempfile.TemporaryFile(dir=str(directory))


def stop_commands(processes):
    """Stop all native process groups, including descendants of exited peers."""
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 1
    while any(process.poll() is None for process in processes) and time.monotonic() < deadline:
        time.sleep(0.02)
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_commands(commands, work, log, job, timeout, control):
    """Run one executable or a pipe; every process must exit successfully."""
    check_cancel(control)
    env = dict(os.environ, IRAGENT_ACCESSION=job.accession, IRAGENT_SPECIES=job.species)
    processes = []
    with log.open('ab') as output:
        output.write(('\n{} commands {}\n'.format(now(), json.dumps(commands))).encode())
        output.flush()
        started = time.monotonic()
        try:
            for index, command in enumerate(commands):
                previous = processes[-1] if processes else None
                process = subprocess.Popen(command, cwd=str(work), env=env,
                    stdin=previous.stdout if previous else None,
                    stdout=output if index == len(commands) - 1 else subprocess.PIPE,
                    stderr=output, start_new_session=True,
                    pass_fds=control.pass_fds)
                processes.append(process)
                if previous is not None:
                    # Let an exited consumer deliver SIGPIPE to its producer.
                    previous.stdout.close()
            while True:
                check_cancel(control)
                statuses = [p.poll() for p in processes]
                for command, status in zip(commands, statuses):
                    if status is not None and status != 0:
                        raise RuntimeError('{} exited with code {}'.format(command_label(command), status))
                if all(status is not None for status in statuses):
                    return
                if timeout and time.monotonic() - started >= timeout:
                    raise RuntimeError('alignment timeout after {} seconds'.format(timeout))
                control.cancelled.wait(0.05)
        finally:
            for process in processes:
                if process.stdout is not None:
                    process.stdout.close()
            stop_commands(processes)


def native_alignment(job, args, tools, reference, input_fastq, work, log, control):
    work.mkdir(parents=True, exist_ok=True)
    # Every attempt starts fresh. Only known generated outputs are removed.
    for path in work.iterdir():
        if path.name in OUTPUT_FILES or re.fullmatch(r'genome\.sorted\.sort\.[0-9]+\.bam', path.name):
            child_path(work, path.name)
            path.unlink()
    started = time.monotonic()

    def remaining():
        if not args.timeout:
            return 0
        value = args.timeout - (time.monotonic() - started)
        if value <= 0:
            raise RuntimeError('preprocessing/alignment timeout after {} seconds'.format(args.timeout))
        return value

    route = job.settings['route']
    if route == 'ont-pcr-cdna':
        full = work / 'full_length.fq'
        command = [tools['pychopper']['path'], '-t', str(args.pychopper_threads),
                   '-k', job.settings.get('pychopper_kit', 'PCS109'),
                   '-S', str(work / 'pychopper_stats.tsv'), '-u', str(work / 'unclassified.fq'),
                   '-w', str(work / 'rescued.fq'), '-r', str(work / 'pychopper_report.pdf'),
                   str(input_fastq), str(full)]
        run_commands([command], work, log, job, remaining(), control)
        if not full.is_file():
            raise ValueError('Pychopper produced no full_length.fq')
        validate_fastq(full, control)
        input_fastq = full
    genome = reference['genome']['path']
    bam = str(work / 'genome.sorted.bam')
    if route == 'iso-seq':
        command = [tools['pbmm2']['path'], 'align', '--preset', 'ISOSEQ', '--sort',
                   '-j', str(args.threads), '-J', str(args.sort_threads), '--bam-index', 'NONE',
                   genome, str(input_fastq), bam]
        run_commands([command], work, log, job, remaining(), control)
        return
    options = ['-ax', 'splice']
    if route in ('flep', 'flep2'):
        options += ['-G', str(job.settings.get('max_intron', 12000))]
    elif route == 'ont-direct-rna':
        options += ['-uf', '-k14']
    options += ['--secondary=no']
    mapper = [tools['minimap2']['path'], '-t', str(args.threads)] + options + [genome, str(input_fastq)]
    sorter = [tools['samtools']['path'], 'sort', '-@', str(args.sort_threads),
              '-T', str(work / 'genome.sorted.sort'), '-o', bam, '-']
    run_commands([mapper, sorter], work, log, job, remaining(), control)


def verified_reference(job, control):
    files = {}
    for key in ['genome', 'annotation']:
        entry = job.metadata['files'][key]
        path = child_path(job.reference, entry['path'])
        digest = sha256(path, control)
        if digest != entry['sha256']:
            raise ValueError('reference hash mismatch: ' + str(path))
        files[key] = {'path': str(path), 'sha256': digest}
    sequences, name, length = {}, None, 0
    with Path(files['genome']['path']).open() as f:
        for line in f:
            check_cancel(control)
            if line.startswith('>'):
                if name is not None:
                    sequences[name] = length
                name, length = line[1:].split()[0], 0
                if name in sequences:
                    raise ValueError('duplicate FASTA sequence name: ' + name)
            else:
                length += len(line.strip())
        if name is not None:
            sequences[name] = length
    if not sequences or any(n <= 0 for n in sequences.values()):
        raise ValueError('reference FASTA has empty sequences')
    return files, sequences


def prepare_input(paths, output, control):
    count = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('wb') as target:
        for path in paths:
            with path.open('rb') as f:
                compressed = f.read(2) == b'\x1f\x8b'
            with (gzip.open(str(path), 'rb') if compressed else path.open('rb')) as source:
                reads = 0
                while True:
                    check_cancel(control)
                    name = source.readline()
                    if not name:
                        break
                    seq, plus, qual = source.readline(), source.readline(), source.readline()
                    seq, qual = seq.rstrip(b'\r\n'), qual.rstrip(b'\r\n')
                    if not name.startswith(b'@') or not plus.startswith(b'+') or not seq or len(seq) != len(qual):
                        raise ValueError('invalid or truncated four-line FASTQ: ' + str(path))
                    target.write(name.rstrip(b'\r\n') + b'\n' + seq + b'\n' + plus.rstrip(b'\r\n') + b'\n' + qual + b'\n')
                    count += 1; reads += 1
                if not reads:
                    raise ValueError('empty FASTQ: ' + str(path))
    return count


def validate_bam(job, output, sequences, input_reads, log, args, control):
    bam = output / 'genome.sorted.bam'
    samtools = job.provenance['tools']['samtools']['path']
    if not bam.is_file() or not bam.stat().st_size:
        raise ValueError('alignment produced no BAM')
    capture([samtools, 'quickcheck', '-v', str(bam)], log, job, args, control)
    header = capture([samtools, 'view', '-H', str(bam)], log, job, args, control)
    names, sorted_coordinate = {}, False
    for line in header.splitlines():
        fields = line.split('\t')
        tags = dict(v.split(':', 1) for v in fields[1:] if ':' in v)
        if fields[0] == '@HD':
            sorted_coordinate = tags.get('SO') == 'coordinate'
        if fields[0] == '@SQ':
            if tags['SN'] in names:
                raise ValueError('duplicate reference sequence in BAM header')
            names[tags['SN']] = int(tags['LN'])
    if not sorted_coordinate or names != sequences:
        raise ValueError('BAM coordinate sort or reference sequence names/lengths do not match')
    # Building a fresh index also checks ordering throughout the BAM, rather than
    # relying on its sort-order declaration alone.
    capture([samtools, 'index', '-@', str(args.sort_threads), str(bam)], log, job, args, control)
    total = int(capture([samtools, 'view', '-c', '-F', '2304', str(bam)], log, job, args, control).strip())
    mapped = int(capture([samtools, 'view', '-c', '-F', '2308', str(bam)], log, job, args, control).strip())
    if mapped <= 0:
        raise ValueError('BAM has no mapped primary reads')
    statistics = capture([samtools, 'flagstat', '-@', str(args.sort_threads), str(bam)],
                         log, job, args, control)
    if not statistics.strip():
        raise ValueError('samtools flagstat produced empty statistics')
    (output / 'genome.sorted.flagstat.txt').write_text(statistics)
    qc = {'sample_id': job.accession, 'species': job.species, 'protocol': job.protocol,
          'reference_id': job.metadata['assembly']['name'], 'annotation_id': job.metadata['annotation']['name'],
          'input_reads': input_reads, 'primary_reads_in_bam': total, 'mapped_primary_reads': mapped,
          'primary_mapping_rate': mapped / total, 'fingerprint': job.fingerprint,
          'provenance': job.provenance, 'validated_at': now()}
    (output / 'alignment.qc.json').write_text(json.dumps(qc, indent=2, ensure_ascii=False) + '\n')
    receipts = []
    for path in sorted(output.rglob('*')):
        if path.is_file():
            child_path(output, path.relative_to(output))
            receipts.append({'path': str(path.relative_to(output)), 'size_bytes': path.stat().st_size,
                             'sha256': sha256(path, control), 'kind': 'bam' if path == bam else 'auxiliary'})
    return receipts


def item(job, status, error='', attempts=0):
    value = {'accession': job.accession, 'species': job.species, 'protocol': job.protocol,
             'backend': job.backend, 'reference_id': job.metadata['assembly']['name'],
             'annotation_id': job.metadata['annotation']['name'], 'status': status, 'attempts': attempts}
    if error:
        value['error'] = error
    return value


def pruned_input_identity(job, args, previous, target, control):
    """Recover original identity only for an authorized, still-useful cleanup."""
    raw = child_path(args.raw_dir, Path(job.species) / job.accession)
    record = journal(args.download_state_db, raw, 'fastq')
    if not record or record['status'] not in ('pruning', 'pruned') or not previous or previous['status'] != 'complete':
        return None
    qc_path = child_path(target, 'alignment.qc.json')
    qc = json.loads(qc_path.read_text())
    inputs = qc['provenance']['inputs']
    if all(Path(entry['path']).is_file() for entry in inputs):
        return None  # Restored inputs are hashed afresh, not assumed identical.
    consumer = record['payload']['consumer']
    allowed = authorized_files(args.download_state_db, raw, 'fastq', record['request_fingerprint'], control)
    if consumer['fingerprint'] != previous['fingerprint'] or consumer['target'] != str(target) or not allowed:
        raise ValueError('pruned FASTQ dependency is no longer valid; rebuild inputs with download --redownload')
    originals = set()
    for entry in inputs:
        relative = Path(entry['path']).relative_to(raw)
        path = child_path(raw, relative); originals.add(str(path))
        if path.is_file():
            if sha256(path, control) != entry['sha256']:
                raise ValueError('restored FASTQ changed; rebuild the complete input set')
        elif allowed.get(str(relative), {}).get('sha256') != entry['sha256']:
            raise ValueError('missing FASTQ has no matching cleanup receipt; rebuild inputs')
    try:
        present = inputs_for(args, job)
    except ValueError as exc:
        if not str(exc).startswith('no FASTQ files found'):
            raise
        present = []
    if any(str(path) not in originals for path in present):
        raise ValueError('new FASTQ files mixed with pruned inputs; rebuild the complete input set')
    return inputs


def finish_alignment(job, args, state, control, status, attempts=0):
    result = item(job, status, attempts=attempts)
    if args.keep_fastq:
        result['cleanup'] = [{'stage': 'fastq', 'status': 'retained_by_request'}]
        return result
    namespace = child_path(args.alignment_dir, job.namespace)
    target = child_path(namespace, job.accession)
    row = state.read(job, namespace)
    outcomes = []
    try:
        proof = alignment_proof(args.state_db, target, job.accession, job.species, job.reference / 'reference.json')
        derived = [entry for entry in row['files'] if entry['path'] in ('full_length.fq', 'unclassified.fq', 'rescued.fq')]
        if derived:
            outcomes.append(dict(prune(args.state_db, target, 'alignment_fastq', row['fingerprint'], derived, proof, control), stage='pychopper_fastq'))
        raw = child_path(args.raw_dir, Path(job.species) / job.accession)
        source = task(args.download_state_db, 'raw', raw, job.accession, job.species)
        if not source or source['status'] != 'complete':
            outcomes.append({'stage': 'raw_fastq', 'status': 'retained_unregistered'})
        else:
            qc = json.loads(child_path(target, 'alignment.qc.json').read_text())
            used = {entry['path']: entry['sha256'] for entry in qc['provenance']['inputs']}
            selected = [entry for entry in source['files'] if entry['kind'] == 'fastq' and
                        used.get(str(child_path(raw, entry['path']))) == entry['sha256']]
            if len(selected) != len(used):
                outcomes.append({'stage': 'raw_fastq', 'status': 'retained_unregistered'})
            else:
                with control.cleanup_lock:
                    with directory_lock(args.raw_dir):
                        outcomes.append(dict(prune(args.download_state_db, raw, 'fastq', source['fingerprint'], selected, proof, control), stage='raw_fastq'))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as exc:
        outcomes.append({'stage': 'fastq', 'status': 'failed', 'error': str(exc)})
    result['cleanup'] = outcomes
    return result


def align(job, args, state, control):
    namespace = child_path(args.alignment_dir, job.namespace)
    target = child_path(namespace, job.accession)
    stage = child_path(args.alignment_dir, Path('.partial') / job.namespace / job.accession)
    work = stage / 'output' / job.accession
    backup = work.parent / ('.previous-' + job.accession)
    previous, old_files, prepared, publishing, attempts = None, [], False, False, 0
    log = args.log_dir / job.namespace / (job.accession + '.log')
    try:
        previous = state.read(job, namespace)
        old_files = previous['files'] if previous else []
        publishing = bool(previous and previous['status'] == 'publishing')
        ensure_stage_owned(stage, job, previous)
        # Reconcile the old publication first, then evaluate the current request;
        # a changed protocol/input/reference or --force may need new alignment.
        if publishing:
            job.fingerprint = previous['fingerprint']
            if not valid_receipts(old_files, target, control, required_kind='bam'):
                if not valid_receipts(old_files, work, control, required_kind='bam'):
                    raise ValueError('pending publication cannot be verified; preserve files before recovery')
                promote(work, target, previous)
            retire_journals(args.state_db, target)
            state.write(job, namespace, 'complete', previous['attempts'], files=old_files,
                        previous_files=previous['previous_files'])
            cleanup_backup(backup, previous['previous_files'])
            previous = state.read(job, namespace)
            publishing = False
        if previous and previous['status'] == 'complete':
            cleanup_backup(backup, previous['previous_files'])
        ensure_owned_target(target, previous)
        tools = tool_environment(job)
        reference, sequences = verified_reference(job, control)
        restored = pruned_input_identity(job, args, previous, target, control)
        paths = [] if restored else inputs_for(args, job)
        inputs = restored or [{'path': str(p), 'sha256': sha256(p, control), 'size_bytes': p.stat().st_size} for p in paths]
        log.parent.mkdir(parents=True, exist_ok=True)
        for name in tools:
            identify_tool(name, tools[name], log, job, args, control)
        job.provenance = {'inputs': inputs, 'reference': reference, 'settings': job.settings,
                          'protocol': job.protocol,
                          'tools': tools, 'execution_backend': 'native', 'adapter_sha256': sha256(Path(__file__)),
                          'lifecycle_sha256': sha256(Path(__file__).with_name('artifact_lifecycle.py')),
                          'resources': {key: getattr(args, key) for key in ['threads', 'sort_threads', 'pychopper_threads']}}
        job.fingerprint = hashlib.sha256(json.dumps(job.provenance, sort_keys=True).encode()).hexdigest()
        if not args.force and previous and previous['fingerprint'] == job.fingerprint and previous['status'] == 'complete':
            omitted = authorized_files(args.state_db, target, 'alignment_fastq', previous['fingerprint'], control)
            omitted.update(authorized_files(args.state_db, target, 'bam', previous['fingerprint'], control))
            if kept_receipts_valid(old_files, target, omitted, control):
                if stage.exists():
                    shutil.rmtree(str(stage))
                status = 'reused' if (target / 'genome.sorted.bam').is_file() else 'skipped_downstream'
                return finish_alignment(job, args, state, control, status)
        if restored:
            raise ValueError('alignment request changed or output is unavailable after FASTQ pruning; rebuild inputs with download --redownload')
        if stage.exists() and any(stage.iterdir()) and (not previous or previous['fingerprint'] != job.fingerprint):
            # A completed managed stage is disposable; an unknown/incomplete
            # stage from another request is not silently adopted.
            if not previous or previous['status'] != 'complete':
                raise ValueError('partial directory belongs to a different or unregistered request: ' + str(stage))
            shutil.rmtree(str(stage))
        stage.mkdir(parents=True, exist_ok=True)
        prepared = True
        error = ''
        for attempts in range(1, args.retries + 2):
            check_cancel(control)
            state.write(job, namespace, 'running', attempts, files=old_files)
            try:
                reads = prepare_input(paths, stage / 'inputs' / (job.accession + '.fastq'), control)
                # Detect mutation during input adaptation before allowing a result
                # to be associated with the earlier input fingerprint.
                if any(sha256(p, control) != entry['sha256'] for p, entry in zip(paths, inputs)):
                    raise ValueError('input changed while preparing alignment')
                native_alignment(job, args, tools, reference,
                                 stage / 'inputs' / (job.accession + '.fastq'), work, log, control)
                files = validate_bam(job, work, sequences, reads, log, args, control)
                if any(sha256(Path(entry['path']), control) != entry['sha256'] for entry in reference.values()):
                    raise ValueError('reference changed during alignment; result was not published')
                check_cancel(control)
                state.write(job, namespace, 'publishing', attempts, files=files, previous_files=old_files)
                publishing = True
                promote(work, target, previous)
                retire_journals(args.state_db, target)
                state.write(job, namespace, 'complete', attempts, files=files, previous_files=old_files)
                cleanup_backup(backup, old_files)
                shutil.rmtree(str(stage))
                return finish_alignment(job, args, state, control, 'aligned', attempts)
            except (OSError, ValueError, RuntimeError, EOFError, zlib.error, sqlite3.Error) as exc:
                error = str(exc)
                with log.open('a') as f:
                    f.write('\n{} attempt {} FAILED: {}\n'.format(now(), attempts, error))
                if publishing:
                    return item(job, 'failed', error, attempts)
                if control.cancelled.is_set():
                    break
                if attempts <= args.retries:
                    control.cancelled.wait(args.retry_delay)
        status = 'interrupted' if control.cancelled.is_set() else 'failed'
        state.write(job, namespace, status, attempts, error, old_files)
        return item(job, status, error, attempts)
    except (OSError, ValueError, RuntimeError, EOFError, zlib.error, sqlite3.Error, KeyError) as exc:
        status = 'interrupted' if control.cancelled.is_set() else 'failed'
        if prepared and not publishing:
            state.write(job, namespace, status, attempts, str(exc), old_files)
        return item(job, status, str(exc), attempts)


def display(args, results):
    statuses = ['aligned', 'reused', 'skipped_evidence', 'skipped_downstream', 'planned', 'failed', 'interrupted']
    counts = {key: sum(r['status'] == key for r in results) for key in statuses}
    counts['cleanup_failed'] = sum(any(c['status'] == 'failed' for c in r.get('cleanup', [])) for r in results)
    summary = {'dry_run': args.dry_run, 'selected': len(results), 'counts': counts,
               'results': sorted(results, key=lambda r: r['accession'])}
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        for r in summary['results']:
            print('{} {} {} [{}]{}'.format(r['status'].upper(), r['species'], r['accession'], r['protocol'],
                                          (': ' + r['error'] if r.get('error') else '') +
                                          (' [evidence unchecked]' if r.get('evidence', {}).get('state') == 'unchecked' else '')))
        print('Summary: ' + ', '.join('{}={}'.format(k, v) for k, v in counts.items()))
    return 1 if counts['failed'] or counts['interrupted'] or counts['cleanup_failed'] else 0


def run(args):
    for name in ['samples', 'raw_dir', 'references_dir', 'alignment_dir', 'log_dir', 'state_db', 'evidence_dir', 'config', 'download_state_db']:
        setattr(args, name, getattr(args, name).expanduser().resolve())
    config = load_config(args)
    jobs = read_jobs(args, config)
    results, pending = [], []
    if args.dry_run:
        for job in jobs:
            value = item(job, 'planned')
            value['evidence'] = {'state': 'unchecked', 'reason': 'EVIDENCE_UNCHECKED'}
            results.append(value)
        return display(args, results)
    if args.ignore_evidence or args.force:
        pending = jobs
    else:
        from evidence_readiness import build_reference, inspect_evidence, request_for, ReadinessCache
        cache = ReadinessCache(args.evidence_cache_db)
        references = {}
        try:
            request = request_for(args.evidence_config)
            for job in jobs:
                try:
                    if job.namespace not in references:
                        references[job.namespace] = build_reference(job.reference, job.metadata)
                    decision = inspect_evidence(args.evidence_dir / job.namespace,
                        {'Accession': job.accession, 'Species': job.species, 'Protocol': job.protocol},
                        references[job.namespace], request=request, cache=cache)
                    if decision.state == 'missing':
                        pending.append(job)
                    else:
                        value = item(job, 'skipped_evidence' if decision.state == 'ready' else 'failed',
                                     '' if decision.state == 'ready' else decision.message)
                        value['evidence'] = decision.to_dict()
                        results.append(value)
                except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as exc:
                    results.append(item(job, 'failed', str(exc)))
        finally:
            cache.close()
    if not pending:
        return display(args, results)
    args.alignment_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(args.alignment_dir), os.O_RDONLY)
    control, handlers = Control(), {}
    control.lock_fd = descriptor
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('another alignment process is using this output directory')
        state = State(args.state_db)
        for signum in [signal.SIGINT, signal.SIGTERM]:
            handlers[signum] = signal.signal(signum, control.signal)
        with ThreadPoolExecutor(max_workers=args.jobs) as executor:
            futures = {executor.submit(align, job, args, state, control): job for job in pending}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append(item(futures[future], 'failed', str(exc)))
        code = display(args, results)
        return 128 + control.signum if control.signum else code
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        os.close(descriptor)


def main():
    args = parser().parse_args()
    try:
        return run(args)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error, argparse.ArgumentTypeError) as exc:
        if args.json:
            print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        else:
            print('ERROR: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
