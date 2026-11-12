"""Exact, audited software renames; never infer compatibility from a name."""
import json
from pathlib import Path
import re

MIGRATIONS = Path(__file__).resolve().parents[1] / 'configs/evidence_identity_migrations.json'
CONTRACT = 'plantvelo.irprior.read_intron_pairs.v1'
SOFTWARE_FIELDS = ('name', 'version', 'package_sha256', 'pysam_version', 'pysam_sha256')


def software_policy(software):
    return {key: software[key] for key in SOFTWARE_FIELDS}


def registry():
    data = json.loads(MIGRATIONS.read_text())
    if (set(data) != {'schema_version', 'contract_aliases', 'software_migrations'}
            or type(data['schema_version']) is not int or data['schema_version'] != 1
            or not isinstance(data['contract_aliases'], dict)
            or not isinstance(data['software_migrations'], list)
            or any(not isinstance(k, str) or v != CONTRACT for k, v in data['contract_aliases'].items())):
        raise ValueError('invalid evidence identity migration registry')
    sources, targets = set(), set()
    for entry in data['software_migrations']:
        if set(entry) != {'source', 'target', 'verification'}:
            raise ValueError('invalid software identity migration')
        for key in ('source', 'target'):
            software = entry[key]
            if (not isinstance(software, dict) or set(software) != set(SOFTWARE_FIELDS)
                    or any(not isinstance(v, str) or not v for v in software.values())
                    or any(not re.fullmatch('[a-f0-9]{64}', software[k])
                           for k in ('package_sha256', 'pysam_sha256'))):
                raise ValueError('invalid software identity migration digest')
        old, new = entry['source'], entry['target']
        if any(old[k] != new[k] for k in ('pysam_version', 'pysam_sha256')):
            raise ValueError('software rename cannot change pysam identity')
        verification = entry['verification']
        if (not isinstance(verification, dict)
                or verification.get('algorithm_equivalence') != 'normalized_ast'
                or not re.fullmatch('[a-f0-9]{64}', verification.get('normalized_ast_sha256', ''))):
            raise ValueError('software rename lacks algorithm equivalence verification')
        source = json.dumps(old, sort_keys=True)
        target = json.dumps(new, sort_keys=True)
        if source == target or source in sources:
            raise ValueError('duplicate or self-referencing software rename')
        sources.add(source); targets.add(target)
    if sources & targets:
        raise ValueError('chained software renames require a new explicit equivalence audit')
    return data


def supported_contract(value):
    return value == CONTRACT or value in registry()['contract_aliases']


def canonical_software(software):
    policy = software_policy(software)
    for entry in registry()['software_migrations']:
        if policy == entry['source']:
            return entry['target']
    return policy


def recognized_extractor(software):
    policy = software_policy(software)
    return (policy['name'] == 'plantvelo'
            or any(policy == entry['source'] for entry in registry()['software_migrations']))


def equivalent_extraction(old, new):
    """Reuse a completed run only across the exact audited adapter migration."""
    for entry in registry()['software_migrations']:
        if (software_policy(old.get('software', {})) != entry['source']
                or software_policy(new.get('software', {})) != entry['target']):
            continue
        proof = entry['verification']
        source_adapters = proof.get('source_adapter_sha256', [])
        if isinstance(source_adapters, dict):
            source_adapters = [source_adapters]
        if (not proof.get('source_adapter_sha256') or not proof.get('target_adapter_sha256')
                or old.get('code_sha256') not in source_adapters
                or new.get('code_sha256') != proof['target_adapter_sha256']):
            return False
        ignored = {'software', 'code_sha256'}
        # Python itself is unchanged. Include the additional interpreter/runtime
        # fields in the comparison rather than silently discarding them.
        old_software = {k:v for k,v in old['software'].items() if k not in SOFTWARE_FIELDS}
        new_software = {k:v for k,v in new['software'].items() if k not in SOFTWARE_FIELDS}
        return (old_software == new_software
                and {k:v for k,v in old.items() if k not in ignored}
                == {k:v for k,v in new.items() if k not in ignored})
    return False
