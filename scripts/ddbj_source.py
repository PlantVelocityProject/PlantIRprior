"""DDBJ-native discovery and normalization of the archive's actual JSON records.

DDBJ currently stores taxonomy on samples, library metadata on experiments, and
neither on runs. Never interpret a zero-result organism filter on runs as an
absence of data. All transport, response receipts, and offline replay use EnaClient.
"""
import base64
import json
import re
from urllib.error import HTTPError

from discovery_metadata import (BIOSAMPLE, CORE_FIELDS, TAXA, TREATMENT_KEYS, UNKNOWN,
                                classify_protocol, clean, identity)
from download_reads import SPECIES
from ena_source import EnaClient, MetadataLinkError
from treatment_metadata import summarize_treatment, TREATMENT_NORMALIZATION_VERSION

BASE = 'https://ddbj.nig.ac.jp/search/api/'
DRR = re.compile(r'DRR[0-9]+\Z')
DRX = re.compile(r'DRX[0-9]+\Z')
PROJECT = re.compile(r'(?:PRJD[AB][0-9]+|DRP[0-9]+)\Z')
SORT = [{'_score': {'order': 'desc'}}, {'identifier': {'order': 'asc'}}]
PLATFORMS = {'PACBIO_SMRT', 'OXFORD_NANOPORE'}


def _list(value):
    return value if isinstance(value, list) else ([] if value is None else [value])


def _located_record(document, set_name, tag, expected):
    value = document.get('properties', {}).get(set_name, {}).get(tag)
    matches = [(index, node) for index, node in enumerate(_list(value))
               if isinstance(node, dict) and node.get('accession') == expected]
    if len(matches) != 1:
        raise MetadataLinkError('DDBJ expected one {} properties record for {}'.format(tag, expected))
    index, node = matches[0]
    path = '$.properties.' + set_name + '.' + tag
    if isinstance(value, list): path += '[{}]'.format(index)
    return node, path


def _record(document, set_name, tag, expected):
    return _located_record(document, set_name, tag, expected)[0]


def _primary(reference, label):
    return {clean(n.get('content')) for n in _list(reference.get('IDENTIFIERS', {}).get('PRIMARY_ID'))
            if isinstance(n, dict) and n.get('label') == label and clean(n.get('content'))}


def _refs(document, kind):
    refs = document.get('dbXrefs', [])
    if not isinstance(refs, list) or any(not isinstance(x, dict) for x in refs):
        raise ValueError('invalid DDBJ dbXrefs')
    values = {r.get('identifier') for r in refs if r.get('type') == kind}
    if any(not isinstance(a, str) or not a for a in values):
        raise ValueError('invalid DDBJ linked identifier')
    count = document.get('dbXrefsCount', {}).get(kind)
    if count is not None and (type(count) is not int or count != len(values)):
        raise MetadataLinkError('DDBJ truncated or conflicting {} links'.format(kind))
    return values


def _one(values, label):
    if len(values) != 1:
        raise MetadataLinkError('missing or multiple DDBJ ' + label)
    return next(iter(values))


def _records_fingerprint(bundle):
    """Identify the actual archive records, independently of derived search rows."""
    return identity({key: bundle[key] for key in
                     ['run_json', 'experiment_json', 'sample_json', 'biosample_json', 'project_json']})


def _species_match(document, species):
    organism = document.get('organism') or {}
    taxon, name = clean(organism.get('identifier')), clean(organism.get('name'))
    # Unknown or malformed taxonomy remains reviewable.  It must neither be
    # treated as a positive species match nor suppress a potentially valid run.
    if not re.fullmatch(r'[1-9][0-9]*', taxon) or not name:
        return None
    expected = SPECIES[species].casefold()
    if re.search(r'\bhybrid\b|\bmixed\b|\s[x×]\s', name, re.I):
        return False
    if taxon in {str(v) for v in TAXA.values()} and taxon != str(TAXA[species]):
        return False
    return name.casefold() == expected or name.casefold().startswith(expected + ' ')


class _Missing(ValueError):
    pass


