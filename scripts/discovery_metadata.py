"""Conservative, source-linked normalization of ENA run metadata."""
import hashlib
import json
import re
import xml.etree.ElementTree as ET

from download_reads import SPECIES
from treatment_metadata import summarize_treatment, TREATMENT_NORMALIZATION_VERSION

CORE_FIELDS = ['Accession', 'BioSample', 'BioProject', 'Species', 'Tissue', 'Treatment',
               'Ecotype', 'Genotype', 'Platform', 'Protocol', 'Database']
TAXA = {'ath': 3702, 'gmx': 3847, 'osa': 4530, 'sly': 4081, 'zma': 4577}
RUN = re.compile(r'(?:SRR|ERR|DRR|CRR)[0-9]+\Z')
BIOSAMPLE = re.compile(r'SAM(?:N|EA|D|C)[0-9]+\Z')
PROJECT_ID = re.compile(r'PRJ(?:NA|EB|D[AB]|CA)[0-9]+\Z')
UNKNOWN = {'', 'unknown', 'missing', 'not provided', 'not collected', 'not available', 'na', 'n/a', 'null'}
TREATMENT_KEYS={'treatment','treatment description','experimental treatment','growth condition',
                'growth conditions','age','dev stage','developmental stage','time point',
                'time','timepoint','sampling time','collection time'}


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def clean(value):
    return ' '.join(str(value or '').split())


def identifiers(node):
    values = {node.get('accession', '')}
    for tag in ('PRIMARY_ID', 'SECONDARY_ID'):
        values.update(clean(n.text) for n in node.findall('./IDENTIFIERS/' + tag))
    values.update(clean(n.text) for n in node.findall('./IDENTIFIERS/EXTERNAL_ID')
                  if n.get('namespace', '').casefold() in {'biosample', 'bioproject', 'sra', 'ena', 'ddbj', 'insdc'})
    return values - {''}


def xml_record(text, tag, expected):
    if re.search(r'<!\s*(DOCTYPE|ENTITY)', text, re.I):
        raise ValueError('DTD and entity declarations are not accepted')
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError('invalid ENA XML: ' + str(exc)) from exc
    nodes = [root] if root.tag == tag else root.findall('.//' + tag)
    matches = [n for n in nodes if expected in identifiers(n)]
    if len(matches) != 1:
        raise ValueError('expected exactly one {} record for {}'.format(tag, expected))
    return matches[0]


def split_ids(value):
    return {x for x in re.split(r'[;,\s]+', value or '') if x}


def unlinked_candidate(portal, species, reason,source='ENA',entries=None):
    """Retain incomplete archive relationships for review, without inventing values."""
    accession = portal['run_accession']
    row = {k: 'unknown' for k in CORE_FIELDS}
    row['Accession'] = accession
    row['Database'] = {'SRR': 'NCBI', 'ERR': 'ENA', 'DRR': 'DDBJ','CRR':'NGDC'}[accession[:3]]
    evidence=entries if entries is not None else [{'source':source,'record':accession,
        'path':'normalized_search_record','raw_value':{k:v for k,v in portal.items()if not k.startswith('_')}}]
    return {'accession': accession, 'aliases': [accession], 'row': row,
            'fields': {k: {'normalized_value': v, 'status': 'unresolved', 'reason': reason,
                           'evidence': evidence}
                       for k, v in row.items()},
            'issues': ['linkage: ' + reason], 'source_fingerprint': identity(portal), 'normalization_version': 1}


