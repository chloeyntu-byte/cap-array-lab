"""Owned, read-only Cadence metadata worker reused within one Python process.

Only catalog, symbol and cellview inspection belongs here. Simulation and design
creation keep using separate finite Cadence children. No PID persisted on disk
is ever trusted for shutdown; only Popen objects created in this process are used.
"""
import atexit
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
import uuid

from .model import ROOT


_WORKERS = {}
_WORKERS_LOCK = threading.RLock()
# A second boundary against accidentally routing mutable/native simulation code
# through the long-lived metadata interpreter. Output files are task-local only.
_MUTATION = re.compile(r'\b(?:dbCreate\w*|dbSave|dbDelete\w*|schCreate\w*|schCheck|mae\w*|asi\w*|exit)\s*\(', re.I)


def _quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'


def _event(callback, message, level='info'):
    if callback:
        callback({'kind': 'log', 'level': level, 'message': message, 'time': time.time()})


class MetadataWorker:
    def __init__(self, workspace, native_environment=None, native_virtuoso=None, native_workdir=None):
        self.native_environment = copy.deepcopy(native_environment)
        self.native_virtuoso = native_virtuoso
        self.native_workdir = str(Path(native_workdir).resolve()) if native_workdir else None
        self.workspace = str(Path(workspace).resolve())
        self.folder = ROOT / '.work' / ('metadata_worker_' + uuid.uuid4().hex[:12])
        self.folder.mkdir(parents=True, exist_ok=False)
        self.request_path = self.folder / 'request.il'
        self.stdout = self.folder / 'worker.stdout'
        self.process = None
        self.owner = os.getpid()
        self._serial = threading.Lock()
        self._process_lock = threading.Lock()
        self._closed = threading.Event()
        self._log_offset = 0

    def _start(self, callback):
        if not (Path(self.workspace) / 'cds.lib').is_file():
            raise ValueError('No cds.lib found in the selected workspace.')
        script = self.folder / 'worker.il'
        # hiRegTimer yields to the native event loop instead of blocking it with
        # an invented sleep primitive. errset(load(...)) catches parse errors too.
        script.write_text('''procedure(calMetadataPoll()
 when(isFile(%s)
  calMetadataLoaded=errset(load(%s) t)
 )
 hiRegTimer("calMetadataPoll()" 1)
)
unless(isContextLoaded("schView")
 loadContext(prependInstallPath("etc/context/schView.cxt"))
 callInitProc("schView")
)
hiRegTimer("calMetadataPoll()" 1)
''' % (_quote(self.request_path), _quote(self.request_path)), encoding='utf-8')
        env = os.environ.copy()
        # A standalone page must never inherit another page's snapshot.
        env.pop('CAPLAB_NATIVE_ENV_JSON', None)
        env.pop('CAPLAB_NATIVE_VIRTUOSO', None)
        env.pop('CAPLAB_NATIVE_CDS_LIB', None)
        if self.native_workdir:
            env['CAPLAB_NATIVE_CDS_LIB'] = str(Path(self.workspace) / 'cds.lib')
        if self.native_environment is not None:
            env['CAPLAB_NATIVE_ENV_JSON'] = json.dumps(self.native_environment)
            env['CAPLAB_NATIVE_VIRTUOSO'] = self.native_virtuoso
        for key in list(env):
            if key.startswith('CDS_MPS_') or key in ('CDS_LIB_PATH', 'CDS_LOAD_PATH'):
                env.pop(key, None)
        env['LC_ALL'] = 'en_US.utf8'
        with self._process_lock:
            if self._closed.is_set():
                raise RuntimeError('Metadata worker has been closed.')
            with self.stdout.open('w') as stream:
                self.process = subprocess.Popen(['/bin/bash', str(ROOT / 'cadence-child.sh'), str(script), str(self.folder / 'worker.log')],
                                                cwd=self.native_workdir or self.workspace, env=env, stdin=subprocess.DEVNULL,
                                                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        _event(callback, 'Started reusable metadata worker (PID {}).'.format(self.process.pid))

    def _logs(self, callback):
        try:
            with self.stdout.open('r', encoding='utf-8', errors='replace') as stream:
                stream.seek(self._log_offset)
                lines = stream.readlines()
                self._log_offset = stream.tell()
        except OSError:
            return
        for line in lines:
            text = line.strip()
            low = text.lower()
            if not text or 'helpapi' in low:
                continue
            if any(token in low for token in ('warning', '*error*', 'error:', 'fatal')):
                level = 'error' if any(token in low for token in ('*error*', 'error:', 'fatal')) else 'warning'
                _event(callback, text[:1000], level)

    def request(self, body, folder, timeout, cancelled, progress):
        if _MUTATION.search(body):
            raise ValueError('Only read-only library metadata is allowed in the metadata worker.')
        deadline = time.monotonic() + timeout
        while not self._serial.acquire(timeout=.1):
            if cancelled and cancelled.is_set():
                raise RuntimeError('Stopped by user.')
            if time.monotonic() >= deadline:
                raise RuntimeError('Timed out waiting for the metadata worker.')
        try:
            if cancelled and cancelled.is_set():
                raise RuntimeError('Stopped by user.')
            if self._closed.is_set():
                raise RuntimeError('Metadata worker has been closed.')
            if self.process is None:
                self._start(progress)
            elif self.process.poll() is not None:
                raise RuntimeError('The metadata worker exited unexpectedly; load the library again.')
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)
            token = uuid.uuid4().hex[:12]
            job = folder / ('metadata_' + token + '.il')
            ack = folder / ('metadata_' + token + '.ack')
            job.write_text('''calMetadataResult=errset(progn(
%s
) t)
calMetadataAck=outfile(%s)
if(calMetadataResult then fprintf(calMetadataAck "OK") else fprintf(calMetadataAck "ERROR"))
close(calMetadataAck)
''' % (body, _quote(ack)), encoding='utf-8')
            # Request is a tiny loader: syntax errors in the job still acknowledge
            # an error and do not strand the native interpreter waiting for input.
            loader = '''deleteFile(%s)
calMetadataParse=errset(load(%s) t)
unless(isFile(%s)
 calMetadataAck=outfile(%s) fprintf(calMetadataAck "ERROR") close(calMetadataAck)
)
''' % (_quote(self.request_path), _quote(job), _quote(ack), _quote(ack))
            tmp = self.request_path.with_suffix('.tmp')
            tmp.write_text(loader, encoding='utf-8')
            tmp.replace(self.request_path)
            started = time.monotonic()
            while True:
                try:
                    acknowledgement=ack.read_text(encoding='utf-8')
                except OSError:
                    acknowledgement=''
                if acknowledgement in ('OK','ERROR'):
                    break
                self._logs(progress)
                if self.process.poll() is not None:
                    raise RuntimeError('The metadata worker exited before returning its result.')
                if self._closed.is_set() or (cancelled and cancelled.is_set()):
                    self.close()
                    raise RuntimeError('Stopped by user.')
                if time.monotonic() >= deadline:
                    self.close()
                    raise RuntimeError('Cadence metadata operation timed out.')
                time.sleep(.04)
            self._logs(progress)
            if acknowledgement != 'OK':
                text = self.stdout.read_text(encoding='utf-8', errors='replace')
                errors = [line.strip() for line in text.splitlines() if '*Error*' in line or 'error:' in line.lower()]
                raise RuntimeError(('\n'.join(errors[-3:]) or 'Cadence could not read the requested library metadata.')[-1000:])
            _event(progress, 'Metadata ready in {:.2f} s (reused worker PID {}).'.format(time.monotonic()-started, self.process.pid))
            return self.stdout
        finally:
            self._serial.release()

    def close(self):
        self._closed.set()
        with self._process_lock:
            proc = self.process
            # A worker inherited by fork or recovered from a PID file is never
            # allowed to terminate another controller's process group.
            if self.owner != os.getpid() or not proc or proc.poll() is not None:
                return
            try:
                if os.getpgid(proc.pid) != proc.pid:
                    return
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                if proc.poll() is None and os.getpgid(proc.pid) == proc.pid:
                    os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=2)


