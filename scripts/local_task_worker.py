#!/usr/bin/env python3
"""Independent systemd task worker; owns process, logs and terminal receipt."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys

from local_tasks import TaskStore, atomic_json, json_object, now
from task_contract import (ContractError, error_record, execution_from_result, phase_for,
                           validate_result)

PROJECT = Path(__file__).resolve().parents[1]


def completed_result(project, batch_id):
    """Read a frozen result before dispatch, without creating scientific state."""
    path = Path(project) / 'state/updates.sqlite3'
    if not path.is_file(): return None
    db = None
    try:
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        row = db.execute("SELECT payload FROM cycles WHERE id=? AND status='complete'", (batch_id,)).fetchone()
        return json_object(row[0]).get('result') if row else None
    except (OSError, ValueError, sqlite3.Error):
        return None
    finally:
        if db is not None: db.close()


def run(task_id, attempt, project=PROJECT):
    store = TaskStore(project, None, None)
    task = store.load(task_id)
    operation=task.get('kind','update')
    if type(attempt) is not int or attempt != len(task['attempts']): raise ValueError('stale worker attempt')
    destination = store.receipt_path(task_id, attempt)
    if destination.exists(): return
    prefix = store.tasks / (task_id + '.a' + str(attempt))
    started = now(); child = None; interrupted = [None]
    def stop(signum, _frame):
        interrupted[0] = signum
        if child is not None:
            try: os.killpg(child.pid, signum)
            except ProcessLookupError: pass
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    result = None; code = 1; errors = []; replayed = False; execution = None
    with (store.tasks/'execution.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if store.fingerprint(task['params']) != task['fingerprint']: raise ContractError('CONFIG_CHANGED','configuration identity changed before worker start')
            frozen = completed_result(project, task['batch_id'])if operation=='update'else None
            atomic_json(Path(str(prefix)+'.process.json'),dict(pid=os.getpid(),started_at=started,
                        boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                        start_ticks=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]))
            with Path(str(prefix)+'.stdout.log').open('xb') as stdout, Path(str(prefix)+'.stderr.log').open('xb') as stderr:
                child = subprocess.Popen(store.command(task['params'],task['batch_id']),cwd=str(project),
                                         stdout=stdout,stderr=stderr,start_new_session=True)
                if interrupted[0]: stop(interrupted[0],None)
                code = child.wait()
            output = Path(str(prefix)+'.stdout.log')
            if output.stat().st_size > 4*1024*1024: raise ContractError('INVALID_RESULT','update JSON exceeds receipt limit; inspect logs')
            raw = json_object(output.read_text(),max_depth=48)
            result = validate_result(raw,operation,task['job_id']if operation=='delivery'else task['batch_id'])
            replayed = frozen is not None and frozen == raw
            # Retain verified science on exit disagreement, without claiming success.
            execution = execution_from_result(result,code)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            error_code = getattr(exc,'code','INVALID_RESULT')
            errors.append(error_record(error_code,'worker',str(exc)))
            if error_code != 'EXIT_STATUS_MISMATCH': result = None
            execution = dict(state='stopped',outcome='interrupted' if interrupted[0] and result is None else 'failed')
        receipt = dict(schema_version=2,kind='task_receipt',operation=operation,job_id=task.get('job_id'),task_id=task_id,batch_id=task.get('batch_id'),
                       attempt=attempt,execution=execution,phase=phase_for(execution),returncode=code,
                       result=result,result_replayed=replayed,errors=errors,
                       error=errors[0]['message'] if errors else None,started_at=started,finished_at=now())
        if len(json.dumps(receipt,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()) + 1 > 4*1024*1024:
            errors=[error_record('INVALID_RESULT','worker','wrapped result exceeds receipt size limit; inspect private logs')]
            execution=dict(state='stopped',outcome='failed')
            receipt.update(execution=execution,phase=phase_for(execution),result=None,
                           result_replayed=False,errors=errors,error=errors[0]['message'])
        atomic_json(destination,receipt)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-id',required=True);parser.add_argument('--attempt',type=int,required=True)
    args = parser.parse_args()
    run(args.task_id,args.attempt)
