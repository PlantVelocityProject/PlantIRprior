#!/usr/bin/env python3
"""Deliver validated scientific releases to the static website, index last."""
import argparse
from contextlib import ExitStack
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import zipfile

from artifact_lifecycle import directory_lock
from download_reads import PROJECT, sha256
from discovery_io import atomic_write
from prior_sync import configuration, intervals, OUTPUTS
from task_contract import ContractError

SPECIES = {
    'ath': ('Arabidopsis thaliana', 'Arabidopsis', 'arabidopsis'),
    'gmx': ('Glycine max', 'Soybean', 'soybean'),
    'zma': ('Zea mays', 'Maize', 'maize'),
    'sly': ('Solanum lycopersicum', 'Tomato', 'tomato'),
    'osa': ('Oryza sativa', 'Rice', 'rice'),
}
SAMPLE_COLUMNS = ['Accession', 'BioSample', 'BioProject', 'Species', 'Tissue',
                  'Treatment', 'Ecotype', 'Genotype', 'Platform', 'Protocol', 'Database']
VERSION = re.compile(r'g([0-9]{6})-[a-f0-9]{10}')


def reference_species(name):
    # These are the reference taxon labels used by the manuscript's rice row.
    # Assembly and annotation identities still have to match independently.
    return {'Oryza sativa Japonica': 'Oryza sativa',
            'Oryza sativa Japonica Group': 'Oryza sativa'}.get(name, name)


def species_metadata(website, latest, records):
    references = {}
    if (website/'data/reference_genomes.csv').exists():
        _, rows = read_csv(website/'data/reference_genomes.csv')
        references = {(reference_species(r['species']),r['genome_assembly'],r['annotation_version']):r['assembly_accession'] for r in rows}
    species = {}
    for code,key in latest.items():
        record = records[key]; name,common,art = SPECIES.get(code,(code,code,'arabidopsis'))
        species[code] = {'name':name,'common':common,'art':art,'events':record['events'],'samples':record['samples'],
                         'assembly':record['reference_id'],'annotation':record['annotation_id'],
                         'accession':references.get((name,record['reference_id'],record['annotation_id']),'')}
    return species


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)+'\n').encode()


def method_metadata(website, records):
    """Expose documented release policy without inventing historical settings."""
    result = {}
    for key,record in records.items():
        provenance = json.loads(safe_path(website,record['files']['provenance']['path']).read_text())
        kinds = {}; native = []; historical_verified = []
        for sample in provenance['samples']:
            signature = sample.get('compatibility',{})
            kind = signature.get('kind','unknown'); kinds[kind] = kinds.get(kind,0)+1
            if kind == 'native' and isinstance(signature.get('parameters'),dict):
                if signature['parameters'] not in native: native.append(signature['parameters'])
            if kind == 'validated_import': historical_verified.append(signature.get('historical_parameters_verified',False))
        result[key] = {'input_kinds':kinds,'native_parameters':native,
            'admission_parameters':provenance.get('evidence_admission',{}).get('native_policy',{}).get('parameters'),
            'historical_parameters_verified':all(historical_verified) if historical_verified else None}
    return result


def refresh_asset_versions(website):
    """Version mutable entry assets without changing immutable release paths."""
    html = website/'index.html'
    if not html.is_file(): return False
    original = html.read_text()
    paths = ['app.js','releases.js','data/releases/index.js','assets/site.css',
             'assets/releases.css','assets/methods.css','assets/ui.css','assets/ui.js','assets/fonts/fonts.css','assets/logo.svg']
    pattern = re.compile(r'\b(src|href)="('+'|'.join(re.escape(p) for p in paths)+r')(?:\?[^"\r\n]*)?"')
    def version(match):
        path = safe_path(website,match[2])
        if not path.is_file(): return match[0]
        return f'{match[1]}="{match[2]}?v={sha256(path)[:12]}"'
    updated = pattern.sub(version,original)
    if updated == original: return False
    atomic_write(html,updated.encode(),mode=0o644)
    return True


