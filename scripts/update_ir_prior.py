#!/usr/bin/env python3
"""Recoverable serial discovery, download, alignment, extraction and batched IR publication."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import uuid
from types import SimpleNamespace

from aggregate_evidence import configuration as aggregation_configuration
from align_reads import reference_for
from artifact_lifecycle import directory_lock
from discovery_io import atomic_write,digest
from download_reads import PROJECT,sha256,species_code,METHODS,nonnegative_float,remote_paths
from qtrans_download import DownloadBlocked, QtransDownload
from evidence_readiness import build_reference,request_for,inspect_evidence,check_group,ReadinessCache,EvidenceDecision
from sample_catalog import read_catalog,table_bytes
from update_state import UpdateState
from delivery_store import capture_plan
from deliver_releases import run_job as run_delivery_job
from task_contract import (build_update_result, error_record, project_aggregations, COUNTERS, ContractError)

STAGES={'discovery':'run_discovery.sh','download':'run_download.sh','alignment':'run_alignment.sh',
        'evidence':'run_evidence.sh','aggregation':'run_aggregation.sh'}


class StageFailure(RuntimeError):
    def __init__(self,message,result):
        super().__init__(message);self.result=result


def nonnegative(value):
    n=int(value)
    if n<0:raise argparse.ArgumentTypeError('must be nonnegative')
    return n


def parser():
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('--species',action='append',required=True);p.add_argument('--accession',action='append',default=[])
    p.add_argument('--project',action='append',default=[]);p.add_argument('--reference-id');p.add_argument('--annotation-id')
    for name,default in [('samples','metadata/samples_info.tsv'),('references-dir','storage/references'),
            ('evidence-dir','storage/evidence'),('raw-dir','storage/raw'),('alignment-dir','storage/alignments'),
            ('aggregate-dir','storage/aggregates'),('release-dir','releases'),('state-dir','state'),('log-dir','logs'),
            ('discovery-dir','metadata/discovery'),('alignment-config','configs/alignment.json'),
            ('evidence-config','configs/evidence.json'),('aggregation-config','configs/aggregation.json'),
            ('classification-config','configs/classification.json'),('resource-config','configs/prior_resources.json'),
            ('prior-data-dir','plantvelo/data')]:
        p.add_argument('--'+name,type=Path,default=PROJECT/default)
    p.add_argument('--batch-id',help='resume this cycle; omitted resumes matching unfinished cycle automatically')
    p.add_argument('--skip-discovery',action='store_true',help='process the current sample catalog without HTTP discovery')
    p.add_argument('--max-samples',type=nonnegative,default=10,help='maximum samples attempted serially per cycle; 0 means all')
    p.add_argument('--discovery-limit',type=nonnegative,default=100)
    p.add_argument('--discovery-source',choices=('auto','ncbi','ena','ddbj','ngdc'),default='auto',
                   help='original metadata archive; auto routes by accession/project')
    p.add_argument('--download-backend',choices=('auto','kingfisher','qtransc','ena-https'),default='auto',
                   help='download transport; may change when resuming')
    p.add_argument('--download-methods',nargs='+',choices=METHODS,
                   help='explicit Kingfisher transport preference; may change when resuming')
    p.add_argument('--download-timeout',type=nonnegative_float,default=0,
                   help='seconds per download backend attempt; 0 disables')
    p.add_argument('--download-retries',type=nonnegative,default=2)
    p.add_argument('--initialize',action='store_true',help='explicitly initialize previously unpublished reference groups')
    p.add_argument('--publish-only',action='store_true',help='explicitly publish existing evidence and deliver; no discovery or sample processing')
    p.add_argument('--dry-run',action='store_true',help='read-only metadata preview, without network or state writes')
    p.add_argument('--verify-evidence',action='store_true',help='with --dry-run, fully verify evidence without application writes')
    p.add_argument('--json',action='store_true');return p


def invoke(stage,arguments,log):
    log.parent.mkdir(parents=True,exist_ok=True)
    command=['bash',str(PROJECT/'scripts'/STAGES[stage])]+arguments+['--json']
    with log.open('a') as errors:
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=errors,text=True,start_new_session=True)
        try:output,_=process.communicate()
        except BaseException:
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:process.wait(timeout=30)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
            raise
        errors.write(output+'\n');errors.flush()
    try:result=json.loads(output)
    except ValueError as exc:raise RuntimeError('stage {} returned invalid JSON; see {}'.format(stage,log)) from exc
    if not isinstance(result,dict):raise RuntimeError('stage {} returned non-object JSON; see {}'.format(stage,log))
    if process.returncode:
        raise StageFailure('{} failed ({}): {}; log {}'.format(stage,process.returncode,result.get('error',result),log),result)
    return result


def publication_pending(args,namespace,accession):
    path=args.state_dir/'evidence.sqlite3'
    if not path.exists():return False
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        row=db.execute('SELECT status FROM evidence WHERE accession=? AND namespace=?',
            (accession,str(namespace))).fetchone()
        return bool(row and row[0]=='publishing')
    finally:db.close()


def group_preflight(args,rows,groups,cache=None):
    """Project the same active cohort as aggregation before spending sample budget."""
    from aggregate_store import AggregateStore
    from aggregate_evidence import ensure_baseline_available
    request=request_for(args.evidence_config)
    contexts,decisions,blocked,inactive={},{},{},set()
    for code,(reference_id,annotation_id) in sorted(groups.items()):
        folder,meta=reference_for(args,code)
        reference=build_reference(folder,meta)
        namespace=args.evidence_dir/code/reference_id/annotation_id
        contexts[code]=(reference,request)
        current={};retired=set();cached_samples={};cached_sources={}
        try:
            aggregate_root=args.aggregate_dir/code/reference_id/annotation_id
            ensure_baseline_available(aggregate_root,args.aggregate_dir)
            database=aggregate_root/'aggregate.sqlite3'
            if database.exists():
                store=AggregateStore(database)
                try:
                    for member in store.db.execute('SELECT s.accession,s.active,v.signature,v.metadata,v.source FROM samples s JOIN versions v ON v.id=s.version_id'):
                        if member['active']:
                            current[member['accession']]=json.loads(member['signature'])
                            cached_samples[member['accession']]=json.loads(member['metadata'])
                            cached_sources[member['accession']]=json.loads(member['source'])
                        else:retired.add(member['accession'])
                finally:store.close()
            proposed=False;pending=False
            group_samples={sample['Accession']:sample for sample in rows if species_code(sample['Species'])==code}
            old_only=set(cached_samples)-set(group_samples)
            group_samples=dict(cached_samples,**group_samples)
            for sample in group_samples.values():
                accession=sample['Accession']
                if accession in retired:
                    inactive.add(accession);continue
                if publication_pending(args,namespace,accession):
                    pending=True
                    blocked.setdefault(code,[])
                    decisions[accession]=EvidenceDecision('pending','EVIDENCE_RECOVERY_REQUIRED','unknown',
                        differences=('recover the saved evidence publication before checking its replacement',))
                    if args.dry_run or accession not in {row['Accession']for row in rows} or(args.accession and accession not in args.accession):
                        blocked.setdefault(code,[]).append(error_record('EVIDENCE_RECOVERY_REQUIRED','preflight',
                            'saved evidence publication requires recovery for '+accession,accession,retryable=False))
                    continue
                decision=inspect_evidence(namespace,sample,reference,request=request,cache=cache)
                decisions[accession]=decision
                if decision.state=='ready':
                    if accession in old_only and any(decision.identity.get(key)!=cached_sources[accession].get(key)for key in ('sha256','report_sha256')):
                        blocked.setdefault(code,[]).append(error_record('EVIDENCE_INVALID','preflight',
                            'active evidence outside the catalog differs from its stored contribution',accession,retryable=False))
                    current[accession]=decision.signature
                elif decision.state=='missing' and accession not in current:
                    if not args.accession or accession in args.accession:proposed=True
                else:
                    # Only a saved publication intent can explain a partially replaced pair.
                    recovery=publication_pending(args,namespace,accession)
                    if recovery:
                        proposed=True;continue
                    reason=decision.reason or 'EVIDENCE_INVALID'
                    blocked.setdefault(code,[]).append(error_record(reason,'preflight',decision.message,accession,retryable=False))
            if pending:continue
            admission=check_group(list(current.values()),proposed_native=(dict(kind='native',**request)if proposed else None))
            if admission['state']!='ready':
                blocked.setdefault(code,[]).append(error_record('EVIDENCE_COHORT_INCOMPATIBLE','preflight',admission['message'],retryable=False))
        except (OSError,ValueError,RuntimeError,KeyError,TypeError,sqlite3.Error)as exc:
            blocked.setdefault(code,[]).append(error_record('EVIDENCE_RECOVERY_REQUIRED'if 'pending'in str(exc)else'EVIDENCE_INVALID',
                'preflight',str(exc),retryable=False))
    return contexts,decisions,blocked,inactive


def stage_arguments(stage,args,snapshot,row,group):
    code,reference_id,annotation_id=group
    result=['--samples',str(snapshot),'--species',code,'--accession',row['Accession'],
            '--evidence-dir',str(args.evidence_dir)]
    db={'download':'downloads','alignment':'alignments','evidence':'evidence'}[stage]
    result+=['--state-db',str(args.state_dir/(db+'.sqlite3')),'--log-dir',str(args.log_dir/stage)]
    result+=['--evidence-cache-db',str(args.state_dir/'updates.sqlite3')]
    if stage in {'download','alignment'}:result+=['--evidence-config',str(args.evidence_config)]
    if stage in {'download','alignment'}:result+=['--raw-dir',str(args.raw_dir),'--jobs','1']
    if stage=='download':
        result+=['--references-dir',str(args.references_dir),'--reference-id',reference_id,'--annotation-id',annotation_id]
        result+=['--backend',args.download_backend]
        result+=['--timeout',str(args.download_timeout),'--retries',str(args.download_retries)]
        if args.download_methods:result+=['--methods']+args.download_methods
    if stage in {'alignment','evidence'}:
        result+=['--references-dir',str(args.references_dir),'--reference-id',reference_id,'--annotation-id',annotation_id,
                 '--alignment-dir',str(args.alignment_dir)]
    if stage=='alignment':
        result+=['--config',str(args.alignment_config),'--download-state-db',str(args.state_dir/'downloads.sqlite3')]
    if stage=='evidence':
        result+=['--config',str(args.evidence_config),'--alignment-state-db',str(args.state_dir/'alignments.sqlite3')]
    return result


def result_for(args, outcome):
    context=args._context
    payload=context.get('_payload', {})
    groups,delivery=project_aggregations(context.get('_groups', {}),payload.get('aggregations', {}))
    public=dict(context, aggregations=payload.get('aggregations', {}),snapshot=payload.get('snapshot'))
    return build_update_result(public,outcome,context.get('summary'),groups,delivery,context.get('_errors', []))


def exception_result(args, exc):
    context=args._context
    interrupted=isinstance(exc,KeyboardInterrupt)
    outcome='interrupted' if interrupted else 'failed'
    summary=context.get('summary')
    if summary is not None:
        unsettled=summary['attempted']-summary['succeeded']-summary['failed']-summary['interrupted']
        summary['interrupted' if interrupted else 'failed']+=unsettled
        summary['unvisited']=summary['selected']-summary['ready_before']-summary['attempted']-summary['deferred']
    message='resume with the same selection and batch-id' if interrupted else str(exc)
    context.setdefault('_errors', []).append(error_record(
        'UPDATE_INTERRUPTED' if interrupted else getattr(exc,'code','UPDATE_FAILED'),context['stage'],message,
        context.get('_accession')))
    try:
        return result_for(args,outcome)
    except Exception as result_error:
        # A damaged old checkpoint must not prevent a bounded failure receipt.
        context['_errors'].append(error_record('INVALID_RESULT',context['stage'],str(result_error)))
        return build_update_result(context,outcome,context.get('summary'),[],
                                   dict(resources=[],website=None),context['_errors'])


def qtrans_preflight(client, raw_root):
    live = True  # Until process ownership is known, protect the entire raw root.
    try:
        live = bool(client.live_processes())
        unfinished = (client.unfinished_live_tasks(raw_root) if live
                      else client.unfinished_saved_tasks(raw_root))
        return live, unfinished, None
    except (OSError, ValueError, DownloadBlocked) as exc:
        return live, set(), str(exc)


def qtrans_recovery_sample(args, rows, unfinished):
    if not unfinished or args.download_backend not in {'auto', 'qtransc'}:
        return None
    for row in rows:
        accession = row['Accession']
        if not accession.startswith('CRR'):
            continue
        work = args.raw_dir / '.partial' / species_code(row['Species']) / accession
        try:
            paths = remote_paths(row.get('RemotePaths', ''))
        except ValueError:
            continue
        # A cold start can resume every saved task. All unfinished transfers
        # must belong to this one authorized sample before starting its service.
        if unfinished <= {(source, str(work / Path(source).name)) for source in paths}:
            return accession
    return None


def execute(args,state,batch,request,groups,invoke_fn):
    previous=state.cycle(batch)
    if previous and previous['request']!=request:raise ContractError('CONFIG_CHANGED','update batch request/configuration changed; use a new --batch-id')
    payload=previous['payload'] if previous else {'discovery_done':args.skip_discovery or args.publish_only,'tasks':{},'aggregations':{}}
    if previous and previous['status']=='complete':
        path=Path(payload['snapshot'])
        if digest(path.read_bytes())!=payload['snapshot_sha256']:raise ValueError('update snapshot hash mismatch')
        return payload['result']
    args._context['_payload']=payload
    cycle=args.state_dir/'updates'/batch;cycle.mkdir(parents=True,exist_ok=True)
    errors=args._context['_errors']
    def save(status='running'):
        payload['invocation']={k:args._context.get(k) for k in ('invocation_id','stage','summary')}
        state.save(batch,request,status,payload)
    save()
    if not payload['discovery_done']:
        args._context['stage']='discovery';save()
        try:
            values=['--samples',str(args.samples),'--discovery-dir',str(args.discovery_dir),
                    '--source',args.discovery_source,
                    '--config',str(args.alignment_config),'--batch-id',batch+'.discovery',
                    '--max-records',str(args.discovery_limit),'--apply']
            for code in sorted(groups):values+=['--species',code]
            for a in args.accession:values+=['--accession',a]
            for p in args.project:values+=['--project',p]
            payload['discovery']=invoke_fn('discovery',values,args.log_dir/'update'/batch/'discovery.log')
            payload['discovery_done']=True;save()
        except (OSError,ValueError,RuntimeError) as exc:
            errors.append(error_record('STAGE_FAILED','discovery',str(exc)));payload['discovery_error']=str(exc);save()
    args._context['stage']='snapshot';save()
    snapshot=cycle/'samples.snapshot.tsv'
    if 'snapshot' not in payload:
        with directory_lock(args.samples.parent):
            fields,rows=read_catalog(args.samples)
            rows=[r for r in rows if species_code(r['Species']) in groups]
            missing=set(args.accession)-{r['Accession'] for r in rows}
            if missing:raise ValueError('requested samples missing from catalog: '+', '.join(sorted(missing)))
            data=table_bytes(fields,rows);atomic_write(snapshot,data)
        payload.update(snapshot=str(snapshot),snapshot_sha256=digest(data));save()
    else:
        if digest(snapshot.read_bytes())!=payload['snapshot_sha256']:raise ValueError('update snapshot hash mismatch')
        _,rows=read_catalog(snapshot)
    summary=dict(scope='invocation', **dict.fromkeys(COUNTERS,0))
    task_rows=[] if args.publish_only else [r for r in rows if not args.accession or r['Accession'] in args.accession]
    summary['selected']=len(task_rows);args._context['summary']=summary
    args._context['stage']='preflight'
    cache=ReadinessCache(connection=state.db)
    if args.publish_only:
        # The aggregator validates the complete registered cohort and recovers
        # frozen publications itself; publication never starts sample work.
        contexts,decisions,blocked,inactive={},{},{},set()
    else:
        contexts,decisions,blocked,inactive=group_preflight(args,rows,groups,cache)
    for code,problems in blocked.items():
        errors.extend(dict(problem,species=code)for problem in problems)
        payload['aggregations'][code]={'status':'failed'}
    qtrans = QtransDownload('', args.raw_dir, [], 0, threading.Event())
    qtrans_live, unfinished_qtrans, qtrans_error = (False,set(),None) if args.publish_only else qtrans_preflight(qtrans, args.raw_dir)
    recovery = qtrans_recovery_sample(args, task_rows, unfinished_qtrans)
    qtrans_reported = False
    task_rows.sort(key=lambda r:(not publication_pending(args,args.evidence_dir.joinpath(species_code(r['Species']),*groups[species_code(r['Species'])]),r['Accession']),
        r['Accession'] != recovery,
        state.priority(args.evidence_dir.joinpath(species_code(r['Species']),*groups[species_code(r['Species'])]),r['Accession'])))
    download_blocked=False
    for row in task_rows:
        code=species_code(row['Species']);group=(code,*groups[code]);namespace=args.evidence_dir.joinpath(*group)
        accession=row['Accession'];args._context['_accession']=accession
        publishing=publication_pending(args,namespace,accession)
        if(code in blocked and not publishing)or accession in inactive:
            payload['tasks'][accession]={'status':'blocked'if code in blocked else'inactive'};continue
        cleanup='evidence'if publishing else state.cleanup(namespace,accession)
        reference,policy=contexts[code]
        decision=inspect_evidence(namespace,row,reference,request=policy,cache=cache)
        if decision.state not in {'ready','missing'}and not(cleanup=='evidence'and publication_pending(args,namespace,accession)):
            errors.append(error_record(decision.reason or 'EVIDENCE_INVALID','preflight',decision.message,accession,retryable=False))
            payload['tasks'][accession]={'status':'blocked'}
            blocked.setdefault(code,[]);payload['aggregations'][code]={'status':'failed'}
            continue
        if decision.state=='ready' and not cleanup:
            summary['ready_before']+=1;payload['tasks'][accession]={'status':'ready_evidence'};continue
        if download_blocked and not cleanup:
            summary['deferred']+=1;payload['tasks'][accession]={'status':'deferred','stage':'download'};continue
        if args.max_samples and summary['attempted']>=args.max_samples:summary['deferred']+=1;continue
        if (not cleanup and accession != recovery and (unfinished_qtrans or qtrans_error)
                and (qtrans_live or accession.startswith('CRR'))):
            summary['deferred'] += 1
            payload['tasks'][accession] = {'status': 'deferred', 'stage': 'download'}
            if not qtrans_reported:
                labels = sorted({Path(dest).parent.name for _, dest in unfinished_qtrans
                                 if re.fullmatch(r'CRR\d+', Path(dest).parent.name)})
                message = qtrans_error or 'Recover unfinished Qtrans tasks before starting affected downloads'
                if labels:
                    message += ': ' + ', '.join(labels)
                errors.append(error_record('STAGE_FAILED', 'download', message,
                                           labels[0] if len(labels) == 1 else None))
                qtrans_reported = True
            continue
        summary['attempted']+=1
        args._context['_accession']=accession
        state.attempted(namespace,accession)
        stages=('download','alignment','evidence')
        for stage in stages[stages.index(cleanup):] if cleanup else stages:
            try:
                args._context['stage']=stage
                payload['tasks'][accession]={'status':'running','stage':stage};save()
                if stage in {'alignment','evidence'}:
                    state.set_cleanup(namespace,accession,stage)
                arguments=stage_arguments(stage,args,snapshot,row,group)
                if cleanup=='alignment' and stage=='alignment':arguments+=['--ignore-evidence']
                result=invoke_fn(stage,arguments,
                                 args.log_dir/'update'/batch/(accession+'.'+stage+'.log'))
                if result.get('counts',{}).get('failed') or result.get('counts',{}).get('cleanup_failed'):
                    raise StageFailure(stage+' reported failed items',result)
                if stage == 'download' and accession == recovery:
                    qtrans_live, unfinished_qtrans, qtrans_error = qtrans_preflight(qtrans, args.raw_dir)
                    recovery = None
                if stage in {'alignment','evidence'}:state.set_cleanup(namespace,accession,None)
                payload['tasks'][accession]={'status':'stage_complete','stage':stage};save()
            except (OSError,ValueError,RuntimeError) as exc:
                if stage == 'download' and accession == recovery:
                    qtrans_reported = True
                if stage=='download' and getattr(exc,'result',{}).get('download_blocked') is True:
                    download_blocked=True
                if getattr(exc,'result',{}).get('counts',{}).get('cleanup_failed'):
                    state.set_cleanup(namespace,accession,stage)
                payload['tasks'][accession]={'status':'failed','stage':stage,'error':str(exc)}
                cleanup_failed=bool(getattr(exc,'result',{}).get('counts',{}).get('cleanup_failed'))
                errors.append(error_record('CLEANUP_FAILED' if cleanup_failed else 'STAGE_FAILED',
                    'cleanup' if cleanup_failed else stage,str(exc),accession))
                summary['failed']+=1;save();break
        else:
            decision=inspect_evidence(namespace,row,reference,request=policy,cache=cache)
            if decision.state!='ready':
                error={'accession':accession,'stage':'evidence','error':decision.message}
                errors.append(error_record(decision.reason or 'STAGE_FAILED','evidence',error['error'],accession))
                summary['failed']+=1;payload['tasks'][accession]=dict(error,status='failed')
            else:
                summary['succeeded']+=1;payload['tasks'][accession]={'status':'complete'}
            save()
        if publishing:
            fresh,_,problems,_=group_preflight(args,rows,{code:groups[code]},cache)
            contexts.update(fresh)
            if problems.get(code):
                errors.extend(dict(problem,species=code)for problem in problems[code])
                blocked[code]=problems[code];payload['aggregations'][code]={'status':'failed'}
            elif code not in problems and not blocked.get(code):
                blocked.pop(code,None);payload['aggregations'].pop(code,None)
            if publication_pending(args,namespace,accession):
                blocked.setdefault(code,[]);payload['aggregations'][code]={'status':'failed'}
    args._context['_accession']=None
    for code,problems in blocked.items():
        if not problems:
            errors.append(dict(error_record('EVIDENCE_RECOVERY_REQUIRED','preflight',
                'saved evidence publications remain pending; recover them before aggregation',retryable=True),species=code))
    summary['unvisited']=summary['selected']-summary['ready_before']-summary['attempted']-summary['deferred']
    args._context['stage']='aggregation';save()
    # Always use the complete frozen catalog, never just this cycle's new run list.
    for code,(ref_id,annotation_id) in sorted(groups.items()):
        if code in blocked:continue
        if not args.publish_only and not any(species_code(r['Species'])==code for r in rows):continue
        values=['--samples',str(snapshot),'--species',code,'--reference-id',ref_id,'--annotation-id',annotation_id,
                '--references-dir',str(args.references_dir),'--evidence-dir',str(args.evidence_dir),
                '--aggregate-dir',str(args.aggregate_dir),'--release-dir',str(args.release_dir),
                '--config',str(args.aggregation_config),'--classification-config',str(args.classification_config),
                '--evidence-cache-db',str(args.state_dir/'updates.sqlite3')]
        database=args.aggregate_dir/code/ref_id/annotation_id/'aggregate.sqlite3'
        if args.publish_only:values+=['--publish']
        if args.initialize and not database.exists():values+=['--initialize']
        try:
            value=invoke_fn('aggregation',values,args.log_dir/'update'/batch/(code+'.aggregation.log'))
            if value.get('status') in {'failed','needs_initialization'}:raise RuntimeError(str(value))
            project_aggregations({code:(ref_id,annotation_id)},{code:value})
            payload['aggregations'][code]=value
        except (OSError,ValueError,RuntimeError) as exc:
            errors.append(dict(error_record('STAGE_FAILED','aggregation',str(exc)),species=code))
        save()
    args._context['stage']='finished'
    result=result_for(args,'partial' if errors else 'success')
    if not (args.publish_only or args.initialize):
        result.update(science_outcome=result['outcome'],result_finalized=True,
                      delivery=dict(resources=[],website=None))
        payload.update(science_done=True,science_outcome=result['outcome'],result=result)
        state.save(batch,request,result['status'],payload)
        return result
    targets=['resources']
    if args.release_dir==(PROJECT/'releases').resolve() and (PROJECT/'website/index.html').is_file():targets.append('website')
    plan=capture_plan(sorted(groups),args.release_dir,args.references_dir,args.prior_data_dir,
                      PROJECT/'website',args.resource_config,targets=targets,groups=groups,project_root=PROJECT)
    result=state.finish_science(batch,request,result['status'],payload,result,plan)
    args._context['_fresh_science']=True
    return result


def run_science(args,invoke_fn=None):
    invoke_fn=invoke_fn or invoke
    supplied=args.batch_id if args.batch_id and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',args.batch_id) else None
    args._context=dict(batch_id=supplied,invocation_id='inv-'+uuid.uuid4().hex,stage='preflight',_errors=[])
    if args.verify_evidence and not args.dry_run:raise ValueError('--verify-evidence requires --dry-run')
    if args.publish_only and (args.accession or args.project or args.initialize):
        raise ValueError('--publish-only uses complete reference groups and cannot combine with --accession, --project or --initialize')
    for action in parser()._actions:
        if action.type is Path:setattr(args,action.dest,getattr(args,action.dest).expanduser().resolve())
    checkpoint,_=aggregation_configuration(SimpleNamespace(config=args.aggregation_config,
        classification_config=args.classification_config))
    codes=sorted({species_code(s) for s in args.species});groups={};refs={}
    for code in codes:
        folder,meta=reference_for(args,code);groups[code]=(meta['assembly']['name'],meta['annotation']['name'])
        if not args.dry_run:refs[code]=sha256(folder/'reference.json')
    args._context['_groups']=groups
    if args.dry_run:
        _,rows=read_catalog(args.samples)
        selected=[r for r in rows if species_code(r['Species']) in groups and (not args.accession or r['Accession'] in args.accession)]
        value={'schema_version':2,'kind':'update_plan','status':'planned','species':codes,'selected_samples':len(selected),
                'discovery':not (args.skip_discovery or args.publish_only),'serial':True,
                'max_samples':0 if args.publish_only else args.max_samples,'publish_only':args.publish_only,
                'publication_mode':'manual','checkpoint_new_samples':checkpoint,
                'aggregation':f'manual publication per species/reference/annotation; {checkpoint} new valid samples is a review checkpoint; no automatic publication',
                'evidence_verification':'unchecked'}
        if args.verify_evidence:
            contexts,decisions,blocked,inactive=group_preflight(args,rows,groups,ReadinessCache(readonly=True))
            value.update(evidence_verification='verified',evidence_counts={key:sum(d.state==key for d in decisions.values())
                for key in ('ready','missing','incompatible','invalid','pending','unchecked')},
                evidence_errors=[dict(e,species=code)for code,items in blocked.items()for e in items][:50])
        return value
    if args.batch_id and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',args.batch_id):raise ValueError('invalid update batch-id')
    request={a.dest:str(getattr(args,a.dest)) if a.type is Path else getattr(args,a.dest) for a in parser()._actions
             if a.dest not in {'help','batch_id','dry_run','verify_evidence','json','download_backend','download_methods','download_timeout','download_retries'}}
    if not args.publish_only:request.pop('publish_only',None)
    for name,default in [('resource_config','configs/prior_resources.json'),('prior_data_dir','plantvelo/data')]:
        if getattr(args,name)==PROJECT/default:request.pop(name,None)
    request['reference_sha256']=refs
    # Completed frozen cycles keep their historical discovery receipt. The new
    # automatic origin policy applies to fresh retrieval, without rewriting them.
    if args.discovery_source=='auto':request.pop('discovery_source',None)
    request['config_sha256']={name:sha256(getattr(args,name)) for name in
        ('alignment_config','evidence_config','aggregation_config','classification_config')}
    if 'resource_config' in request:request['config_sha256']['resource_config']=sha256(args.resource_config)
    args.state_dir.mkdir(parents=True,exist_ok=True)
    with directory_lock(args.state_dir):
        state=UpdateState(args.state_dir/'updates.sqlite3')
        try:
            pending=None if args.batch_id else state.pending(request)
            if pending:
                args._context.update(batch_id=pending,stage='snapshot')
                old=state.cycle(pending);payload=old['payload']
                if payload.get('snapshot'):
                    if digest(Path(payload['snapshot']).read_bytes())!=payload['snapshot_sha256']:
                        raise ValueError('update snapshot hash mismatch')
                    with directory_lock(args.samples.parent):
                        fields,rows=read_catalog(args.samples)
                        current=table_bytes(fields,[r for r in rows if species_code(r['Species']) in groups])
                    if digest(current)!=payload['snapshot_sha256']:
                        state.save(pending,request,'superseded',payload);pending=None
            batch=args.batch_id or pending or datetime.now(timezone.utc).strftime('u%Y%m%dT%H%M%SZ-')+uuid.uuid4().hex[:8]
            args._context['batch_id']=batch
            try:
                return execute(args,state,batch,request,groups,invoke_fn)
            except (Exception,KeyboardInterrupt) as exc:
                value=exception_result(args,exc)
                payload=args._context.get('_payload')
                if payload is not None:
                    payload['result']=value
                    payload['invocation']={k:args._context.get(k) for k in ('invocation_id','stage','summary')}
                    try:state.save(batch,request,value['status'],payload)
                    except (OSError,sqlite3.Error) as save_error:
                        args._context['_errors'].append(error_record('CHECKPOINT_WRITE_FAILED',args._context['stage'],str(save_error)))
                        value['errors']=list(args._context['_errors'])
                args._terminal_result=value
                raise
        finally:state.close()


def finish_delivery(args,value,delivery_fn):
    science_outcome=value['science_outcome']
    args._context['stage']='delivery'
    interrupted=None
    try:
        result=delivery_fn(args.state_dir/'updates.sqlite3',value['delivery_job_id'])
        from task_contract import validate_delivery_result
        validate_delivery_result(result,value['delivery_job_id'])
    except (Exception,KeyboardInterrupt)as exc:
        interrupted=exc if isinstance(exc,KeyboardInterrupt)else None
        result=dict(outcome='interrupted'if interrupted else'partial',delivery=dict(resources=[],website=None),
            errors=[error_record('UPDATE_INTERRUPTED'if interrupted else getattr(exc,'code','DELIVERY_FAILED'),'delivery',str(exc)or'delivery interrupted')])
    value['delivery']=result['delivery']
    value['errors']+=result['errors']
    outcome='interrupted'if interrupted else'partial'if science_outcome!='success'or result['outcome']!='success'else'success'
    value.update(outcome=outcome,status='complete'if outcome=='success'else outcome,result_finalized=True)
    for resource in value['delivery']['resources']:
        aggregate=value['aggregations'].get(resource['species'])
        if aggregate is not None:
            raw=result.get('legacy_sync',{}).get('resources',{}).get(resource['species'],{})
            aggregate['resource_sync']=dict(resource,**raw)
    if value['delivery']['website']is not None:
        raw=result.get('legacy_sync',{}).get('website')or{}
        for aggregate in value['aggregations'].values():aggregate['website_sync']=dict(value['delivery']['website'],**raw)
    with directory_lock(args.state_dir):
        state=UpdateState(args.state_dir/'updates.sqlite3')
        try:
            cycle=state.cycle(value['batch_id'])
            if cycle['payload']['result']['invocation_id']!=value['invocation_id']:
                raise ContractError('CONFIG_CHANGED','another invocation replaced the scientific checkpoint')
            cycle['payload']['result']=value
            state.save(value['batch_id'],cycle['request'],'complete'if science_outcome=='success'else science_outcome,cycle['payload'])
        finally:state.close()
    if interrupted:
        args._terminal_result=value
        raise interrupted
    return value


def run(args,invoke_fn=None,delivery_fn=None):
    value=run_science(args,invoke_fn)
    if args.dry_run or not args._context.get('_fresh_science'):return value
    try:
        return finish_delivery(args,value,delivery_fn or run_delivery_job)
    except (Exception,KeyboardInterrupt)as exc:
        if not getattr(args,'_terminal_result',None):
            value['errors'].append(error_record('CHECKPOINT_WRITE_FAILED'if isinstance(exc,(OSError,sqlite3.Error))else'UPDATE_INTERRUPTED'if isinstance(exc,KeyboardInterrupt)else'UPDATE_FAILED','delivery',str(exc)or'delivery interrupted'))
            outcome='interrupted'if isinstance(exc,KeyboardInterrupt)else'failed'
            value.update(outcome=outcome,status=outcome)
            args._terminal_result=value
        raise


def main():
    args=parser().parse_args()
    def interrupted(signum,_frame):
        args._signum=signum
        raise KeyboardInterrupt()
    previous={sig:signal.signal(sig,interrupted) for sig in (signal.SIGINT,signal.SIGTERM)}
    try:
        value=run(args);code=1 if value['status']=='partial' else 0
    except (Exception,KeyboardInterrupt) as exc:
        value=getattr(args,'_terminal_result',None)
        if value is None:value=exception_result(args,exc)
        code=128+getattr(args,'_signum',signal.SIGINT) if isinstance(exc,KeyboardInterrupt) else 1
    finally:
        for sig,handler in previous.items():signal.signal(sig,handler)
    print(json.dumps(value,ensure_ascii=False,indent=2));return code


if __name__=='__main__':sys.exit(main())