def request(workspace, body, folder, timeout=180, cancelled=None, progress=None,
            native_environment=None, native_virtuoso=None, native_workdir=None):
    """Run metadata in a worker bound to this workspace and native snapshot."""
    workspace = str(Path(workspace).resolve())
    if native_environment is None:
        if native_virtuoso is not None:
            raise ValueError('Native executable requires an environment snapshot')
        key = workspace if not native_workdir else (workspace, None, None, str(Path(native_workdir).resolve()))
    else:
        if not isinstance(native_virtuoso, str) or not native_virtuoso or not os.path.isabs(native_virtuoso):
            raise ValueError('Native environment requires an absolute Virtuoso executable')
        from .launch_context import child_environment
        child_environment({}, native_environment)
        encoded = json.dumps(native_environment, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
        key = (workspace, native_virtuoso, hashlib.sha256(encoded.encode('utf-8')).hexdigest(), str(Path(native_workdir).resolve()) if native_workdir else None)
    with _WORKERS_LOCK:
        worker = _WORKERS.get(key)
        if worker is None or worker.owner != os.getpid() or worker._closed.is_set() or (worker.process is not None and worker.process.poll() is not None):
            worker = MetadataWorker(workspace, native_environment, native_virtuoso, native_workdir)
            _WORKERS[key] = worker
    return worker.request(body, folder, timeout, cancelled, progress)


def shutdown_all():
    """Stop only metadata children launched by this live Python process."""
    with _WORKERS_LOCK:
        workers = list(_WORKERS.values())
        _WORKERS.clear()
    for worker in workers:
        worker.close()


atexit.register(shutdown_all)