def csv_bytes(columns, rows):
    handle = io.StringIO(newline='')
    writer = csv.writer(handle, lineterminator='\r\n')
    writer.writerow(columns); writer.writerows(rows)
    return handle.getvalue().encode('utf-8-sig')


def safe_path(root, relative):
    path = Path(relative)
    if path.is_absolute() or any(p in {'..', '.'} for p in path.parts):
        raise ValueError('unsafe website asset path')
    result = root / path
    if any(p.is_symlink() for p in [result, *result.parents] if p != root.parent):
        raise ValueError('symlinked website release path')
    return result


def asset(website, path, label):
    return {'path': path.relative_to(website).as_posix(), 'sha256': sha256(path),
            'bytes': path.stat().st_size, 'label': label}


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def read_release(folder, code, reference, annotation, expected=None):
    if folder.is_symlink() or not folder.is_dir() or not VERSION.fullmatch(folder.name):
        raise ValueError('invalid release directory')
    if {p.name for p in folder.iterdir()} != OUTPUTS:
        raise ValueError('incomplete scientific release')
    hashes = {}
    for name in sorted(OUTPUTS):
        path = safe_path(folder, name)
        if not path.is_file(): raise ValueError('missing release artifact')
        hashes[name] = sha256(path)
    if expected is not None and hashes != expected:
        raise ValueError('scientific release checksum mismatch')
    prov = json.loads((folder/'provenance.json').read_text())
    qc = json.loads((folder/'qc.json').read_text())
    generation = int(VERSION.fullmatch(folder.name)[1])
    ref = prov['reference']
    if (prov['release_id'] != folder.name or prov['generation'] != generation
            or qc['generation'] != generation or qc['cohort_hash'] != prov['cohort_hash']
            or ref['species'] != code or ref['reference_id'] != reference
            or ref['annotation_id'] != annotation
            or qc.get('plantvelo_validation', {}).get('status') != 'passed'):
        raise ValueError('scientific release identity/QC mismatch')
    prior = intervals((folder/'high_confidence_IR.tsv').read_bytes())
    with gzip.open(folder/'intron_evidence.tsv.gz', 'rt', newline='') as f:
        reader = csv.DictReader(f, delimiter='\t')
        columns = list(reader.fieldnames or []) + ['species']
        events = [dict(row, species=SPECIES.get(code, (code,))[0]) for row in reader
                  if row['IR_class'] == 'high_confidence_IR']
    keys = {tuple(r[k] for k in ['gene_id','chromosome','start','end','strand']) for r in events}
    if (keys != prior or len(events) != len(prior) or len(events) != qc['high_confidence_rows']
            or qc['plantvelo_validation']['matched_introns'] != len(events)
            or len({r['intron_uid'] for r in events}) != len(events)):
        raise ValueError('high-confidence browser/export row mismatch')
    samples = [dict(s['metadata'], Accession=s['accession']) for s in prov['samples']]
    if len({s['Accession'] for s in samples}) != len(samples):
        raise ValueError('duplicate release sample')
    return hashes, prov, qc, columns, events, samples


def change_summary(website, previous, prov, events):
    if previous is None: return {'kind': 'initial'}
    _, before = read_csv(safe_path(website, previous['files']['events']['path']))
    old = {r['intron_uid']: r for r in before}; new = {r['intron_uid']: r for r in events}
    old_prov = json.loads(safe_path(website, previous['files']['provenance']['path']).read_text())
    a = {s['accession']: s['version'] for s in old_prov['samples']}
    b = {s['accession']: s['version'] for s in prov['samples']}
    metrics = ['N_retained','N_spliced','N_specific','N_informative','IR_ratio','IR_specificity']
    return {'kind': 'update', 'previous': previous['key'],
            'samples_added': len(b.keys()-a.keys()), 'samples_removed': len(a.keys()-b.keys()),
            'samples_revised': sum(a[k] != b[k] for k in a.keys() & b.keys()),
            'events_added': len(new.keys()-old.keys()), 'events_removed': len(old.keys()-new.keys()),
            'evidence_changed': sum(any(str(new[k][m]) != str(old[k][m]) for m in metrics)
                                    for k in new.keys() & old.keys()),
            'rules_changed': prov['recipe']['rules'] != old_prov['recipe']['rules'],
            'software_changed': prov['recipe'].get('implementation') != old_prov['recipe'].get('implementation')}


