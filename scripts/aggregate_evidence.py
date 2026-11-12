#!/usr/bin/env python3
"""Incremental IR aggregation, revision and recoverable publication. No R dependency."""
import argparse
from contextlib import nullcontext
import csv
import json
import math
from pathlib import Path
import re
import signal
import sqlite3
import sys
from types import SimpleNamespace

from aggregate_store import AggregateStore, identity
from aggregate_release import publish
from align_reads import reference_for, verified_reference
from artifact_lifecycle import directory_lock
from download_reads import PROJECT, Control, child_path, sha256, species_code
from evidence_admission import load_admission
from evidence_readiness import build_reference,inspect_evidence,check_group,ReadinessCache


def parser():
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    for flag,default in [('samples','metadata/samples_info.tsv'),('references-dir','storage/references'),
                         ('evidence-dir','storage/evidence'),('aggregate-dir','storage/aggregates'),
                         ('release-dir','releases'),('config','configs/aggregation.json'),
                         ('classification-config','configs/classification.json'),('plantvelo-dir','plantvelo'),
                         ('resource-config','configs/prior_resources.json'),('prior-data-dir','plantvelo/data')]:
        p.add_argument('--'+flag,type=Path,default=PROJECT/default)
    p.add_argument('--species',required=True,help='one species code or scientific name')
    p.add_argument('--reference-id'); p.add_argument('--annotation-id')
    p.add_argument('--initialize',action='store_true',help='explicit first baseline import/publication')
    p.add_argument('--publish',action='store_true',help='explicitly aggregate, validate and publish; sample checkpoint is advisory')
    p.add_argument('--withdraw',action='append',default=[])
    p.add_argument('--restore',action='append',default=[])
    p.add_argument('--allow-legacy',action='store_true',help='explicitly accept unknown legacy extraction identity')
    p.add_argument('--allow-legacy-mixing',action='store_true',help='explicit audited decision to mix legacy with one native identity')
    p.add_argument('--revise-pending',action='store_true',help='with --publish, revise an applied but unvalidated/unpublished generation')
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--json',action='store_true')
    p.add_argument('--evidence-cache-db',type=Path,help='optional shared evidence validation cache')
    return p


def configuration(args):
    policy=json.loads(args.config.read_text())
    if set(policy)!={'schema_version','group_by','trigger'} or type(policy['schema_version']) is not int or policy['schema_version']!=2:
        raise ValueError('aggregation config requires schema 2 with manual publication')
    if policy['group_by']!=['species','reference_id','annotation_id']:
        raise ValueError('unsupported aggregation grouping')
    trigger=policy['trigger']
    if (set(trigger)!={'publication_mode','checkpoint_new_samples','max_wait_days'}
            or trigger['publication_mode']!='manual' or type(trigger['checkpoint_new_samples']) is not int
            or trigger['checkpoint_new_samples']<1 or trigger['max_wait_days'] is not None):
        raise ValueError('aggregation requires manual publication, a positive sample checkpoint and no time trigger')
    cfg=json.loads(args.classification_config.read_text())
    common={'min_ir_ratio','min_ir_specificity','min_retained','min_informative','min_spliced'}
    expected={'high_confidence_IR':common,'candidate_IR':common,
              'nonIR':{'max_ir_ratio','max_retained','min_informative','min_spliced'}}
    if (set(cfg)!={'schema_version','thresholds'} or type(cfg['schema_version']) is not int
            or cfg['schema_version']!=2 or not isinstance(cfg['thresholds'],dict)
            or set(cfg['thresholds'])!=set(expected)):
        raise ValueError('unsupported classification config')
    rules=cfg['thresholds']
    for label,keys in expected.items():
        if not isinstance(rules[label],dict) or set(rules[label])!=keys:
            raise ValueError('unsupported classification thresholds: '+label)
        for key,value in rules[label].items():
            if key in ('min_ir_ratio','min_ir_specificity','max_ir_ratio'):
                if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1:
                    raise ValueError('classification ratios must be finite fractions')
            elif type(value) is not int or value<0:
                raise ValueError('classification count thresholds must be nonnegative integers')
    return trigger['checkpoint_new_samples'],rules