class DdbjClient(EnaClient):
    def __init__(self, batch, base=BASE, offline=False, timeout=45, retries=2,
                 page_size=100, **kwargs):
        super().__init__(batch, base=base, offline=offline, timeout=timeout,
                         retries=retries, provider='DDBJ', **kwargs)
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError('DDBJ page_size must be 1..100')
        self.page_size = page_size
        self.documents = {}
        self.bundles = {}
        self.missing_samples = set()
        self.link_documents = {}

    def _json(self, path, params=None, method='GET', json_body=None):
        try:
            if method == 'GET':
                raw, receipt = self.fetch(path, params)
            else:
                raw, receipt = self.fetch(path, params, method=method, json_body=json_body)
        except RuntimeError as exc:
            cause = exc.__cause__
            if isinstance(cause, HTTPError) and cause.code == 404:
                raise _Missing('DDBJ record not found: ' + path) from exc
            raise
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ValueError('DDBJ response is not valid JSON: ' + path) from exc
        if not isinstance(value, dict):
            raise ValueError('DDBJ response is not an object: ' + path)
        if isinstance(value.get('status'), int) and value['status'] >= 400:
            error = _Missing if value['status'] == 404 else ValueError
            raise error('DDBJ API error: ' + str(value.get('detail') or value.get('title')))
        return value, receipt

    def _detail(self, kind, accession):
        key = (kind, accession)
        if key not in self.documents:
            value, receipt = self._json('entries/{}/{}'.format(kind, accession))
            self._store(kind, accession, value, receipt)
        return self.documents[key]

    def _store(self, kind, accession, value, receipt):
        if value.get('identifier') != accession or value.get('type') != kind:
            raise MetadataLinkError('DDBJ {} identifier/type mismatch: {}'.format(kind, accession))
        self.documents[(kind, accession)] = value, receipt

    def _bulk_samples(self, accessions):
        missing = sorted(a for a in set(accessions) if ('biosample', a) not in self.documents)
        for start in range(0, len(missing), 1000):
            ids = missing[start:start + 1000]
            value, receipt = self._json('entries/biosample/bulk', {'includeDbXrefs': 'false'},
                                        method='POST', json_body={'ids': ids})
            entries, absent = value.get('entries'), value.get('notFound')
            if not isinstance(entries, list) or not isinstance(absent, list):
                raise ValueError('invalid DDBJ bulk response')
            seen = set()
            for index, entry in enumerate(entries):
                accession = entry.get('identifier') if isinstance(entry, dict) else None
                if accession not in ids or accession in seen:
                    raise ValueError('DDBJ bulk returned duplicate or unrequested sample')
                seen.add(accession)
                self._store('biosample', accession, entry,
                            dict(receipt, record_path='$.entries[{}]'.format(index)))
            if seen & set(absent) or seen | set(absent) != set(ids):
                raise ValueError('incomplete DDBJ bulk response')
            # Missing linked metadata must remain reviewable, rather than becoming
            # an apparently complete, empty species search.
            self.missing_samples.update(absent)

    def _experiments(self):
        params = {'platform': 'PACBIO_SMRT,OXFORD_NANOPORE', 'librarySource': 'TRANSCRIPTOMIC',
                  'perPage': self.page_size, 'includeProperties': 'false'}
        total = None; previous = ''; seen = set(); tokens = set(); count = 0
        while True:
            value, receipt = self._json('entries/sra-experiment/', params)
            pagination, items = value.get('pagination'), value.get('items')
            if not isinstance(pagination, dict) or not isinstance(items, list):
                raise ValueError('invalid DDBJ search response')
            current = pagination.get('total'); more = pagination.get('hasNext')
            token = pagination.get('nextCursor')
            if type(current) is not int or current < 0 or type(more) is not bool:
                raise ValueError('invalid DDBJ pagination')
            if total is None: total = current
            if current != total: raise ValueError('DDBJ search total changed during pagination')
            if len(items) != min(self.page_size, total - count):
                raise ValueError('DDBJ incomplete or oversized search page')
            if bool(token) != more or (token is not None and not isinstance(token, str)):
                raise ValueError('DDBJ pagination cursor/hasNext mismatch')
            native = []; crossed = False
            for item in items:
                identifier = item.get('identifier') if isinstance(item, dict) else None
                if (not isinstance(identifier, str) or not re.fullmatch(r'[SED]RX[0-9]+', identifier)
                        or item.get('type') != 'sra-experiment'):
                    raise ValueError('invalid DDBJ experiment search record')
                if identifier in seen or (previous and identifier <= previous):
                    raise ValueError('DDBJ duplicate or nonmonotonic experiment pagination')
                seen.add(identifier); previous = identifier; count += 1
                if identifier.startswith('DRX'): native.append(item)
                else: crossed = True
            if count > total or (not more and count != total):
                raise ValueError('DDBJ search count does not match total')
            if more:
                try:
                    state = json.loads(base64.urlsafe_b64decode(token.split('.', 1)[1] + '==='))
                except (ValueError, IndexError, UnicodeError) as exc:
                    raise ValueError('DDBJ cursor cannot establish deterministic native ordering') from exc
                if (state.get('sort') != SORT or state.get('search_after') != [0.0, previous]
                        or token in tokens):
                    raise ValueError('DDBJ cursor sorting changed or cursor repeated')
                tokens.add(token)
            for item in native: yield item, receipt
            # Only a verified, zero-score identifier ordering authorizes stopping
            # before mirror records. Final-page count is checked above as well.
            if crossed or not more: return
            params = {'cursor': token, 'perPage': self.page_size}

    def _project_runs(self, projects):
        runs = set()
        for project in sorted(set(projects)):
            kind = 'sra-study' if project.startswith('DRP') else 'bioproject'
            # DBLinks can return 200 + [] for a nonexistent source accession.
            # Verify the actual project before accepting an empty selection.
            detail, _ = self._detail(kind, project)
            value, _ = self._json('dblink/{}/{}'.format(kind, project), {'target': 'sra-run'})
            if value.get('identifier') != project or value.get('type') != kind:
                raise ValueError('DDBJ project links identifier mismatch')
            linked = _refs(value, 'sra-run')
            declared = detail.get('dbXrefsCount', {}).get('sra-run')
            if declared is not None and (type(declared) is not int or declared < 0 or declared != len(linked)):
                raise ValueError('DDBJ project run count disagrees with complete DBLinks response: ' + project)
            runs.update(a for a in linked if DRR.fullmatch(a))
        return runs

    def _assemble(self, accession):
        result = {'source': 'DDBJ', 'urls': {}, 'timestamps': {}, 'json_paths': {}}
        self.link_documents[accession] = []

        def add(label, kind, identifier):
            if kind == 'biosample' and identifier in self.missing_samples:
                raise MetadataLinkError('DDBJ linked BioSample missing: ' + identifier)
            try:
                document, receipt = self._detail(kind, identifier)
            except _Missing as exc:
                if kind == 'sra-run': raise
                raise MetadataLinkError('DDBJ linked record missing: ' + identifier) from exc
            result[label + '_json'] = document
            result['urls'][label] = receipt['url']
            result['timestamps'][label] = receipt['fetched_at']
            result['json_paths'][label] = receipt.get('record_path', '$')
            result['fetched_at'] = receipt['fetched_at']
            self.link_documents[accession].append((kind, identifier))
            return document

        run = add('run', 'sra-run', accession)
        raw_run = _record(run, 'RUN_SET', 'RUN', accession)
        experiment_id = _one(_refs(run, 'sra-experiment'), 'run experiment link')
        if not DRX.fullmatch(experiment_id) or raw_run.get('EXPERIMENT_REF', {}).get('accession') != experiment_id:
            raise MetadataLinkError('DDBJ run experiment linkage mismatch')
        experiment = add('experiment', 'sra-experiment', experiment_id)
        raw_experiment = _record(experiment, 'EXPERIMENT_SET', 'EXPERIMENT', experiment_id)
        descriptor = raw_experiment.get('DESIGN', {}).get('SAMPLE_DESCRIPTOR', {})
        sample_id = _one(_refs(run, 'sra-sample'), 'run sample link')
        if (descriptor.get('accession') != sample_id or sample_id not in _refs(experiment, 'sra-sample')):
            raise MetadataLinkError('DDBJ experiment sample linkage mismatch')
        sample = add('sample', 'sra-sample', sample_id)
        raw_sample = _record(sample, 'SAMPLE_SET', 'SAMPLE', sample_id)
        bio_id = _one(_refs(run, 'biosample'), 'run BioSample link')
        if (not BIOSAMPLE.fullmatch(bio_id) or _primary(descriptor, 'BioSample ID') != {bio_id}
                or _primary(raw_sample, 'BioSample ID') != {bio_id}
                or _refs(experiment, 'biosample') != {bio_id} or _refs(sample, 'biosample') != {bio_id}):
            raise MetadataLinkError('DDBJ experiment BioSample linkage mismatch')
        biosample = add('biosample', 'biosample', bio_id)
        bio_properties = biosample.get('properties', {}).get('BioSample', {})
        primary_ids = {clean(n.get('content')) for n in _list(bio_properties.get('Ids', {}).get('Id'))
                       if isinstance(n, dict) and n.get('namespace') == 'BioSample'}
        if primary_ids != {bio_id}: raise MetadataLinkError('DDBJ BioSample properties identifier mismatch')
        projects = _refs(run, 'bioproject')
        project_id = _one(projects, 'BioProject link')
        study = raw_experiment.get('STUDY_REF', {})
        if (_primary(study, 'BioProject ID') != projects or _refs(experiment, 'bioproject') != projects
                or (study.get('accession') not in _refs(run, 'sra-study'))):
            raise MetadataLinkError('DDBJ experiment BioProject linkage mismatch')
        add('project', 'bioproject', project_id)
        result['portal'] = {'run_accession': accession, 'experiment_accession': experiment_id,
            'sample_accession': sample_id, 'secondary_sample_accession': bio_id,
            'study_accession': project_id, 'secondary_study_accession': clean(study.get('accession')),
            'tax_id': clean((biosample.get('organism') or {}).get('identifier')),
            'scientific_name': clean((biosample.get('organism') or {}).get('name')),
            'instrument_platform': clean(experiment.get('platform')),
            'library_source': ','.join(experiment.get('librarySource') or []),
            'library_strategy': ','.join(experiment.get('libraryStrategy') or []),
            'library_name': clean(experiment.get('libraryName')),
            'experiment_title': clean(experiment.get('title')),
            'library_construction_protocol': clean(experiment.get('libraryConstructionProtocol'))}
        result['urls']['portal'] = result['urls']['run']
        result['timestamps']['portal'] = result['timestamps']['run']
        return result

    def search(self, species, accessions=(), projects=(), allow_missing=False):
        if species not in TAXA: raise ValueError('unsupported species: ' + species)
        if any(not DRR.fullmatch(a) for a in accessions):
            raise ValueError('DDBJ native discovery requires DRR accessions')
        if any(not PROJECT.fullmatch(p) for p in projects):
            raise ValueError('DDBJ native project must be PRJDB/PRJDA/DRP')
        requested = set(accessions)
        selected_projects = self._project_runs(projects) if projects else None
        if requested:
            candidates = requested & selected_projects if projects else requested
        elif projects:
            candidates = selected_projects
        else:
            experiments = list(self._experiments())
            samples = {a for document, _ in experiments for a in _refs(document, 'biosample')}
            self._bulk_samples(samples)
            candidates = set()
            for experiment, receipt in experiments:
                biosamples = _refs(experiment, 'biosample')
                matches = [_species_match(self.documents.get(('biosample', a), ({}, None))[0], species)
                           for a in biosamples]
                if matches and all(match is False for match in matches): continue
                candidates.update(a for a in _refs(experiment, 'sra-run') if DRR.fullmatch(a))
        rows = []
        for accession in sorted(candidates):
            try:
                bundle = self._assemble(accession)
            except _Missing:
                if accession in requested and allow_missing: continue
                raise
            except MetadataLinkError as exc:
                # Linkage failures are candidates for explicit review, not evidence
                # that the species has no new runs.
                rows.append({'run_accession': accession, '_link_error': str(exc)})
                continue
            if _species_match(bundle['biosample_json'], species) is False: continue
            row = bundle['portal']
            if projects and not set(projects) & {row['study_accession'], row['secondary_study_accession']}:
                raise ValueError('DDBJ project selector disagrees with linked run metadata: ' + accession)
            if row['instrument_platform'] and row['instrument_platform'] not in PLATFORMS: continue
            if row['library_source'] and row['library_source'] != 'TRANSCRIPTOMIC': continue
            # These linked records have already been read during discovery. Their
            # revisions must invalidate pending skips even when the small set of
            # search columns stays unchanged. Never mutate the archived JSON.
            row['_records_fingerprint'] = _records_fingerprint(bundle)
            self.bundles[identity(row)] = bundle
            rows.append(row)
        found = {r['run_accession'] for r in rows}
        if requested - found and not allow_missing:
            raise ValueError('requested DDBJ runs not returned by species/RNA/platform/project selection: ' +
                             ', '.join(sorted(requested - found)))
        return rows

    def bundle(self, row):
        if row.get('_link_error'): raise MetadataLinkError(row['_link_error'])
        if identity(row) not in self.bundles:
            raise ValueError('DDBJ bundle requires a verified search row')
        return self.bundles[identity(row)]

    def link_evidence(self, row):
        """Return actual successful response references after a linkage failure.

        Local error messages and inferred fields are not source facts.  Missing
        records, including an uncached/404 run, produce no invented evidence.
        """
        entries = []
        for key in self.link_documents.get(row.get('run_accession'), []):
            document, receipt = self.documents[key]
            entries.append({'source': 'DDBJ', 'url': receipt['url'],
                'record': document['identifier'],
                'path': receipt.get('record_path', '$') + '.identifier',
                'raw_value': document['identifier'], 'fetched_at': receipt['fetched_at']})
        return entries


