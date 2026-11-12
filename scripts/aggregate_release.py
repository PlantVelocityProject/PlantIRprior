"""Stream cached totals into validated immutable releases and recover publication."""
import csv
import errno
import gzip
import json
import os
from pathlib import Path
import re
import sys

from aggregate_store import COUNT_NAMES, encoded
from download_reads import check_cancel, child_path, now, sha256
from evidence_store import atomic_json, sync_directory

OUTPUTS = {'intron_evidence.tsv.gz','high_confidence_IR.tsv','provenance.json','qc.json'}
PRIOR_FIELDS = ['gene_id','chromosome','start','end','strand']


def classify(row, rules):
    ratio = row['retained']/row['informative'] if row['informative'] else None
    specificity = row['specific']/row['retained'] if row['retained'] else None
    for label in ('high_confidence_IR', 'candidate_IR'):
        limits = rules[label]
        supported = all(row[key] >= limits['min_'+key] for key in ('retained','informative','spliced'))
        if (supported and ratio is not None and specificity is not None
                and ratio >= limits['min_ir_ratio'] and specificity >= limits['min_ir_specificity']):
            return ratio, specificity, label
    limits = rules['nonIR']
    if (ratio is not None and ratio <= limits['max_ir_ratio'] and row['retained'] <= limits['max_retained']
            and row['informative'] >= limits['min_informative'] and row['spliced'] >= limits['min_spliced']):
        return ratio, specificity, 'nonIR'
    return ratio, specificity, 'uncertain'


def owned_outputs(folder, expected=None, control=None):
    if not folder.is_dir() or folder.is_symlink():
        raise ValueError('release directory is missing or unsafe: '+str(folder))
    files = set()
    for path in folder.iterdir():
        child_path(folder,path.name)
        reserved_temp=expected is None and re.fullmatch(r'\.(?:qc|provenance)\.json-[A-Za-z0-9_]{8}',path.name)
        if not path.is_file() or (path.name not in OUTPUTS and not reserved_temp):
            raise ValueError('unknown file in release staging/destination: '+str(path))
        files.add(path.name)
    if expected is not None:
        if files != set(expected):
            raise ValueError('incomplete pending release')
        if any(sha256(child_path(folder,name),control) != digest for name,digest in expected.items()):
            raise ValueError('pending release file hash mismatch')


def loader_identity(directory=None):
    # Publication checks a stable TSV contract, independent of the version of
    # the long-read extractor installed in the fixed computation image.
    import prior_format
    return prior_format, {'name':'IRagent.prior_format','version':'1',
                          'schema':'plantvelo-high-confidence-five-columns-v1',
                          'source_sha256':{'prior_format.py':sha256(Path(prior_format.__file__))}}