def classify_protocol(text, platform, configured):
    value = text.casefold().replace('–', '-').replace('‐', '-')
    if re.search(r'\b(?:not|without|rather than)\s+(?:direct[ -]?(?:rna|cdna)|pcr[ -]?cdna|iso[ -]?seq|flep[ -]?seq)', value):
        return 'unknown', 'negated_or_comparative_protocol_description'
    # FLEP methods include several preparation terms; the named method takes precedence.
    flep2 = bool(re.search(r'\bflep[ -]?seq\s*(?:2|v2|version\s*2)\b', value))
    flep = bool(re.search(r'\bflep[ -]?seq\b', value))
    choices = set()
    if flep2 or flep:
        choices.add('FLEP-seq2' if flep2 else 'FLEP-seq')
    else:
        if re.search(r'\biso[ -]?seq\b', value): choices.add('Iso-seq')
        if re.search(r'\bdirect[ -]?rna\b|\bsqk-rna\d+\b', value): choices.add('ONT direct RNA-seq')
        if re.search(r'\bpcr[ -]?cdna\b|\bsqk-pcs\d+\b', value): choices.add('ONT PCR-cDNA-seq')
        if re.search(r'\bdirect[ -]?cdna\b|\bpcr[ -]?free\s+cdna\b|\bsqk-dcs\d+\b', value):
            choices.add('ONT cDNA-seq')
    if len(choices) != 1:
        return 'unknown', 'ambiguous_or_missing_protocol'
    result = choices.pop()
    expected = 'PACBIO_SMRT' if result == 'Iso-seq' else 'OXFORD_NANOPORE'
    if platform != expected:
        return 'unknown', 'protocol_platform_conflict'
    if result not in configured:
        return 'unknown', 'protocol_not_configured'
    if result == 'ONT PCR-cDNA-seq':
        kits = {k.upper() for k in re.findall(r'\bsqk-(pcs\d+)\b', value)}
        configured_kit = configured[result].get('pychopper_kit', 'PCS109')
        if kits and kits != {configured_kit}:
            return 'unknown', 'PCR_cDNA_kit_differs_from_alignment_config'
    return result, None


