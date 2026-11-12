"""Shared scientific evidence rules, independent of workflow entry points.

Rows retain their original string values and any additional historical columns.
Consumers may persist each checked row; their caller owns transaction rollback.
"""
import math
import re

from evidence_identity import recognized_extractor

COUNT_NAMES = ['retained', 'spliced', 'specific', 'informative']
CORE_FIELDS = ['sample_id', 'gene_id', 'intron_id', 'intron_uid', 'chromosome', 'start', 'end',
               'strand', 'supporting_transcripts', 'anchor_count', 'exon_overlap_warning'] + [
               'N_' + n for n in COUNT_NAMES] + ['protocol_support', 'species']
MAX_INT = 2**63 - 1
DEFAULT_PARAMETERS = dict(min_mapq=20,min_anchor=8,min_intron_coverage=.90,
                          splice_tolerance=5,strand_mode='unstranded')
SPECIES = {'ath': 'Arabidopsis thaliana', 'gmx': 'Glycine max', 'osa': 'Oryza sativa',
           'sly': 'Solanum lycopersicum', 'zma': 'Zea mays'}


def normalize_species(value):
    if isinstance(value, str):
        normalized = value.strip().replace('_', ' ').casefold()
        for code, name in SPECIES.items():
            if normalized in (code, name.casefold()):
                return code
    raise ValueError('unsupported species: ' + str(value))


def validate_parameters(params):
    if not isinstance(params, dict) or set(params) != {
            'min_mapq', 'min_anchor', 'min_intron_coverage', 'splice_tolerance', 'strand_mode'}:
        raise ValueError('invalid native evidence parameter set')
    for key, minimum in [('min_mapq', 0), ('min_anchor', 1), ('splice_tolerance', 0)]:
        if type(params[key]) is not int or params[key] < minimum:
            raise ValueError('invalid native evidence integer parameter: ' + key)
    coverage = params['min_intron_coverage']
    if type(coverage) not in (int, float) or not 0 < coverage <= 1 or not math.isfinite(coverage):
        raise ValueError('invalid native evidence coverage parameter')
    if params['strand_mode'] not in ('unstranded', 'tag', 'flag', 'tag_then_flag'):
        raise ValueError('invalid native strand mode')
    return dict(params)


def native_signature(report, sample, reference):
    """Validate stored native identity without rewriting audited software names."""
    try:
        provenance = report['agent']['provenance']
        parameters = validate_parameters(provenance['parameters'])
        software = provenance['software']
        fields = ('name', 'version', 'package_sha256', 'pysam_version', 'pysam_sha256')
        if any(not isinstance(software.get(k), str) or not software[k] for k in fields):
            raise ValueError('native software identity is missing')
        if any(not re.fullmatch('[a-f0-9]{64}', software[k]) for k in ('package_sha256', 'pysam_sha256')):
            raise ValueError('native software source identity is missing')
        if (not recognized_extractor(software)
                or any(report['software'].get(k) != software[k] for k in ('name', 'version', 'pysam_version'))):
            raise ValueError('inconsistent native software identity')
        for key in ('genome', 'annotation'):
            if provenance['reference'][key]['sha256'] != reference['files'][key]['sha256']:
                raise ValueError('native evidence reference mismatch')
        if any(report['parameters'].get(k) != v for k, v in parameters.items()):
            raise ValueError('native evidence effective parameters mismatch')
        if report['parameters']['sample_id'] != sample['Accession']:
            raise ValueError('native evidence report sample mismatch')
        if (report['parameters']['protocol'] != sample['Protocol']
                or normalize_species(report['parameters']['species']) != normalize_species(reference['species'])
                or normalize_species(sample['Species']) != normalize_species(reference['species'])):
            raise ValueError('native evidence report protocol/species mismatch')
        return {'kind': 'native', 'parameters': parameters, 'software': {k: software[k] for k in fields}}
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('malformed native evidence report') from exc


def checked_counts(values):
    counts = []
    for value in values:
        if not re.fullmatch(r'[0-9]+', str(value)) or int(value) > MAX_INT:
            raise ValueError('invalid nonnegative integer evidence count')
        counts.append(int(value))
    if counts[3] != counts[0] + counts[1] or counts[2] > counts[0]:
        raise ValueError('inconsistent evidence counts')
    return counts


def validate_rows(rows, sample, intron_lookup, expected_rows, kind, report, consume=None):
    """Stream reference-checked rows, enforcing native completeness and totals.

    Pass a DictReader (rather than converting it to a list) to check duplicate
    headers. Historical evidence may cover a subset of the reference catalogue.
    """
    if kind not in ('native', 'validated_import', 'legacy'):
        raise ValueError('unknown evidence admission type')
    fields = getattr(rows, 'fieldnames', None)
    if fields is not None and (len(fields) != len(set(fields)) or set(CORE_FIELDS) - set(fields)):
        raise ValueError('invalid evidence columns: ' + sample['Accession'])
    protocol = sample['Protocol']
    accepted = {protocol}
    if protocol.startswith('ONT'):
        accepted.add('ONT')
    if protocol == 'FLEP-seq2':
        accepted.add('FLEP-seq')
    species = normalize_species(sample['Species'])
    seen, totals = set(), dict.fromkeys(COUNT_NAMES, 0)
    for row in rows:
        if (not isinstance(row, dict) or None in row or set(CORE_FIELDS) - set(row)
                or any(v is None for v in row.values()) or row['sample_id'] != sample['Accession']):
            raise ValueError('malformed evidence row/sample: ' + sample['Accession'])
        uid = row['intron_uid']
        if uid in seen:
            raise ValueError('duplicate evidence intron: ' + uid)
        attributes = intron_lookup(uid)
        if attributes is None:
            raise ValueError('evidence intron absent from reference: ' + uid)
        if any(row.get(k) != value for k, value in attributes.items()):
            raise ValueError('evidence reference/transcript fields mismatch: ' + uid)
        if normalize_species(row['species']) != species:
            raise ValueError('evidence species mismatch')
        if row['protocol_support'] not in accepted:
            raise ValueError('evidence protocol mismatch')
        counts = checked_counts([row['N_' + k] for k in COUNT_NAMES])
        seen.add(uid)
        for key, count in zip(COUNT_NAMES, counts):
            totals[key] += count
        if consume is not None:
            consume(row)
    if not seen:
        raise ValueError('empty evidence: ' + sample['Accession'])
    if kind == 'native':
        if len(seen) != expected_rows:
            raise ValueError('incomplete native intron catalogue')
        if not totals['informative']:
            raise ValueError('no informative read-intron evidence; result was not registered')
        if not isinstance(report, dict) or report.get('counts') != dict(totals, unit='read_intron_pairs'):
            raise ValueError('native report totals mismatch')
    return {'rows': len(seen), 'counts': totals}
