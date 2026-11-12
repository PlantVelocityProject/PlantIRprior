"""Persistent reference catalogue, sample contributions and incremental totals."""
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from download_reads import check_cancel, child_path, now, sha256, species_code
from evidence_admission import imported_signature, validate_cohort, load_admission
from evidence_contract import COUNT_NAMES, CORE_FIELDS, MAX_INT, checked_counts, native_signature, validate_rows

def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def identity(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class AggregateStore:
    def __init__(self, path, create=False):
        self.path = Path(path)
        fresh = not self.path.exists()
        if fresh and not create:
            raise ValueError('aggregate database does not exist')
        self.db = sqlite3.connect(str(self.path) if create else self.path.as_uri()+'?mode=ro', uri=not create)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA cache_size=-16384')
        self.db.execute('PRAGMA temp_store=FILE')
        if fresh:
            self.db.executescript('''
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE introns (id INTEGER PRIMARY KEY, uid TEXT UNIQUE NOT NULL, gene TEXT,
                    chromosome TEXT, start INTEGER, end INTEGER, strand TEXT, attributes TEXT);
                CREATE TABLE versions (id INTEGER PRIMARY KEY, accession TEXT, version TEXT,
                    metadata TEXT, source TEXT, signature TEXT, created_at TEXT, UNIQUE(accession,version));
                CREATE TABLE samples (accession TEXT PRIMARY KEY, version_id INTEGER REFERENCES versions(id), active INTEGER CHECK(active IN(0,1)));
                CREATE TABLE contributions (version_id INTEGER REFERENCES versions(id), intron_id INTEGER REFERENCES introns(id),
                    retained INTEGER, spliced INTEGER, specific INTEGER, informative INTEGER,
                    PRIMARY KEY(version_id,intron_id), CHECK(typeof(retained)='integer' AND retained>=0),
                    CHECK(typeof(spliced)='integer' AND spliced>=0), CHECK(typeof(specific)='integer' AND specific>=0 AND specific<=retained),
                    CHECK(typeof(informative)='integer' AND informative=retained+spliced)) WITHOUT ROWID;
                CREATE TABLE totals (intron_id INTEGER PRIMARY KEY REFERENCES introns(id),
                    retained INTEGER NOT NULL DEFAULT 0, spliced INTEGER NOT NULL DEFAULT 0,
                    specific INTEGER NOT NULL DEFAULT 0, informative INTEGER NOT NULL DEFAULT 0,
                    n_samples INTEGER NOT NULL DEFAULT 0,
                    CHECK(typeof(retained)='integer' AND retained>=0), CHECK(typeof(spliced)='integer' AND spliced>=0),
                    CHECK(typeof(specific)='integer' AND specific>=0 AND specific<=retained),
                    CHECK(typeof(informative)='integer' AND informative=retained+spliced), CHECK(n_samples>=0));
                CREATE TABLE batches (id TEXT PRIMARY KEY, generation INTEGER UNIQUE, status TEXT,
                    recipe TEXT, read_samples TEXT, cohort_hash TEXT, files TEXT, created_at TEXT);
                CREATE TABLE batch_members (batch_id TEXT REFERENCES batches(id), accession TEXT,
                    version_id INTEGER REFERENCES versions(id), PRIMARY KEY(batch_id,accession));
                CREATE TABLE published_accessions (accession TEXT PRIMARY KEY);
                CREATE TABLE operations (batch_id TEXT, accession TEXT, operation TEXT, old_version INTEGER, new_version INTEGER);
            ''')
            with self.db:
                self.set('schema_version', 1)
        elif self.get('schema_version') != 1:
            raise ValueError('unrecognized aggregate database; preserve it before retrying')

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, encoded(value)))

    def catalogue(self, reference, control):
        if self.get('reference') is not None:
            if self.get('reference') != reference:
                raise ValueError('aggregate reference identity changed; use a new reference namespace')
            return
        # This single reference model is built once. Sample matrices are never
        # concatenated in RAM; all subsequent count updates stream into SQLite.
        from plantvelo.irprior import load_introns_from_gtf
        from plantvelo._version import __version__
        introns = load_introns_from_gtf(reference['files']['annotation']['path'])
        if not introns:
            raise ValueError('reference annotation contains no introns')
        for intron in introns:
            check_cancel(control)
            attributes = {'gene_id':intron.gene_id,'intron_id':intron.intron_id,'intron_uid':intron.intron_uid,
                'chromosome':intron.chrom,'start':str(intron.start0+1),'end':str(intron.end0),'strand':intron.strand,
                'supporting_transcripts':intron.supporting_transcripts,'anchor_count':str(intron.anchor_count),
                'exon_overlap_warning':intron.exon_overlap_warning}
            cursor = self.db.execute('INSERT INTO introns(uid,gene,chromosome,start,end,strand,attributes) VALUES (?,?,?,?,?,?,?)',
                (intron.intron_uid,intron.gene_id,intron.chrom,intron.start0+1,intron.end0,intron.strand,encoded(attributes)))
            self.db.execute('INSERT INTO totals(intron_id) VALUES (?)', (cursor.lastrowid,))
        self.set('reference', reference)
        self.set('catalogue_software', {'name':'plantvelo','version':__version__})

    def membership(self):
        return {r['accession']:dict(r) for r in self.db.execute('''SELECT s.*,v.version FROM samples s JOIN versions v ON s.version_id=v.id''')}

    def pending(self):
        row = self.db.execute("SELECT * FROM batches WHERE status IN ('aggregated','publishing') ORDER BY generation LIMIT 1").fetchone()
        return dict(row) if row else None

    def plan(self, candidates, withdraw, restore):
        members = self.membership()
        for accession in withdraw | restore:
            if accession not in members:
                raise ValueError('cannot withdraw/restore unknown sample: '+accession)
        changes, removals = {}, []
        for accession, candidate in candidates.items():
            old = members.get(accession)
            if accession in withdraw or (old and not old['active'] and accession not in restore):
                continue
            if not old or not old['active'] or old['version'] != candidate['version']:
                changes[accession] = (dict(candidate, restore_cached=True)
                                      if accession in restore and old and not old['active']
                                      and old['version'] == candidate['version'] else candidate)
        for accession in withdraw:
            if members[accession]['active']:
                removals.append(accession)
        if any(a not in candidates for a in restore):
            raise ValueError('restored sample requires current registered evidence metadata')
        published = {r[0] for r in self.db.execute('SELECT accession FROM published_accessions')}
        desired = {a for a,row in members.items() if row['active']} - set(removals)
        desired.update(changes)
        # Applied is not published: a failed export must retain its pending-new
        # count even though those accessions already have cached contributions.
        new = sorted(desired - published)
        return {'changes':changes,'withdrawals':sorted(removals),'new':new}

    def import_version(self, candidate, reference, allow_legacy, control):
        found = self.db.execute('SELECT id FROM versions WHERE accession=? AND version=?',
                                (candidate['accession'],candidate['version'])).fetchone()
        accession = candidate['accession']
        source = Path(candidate['path'])
        if found and candidate.get('restore_cached') and not source.exists() and not source.is_symlink():
            # Explicit restoration may use the frozen contribution after source cleanup.
            # A surviving report remains subject to its registered byte identity.
            if candidate.get('report_sha256'):
                report_path = child_path(source.parent, source.name + '.run.json')
                if report_path.exists() and sha256(report_path, control) != candidate['report_sha256']:
                    raise ValueError('modified evidence run report: ' + str(report_path))
            return found[0], False
        if not source.is_file() or sha256(source, control) != candidate['sha256']:
            raise ValueError('missing or modified registered evidence: '+str(source))
        report = None
        if candidate.get('report_sha256'):
            path = child_path(source.parent,source.name+'.run.json')
            if sha256(path,control) != candidate['report_sha256']:
                raise ValueError('modified evidence run report: '+str(path))
            report = json.loads(path.read_text())
            signature = native_signature(report, candidate['metadata'], reference)
        elif candidate.get('admission'):
            audit=load_admission(source.parent,{'admission':candidate['admission']},reference)
            if audit['samples'].get(accession,{}).get('sha256') != candidate['sha256']:
                raise ValueError('sample is not covered by formal evidence admission')
            signature=imported_signature(audit)
            self.set('evidence_admission',dict(candidate['admission'],count_contract=audit['count_contract'],
                                              native_policy=audit['native_policy']))
        else:
            if not allow_legacy:
                raise ValueError('legacy evidence requires explicit --allow-legacy')
            signature = {'kind':'legacy','parameters':'unknown','software':'unknown'}
        if found:
            return found[0], False
        cursor = self.db.execute('INSERT INTO versions(accession,version,metadata,source,signature,created_at) VALUES (?,?,?,?,?,?)',
            (accession,candidate['version'],encoded(candidate['metadata']),encoded(candidate),encoded(signature),now()))
        version_id = cursor.lastrowid
        def lookup(uid):
            check_cancel(control)
            intron = self.db.execute('SELECT attributes FROM introns WHERE uid=?', (uid,)).fetchone()
            return json.loads(intron['attributes']) if intron is not None else None

        def consume(row):
            intron_id = self.db.execute('SELECT id FROM introns WHERE uid=?', (row['intron_uid'],)).fetchone()[0]
            counts = checked_counts([row['N_' + key] for key in COUNT_NAMES])
            self.db.execute('INSERT INTO contributions VALUES (?,?,?,?,?,?)', (version_id, intron_id, *counts))

        with source.open(newline='') as handle:
            validate_rows(csv.DictReader(handle, delimiter='\t'), candidate['metadata'], lookup,
                          self.db.execute('SELECT count(*) FROM introns').fetchone()[0],
                          signature['kind'], report, consume)
        if sha256(source,control) != candidate['sha256'] or (report is not None and sha256(Path(str(source)+'.run.json'),control) != candidate['report_sha256']):
            raise ValueError('evidence changed during aggregation')
        return version_id, True

    def delta(self, version_id, sign):
        for row in self.db.execute('SELECT * FROM contributions WHERE version_id=?',(version_id,)):
            self.db.execute('''UPDATE totals SET retained=retained+?,spliced=spliced+?,specific=specific+?,
                informative=informative+?,n_samples=n_samples+? WHERE intron_id=?''',
                (*(sign*row[k] for k in COUNT_NAMES),sign,row['intron_id']))

    def apply(self, plan, reference, recipe, allow_legacy, allow_mixed, control):
        read_samples = []
        batch_id = 'g{:06d}-{}'.format(self.get('generation',0)+1,uuid.uuid4().hex[:10])
        with self.db:
            if getattr(self,'supersede',None):
                self.db.execute("UPDATE batches SET status='superseded' WHERE id=? AND status='aggregated'",(self.supersede,))
            self.catalogue(reference,control)
            for accession in plan['withdrawals']:
                old = self.db.execute('SELECT version_id FROM samples WHERE accession=?',(accession,)).fetchone()[0]
                self.delta(old,-1)
                self.db.execute('UPDATE samples SET active=0 WHERE accession=?',(accession,))
                self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?)',(batch_id,accession,'withdraw',old,None))
            for accession,candidate in sorted(plan['changes'].items()):
                version,parsed = self.import_version(candidate,reference,allow_legacy,control)
                if parsed: read_samples.append(accession)
                old = self.db.execute('SELECT version_id,active FROM samples WHERE accession=?',(accession,)).fetchone()
                if old and old['active']:
                    self.delta(old['version_id'],-1)
                self.delta(version,1)
                self.db.execute('INSERT OR REPLACE INTO samples VALUES (?,?,1)',(accession,version))
                self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?)',
                    (batch_id,accession,'replace' if old and old['active'] else 'add',old['version_id'] if old else None,version))
            signatures = [json.loads(r[0]) for r in self.db.execute('''SELECT DISTINCT v.signature FROM samples s JOIN versions v ON v.id=s.version_id WHERE s.active=1''')]
            validate_cohort(signatures,allow_mixed)
            for entry in reference['files'].values():
                if sha256(Path(entry['path']),control) != entry['sha256']:
                    raise ValueError('reference changed during aggregation')
            generation = self.get('generation',0)+1
            cohort = [(r['accession'],r['version']) for r in self.db.execute('''SELECT s.accession,v.version FROM samples s JOIN versions v ON v.id=s.version_id WHERE s.active=1 ORDER BY s.accession''')]
            self.db.execute('INSERT INTO batches VALUES (?,?,?,?,?,?,?,?)',
                (batch_id,generation,'aggregated',encoded(recipe),encoded(read_samples),identity(cohort),'{}',now()))
            self.db.execute('INSERT INTO batch_members SELECT ?,accession,version_id FROM samples WHERE active=1',(batch_id,))
            self.set('generation',generation)
        return self.pending()