def export_record(website, folder, code, reference, annotation, values, previous):
    hashes, prov, qc, columns, events, samples = values
    key = '/'.join([code, reference, annotation, folder.name])
    destination = safe_path(website, 'data/releases/'+key)
    if destination.exists():
        record = json.loads((destination/'record.json').read_text())
        identity = {'key': key, 'species': code, 'release_id': folder.name,
                    'reference_id': reference, 'annotation_id': annotation,
                    'generation': prov['generation'], 'created_at': prov['created_at'],
                    'events': len(events), 'samples': len(samples), 'intron_rows': qc['intron_rows'],
                    'cohort_hash': prov['cohort_hash'], 'source_hashes': hashes,
                    'reference_sha256': {k: v['sha256'] for k, v in prov['reference']['files'].items()},
                    'rules': prov['recipe']['rules'], 'software': prov.get('plantvelo', {}),
                    'changes': change_summary(website, previous, prov, events)}
        if any(record.get(k) != v for k, v in identity.items()):
            raise ValueError('immutable recovered website version changed')
        for file in record['files'].values():
            if sha256(safe_path(website,file['path'])) != file['sha256']:
                raise ValueError('incomplete recovered website version')
        return record
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.export-', dir=destination.parent) as tmp:
        stage = Path(tmp)
        for name in OUTPUTS: shutil.copyfile(folder/name, stage/name)
        (stage/'events.csv').write_bytes(csv_bytes(columns, [[r[k] for k in columns] for r in events]))
        (stage/'samples.csv').write_bytes(csv_bytes(SAMPLE_COLUMNS, [[s.get(k,'') for k in SAMPLE_COLUMNS] for s in samples]))
        payload = {'events': {'columns': columns, 'rows': [[r[k] for k in columns] for r in events]},
                   'samples': {'columns': SAMPLE_COLUMNS, 'rows': [[s.get(k,'') for k in SAMPLE_COLUMNS] for s in samples]}}
        script = 'window.PLANTIR_DATASETS = window.PLANTIR_DATASETS || {};\nwindow.PLANTIR_DATASETS['+json.dumps(key)+'] = '+json.dumps(payload, ensure_ascii=True, separators=(',',':'))+';\n'
        (stage/'browse.js').write_text(script)
        labels = {'prior': ('high_confidence_IR.tsv','PlantVelo prior · TSV'),
              'events': ('events.csv','High-confidence events · CSV'),
              'samples': ('samples.csv','Included samples · CSV'),
              'evidence': ('intron_evidence.tsv.gz','Complete intron evidence · TSV.gz'),
              'provenance': ('provenance.json','Source provenance · JSON'),
              'qc': ('qc.json','Quality report · JSON'), 'browse': ('browse.js','Browser data')}
        record = {'key': key, 'species': code, 'release_id': folder.name,
            'generation': prov['generation'], 'created_at': prov['created_at'],
            'reference_id': reference, 'annotation_id': annotation, 'events': len(events),
            'samples': len(samples), 'intron_rows': qc['intron_rows'], 'cohort_hash': prov['cohort_hash'],
            'reference_sha256': {k:v['sha256'] for k,v in prov['reference']['files'].items()},
            'rules': prov['recipe']['rules'], 'software': prov.get('plantvelo', {}),
            'source_hashes': hashes, 'changes': change_summary(website, previous, prov, events),
            'files': {key:dict(asset(website, stage/name, label),path=(destination/name).relative_to(website).as_posix()) for key,(name,label) in labels.items()}}
        (stage/'record.json').write_bytes(json_bytes(record))
        # A complete record makes interrupted delivery resumable before index commit.
        stage.chmod(0o755)
        os.rename(stage, destination)
        Path(tmp).mkdir()
    return record


