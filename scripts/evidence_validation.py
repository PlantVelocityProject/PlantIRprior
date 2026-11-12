#!/usr/bin/env python3
"""Scientific checks run with the selected PlantVelo Python environment."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import platform
import sys

from evidence_contract import CORE_FIELDS, validate_rows


def package_hash(module):
    root = Path(module.__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if path.is_file() and path.suffix in ('.py', '.so', '.pyd'):
            digest.update(str(path.relative_to(root)).encode())
            with path.open('rb') as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(block)
    return digest.hexdigest()


def identity():
    import plantvelo
    import pysam
    from plantvelo._version import __version__
    return {'name': 'plantvelo', 'version': __version__, 'python_version': platform.python_version(),
            'pysam_version': pysam.__version__, 'package_sha256': package_hash(plantvelo),
            'pysam_sha256': package_hash(pysam)}


def check_input(request):
    import pysam
    pysam.quickcheck(request['bam'])
    with pysam.AlignmentFile(request['bam'], 'rb') as bam:
        if bam.header.to_dict().get('HD', {}).get('SO') != 'coordinate':
            raise ValueError('BAM is not declared coordinate-sorted')
        if dict(zip(bam.references, bam.lengths)) != request['sequences']:
            raise ValueError('BAM reference sequences do not match FASTA')
        if not bam.check_index():
            raise ValueError('BAM has no usable index')


def validate(request):
    from plantvelo.irprior import load_introns_from_gtf, normalize_protocol
    introns = load_introns_from_gtf(request['gtf'])
    expected = {i.intron_uid: i for i in introns}
    if not expected:
        raise ValueError('annotation contains no introns')
    report = json.loads(Path(request['out'] + '.run.json').read_text())

    def lookup(uid):
        intron = expected.get(uid)
        if intron is None:
            return None
        return {'gene_id': intron.gene_id, 'intron_id': intron.intron_id,
                'intron_uid': intron.intron_uid, 'chromosome': intron.chrom,
                'start': str(intron.start0 + 1), 'end': str(intron.end0), 'strand': intron.strand,
                'supporting_transcripts': intron.supporting_transcripts,
                'anchor_count': str(intron.anchor_count), 'exon_overlap_warning': intron.exon_overlap_warning}

    with open(request['out'], newline='') as handle:
        reader = csv.DictReader(handle, delimiter='\t')
        if reader.fieldnames != CORE_FIELDS:
            raise ValueError('unexpected evidence TSV columns')
        checked = validate_rows(reader, {'Accession': request['sample_id'], 'Species': request['species'],
                                        'Protocol': normalize_protocol(request['protocol'])},
                                lookup, len(expected), 'native', report)
    parameters = dict(request['parameters'], gtf=request['gtf'], bam=request['bam'], out=request['out'],
                      sample_id=request['sample_id'], protocol=normalize_protocol(request['protocol']), species=request['species'])
    if report.get('parameters') != parameters:
        raise ValueError('PlantVelo run report parameters do not match request')
    for key in ['name', 'version', 'pysam_version', 'python_version']:
        if report.get('software', {}).get(key) != request['software'][key]:
            raise ValueError('PlantVelo run report software identity mismatch')
    return dict(checked, coordinate_system='1-based inclusive',
                reference_match='exact_introns_and_supporting_transcripts')



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['identity', 'input', 'output', 'readiness'])
    parser.add_argument('request', nargs='?')
    args = parser.parse_args()
    try:
        if args.mode == 'identity':
            result = identity()
        elif args.mode == 'readiness':
            from evidence_readiness import build_reference,inspect_evidence,ReadinessCache
            request=json.loads(Path(args.request).read_text())
            reference=build_reference(Path(request['reference_folder']),request['metadata'])
            with ReadinessCache(path=request.get('cache_db'))as cache:
                result=inspect_evidence(Path(request['namespace']),request['sample'],reference,
                    request=request.get('policy'),cache=cache).to_dict()
        else:
            request = json.loads(Path(args.request).read_text())
            result = check_input(request) if args.mode == 'input' else validate(request)
        print(json.dumps(result, allow_nan=False))
        return 0
    except Exception as exc:
        print('{}: {}'.format(type(exc).__name__, exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
