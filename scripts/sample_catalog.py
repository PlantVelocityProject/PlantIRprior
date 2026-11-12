"""Preview, merge and durably commit a sample catalog without overwriting curation."""
import csv
import io
import json
import os
from pathlib import Path,PurePosixPath
import re
import stat

from artifact_lifecycle import directory_lock
from discovery_io import atomic_json, atomic_write, digest
from discovery_metadata import CORE_FIELDS, RUN, BIOSAMPLE, PROJECT_ID, UNKNOWN, identity,TREATMENT_KEYS
from discovery_routing import origin
from download_reads import SPECIES, remote_paths
from treatment_metadata import summarize_treatment, treatment_issues

PROTOCOLS = {'Iso-seq', 'ONT cDNA-seq', 'ONT PCR-cDNA-seq', 'ONT direct RNA-seq', 'FLEP-seq', 'FLEP-seq2'}


def read_catalog(path):
    path = Path(path)
    if path.is_symlink(): raise ValueError('sample catalog must not be a symbolic link')
    if not path.exists(): return list(CORE_FIELDS), []
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle, delimiter='\t'); fields = reader.fieldnames or []
        if fields[:len(CORE_FIELDS)] != CORE_FIELDS or len(fields) != len(set(fields)):
            raise ValueError('sample catalog must start with the existing 11 columns, without duplicate headers')
        rows, seen = [], set()
        for number, row in enumerate(reader, 2):
            if None in row or any(v is None for v in row.values()):
                raise ValueError('malformed sample catalog row {}'.format(number))
            accession = row['Accession']
            if not RUN.fullmatch(accession) or accession in seen:
                raise ValueError('invalid or duplicate catalog accession: ' + accession)
            seen.add(accession); rows.append(row)
    return fields, rows


def table_bytes(fields, rows):
    for row in rows:
        problems = treatment_issues(row.get('Treatment', ''))
        if problems:
            raise ValueError('invalid Treatment for {}: {}'.format(row.get('Accession', ''), '; '.join(problems)))
    output = io.StringIO(newline=''); writer = csv.DictWriter(output, fieldnames=fields, delimiter='\t', lineterminator='\n')
    writer.writeheader(); writer.writerows(rows)
    return output.getvalue().encode()


def unknown(value):
    # Historical nonempty curation is protected, including literal "NA"/"n/a".
    return value.strip().casefold() in {'', 'unknown'}


def row_issues(row):
    issues = []
    issues.extend(treatment_issues(row.get('Treatment', '')))
    if not RUN.fullmatch(row.get('Accession', '')): issues.append('invalid accession')
    if not BIOSAMPLE.fullmatch(row.get('BioSample', '')): issues.append('missing BioSample')
    if not PROJECT_ID.fullmatch(row.get('BioProject', '')): issues.append('missing BioProject')
    if row.get('Species') not in SPECIES.values(): issues.append('unsupported species')
    protocol = row.get('Protocol')
    if protocol not in PROTOCOLS: issues.append('unknown protocol')
    platform = 'PACBIO_SMRT' if protocol == 'Iso-seq' else 'OXFORD_NANOPORE'
    if row.get('Platform') != platform: issues.append('platform/protocol mismatch')
    accession = row.get('Accession', '')
    expected_database = {'SRR': 'NCBI', 'ERR': 'ENA', 'DRR': 'DDBJ', 'CRR': 'NGDC'}.get(accession[:3])
    if row.get('Database') != expected_database: issues.append('accession/database mismatch')
    if accession.startswith('CRR'):
        try:
            paths=remote_paths(row.get('RemotePaths',''));names=[]
            if not paths:issues.append('CRR RemotePaths missing')
            for value in paths:
                path=PurePosixPath(value)
                if not re.match(r'^/gsa[0-9]*/',value)or '..'in path.parts or '\x00'in value or not path.name:
                    issues.append('invalid CRR RemotePaths')
                names.append(path.name)
            if len(names)!=len(set(names)):issues.append('duplicate CRR remote filenames')
        except (ValueError,TypeError,AttributeError):issues.append('invalid CRR RemotePaths')
    return issues


