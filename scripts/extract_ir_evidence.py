#!/usr/bin/env python3
"""Validated, recoverable per-sample IR evidence. Python 3.8+, Linux."""
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import sys
import tempfile

from align_reads import capture, read_jobs, run_commands, verified_reference
from download_reads import (PROJECT, SPECIES, Control, check_cancel, child_path, nonnegative_float,
                            nonnegative_int, now, sha256)
from evidence_store import (EvidenceState, clear_stage, ensure_stage, existing_files,
                            publish, registration)
from artifact_lifecycle import (authorized_files, directory_lock, evidence_proof,
                                journal, prune, task)

from evidence_identity import equivalent_extraction
from evidence_contract import validate_parameters,DEFAULT_PARAMETERS
from evidence_readiness import build_reference,inspect_evidence,request_for,ReadinessCache,EvidenceDecision

VALIDATOR = Path(__file__).with_name('evidence_validation.py')
PARAMETERS = DEFAULT_PARAMETERS
PROTOCOLS = ['Iso-seq', 'ONT cDNA-seq', 'ONT PCR-cDNA-seq', 'ONT direct RNA-seq', 'FLEP-seq', 'FLEP-seq2']


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    for flag, default in [('samples', 'metadata/samples_info.tsv'), ('references-dir', 'storage/references'),
                          ('alignment-dir', 'storage/alignments'), ('alignment-state-db', 'state/alignments.sqlite3'),
                          ('evidence-dir', 'storage/evidence'), ('state-db', 'state/evidence.sqlite3'),
                          ('log-dir', 'logs/evidence'), ('config', 'configs/evidence.json')]:
        p.add_argument('--' + flag, type=Path, default=PROJECT / default)
    for name in ['accession', 'species']:
        p.add_argument('--' + name, action='append', default=[])
    p.add_argument('--reference-id'); p.add_argument('--annotation-id')
    p.add_argument('--protocol', choices=PROTOCOLS, help='explicit override for selected samples; must match BAM QC')
    p.add_argument('--plantvelo-python', default=sys.executable, help='Python with installed PlantVelo and pysam')
    for key in ['min_mapq', 'min_anchor', 'splice_tolerance']:
        p.add_argument('--' + key.replace('_', '-'), type=nonnegative_int)
    p.add_argument('--min-intron-coverage', type=float)
    p.add_argument('--strand-mode', choices=['unstranded', 'tag', 'flag', 'tag_then_flag'])
    p.add_argument('--retries', type=nonnegative_int, default=0)
    p.add_argument('--retry-delay', type=nonnegative_float, default=5)
    p.add_argument('--timeout', type=nonnegative_float, default=0, help='seconds per external command; 0 disables')
    p.add_argument('--force', action='store_true', help='replace managed legacy evidence and recompute native evidence')
    p.add_argument('--evidence-cache-db', type=Path, help='optional shared evidence validation cache')
    p.add_argument('--keep-bam', action='store_true', help='retain registered BAM/BAI after durable evidence publication')
    p.add_argument('--dry-run', action='store_true', help='read-only metadata plan; no BAM/tool/hash checks')
    p.add_argument('--json', action='store_true')
    return p


def effective_config(args):
    value = json.loads(args.config.read_text())
    if set(value) - {'schema_version', 'parameters'} or type(value.get('schema_version'))is not int or value.get('schema_version') != 1:
        raise ValueError('unsupported evidence configuration')
    configured = value.get('parameters', {})
    if not isinstance(configured, dict) or set(configured) - set(PARAMETERS):
        raise ValueError('unknown evidence parameters')
    parameters = dict(PARAMETERS, **configured)
    for key in parameters:
        override = getattr(args, key)
        if override is not None:
            parameters[key] = override
    return validate_parameters(parameters)


def identity_for(job, reference, target):
    return {'species_code': job.species, 'reference_id': job.metadata['assembly']['name'],
            'annotation_id': job.metadata['annotation']['name'],
            'reference_metadata_path': os.path.relpath(job.reference / 'reference.json', target),
            'reference_sha256': {k: v['sha256'] for k, v in reference.items()}}


