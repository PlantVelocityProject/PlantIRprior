#!/usr/bin/env python3
"""Audit historical TSVs against cached counts and admit them for formal publication."""
import argparse
from array import array
import csv
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

from aggregate_store import AggregateStore, COUNT_NAMES, CORE_FIELDS, MAX_INT, encoded
from align_reads import reference_for, verified_reference
from artifact_lifecycle import directory_lock
from discovery_io import atomic_json, atomic_write, digest
from download_reads import PROJECT, Control, now, sha256, species_code
from evidence_admission import CONTRACT, audit_identity, imported_signature, software_policy
from evidence_validation import identity as software_identity


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--species', required=True); p.add_argument('--reference-id'); p.add_argument('--annotation-id')
    for key, default in [('samples','metadata/samples_info.tsv'),('references-dir','storage/references'),
                         ('evidence-dir','storage/evidence'),('aggregate-dir','storage/aggregates'),
                         ('release-dir','releases'),('config','configs/evidence.json')]:
        p.add_argument('--'+key, type=Path, default=PROJECT/default)
    p.add_argument('--source-project',type=Path,default=PROJECT.parent/'20260612PlantIRref')
    p.add_argument('--apply',action='store_true'); p.add_argument('--publish',action='store_true')
    p.add_argument('--dry-run',action='store_true'); p.add_argument('--json',action='store_true')
    return p


def verify_sources(report, folder, control):
    for accession, item in report['samples'].items():
        if sha256(folder/(accession+'.intron_prior.tsv'),control) != item['sha256']:
            raise ValueError('historical source changed: '+accession)


