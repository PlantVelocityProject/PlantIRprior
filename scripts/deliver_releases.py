#!/usr/bin/env python3
"""Deliver a pinned scientific release plan without running scientific stages."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import signal
import sys
import uuid

from artifact_lifecycle import directory_lock
from delivery_store import DeliveryStore,capture_plan,file_hash,job_identity,read_json,runtime_plan
from download_reads import PROJECT,species_code
from task_contract import ContractError,error_record,validate_delivery_result


def sync_resource(*args,**kwargs):
    from prior_sync import sync_species
    return sync_species(*args,**kwargs)


def sync_website(*args,**kwargs):
    from website_release import sync_website as sync
    return sync(*args,**kwargs)


def delivery_view(job,targets=None):
    plan=job['plan'];targets=targets or plan['targets'];states=job['target_state'];resources=[]
    if 'resources'in targets:
        for code in sorted(set(plan['resources'])|set(plan['skipped_resources'])):
            descriptor=plan['resources'].get(code)
            namespace=descriptor or plan['skipped_resources'].get(code)or plan['config']['defaults'][code]
            state=states.get('resources/'+code,{})
            resources.append(dict(species=code,reference_id=namespace['reference_id'],annotation_id=namespace['annotation_id'],
                release_id=descriptor['release_id']if descriptor else None,status='pending'if state.get('status')in (None,'running')else state['status']))
    website=None
    if 'website'in targets:
        state=states.get('website',{});result=state.get('result')or{}
        website=dict(status='pending'if state.get('status')in (None,'running')else state['status'],snapshot_id=result.get('snapshot_id'))
    return dict(resources=resources,website=website)


def job_result(job,targets=None,invocation_id=None,outcome=None,errors=None):
    targets=sorted(targets or job['plan']['targets']);view=delivery_view(job,targets)
    if errors is None:
        errors=[]
        for key,state in job['target_state'].items():
            if key.split('/')[0] not in targets:continue
            error=(state.get('result')or{}).get('error_record')
            if error:errors.append(error)
    states=[r['status']for r in view['resources']]+([]if view['website']is None else[view['website']['status']])
    outcome=outcome or ('partial'if any(s not in ('succeeded','skipped')for s in states)else'success')
    legacy=dict(resources={},website=None)
    for key,state in job['target_state'].items():
        if key.split('/')[0]not in targets:continue
        raw={k:v for k,v in (state.get('result')or{}).items()if k not in ('verification_cache','error_record')}
        raw.setdefault('status','failed'if state['status']=='superseded'else state['status'])
        error=(state.get('result')or{}).get('error_record')
        if error:raw['error']=error['message']
        if key=='website':legacy['website']=raw
        else:legacy['resources'][key.split('/')[1]]=raw
    value=dict(schema_version=2,kind='delivery_result',job_id=job['id'],invocation_id=invocation_id or 'inv-'+uuid.uuid4().hex,
        outcome=outcome,status='complete'if outcome=='success'else outcome,delivery=view,errors=errors,
        targets=targets,job_status=job['status'],legacy_sync=legacy)
    return validate_delivery_result(value,job['id'])


def run_job(state_db,job_id,targets=None,project_root=None):
    state_db=Path(state_db).resolve()
    with directory_lock(state_db.parent):
        with DeliveryStore(state_db,create=False)as reader:job=reader.get(job_id)
        plan=runtime_plan(job['plan'],project_root or PROJECT);selected=sorted(set(targets or plan['targets']))
        if not selected or not set(selected)<=set(plan['targets']):raise ContractError('DELIVERY_PLAN_MISMATCH','target expansion is not allowed')
        if file_hash(plan['config_path'])!=plan['config_sha256']:raise ContractError('DELIVERY_PLAN_MISMATCH','delivery configuration changed; create a new plan')
        with DeliveryStore(state_db)as store:
            items=[]
            if 'resources'in selected:items+=['resources/'+code for code in sorted(set(plan['resources'])|set(plan['skipped_resources']))]
            if 'website'in selected:items+=['website']
            for key in items:
                old=job['target_state'].get(key,{})
                cache=deepcopy((old.get('result')or{}).get('verification_cache',{}))
                store.checkpoint(job_id,key,'running',dict(verification_cache=cache))
                try:
                    if key=='website':
                        if plan['missing_website']:raise ContractError('MISSING_RELEASE','website is missing published releases: '+', '.join(plan['missing_website']))
                        value=sync_website(plan['release_dir'],plan['website_dir'],plan['config'],
                            releases=plan['website_releases'],verification_cache=cache)
                    else:
                        code=key.split('/')[1]
                        if code in plan['skipped_resources']:
                            store.checkpoint(job_id,key,'skipped',dict(status='not_default_reference'));continue
                        descriptor=plan['resources'][code]
                        if descriptor is None:raise ContractError('MISSING_RELEASE','no published release: '+code)
                        value=sync_resource(code,plan['release_dir'],plan['references_dir'],plan['resources_dir'],plan['config'],descriptor=descriptor)
                    if not isinstance(value,dict)or value.get('status')not in ('synchronized','up_to_date'):
                        raise ValueError('delivery engine did not confirm a successful target')
                    store.checkpoint(job_id,key,'succeeded',dict(value,verification_cache=cache))
                except KeyboardInterrupt:
                    store.checkpoint(job_id,key,'pending',dict(error_record=error_record('UPDATE_INTERRUPTED','delivery','delivery interrupted'),verification_cache=cache))
                    raise
                except (OSError,ValueError,RuntimeError,KeyError,TypeError,ImportError)as exc:
                    error=error_record(getattr(exc,'code','DELIVERY_FAILED'),'website_sync'if key=='website'else'prior_sync',str(exc))
                    store.checkpoint(job_id,key,'superseded'if error['code']=='DELIVERY_SUPERSEDED'else'failed',dict(error_record=error,verification_cache=cache))
            return job_result(store.get(job_id),selected)


def parser():
    p=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    p.add_argument('--job-id');p.add_argument('--species',action='append',default=[])
    p.add_argument('--target',action='append',choices=('resources','website'))
    for name,default in [('state-db','state/updates.sqlite3'),('release-dir','releases'),('references-dir','storage/references'),
        ('prior-data-dir','plantvelo/data'),('website-dir','website'),('config','configs/prior_resources.json')]:
        p.add_argument('--'+name,type=Path,default=PROJECT/default)
    p.add_argument('--dry-run',action='store_true');p.add_argument('--audit',action='store_true');p.add_argument('--json',action='store_true')
    return p


def run(args):
    if args.job_id and args.species:raise ValueError('job-id and species selectors are mutually exclusive')
    if args.audit:
        if args.job_id or args.target!=['website']:raise ValueError('audit requires only --target website and no job-id')
        return sync_website(args.release_dir,args.website_dir,read_json(args.config),audit=True)
    if args.job_id:
        with DeliveryStore(args.state_db,create=False)as store:job=store.get(args.job_id)
        if args.target and not set(args.target)<=set(job['plan']['targets']):raise ValueError('target expansion is not allowed')
    else:
        plan=capture_plan([species_code(s)for s in args.species],args.release_dir,args.references_dir,
            args.prior_data_dir,args.website_dir,args.config,targets=args.target,project_root=PROJECT)
        job=dict(id=job_identity(plan),plan=plan,status='pending',target_state={})
    if args.dry_run:return dict(schema_version=1,kind='delivery_plan_preview',status='planned',job_id=job['id'],targets=sorted(args.target or job['plan']['targets']))
    if not args.job_id:
        args.state_db.parent.mkdir(parents=True,exist_ok=True)
        with directory_lock(args.state_db.parent),DeliveryStore(args.state_db)as store:store.register(job['plan'])
    return run_job(args.state_db,job['id'],targets=args.target)


def main():
    args=parser().parse_args();received=[signal.SIGINT]
    def interrupted(signum,_frame):received[0]=signum;raise KeyboardInterrupt()
    handlers={s:signal.signal(s,interrupted)for s in (signal.SIGINT,signal.SIGTERM)}
    try:
        value=run(args);code=1 if value.get('outcome')in ('partial','failed')else 0
    except (Exception,KeyboardInterrupt)as exc:
        error=error_record('UPDATE_INTERRUPTED'if isinstance(exc,KeyboardInterrupt)else getattr(exc,'code','DELIVERY_FAILED'),'delivery',str(exc)or'delivery interrupted')
        value=dict(schema_version=2,kind='delivery_result',job_id=args.job_id,invocation_id='inv-'+uuid.uuid4().hex,
            outcome='interrupted'if isinstance(exc,KeyboardInterrupt)else'failed',status='interrupted'if isinstance(exc,KeyboardInterrupt)else'failed',
            delivery=dict(resources=[],website=None),errors=[error])
        if args.job_id:
            try:
                with DeliveryStore(args.state_db,create=False)as store:value=job_result(store.get(args.job_id),args.target,outcome=value['outcome'],errors=[error])
            except (OSError,ValueError):pass
        code=128+received[0]if isinstance(exc,KeyboardInterrupt)else 1
    finally:
        for s,handler in handlers.items():signal.signal(s,handler)
    print(json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2));return code


if __name__=='__main__':sys.exit(main())