def normalize_bundle(bundle, species, protocols):
    portal = bundle['portal']; accession = portal['run_accession']
    if species not in SPECIES or not RUN.fullmatch(accession) or accession.startswith('CRR'):
        raise ValueError('unsupported ENA run/species')
    run = xml_record(bundle['run_xml'], 'RUN', accession)
    experiment_id = portal['experiment_accession']
    experiment = xml_record(bundle['experiment_xml'], 'EXPERIMENT', experiment_id)
    sample_id = portal['sample_accession']
    sample = xml_record(bundle['sample_xml'], 'SAMPLE', sample_id)
    row, fields, issues = {}, {}, []

    def evidence(source, path, value):
        return {'source': bundle.get('source','ENA'), 'url': bundle['urls'].get(source, ''),
                'record': {'run': accession, 'experiment': experiment_id, 'sample': sample_id,
                           'portal': accession,'biosample':bundle.get('biosample_accession',accession)}.get(source, accession),
                'path': path, 'raw_value': value,
                'fetched_at': bundle.get('timestamps', {}).get(source, bundle.get('fetched_at'))}

    def put(name, value, entries, reason=None):
        row[name] = value
        fields[name] = {'normalized_value': value, 'evidence': entries,
                        'status': 'missing' if value == 'unknown' else 'reported',
                        'reason': reason or ('not_reported' if value == 'unknown' else 'explicit_record_value')}

    def issue(name, reason):
        issues.append(name + ': ' + reason)

    def from_ids(name, values, pattern, entries):
        found = sorted(v for v in values if pattern.fullmatch(v))
        put(name, found[0] if len(found) == 1 else 'unknown', entries,
            'multiple_identifiers' if len(found) > 1 else None)
        if len(found) != 1: issue(name, 'missing_or_conflicting_linked_identifier')

    run_ids = {run.get('accession', '')}
    for tag in ('PRIMARY_ID', 'SECONDARY_ID'):
        run_ids.update(clean(n.text) for n in run.findall('./IDENTIFIERS/' + tag))
    run_ids.update(clean(n.text) for n in run.findall('./IDENTIFIERS/EXTERNAL_ID')
                   if n.get('namespace', '').upper() in {'SRA', 'ENA', 'DDBJ', 'INSDC'})
    aliases = sorted(x for x in run_ids if RUN.fullmatch(x))
    put('Accession', accession, [evidence('run', '@accession/IDENTIFIERS', aliases)])
    refs = run.findall('./EXPERIMENT_REF')
    if len(refs) != 1 or experiment_id not in identifiers(refs[0]):
        issue('Accession', 'run_experiment_link_mismatch')
    descriptors = experiment.findall('./DESIGN/SAMPLE_DESCRIPTOR')
    sample_ids = identifiers(sample)
    expected_sample_ids = split_ids(portal.get('sample_accession')) | split_ids(portal.get('secondary_sample_accession'))
    if (len(descriptors) != 1 or not identifiers(descriptors[0]) & sample_ids
            or not expected_sample_ids <= sample_ids):
        issue('BioSample', 'experiment_sample_link_mismatch')
    all_sample_ids = sample_ids | expected_sample_ids
    if len(descriptors) == 1: all_sample_ids |= identifiers(descriptors[0])
    from_ids('BioSample', all_sample_ids, BIOSAMPLE,
             [evidence('sample', 'IDENTIFIERS', sorted(all_sample_ids))])
    project_ids = split_ids(portal.get('study_accession')) | split_ids(portal.get('secondary_study_accession'))
    study_refs = experiment.findall('./STUDY_REF')
    if len(study_refs) != 1:
        issue('BioProject', 'missing_or_multiple_study_links')
    else:
        xml_projects = identifiers(study_refs[0])
        if not project_ids & xml_projects: issue('BioProject', 'experiment_project_link_mismatch')
        project_ids |= xml_projects
    from_ids('BioProject', project_ids, PROJECT_ID,
             [evidence('experiment', 'STUDY_REF/IDENTIFIERS + portal study', sorted(project_ids))])

    tax = clean(portal.get('tax_id')); sample_tax = clean(sample.findtext('./SAMPLE_NAME/TAXON_ID'))
    names = [clean(portal.get('scientific_name')), clean(sample.findtext('./SAMPLE_NAME/SCIENTIFIC_NAME'))]
    expected_name = SPECIES[species]
    names_valid = all(n.casefold() == expected_name.casefold() or
                      n.casefold().startswith(expected_name.casefold() + ' ') for n in names)
    mixed = any(re.search(r'\bhybrid\b|\bmixed\b|\s[x×]\s', n, re.I) for n in names)
    # Search is constrained by tax_tree; additionally verify two linked records agree.
    if not tax.isdigit() or tax != sample_tax or not names_valid or mixed:
        issue('Species', 'taxon_or_scientific_name_conflict')
    if tax in {str(t) for t in TAXA.values()} and tax != str(TAXA[species]):
        issue('Species', 'taxon_outside_requested_species')
    put('Species', expected_name, [evidence('portal', 'tax_id/scientific_name', [tax, names[0]]),
                                 evidence('sample', 'SAMPLE_NAME', [sample_tax, names[1]])])

    platform = clean(portal.get('instrument_platform')).upper()
    xml_platform = [n.tag for n in experiment.findall('./PLATFORM/*')]
    if platform not in {'PACBIO_SMRT', 'OXFORD_NANOPORE'} or xml_platform != [platform]:
        issue('Platform', 'missing_or_conflicting_platform')
    put('Platform', platform or 'unknown', [evidence('portal', 'instrument_platform', platform),
                                           evidence('experiment', 'PLATFORM', xml_platform)])
    source = clean(portal.get('library_source')).upper()
    xml_source = clean(experiment.findtext('./DESIGN/LIBRARY_DESCRIPTOR/LIBRARY_SOURCE')).upper()
    if source != 'TRANSCRIPTOMIC' or source != xml_source:
        issue('Protocol', 'non_transcriptomic_or_conflicting_library_source')
    protocol_entries = [evidence('portal', key, portal.get(key, '')) for key in
                        ('library_construction_protocol', 'experiment_title', 'library_name')]
    protocol_entries += [evidence('experiment', path, clean(experiment.findtext(path))) for path in
                         ('./TITLE', './DESIGN/DESIGN_DESCRIPTION', './DESIGN/LIBRARY_DESCRIPTOR/LIBRARY_NAME',
                          './DESIGN/LIBRARY_DESCRIPTOR/LIBRARY_CONSTRUCTION_PROTOCOL')]
    protocol, reason = classify_protocol(' '.join(str(e['raw_value']) for e in protocol_entries), platform, protocols)
    put('Protocol', protocol, protocol_entries, reason)
    if reason: issue('Protocol', reason)

    attributes = []
    for attr in sample.findall('./SAMPLE_ATTRIBUTES/SAMPLE_ATTRIBUTE'):
        key = clean(attr.findtext('TAG')).casefold().replace('_', ' ').replace('-', ' ')
        value = clean(attr.findtext('VALUE'))
        attributes.append((key, value, evidence('sample', 'SAMPLE_ATTRIBUTE/' + key, value)))

    if bundle.get('biosample_xml'):
        text=bundle['biosample_xml']
        if re.search(r'<!\s*(DOCTYPE|ENTITY)',text,re.I):raise ValueError('DTD/entities in BioSample XML')
        biosamples=ET.fromstring(text).findall('.//BioSample')
        if len(biosamples)!=1 or biosamples[0].get('accession')!=bundle['biosample_accession']:
            raise ValueError('BioSample accession mismatch')
        bio=biosamples[0];organism=bio.find('Description/Organism')
        if (organism is None or organism.get('taxonomy_id')!=sample_tax
                or clean(organism.findtext('OrganismName')).casefold()!=names[1].casefold()):
            issue('Species','BioSample_taxon_mismatch')
        for attr in bio.findall('Attributes/Attribute'):
            raw_key=attr.get('attribute_name','');key=clean(raw_key).casefold().replace('_',' ').replace('-',' ')
            value=clean(attr.text)
            attributes.append((key,value,evidence('biosample','Attributes/Attribute[@attribute_name='+raw_key+']',value)))

    def attr_field(name, keys, synonyms=None):
        entries = [e for k, v, e in attributes if k in keys]
        values = []
        for e in entries:
            value = e['raw_value']
            if value.casefold() in UNKNOWN: continue
            if value.casefold() == 'not applicable': value = 'not_applicable'
            value = (synonyms or {}).get(value.casefold(), value)
            if value not in values: values.append(value)
        put(name, values[0] if len(values) == 1 else 'unknown', entries,
            'conflicting_values' if len(values) > 1 else None)
        if len(values) > 1: issue(name, 'conflicting_structured_values')

    attr_field('Tissue', {'tissue', 'tissue type', 'plant tissue', 'organ', 'organism part'},
               {'seedlings': 'seedling', 'leaves': 'leaf', 'roots': 'root'})
    attr_field('Ecotype', {'ecotype', 'cultivar', 'variety', 'strain', 'accession'})
    attr_field('Genotype', {'genotype', 'genetic background'})
    treatment_keys = TREATMENT_KEYS
    treatment = [(k, v, e) for k, v, e in attributes if k in treatment_keys and v.casefold() not in UNKNOWN]
    summary, reason = summarize_treatment('; '.join(dict.fromkeys(k + ': ' + v for k, v, e in treatment)))
    put('Treatment', summary, [e for k, v, e in treatment], reason)
    put('Database', {'SRR': 'NCBI', 'ERR': 'ENA', 'DRR': 'DDBJ'}[accession[:3]],
        [evidence('run', 'INSDC run accession prefix', accession)])
    return {'accession': accession, 'aliases': aliases, 'row': {k: row[k] for k in CORE_FIELDS},
            'fields': fields, 'issues': issues, 'source_fingerprint': identity(portal),
            'normalization_version': TREATMENT_NORMALIZATION_VERSION}
