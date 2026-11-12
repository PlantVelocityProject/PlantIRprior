#!/usr/bin/env python3
"""Same-account IR task API authenticated by Unix peer credentials."""
import fcntl
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import threading

from local_tasks import TaskStore, authorized, json_object, normalize_params

PROJECT = Path(__file__).resolve().parents[1]
MAX_REQUEST = 65536
MAX_RESPONSE = 4 * 1024 * 1024


def unit_activity(unit):
    process = subprocess.run(['systemctl','--user','show',unit,'--property=LoadState,ActiveState'],
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=15)
    fields = dict(line.split('=',1) for line in process.stdout.splitlines() if '=' in line)
    if fields.get('LoadState') == 'not-found': return 'inactive'
    if process.returncode: return 'unknown'
    state = fields.get('ActiveState')
    if state in ('active','activating','reloading','deactivating'): return 'active'
    if state in ('inactive','failed'): return 'inactive'
    return 'unknown'


def launch(task, number):
    unit = task['attempts'][-1]['unit']
    command = ['systemd-run','--user','--quiet','--collect','--unit='+unit,
               '--property=Type=exec','--property=UMask=0077','--property=TimeoutStopSec=45',
               '/usr/bin/python3','-B',str(PROJECT/'scripts/local_task_worker.py'),
               '--task-id',task['id'],'--attempt',str(number)]
    value = subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=30)
    if value.returncode: raise RuntimeError('systemd dispatch failed: ' + value.stderr.strip())


def preflight(store):
    for relative in ('containers/images/plantiragent-v1.sif','containers/images/plantiragent-v1.sif.sha256',
                     'scripts/run_update.sh'):
        if not (store.project/relative).is_file(): raise ValueError('required project input is missing: '+relative)
    free = shutil.disk_usage(str(store.project)).free
    if free < 1024**3: raise ValueError('less than 1 GiB free; do not start another update')
    return free


class TaskHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            self.request.settimeout(10)
            _pid, uid, _gid = struct.unpack('3i',self.request.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if not authorized(uid,self.server.allowed): raise ValueError('caller UID is not authorized')
            data = self.rfile.readline(MAX_REQUEST+1)
            if len(data)>MAX_REQUEST or not data.endswith(b'\n'): raise ValueError('invalid or oversized request frame')
            request = json_object(data.decode('utf-8'))
            value = self.server.dispatch(request)
            reply = dict(ok=True,**value)
        except (OSError,ValueError,RuntimeError,KeyError,TypeError,subprocess.SubprocessError) as exc:
            reply = dict(ok=False,error=str(exc))
            if isinstance(exc,ValueError)and hasattr(exc,'code'):
                reply['code']=exc.code
                if hasattr(exc,'job_id'):reply['job_id']=exc.job_id
        try:
            wire=(json.dumps(reply,ensure_ascii=False,allow_nan=False)+'\n').encode()
            if len(wire)>MAX_RESPONSE:
                wire=(json.dumps(dict(ok=False,error='response exceeds size limit; inspect task receipt'))+'\n').encode()
            self.wfile.write(wire)
        except (OSError,ValueError): pass  # Client loss never resubmits or cancels admitted work.


class TaskServer(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=True

    def __init__(self,path,store,allowed,owner_uid):
        self.store=store;self.allowed=set(allowed);self.owner_uid=owner_uid
        self.store.check_capacity=lambda:preflight(self.store)
        self.plan_lock=threading.Lock()
        super().__init__(str(path),TaskHandler)
        os.chmod(str(path),0o600)  # Same UID only; SO_PEERCRED additionally verifies each request.

    def dispatch(self, request):
        operation=request.get('op')
        keys={'ping':{'op'},'plan':{'op','params'},'start':{'op','params','request_id'},
              'deliver':{'op','params','request_id'},'status':{'op','task_id','tail'},'resume':{'op','task_id','request_id'}}
        if operation not in keys or set(request)-keys[operation]: raise ValueError('unsupported operation or fields')
        if operation=='ping':
            return dict(service='plantiragent-local-v1',owner_uid=self.owner_uid,owner=pwd.getpwuid(self.owner_uid).pw_name,
                        project=str(self.store.project),socket=str(self.server_address),capabilities=['update-result-v2','delivery-v1','manual-publication-v1'])
        if operation=='status':
            tail=request.get('tail',0)
            if type(tail) is not int or not 0<=tail<=8192: raise ValueError('invalid tail budget')
            return self.store.status(request['task_id'],tail)
        if operation=='resume':
            return self.store.resume(request['task_id'],request['request_id'])
        if operation=='deliver':return self.store.deliver(request['request_id'],request['params'])
        params=normalize_params(request['params'])
        free=preflight(self.store) if operation=='plan' else None
        if operation=='plan':
            if not self.plan_lock.acquire(blocking=False):raise RuntimeError('another preview is running')
            try:
                self.store.fingerprint(params)
                # Use temporary captures; plan is read-only with respect to scientific state.
                import tempfile
                with tempfile.TemporaryFile() as stdout,tempfile.TemporaryFile() as stderr:
                    value=subprocess.run(self.store.command(params,preview=True),cwd=str(self.store.project),
                                         stdout=stdout,stderr=stderr,timeout=240)
                    if stdout.tell()>4*1024*1024: raise ValueError('preview reply too large')
                    stdout.seek(0);body=stdout.read().decode('utf-8')
                    if value.returncode: raise RuntimeError('preview failed; verify pinned image and runtime permissions')
                    result=json_object(body)
                    if result.get('status')!='planned': raise ValueError('unexpected preview result')
                return dict(phase='planned',params=params,free_disk_bytes=free,result=result)
            finally:self.plan_lock.release()
        return self.store.start(request['request_id'],params)


def runtime_identity():
    owner=pwd.getpwnam('Zhuxiangrong')
    if os.geteuid()!=owner.pw_uid: raise RuntimeError('run task service as Zhuxiangrong')
    return owner.pw_uid,{owner.pw_uid}


def main():
    owner_uid,allowed=runtime_identity()
    os.umask(0o077)
    new_base=not (PROJECT/'state/agent').exists()
    store=TaskStore(PROJECT,launch,unit_activity)
    if new_base:store.base.chmod(0o755)
    path=store.base/'service.sock'
    if len(str(path).encode())>107: raise ValueError('Unix socket path too long')
    with (store.tasks/'api.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if path.is_symlink(): raise ValueError('symlink socket rejected')
        if path.exists():
            if not stat.S_ISSOCK(path.stat().st_mode) or path.stat().st_uid!=owner_uid:
                raise ValueError('unknown existing socket path preserved')
            path.unlink()
        server=TaskServer(path,store,allowed,owner_uid)
        def stop(*_):threading.Thread(target=server.shutdown,daemon=True).start()
        signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
        try:server.serve_forever()
        finally:
            server.server_close()
            if path.exists(): path.unlink()


if __name__=='__main__':main()
