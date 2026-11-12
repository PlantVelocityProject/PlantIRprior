"""Extract Fig1 C as native vectors. Run from the project root with Python 3."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
SVG = 'http://www.w3.org/2000/svg'
XLINK = 'http://www.w3.org/1999/xlink'
ET.register_namespace('', SVG)
ET.register_namespace('xlink', XLINK)


def main():
    source = ROOT / 'Fig1.pdf'
    destination = ROOT / 'website/assets/methods'
    with tempfile.TemporaryDirectory(prefix='plantir-agent-figure-') as temporary:
        converted = Path(temporary) / 'figure.svg'
        subprocess.run(['pdftocairo', '-svg', str(source), str(converted)], check=True)
        original = ET.parse(converted).getroot()
    surface = original.find(f'{{{SVG}}}g')
    assert surface is not None and len(surface) == 614, 'Fig1 changed; remeasure panel C before extracting.'
    indices = [*range(481, 611), 612]
    artwork = ET.Element(f'{{{SVG}}}svg', {
        'viewBox': '31 564 554 167', 'width': '554', 'height': '167',
        'role': 'img', 'aria-labelledby': 'agent-methods-title agent-methods-description',
    })
    ET.SubElement(artwork, f'{{{SVG}}}title', {'id': 'agent-methods-title'}).text = 'Agent-assisted IR prior updates'
    ET.SubElement(artwork, f'{{{SVG}}}desc', {'id': 'agent-methods-description'}).text = (
        'Fig1 panel C. An agent plans, monitors and reports dataset discovery, read retrieval and alignment, '
        'IR evidence validation, incremental pooling and classification. Status returns to the agent. '
        'Updates require manual review; versioned IR priors serve PlantVelocity and the web resource '
        'with provenance, versioning and reproducibility.'
    )
    definitions = ET.SubElement(artwork, f'{{{SVG}}}defs')
    extracted = ET.SubElement(artwork, f'{{{SVG}}}g', {'id': 'agent-methods-surface'})
    for index in indices:
        node = deepcopy(surface[index])
        node.set('data-method-object', str(index))
        if index == 482:
            # The source reads "datesets". a and e use the same advance width.
            node[3].set(f'{{{XLINK}}}href', node[1].get(f'{{{XLINK}}}href'))
        extracted.append(node)
    by_id = {node.get('id'): node for node in original.iter() if node.get('id')}
    needed = set()

    def references(node):
        for element in node.iter():
            for value in element.attrib.values():
                if value.startswith('#'):
                    yield value[1:]
                yield from re.findall(r'url\(#([^)]*)\)', value)

    queue = list(references(extracted))
    while queue:
        identifier = queue.pop()
        if identifier in needed:
            continue
        needed.add(identifier)
        queue.extend(references(by_id[identifier]))
    for identifier in sorted(needed):
        definitions.append(deepcopy(by_id[identifier]))
    # Prefix every imported reference so both inline figures can coexist.
    for node in artwork.iter():
        if node.get('id') in needed:
            node.set('id', 'agent-methods-' + node.get('id'))
        for attribute, value in list(node.attrib.items()):
            if value.startswith('#') and value[1:] in needed:
                node.set(attribute, '#agent-methods-' + value[1:])
            elif 'url(#' in value:
                node.set(attribute, re.sub(r'url\(#([^)]*)\)', r'url(#agent-methods-\1)', value))
    content = ET.tostring(artwork, encoding='utf-8', xml_declaration=True) + b'\n'
    (destination / 'agent-update.svg').write_bytes(content)
    manifest = {
        'source_pdf': 'Fig1.pdf', 'panel': 'C', 'source_sha256': sha256(source.read_bytes()).hexdigest(),
        'conversion': 'pdftocairo -svg; native paths/glyphs; unrelated objects/definitions removed; unique IDs',
        'source_object_count': 614, 'source_object_indices': indices,
        'viewport': {'x': 31, 'y': 564, 'width': 554, 'height': 167},
        'corrections': [{'source_object': 482, 'before': 'datesets', 'after': 'datasets'}],
        'artwork': 'agent-update.svg', 'artwork_sha256': sha256(content).hexdigest(),
    }
    (destination / 'agent-update-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Extracted {len(indices)} source objects and {len(needed)} definitions ({len(content):,} bytes).')


if __name__ == '__main__':
    main()
