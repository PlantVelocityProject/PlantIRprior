#!/usr/bin/env python3
"""Supervise the fixed asynchronous Qtrans client until transfer termination."""
import argparse
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import ProxyHandler, Request, build_opener

BLOCKED_EXIT = 75
TIMEOUT_EXIT = 124
TERMINAL = {'completed', 'failed', 'paused', 'stopped', 'cancelled'}
CACHED_TERMINAL = TERMINAL | {'已完成', '已停止', '已暂停', '失败', '下载失败'}


class DownloadBlocked(RuntimeError):
    """A shared service/ownership problem must pause further raw downloads."""


class ServiceUnavailable(DownloadBlocked):
    pass


class TaskMissing(DownloadBlocked):
    pass


class TransferTimeout(RuntimeError):
    pass


class SubmissionUncertain(DownloadBlocked):
    pass


class QtransDownload:
    def __init__(self, binary, work, remotes, timeout, cancelled, runtime=None,
                 poll_interval=.25, cleanup_timeout=10, raw_root=None):
        self.binary = str(binary)
        self.work = Path(work).resolve()
        self.remotes = list(remotes)
        self.timeout = timeout
        self.cancelled = cancelled
        self.runtime = Path(runtime) if runtime else Path.home() / '.qtrans'
        self.raw_root = Path(raw_root) if raw_root else None
        self.poll_interval = poll_interval
        self.cleanup_timeout = cleanup_timeout
        self.opener = build_opener(ProxyHandler({}))  # Local control never uses an outbound proxy.
        self.deadline = None
        self.owned = set()
        self.submitting = None
        self.cleanup_required = False
        self.service_processes = set()

    def request(self, service, path, method='GET'):
        try:
            port = int((self.runtime / 'bin' / (service + '.port')).read_text().strip())
            if not 1 <= port <= 65535: raise ValueError('invalid port')
            request = Request('http://127.0.0.1:{}{}'.format(port, path),
                              data=b'' if method == 'POST' else None, method=method)
            with self.opener.open(request, timeout=2) as response:
                data = response.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024: raise ValueError('response too large')
            result = json.loads(data)
            if not isinstance(result, dict): raise ValueError('expected object')
            if service == 'engine' and result.get('code') != 200:
                raise ValueError('engine rejected request')
            if service == 'daemon' and result.get('message') != 'success' and 'message' not in result:
                raise ValueError('invalid client response')
            return result
        except HTTPError as exc:
            with exc:
                try: detail = json.loads(exc.read(4096))
                except (ValueError, OSError): detail = {}
            if service == 'engine' and '/tasks/' in path and (exc.code == 404 or (
                    isinstance(detail, dict) and 'task not found' in str(detail.get('message', '')).lower())):
                raise TaskMissing('Qtrans engine has no matching task') from exc
            raise DownloadBlocked('Qtrans {} control rejected request (HTTP {})'.format(service, exc.code)) from exc
        except (OSError, URLError) as exc:
            raise ServiceUnavailable('Qtrans {} control unavailable'.format(service)) from exc
        except ValueError as exc:
            raise DownloadBlocked('Qtrans {} returned an invalid control response'.format(service)) from exc

    def check(self):
        if self.cancelled.is_set(): raise RuntimeError('interrupted')
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise TransferTimeout('qtrans download timeout after {} seconds'.format(self.timeout))

    def command(self, arguments):
        # The persistent services must never inherit the raw-directory lock.
        self.check()
        process = subprocess.Popen([self.binary] + list(arguments), close_fds=True,
                                   cwd=str(self.runtime.parent))
        deadline = time.monotonic() + 10
        submission = arguments[0] == 'download'
        try:
            while process.poll() is None:
                if not submission: self.check()
                if time.monotonic() >= deadline:
                    if submission: raise SubmissionUncertain('Qtrans submission did not respond')
                    raise DownloadBlocked('Qtrans command did not respond')
                # Let an in-flight submission reply before trying to stop it.
                time.sleep(.05)
            if process.returncode: raise RuntimeError('qtrans command exited with code {}'.format(process.returncode))
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=1)
                except subprocess.TimeoutExpired: process.kill(); process.wait()

    def tasks(self):
        response = self.request('daemon', '/api/task/list')
        if response.get('message') != 'success' or 'tasks' not in response:
            raise DownloadBlocked('Qtrans returned an invalid task list')
        tasks = response['tasks']
        if tasks is None: return []
        if not isinstance(tasks, list) or not all(isinstance(t, dict) for t in tasks):
            raise DownloadBlocked('Qtrans returned an invalid task list')
        return tasks

    def services_ready(self):
        try:
            self.tasks()
            data = self.request('engine', '/api/v1/health').get('data')
            return isinstance(data, dict) and data.get('me') == 'qtransc-engine'
        except ServiceUnavailable:
            return False

    @staticmethod
    def process_start(pid):
        try:
            name, info = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(') ', 1)
            fields = info.split()
            if name.split('(', 1)[-1] in {'qtransc', 'qtransc-engine'} and fields[0] != 'Z':
                return fields[19]
        except (FileNotFoundError, ProcessLookupError):
            pass
        return None

    def live_processes(self):
        found = set()
        for service in ('daemon', 'engine'):
            try: pid = int((self.runtime/'bin'/(service + '.pid')).read_text().strip())
            except (FileNotFoundError, ValueError): continue
            start = self.process_start(pid) if pid > 1 else None
            if start: found.add((pid, start))
        return found

    def processes_gone(self):
        return bool(self.service_processes) and not self.live_processes() and all(
            self.process_start(pid) != start for pid, start in self.service_processes)

    @staticmethod
    def in_raw_root(destination, raw_root):
        if not isinstance(destination, str) or not destination: return False
        return Path(raw_root) in Path(destination).parents

    def unfinished_live_tasks(self, raw_root, selected=()):
        unfinished = set()
        for task in self.tasks():
            dest, source = task.get('Destination'), task.get('Source')
            if task.get('Type') not in {'下载', 'download'} or not self.in_raw_root(dest, raw_root): continue
            if (source, dest) in selected: continue
            code = task.get('Code')
            if not isinstance(code, str) or not code: raise DownloadBlocked('Qtrans task has no usable code')
            try:
                if self.summary(code)['status'] in TERMINAL: continue
            except TaskMissing:
                if task.get('Status') in CACHED_TERMINAL: continue
            unfinished.add((source, dest))
        return unfinished

    def check_raw_writers(self, raw_root, selected):
        # An interrupted supervisor may have lost its lock while a writer lives.
        if self.live_processes() and self.unfinished_live_tasks(raw_root, selected):
            raise DownloadBlocked('another Qtrans task may still be writing to this raw directory')

    def unfinished_saved_tasks(self, raw_root):
        path = self.runtime / 'data/qtransc.db'
        if not path.is_file(): return set()
        try:
            db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
            try:
                return {(source, dest) for source, dest, kind, status in
                        db.execute('SELECT Source,Destination,Type,Status FROM tasks')
                        if kind in {'下载', 'download'} and self.in_raw_root(dest, raw_root)
                        and status not in CACHED_TERMINAL}
            finally: db.close()
        except sqlite3.Error as exc:
            raise DownloadBlocked('Qtrans saved tasks could not be checked') from exc

    def check_saved_tasks(self):
        # Startup can recover all saved downloads. Keep it inside this sample.
        if self.raw_root is None: return
        selected = {(r, str(self.work / PurePosixPath(r).name)) for r in self.remotes}
        if self.unfinished_saved_tasks(self.raw_root) - selected:
            raise DownloadBlocked('an unfinished Qtrans transfer must be recovered before starting other downloads')

    def ensure_services(self):
        if self.services_ready(): return
        # Serialize cold starts without passing this lock to the daemon either.
        folder = self.runtime / 'bin'; folder.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 10
        lock = os.open(str(folder), os.O_RDONLY)
        try:
            while True:
                self.check()
                try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except BlockingIOError:
                    if time.monotonic() >= deadline: raise DownloadBlocked('Qtrans service startup is occupied')
                    self.cancelled.wait(self.poll_interval)
            if self.services_ready(): return
            self.check_saved_tasks()
            try: self.command(['engine', 'start'])
            except RuntimeError as exc:
                self.check()
                raise DownloadBlocked('Qtrans service could not start: ' + str(exc)) from exc
            while time.monotonic() < deadline:
                self.check()
                if self.services_ready(): return
                self.cancelled.wait(self.poll_interval)
        finally:
            os.close(lock)
        raise DownloadBlocked('Qtrans service was not ready before the startup deadline')

    def matches(self, remote, include_completed=True):
        destination = str(self.work / PurePosixPath(remote).name)
        found = []
        for task in self.tasks():
            if task.get('Destination') != destination: continue
            same_request = task.get('Source') == remote and task.get('Type') in {'下载', 'download'}
            code = task.get('Code')
            if not isinstance(code, str) or not code: raise DownloadBlocked('Qtrans task has no usable code')
            if task.get('Status') == '已完成' and (not same_request or not include_completed):
                try:
                    if self.summary(code)['status'] in TERMINAL: continue
                except TaskMissing:
                    continue
                task = dict(task, Status='下载中')
            if not same_request:
                raise DownloadBlocked('Qtrans destination belongs to another request; preserve existing files')
            found.append(task)
        active = [t for t in found if t.get('Status') != '已完成']
        if active: found = active
        elif found: found = [max(found, key=lambda t: str(t.get('CreatedAt', '')))]
        if len(found) > 1: raise DownloadBlocked('Multiple Qtrans tasks own the same destination')
        return found

    def summary(self, code):
        data = self.request('engine', '/api/v1/tasks/' + quote(code, safe='') + '/summary').get('data')
        if (not isinstance(data, dict) or data.get('task_id') != code or data.get('mode') != 'download'
                or not isinstance(data.get('status'), str)):
            raise DownloadBlocked('Qtrans returned an invalid task summary')
        return data

    def action(self, code, action):
        self.request('daemon', '/api/task/' + action + '?' + urlencode({'task_code': code}), 'POST')

    def await_summary(self, code):
        deadline = time.monotonic() + 3
        while True:
            self.check()
            try: return self.summary(code)
            except TaskMissing:
                if time.monotonic() >= deadline: raise
                self.cancelled.wait(self.poll_interval)

    def transfer(self, remote):
        self.check()
        target = self.work / PurePosixPath(remote).name
        found = self.matches(remote, include_completed=target.is_file())
        existing = bool(found)
        if not found:
            self.submitting = remote
            failure = None
            try: self.command(['download', remote, str(self.work)])
            except RuntimeError as exc: failure = exc
            # A lost/duplicate submission reply is reconciled against exact ownership.
            found = self.matches(remote, include_completed=target.is_file())
            if not found:
                if failure and not isinstance(failure, SubmissionUncertain):
                    self.submitting = None
                    raise failure
                raise SubmissionUncertain('Qtrans submission could not be matched to a task')
        task = found[0]; code = task['Code']; self.owned.add(code)
        self.submitting = None
        try:
            state = self.await_summary(code)
        except TaskMissing:
            # Recovered completed records may no longer have an engine task.
            # Accept only the recorded byte count; FASTQ validation still follows.
            size = task.get('TotalSize')
            if task.get('Status') == '已完成' and type(size) is int and size > 0 and target.is_file() \
                    and target.stat().st_size == size == task.get('Downloaded'):
                self.owned.discard(code)
                return
            raise
        if existing and state['status'] in {'paused', 'stopped', 'cancelled', 'failed'}:
            self.action(code, 'start')
            # The wrapper schedules resume asynchronously; tolerate its previous state briefly.
            resume_deadline = time.monotonic() + 2
            while state['status'] in {'paused', 'stopped', 'cancelled', 'failed'}:
                self.check()
                if time.monotonic() >= resume_deadline: break
                self.cancelled.wait(self.poll_interval); state = self.summary(code)
        while True:
            self.check()
            if state['status'] == 'completed':
                self.owned.discard(code)
                return
            if state['status'] in TERMINAL:
                raise RuntimeError('Qtrans transfer {}: {}'.format(state['status'], state.get('error', 'task stopped')))
            if state['status'] not in {'pending', 'running'}:
                raise DownloadBlocked('Qtrans returned an unknown transfer state')
            self.cancelled.wait(self.poll_interval); state = self.summary(code)

    def stop(self):
        # Reconcile a submission even when its reply or the first lookup was lost.
        if self.submitting:
            found = self.matches(self.submitting)
            if not found: raise DownloadBlocked('Qtrans submission is still unconfirmed')
            for task in found: self.owned.add(task['Code'])
            self.submitting = None
        deadline = time.monotonic() + self.cleanup_timeout
        for code in sorted(self.owned):
            try:
                if self.summary(code)['status'] in TERMINAL: continue
            except TaskMissing:
                pass
            self.action(code, 'stop')
            try:
                if self.summary(code)['status'] not in TERMINAL:
                    self.request('engine', '/api/v1/tasks/' + quote(code, safe='') + '/pause', 'POST')
            except TaskMissing:
                # The wrapper has stopped scheduling this task and the engine
                # has no writer for it.
                continue
            while self.summary(code)['status'] not in TERMINAL:
                if time.monotonic() >= deadline:
                    raise DownloadBlocked('Qtrans stop was not confirmed; raw downloads remain blocked')
                time.sleep(self.poll_interval)

    def run(self):
        self.deadline = time.monotonic() + self.timeout if self.timeout else None
        self.ensure_services()
        self.service_processes = self.live_processes()
        try:
            for remote in self.remotes: self.transfer(remote)
        except BaseException:
            if self.owned or self.submitting:
                try: self.stop()
                except (RuntimeError, OSError) as exc:
                    self.cleanup_required = True
                    raise DownloadBlocked('Qtrans stop could not be confirmed; preserve partial files and check the service') from exc
            raise