def alias_index(candidates, known_aliases):
    parents = {}
    def root(value):
        parents.setdefault(value, value)
        if parents[value] != value: parents[value] = root(parents[value])
        return parents[value]
    for group in list(known_aliases) + [c['aliases'] + [c['accession']] for c in candidates]:
        values = [a for a in group if RUN.fullmatch(a)]
        for a in values[1:]: parents[root(a)] = root(values[0])
    groups = {}
    for a in list(parents): groups.setdefault(root(a), set()).add(a)
    return {a: group for group in groups.values() for a in group}


def equivalent_field(name,left,right):
    if left==right:return True
    if name!='Treatment':return False
    # Do not collapse protected historical NA/n/a curation into unknown.
    if left.strip().casefold() in UNKNOWN or right.strip().casefold() in UNKNOWN:return False
    compact_left, left_reason = summarize_treatment(left)
    compact_right, right_reason = summarize_treatment(right)
    if left_reason is None and right_reason is None:
        if sorted(compact_left.split('; ')) == sorted(compact_right.split('; ')):return True
    def entries(value):
        parts=value.split('; ')
        if not all(': 'in p and p.split(': ',1)[0]in TREATMENT_KEYS and p.split(': ',1)[1]for p in parts):return None
        return sorted(parts)
    parsed=entries(left)
    return parsed is not None and parsed==entries(right)


def build_plan(fields, rows, candidates, refresh_existing=False, known_aliases=(),strict_origin=False):
    result = {'additions': [], 'updates': [], 'conflicts': [], 'pending': [], 'unchanged': [], 'aliases': []}
    existing = {r['Accession']: r for r in rows}; combined = {a: dict(r) for a, r in existing.items()}
    index = alias_index(candidates, known_aliases); grouped = {}
    for candidate in candidates:
        aliases = index[candidate['accession']]
        found = aliases & set(existing)
        if len(found) > 1:
            result['conflicts'].append({'accession': candidate['accession'], 'reason': 'multiple_existing_canonical_runs',
                                        'aliases': sorted(found)})
            continue
        canonical = next(iter(found)) if found else min(aliases, key=lambda a: ({'SRR': 0, 'ERR': 1, 'DRR': 2, 'CRR': 3}[a[:3]], a))
        grouped.setdefault(canonical, []).append(candidate)
        if candidate['accession'] != canonical:
            result['aliases'].append({'source_accession': candidate['accession'], 'canonical_accession': canonical})
    selected = []
    for canonical, group in sorted(grouped.items()):
        if strict_origin:
            owner=origin(canonical)
            group=[c for c in group if origin(c['accession'])==owner]
            if not group:
                result['pending'].append({'accession':canonical,
                    'issues':['canonical_run_requires_own_archive_metadata: '+owner]});continue
            expected={'ncbi':'NCBI','ena':'ENA','ddbj':'DDBJ','ngdc':'NGDC'}[owner]
            if any(e.get('source')!=expected for c in group for f in c['fields'].values()for e in f['evidence']):
                result['pending'].append({'accession':canonical,'issues':['field_source_differs_from_original_archive']});continue
        bad = [issue for c in group for issue in c['issues']]
        if bad:
            result['pending'].append({'accession': canonical, 'issues': sorted(set(bad))}); continue
        proposed = {k: 'unknown' for k in CORE_FIELDS}; proposed['Accession'] = canonical
        proposed['Database'] = {'SRR': 'NCBI', 'ERR': 'ENA', 'DRR': 'DDBJ', 'CRR': 'NGDC'}[canonical[:3]]
        conflicts = []
        for c in group:
            for k, v in c['row'].items():
                if k in {'Accession', 'Database'} or unknown(v): continue
                old = proposed.get(k, 'unknown')
                if not unknown(old) and not equivalent_field(k,old,v):
                    conflicts.append({'accession': canonical, 'field': k, 'existing': old, 'proposed': v,
                                      'reason': 'conflicting_mirror_metadata'})
                else: proposed[k] = v
        bad = row_issues(proposed)
        if bad:
            result['pending'].append({'accession': canonical, 'issues': bad}); continue
        if canonical in existing:
            old = existing[canonical]; changes = {}
            for k, v in proposed.items():
                if unknown(v): continue
                if unknown(old.get(k, '')):
                    if refresh_existing: changes[k] = {'before': old.get(k, ''), 'after': v}
                elif not equivalent_field(k,old[k],v):
                    conflicts.append({'accession': canonical, 'field': k, 'existing': old[k], 'proposed': v,
                                      'reason': 'protected_existing_value'})
            if conflicts:
                result['conflicts'].extend(conflicts); continue
            final = dict(old, **{k: v['after'] for k, v in changes.items()})
            invalid = row_issues(final)
            if invalid:
                result['pending'].append({'accession': canonical,
                    'issues': ['historical_row_requires_explicit_refresh: ' + reason for reason in invalid]})
                continue
            if changes:
                result['updates'].append({'accession': canonical, 'changes': changes})
                combined[canonical].update({k: v['after'] for k, v in changes.items()})
            else: result['unchanged'].append(canonical)
        else:
            if conflicts:
                result['conflicts'].extend(conflicts); continue
            new = {k: proposed.get(k, '[]' if k == 'RemotePaths' else '') for k in fields}
            if 'RemotePaths'in proposed:new['RemotePaths']=proposed['RemotePaths']
            combined[canonical] = new; result['additions'].append(new)
        selected.append(canonical)
    ordered = [combined[r['Accession']] for r in rows] + [combined[a] for a in sorted(set(combined) - set(existing))]
    snapshot = [combined[a] for a in selected]
    return result, ordered, snapshot


