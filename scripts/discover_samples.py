#!/usr/bin/env python3
"""Discover long-read RNA runs from their original archives and safely update metadata."""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
import uuid

from artifact_lifecycle import directory_lock
from discovery_io import atomic_json, digest
from discovery_metadata import identity, normalize_bundle, unlinked_candidate
from download_reads import PROJECT, species_code
from ena_source import EnaClient, MetadataLinkError, verify_response_cache
from ncbi_source import NcbiClient
from ddbj_source import DdbjClient,normalize_ddbj_bundle
from ngdc_source import NgdcClient,normalize_ngdc_bundle
from discovery_routing import resolve_sources,origin
from sample_catalog import read_catalog, update_catalog
from treatment_metadata import summarize_treatment


def nonnegative(value):
    number = int(value)
    if number < 0: raise argparse.ArgumentTypeError('must be nonnegative')
    return number


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0: raise argparse.ArgumentTypeError('must be finite and positive')
    return number


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('--source', choices=['auto','ncbi','ena','ddbj','ngdc'], default='auto',
                   help='original metadata archive; auto routes by run/project origin')
    p.add_argument('--species', action='append', required=True, help='species code or scientific name; repeatable')
    p.add_argument('--accession', action='append', default=[], help='restrict to SRR/ERR/DRR/CRR runs; repeatable')
    p.add_argument('--project', action='append', default=[], help='restrict to native projects/studies; repeatable')
    p.add_argument('--samples', type=Path, default=PROJECT / 'metadata/samples_info.tsv')
    p.add_argument('--discovery-dir', type=Path, default=PROJECT / 'metadata/discovery')
    p.add_argument('--config', type=Path, default=PROJECT / 'configs/alignment.json')
    p.add_argument('--batch-id', help='reuse this batch to apply a preview or resume interrupted work')
    p.add_argument('--max-records', type=nonnegative, default=100, help='maximum runs enriched in one batch; 0 means all')
    p.add_argument('--refresh-existing', action='store_true', help='explicitly fetch existing runs and fill unknown fields; preserve reported values')
    p.add_argument('--retry-pending', action='store_true', help='re-examine unchanged previously quarantined metadata')
    p.add_argument('--offline', action='store_true', help='reuse verified responses from an existing --batch-id; no HTTP')
    p.add_argument('--timeout', type=positive_seconds, default=45)
    p.add_argument('--retries', type=nonnegative, default=2)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='commit accepted changes to the sample catalog')
    mode.add_argument('--dry-run', action='store_true', help='preview only (default); retrieval/audit files are still saved')
    p.add_argument('--json', action='store_true')
    return p


def load_candidates(batch, verify_responses=True):
    scan = json.loads((batch / 'scan.json').read_text())
    raw = (batch / 'candidates.json').read_bytes()
    if digest(raw) != scan['candidates_sha256']: raise ValueError('candidate cache hash mismatch: ' + str(batch))
    if verify_responses: verify_response_cache(batch)
    return json.loads(raw), scan


def normalization_signature():
    paths={Path(fn.__code__.co_filename)for fn in (normalize_bundle,normalize_ddbj_bundle,normalize_ngdc_bundle,summarize_treatment)}
    return identity({path.name:digest(path.read_bytes())for path in paths})


def history(root, current, protocols):
    groups, pending = [], set()
    normalizer_sha256 = normalization_signature()
    protocol_config_sha256 = identity(protocols)
    for path in sorted(root.glob('*/*/scan.json')):
        if path.parent == current: continue
        candidates, scan = load_candidates(path.parent, verify_responses=False)
        for c in candidates:
            groups.append(c['aliases'] + [c['accession']])
            if (c['issues'] and scan.get('normalizer_sha256') == normalizer_sha256
                    and scan.get('protocol_config_sha256') == protocol_config_sha256):
                pending.add((c['accession'], c.get('discovery_fingerprint',c['source_fingerprint'])))
    return groups, pending


def summary(plan, scan, batch):
    return {'status': plan['status'], 'source': scan.get('source','ena'), 'batch_id': batch.name, 'batch': str(batch),
            'added_samples': len(plan['additions']), 'updated_samples': len(plan['updates']),
            'pending_samples': len(plan['pending']), 'conflicting_samples': len({c['accession'] for c in plan['conflicts']}),
            'unchanged_samples': len(plan['unchanged']), 'skipped_existing': len(scan['skipped_existing']),
            'skipped_pending': len(scan['skipped_pending']), 'deferred_samples': len(scan['deferred']),
            'retrieved_runs': scan['retrieved_runs'], 'snapshot': plan['snapshot'],
            'snapshot_sha256': plan['snapshot_sha256'], 'preview': str(batch / 'preview.json'),
            'recovered': plan['recovered'], 'catalog_changed': plan['catalog_before_sha256'] != plan['catalog_after_sha256']}