def retain_cleanup_lock(client):
    """An uncertain writer retains the inherited lock until it is safely stopped."""
    pid = os.fork()
    if pid: return pid
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    while True:
        try:
            client.stop()
            os._exit(0)
        except (RuntimeError, OSError):
            try:
                if client.processes_gone(): os._exit(0)
            except OSError:
                pass
            time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--raw-root', type=Path)
    parser.add_argument('--remote', action='append', required=True)
    parser.add_argument('--timeout', type=float, default=0)
    args = parser.parse_args()
    cancelled = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM): signal.signal(sig, lambda *_: cancelled.set())
    client = QtransDownload(args.binary, args.directory, args.remote, args.timeout, cancelled, raw_root=args.raw_root)
    try:
        client.run()
        return 0
    except (RuntimeError, OSError) as exc:
        blocked = isinstance(exc, DownloadBlocked)
        if blocked and client.cleanup_required:
            pid = retain_cleanup_lock(client)
            print('Qtrans cleanup supervisor retains download ownership (pid {})'.format(pid), file=sys.stderr, flush=True)
        print(str(exc), file=sys.stderr, flush=True)
        return BLOCKED_EXIT if blocked else TIMEOUT_EXIT if isinstance(exc, TransferTimeout) else 1


if __name__ == '__main__': sys.exit(main())
