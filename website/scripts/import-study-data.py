"""Rebuild the local website data from the manuscript's Supplementary Table.xlsx.

Uses only Python's standard library. Source values and gene identifiers are
preserved; no per-event sample support or cross-protocol fields are inferred.
"""
from collections import Counter
import csv
import hashlib
import io
import json
from pathlib import Path
import posixpath
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'manuscript' / 'Supplementary Table.xlsx'
OUT = ROOT / 'website' / 'data'
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
SPECIES = {
    'ath': ('Arabidopsis thaliana', 2256, 146),
    'gmx': ('Glycine max', 2097, 61),
    'zma': ('Zea mays', 1782, 175),
    'sly': ('Solanum lycopersicum', 1330, 53),
    'osa': ('Oryza sativa', 1470, 248),
}


def extract_tables():
    with zipfile.ZipFile(SOURCE) as archive:
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            for _, element in ET.iterparse(archive.open('xl/sharedStrings.xml'), events=['end']):
                if element.tag.endswith('}si'):
                    strings.append(''.join(t.text or '' for t in element.iter() if t.tag.endswith('}t')))
                    element.clear()
        relationships = {el.attrib['Id']: el.attrib['Target'] for el in
                         ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))}
        tables = {}
        for sheet in ET.fromstring(archive.read('xl/workbook.xml')).find('s:sheets', NS):
            if sheet.attrib['name'] not in {'Table S1', 'Table S2', 'Table S4'}:
                continue
            target = relationships[sheet.attrib['{' + NS['r'] + '}id']]
            filename = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
            columns, rows = [], []
            for _, element in ET.iterparse(archive.open(filename), events=['end']):
                if not element.tag.endswith('}row'):
                    continue
                values = {}
                for cell in element.findall('s:c', NS):
                    val = cell.find('s:v', NS)
                    text = val.text if val is not None else ''.join(t.text or '' for t in cell.findall('.//s:t', NS))
                    if cell.attrib.get('t') == 's' and text:
                        text = strings[int(text)]
                    values[''.join(c for c in cell.attrib['r'] if c.isalpha())] = text or ''
                row_number = int(element.attrib['r'])
                if row_number == 2:
                    columns = list(values)
                    headers = [values[k] for k in columns]
                elif row_number > 2 and values.get('A'):
                    rows.append([values.get(k, '') for k in columns])
                element.clear()
            tables[sheet.attrib['name']] = {'columns': headers, 'rows': rows}
        return tables


def csv_bytes(columns, rows):
    handle = io.StringIO(newline='')
    writer = csv.writer(handle, lineterminator='\r\n')
    writer.writerow(columns)
    writer.writerows(rows)
    return handle.getvalue().encode('utf-8-sig')


def main():
    tables = extract_tables()
    events, samples, references = (tables[k] for k in ['Table S4', 'Table S1', 'Table S2'])
    event_species = events['columns'].index('species')
    sample_species = samples['columns'].index('Species')
    event_counts = Counter(row[event_species].replace('_', ' ') for row in events['rows'])
    sample_counts = Counter(row[sample_species] for row in samples['rows'])
    assert len(events['rows']) == 8935 and len(samples['rows']) == 683
    assert len({(row[event_species], row[events['columns'].index('intron_uid')]) for row in events['rows']}) == 8935
    assert all(row[events['columns'].index('IR_class')] == 'high_confidence_IR' for row in events['rows']), 'Unexpected IR classification'
    OUT.mkdir(exist_ok=True)
    resources = {}
    resources['ir-priors.csv'] = csv_bytes(events['columns'], events['rows'])
    resources['samples.csv'] = csv_bytes(samples['columns'], samples['rows'])
    reference_columns = ['species', 'genome_assembly', 'assembly_accession', 'annotation_version']
    resources['reference_genomes.csv'] = csv_bytes(reference_columns, references['rows'])
    for code, (name, expected_events, expected_samples) in SPECIES.items():
        assert event_counts[name] == expected_events and sample_counts[name] == expected_samples
        resources[f'{code}-ir-priors.csv'] = csv_bytes(events['columns'], [r for r in events['rows'] if r[event_species].replace('_', ' ') == name])
        resources[f'{code}-samples.csv'] = csv_bytes(samples['columns'], [r for r in samples['rows'] if r[sample_species] == name])
    for filename, data in resources.items():
        (OUT / filename).write_bytes(data)
    readme = ('PlantIR Prior study resources\n\n'
              'Source: manuscript/Supplementary Table.xlsx\n'
              'Table S1: 683 source long-read sequencing records.\n'
              'Table S2: study reference genome assemblies and gene annotations.\n'
              'Table S4: 8,935 species-specific high-confidence IR events.\n\n'
              'Coordinates are 1-based genomic coordinates, as defined in Supplementary Method 2.\n'
              'Species collections are independent; totals do not imply orthologous events.\n'
              'The source tables do not provide per-event sample-to-read mappings.\n'
              'This is a local manuscript data snapshot, not a versioned public release.\n'
              'Data licensing and a permanent accession have not yet been specified.\n')
    (OUT / 'README.txt').write_text(readme, encoding='utf-8')
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in resources.items()}
    manifest = {
        'source': SOURCE.name, 'source_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        'events': len(events['rows']), 'samples': len(samples['rows']),
        'species': {code: {'name': name, 'events': event_counts[name], 'samples': sample_counts[name]}
                    for code, (name, _, _) in SPECIES.items()},
        'files_sha256': hashes,
    }
    (OUT / 'source-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    payload = {'events': events, 'samples': samples}
    (OUT / 'study-data.js').write_text('window.PLANTIR_DATA = ' + json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + ';\n', encoding='utf-8')
    with zipfile.ZipFile(OUT / 'plantir-study-resources.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for filename in resources:
            archive.write(OUT / filename, filename)
        for filename in ['README.txt', 'source-manifest.json']:
            archive.write(OUT / filename, filename)
    print(json.dumps({'events': len(events['rows']), 'samples': len(samples['rows']), 'species': manifest['species'], 'files': len(resources)}, indent=2))


if __name__ == '__main__':
    main()
