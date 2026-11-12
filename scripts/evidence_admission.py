"""Formal admission of audited historical counts, distinct from native extraction."""
import json

from discovery_io import digest
from download_reads import child_path, sha256
from evidence_identity import CONTRACT, canonical_software, software_policy, supported_contract


def audit_identity(report):
    return digest(json.dumps({k:v for k,v in report.items() if k != 'audit_id'}, sort_keys=True,
                             ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode())


def imported_signature(report):
    if report.get('schema_version') != 1 or not supported_contract(report.get('count_contract')):
        raise ValueError('unsupported imported evidence count contract')
    if report.get('audit_id') != audit_identity(report):
        raise ValueError('imported evidence audit identity mismatch')
    policy = report['native_policy']
    if policy.get('aggregation') != 'audited_heterogeneous_counts':
        raise ValueError('unrecognized imported/native admission policy')
    return {'kind':'validated_import','audit_id':report['audit_id'],'count_contract':report['count_contract'],
            'historical_parameters_verified':False,'native_policy':policy}


def load_admission(folder, registration, reference):
    receipt = registration.get('admission')
    if receipt is None: return None
    path = child_path(folder, receipt['path'])
    if sha256(path) != receipt['sha256']: raise ValueError('imported evidence audit hash mismatch')
    report = json.loads(path.read_text()); imported_signature(report)
    expected = {'species':reference['species'],'reference_id':reference['reference_id'],
                'annotation_id':reference['annotation_id'],
                'sha256':{k:v['sha256'] for k,v in reference['files'].items()}}
    if report['reference'] != expected: raise ValueError('imported evidence audit reference mismatch')
    return report


def validate_cohort(signatures, allow_legacy_mixing=False):
    native = [s for s in signatures if s['kind']=='native']
    imports = [s for s in signatures if s['kind']=='validated_import']
    legacy = any(s['kind']=='legacy' for s in signatures)
    if any(s['kind'] not in {'native','legacy','validated_import'} for s in signatures):
        raise ValueError('unknown evidence admission type')
    # DISTINCT signatures normally makes this length one; standalone callers get the same check.
    if len({json.dumps(dict(n,software=canonical_software(n['software'])),sort_keys=True) for n in native}) > 1:
        raise ValueError('incompatible native extraction identities')
    if legacy and (imports or (native and not allow_legacy_mixing)):
        raise ValueError('incompatible evidence extraction identities; legacy/native mixing requires explicit audited policy')
    for entry in imports:
        if not supported_contract(entry.get('count_contract')) or entry.get('historical_parameters_verified') is not False:
            raise ValueError('invalid formal imported evidence signature')
        for n in native:
            if (entry['native_policy']['parameters'] != n['parameters']
                    or canonical_software(entry['native_policy']['software']) != canonical_software(n['software'])):
                raise ValueError('native extraction identity differs from audited import admission policy')