def snapshot_files(website, base, signature, latest, records, old):
    destination = base/'snapshots'/signature
    receipt = destination/'snapshot-files.json'
    if destination.exists():
        if receipt.exists(): files = json.loads(receipt.read_text())
        elif old.get('snapshot',{}).get('id') == signature:
            files = old['snapshot']['files']
        else: raise ValueError('unknown incomplete website snapshot')
        for file in files.values():
            if sha256(safe_path(website,file['path'])) != file['sha256']:
                raise ValueError('immutable website snapshot changed')
        return files
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.snapshot-',dir=destination.parent) as tmp:
        stage = Path(tmp)
        manifest = {'schema_version':1,'snapshot_id':signature,'releases':{k:records[v] for k,v in latest.items()}}
        (stage/'manifest.json').write_bytes(json_bytes(manifest))
        with zipfile.ZipFile(stage/'plantir-resources.zip','w',zipfile.ZIP_DEFLATED) as archive:
            archive.write(stage/'manifest.json','manifest.json')
            for code,key in latest.items():
                for kind,file in records[key]['files'].items():
                    if kind != 'browse': archive.write(safe_path(website,file['path']),code+'/'+Path(file['path']).name)
        all_events, all_samples = [], []; event_columns = None
        for key in latest.values():
            cols, rows = read_csv(safe_path(website,records[key]['files']['events']['path']))
            if event_columns is None: event_columns = cols
            if cols != event_columns: raise ValueError('incompatible event schemas across species')
            all_events.extend([[r[k] for k in cols] for r in rows])
            _, rows = read_csv(safe_path(website,records[key]['files']['samples']['path']))
            all_samples.extend([[r[k] for k in SAMPLE_COLUMNS] for r in rows])
        payload = {'events':{'columns':event_columns,'rows':all_events},'samples':{'columns':SAMPLE_COLUMNS,'rows':all_samples}}
        (stage/'browse.js').write_text('window.PLANTIR_DATASETS = window.PLANTIR_DATASETS || {};\nwindow.PLANTIR_DATASETS['+json.dumps('snapshot/'+signature)+'] = '+json.dumps(payload,ensure_ascii=True,separators=(',',':'))+';\n')
        labels = {'bundle':('plantir-resources.zip','Complete resources · ZIP'),
                  'manifest':('manifest.json','Snapshot manifest · JSON'),'browse':('browse.js','Current browser data')}
        files = {k:dict(asset(website,stage/name,label),path=(destination/name).relative_to(website).as_posix()) for k,(name,label) in labels.items()}
        (stage/'snapshot-files.json').write_bytes(json_bytes(files))
        stage.chmod(0o755)
        os.rename(stage,destination);Path(tmp).mkdir()
    return files


def verified_file(path, expected, cache):
    """Reuse a digest only while every filesystem identity field is unchanged."""
    info = path.stat()
    identity = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    entry = cache.get(str(path))
    if entry == {'sha256': expected, 'identity': identity}:
        return
    digest = sha256(path)
    after = path.stat()
    if identity != [after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns]:
        raise ValueError('file changed during website verification: '+str(path))
    if digest != expected:
        raise ValueError('website or scientific artifact was modified: '+str(path))
    cache[str(path)] = {'sha256': digest, 'identity': identity}