def render(store, batch, stage, recipe, plantvelo_dir, control):
    if stage.exists():
        owned_outputs(stage)
        for path in stage.iterdir():
            path.unlink()
    else:
        stage.mkdir(parents=True)
    fields = ['gene_id','intron_id','intron_uid','chromosome','start','end','strand',
              'supporting_transcripts','anchor_count','exon_overlap_warning','n_samples'] + ['N_'+k for k in COUNT_NAMES] + ['IR_ratio','IR_specificity','IR_class']
    high, count = 0, 0
    sums = dict.fromkeys(COUNT_NAMES,0)
    with gzip.open(str(stage/'intron_evidence.tsv.gz'),'wt',newline='') as all_handle, (stage/'high_confidence_IR.tsv').open('w',newline='') as prior_handle:
        all_writer = csv.DictWriter(all_handle,fieldnames=fields,delimiter='\t',lineterminator='\n')
        prior_writer = csv.DictWriter(prior_handle,fieldnames=PRIOR_FIELDS,delimiter='\t',lineterminator='\n')
        all_writer.writeheader(); prior_writer.writeheader()
        for row in store.db.execute('''SELECT i.*,t.* FROM introns i JOIN totals t ON i.id=t.intron_id ORDER BY i.chromosome,i.start,i.end,i.gene,i.uid'''):
            check_cancel(control)
            attributes = json.loads(row['attributes'])
            ratio,specificity,label = classify(row,recipe['rules'])
            output = dict(attributes,n_samples=row['n_samples'],IR_ratio='' if ratio is None else ratio,
                          IR_specificity='' if specificity is None else specificity,IR_class=label)
            output.update({'N_'+key:row[key] for key in COUNT_NAMES})
            all_writer.writerow(output); count += 1
            for key in sums: sums[key] += row[key]
            if label=='high_confidence_IR':
                prior_writer.writerow({k:output[k] for k in PRIOR_FIELDS}); high += 1
    qc = {'generation':batch['generation'],'intron_rows':count,'high_confidence_rows':high,
          'counts':sums,'count_unit':'read_intron_pairs','cohort_hash':batch['cohort_hash']}
    atomic_json(stage/'qc.json',qc)
    if high==0:
        raise ValueError('no high-confidence IR records; previous release retained, aggregated counts cached')
    loader, software = loader_identity(plantvelo_dir)
    loaded = loader.load_builtin_ir_prior(str(stage/'high_confidence_IR.tsv'), species=store.get('reference')['species'])
    for record in loaded.records:
        key = record.key
        uid = '{}|{}:{}-{}:{}'.format(key.gene_id,key.chromosome,key.start+1,key.end,key.strand)
        if not store.db.execute('SELECT 1 FROM introns WHERE uid=?',(uid,)).fetchone():
            raise ValueError('exported prior does not match the reference annotation')
    if len(loaded.records) != high:
        raise ValueError('PlantVelo loader changed high-confidence row set')
    qc.update(plantvelo_validation={'status':'passed','matched_introns':high,'software':software})
    atomic_json(stage/'qc.json',qc)
    # Stream the cohort list rather than constructing a sample×intron matrix.
    members = [dict(accession=r['accession'],version=r['version'],source=json.loads(r['source']),
                    metadata=json.loads(r['metadata']),compatibility=json.loads(r['signature']))
               for r in store.db.execute('''SELECT m.accession,v.* FROM batch_members m JOIN versions v ON m.version_id=v.id WHERE m.batch_id=? ORDER BY m.accession''',(batch['id'],))]
    provenance = {'schema_version':1,'release_id':batch['id'],'generation':batch['generation'],
                  'created_at':batch['created_at'],'reference':store.get('reference'),
                  'catalogue_software':store.get('catalogue_software'),'recipe':recipe,
                  'cohort_hash':batch['cohort_hash'],'samples':members,'plantvelo':software,
                  'read_samples':json.loads(batch['read_samples'])}
    if store.get('evidence_admission'): provenance['evidence_admission']=store.get('evidence_admission')
    atomic_json(stage/'provenance.json',provenance)
    for path in stage.iterdir():
        with path.open('rb') as handle: os.fsync(handle.fileno())
    sync_directory(stage)
    return {name:sha256(stage/name,control) for name in sorted(OUTPUTS)}


def publish(store, batch, root, recipe, plantvelo_dir, control, validate=None):
    root.mkdir(parents=True,exist_ok=True)
    stage = child_path(root,Path('.partial')/batch['id'])
    destination = child_path(root,batch['id'])
    if batch['status']=='aggregated':
        if destination.exists():
            raise ValueError('unregistered release destination already exists')
        files = render(store,batch,stage,recipe,plantvelo_dir,control)
        if validate:validate(stage)
        with store.db:
            store.db.execute("UPDATE batches SET status='publishing',files=?,recipe=? WHERE id=?",
                             (encoded(files),encoded(recipe),batch['id']))
        batch = dict(store.db.execute('SELECT * FROM batches WHERE id=?',(batch['id'],)).fetchone())
    files = json.loads(batch['files'])
    check_cancel(control)
    if not destination.exists():
        owned_outputs(stage,files,control)
        if validate:validate(stage)
        os.replace(stage,destination)
        sync_directory(root)
        sync_directory(stage.parent)
    owned_outputs(destination,files,control)
    if validate:validate(destination)
    for entry in store.get('reference')['files'].values():
        if sha256(Path(entry['path']),control)!=entry['sha256']:
            raise ValueError('reference changed before publication; previous release retained')
    pointer = {'release_id':batch['id'],'generation':batch['generation'],
               'cohort_hash':batch['cohort_hash'],'files':files}
    current = child_path(root,'current.json')
    previous = store.get('published_pointer')
    if current.exists():
        existing = json.loads(current.read_text())
        if existing not in (previous,pointer):
            raise ValueError('current release pointer was modified; preserve it before recovery')
    elif previous is not None:
        # A missing pointer is recoverable from the DB only when publishing a
        # saved batch; no unrelated file is ever overwritten.
        pass
    atomic_json(current,pointer)
    with store.db:
        store.db.execute("UPDATE batches SET status='published' WHERE id=?",(batch['id'],))
        store.db.execute('INSERT OR IGNORE INTO published_accessions SELECT accession FROM batch_members WHERE batch_id=?',(batch['id'],))
        store.set('published_pointer',pointer)
    try:
        stage.parent.rmdir()
    except OSError as exc:
        if exc.errno not in (errno.ENOENT,errno.ENOTEMPTY,errno.EEXIST):
            raise
    else:
        sync_directory(root)
    return {'status':'published','generation':batch['generation'],'release_id':batch['id'],
            'prior':str(destination/'high_confidence_IR.tsv'),'read_samples':json.loads(batch['read_samples'])}
