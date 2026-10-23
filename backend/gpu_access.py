"""Cooperative, node-local device exclusion for search and inference processes.

All participating processes must use the same lock directory and the same
physical CUDA identifiers on a node. UUIDs avoid container/local-index aliases.
This is not a cluster scheduler or a lock for unrelated external GPU programs.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import tempfile
import threading


class GPUDevicesBusy(RuntimeError):
    pass


class DeviceGroup(list):
    """List-compatible visible devices plus FDs retained by GPU subprocesses."""
    def __init__(self, devices, descriptors):
        super().__init__(devices)
        self.lock_fds = tuple(descriptors)


def normalize_group(group):
    if not isinstance(group, (tuple, list)) or not 1 <= len(group) <= 256:
        raise ValueError('A GPU device group must be a nonempty bounded list.')
    result = []
    for value in group:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError('GPU devices must use explicit CUDA identifiers.')
        value = str(value)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_\-/]*', value) or value in result:
            raise ValueError('GPU identifiers must be valid and unique within a group.')
        result.append(value)
    return result


def _check(cancelled):
    if cancelled():
        from .engine import PredictionCancelled
        raise PredictionCancelled('GPU device acquisition cancelled.')


def _root():
    default = Path(tempfile.gettempdir()) / ('foldspace-gpu-locks-' + str(os.getuid()))
    root = Path(os.environ.get('PROTEIN_STUDIO_GPU_LOCK_DIR') or default).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _release(descriptors):
    import fcntl
    for descriptor in reversed(descriptors):
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _try_acquire(group, root):
    import fcntl
    acquired = []
    try:
        # Canonical ordering plus nonblocking acquisition prevents a group from
        # holding one GPU while waiting indefinitely for another GPU.
        for device in sorted(group):
            filename = hashlib.sha256(device.encode()).hexdigest() + '.lock'
            flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
            descriptor = os.open(root / filename, flags, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                os.close(descriptor)
                raise
            acquired.append(descriptor)
        return acquired
    except BlockingIOError:
        _release(acquired)
        return None
    except BaseException:
        _release(acquired)
        raise


@contextmanager
def gpu_group_lease(group, cancelled=lambda: False, *, blocking=True):
    """Hold every named device until the protected GPU process has stopped."""
    if os.name != 'posix':
        raise ValueError('Shared GPU device leases require POSIX advisory locks.')
    group = normalize_group(group)
    _check(cancelled)
    root = _root()
    descriptors = None
    while descriptors is None:
        _check(cancelled)
        descriptors = _try_acquire(group, root)
        if descriptors is None:
            if not blocking:
                raise GPUDevicesBusy('The requested GPU device group is in use on this node.')
            threading.Event().wait(.1)
    try:
        _check(cancelled)
        yield DeviceGroup(group, descriptors)
    finally:
        _release(descriptors)


@contextmanager
def try_gpu_group(groups, cancelled=lambda: False):
    """Try each explicitly configured group once and hold the first free group.

    Busy search preparation can durably yield its target/CPU reservation instead
    of waiting inside this context. No CUDA runtime or GPU hardware is queried.
    """
    if os.name != 'posix':
        raise ValueError('Shared GPU device leases require POSIX advisory locks.')
    if not isinstance(groups, (list, tuple)) or not 1 <= len(groups) <= 256:
        raise ValueError('GPU device groups must be an explicit bounded list.')
    normalized = [normalize_group(group) for group in groups]
    seen = set()
    for group in normalized:
        if seen.intersection(group):
            raise ValueError('Configured GPU groups may not overlap.')
        seen.update(group)
    _check(cancelled)
    root = _root()
    for group in normalized:
        _check(cancelled)
        descriptors = _try_acquire(group, root)
        if descriptors is None:
            continue
        try:
            _check(cancelled)
            yield DeviceGroup(group, descriptors)
        finally:
            _release(descriptors)
        return
    raise GPUDevicesBusy('All configured search GPU groups are in use on this node.')
