#!/usr/bin/env python3
"""One-time, journaled release-baseline reset; scientific caches are preserved."""
import argparse
from contextlib import closing, ExitStack
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid

from aggregate_evidence import configuration, make_recipe
from aggregate_release import OUTPUTS, owned_outputs, publish
from aggregate_store import AggregateStore, encoded, identity
from artifact_lifecycle import directory_lock
from download_reads import PROJECT, Control, child_path, now, sha256
from evidence_store import atomic_json, sync_directory

MARKER='baseline_reset.json'
NATIVE=re.compile(r'g[0-9]{6,}-[a-f0-9]{10}')
LEGACY=re.compile(r'legacy-[a-f0-9]{12}')
PROTECTED={'introns','versions','samples','contributions','totals','published_accessions'}


def hook(fault,event):
    if fault: fault(event)


def content_hash(path):
    digest=hashlib.sha256()
    with gzip.open(path,'rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def cache_signature(store):
    digest=hashlib.sha256()
    for table in ('versions','samples','published_accessions'):
        digest.update(table.encode())
        for row in store.db.execute('SELECT * FROM '+table+' ORDER BY 1,2' if table!='published_accessions' else 'SELECT * FROM published_accessions ORDER BY 1'):
            digest.update(encoded(tuple(row)).encode());digest.update(b'\n')
    for row in store.db.execute('SELECT * FROM totals ORDER BY intron_id'):
        digest.update(encoded(tuple(row)).encode());digest.update(b'\n')
    digest.update(encoded([tuple(r) for r in store.db.execute("SELECT * FROM meta WHERE key NOT IN ('generation','published_pointer') ORDER BY key")]).encode())
    return {'sha256':digest.hexdigest(),
            'introns':store.db.execute('SELECT count(*) FROM introns').fetchone()[0],
            'contributions':store.db.execute('SELECT count(*) FROM contributions').fetchone()[0],
            'active_samples':store.db.execute('SELECT count(*) FROM samples WHERE active=1').fetchone()[0]}


def files(folder):
    owned_outputs(folder)
    if {p.name for p in folder.iterdir()}!=OUTPUTS:raise ValueError('incomplete release directory: '+str(folder))
    return {p.name:sha256(p) for p in sorted(folder.iterdir())}


def preflight(database,root):
    if Path(database).is_symlink() or Path(root).is_symlink():raise ValueError('symlinked maintenance database/release root')
    database=Path(database).resolve();root=Path(root).resolve()
    if not database.is_file():raise ValueError('missing aggregate DB: '+str(database))
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute('PRAGMA journal_mode').fetchone()[0]!='delete':
            raise ValueError('baseline reset requires a quiescent DELETE-journal database')
    store=AggregateStore(database)
    try:
        if store.pending():raise ValueError('finish pending publication before baseline reset')
        pointer=store.get('published_pointer')
        if not pointer or json.loads(child_path(root,'current.json').read_text())!=pointer:
            raise ValueError('current pointer and database do not agree')
        batch=store.db.execute('SELECT * FROM batches WHERE id=?',(pointer['release_id'],)).fetchone()
        if batch is None or batch['status']!='published':raise ValueError('current batch is not published')
        active=[tuple(r) for r in store.db.execute('SELECT accession,version_id FROM samples WHERE active=1 ORDER BY accession')]
        members=[tuple(r) for r in store.db.execute('SELECT accession,version_id FROM batch_members WHERE batch_id=? ORDER BY accession',(batch['id'],))]
        if active!=members:raise ValueError('cached cohort differs from latest published cohort')
        if store.db.execute('SELECT accession FROM samples WHERE active=1 EXCEPT SELECT accession FROM published_accessions').fetchone():
            raise ValueError('active samples are missing from published accession history')
        old_dirs={};old_metadata={};reference=store.get('reference')
        for entry in root.iterdir():
            child_path(root,entry.name)
            if entry.name=='current.json':continue
            if entry.name=='.partial':
                if not entry.is_dir() or entry.is_symlink() or any(entry.iterdir()):
                    raise ValueError('nonempty or unsafe release staging directory')
                continue
            if not entry.is_dir() or entry.is_symlink():raise ValueError('unknown release entry: '+str(entry))
            snapshot=files(entry)
            if NATIVE.fullmatch(entry.name):
                old=store.db.execute('SELECT status,files FROM batches WHERE id=?',(entry.name,)).fetchone()
                if old is None or old['status']!='published' or json.loads(old['files'])!=snapshot:
                    raise ValueError('unregistered or changed published directory: '+str(entry))
            elif LEGACY.fullmatch(entry.name):
                provenance=json.loads((entry/'provenance.json').read_text())
                if (provenance.get('origin')!='legacy_classified_csv_import'
                        or any(provenance.get(k)!=reference[k] for k in ('species','reference_id','annotation_id'))):
                    raise ValueError('unrecognized legacy directory: '+str(entry))
                legacy_files=provenance.get('files',{})
                if set(legacy_files)!=OUTPUTS-{'provenance.json'}:raise ValueError('legacy file ownership is incomplete')
                for name,digest in legacy_files.items():
                    if snapshot.get(name)!=digest:raise ValueError('changed legacy file: '+str(entry/name))
            else:raise ValueError('unknown release directory: '+str(entry))
            old_dirs[entry.name]=snapshot
            old_provenance=json.loads((entry/'provenance.json').read_text())
            old_metadata[entry.name]={'provenance':{k:v for k,v in old_provenance.items() if k not in ('samples','read_samples')},
                                      'qc':json.loads((entry/'qc.json').read_text())}
        latest=child_path(root,pointer['release_id'])
        if old_dirs.get(pointer['release_id'])!=pointer['files']:raise ValueError('latest release hash mismatch')
        return {'old_pointer':pointer,'old_dirs':old_dirs,'old_metadata':old_metadata,'cache':cache_signature(store),
                'evidence_text_sha256':content_hash(latest/'intron_evidence.tsv.gz'),
                'prior_sha256':sha256(latest/'high_confidence_IR.tsv'),
                'old_batches':[dict(r) for r in store.db.execute('SELECT * FROM batches ORDER BY generation')]}
    finally:store.close()


def protect_cache(action,table,column,database,trigger):
    # Standard publication repeats INSERT OR IGNORE for already-published
    # accessions. Preflight proves these rows exist; signatures verify no change.
    if action==sqlite3.SQLITE_INSERT and table=='published_accessions':return sqlite3.SQLITE_OK
    if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE) and table in PROTECTED:
        return sqlite3.SQLITE_DENY
    if action in (sqlite3.SQLITE_DROP_TABLE,sqlite3.SQLITE_ALTER_TABLE):return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def verify_baseline(database,root,journal):
    store=AggregateStore(database)
    try:
        if cache_signature(store)!=journal['cache']:raise ValueError('scientific cache changed during baseline reset')
        pointer=store.get('published_pointer')
        if (not pointer or pointer['release_id']!=journal['new_release_id'] or pointer['generation']!=1
                or json.loads(child_path(root,'current.json').read_text())!=pointer):
            raise ValueError('baseline pointer mismatch')
        if [tuple(r) for r in store.db.execute('SELECT generation,status FROM batches')]!=[(1,'published')]:
            raise ValueError('baseline release ledger mismatch')
        folder=child_path(root,pointer['release_id'])
        owned_outputs(folder,pointer['files'])
        if (sha256(folder/'high_confidence_IR.tsv')!=journal['prior_sha256']
                or content_hash(folder/'intron_evidence.tsv.gz')!=journal['evidence_text_sha256']):
            raise ValueError('baseline scientific output differs from previous latest release')
        for name in ('provenance.json','qc.json'):
            if json.loads((folder/name).read_text())['generation']!=1:raise ValueError('baseline metadata generation mismatch')
        return pointer
    finally:store.close()