def discover(args,meta,folder):
    selected,seen={},set()
    with args.samples.open(encoding='utf-8-sig',newline='') as handle:
        reader=csv.DictReader(handle,delimiter='\t')
        fields=reader.fieldnames or []
        if len(fields)!=len(set(fields)) or not {'Accession','Species','Protocol'}<=set(fields):
            raise ValueError('sample metadata requires unique Accession, Species and Protocol columns')
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ValueError('malformed sample metadata')
            accession=row['Accession'].strip()
            if not re.fullmatch(r'(?:SRR|ERR|DRR|CRR)[0-9]+',accession) or accession in seen:
                raise ValueError('invalid or duplicate sample accession')
            seen.add(accession)
            if species_code(row['Species'])==args.species:
                selected[accession]={k:v.strip() for k,v in row.items()}
    path=child_path(folder,'registration.json')
    if not path.exists(): return {}
    record=json.loads(path.read_text())
    if record['species_code']!=args.species or record['reference_id']!=meta['assembly']['name'] or record['annotation_id']!=meta['annotation']['name']:
        raise ValueError('evidence registration namespace mismatch')
    if record.get('coordinate_system')!='1-based inclusive' or record['validation']['reference_match']!='exact_introns_and_supporting_transcripts':
        raise ValueError('evidence registration is not reference-validated')
    if any(record['reference_sha256'][k]!=meta['files'][k]['sha256'] for k in ('genome','annotation')):
        raise ValueError('evidence registration reference hash mismatch')
    admission=load_admission(folder,record,{'species':args.species,'reference_id':meta['assembly']['name'],
        'annotation_id':meta['annotation']['name'],'files':{k:meta['files'][k] for k in ('genome','annotation')}})
    candidates={}
    for accession,row in selected.items():
        name=accession+'.intron_prior.tsv'
        digest=record['file_sha256'].get(name)
        if digest is None: continue  # Newly discovered samples may not be ready.
        report=record['file_sha256'].get(name+'.run.json')
        if any(not isinstance(v,str) or not re.fullmatch('[a-f0-9]{64}',v) for v in [digest]+([report] if report is not None else [])):
            raise ValueError('invalid registered evidence hash')
        path=child_path(folder,name)
        # Download locators do not change biological metadata or IR counts.
        # Adding the optional column must not reimport all historical evidence.
        scientific_metadata={k:v for k,v in row.items()if k!='RemotePaths'}
        version=identity({'sha256':digest,'report_sha256':report,'metadata':scientific_metadata})
        candidates[accession]={'accession':accession,'version':version,'path':str(path),'sha256':digest,
                               'report_sha256':report,'metadata':row}
        if report is None and admission is not None:
            admitted=admission['samples'].get(accession)
            if not admitted or admitted['sha256']!=digest: raise ValueError('historical source is not covered by admission')
            candidates[accession]['admission']=record['admission']
    return candidates


def make_recipe(rules,dry_run,store,plantvelo_dir):
    if dry_run:
        old=store.db.execute("SELECT recipe FROM batches WHERE status='published' ORDER BY generation DESC LIMIT 1").fetchone() if store else None
        implementation=json.loads(old[0])['implementation'] if old else {}
    else:
        implementation={p.name:sha256(p) for p in [Path(__file__),Path(__file__).with_name('aggregate_store.py'),Path(__file__).with_name('aggregate_release.py')]}
        implementation['evidence_admission.py']=sha256(Path(__file__).with_name('evidence_admission.py'))
        implementation['evidence_identity.py']=sha256(Path(__file__).with_name('evidence_identity.py'))
        implementation['evidence_identity_migrations.json']=sha256(PROJECT/'configs/evidence_identity_migrations.json')
        implementation['prior_format.py']=sha256(Path(__file__).with_name('prior_format.py'))
        for name in ('evidence_contract.py','evidence_readiness.py'):
            implementation[name]=sha256(Path(__file__).with_name(name))
    recipe={'rules':rules,'implementation':implementation}
    if store and store.get('evidence_admission'): recipe['evidence_admission']=store.get('evidence_admission')
    return recipe


def decision(store,plan,args,threshold,recipe):
    pending=store.pending() if store else None
    initialized=bool(store and store.get('published_pointer'))
    result={'status':'waiting','new_samples':len(plan['new']),'changed_samples':len(plan['changes']),
            'withdrawals':plan['withdrawals'],'read_samples':[],'threshold':threshold,'time_trigger':None,
            'publication_mode':'manual','checkpoint_reached':len(plan['new'])>=threshold}
    if pending:
        result['status']='pending_publication';return result
    if not initialized:
        result['status']='ready' if args.initialize else 'needs_initialization';return result
    last=store.db.execute("SELECT recipe FROM batches WHERE status='published' ORDER BY generation DESC LIMIT 1").fetchone()
    changed=bool(plan['changes'] or plan['withdrawals'] or not last or json.loads(last[0])!=recipe)
    if not changed: result['status']='up_to_date'
    elif args.publish: result['status']='ready'
    elif result['checkpoint_reached']: result['status']='checkpoint_reached'
    return result


