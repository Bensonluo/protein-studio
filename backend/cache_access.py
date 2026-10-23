"""Cooperative POSIX maintenance locks for controller preparation caches.

Use maintenance -> whole-key -> chain-key -> short SQLite transaction ordering.
Shared access is reentrant within a thread, including across callers/instances.
Writers stop new local readers before waiting for existing ones to finish. The
intent file keeps new cooperating processes out while an OS writer drains reads.
These locks are not a distributed filesystem lease service.
"""
from contextlib import contextmanager
import fcntl
import math
from pathlib import Path
import threading
import time

from .engine import PredictionCancelled

_registry_lock = threading.Lock()
_registry = {}
_local = threading.local()
_POLL = .05


class _State:
    def __init__(self):
        self.condition = threading.Condition()
        self.readers = 0
        self.writer = False
        self.waiting_writers = 0


def _check(cancelled, deadline):
    if cancelled():
        raise PredictionCancelled('Cache maintenance access cancelled.')
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError('Timed out waiting for cache maintenance access.')


def _flock(stream, mode, cancelled, deadline):
    while True:
        _check(cancelled, deadline)
        try:
            fcntl.flock(stream.fileno(), mode | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            threading.Event().wait(_POLL)


@contextmanager
def _access(cache_root, exclusive, cancelled, timeout):
    root = Path(cache_root).resolve()
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('Cache maintenance timeout must be positive and finite.')
    deadline = None if timeout is None else time.monotonic() + timeout
    _check(cancelled, deadline)
    held = getattr(_local, 'held', None)
    if held is None:
        held = _local.held = {}
    previous = held.get(root)
    if previous:
        if exclusive and not previous['exclusive']:
            raise ValueError('Cannot upgrade shared cache access to exclusive maintenance access.')
        previous['depth'] += 1
        try:
            yield
        finally:
            previous['depth'] -= 1
        return
    with _registry_lock:
        state = _registry.setdefault(root, _State())
    registered = False
    intent = main = None
    try:
        with state.condition:
            if exclusive:
                state.waiting_writers += 1
                try:
                    while state.writer or state.readers:
                        _check(cancelled, deadline)
                        state.condition.wait(_POLL)
                    _check(cancelled, deadline)
                    state.writer = True
                    registered = True
                finally:
                    state.waiting_writers -= 1
                    state.condition.notify_all()
            else:
                while state.writer or state.waiting_writers:
                    _check(cancelled, deadline)
                    state.condition.wait(_POLL)
                _check(cancelled, deadline)
                state.readers += 1
                registered = True
        root.mkdir(parents=True, exist_ok=True)
        intent_path, main_path = root / '.maintenance.intent.lock', root / '.maintenance.lock'
        if intent_path.is_symlink() or main_path.is_symlink():
            raise ValueError('Cache maintenance lock files must not be symbolic links.')
        intent, main = intent_path.open('a+b'), main_path.open('a+b')
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        _flock(intent, mode, cancelled, deadline)
        _flock(main, mode, cancelled, deadline)
        if not exclusive:
            fcntl.flock(intent.fileno(), fcntl.LOCK_UN)
        held[root] = {'exclusive': exclusive, 'depth': 1}
        try:
            yield
        finally:
            del held[root]
    finally:
        # Close releases even when cancellation/timeout interrupts acquisition.
        if main is not None:
            main.close()
        if intent is not None:
            intent.close()
        if registered:
            with state.condition:
                if exclusive:
                    state.writer = False
                else:
                    state.readers -= 1
                state.condition.notify_all()


@contextmanager
def shared_cache(cache_root, cancelled=lambda: False, timeout=None):
    """Protect cache reads and prepare-to-ledger publication from maintenance."""
    with _access(cache_root, False, cancelled, timeout):
        yield


@contextmanager
def exclusive_cache(cache_root, cancelled=lambda: False, timeout=None):
    """Drain cooperating readers before scanning or changing cache ownership."""
    with _access(cache_root, True, cancelled, timeout):
        yield