def registered_inputs(job, args, reference, control, report_fingerprint=None):
    """Read alignment receipts without opening/creating its state database writable."""
    namespace = child_path(args.alignment_dir, job.namespace)
    folder = child_path(namespace, job.accession)
    if not args.alignment_state_db.is_file():
        raise ValueError('alignment state database is missing; only registered BAMs are accepted')
    from contextlib import closing
    with closing(sqlite3.connect(args.alignment_state_db.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM downloads WHERE accession=? AND species=? AND raw_root=?',
                         (job.accession, job.species, str(namespace))).fetchone()
    if row is None or row['status'] != 'complete':
        raise ValueError('no complete registered alignment for this sample/reference')
    payload = json.loads(row['files_json'])
    receipts = payload if isinstance(payload, list) else payload['files']
    entries = {r['path']: r for r in receipts}
    omitted = authorized_files(args.alignment_state_db, folder, 'bam', row['fingerprint'], control) if report_fingerprint else {}
    pruned = journal(args.alignment_state_db, folder, 'bam')
    if pruned and pruned['payload']['consumer']['fingerprint'] != report_fingerprint:
        omitted = {}
    inputs = {}
    for name in ['genome.sorted.bam', 'genome.sorted.bam.bai', 'alignment.qc.json']:
        path = child_path(folder, name)
        entry = entries.get(name)
        if entry and not path.exists() and omitted.get(name) == entry:
            inputs[name] = {'path': str(path), 'sha256': entry['sha256'], 'size_bytes': entry['size_bytes']}
            continue
        if not entry or not path.is_file() or path.stat().st_size != entry['size_bytes'] or sha256(path, control) != entry['sha256']:
            if pruned:
                raise ValueError('BAM was pruned and its dependency is unavailable or changed; rebuild the registered alignment')
            raise ValueError('missing or modified registered alignment input: ' + str(path))
        inputs[name] = {'path': str(path), 'sha256': entry['sha256'], 'size_bytes': entry['size_bytes']}
    qc = json.loads(Path(inputs['alignment.qc.json']['path']).read_text())
    expected = {'sample_id': job.accession, 'species': job.species, 'protocol': job.protocol,
                'reference_id': job.metadata['assembly']['name'], 'annotation_id': job.metadata['annotation']['name'],
                'fingerprint': row['fingerprint']}
    if any(qc.get(k) != v for k, v in expected.items()):
        raise ValueError('alignment QC sample/protocol/reference/fingerprint does not match')
    for key, entry in reference.items():
        if qc.get('provenance', {}).get('reference', {}).get(key, {}).get('sha256') != entry['sha256']:
            raise ValueError('alignment QC reference hashes do not match')
    return inputs, row['fingerprint']


def result(job, status, error='', attempts=0):
    value = {'accession': job.accession, 'species': job.species, 'protocol': job.protocol,
             'reference_id': job.metadata['assembly']['name'], 'annotation_id': job.metadata['annotation']['name'],
             'status': status, 'attempts': attempts}
    if error:
        value['error'] = error
    return value


def scientific_check(executable, mode, request_path, log, job, args, control):
    command = [executable, '-B', str(VALIDATOR), mode]
    if request_path is not None:
        command.append(str(request_path))
    try:
        return json.loads(capture(command, log, job, args, control))
    except RuntimeError as exc:
        # The helper emits a concise reason on stderr. Return it in structured
        # task errors as well as retaining the full central log.
        if 'exited with code' in str(exc):
            with log.open('rb') as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 4096))
                lines = handle.read().decode('utf-8', errors='replace').strip().splitlines()
            detail = lines[-1] if lines else str(exc)
            raise RuntimeError('{} check failed: {} (log: {})'.format(mode, detail, log)) from exc
        raise