def verify_scientific_output(folder,journal):
    if (sha256(folder/'high_confidence_IR.tsv')!=journal['prior_sha256']
            or content_hash(folder/'intron_evidence.tsv.gz')!=journal['evidence_text_sha256']):
        raise ValueError('baseline scientific output differs from previous latest release')


def rebase_group(database,release_root,report_root,recipe,plantvelo_dir,control=None,fault=None,expected_pointer=None):
    if any(Path(p).is_symlink() for p in (database,release_root,report_root)):
        raise ValueError('symlinked maintenance path')
    database=Path(database).resolve();root=Path(release_root).resolve();report=Path(report_root).resolve()
    if report==root or root in report.parents or report==database.parent or database.parent in report.parents:
        raise ValueError('maintenance reports must be outside managed release/database directories')
    control=control or Control();journal_path=report/'journal.json';marker=database.parent/MARKER
    with directory_lock(database.parent):
        if journal_path.exists():
            journal=json.loads(journal_path.read_text())
            if (journal.get('database')!=str(database) or journal.get('release_root')!=str(root)
                    or journal.get('recipe')!=recipe or not NATIVE.fullmatch(journal.get('new_release_id',''))):
                raise ValueError('baseline reset journal identity/recipe mismatch')
        else:
            if marker.exists() or marker.is_symlink():raise ValueError('another baseline reset owns this namespace')
            snapshot=preflight(database,root)
            if expected_pointer is not None and snapshot['old_pointer']!=expected_pointer:
                raise ValueError('published source changed since maintenance was planned')
            journal=dict(snapshot,schema_version=1,database=str(database),release_root=str(root),
                         new_release_id='g000001-'+uuid.uuid4().hex[:10],recipe=recipe,created_at=now(),phase='planned',removed_dirs=[])
            report.mkdir(parents=True,exist_ok=True);atomic_json(journal_path,journal)
        new_id=journal['new_release_id']
        next_db=database.parent/('.aggregate.next-'+new_id+'.sqlite3')
        backup=database.parent/('.aggregate.before-'+new_id+'.sqlite3')
        if next_db.is_symlink() or backup.is_symlink():raise ValueError('symlinked baseline scratch database')
        marker_value={'journal':str(journal_path),'new_release_id':new_id}
        if marker.exists() or marker.is_symlink():
            if marker.is_symlink() or json.loads(marker.read_text())!=marker_value:raise ValueError('foreign baseline maintenance marker')
        elif journal['phase']!='complete':atomic_json(marker,marker_value)
        def save(phase):
            journal['phase']=phase;atomic_json(journal_path,journal)
        if journal['phase']=='planned':
            if preflight(database,root)['old_pointer']!=journal['old_pointer']:raise ValueError('source release changed before baseline preparation')
            for path in (next_db,Path(str(next_db)+'-journal')):
                if path.is_symlink():raise ValueError('unsafe baseline clone')
                if path.exists():path.unlink()
            with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as original, closing(sqlite3.connect(str(next_db))) as clone:
                original.backup(clone,pages=2048)
            clone=AggregateStore(next_db,create=True)
            try:
                clone.db.set_authorizer(protect_cache)
                with clone.db:
                    clone.db.execute('DELETE FROM operations');clone.db.execute('DELETE FROM batch_members');clone.db.execute('DELETE FROM batches')
                    clone.db.execute('INSERT INTO batches VALUES (?,?,?,?,?,?,?,?)',
                        (new_id,1,'aggregated',encoded(recipe),'[]',journal['old_pointer']['cohort_hash'],'{}',journal['created_at']))
                    clone.db.execute('INSERT INTO batch_members SELECT ?,accession,version_id FROM samples WHERE active=1',(new_id,))
                    clone.db.execute("INSERT INTO operations SELECT ?,accession,'baseline',NULL,version_id FROM samples WHERE active=1",(new_id,))
                    clone.set('generation',1)
                if cache_signature(clone)!=journal['cache']:raise ValueError('clone changed scientific caches')
            finally:clone.close()
            with next_db.open('rb') as handle:os.fsync(handle.fileno())
            sync_directory(database.parent)
            save('prepared');hook(fault,'after_prepared')
        if journal['phase']=='prepared':
            clone=AggregateStore(next_db,create=True)
            try:
                clone.db.set_authorizer(protect_cache)
                batch=clone.db.execute('SELECT * FROM batches WHERE id=?',(new_id,)).fetchone()
                if batch['status']!='published':
                    publish(clone,dict(batch),root,recipe,Path(plantvelo_dir),control,
                            validate=lambda folder:verify_scientific_output(folder,journal))
            finally:clone.close()
            verify_baseline(next_db,root,journal)
            save('published');hook(fault,'after_publish')
        if journal['phase']=='published':
            if next_db.exists():
                if not backup.exists():
                    if not database.exists():raise ValueError('missing source database before backup')
                    os.replace(database,backup);sync_directory(database.parent);hook(fault,'after_db_backup')
                elif database.exists():raise ValueError('ambiguous database installation state')
                os.replace(next_db,database);sync_directory(database.parent)
            elif not database.exists() or not backup.exists():raise ValueError('incomplete baseline database installation')
            verify_baseline(database,root,journal)
            save('installed');hook(fault,'after_db_install')
        if journal['phase']=='installed':
            for name,expected in journal['old_dirs'].items():
                if name==new_id or not (NATIVE.fullmatch(name) or LEGACY.fullmatch(name)) or set(expected)!=OUTPUTS:
                    raise ValueError('unsafe obsolete directory record')
                if name in journal['removed_dirs']:continue
                folder=child_path(root,name)
                if journal.get('removing_dir')!=name:
                    if files(folder)!=expected:raise ValueError('obsolete release changed before deletion')
                    journal['removing_dir']=name;save('installed')
                if folder.exists():
                    if not folder.is_dir() or folder.is_symlink():raise ValueError('unsafe obsolete release')
                    for path in folder.iterdir():
                        child_path(folder,path.name)
                        if path.name not in expected or not path.is_file() or path.is_symlink() or sha256(path)!=expected[path.name]:
                            raise ValueError('unknown or changed obsolete file: '+str(path))
                    for path in list(folder.iterdir()):
                        path.unlink();hook(fault,'during_cleanup')
                    folder.rmdir();sync_directory(root)
                journal['removed_dirs'].append(name);journal.pop('removing_dir',None);save('installed')
            partial=root/'.partial'
            if partial.exists():
                if partial.is_symlink() or not partial.is_dir() or any(partial.iterdir()):raise ValueError('unexpected staging after reset')
                partial.rmdir();sync_directory(root)
            verify_baseline(database,root,journal)
            if {p.name for p in root.iterdir()}!={new_id,'current.json'}:raise ValueError('unexpected final release entries')
            save('complete')
        if journal['phase']!='complete':raise ValueError('unknown baseline reset phase')
        verify_baseline(database,root,journal)
        if {p.name for p in root.iterdir()}!={new_id,'current.json'}:raise ValueError('unexpected final release entries')
        if marker.exists():marker.unlink();sync_directory(database.parent)
        return {'status':'complete','new_release_id':new_id,'old_release_id':journal['old_pointer']['release_id'],
                'generation':1,'cache':journal['cache'],'journal':str(journal_path)}


