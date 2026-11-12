"""Small update-cycle checkpoints; scientific stage state stays in its own databases."""
import json
import sqlite3

from download_reads import now


class UpdateState:
    def __init__(self,path):
        self.db=sqlite3.connect(str(path));self.db.row_factory=sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS cycles(id TEXT PRIMARY KEY,request TEXT,status TEXT,payload TEXT,updated_at TEXT);
            CREATE TABLE IF NOT EXISTS evidence_checks(path TEXT PRIMARY KEY,sha256 TEXT,stat TEXT);
            CREATE TABLE IF NOT EXISTS evidence_readiness_checks (
                namespace TEXT NOT NULL,accession TEXT NOT NULL,dependencies TEXT NOT NULL,
                result TEXT NOT NULL,checked_at TEXT NOT NULL,PRIMARY KEY(namespace,accession));
            CREATE TABLE IF NOT EXISTS attempts(namespace TEXT,accession TEXT,sequence INTEGER,PRIMARY KEY(namespace,accession));
            CREATE INDEX IF NOT EXISTS attempts_sequence ON attempts(sequence);
            CREATE TABLE IF NOT EXISTS cleanup_tasks(namespace TEXT,accession TEXT,stage TEXT,PRIMARY KEY(namespace,accession));
        ''')

    def close(self):self.db.close()

    def readiness_cache(self,readonly=False):
        from evidence_readiness import ReadinessCache
        return ReadinessCache(connection=self.db,readonly=readonly)

    def cycle(self,name):
        row=self.db.execute('SELECT * FROM cycles WHERE id=?',(name,)).fetchone()
        if not row:return None
        return dict(row,request=json.loads(row['request']),payload=json.loads(row['payload']))

    def pending(self,request):
        encoded=json.dumps(request,sort_keys=True)
        row=self.db.execute("SELECT id FROM cycles WHERE status IN ('running','partial','interrupted','failed') AND request=? ORDER BY updated_at LIMIT 1",(encoded,)).fetchone()
        return row[0] if row else None

    def save(self,name,request,status,payload):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO cycles VALUES (?,?,?,?,?)',
                (name,json.dumps(request,sort_keys=True),status,json.dumps(payload,ensure_ascii=False),now()))

    def checked(self,path,sha,stat):
        row=self.db.execute('SELECT sha256,stat FROM evidence_checks WHERE path=?',(str(path),)).fetchone()
        return bool(row and row['sha256']==sha and json.loads(row['stat'])==stat)

    def remember(self,path,sha,stat):
        with self.db:self.db.execute('INSERT OR REPLACE INTO evidence_checks VALUES (?,?,?)',
                                    (str(path),sha,json.dumps(stat)))

    def priority(self,namespace,accession):
        row=self.db.execute('SELECT sequence FROM attempts WHERE namespace=? AND accession=?',(str(namespace),accession)).fetchone()
        return row[0] if row else 0

    def attempted(self,namespace,accession):
        with self.db:
            sequence=self.db.execute('SELECT coalesce(max(sequence),0)+1 FROM attempts').fetchone()[0]
            self.db.execute('INSERT OR REPLACE INTO attempts VALUES (?,?,?)',(str(namespace),accession,sequence))

    def cleanup(self,namespace,accession):
        row=self.db.execute('SELECT stage FROM cleanup_tasks WHERE namespace=? AND accession=?',(str(namespace),accession)).fetchone()
        return row[0] if row else None

    def set_cleanup(self,namespace,accession,stage):
        with self.db:
            if stage:self.db.execute('INSERT OR REPLACE INTO cleanup_tasks VALUES (?,?,?)',(str(namespace),accession,stage))
            else:self.db.execute('DELETE FROM cleanup_tasks WHERE namespace=? AND accession=?',(str(namespace),accession))

    def finish_science(self,name,request,status,payload,result,plan):
        from delivery_store import DeliveryStore
        with self.db:
            store=DeliveryStore(None,connection=self.db)
            job=store.register(plan)
            result.update(science_outcome=result['outcome'],delivery_job_id=job,result_finalized=False)
            payload.update(science_done=True,science_outcome=result['outcome'],delivery_job_id=job,result=result)
            self.db.execute('INSERT OR REPLACE INTO cycles VALUES (?,?,?,?,?)',
                (name,json.dumps(request,sort_keys=True),status,json.dumps(payload,ensure_ascii=False),now()))
        return result
