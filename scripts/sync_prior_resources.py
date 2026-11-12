#!/usr/bin/env python3
"""Synchronize published high-confidence snapshots into PlantVelo resources."""
import argparse
import json
from pathlib import Path
import sys
from download_reads import PROJECT,species_code
from prior_sync import configuration,json_object,sync_species


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--species',action='append',default=[])
    for name,default in [('release-dir','releases'),('references-dir','storage/references'),
                         ('prior-data-dir','plantvelo/data'),('config','configs/prior_resources.json')]:
        p.add_argument('--'+name,type=Path,default=PROJECT/default)
    p.add_argument('--dry-run',action='store_true');p.add_argument('--json',action='store_true')
    return p


def run(args):
    for name in ('release_dir','references_dir','prior_data_dir','config'):
        setattr(args,name,getattr(args,name).expanduser().absolute())
    config=json_object(args.config);defaults=configuration(config)
    codes=sorted({species_code(s)for s in args.species}if args.species else defaults)
    results=[]
    for code in codes:
        try:results.append(sync_species(code,args.release_dir,args.references_dir,args.prior_data_dir,config,dry_run=args.dry_run))
        except (OSError,ValueError,RuntimeError,KeyError,TypeError,ImportError)as exc:
            results.append({'status':'failed','species':code,'error':str(exc)})
    return {'status':'partial'if any(r['status']=='failed'for r in results)else 'complete','dry_run':args.dry_run,'results':results}


def main():
    try:value=run(parser().parse_args());code=1 if value['status']=='partial'else 0
    except (OSError,ValueError,RuntimeError,KeyError,TypeError,ImportError)as exc:value={'status':'failed','error':str(exc)};code=1
    print(json.dumps(value,ensure_ascii=False,indent=2));return code


if __name__=='__main__':sys.exit(main())