def ensure_baseline_available(aggregate_root,aggregate_base=None):
    roots=[aggregate_root]+([aggregate_base] if aggregate_base is not None else [])
    for root in roots:
        marker=root/'baseline_reset.json'
        if marker.exists() or marker.is_symlink():
            raise ValueError('release baseline reset is pending; resume scripts/rebase_release_baseline.py before aggregating this namespace')
        if (root/'admission_pending.json').exists():
            raise ValueError('evidence admission is pending; resume scripts/validate_imported_evidence.py')


def verify_candidates(args,candidates,plan,store,folder,reference,meta):
    """Verify current source facts even when no publication is due."""
    context=build_reference(reference,meta,getattr(args,'_control',None))
    signatures={}
    if store:
        signatures={row['accession']:json.loads(row['signature'])for row in store.db.execute(
            'SELECT s.accession,v.signature FROM samples s JOIN versions v ON s.version_id=v.id WHERE s.active=1')}
    for accession in plan['withdrawals']:signatures.pop(accession,None)
    members=store.membership()if store else{}
    candidates=dict(candidates)
    if store:
        for row in store.db.execute('SELECT s.accession,v.source,v.metadata FROM samples s JOIN versions v ON s.version_id=v.id WHERE s.active=1'):
            if row['accession']not in candidates and row['accession']not in plan['withdrawals']:
                source=json.loads(row['source']);source['metadata']=json.loads(row['metadata'])
                candidates[row['accession']]=source
    cache=ReadinessCache(path=args.evidence_cache_db)
    try:
        for accession,candidate in candidates.items():
            if accession in plan['withdrawals']:continue
            if accession in members and not members[accession]['active']and accession not in args.restore:continue
            restoring=plan['changes'].get(accession,{})
            if restoring.get('restore_cached')and not Path(candidate['path']).exists():
                frozen=store.db.execute('SELECT signature FROM versions WHERE accession=? AND version=?',
                    (accession,candidate['version'])).fetchone()
                if frozen:
                    signatures[accession]=json.loads(frozen[0]);continue
            checked=inspect_evidence(folder,candidate['metadata'],context,cache=cache,
                control=getattr(args,'_control',None),allow_legacy=args.allow_legacy or bool(
                    store and accession in members and members[accession]['version']==candidate['version']
                    and signatures.get(accession,{}).get('kind')=='legacy'))
            if checked.state!='ready':raise ValueError((checked.reason or 'EVIDENCE_INVALID')+': '+checked.kind+' '+checked.message)
            if(checked.identity['sha256']!=candidate['sha256']or checked.identity['report_sha256']!=candidate['report_sha256']):
                raise ValueError('EVIDENCE_INVALID: evidence registration changed during aggregation preflight')
            signatures[accession]=checked.signature
    finally:cache.close()
    admitted=check_group(list(signatures.values()),allow_legacy_mixing=args.allow_legacy_mixing)
    if admitted['state']!='ready':raise ValueError(admitted['reason']+': '+admitted['message'])