def run(args):
    if args.offline and not args.batch_id: raise ValueError('--offline requires an existing --batch-id')
    for name in ('samples', 'discovery_dir', 'config'):
        path = getattr(args, name).expanduser()
        if name == 'samples' and path.is_symlink(): raise ValueError('sample catalog must not be a symbolic link')
        setattr(args, name, path.resolve())
    species = sorted({species_code(s) for s in args.species})
    routes=resolve_sources(args.source,args.accession,args.project)
    config = json.loads(args.config.read_text())
    if config.get('schema_version') != 1 or not isinstance(config.get('protocols'), dict):
        raise ValueError('unsupported alignment config schema')
    protocols = config['protocols']
    batch_id = args.batch_id or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', batch_id): raise ValueError('invalid batch-id')
    root = args.discovery_dir; batch = root / args.source / batch_id
    request = {'schema_version': 2, 'source': args.source, 'routes':routes, 'species': species, 'accessions': sorted(set(args.accession)),
               'projects': sorted(set(args.project)), 'samples': str(args.samples), 'protocols': protocols,
               'max_records': args.max_records, 'refresh_existing': args.refresh_existing,
               'retry_pending': args.retry_pending}
    if args.offline and not (batch / 'request.json').is_file(): raise ValueError('offline batch does not exist')
    batch.mkdir(parents=True, exist_ok=True)
    with directory_lock(batch):
        args._batch = batch
        request_path = batch / 'request.json'
        if request_path.exists():
            saved=json.loads(request_path.read_text())
            frozen=saved.get('routes',{})
            if (args.source=='auto'and not args.accession and not args.project and frozen
                    and set(frozen)<=set(routes)and all(frozen[key]==routes[key]for key in frozen)):
                routes=frozen;request['routes']=frozen
            if saved != request: raise ValueError('batch request changed; use a new batch-id')
        else: atomic_json(request_path, request)
        known_aliases, pending = history(root, batch, protocols)
        if (batch / 'scan.json').exists():
            candidates, scan = load_candidates(batch)
        else:
            _, rows = read_catalog(args.samples); existing = {r['Accession'] for r in rows}
            alias_lookup = {}
            for group in known_aliases:
                for a in group: alias_lookup.setdefault(a, set()).update(group)
            records = {}; codes = {}; clients={}; owners={}
            for source,selectors in routes.items():
                factory={'ncbi':NcbiClient,'ena':EnaClient,'ddbj':DdbjClient,'ngdc':NgdcClient}[source]
                client=factory(batch,offline=args.offline,timeout=args.timeout,retries=args.retries)
                clients[source]=client
                for code in species:
                    options=dict(accessions=selectors['accessions'],projects=selectors['projects'],allow_missing=True)
                    if source=='ena':options['native_only']=True
                    for row in client.search(code,**options):
                        accession=row['run_accession']
                        if origin(accession)!=source:continue
                        if accession in records and records[accession]!=row:raise ValueError('run returned conflicting species queries: '+accession)
                        records[accession]=row;codes[accession]=code;owners[accession]=source
            missing = set(request['accessions']) - set(records)
            if missing: raise ValueError('requested runs not returned by their native species/RNA/platform query: ' + ', '.join(sorted(missing)))
            candidates = []; skipped_existing = []; skipped_pending = []; deferred = []
            for accession, row in sorted(records.items()):
                if not args.refresh_existing and ({accession} | alias_lookup.get(accession, set())) & existing:
                    skipped_existing.append(accession); continue
                if not args.retry_pending and (accession, identity(row)) in pending:
                    skipped_pending.append(accession); continue
                if args.max_records and len(candidates) >= args.max_records:
                    deferred.append(accession); continue
                try:
                    source=owners[accession]
                    normalize={'ncbi':normalize_bundle,'ena':normalize_bundle,
                               'ddbj':normalize_ddbj_bundle,'ngdc':normalize_ngdc_bundle}[source]
                    candidate = normalize(clients[source].bundle(row), codes[accession], protocols)
                except MetadataLinkError as exc:
                    evidence=clients[source].link_evidence(row)if hasattr(clients[source],'link_evidence')else None
                    candidate = unlinked_candidate(row, codes[accession], str(exc),source=source.upper(),entries=evidence)
                candidate['discovery_fingerprint']=identity(row)
                candidates.append(candidate)
            atomic_json(batch / 'candidates.json', candidates)
            scan = {'schema_version': 2, 'source':args.source,'retrieved_runs': len(records), 'skipped_existing': skipped_existing,
                    'skipped_pending': skipped_pending, 'deferred': deferred,
                    'normalizer_sha256': normalization_signature(),
                    'protocol_config_sha256': identity(protocols),
                    'candidates_sha256': digest((batch / 'candidates.json').read_bytes())}
            atomic_json(batch / 'scan.json', scan)
        plan = update_catalog(args.samples, candidates, batch, apply=args.apply,
                              refresh_existing=args.refresh_existing, known_aliases=known_aliases,strict_origin=True)
        return summary(plan, scan, batch)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = run(args); code = 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        result = {'status': 'failed', 'error': str(exc)}; code = 1
    except KeyboardInterrupt:
        result = {'status': 'interrupted', 'error': 'rerun the same --batch-id to resume'}; code = 130
    if code and hasattr(args, '_batch'):
        result.update({'batch_id': args._batch.name, 'batch': str(args._batch),
                       'resume': 'rerun the same arguments and --batch-id'})
        try: atomic_json(args._batch / 'failure.json', result)
        except OSError: pass
    if args.json: print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__': sys.exit(main())
