"""Local preparation admission using explicit administrator resource estimates.

Reservations are not CPU/RSS enforcement or filesystem quotas. They cover only
this controller's preparation workers. No GPU lease is acquired here.
"""
from __future__ import annotations

from collections import deque
import copy
import json
import math
import os
from pathlib import Path
import shutil
import threading
import time
import uuid

ENV_NAME = 'PROTEIN_STUDIO_PREPARATION_RESOURCES_JSON'
FIELDS = {'cpuBudget', 'cpuPerPrepare', 'memoryBudgetBytes', 'memoryPerPrepareBytes',
          'diskMinFreeBytes', 'diskPerPrepareBytes'}


def parse_config(value=None):
    """None reads the environment; an explicit empty object disables limits."""
    if value is None:
        raw = os.environ.get(ENV_NAME)
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f'{ENV_NAME} must be a JSON object.') from exc
    if not isinstance(value, dict):
        raise ValueError(f'{ENV_NAME} must be a JSON object.')
    unknown = set(value) - FIELDS
    if unknown:
        raise ValueError(f'Unknown preparation resource field(s): {", ".join(sorted(unknown))}.')
    config = dict(value)
    for key, number in config.items():
        cpu = key in {'cpuBudget', 'cpuPerPrepare'}
        try:
            finite = math.isfinite(number)
        except (TypeError, OverflowError):
            finite = False
        if (isinstance(number, bool) or not isinstance(number, (int, float) if cpu else int)
            or not finite or number < 0
            or (key not in {'diskMinFreeBytes', 'diskPerPrepareBytes'} and number == 0)):
            unit = 'positive finite number' if cpu else ('nonnegative integer' if key.startswith('disk') else 'positive integer')
            raise ValueError(f'{key} must be a {unit}; byte values use bytes, not GiB.')
    for budget, estimate in [('cpuBudget', 'cpuPerPrepare'), ('memoryBudgetBytes', 'memoryPerPrepareBytes')]:
        if (budget in config) != (estimate in config):
            raise ValueError(f'{budget} and {estimate} must be configured together.')
        if budget in config and config[estimate] > config[budget]:
            raise ValueError(f'{estimate} cannot exceed {budget}; no preparation could ever be admitted.')
    return config


