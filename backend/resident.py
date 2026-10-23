"""Long-lived JSONL model-process client; one serialized batch at a time.

No target starts a new model subprocess. A dead, cancelled or timed-out process
is marked broken and requires an explicit restart. Partial completed results are
attached to the raised exception, so a caller can retry only unresolved targets.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

from .engine import PredictionCancelled, _stop

MAX_LINE = 1024 * 1024
MAX_REQUEST = 8 * 1024 * 1024


class ResidentProcessError(RuntimeError):
    def __init__(self, message, completed=None, pending_target_ids=None):
        super().__init__(message)
        self.completed = list(completed or [])
        self.pending_target_ids = list(pending_target_ids or [])


class ResidentCancelled(PredictionCancelled):
    def __init__(self, message, completed=None, pending_target_ids=None):
        super().__init__(message)
        self.completed = list(completed or [])
        self.pending_target_ids = list(pending_target_ids or [])


class ResidentProcess:
    def __init__(self, command, work_root, env=None, startup_timeout=120, request_timeout=7200):
        if not isinstance(command, (list, tuple)) or not command or not all(isinstance(v, str) and v and '\0' not in v for v in command):
            raise ValueError('Resident command must be a nonempty argv array.')
        self.command = list(command)
        self.work_root = Path(work_root).resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.env = dict(env or {})
        if any(not isinstance(k, str) or not isinstance(v, str) or '\0' in k + v for k, v in self.env.items()):
            raise ValueError('Resident environment overrides must be string mappings.')
        self.startup_timeout = float(startup_timeout)
        self.request_timeout = float(request_timeout)
        if self.startup_timeout <= 0 or self.request_timeout <= 0:
            raise ValueError('Resident timeouts must be positive.')
        self.process = None
        self.ready = None
        self.broken = False
        self.closed = False
        self.generation = 0
        self._events = None
        self._reader = None
        self._log = None
        self._lock = threading.RLock()
        # Worker-held device locks are inherited by the actual model process.
        # If the client dies first, an orphan model still retains exclusion.
        self.inherited_fds = ()

    @property
    def pid(self):
        return self.process.pid if self.process is not None else None

    def _pump(self, stream, events):
        try:
            while True:
                line = stream.readline(MAX_LINE + 1)
                if not line:
                    events.put(('eof', None))
                    return
                if len(line) > MAX_LINE or not line.endswith(b'\n'):
                    events.put(('error', 'Resident emitted an oversized or unterminated JSONL event.'))
                    return
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError('not an object')
                except (ValueError, UnicodeDecodeError):
                    events.put(('error', 'Resident stdout contained invalid protocol JSON. Check stderr for model logs.'))
                    return
                events.put(('event', event))
        except (OSError, ValueError) as exc:
            events.put(('error', str(exc)))

    def _next(self, deadline, cancelled):
        while True:
            if cancelled and cancelled():
                raise ResidentCancelled('Resident batch cancelled; process terminated, explicit restart required.')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ResidentProcessError('Resident operation timed out; explicit restart required.')
            try:
                kind, payload = self._events.get(timeout=min(.1, remaining))
            except queue.Empty:
                continue
            if kind == 'eof':
                raise ResidentProcessError('Resident process exited before completing the protocol; explicit restart required.')
            if kind == 'error':
                raise ResidentProcessError(payload)
            return payload

    def _terminate(self):
        if self.process is not None:
            _stop(self.process)
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()
        if self._reader and self._reader is not threading.current_thread():
            self._reader.join(timeout=1)
        if self._log:
            self._log.close()
            self._log = None

    def start(self, cancelled=None):
        with self._lock:
            if self.closed:
                raise ResidentProcessError('Resident client is closed; create a new client.')
            if self.broken:
                raise ResidentProcessError('Resident is broken; call restart() explicitly before new work.')
            if self.process is not None:
                if self.process.poll() is not None:
                    self.broken = True
                    raise ResidentProcessError('Resident process died; call restart() explicitly.')
                return self.ready
            self.generation += 1
            self._events = queue.Queue()
            log_path = self.work_root / f'resident-{self.generation}.stderr.log'
            self._log = log_path.open('ab')
            environment = os.environ.copy()
            environment.update(self.env)
            try:
                self.process = subprocess.Popen(self.command, cwd=self.work_root, env=environment,
                                                pass_fds=self.inherited_fds,
                                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log,
                                                shell=False, start_new_session=os.name == 'posix', bufsize=0)
                self._reader = threading.Thread(target=self._pump, args=(self.process.stdout, self._events), daemon=True)
                self._reader.start()
                event = self._next(time.monotonic() + self.startup_timeout, cancelled)
                if event.get('event') != 'ready' or event.get('protocolVersion') != 1:
                    raise ResidentProcessError('Resident startup failed: ' + str(event.get('error') or event))
                self.ready = dict(event, clientGeneration=self.generation, stderrLog=str(log_path))
                return self.ready
            except BaseException:
                self.broken = True
                self._terminate()
                raise

    def restart(self, cancelled=None):
        with self._lock:
            if self.closed:
                raise ResidentProcessError('Closed clients cannot be restarted.')
            self._terminate()
            self.process = None
            self.ready = None
            self.broken = False
            return self.start(cancelled)

    def close(self):
        with self._lock:
            self._terminate()
            self.closed = True

    def predict_batch(self, targets, cancelled=None, emit=None, load_results=False):
        """Run targets, retaining only result-file pointers by default.

        load_results=True is for small interactive/testing uses; large PAE
        matrices otherwise remain on disk and never accumulate across a batch.
        """
        cancelled = cancelled or (lambda: False)
        emit = emit or (lambda event: None)
        if not isinstance(targets, list) or not 1 <= len(targets) <= 256:
            raise ValueError('Resident batch requires 1–256 targets.')
        target_map = {}
        for target in targets:
            if not isinstance(target, dict) or not isinstance(target.get('targetId'), str) or not target['targetId']:
                raise ValueError('Every resident target requires a nonempty targetId.')
            if target['targetId'] in target_map:
                raise ValueError('Target IDs must be unique within a resident request.')
            if not isinstance(target.get('job'), dict):
                raise ValueError('Every resident target requires its normalized job.')
            for field in ('preparedPath', 'outputDir'):
                if not isinstance(target.get(field), str) or not Path(target[field]).is_absolute():
                    raise ValueError(f'Resident target {field} must be an absolute path.')
            target_map[target['targetId']] = target
        request = dict(id=uuid.uuid4().hex, operation='predict', targets=targets)
        encoded = (json.dumps(request, allow_nan=False, ensure_ascii=False) + '\n').encode()
        if len(encoded) > MAX_REQUEST:
            raise ValueError('Resident request exceeds the 8 MiB protocol limit; use smaller batches.')
        results, seen = [], set()
        with self._lock:
            try:
                self.start(cancelled)
                remaining_bytes = memoryview(encoded)
                while remaining_bytes:
                    written = self.process.stdin.write(remaining_bytes)
                    if not written:
                        raise ResidentProcessError('Resident request pipe closed before the full request was written.')
                    remaining_bytes = remaining_bytes[written:]
                self.process.stdin.flush()
                deadline = time.monotonic() + self.request_timeout
                retiring = False
                while True:
                    event = self._next(deadline, cancelled)
                    if event.get('id') != request['id']:
                        raise ResidentProcessError('Resident response request ID did not match the active batch.')
                    kind = event.get('event')
                    if kind == 'batch_complete':
                        if seen != set(target_map):
                            raise ResidentProcessError('Resident batch completed without a terminal event for every target.')
                        emit(event)
                        if retiring:
                            self.broken = True
                            self._terminate()
                        return results
                    if kind == 'worker_retiring':
                        retiring = True
                        emit(event)
                        continue
                    if kind == 'request_failed':
                        raise ResidentProcessError(str(event.get('error', 'Resident rejected request.')))
                    target_id = event.get('targetId')
                    if target_id not in target_map:
                        raise ResidentProcessError('Resident responded for an unknown target.')
                    if kind == 'target_started':
                        emit(event)
                        continue
                    if kind not in ('target_complete', 'target_failed') or target_id in seen:
                        raise ResidentProcessError('Resident emitted an invalid or duplicate target event.')
                    if kind == 'target_complete':
                        original_path = Path(event.get('resultJson', ''))
                        path = original_path.resolve()
                        output = Path(target_map[target_id]['outputDir']).resolve()
                        if not path.is_relative_to(output) or not path.is_file() or original_path.is_symlink() or path.stat().st_size == 0:
                            raise ResidentProcessError('Resident result JSON escaped its target output directory or is missing.')
                        item = dict(targetId=target_id, status='completed', resultJson=str(path),
                                    resultBytes=path.stat().st_size, pid=self.pid, generation=self.generation)
                        if load_results:
                            result = json.loads(path.read_text())
                            if not isinstance(result, dict):
                                raise ResidentProcessError('Resident result JSON is not an object.')
                            item['result'] = result
                        results.append(item)
                    else:
                        results.append(dict(targetId=target_id, status='failed', error=str(event.get('error', 'Unknown model error')),
                                            retryable=bool(event.get('retryable')), pid=self.pid, generation=self.generation))
                    seen.add(target_id)
                    emit(dict(event, type='target_result', **results[-1]))
            except BaseException as exc:
                self.broken = True
                self._terminate()
                pending = [target['targetId'] for target in targets if target['targetId'] not in seen]
                if isinstance(exc, (ResidentProcessError, ResidentCancelled)):
                    exc.completed = results
                    exc.pending_target_ids = pending
                    raise
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise ResidentProcessError(str(exc), results, pending) from exc

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()