def release_pointer(path):
    if not path.is_file():
        raise ContractError('MISSING_RELEASE', 'missing current release: '+str(path))
    pointer = json.loads(path.read_text())
    match = VERSION.fullmatch(pointer.get('release_id', ''))
    if (not match or type(pointer.get('generation')) is not int
            or pointer['generation'] != int(match[1]) or pointer['generation'] < 1
            or not isinstance(pointer.get('files'), dict) or set(pointer['files']) != OUTPUTS
            or any(not isinstance(h, str) or not re.fullmatch('[a-f0-9]{64}', h) for h in pointer['files'].values())
            or not isinstance(pointer.get('cohort_hash'), str)
            or not re.fullmatch('[a-f0-9]{64}', pointer['cohort_hash'])):
        raise ValueError('invalid current release pointer')
    return pointer


def check_pointer(pointer, selected):
    if pointer['generation'] > selected['generation']:
        raise ContractError('DELIVERY_SUPERSEDED', 'current release advanced: '+selected['release_id'])
    if any(pointer[k] != selected[k] for k in ('release_id', 'generation', 'files', 'cohort_hash')):
        raise ContractError('DELIVERY_PLAN_MISMATCH', 'current release differs from frozen website source')


def validate_record(website, release_dir, record, cache, audit=False):
    code, ref, ann, release_id = record['key'].split('/')
    folder = safe_path(release_dir, record['key'])
    if (record['species'], record['reference_id'], record['annotation_id'], record['release_id']) != (code, ref, ann, release_id):
        raise ValueError('website record identity mismatch')
    if set(record['source_hashes']) != OUTPUTS or {p.name for p in folder.iterdir()} != OUTPUTS:
        raise ValueError('incomplete scientific release')
    for name, digest in record['source_hashes'].items():
        verified_file(safe_path(folder, name), digest, cache)
    for file in record['files'].values():
        verified_file(safe_path(website, file['path']), file['sha256'], cache)
    record_path = safe_path(website, 'data/releases/'+record['key']+'/record.json')
    if json.loads(record_path.read_text()) != record:
        raise ValueError('immutable website record was modified')
    if audit:
        values = read_release(folder, code, ref, ann, record['source_hashes'])
        if (values[1]['cohort_hash'] != record['cohort_hash']
                or values[1]['generation'] != record['generation']
                or {k: v['sha256'] for k, v in values[1]['reference']['files'].items()} != record['reference_sha256']):
            raise ValueError('website historical source identity mismatch')


def validate_snapshots(website, base, old, cache, recover=False):
    missing_receipts = []
    snapshots = base/'snapshots'
    if snapshots.exists():
        for folder in snapshots.iterdir():
            if folder.name.startswith('.snapshot-'):
                continue  # Interrupted staging and unknown files are never removed.
            receipt = safe_path(website, (folder/'snapshot-files.json').relative_to(website))
            if receipt.is_file():
                files = json.loads(receipt.read_text())
            elif old.get('snapshot', {}).get('id') == folder.name:
                # Older published snapshots stored their receipt only in the
                # committed browser index. Never use this for unknown folders.
                files = old['snapshot']['files']
                missing_receipts.append((receipt, files))
            else:
                raise ValueError('unknown incomplete website snapshot')
            if old.get('snapshot', {}).get('id') == folder.name and files != old['snapshot']['files']:
                raise ValueError('website snapshot receipt was modified')
            for file in files.values():
                verified_file(safe_path(website, file['path']), file['sha256'], cache)
    for file in old.get('snapshot', {}).get('files', {}).values():
        verified_file(safe_path(website, file['path']), file['sha256'], cache)
    if recover:
        for receipt, files in missing_receipts:
            # Preserve the authenticated legacy receipt before this snapshot
            # becomes historical. The audit path never creates it.
            atomic_write(receipt, json_bytes(files), mode=0o644)