class PreparationResourceGuard:
    """FIFO admission with cancellation and periodic disk-space rechecks."""

    def __init__(self, disk_paths, config=None, disk_usage=None, poll_seconds=1.0):
        self.settings = parse_config(config)
        self.search_gpu = search_gpu_configuration()
        self.paths = list(dict.fromkeys(str(Path(path).resolve()) for path in disk_paths))
        self._disk_usage = disk_usage or shutil.disk_usage
        self._poll_seconds = float(poll_seconds)
        if not math.isfinite(self._poll_seconds) or self._poll_seconds <= 0:
            raise ValueError('Resource admission poll interval must be positive and finite.')
        self._disk_enabled = any(key in self.settings for key in ('diskMinFreeBytes', 'diskPerPrepareBytes'))
        if self._disk_enabled and not self.paths:
            raise ValueError('Disk admission requires at least one preparation filesystem path.')
        self._condition = threading.Condition()
        self._queue = deque()
        self._reservations = {}
        self._closed = False
        self._last_probe = float('-inf')
        self._disk = {'paths': [], 'minFreeBytes': None, 'observedAt': None}

    def _disk_snapshot(self):
        if not self._disk_enabled:
            return self._disk
        now = time.monotonic()
        if now - self._last_probe < self._poll_seconds:
            return self._disk
        paths = []
        for path in self.paths:
            try:
                free = self._disk_usage(path).free
                if type(free) is not int or free < 0:
                    raise OSError('Filesystem probe returned invalid free bytes.')
                paths.append({'path': path, 'freeBytes': free, 'error': None})
            except OSError as exc:
                paths.append({'path': path, 'freeBytes': None, 'error': str(exc)})
        good = [entry['freeBytes'] for entry in paths if entry['freeBytes'] is not None]
        self._disk = {'paths': paths, 'minFreeBytes': min(good) if len(good) == len(paths) else None,
                      'observedAt': time.time()}
        self._last_probe = now
        return self._disk

    def _reasons(self):
        count, settings = len(self._reservations), self.settings
        reasons = []
        for budget, estimate, code, label in (
            ('cpuBudget', 'cpuPerPrepare', 'cpu_budget', 'CPU estimate budget is fully reserved.'),
            ('memoryBudgetBytes', 'memoryPerPrepareBytes', 'memory_budget', 'Memory estimate budget is fully reserved.'),
        ):
            if budget in settings:
                required = settings[estimate]
                available = settings[budget] - count * required
                # Fractional CPU units are useful for explicitly configured quotas.
                fits = required <= available or (code == 'cpu_budget' and math.isclose(required, available, rel_tol=1e-12))
                if not fits:
                    reasons.append({'code': code, 'label': label, 'available': max(0, available), 'required': required})
        if self._disk_enabled:
            disk = self._disk_snapshot()
            if disk['minFreeBytes'] is None:
                reasons.append({'code': 'disk_probe', 'label': 'Cannot verify free space on every preparation filesystem.',
                                'available': None, 'required': None})
            else:
                reserve = settings.get('diskPerPrepareBytes', 0)
                available = max(0, disk['minFreeBytes'] - count * reserve)
                required = settings.get('diskMinFreeBytes', 0) + reserve
                if available < required:
                    reasons.append({'code': 'disk_free', 'label': 'Free disk space is below the floor plus preparation reservations.',
                                    'available': available, 'required': required})
        return reasons

    def acquire(self, cancelled=lambda: False):
        """Return a reservation ID, or None when waiting is cancelled/closed."""
        ticket = object()
        with self._condition:
            if self._closed or cancelled():
                return None
            self._queue.append(ticket)
            try:
                while True:
                    if self._closed or cancelled():
                        return None
                    if self._queue[0] is ticket and not self._reasons():
                        token = uuid.uuid4().hex
                        self._queue.popleft()
                        self._reservations[token] = {'id': token, 'worker': threading.current_thread().name,
                                                     'acquiredAt': time.time()}
                        self._condition.notify_all()
                        return token
                    # Release/close wake immediately; disk or cancellation changes
                    # are rechecked even when no worker can release a reservation.
                    self._condition.wait(self._poll_seconds)
            finally:
                if ticket in self._queue:
                    self._queue.remove(ticket)
                    self._condition.notify_all()

    def bind(self, token, item_id, campaign_id):
        with self._condition:
            if token in self._reservations:
                self._reservations[token].update(itemId=item_id, campaignId=campaign_id)

    def release(self, token):
        with self._condition:
            removed = self._reservations.pop(token, None) is not None
            if removed:
                self._condition.notify_all()
            return removed

    def close(self):
        """Stop waiting admissions without revoking already running work."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            reasons = self._reasons()
            count, settings = len(self._reservations), self.settings
            reserved = lambda key: count * settings[key] if key in settings else None
            return {
                'configured': bool(settings), 'mode': 'estimated-admission',
                'searchGpu': copy.deepcopy(self.search_gpu),
                'limits': {key: settings.get(key) for key in ('cpuBudget', 'memoryBudgetBytes', 'diskMinFreeBytes')},
                'estimates': {key: settings.get(key) for key in ('cpuPerPrepare', 'memoryPerPrepareBytes', 'diskPerPrepareBytes')},
                'inflight': {'count': count, 'cpuReserved': reserved('cpuPerPrepare'),
                             'memoryReservedBytes': reserved('memoryPerPrepareBytes'),
                             'diskReservedBytes': reserved('diskPerPrepareBytes'),
                             'reservations': copy.deepcopy(list(self._reservations.values()))},
                'waiting': len(self._queue), 'wouldBlock': bool(reasons), 'blockedReasons': reasons,
                'disk': copy.deepcopy(self._disk), 'closed': self._closed,
                'note': ('Administrator estimates for this controller only; not CPU/RSS limits or disk quotas. '
                         'Disk uses the least free space across cache/attempt paths, subtracting all current reservations '
                         'even if part has already been written. Waiting counts preparation threads, not targets. '
                         'Unconfigured CPU or memory estimates remain unknown; profile thread counts are not guessed.'),
            }


def search_gpu_configuration(require=False):
    """Explicit local search devices; no GPU inventory or availability is invented.

    Shared per-device locks are also held by cooperating prediction processes.
    Independent search groups prevent a resident model from permanently owning
    the only devices a queued search can use.
    """
    import re
    raw = os.environ.get('PROTEIN_STUDIO_SEARCH_GPU_GROUPS_JSON')
    lock_dir = os.environ.get('PROTEIN_STUDIO_GPU_LOCK_DIR')
    groups, seen, diagnostics = [], set(), []
    if raw:
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError('PROTEIN_STUDIO_SEARCH_GPU_GROUPS_JSON must be a JSON array of device groups.') from exc
        if not isinstance(values, list) or not values or len(values) > 64:
            raise ValueError('Search GPU groups must contain 1–64 explicitly configured groups.')
        for group in values:
            if not isinstance(group, list) or not group:
                raise ValueError('Each search GPU group must be a nonempty array.')
            normalized = []
            for device in group:
                if isinstance(device, bool) or not isinstance(device, (str, int)):
                    raise ValueError('Search GPU device IDs must be strings or integers.')
                device = str(device)
                if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_\\-/]*', device) or device in seen:
                    raise ValueError('Search GPU device IDs must be valid and cannot overlap between groups.')
                seen.add(device)
                normalized.append(device)
            groups.append(normalized)
        from .engine import _gpu_groups
        inference = _gpu_groups() or ()
        if seen.intersection(device for group in inference for device in group):
            raise ValueError('Search GPU groups must be disjoint from declared inference GPU groups on this host.')
    else:
        diagnostics.append('Declare independent PROTEIN_STUDIO_SEARCH_GPU_GROUPS_JSON before GPU MSA search.')
    if not lock_dir:
        diagnostics.append('Declare a shared host-local PROTEIN_STUDIO_GPU_LOCK_DIR for search and inference processes.')
    elif not Path(lock_dir).is_absolute():
        raise ValueError('PROTEIN_STUDIO_GPU_LOCK_DIR must be an absolute host-local path.')
    if require and diagnostics:
        raise ValueError(' '.join(diagnostics))
    return {'configured': bool(groups and lock_dir), 'groups': groups, 'lockDirectory': lock_dir,
            'diagnostics': diagnostics, 'mode': 'explicit-search-device-groups',
            'scope': 'Declared local devices with cooperating per-device process locks; not measured GPU utilization.'}