def update_catalog(samples, candidates, batch, apply=False, refresh_existing=False, known_aliases=(),strict_origin=False):
    samples, batch = Path(samples).absolute(), Path(batch).absolute()
    if samples.is_symlink(): raise ValueError('sample catalog must not be a symbolic link')
    samples.parent.mkdir(parents=True, exist_ok=True); batch.mkdir(parents=True, exist_ok=True)
    policy={'samples': str(samples), 'candidates': candidates, 'refresh_existing': refresh_existing}
    if strict_origin:policy['metadata_policy']='original_archive_only.v1'
    signature = identity(policy)
    with directory_lock(samples.parent):
        result_path, intent_path = batch / 'result.json', batch / 'commit.json'
        if result_path.exists():
            result = json.loads(result_path.read_text())
            if result['request_signature'] != signature: raise ValueError('completed batch inputs changed')
            if digest((batch / 'samples.snapshot.tsv').read_bytes()) != result['snapshot_sha256']:
                raise ValueError('batch sample snapshot hash mismatch')
            return result
        before = samples.read_bytes() if samples.exists() else b''
        if apply and intent_path.exists():
            intent = json.loads(intent_path.read_text())
            if intent['request_signature'] != signature: raise ValueError('pending batch inputs changed')
            if digest(before) == intent['catalog_after_sha256']:
                if digest((batch / 'samples.snapshot.tsv').read_bytes()) != intent['snapshot_sha256']:
                    raise ValueError('pending batch snapshot hash mismatch')
                result = dict(intent, recovered=True)
                atomic_json(result_path, result); return result
        fields, rows = read_catalog(samples)
        plan, merged, snapshot = build_plan(fields, rows, candidates, refresh_existing, known_aliases,strict_origin)
        if ('RemotePaths'not in fields and (plan['additions']or plan['updates'])
                and any('RemotePaths'in row for row in merged)):
            fields=fields+['RemotePaths']
            merged=[dict(row,RemotePaths=row.get('RemotePaths','[]'))for row in merged]
            snapshot=[dict(row,RemotePaths=row.get('RemotePaths','[]'))for row in snapshot]
        after = table_bytes(fields, merged) if plan['additions'] or plan['updates'] else before
        snapshot_data = table_bytes(fields, snapshot)
        plan.update({'schema_version': 1, 'request_signature': signature,
                     'catalog_before_sha256': digest(before), 'catalog_after_sha256': digest(after),
                     'snapshot_sha256': digest(snapshot_data), 'samples': str(samples),
                     'snapshot': str(batch / 'samples.snapshot.tsv'), 'recovered': False,
                     'status': 'preview' if not apply else ('applied' if after != before else 'unchanged')})
        atomic_json(batch / 'preview.json', dict(plan, status='preview'))
        atomic_write(batch / 'samples.snapshot.tsv', snapshot_data)
        if not apply: return plan
        atomic_json(intent_path, plan)
        if after != before:
            mode = stat.S_IMODE(samples.stat().st_mode) if samples.exists() else 0o644
            atomic_write(samples, after, mode=mode)
        atomic_json(result_path, plan)
        return plan