def validate_pending_index(candidate,old,website,release_dir,vector,cache):
    required={'schema_version','updated_at','latest','records','species','methods','snapshot'}
    if not isinstance(candidate,dict)or not required<=set(candidate)or candidate['schema_version']!=1:
        raise ValueError('website index pair contains an invalid JSON index')
    if not isinstance(candidate['records'],dict)or not isinstance(candidate['latest'],dict)or set(candidate['latest'])!=set(vector):
        raise ValueError('website index pair has invalid release scope')
    if any(candidate['records'].get(key)!=record for key,record in old.get('records',{}).items()):
        raise ValueError('website JSON index modified immutable history')
    for record in candidate['records'].values():validate_record(website,release_dir,record,cache)
    for code,key in candidate['latest'].items():
        record=candidate['records'].get(key)
        if not isinstance(record,dict)or record['species']!=code:
            raise ValueError('website JSON index has invalid latest identity')
        descriptor=vector[code]
        if (record['reference_id'],record['annotation_id'])!=(descriptor['reference_id'],descriptor['annotation_id']):
            raise ValueError('website pending index reference mismatch')
        if record['generation']>descriptor['generation']:
            raise ContractError('DELIVERY_SUPERSEDED','pending website index is newer than the plan')
        previous=old.get('records',{}).get(old.get('latest',{}).get(code))
        if previous and record['generation']<previous['generation']:
            raise ValueError('website pending JSON index would revert the browser')
    signature=hashlib.sha256(json_bytes({code:candidate['records'][key]['source_hashes']for code,key in candidate['latest'].items()})).hexdigest()[:16]
    if candidate['snapshot'].get('id')!=signature:raise ValueError('website pending JSON snapshot identity mismatch')
    for file in candidate['snapshot']['files'].values():verified_file(safe_path(website,file['path']),file['sha256'],cache)
    manifest=json.loads(safe_path(website,candidate['snapshot']['files']['manifest']['path']).read_text())
    if manifest.get('releases')!={code:candidate['records'][key]for code,key in candidate['latest'].items()}:
        raise ValueError('website JSON index and snapshot manifest disagree')