def remove_backup(report_root):
    path=Path(report_root)/'journal.json';journal=json.loads(path.read_text())
    if journal['phase']!='complete':raise ValueError('cannot discard backup before reset completion')
    database=Path(journal['database']);root=Path(journal['release_root']);new_id=journal['new_release_id']
    if not NATIVE.fullmatch(new_id):raise ValueError('unsafe baseline ID')
    with directory_lock(database.parent):
        verify_baseline(database,root,journal)
        backup=database.parent/('.aggregate.before-'+new_id+'.sqlite3')
        if backup.is_symlink():raise ValueError('unsafe baseline backup')
        if backup.exists():backup.unlink();sync_directory(database.parent)
        journal['backup_removed']=True;atomic_json(path,journal)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--aggregate-dir',type=Path,default=PROJECT/'storage/aggregates')
    parser.add_argument('--release-dir',type=Path,default=PROJECT/'releases')
    parser.add_argument('--report-dir',type=Path,required=True,help='reuse this same directory to resume')
    parser.add_argument('--plantvelo-dir',type=Path,default=PROJECT/'plantvelo')
    parser.add_argument('--config',type=Path,default=PROJECT/'configs/aggregation.json')
    parser.add_argument('--classification-config',type=Path,default=PROJECT/'configs/classification.json')
    parser.add_argument('--apply',action='store_true',help='without this flag, preview only')
    args=parser.parse_args()
    try:
        aggregate=args.aggregate_dir.resolve();release=args.release_dir.resolve();report=args.report_dir.resolve()
        _,rules=configuration(args);recipe=make_recipe(rules,False,None,args.plantvelo_dir.resolve())
        plan_path=report/'plan.json'
        plan=None
        if plan_path.exists():
            plan=json.loads(plan_path.read_text())
            if (plan.get('aggregate_dir')!=str(aggregate) or plan.get('release_dir')!=str(release)
                    or plan.get('recipe')!=recipe):raise ValueError('maintenance plan identity/recipe mismatch')
            groups=[]
            for name in plan['namespaces']:
                namespace=Path(name)
                if len(namespace.parts)!=3 or namespace.is_absolute() or '..' in namespace.parts:
                    raise ValueError('unsafe namespace in maintenance plan')
                groups.append(aggregate/namespace/'aggregate.sqlite3')
        else:
            groups=sorted(p for p in aggregate.glob('*/*/*/aggregate.sqlite3') if p.is_file())
        if not groups:raise ValueError('no existing aggregate namespaces')
        global_marker=aggregate/MARKER;global_value={'report':str(report)}
        with directory_lock(aggregate):
            if global_marker.exists() or global_marker.is_symlink():
                if global_marker.is_symlink() or json.loads(global_marker.read_text())!=global_value:
                    raise ValueError('another maintenance operation owns aggregate root')
            elif plan is not None and (report/'summary.json').exists():
                # A completed one-time operation is a no-op even if later,
                # independent releases have advanced the live generation.
                for database in groups:
                    record=json.loads((report/database.parent.relative_to(aggregate)/'journal.json').read_text())
                    if record['phase']!='complete' or not record.get('backup_removed'):
                        raise ValueError('completion report disagrees with maintenance journals')
                print(json.dumps({'status':'already_complete','report':str(report)}));return 0
            snapshots={}
            # Freeze all namespace pointers until the global marker is durable.
            # Ordinary aggregation checks that marker again under its own lock.
            with ExitStack() as locks:
                for database in groups:locks.enter_context(directory_lock(database.parent))
                for database in groups:
                    namespace=database.parent.relative_to(aggregate)
                    if not (report/namespace/'journal.json').exists():
                        if (database.parent/MARKER).exists() or (database.parent/MARKER).is_symlink():
                            raise ValueError('another baseline reset owns namespace: '+str(namespace))
                        snapshots[str(namespace)]=preflight(database,release/namespace)
                        if plan is not None and snapshots[str(namespace)]['old_pointer']!=plan['pointers'][str(namespace)]:
                            raise ValueError('published source changed since maintenance was planned')
                if args.apply:
                    if plan is None:
                        report.mkdir(parents=True,exist_ok=True)
                        plan={'schema_version':1,'aggregate_dir':str(aggregate),'release_dir':str(release),'recipe':recipe,
                              'namespaces':[str(p.parent.relative_to(aggregate)) for p in groups],
                              'pointers':{name:snapshot['old_pointer'] for name,snapshot in snapshots.items()}}
                        atomic_json(plan_path,plan)
                    if not global_marker.exists():atomic_json(global_marker,global_value)
            results=[]
            for database in groups:
                namespace=database.parent.relative_to(aggregate)
                if args.apply:
                    item=rebase_group(database,release/namespace,report/namespace,recipe,args.plantvelo_dir.resolve(),
                                      expected_pointer=plan['pointers'][str(namespace)])
                else:
                    snapshot=snapshots[str(namespace)]
                    item={'namespace':str(namespace),'old_release_id':snapshot['old_pointer']['release_id'],
                          'old_generation':snapshot['old_pointer']['generation'],'cache':snapshot['cache'],
                          'obsolete_directories':list(snapshot['old_dirs']),'target_generation':1}
                results.append(item);print(json.dumps(item,ensure_ascii=False),flush=True)
            if args.apply:
                for database in groups:remove_backup(report/database.parent.relative_to(aggregate))
                atomic_json(report/'summary.json',results)
                global_marker.unlink();sync_directory(aggregate)
        return 0
    except (OSError,ValueError,RuntimeError,sqlite3.Error,KeyError,TypeError) as error:
        print(json.dumps({'status':'failed','error':str(error)},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