def aggregate(args):
    for key in ('samples','references_dir','evidence_dir','aggregate_dir','release_dir','config','classification_config','plantvelo_dir','resource_config','prior_data_dir'):
        setattr(args,key,getattr(args,key).expanduser().resolve())
    args.species=species_code(args.species)
    if args.initialize and args.publish: raise ValueError('choose --initialize or --publish')
    if set(args.withdraw)&set(args.restore): raise ValueError('cannot withdraw and restore the same accession')
    if (args.withdraw or args.restore or args.revise_pending) and not args.publish:
        raise ValueError('withdrawal/restore/pending revision requires explicit --publish')
    threshold,rules=configuration(args)
    reference,meta=reference_for(args,args.species)
    namespace=Path(args.species)/meta['assembly']['name']/meta['annotation']['name']
    folder=child_path(args.evidence_dir,namespace)
    aggregate_root=child_path(args.aggregate_dir,namespace)
    release_root=child_path(args.release_dir,namespace)
    ensure_baseline_available(aggregate_root,args.aggregate_dir)
    db_path=child_path(aggregate_root,'aggregate.sqlite3')
    candidates=discover(args,meta,folder)
    store=AggregateStore(db_path) if db_path.exists() else None
    try:
        plan=store.plan(candidates,set(args.withdraw),set(args.restore)) if store else {'changes':candidates,'withdrawals':[],'new':list(candidates)}
        if not args.dry_run and not(store and store.pending()and (args.publish or args.initialize) and not args.revise_pending):
            with directory_lock(args.evidence_dir,shared=True)if args.evidence_dir.exists()else nullcontext():
                verify_candidates(args,candidates,plan,store,folder,reference,meta)
        recipe=make_recipe(rules,args.dry_run,store,args.plantvelo_dir)
        initial=decision(store,plan,args,threshold,recipe)
        initial.update(species=args.species,reference_id=meta['assembly']['name'],annotation_id=meta['annotation']['name'],dry_run=args.dry_run)
    finally:
        if store: store.close()
    if (args.dry_run or initial['status'] not in ('ready','pending_publication')
            or not (args.publish or args.initialize)):
        return initial
    if not candidates and args.initialize:
        raise ValueError('no registered evidence is ready for initialization')
    aggregate_root.mkdir(parents=True,exist_ok=True)
    control,handlers=Control(),{}
    args._control=control
    for signum in (signal.SIGINT,signal.SIGTERM):
        handlers[signum]=signal.signal(signum,control.signal)
    try:
        with directory_lock(aggregate_root),directory_lock(args.evidence_dir,shared=True):
            ensure_baseline_available(aggregate_root,args.aggregate_dir)
            store=AggregateStore(db_path,create=True)
            try:
                verified,_=verified_reference(SimpleNamespace(reference=reference,metadata=meta),control)
                ref_identity={'species':args.species,'reference_id':meta['assembly']['name'],
                              'annotation_id':meta['annotation']['name'],'files':verified}
                if store.get('reference') is not None and store.get('reference')!=ref_identity:
                    raise ValueError('aggregate reference identity changed; use a new reference namespace')
                pending=store.pending()
                if pending and args.revise_pending:
                    if pending['status']!='aggregated':
                        raise ValueError('finish pending publication before revising its generation')
                elif pending:
                    recovered=publish(store,pending,release_root,recipe,args.plantvelo_dir,control)
                    candidates=discover(args,meta,folder)
                    recovered['pending_new_samples']=len(store.plan(candidates,set(),set())['new'])
                    queued=store.plan(candidates,set(args.withdraw),set(args.restore))
                    published_recipe=json.loads(store.db.execute('SELECT recipe FROM batches WHERE id=?',(pending['id'],)).fetchone()[0])
                    recovered['withdrawals']=[r[0] for r in store.db.execute(
                        "SELECT accession FROM operations WHERE batch_id=? AND operation='withdraw' ORDER BY accession",(pending['id'],))]
                    recovered['deferred_changes']={'new_samples':len(queued['new']),'changed_samples':len(queued['changes']),
                        'withdrawals':queued['withdrawals'],'restores':sorted(set(args.restore)),
                        'rules_changed':published_recipe['rules']!=recipe['rules'],
                        'implementation_changed':published_recipe['implementation']!=recipe['implementation']}
                    return dict(initial,**recovered,dry_run=False,recovered_batch=True)
                elif args.initialize and store.get('published_pointer'):
                    raise ValueError('baseline already initialized; use --publish for explicit revisions')
                candidates=discover(args,meta,folder)
                plan=store.plan(candidates,set(args.withdraw),set(args.restore))
                verify_candidates(args,candidates,plan,store,folder,reference,meta)
                current=decision(store,plan,args,threshold,recipe)
                if pending and args.revise_pending:
                    if not (plan['changes'] or plan['withdrawals']):
                        raise ValueError('pending revision requires an explicit evidence or membership change')
                    current['status']='ready'
                if current['status']=='ready':
                    if pending and args.revise_pending:
                        store.supersede=pending['id']
                    batch=store.apply(plan,ref_identity,recipe,args.allow_legacy,args.allow_legacy_mixing,control)
                    current=publish(store,batch,release_root,recipe,args.plantvelo_dir,control)
                current['pending_new_samples']=len(store.plan(candidates,set(args.withdraw),set(args.restore))['new'])
                return dict(initial,**current,dry_run=False)
            finally:
                store.close()
    finally:
        for signum,handler in handlers.items(): signal.signal(signum,handler)


def run(args):
    return aggregate(args)


def main():
    args=parser().parse_args()
    if any(token.split('=')[0]in {'--resource-config','--prior-data-dir'}for token in sys.argv[1:]):
        print(json.dumps({'status':'failed','error':'delivery options moved to scripts/run_delivery.sh; aggregation only publishes science'}));return 2
    try:
        value=run(args);code=1 if any(value.get(k,{}).get('status')=='failed' for k in ('resource_sync','website_sync')) else 0
    except (OSError,ValueError,RuntimeError,KeyError,TypeError,sqlite3.Error,ImportError) as exc:
        value={'status':'failed','error':str(exc)};code=1
    if getattr(args,'_control',None) and args._control.signum:
        value['status']='interrupted';code=128+args._control.signum
    if args.json: print(json.dumps(value,ensure_ascii=False,indent=2))
    else: print(json.dumps(value,ensure_ascii=False))
    return code


if __name__=='__main__':
    sys.exit(main())
