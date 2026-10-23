"""Single-operation asynchronous facade for explicit cache maintenance requests.

GET routes inspect catalog snapshots/pages and bounded owner-lock state; they
may reconcile abandoned catalog status, but never scan or change cache files.
Actual cache changes require a concrete plan digest and confirm.
"""
import copy
import os
import re
import shutil
import threading
import uuid

from .cache_lifecycle import CacheLifecycle, LifecycleCancelled
from .engine import PredictionCancelled
from .store import ConflictError, utcnow

ID = re.compile('[a-f0-9]{32}')
DIGEST = re.compile('[a-f0-9]{64}')
KINDS = {'whole', 'leaf', 'partial', 'quarantine', 'unknown'}


def _integer(value, label, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{label} must be an integer in {minimum}..{maximum}.')
    return value


def _id(value, label):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError(label + ' must be a 32-character ID.')
    return value


class StorageService:
    def __init__(self, data_dir, manager=None):
        self.manager = manager or CacheLifecycle(data_dir)
        self.data_dir = data_dir
        self.purge_enabled = os.environ.get('PROTEIN_STUDIO_CACHE_PURGE_ENABLED') == '1'
        self._lock = threading.Lock()
        self._job = None
        self._cancel = threading.Event()
        self._thread = None
        self._closed = False

    def snapshot(self):
        with self._lock:
            job = copy.deepcopy(self._job)
        try:
            usage = shutil.disk_usage(self.manager.cache_root)
            disk = dict(freeBytes=usage.free, totalBytes=usage.total, error=None)
        except OSError as exc:
            disk = dict(freeBytes=None, totalBytes=None, error=str(exc)[:500])
        return dict(scope='preparation-cache',
                    capabilities=dict(scan=True, quarantine=True, restore=True, purge=self.purge_enabled),
                    state=self.manager.snapshot(), job=job, disk=disk)

    def start(self, action, payload):
        if not isinstance(payload, dict):
            raise ValueError('Storage operation must be a JSON object.')
        payload = copy.deepcopy(payload)
        if action == 'scan':
            if set(payload) - {'maxEntries', 'maxSeconds'}:
                raise ValueError('Unsupported scan fields.')
            arguments = dict(max_entries=_integer(payload.get('maxEntries', 100000), 'maxEntries', 1, 1000000),
                             max_seconds=_integer(payload.get('maxSeconds', 30), 'maxSeconds', 1, 300))
        elif action == 'plan':
            if set(payload) - {'action', 'scanId', 'minAgeSeconds', 'retentionSeconds', 'kinds', 'quarantineId'}:
                raise ValueError('Unsupported plan fields.')
            kind = payload.get('action', 'quarantine')
            if kind not in {'quarantine', 'purge', 'restore'}:
                raise ValueError('Unknown cache plan action.')
            if kind == 'purge' and not self.purge_enabled:
                raise ValueError('Permanent cache purge is disabled on this controller.')
            arguments = dict(action=kind,
                             min_age_seconds=_integer(payload.get('minAgeSeconds', 7 * 86400), 'minAgeSeconds', 0, 10 * 365 * 86400),
                             retention_seconds=_integer(payload.get('retentionSeconds', 7 * 86400), 'retentionSeconds', 0, 10 * 365 * 86400))
            if payload.get('scanId') is not None:
                arguments['scan_id'] = _id(payload['scanId'], 'scanId')
            if payload.get('quarantineId') is not None:
                arguments['quarantine_id'] = _id(payload['quarantineId'], 'quarantineId')
            if 'kinds' in payload:
                kinds = payload['kinds']
                if not isinstance(kinds, list) or not kinds or any(not isinstance(k, str) or k not in KINDS for k in kinds):
                    raise ValueError('kinds must contain known cache categories.')
                arguments['kinds'] = kinds
        elif action == 'apply':
            if set(payload) - {'planId', 'digest', 'confirm'}:
                raise ValueError('Unsupported apply fields.')
            digest = payload.get('digest')
            if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
                raise ValueError('A concrete plan SHA256 digest is required.')
            if payload.get('confirm') is not True:
                raise ValueError('Confirm this concrete cache plan before applying it.')
            plan_id = _id(payload.get('planId'), 'planId')
            details = self.manager.plan_details(plan_id, limit=1, offset=0)
            if details['plan'].get('digest') != digest:
                raise ConflictError('The supplied digest does not match this concrete cache plan.')
            if details['plan']['action'] == 'purge' and not self.purge_enabled:
                raise ValueError('Permanent cache purge is disabled on this controller.')
            arguments = dict(plan_id=plan_id, digest=digest, confirm=True)
        else:
            raise ValueError('Unknown storage operation.')
        with self._lock:
            if self._closed:
                raise ConflictError('Cache maintenance service is stopping.')
            if self._thread is not None and self._thread.is_alive():
                raise ConflictError('A cache maintenance operation is already running.')
            self._cancel = threading.Event()
            self._job = dict(id=uuid.uuid4().hex, action=action, status='running', createdAt=utcnow(),
                             finishedAt=None, result=None, error=None, cancelRequested=False)
            self._thread = threading.Thread(target=self._run, args=(action, arguments, self._cancel),
                                            name='cache-maintenance', daemon=True)
            result = copy.deepcopy(self._job)
            self._thread.start()
            return result

    def _run(self, action, arguments, cancelled):
        try:
            result = getattr(self.manager, action)(**arguments, cancelled=cancelled.is_set)
            outcome = result.get('status')
            status = ('cancelled' if outcome in {'cancelled', 'canceled'}
                      else 'failed' if outcome in {'failed', 'partial'} else 'completed')
            errors = [item.get('message', '') for item in result.get('diagnostics', []) if isinstance(item, dict)]
            error = ('; '.join(errors[:3]) or 'Cache operation did not complete all requested changes.')[:2000] if status == 'failed' else None
        except (PredictionCancelled, LifecycleCancelled):
            result, status, error = None, 'cancelled', None
        except Exception as exc:
            result, status, error = None, 'failed', str(exc)[:2000]
        with self._lock:
            self._job.update(status=status, result=result, error=error, finishedAt=utcnow())

    def cancel(self, job_id):
        _id(job_id, 'jobId')
        with self._lock:
            if self._job is None or self._job['id'] != job_id:
                raise KeyError('Storage operation not found.')
            if self._job['status'] == 'running':
                self._cancel.set()
                self._job['cancelRequested'] = True
            return copy.deepcopy(self._job)

    def close(self):
        with self._lock:
            self._closed = True
            self._cancel.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=6)
        return thread is None or not thread.is_alive()
