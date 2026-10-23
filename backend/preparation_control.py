"""Bounded, private preparation checkpoints and cooperative cache yielding."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re

KEY = re.compile(r'[a-f0-9]{64}')
PROGRESS_LIMIT = 64 * 1024


class CacheBusy(Exception):
    def __init__(self, key, wait_reason='shared_cache'):
        self.key = key
        self.wait_reason = wait_reason
        super().__init__('Preparation cache key is owned by another search.')


class PreparationDeferred(Exception):
    """No scientific failure: published leaves are retained while slots yield."""
    def __init__(self, progress, chain_key=None, chain_ids=None, wait_reason='shared_cache'):
        self.progress = validate_progress(progress)
        self.chain_key = chain_key
        self.chain_ids = chain_ids or []
        self.wait_reason = wait_reason
        super().__init__('Shared preparation is in progress; this target yielded its resource reservation.')


def validate_progress(value):
    if (not isinstance(value, dict) or value.get('schema') != 1
            or not isinstance(value.get('wholeCacheKey'), str)
            or not KEY.fullmatch(value['wholeCacheKey'])):
        raise ValueError('Invalid preparation checkpoint identity.')
    keys, ids = set(), set()
    for field in ('completedLeaves', 'pendingLeaves'):
        entries = value.get(field)
        if not isinstance(entries, list) or len(entries) > 26:
            raise ValueError('Preparation checkpoint must contain bounded chain references.')
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError('Invalid preparation checkpoint chain reference.')
            for name in ('cacheKey', 'sequenceSha256'):
                if not isinstance(entry.get(name), str) or not KEY.fullmatch(entry[name]):
                    raise ValueError('Invalid preparation checkpoint chain hash.')
            chain_ids = entry.get('chainIds')
            if (not isinstance(chain_ids, list) or not chain_ids or len(chain_ids) > 26
                    or any(not isinstance(x, str) or not re.fullmatch('[A-Z]', x) for x in chain_ids)
                    or len(set(chain_ids)) != len(chain_ids) or ids.intersection(chain_ids)):
                raise ValueError('Invalid preparation checkpoint chain IDs.')
            ids.update(chain_ids)
            if entry['cacheKey'] in keys or type(entry.get('residueCount')) is not int or entry['residueCount'] <= 0:
                raise ValueError('Invalid or repeated preparation checkpoint leaf.')
            keys.add(entry['cacheKey'])
            if field == 'completedLeaves' and type(entry.get('cacheHit')) is not bool:
                raise ValueError('Completed preparation references need their original cache-hit identity.')
    if len(ids) > 26 or len(keys) > 26:
        raise ValueError('Preparation checkpoint exceeds the protein-only chain limit.')
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if len(encoded.encode()) > PROGRESS_LIMIT:
        raise ValueError('Preparation checkpoint exceeds its bounded ledger size.')
    return copy.deepcopy(value)


def retry_delay(item_id, defer_count):
    """Bounded stable jitter; no random polling storms or unbounded exponent."""
    base = min(10., .5 * (2 ** min(max(int(defer_count), 0), 5)))
    jitter = int(hashlib.sha256(str(item_id).encode()).hexdigest()[:8], 16) / 0xffffffff
    return base * (1 + .2 * jitter)


def validate_activity(value):
    if not isinstance(value, dict) or value.get('state') not in {'searching', 'waiting_shared', 'assembling', 'prepared'}:
        raise ValueError('Unknown preparation activity state.')
    result = {'state': value['state']}
    if 'waitReason' in value:
        if value['waitReason'] not in {'shared_cache', 'search_gpu'}:
            raise ValueError('Unknown preparation wait reason.')
        result['waitReason'] = value['waitReason']
    if 'chainKey' in value:
        if not isinstance(value['chainKey'], str) or not KEY.fullmatch(value['chainKey']):
            raise ValueError('Invalid preparation activity key.')
        result['chainKey'] = value['chainKey']
    if 'chainIds' in value:
        ids = value['chainIds']
        if not isinstance(ids, list) or len(ids) > 26 or any(not isinstance(x, str) or not re.fullmatch('[A-Z]', x) for x in ids):
            raise ValueError('Invalid preparation activity chain IDs.')
        result['chainIds'] = ids
    if 'retryAfter' in value:
        if isinstance(value['retryAfter'], bool) or not isinstance(value['retryAfter'], (int, float)) or not math.isfinite(value['retryAfter']):
            raise ValueError('Invalid preparation retry time.')
        result['retryAfter'] = value['retryAfter']
    if 'deferCount' in value:
        if type(value['deferCount']) is not int or value['deferCount'] < 0:
            raise ValueError('Invalid preparation defer count.')
        result['deferCount'] = value['deferCount']
    return result