def finish(store, root, folder, control):
    marker = root/'admission_pending.json'; intent = json.loads(marker.read_text())
    report_path = folder/'admission.json'
    if sha256(report_path,control) != intent['receipt']['sha256']: raise ValueError('admission report changed')
    report = json.loads(report_path.read_text()); signature = imported_signature(report)
    verify_sources(report, folder, control)
    current = sha256(folder/'registration.json',control)
    after = (json.dumps(intent['registration'],ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
    if current not in {intent['registration_before_sha256'],digest(after)}:
        raise ValueError('registration changed during evidence admission')
    with store.db:
        for accession, item in report['samples'].items():
            row = store.db.execute('SELECT id,version,signature FROM versions WHERE id=? AND accession=?',
                                   (item['version_id'],accession)).fetchone()
            if not row or row['version'] != item['version']:
                raise ValueError('cached historical evidence identity changed')
            old = json.loads(row['signature'])
            if old['kind'] != 'legacy' and old != signature: raise ValueError('historical evidence already has another admission')
            store.db.execute('UPDATE versions SET signature=? WHERE id=?',(encoded(signature),row['id']))
        store.set('evidence_admission',dict(intent['receipt'],native_policy=report['native_policy'],count_contract=report['count_contract']))
    atomic_json(folder/'registration.json',intent['registration'])
    marker.unlink()
    from discovery_io import sync_directory
    sync_directory(root)
    return {'status':'validated_import','audit_id':report['audit_id'],'samples':len(report['samples']),
            'report':str(report_path),'historical_parameters_verified':False}


def audit(store, folder, registration, reference, args, control):
    if store.pending(): raise ValueError('finish pending aggregate publication before admission')
    if store.get('reference') != reference: raise ValueError('aggregate reference differs from fixed reference')
    attributes = [None]
    names = CORE_FIELDS[:11]
    for row in store.db.execute('SELECT id,attributes FROM introns ORDER BY id'):
        if row['id'] != len(attributes): raise ValueError('noncanonical cached intron IDs')
        a = json.loads(row['attributes']); attributes.append(tuple(a[k] for k in names[1:]))
    sums = array('q',[0]) * (len(attributes)*5); samples = {}
    versions = list(store.db.execute('''SELECT v.* FROM versions v JOIN samples s ON s.version_id=v.id
                                       WHERE s.active=1 ORDER BY v.accession'''))
    if not versions: raise ValueError('no active historical evidence to audit')
    for number, version in enumerate(versions,1):
        if json.loads(version['signature'])['kind'] != 'legacy':
            raise ValueError('initial admission requires a historical-only cohort')
        accession = version['accession']; path = folder/(accession+'.intron_prior.tsv')
        expected = registration['file_sha256'].get(path.name)
        source = json.loads(version['source']); metadata = json.loads(version['metadata'])
        if not expected or source['sha256'] != expected or sha256(path,control) != expected:
            raise ValueError('historical evidence hash mismatch: '+accession)
        cached = iter(store.db.execute('SELECT * FROM contributions WHERE version_id=? ORDER BY intron_id',(version['id'],)))
        totals = [0]*4; rows = 0
        with path.open(newline='') as handle:
            reader = csv.reader(handle,delimiter='\t'); fields = next(reader)
            if (len(fields)!=len(set(fields)) or fields[:15] != CORE_FIELDS[:15]
                    or not set(CORE_FIELDS)<=set(fields)):
                raise ValueError('unexpected historical evidence columns: '+accession)
            extras = [f for f in fields if f not in CORE_FIELDS]
            if set(extras)-{'N_processed_like','N_nascent_like','N_context_insufficient'}:
                raise ValueError('unknown historical extension columns')
            pi,si=fields.index('protocol_support'),fields.index('species')
            extra_indices=[fields.index(f) for f in extras]
            protocols = {metadata['Protocol']}
            if metadata['Protocol'].startswith('ONT'): protocols.add('ONT')
            if metadata['Protocol']=='FLEP-seq2': protocols.add('FLEP-seq')
            for values in reader:
                row = next(cached,None); rows += 1
                if len(values)!=len(fields) or row is None: raise ValueError('historical row count mismatch: '+accession)
                index = row['intron_id']
                if values[0]!=accession or tuple(values[1:11]) != attributes[index]:
                    raise ValueError('historical reference/order mismatch: '+accession)
                if values[pi] not in protocols or species_code(values[si])!=reference['species']:
                    raise ValueError('historical protocol/species mismatch: '+accession)
                raw = values[11:15]
                if any(not v.isascii() or not v.isdecimal() for v in raw+[values[i] for i in extra_indices]):
                    raise ValueError('invalid historical counts: '+accession)
                counts = tuple(map(int,raw))
                if counts != tuple(row[k] for k in COUNT_NAMES) or max(counts)>MAX_INT:
                    raise ValueError('historical counts differ from cache: '+accession)
                if counts[3]!=counts[0]+counts[1] or counts[2]>counts[0]: raise ValueError('inconsistent historical counts')
                for j,value in enumerate(counts):
                    totals[j]+=value; sums[index*5+j]+=value
                sums[index*5+4]+=1
        if next(cached,None) is not None or rows != len(attributes)-1: raise ValueError('incomplete historical intron catalogue')
        if sha256(path,control)!=expected: raise ValueError('historical evidence changed during audit')
        samples[accession]={'sha256':expected,'version_id':version['id'],'version':version['version'],
                            'rows':rows,'counts':dict(zip(COUNT_NAMES,totals)),'extension_columns':extras}
        if number%10==0 or number==len(versions):
            print('{}: audited {}/{} samples'.format(reference['species'],number,len(versions)),file=sys.stderr,flush=True)
    total_rows=0
    for row in store.db.execute('SELECT * FROM totals ORDER BY intron_id'):
        total_rows+=1
        if row['intron_id']!=total_rows:raise ValueError('aggregate totals catalogue has missing or unexpected rows')
        index=row['intron_id']*5
        if list(sums[index:index+5]) != [row[k] for k in COUNT_NAMES+['n_samples']]:
            raise ValueError('aggregate totals differ from verified per-sample contributions')
    if total_rows != len(attributes)-1:raise ValueError('aggregate totals catalogue is incomplete')
    source_material=[]; source_root=args.source_project
    candidates=list((source_root/'PlantIRref/src/PlantIRref').glob('*.py'))
    historical=source_root if reference['species']=='ath' else source_root/reference['species']
    candidates+=list((historical/'25.intron_stat').glob('*.sh'))
    for n,path in enumerate(sorted(candidates)):
        name='audit_sources/{:02d}-{}'.format(n,path.name); data=path.read_bytes()
        atomic_write(folder/name,data)
        source_material.append({'path':name,'sha256':digest(data),'original_path':str(path),
                                'role':'available_source_material_not_execution_attestation'})
    parameters=json.loads(args.config.read_text())['parameters']
    report={'schema_version':1,'created_at':now(),'count_contract':CONTRACT,
            'reference':{'species':reference['species'],'reference_id':reference['reference_id'],
                         'annotation_id':reference['annotation_id'],'sha256':registration['reference_sha256']},
            'validation':{'file_hashes':'passed','fixed_reference_attributes':'passed','sample_contributions':'passed',
                          'aggregate_totals':'passed','complete_catalogue':'passed'},
            'historical_extraction':{'effective_parameters':None,'executed_software_identity':None,
                'statement':'Historical run reports are absent. Available source matches do not prove execution parameters.'},
            'native_policy':{'aggregation':'audited_heterogeneous_counts','parameters':parameters,
                             'software':software_policy(software_identity()),
                             'historical_extraction_equivalence_claimed':False},
            'source_material':source_material,'samples':samples}
    report['audit_id']=audit_identity(report); return report


def run(args):
    if args.publish and not args.apply: raise ValueError('--publish requires --apply')
    args.species=species_code(args.species)
    for key in ('samples','references_dir','evidence_dir','aggregate_dir','release_dir','config','source_project'):
        setattr(args,key,getattr(args,key).expanduser().resolve())
    ref,meta=reference_for(args,args.species)
    folder=args.evidence_dir/args.species/meta['assembly']['name']/meta['annotation']['name']
    root=args.aggregate_dir/args.species/meta['assembly']['name']/meta['annotation']['name']
    if not args.apply or args.dry_run:
        return {'status':'planned','species':args.species,'aggregate':str(root),'evidence':str(folder)}
    control=Control()
    with directory_lock(root),directory_lock(args.evidence_dir):
        store=AggregateStore(root/'aggregate.sqlite3',create=True)
        try:
            registration=json.loads((folder/'registration.json').read_text())
            files,_=verified_reference(SimpleNamespace(reference=ref,metadata=meta),control)
            reference={'species':args.species,'reference_id':meta['assembly']['name'],
                       'annotation_id':meta['annotation']['name'],'files':files}
            marker=root/'admission_pending.json'
            if marker.exists(): result=finish(store,root,folder,control)
            elif store.get('evidence_admission'):
                from evidence_admission import load_admission
                report=load_admission(folder,registration,reference)
                if report is None: raise ValueError('aggregate admission is not registered')
                result={'status':'validated_import','audit_id':report['audit_id'],'samples':len(report['samples']),
                        'report':str(folder/'admission.json'),'reused':True,'historical_parameters_verified':False}
            else:
                report=audit(store,folder,registration,reference,args,control)
                atomic_json(folder/'admission.json',report)
                receipt={'path':'admission.json','sha256':sha256(folder/'admission.json'),'audit_id':report['audit_id']}
                updated=dict(registration,admission=receipt)
                updated['extraction_provenance']=dict(registration.get('extraction_provenance',{}),
                    origin='validated_import',historical_parameters_verified=False)
                intent={'receipt':receipt,'registration_before_sha256':sha256(folder/'registration.json'),
                        'registration':updated}
                atomic_json(marker,intent); result=finish(store,root,folder,control)
        finally: store.close()
    if args.publish:
        import aggregate_evidence
        forwarded=['--species',args.species,'--reference-id',meta['assembly']['name'],
                   '--annotation-id',meta['annotation']['name'],'--publish']
        for name in ('samples','references_dir','evidence_dir','aggregate_dir','release_dir'):
            forwarded+=['--'+name.replace('_','-'),str(getattr(args,name))]
        result['publication']=aggregate_evidence.run(aggregate_evidence.parser().parse_args(forwarded))
    return result


def main():
    args=parser().parse_args()
    try: result=run(args); code=0
    except (OSError,ValueError,RuntimeError,KeyError,TypeError,sqlite3.Error) as exc:
        result={'status':'failed','error':str(exc)};code=1
    print(json.dumps(result,ensure_ascii=False,indent=2));return code


if __name__=='__main__': sys.exit(main())