def finish_evidence(job, args, control, status, attempts=0):
    result_value = result(job, status, attempts=attempts)
    if args.keep_bam:
        result_value['cleanup'] = [{'stage': 'bam', 'status': 'retained_by_request'}]
        return result_value
    target = child_path(args.alignment_dir, job.namespace / job.accession)
    try:
        row = task(args.alignment_state_db, 'alignment', target, job.accession, job.species)
        proof = evidence_proof(args.state_db, child_path(args.evidence_dir, job.namespace), job.accession, job.species)
        files = [entry for entry in row['files'] if entry['path'] in ('genome.sorted.bam', 'genome.sorted.bam.bai')]
        outcome = prune(args.alignment_state_db, target, 'bam', row['fingerprint'], files, proof, control)
        result_value['cleanup'] = [dict(outcome, stage='bam')]
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as exc:
        result_value['cleanup'] = [{'stage': 'bam', 'status': 'failed', 'error': str(exc)}]
    return result_value


def extract(job, args, parameters, state, control):
    target = child_path(args.evidence_dir, job.namespace)
    stage = child_path(args.evidence_dir, Path('.partial') / job.namespace / job.accession)
    tsv = job.accession + '.intron_prior.tsv'
    names = [tsv, tsv + '.run.json']
    row = state.read(job.accession, target)
    fingerprint, payload, attempts, publishing, prepared = '', {}, 0, False, False
    log = child_path(args.log_dir, job.namespace / (job.accession + '.log'))
    try:
        ensure_stage(stage, row, names)
        if row and row['status'] == 'publishing':
            publishing = True
            publish(target, stage, row['payload'])
            state.write(job.accession, target, row['fingerprint'], 'complete', row['attempts'], row['payload'])
            publishing = False
            if stage.exists():
                shutil.rmtree(str(stage))
            row = state.read(job.accession, target)
        reference, sequences = verified_reference(job, control)
        identity = identity_for(job, reference, target)
        record = registration(target, identity)
        old = existing_files(target, names, record, control)
        executable = shutil.which(args.plantvelo_python)
        software = None
        if not args.force and tsv in record['file_sha256']:
            sample={'Accession':job.accession,'Species':job.species,'Protocol':job.protocol}
            contexts=getattr(args,'_reference_contexts',{})
            if str(job.reference)not in contexts:
                contexts[str(job.reference)]=build_reference(job.reference,job.metadata,control)
            args._reference_contexts=contexts
            policy=None
            if names[1]in record['file_sha256']:
                if not executable:raise ValueError('PlantVelo Python executable not found: '+args.plantvelo_python)
                executable=str(Path(executable).absolute())
                identities=getattr(args,'_software_identities',{})
                if executable not in identities:
                    log.parent.mkdir(parents=True,exist_ok=True)
                    identities[executable]=scientific_check(executable,'identity',None,log,job,args,control)
                args._software_identities=identities;software=identities[executable]
                policy=request_for(args.config,parameters=parameters,software=software)
            decision=inspect_evidence(target,sample,contexts[str(job.reference)],request=policy,
                cache=getattr(args,'_readiness_cache',None),control=control)
            if decision.state=='unchecked':
                if not executable:raise ValueError('PlantVelo Python executable not found: '+args.plantvelo_python)
                log.parent.mkdir(parents=True,exist_ok=True)
                with tempfile.NamedTemporaryFile(mode='w',prefix='.readiness-',suffix='.json',dir=str(log.parent),delete=False)as handle:
                    request_path=Path(handle.name)
                    json.dump(dict(namespace=str(target),sample=sample,reference_folder=str(job.reference),
                        metadata=job.metadata,policy=dict(policy)if policy is not None else None,
                        cache_db=str(args.evidence_cache_db)if args.evidence_cache_db else None),handle,allow_nan=False)
                try:
                    checked=scientific_check(executable,'readiness',request_path,log,job,args,control)
                    checked['differences']=tuple(checked['differences'])
                    decision=EvidenceDecision(**checked)
                finally:request_path.unlink()
            if decision.state!='ready':
                code=decision.reason
                if(decision.kind=='native'and not child_path(args.alignment_dir,job.namespace/job.accession/'genome.sorted.bam').is_file()
                        and(decision.state=='incompatible'or not all(child_path(target,name).is_file()for name in names))):
                    code='EVIDENCE_REBUILD_REQUIRED'
                raise ValueError(code+': '+decision.message)
            if not(row and row['status']!='complete') and not(stage.exists()and any(stage.iterdir())):
                if decision.kind=='validated_import':return result(job,'skipped_validated_import')
                report=json.loads(child_path(target,names[1]).read_text())
                payload={'identity':identity,'old_files':old,'files':old}
                state.write(job.accession,target,report['agent']['fingerprint'],'complete',row['attempts']if row else 0,payload)
                return finish_evidence(job,args,control,'reused')
        old_report = json.loads(child_path(target, names[1]).read_text()) if all(name in old for name in names) else {}
        report_fingerprint = old_report.get('agent', {}).get('fingerprint')
        inputs, alignment_fingerprint = registered_inputs(job, args, reference, control, report_fingerprint)
        if not executable:
            raise ValueError('PlantVelo Python executable not found: ' + args.plantvelo_python)
        executable = str(Path(executable).absolute())
        log.parent.mkdir(parents=True, exist_ok=True)
        software = software or scientific_check(executable, 'identity', None, log, job, args, control)
        code = {p.name: sha256(p, control) for p in [Path(__file__), VALIDATOR, Path(__file__).with_name('evidence_store.py'),
                                                   Path(__file__).with_name('align_reads.py'), Path(__file__).with_name('download_reads.py'),
                                                   Path(__file__).with_name('artifact_lifecycle.py'), Path(__file__).with_name('evidence_identity.py'),
                                                   Path(__file__).with_name('evidence_contract.py'),Path(__file__).with_name('evidence_readiness.py')]}
        provenance = {'inputs': inputs, 'alignment_fingerprint': alignment_fingerprint,
                      'reference': reference, 'parameters': parameters, 'protocol': job.protocol,
                      'species': job.species, 'software': software, 'python': executable,
                      'python_sha256': sha256(Path(executable), control), 'code_sha256': code}
        fingerprint = hashlib.sha256(json.dumps(provenance, sort_keys=True, allow_nan=False).encode()).hexdigest()
        if stage.exists() and any(stage.iterdir()) and row and row['fingerprint'] != fingerprint and row['status'] != 'complete':
            raise ValueError('partial evidence belongs to a different request; preserve it before changing parameters')
        if not args.force and all(name in old for name in names):
            report = json.loads(child_path(target, names[1]).read_text())
            if (report.get('agent', {}).get('fingerprint') == fingerprint
                    or equivalent_extraction(report.get('agent', {}).get('provenance', {}), provenance)):
                payload = {'identity': identity, 'old_files': old, 'files': old}
                state.write(job.accession, target, report['agent']['fingerprint'], 'complete', row['attempts'] if row else 0, payload)
                if stage.exists():
                    shutil.rmtree(str(stage))
                return finish_evidence(job, args, control, 'reused')
        if any(not Path(entry['path']).is_file() for entry in inputs.values()):
            raise ValueError('evidence request changed or force was requested after BAM pruning; rebuild the registered alignment')
        # A missing managed output still has an old registration entry. Keep
        # that receipt so publication can distinguish loss from foreign edits.
        payload = {'identity': identity,
                   'old_files': {name: record['file_sha256'][name] for name in names if name in record['file_sha256']},
                   'files': {}}
        for attempts in range(1, args.retries + 2):
            check_cancel(control)
            state.write(job.accession, target, fingerprint, 'running', attempts, payload)
            prepared = True
            try:
                clear_stage(stage, row, names)
                row = state.read(job.accession, target)
                request = {'bam': inputs['genome.sorted.bam']['path'], 'gtf': reference['annotation']['path'],
                           'out': str(stage / tsv), 'sample_id': job.accession, 'protocol': job.protocol,
                           'species': SPECIES[job.species].replace(' ', '_'), 'parameters': parameters,
                           'sequences': sequences, 'software': software}
                request_path = stage / 'request.json'
                request_path.write_text(json.dumps(request, allow_nan=False))
                scientific_check(executable, 'input', request_path, log, job, args, control)
                command = [executable, '-B', '-m', 'plantvelo.irprior']
                for key in ['gtf', 'bam', 'sample_id', 'protocol', 'species', 'out']:
                    command += ['--' + key.replace('_', '-'), request[key]]
                for key, value in parameters.items():
                    command += ['--' + key.replace('_', '-'), str(value)]
                run_commands([command], stage, log, job, args.timeout, control)
                for name in names:
                    path = child_path(stage, name)
                    if not path.is_file():
                        raise ValueError('PlantVelo did not produce required output: ' + name)
                validation = scientific_check(executable, 'output', request_path, log, job, args, control)
                # Never associate counts with inputs/reference bytes that changed during extraction.
                for entry in list(inputs.values()) + list(reference.values()):
                    if sha256(Path(entry['path']), control) != entry['sha256']:
                        raise ValueError('input or reference changed during extraction; evidence was not published')
                report_path = stage / names[1]
                report = json.loads(report_path.read_text())
                report['agent'] = {'fingerprint': fingerprint, 'provenance': provenance, 'validation': validation,
                                   'validated_at': now()}
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
                payload['files'] = {name: sha256(child_path(stage, name), control) for name in names}
                check_cancel(control)
                state.write(job.accession, target, fingerprint, 'publishing', attempts, payload)
                publishing = True
                publish(target, stage, payload)
                state.write(job.accession, target, fingerprint, 'complete', attempts, payload)
                publishing = False
                prepared = False
                shutil.rmtree(str(stage))
                return finish_evidence(job, args, control, 'extracted', attempts)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as exc:
                with log.open('a') as handle:
                    handle.write('\n{} attempt {} FAILED: {}\n'.format(now(), attempts, exc))
                if publishing:
                    return result(job, 'failed', str(exc), attempts)
                if control.cancelled.is_set() or attempts > args.retries:
                    raise
                control.cancelled.wait(args.retry_delay)
        raise RuntimeError('extraction attempts exhausted')
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as exc:
        status = 'interrupted' if control.cancelled.is_set() else 'failed'
        if prepared and not publishing:
            state.write(job.accession, target, fingerprint, status, attempts, payload, str(exc))
        return result(job, status, str(exc), attempts)