def normalize_ddbj_bundle(bundle, species, protocols):
    """Normalize native JSON without manufacturing XML or evidence paths."""
    portal = bundle['portal']; accession = portal['run_accession']
    if species not in SPECIES or not DRR.fullmatch(accession) or bundle.get('source') != 'DDBJ':
        raise ValueError('unsupported native DDBJ bundle/species')
    experiment_id, sample_id = portal['experiment_accession'], portal['sample_accession']
    run, run_path = _located_record(bundle['run_json'], 'RUN_SET', 'RUN', accession)
    experiment, experiment_path = _located_record(bundle['experiment_json'], 'EXPERIMENT_SET', 'EXPERIMENT', experiment_id)
    sample, sample_path = _located_record(bundle['sample_json'], 'SAMPLE_SET', 'SAMPLE', sample_id)
    biosample = bundle['biosample_json']; bio = biosample.get('properties', {}).get('BioSample', {})
    library = experiment.get('DESIGN', {}).get('LIBRARY_DESCRIPTOR', {})
    row, fields, issues = {}, {}, []
    record_ids = {'run': accession, 'experiment': experiment_id, 'sample': sample_id,
                  'biosample': biosample['identifier'], 'project': portal['study_accession']}
    property_paths = {'run': ('$.properties.RUN_SET.RUN', run_path),
                      'experiment': ('$.properties.EXPERIMENT_SET.EXPERIMENT', experiment_path),
                      'sample': ('$.properties.SAMPLE_SET.SAMPLE', sample_path)}

    def evidence(kind, path, value):
        if kind in property_paths:
            original, actual = property_paths[kind]
            if path == original or path.startswith(original + '.'):
                path = actual + path[len(original):]
        return {'source': 'DDBJ', 'url': bundle['urls'][kind], 'record': record_ids[kind],
                'path': bundle.get('json_paths', {}).get(kind, '$') + path[1:],
                'raw_value': value, 'fetched_at': bundle['timestamps'][kind]}

    def put(name, value, entries, reason=None):
        row[name] = value
        fields[name] = {'normalized_value': value, 'evidence': entries,
                        'status': 'missing' if value == 'unknown' else 'reported',
                        'reason': reason or ('not_reported' if value == 'unknown' else 'explicit_record_value')}

    def issue(name, reason): issues.append(name + ': ' + reason)

    put('Accession', accession, [evidence('run', '$.properties.RUN_SET.RUN.accession', run['accession'])])
    put('BioSample', biosample['identifier'], [evidence('biosample', '$.identifier', biosample['identifier'])])
    put('BioProject', portal['study_accession'], [evidence('project', '$.identifier', bundle['project_json']['identifier'])])
    taxonomies = [bundle['sample_json'].get('organism') or {}, biosample.get('organism') or {},
        {'identifier': sample.get('SAMPLE_NAME', {}).get('TAXON_ID'),
         'name': sample.get('SAMPLE_NAME', {}).get('SCIENTIFIC_NAME')},
        {'identifier': bio.get('Description', {}).get('Organism', {}).get('taxonomy_id'),
         'name': bio.get('Description', {}).get('Organism', {}).get('OrganismName')}]
    species_evidence = [evidence('sample', '$.organism', bundle['sample_json'].get('organism')),
        evidence('biosample', '$.organism', biosample.get('organism')),
        evidence('sample', '$.properties.SAMPLE_SET.SAMPLE.SAMPLE_NAME', sample.get('SAMPLE_NAME')),
        evidence('biosample', '$.properties.BioSample.Description.Organism',
                 bio.get('Description', {}).get('Organism'))]
    valid_species = (all(_species_match({'organism': t}, species) is True for t in taxonomies)
                     and len({clean(t.get('identifier')) for t in taxonomies}) == 1
                     and len({clean(t.get('name')).casefold() for t in taxonomies}) == 1)
    put('Species', SPECIES[species] if valid_species else 'unknown', species_evidence)
    if not valid_species: issue('Species', 'missing_or_conflicting_official_sample_taxonomy')
    platform = clean(portal['instrument_platform'])
    raw_platforms = list(experiment.get('PLATFORM', {}))
    platform_valid = platform in PLATFORMS and raw_platforms == [platform]
    put('Platform', platform if platform_valid else 'unknown', [
        evidence('experiment', '$.platform', bundle['experiment_json'].get('platform')),
        evidence('experiment', '$.properties.EXPERIMENT_SET.EXPERIMENT.PLATFORM', experiment.get('PLATFORM'))])
    if not platform_valid: issue('Platform', 'missing_or_conflicting_platform')
    source = portal['library_source']; raw_source = clean(library.get('LIBRARY_SOURCE'))
    if source != 'TRANSCRIPTOMIC' or raw_source != source:
        issue('Protocol', 'missing_or_nontranscriptomic_or_conflicting_library_source')
    protocol_entries = [evidence('experiment', '$.' + key, bundle['experiment_json'].get(key)) for key in
                        ['title', 'libraryName', 'libraryConstructionProtocol']]
    protocol_entries += [evidence('experiment', '$.properties.EXPERIMENT_SET.EXPERIMENT.' + path, value)
        for path, value in [('TITLE', experiment.get('TITLE')),
            ('DESIGN.DESIGN_DESCRIPTION', experiment.get('DESIGN', {}).get('DESIGN_DESCRIPTION')),
            ('DESIGN.LIBRARY_DESCRIPTOR.LIBRARY_NAME', library.get('LIBRARY_NAME')),
            ('DESIGN.LIBRARY_DESCRIPTOR.LIBRARY_CONSTRUCTION_PROTOCOL', library.get('LIBRARY_CONSTRUCTION_PROTOCOL'))]]
    protocol_text = ' '.join(clean(e['raw_value']) for e in protocol_entries)
    # A named PacBio IsoSeq library can use a PCR cDNA synthesis reagent. The
    # reagent's name is not an Oxford Nanopore PCR-cDNA sequencing protocol.
    # Preserve the exact original text in evidence above, and exclude only this
    # explicit reagent phrase from the classification input.
    if row['Platform'] == 'PACBIO_SMRT':
        protocol_text = re.sub(r'\bpcr[ -]?cdna\s+synthesis\s+kit\b',
                               'cDNA synthesis reagent', protocol_text, flags=re.I)
    protocol, reason = classify_protocol(protocol_text, row['Platform'], protocols)
    if source != 'TRANSCRIPTOMIC' or raw_source != source:
        protocol, reason = 'unknown', 'missing_or_conflicting_library_source'
    put('Protocol', protocol, protocol_entries + [
        evidence('experiment', '$.librarySource', bundle['experiment_json'].get('librarySource')),
        evidence('experiment', '$.properties.EXPERIMENT_SET.EXPERIMENT.DESIGN.LIBRARY_DESCRIPTOR.LIBRARY_SOURCE', library.get('LIBRARY_SOURCE'))], reason)
    if reason: issue('Protocol', reason)
    attributes = []
    for index, attribute in enumerate(_list(sample.get('SAMPLE_ATTRIBUTES', {}).get('SAMPLE_ATTRIBUTE'))):
        if not isinstance(attribute, dict): continue
        key, value = clean(attribute.get('TAG')).casefold().replace('_', ' ').replace('-', ' '), clean(attribute.get('VALUE'))
        path = '$.properties.SAMPLE_SET.SAMPLE.SAMPLE_ATTRIBUTES.SAMPLE_ATTRIBUTE'
        if isinstance(sample.get('SAMPLE_ATTRIBUTES', {}).get('SAMPLE_ATTRIBUTE'), list): path += '[{}]'.format(index)
        attributes.append((key, value, evidence('sample', path, attribute)))
    for index, attribute in enumerate(_list(bio.get('Attributes', {}).get('Attribute'))):
        if not isinstance(attribute, dict): continue
        key, value = clean(attribute.get('attribute_name')).casefold().replace('_', ' ').replace('-', ' '), clean(attribute.get('content'))
        path = '$.properties.BioSample.Attributes.Attribute'
        if isinstance(bio.get('Attributes', {}).get('Attribute'), list): path += '[{}]'.format(index)
        attributes.append((key, value, evidence('biosample', path, attribute)))

    def unknown(value):
        return value.casefold() in UNKNOWN or value.casefold().startswith('missing:')

    def attr_field(name, keys, synonyms=None):
        selected = [(value, entry) for key, value, entry in attributes if key in keys]
        values = []
        for value, entry in selected:
            if unknown(value): continue
            value = 'not_applicable' if value.casefold() == 'not applicable' else (synonyms or {}).get(value.casefold(), value)
            if value not in values: values.append(value)
        reason = 'conflicting_structured_values' if len(values) > 1 else None
        put(name, values[0] if len(values) == 1 else 'unknown', [e for v, e in selected], reason)
        if reason: issue(name, reason)

    attr_field('Tissue', {'tissue', 'tissue type', 'plant tissue', 'organ', 'organism part'},
               {'leaves': 'leaf', 'roots': 'root', 'seedlings': 'seedling'})
    attr_field('Ecotype', {'ecotype', 'cultivar', 'variety', 'strain', 'accession'})
    attr_field('Genotype', {'genotype', 'genetic background'})
    treatment = [(k, v, e) for k, v, e in attributes if k in TREATMENT_KEYS and not unknown(v)]
    summary, reason = summarize_treatment('; '.join(dict.fromkeys(k + ': ' + v for k, v, e in treatment)))
    put('Treatment', summary, [e for k, v, e in treatment], reason)
    put('Database', 'DDBJ', [evidence('run', '$.identifier', accession)])
    return {'accession': accession, 'aliases': [accession], 'row': {k: row[k] for k in CORE_FIELDS},
            'fields': fields, 'issues': issues,
            'source_fingerprint': _records_fingerprint(bundle),
            'normalization_version': TREATMENT_NORMALIZATION_VERSION}