def sync_website(release_dir, website_dir, config, *, releases=None, verification_cache=None, audit=False):
    release_dir = Path(release_dir).resolve(); website = Path(website_dir).absolute()
    if website.is_symlink(): raise ValueError('symlinked website directory')
    config_identity = json_bytes(config)
    defaults = json.loads(json_bytes(configuration(config)))
    if releases is not None and (not isinstance(releases, dict) or set(releases) != set(defaults)):
        raise ContractError('DELIVERY_PLAN_MISMATCH', 'website requires the complete default species vector')
    vector = json.loads(json_bytes(releases)) if releases is not None else {}
    # Freeze all current pointers before creating any target directories or assets.
    pointers = {}
    for code, selected in defaults.items():
        ref, ann = selected['reference_id'], selected['annotation_id']
        pointer_path = safe_path(release_dir, '/'.join([code, ref, ann, 'current.json']))
        pointer = release_pointer(pointer_path)
        if releases is None:
            vector[code] = dict(pointer, species=code, **selected)
        descriptor = vector[code]
        if (not isinstance(descriptor, dict)
                or any(descriptor.get(k) != v for k, v in dict(species=code, **selected).items())
                or not {'release_id', 'generation', 'cohort_hash', 'files'} <= set(descriptor)
                or not isinstance(descriptor.get('release_id'), str)
                or not VERSION.fullmatch(descriptor['release_id'])
                or type(descriptor.get('generation')) is not int or descriptor['generation'] < 1
                or descriptor['generation'] != int(VERSION.fullmatch(descriptor['release_id'])[1])
                or not isinstance(descriptor.get('cohort_hash'), str)
                or not re.fullmatch('[a-f0-9]{64}', descriptor['cohort_hash'])
                or not isinstance(descriptor.get('files'), dict) or set(descriptor['files']) != OUTPUTS
                or any(not isinstance(h, str) or not re.fullmatch('[a-f0-9]{64}', h) for h in descriptor['files'].values())
                or (releases is not None and (not isinstance(descriptor.get('reference_sha256'), dict)
                    or not descriptor['reference_sha256']
                    or any(not isinstance(h, str) or not re.fullmatch('[a-f0-9]{64}', h)
                           for h in descriptor['reference_sha256'].values())))):
            raise ContractError('DELIVERY_PLAN_MISMATCH', 'invalid frozen website descriptor: '+code)
        check_pointer(pointer, descriptor)
        pointers[pointer_path] = descriptor
    base = safe_path(website, 'data/releases')
    if audit and not base.is_dir(): raise ValueError('website has no committed release index to audit')
    # Audit uses an existing directory lock, which does not create lock files.
    if not audit: base.mkdir(parents=True, exist_ok=True)
    cache = {} if audit or verification_cache is None else verification_cache
    with directory_lock(base, shared=audit):
        index_path = base/'index.json'; browser_index = base/'index.js'
        browser_bytes = browser_index.read_bytes() if browser_index.exists() else None
        if browser_bytes is None:
            old = {}
        else:
            script = browser_bytes.decode().strip()
            prefix = 'window.PLANTIR_RELEASES = '
            if not script.startswith(prefix) or not script.endswith(';'):
                raise ValueError('invalid committed website browser index')
            old = json.loads(script[len(prefix):-1])
        if audit and not old: raise ValueError('website has no committed release index to audit')
        expected_json=[index_path.read_bytes()if index_path.exists()else None]
        json_index=json.loads(expected_json[0])if expected_json[0]is not None else None
        if json_index!=old and not (json_index is None and not old):
            if audit:raise ValueError('website index pair is incomplete or modified')
            if json_index is None:raise ValueError('website JSON index is missing')
            validate_pending_index(json_index,old,website,release_dir,vector,cache)
        records = dict(old.get('records', {})); latest = {}; changed = False

        def check_sources():
            if json_bytes(config) != config_identity:
                raise ContractError('DELIVERY_PLAN_MISMATCH', 'website reference configuration changed')
            for path, descriptor in pointers.items(): check_pointer(release_pointer(path), descriptor)

        def check_assets():
            if (index_path.read_bytes()if index_path.exists()else None)!=expected_json[0]:
                raise ValueError('website JSON index was modified during delivery')
            for record in records.values(): validate_record(website, release_dir, record, cache)
            validate_snapshots(website, base, old, cache)
            current_browser = browser_index.read_bytes() if browser_index.exists() else None
            if current_browser != browser_bytes:
                raise ContractError('DELIVERY_SUPERSEDED', 'website index changed during delivery')

        # Reject an older target vector before exporting any version.
        for code, descriptor in vector.items():
            previous = records.get(old.get('latest', {}).get(code))
            if previous is not None:
                if (previous['reference_id'], previous['annotation_id']) != (descriptor['reference_id'], descriptor['annotation_id']):
                    raise ContractError('DELIVERY_PLAN_MISMATCH', 'website default reference differs from frozen vector')
                if previous['generation'] > descriptor['generation']:
                    raise ContractError('DELIVERY_SUPERSEDED', 'website already has a newer release: '+code)
                if previous['generation'] == descriptor['generation'] and previous['release_id'] != descriptor['release_id']:
                    raise ContractError('DELIVERY_PLAN_MISMATCH', 'website release identity differs from frozen vector')
        check_sources()
        for record in records.values(): validate_record(website, release_dir, record, cache, audit)
        validate_snapshots(website, base, old, cache, recover=not audit)
        for code, descriptor in vector.items():
            ref, ann = descriptor['reference_id'], descriptor['annotation_id']
            namespace = safe_path(release_dir, '/'.join([code, ref, ann]))
            folders = sorted(p for p in namespace.iterdir() if VERSION.fullmatch(p.name)
                             and (int(VERSION.fullmatch(p.name)[1]) < descriptor['generation'] or p.name == descriptor['release_id']))
            if not any(p.name == descriptor['release_id'] for p in folders): raise ValueError('missing current release directory')
            for folder in folders:
                key = '/'.join([code, ref, ann, folder.name])
                current = folder.name == descriptor['release_id']
                if key not in records:
                    values = read_release(folder, code, ref, ann, descriptor['files'] if current else None)
                    if audit:
                        raise ValueError('scientific history has not been delivered: '+key)
                    if current and (values[1]['cohort_hash'] != descriptor['cohort_hash']
                            or ('reference_sha256' in descriptor and descriptor['reference_sha256'] !=
                                {k: v['sha256'] for k, v in values[1]['reference']['files'].items()})):
                        raise ContractError('DELIVERY_PLAN_MISMATCH', 'frozen website source identity differs')
                    previous = max((r for r in records.values() if r['species'] == code
                                    and r['reference_id'] == ref and r['annotation_id'] == ann
                                    and r['generation'] < values[1]['generation']), key=lambda r: r['generation'], default=None)
                    records[key] = export_record(website, folder, code, ref, ann, values, previous)
                    changed = True
                    validate_record(website, release_dir, records[key], cache)
                if current:
                    record = records[key]
                    if (record['source_hashes'] != descriptor['files'] or record['cohort_hash'] != descriptor['cohort_hash']
                            or ('reference_sha256' in descriptor and record['reference_sha256'] != descriptor['reference_sha256'])):
                        raise ContractError('DELIVERY_PLAN_MISMATCH', 'frozen website source differs from registered version')
            latest[code] = '/'.join([code, ref, ann, descriptor['release_id']])
        check_sources()
        if audit:
            return {'status': 'audited', 'snapshot_id': old['snapshot']['id'], 'records': len(records)}
        species = species_metadata(website, latest, records)
        methods = method_metadata(website, records)
        if not changed and latest == old.get('latest') and 'manuscript' not in old and species == old.get('species') and methods == old.get('methods'):
            check_sources()
            check_assets()
            return {'status':'up_to_date','snapshot_id':old['snapshot']['id'],
                    'assets_refreshed':refresh_asset_versions(website)}
        signature = hashlib.sha256(json_bytes({k:records[v]['source_hashes'] for k,v in latest.items()})).hexdigest()[:16]
        snapshot = snapshot_files(website,base,signature,latest,records,old)
        index = {'schema_version':1,'updated_at':datetime.now(timezone.utc).isoformat(),
                 'latest':latest,'records':records,'species':species,'methods':methods,
                 'snapshot':{'id':signature,'key':'snapshot/'+signature,
                    'events':sum(records[k]['events'] for k in latest.values()),
                    'samples':sum(records[k]['samples'] for k in latest.values()),
                    'files':snapshot}}
        check_sources()
        check_assets()
        atomic_write(index_path,json_bytes(index),mode=0o644)
        expected_json[0]=json_bytes(index)
        check_sources()
        check_assets()
        atomic_write(browser_index,b'window.PLANTIR_RELEASES = '+json_bytes(index).rstrip()+b';\n',mode=0o644)
        return {'status':'synchronized','snapshot_id':signature,'species':len(latest),
                'assets_refreshed':refresh_asset_versions(website)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-dir',type=Path,default=PROJECT/'releases')
    parser.add_argument('--website-dir',type=Path,default=PROJECT/'website')
    parser.add_argument('--config',type=Path,default=PROJECT/'configs/prior_resources.json')
    parser.add_argument('--audit', action='store_true', help='verify all registered releases and assets without writing')
    args = parser.parse_args()
    try:
        result = sync_website(args.release_dir,args.website_dir,json.loads(args.config.read_text()),audit=args.audit)
        print(json.dumps(result)); return 0
    except (ValueError,OSError,KeyError,TypeError) as error:
        print(json.dumps({'status':'failed','error':str(error)})); return 1


if __name__ == '__main__': raise SystemExit(main())