def display(args, results):
    statuses = ['extracted', 'reused', 'skipped_legacy', 'skipped_validated_import', 'planned', 'failed', 'interrupted']
    counts = {s: sum(r['status'] == s for r in results) for s in statuses}
    counts['cleanup_failed'] = sum(any(c['status'] == 'failed' for c in r.get('cleanup', [])) for r in results)
    summary = {'dry_run': args.dry_run, 'selected': len(results), 'counts': counts, 'results': results}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print('{} {} {}{}'.format(r['status'].upper(), r['species'], r['accession'], ': ' + r['error'] if r.get('error') else ''))
        print('Summary: ' + ', '.join('{}={}'.format(k, v) for k, v in counts.items()))
    return 1 if counts['failed'] or counts['interrupted'] or counts['cleanup_failed'] else 0


def run(args):
    for name in ['samples', 'references_dir', 'alignment_dir', 'alignment_state_db', 'evidence_dir', 'state_db', 'log_dir', 'config']:
        setattr(args, name, getattr(args, name).expanduser().resolve())
    parameters = effective_config(args)
    jobs = read_jobs(args, {'protocols': {name: {} for name in PROTOCOLS}})
    if args.dry_run:
        return display(args, [result(job, 'planned') for job in jobs])
    if not jobs:
        return display(args, [])
    args._readiness_cache=ReadinessCache(path=args.evidence_cache_db)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(args.evidence_dir), os.O_RDONLY)
    control, handlers = Control(), {}
    alignment_lock = None
    control.lock_fd = descriptor
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('another evidence process is using this output directory')
        if args.alignment_dir.exists():
            alignment_lock = directory_lock(args.alignment_dir)
            held = alignment_lock.__enter__()
            control.extra_lock_fds = (held,)
        state = EvidenceState(args.state_db)
        for signum in [signal.SIGINT, signal.SIGTERM]:
            handlers[signum] = signal.signal(signum, control.signal)
        results = [extract(job, args, parameters, state, control) for job in jobs]
        code = display(args, results)
        return 128 + control.signum if control.signum else code
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        os.close(descriptor)
        if alignment_lock is not None:
            alignment_lock.__exit__(None, None, None)
        args._readiness_cache.close()


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
