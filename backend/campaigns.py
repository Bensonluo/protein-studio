"""Persistent, metadata-only campaign ledger for one local controller.

Large model results live in immutable attempt files. SQLite owns the current
lease token and published pointer; a late worker cannot replace another attempt.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .store import ConflictError, utcnow
from .preparation_control import validate_progress, validate_activity

STAGES = ('waiting_input', 'preparing', 'ready', 'predicting', 'validating', 'completed', 'failed', 'cancelled')
TERMINAL = ('completed', 'failed', 'cancelled')
ACTIVE = ('preparing', 'predicting', 'validating')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


PUBLICATION_LIMIT = 4096


def publication_file(item_root, path):
    """Bounded metadata identity of a regular, controller-owned attempt file."""
    root = Path(item_root)
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root/candidate
    relative = candidate.relative_to(root)
    if not relative.parts or '..' in relative.parts or len(relative.as_posix().encode()) > 1024:
        raise ValueError('Publication file must have a bounded item-relative path.')
    # Check the entire ancestry, including the item directory itself; resolve()
    # would otherwise silently turn an unsafe symlink into an accepted target.
    cursor = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        cursor = cursor/part
        if cursor.is_symlink():
            raise ValueError('Publication paths cannot traverse symbolic links.')
    info = candidate.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise ValueError('Publication files must be nonempty regular files.')
    return {'relativePath': relative.as_posix(), 'bytes': info.st_size,
            'mtimeNs': info.st_mtime_ns, 'inode': info.st_ino, 'device': info.st_dev}


def checked_publication(value, row, item_root, result_path, require_identity=True):
    """Validate small trusted metadata without opening result JSON or PAE."""
    if (not isinstance(value, dict) or type(value.get('schema')) is not int or value.get('schema') != 1
            or set(value) - {'schema', 'itemId', 'attempt', 'inputHash', 'result', 'structure'}):
        raise ValueError('Unknown result publication schema or fields.')
    identities = {'itemId': row['id'], 'attempt': row['attempt'], 'inputHash': row['input_hash']}
    if require_identity and not set(identities).issubset(value):
        raise ValueError('Persisted result publication is missing task identity.')
    if 'attempt' in value and type(value['attempt']) is not int:
        raise ValueError('Result publication attempt must be an integer.')
    for name, expected in identities.items():
        if name in value and value[name] != expected:
            raise ValueError('Result publication does not match the current task attempt.')
    normalized = {'schema': 1, **identities}
    for name in ('result', 'structure'):
        entry = value.get(name)
        if not isinstance(entry, dict) or set(entry) != {'relativePath', 'bytes', 'mtimeNs', 'inode', 'device'}:
            raise ValueError('Incomplete result publication file identity.')
        relative = entry['relativePath']
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise ValueError('Result publication paths must be safe and item-relative.')
        if any(type(entry[key]) is not int or entry[key] < 0 for key in ('bytes', 'mtimeNs', 'inode', 'device')):
            raise ValueError('Result publication file attributes must be nonnegative integers.')
        observed = publication_file(item_root, Path(item_root)/relative)
        if observed != entry:
            raise ValueError('Result publication file identity changed or was replaced.')
        normalized[name] = observed
    if Path(result_path) != Path(item_root)/normalized['result']['relativePath']:
        raise ValueError('Result publication does not match the published result pointer.')
    if len(encode(normalized).encode()) > PUBLICATION_LIMIT:
        raise ValueError('Result publication exceeds its bounded ledger size.')
    return normalized


class CampaignStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS campaigns (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, engine TEXT NOT NULL,
                    status TEXT NOT NULL, sealed INTEGER NOT NULL DEFAULT 0,
                    specification TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS campaign_items (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id), target_id TEXT NOT NULL,
                    input_hash TEXT NOT NULL, input_json TEXT NOT NULL, sequence_length INTEGER NOT NULL,
                    stage TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
                    token TEXT, lease_until REAL, started REAL, prepared_path TEXT,
                    candidate_path TEXT, result_path TEXT, error TEXT, failed_stage TEXT, cache_hit INTEGER NOT NULL DEFAULT 0,
                    prep_seconds REAL NOT NULL DEFAULT 0, predict_seconds REAL NOT NULL DEFAULT 0,
                    validate_seconds REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(campaign_id,target_id)
                );
                CREATE INDEX IF NOT EXISTS campaign_item_stage ON campaign_items(campaign_id,stage,seq);
                CREATE INDEX IF NOT EXISTS campaign_stage_queue ON campaign_items(stage,seq);
                CREATE INDEX IF NOT EXISTS campaign_lease ON campaign_items(lease_until) WHERE token IS NOT NULL;
                CREATE TABLE IF NOT EXISTS campaign_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL,
                    time TEXT NOT NULL, stage TEXT NOT NULL, message TEXT NOT NULL, progress REAL
                );
                CREATE INDEX IF NOT EXISTS campaign_event_item ON campaign_events(item_id,seq);
            ''')
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(campaign_items)')}
            if 'failed_stage' not in columns:
                conn.execute('ALTER TABLE campaign_items ADD COLUMN failed_stage TEXT')
            for column in ('remote_owner', 'batch_id'):
                if column not in columns:
                    conn.execute('ALTER TABLE campaign_items ADD COLUMN ' + column + ' TEXT')
            if 'preparation_reuse' not in columns:
                conn.execute('ALTER TABLE campaign_items ADD COLUMN preparation_reuse TEXT')
            if 'result_publication' not in columns:
                conn.execute('ALTER TABLE campaign_items ADD COLUMN result_publication TEXT')
            if 'prepared_cache_key' not in columns:
                conn.execute('ALTER TABLE campaign_items ADD COLUMN prepared_cache_key TEXT')
            for column in ('prepare_progress', 'preparation_activity'):
                if column not in columns:
                    conn.execute('ALTER TABLE campaign_items ADD COLUMN ' + column + ' TEXT')
            for column, declaration in (('prepare_not_before', 'REAL NOT NULL DEFAULT 0'),
                                        ('prepare_defer_count', 'INTEGER NOT NULL DEFAULT 0')):
                if column not in columns:
                    conn.execute('ALTER TABLE campaign_items ADD COLUMN ' + column + ' ' + declaration)
            conn.execute('CREATE INDEX IF NOT EXISTS campaign_cache_reference ON campaign_items(prepared_cache_key) WHERE prepared_cache_key IS NOT NULL')

    @contextmanager
    def connection(self, write=False):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=15000')
        try:
            if write:
                conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def event(conn, item_id, stage, message, progress=None):
        conn.execute('INSERT INTO campaign_events(item_id,time,stage,message,progress) VALUES(?,?,?,?,?)',
                     (item_id, utcnow(), stage, str(message)[:2000], progress))

    def create(self, specification):
        campaign_id, now = uuid.uuid4().hex, utcnow()
        with self.connection(True) as conn:
            conn.execute('INSERT INTO campaigns VALUES(?,?,?,?,?,?,?,?)',
                         (campaign_id, specification['name'], specification['engine'],
                          specification.get('status', 'active'), 0, encode(specification), now, now))
        return self.get(campaign_id)

    def get(self, campaign_id):
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM campaigns WHERE id=?', (campaign_id,)).fetchone()
            if row is None:
                return None
            counts = {stage: 0 for stage in STAGES}
            stats = {stage: {'count': 0, 'active': 0} for stage in STAGES}
            for group in conn.execute('SELECT stage,COUNT(*) AS n,SUM(token IS NOT NULL) AS active FROM campaign_items WHERE campaign_id=? GROUP BY stage', (campaign_id,)):
                stage = group['stage']
                if row['status'] == 'cancelled' and stage not in TERMINAL:
                    stage = 'cancelled'
                counts[stage] += group['n']
                stats[stage]['count'] += group['n']
                if row['status'] != 'cancelled':
                    stats[stage]['active'] += group['active'] or 0
            metrics = conn.execute('SELECT SUM(prep_seconds) AS prep,SUM(predict_seconds) AS predict,SUM(validate_seconds) AS validate,SUM(cache_hit) AS hits FROM campaign_items WHERE campaign_id=?', (campaign_id,)).fetchone()
            # Aggregate compact ledger metadata in SQLite; do not open prepared
            # sidecars, model outputs or all target inputs on this polling route.
            reuse_fields = {'totalChains': 'totalChains', 'uniqueChainOccurrences': 'uniqueChains',
                            'newChainSearches': 'newChainSearches', 'chainCacheHits': 'chainCacheHits',
                            'duplicateChainCopies': 'duplicateChainCopies', 'wholeTargetCacheHits': 'wholeTargetCacheHit'}
            aggregates = ','.join("COALESCE(SUM(json_extract(preparation_reuse,'$." + source + "')),0) AS " + field
                                  for field, source in reuse_fields.items())
            reuse = conn.execute('SELECT COUNT(*) AS targets,' + aggregates +
                                 ' FROM campaign_items WHERE campaign_id=? AND preparation_reuse IS NOT NULL',
                                 (campaign_id,)).fetchone()
            activity_counts = {'searching': 0, 'waitingShared': 0, 'assembling': 0}
            if row['status'] != 'cancelled':
                for activity in conn.execute("SELECT json_extract(preparation_activity,'$.state') AS state,COUNT(*) AS n FROM campaign_items WHERE campaign_id=? AND stage IN ('preparing','waiting_input') AND preparation_activity IS NOT NULL GROUP BY state", (campaign_id,)):
                    name = {'searching': 'searching', 'waiting_shared': 'waitingShared', 'assembling': 'assembling'}.get(activity['state'])
                    if name:
                        activity_counts[name] += activity['n']
            total = sum(counts.values())
            finished = sum(counts[k] for k in TERMINAL)
            complete = bool(row['sealed'] and total == finished)
            result = json.loads(row['specification'])
            result.update(id=row['id'], name=row['name'], engine=row['engine'], status=row['status'],
                          sealed=bool(row['sealed']), createdAt=row['created_at'], updatedAt=row['updated_at'],
                          totalItems=total, counts=counts, stageStats=stats, finishedItems=finished,
                          preparationStateCounts=activity_counts,
                          preparationStateScope='Current target counts, including paused campaigns; not process or unique-chain counts.',
                          progress=(100.0 * finished / total) if total else 0, isComplete=complete,
                          metrics={'preparationSeconds': metrics['prep'] or 0,
                                   'predictionSeconds': metrics['predict'] or 0,
                                   'validationSeconds': metrics['validate'] or 0,
                                   'cacheHits': metrics['hits'] or 0,
                                   'preparationReuse': dict(reuse)})
            return result

    def list(self, limit=50, offset=0):
        with self.connection() as conn:
            ids = [r['id'] for r in conn.execute('SELECT id FROM campaigns ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (limit, offset))]
        return [self.get(campaign_id) for campaign_id in ids]

    def append(self, campaign_id, normalized_items):
        accepted, inserted = [], 0
        with self.connection(True) as conn:
            campaign = conn.execute('SELECT * FROM campaigns WHERE id=?', (campaign_id,)).fetchone()
            if campaign is None:
                raise KeyError('Campaign not found.')
            if campaign['status'] == 'cancelled':
                raise ConflictError('Cancelled campaigns cannot accept new input.')
            for item in normalized_items:
                # Immutable administrator profiles belong to the campaign once,
                # rather than being copied into every target row.
                payload = encode({k: v for k, v in item['submission'].items()
                                  if k not in {'databaseProfile', 'executionProfile'}})
                digest = hashlib.sha256(payload.encode()).hexdigest()
                old = conn.execute("SELECT *,json_extract(input_json,'$.name') AS name FROM campaign_items WHERE campaign_id=? AND target_id=?",
                                   (campaign_id, item['targetId'])).fetchone()
                if old is not None:
                    if old['input_hash'] != digest:
                        # Existing workspaces may contain the original expanded
                        # profile representation. Normalize it on retransmission.
                        previous = json.loads(old['input_json'])
                        previous.pop('databaseProfile', None)
                        previous.pop('executionProfile', None)
                        if encode(previous) != payload:
                            raise ConflictError(f"targetId {item['targetId']!r} already has different input or settings.")
                        conn.execute('UPDATE campaign_items SET input_json=?,input_hash=? WHERE id=?',
                                     (payload, digest, old['id']))
                    summary = self._public_item(old)
                    summary['inputHash'] = digest
                    accepted.append(summary)
                    continue
                if campaign['sealed']:
                    raise ConflictError('Campaign input is sealed; no new target IDs can be appended.')
                now, item_id = utcnow(), uuid.uuid4().hex
                length = sum(len(c['sequence']) for c in item['submission'].get('chains', []))
                conn.execute('''INSERT INTO campaign_items(id,campaign_id,target_id,input_hash,input_json,
                    sequence_length,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,'waiting_input',?,?)''',
                             (item_id, campaign_id, item['targetId'], digest, payload, length, now, now))
                self.event(conn, item_id, 'waiting_input', 'Input recorded in the durable campaign manifest.', 0)
                accepted.append({'id': item_id, 'campaignId': campaign_id, 'targetId': item['targetId'],
                                 'name': item['submission'].get('name') or item['targetId'],
                                 'stage': 'waiting_input', 'status': 'waiting_input', 'attempt': 0,
                                 'sequenceLength': length, 'inputHash': digest, 'createdAt': now, 'updatedAt': now})
                inserted += 1
            conn.execute('UPDATE campaigns SET updated_at=? WHERE id=?', (utcnow(), campaign_id))
            total = conn.execute('SELECT COUNT(*) FROM campaign_items WHERE campaign_id=?', (campaign_id,)).fetchone()[0]
        return {'items': accepted, 'count': len(accepted), 'inserted': inserted,
                'duplicates': len(accepted) - inserted, 'totalItems': total}

    @staticmethod
    def _public_item(row, cancelled=False, include_reuse_entries=False):
        stage = row['stage']
        if cancelled and stage not in TERMINAL:
            stage = 'cancelled'
        result = {'id': row['id'], 'campaignId': row['campaign_id'], 'targetId': row['target_id'],
                'name': row['name'] or row['target_id'],
                'stage': stage, 'status': stage, 'attempt': row['attempt'], 'inputHash': row['input_hash'],
                'sequenceLength': row['sequence_length'], 'error': row['error'],
                'failedStage': row['failed_stage'],
                'cacheHit': bool(row['cache_hit']), 'createdAt': row['created_at'], 'updatedAt': row['updated_at'],
                'timings': {'preparationSeconds': row['prep_seconds'], 'predictionSeconds': row['predict_seconds'],
                            'validationSeconds': row['validate_seconds']},
                'hasResult': bool(row['result_path']), 'resultPath': row['result_path']}
        if not cancelled and 'preparation_activity' in row.keys() and row['preparation_activity']:
            result['preparationProgress'] = json.loads(row['preparation_activity'])
        if 'preparation_reuse' in row.keys() and row['preparation_reuse']:
            reuse = json.loads(row['preparation_reuse'])
            if not include_reuse_entries:
                reuse.pop('entries', None)
            result['preparationReuse'] = reuse
            if include_reuse_entries:
                result['msaQuality'] = [{key: entry.get(key) for key in ('chainIds', 'cacheKey', 'msaQuality', 'searchProcessWallSeconds')}
                                        for entry in reuse.get('entries', [])]
        return result

    def items(self, campaign_id, limit=25, offset=0, stage=None):
        with self.connection() as conn:
            campaign = conn.execute('SELECT status FROM campaigns WHERE id=?', (campaign_id,)).fetchone()
            if campaign is None:
                raise KeyError('Campaign not found.')
            conditions, params = ['campaign_id=?'], [campaign_id]
            if stage:
                if stage not in STAGES:
                    raise ValueError('Unknown pipeline stage.')
                if campaign['status'] == 'cancelled' and stage == 'cancelled':
                    conditions.append("stage NOT IN ('completed','failed')")
                elif campaign['status'] == 'cancelled' and stage not in TERMINAL:
                    conditions.append('0')
                else:
                    conditions.append('stage=?')
                    params.append(stage)
            where = ' AND '.join(conditions)
            total = conn.execute('SELECT COUNT(*) FROM campaign_items WHERE ' + where, params).fetchone()[0]
            # Do not select input_json on the list route: only a page of metadata.
            rows = conn.execute('''SELECT id,campaign_id,target_id,input_hash,sequence_length,stage,attempt,error,failed_stage,
                cache_hit,preparation_reuse,preparation_activity,created_at,updated_at,prep_seconds,predict_seconds,validate_seconds,result_path,
                json_extract(input_json,'$.name') AS name
                FROM campaign_items WHERE ''' + where + ' ORDER BY seq LIMIT ? OFFSET ?', (*params, limit, offset))
            items = [self._public_item(row, campaign['status'] == 'cancelled') for row in rows]
            return {'items': items, 'total': total, 'limit': limit, 'offset': offset,
                    'nextOffset': offset + len(items) if offset + len(items) < total else None}

    def item(self, campaign_id, item_id, internal=False):
        with self.connection() as conn:
            row = conn.execute("SELECT i.*,json_extract(i.input_json,'$.name') AS name,c.status AS campaign_status,c.specification FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id WHERE i.campaign_id=? AND i.id=?", (campaign_id, item_id)).fetchone()
            if row is None:
                return None
            if internal:
                return dict(row)
            result = self._public_item(row, row['campaign_status'] == 'cancelled', include_reuse_entries=True)
            result['input'] = json.loads(row['input_json'])
            specification = json.loads(row['specification'])
            result['input'].update(databaseProfile=specification.get('databaseProfile', {}),
                                   executionProfile=specification.get('executionProfile', {}))
            events = list(conn.execute('SELECT time,stage,message,progress FROM campaign_events WHERE item_id=? ORDER BY seq DESC LIMIT 200', (item_id,)))
            result['events'] = [dict(event) for event in reversed(events)]
            return result

    def control(self, campaign_id, action):
        with self.connection(True) as conn:
            row = conn.execute('SELECT status FROM campaigns WHERE id=?', (campaign_id,)).fetchone()
            if row is None:
                raise KeyError('Campaign not found.')
            if action == 'seal':
                conn.execute('UPDATE campaigns SET sealed=1,updated_at=? WHERE id=?', (utcnow(), campaign_id))
            else:
                status = {'pause': 'paused', 'resume': 'active', 'cancel': 'cancelled'}.get(action)
                if status is None:
                    raise ValueError('Unknown campaign action.')
                if row['status'] == 'cancelled' and status != 'cancelled':
                    raise ConflictError('A cancelled campaign cannot be resumed.')
                conn.execute('UPDATE campaigns SET status=?,updated_at=? WHERE id=?', (status, utcnow(), campaign_id))
        return self.get(campaign_id)

    def retry_failed(self, campaign_id):
        """Atomically requeue only failures, preserving manifest IDs and sealing.

        Reuse only a readable descriptor within this item's directory. Its cache
        assets and hashes are subsequently revalidated by the staged adapter.
        """
        root = self.path.parent / 'campaigns' / campaign_id

        def usable(path, item_id):
            if not path:
                return 0
            try:
                candidate = Path(path).resolve()
                if not candidate.is_relative_to((root / item_id).resolve()) or not candidate.is_file():
                    return 0
                if candidate.stat().st_size > 4 * 1024 * 1024:
                    return 0
                return int(isinstance(json.loads(candidate.read_text()), dict))
            except (OSError, ValueError, UnicodeError):
                return 0

        with self.connection(True) as conn:
            campaign = conn.execute('SELECT status FROM campaigns WHERE id=?', (campaign_id,)).fetchone()
            if campaign is None:
                raise KeyError('Campaign not found.')
            if campaign['status'] == 'cancelled':
                raise ConflictError('A cancelled campaign cannot retry failed items.')
            conn.create_function('prepared_usable', 2, usable)
            conn.execute('''INSERT INTO campaign_events(item_id,time,stage,message,progress)
                SELECT id,?,'retry','Explicit retry requested; previous publication rights revoked.',NULL
                FROM campaign_items WHERE campaign_id=? AND stage='failed' ''', (utcnow(), campaign_id))
            result = conn.execute('''UPDATE campaign_items SET
                stage=CASE WHEN (failed_stage IS NULL OR failed_stage IN ('predicting','validating'))
                    AND prepared_usable(prepared_path,id)=1 THEN 'ready' ELSE 'waiting_input' END,
                token=NULL,lease_until=NULL,started=NULL,remote_owner=NULL,batch_id=NULL,attempt=attempt+1,error=NULL,failed_stage=NULL,
                candidate_path=NULL,result_path=NULL,result_publication=NULL,prepare_not_before=0,preparation_activity=NULL,updated_at=?
                WHERE campaign_id=? AND stage='failed' ''', (utcnow(), campaign_id))
            retried = result.rowcount
            conn.execute('UPDATE campaigns SET updated_at=? WHERE id=?', (utcnow(), campaign_id))
        return {'retried': retried, 'campaign': self.get(campaign_id)}

    def claim(self, kind, ready_cap=64, lease_seconds=60, now=None, preparation_route=None):
        now = time.time() if now is None else now
        source, target = {'prepare': ('waiting_input', 'preparing'), 'predict': ('ready', 'predicting'),
                          'validate': ('validating', 'validating')}[kind]
        with self.connection(True) as conn:
            admission, route_parameters = '1', []
            if kind == 'prepare':
                if preparation_route is not None:
                    admission, route_parameters = preparation_route(conn, now)
                    # A disappearing or incompatible destination must not hold
                    # the global preparation budget hostage. Keep its immutable
                    # prepared files/cache; re-admission safely hits that cache.
                    deferred = conn.execute('''SELECT i.id FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id
                        WHERE c.status='active' AND i.stage='ready' AND i.token IS NULL AND NOT ('''+admission+') ORDER BY i.seq LIMIT 500',
                        route_parameters).fetchall()
                    for item in deferred:
                        conn.execute("UPDATE campaign_items SET stage='waiting_input',updated_at=? WHERE id=?", (utcnow(), item['id']))
                        self.event(conn, item['id'], 'waiting_input', 'Routing deferred: no online compatible worker; prepared cache retained.')
                pending = conn.execute("SELECT COUNT(*) FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id WHERE c.status='active' AND (i.stage='preparing' OR (i.stage='ready' AND ("+admission+')))', route_parameters).fetchone()[0]
                if pending >= ready_cap:
                    return None
            due = ' AND i.prepare_not_before<=?' if kind == 'prepare' else ''
            due_parameters = [now] if kind == 'prepare' else []
            row = conn.execute('''SELECT i.*,c.specification FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id
                WHERE c.status='active' AND i.stage=? AND i.token IS NULL AND ('''+admission+')'+due+' ORDER BY i.seq LIMIT 1',
                [source]+route_parameters+due_parameters).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            conn.execute('UPDATE campaign_items SET stage=?,attempt=attempt+1,token=?,lease_until=?,started=?,updated_at=? WHERE id=?',
                         (target, token, now + lease_seconds, now, utcnow(), row['id']))
            if kind == 'prepare':
                conn.execute('UPDATE campaign_items SET preparation_activity=NULL WHERE id=?', (row['id'],))
            self.event(conn, row['id'], target, f'{kind} worker claimed a fenced attempt.')
            result = dict(row)
            result.update(stage=target, token=token, attempt=row['attempt'] + 1, lease_until=now + lease_seconds)
            result['job'] = json.loads(row['input_json'])
            if kind == 'prepare' and row['prepare_progress']:
                result['job']['_preparationProgress'] = validate_progress(json.loads(row['prepare_progress']))
            specification = json.loads(row['specification'])
            result['job'].update(databaseProfile=specification.get('databaseProfile', {}),
                                 executionProfile=specification.get('executionProfile', {}))
            return result

    def _authorized(self, conn, item_id, token, now):
        return conn.execute('''SELECT i.* FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id
            WHERE i.id=? AND i.token=? AND i.lease_until>? AND c.status!='cancelled' ''', (item_id, token, now)).fetchone()

    def heartbeat(self, item_id, token, lease_seconds=60, now=None):
        now = time.time() if now is None else now
        with self.connection(True) as conn:
            row = self._authorized(conn, item_id, token, now)
            if row is None or row['remote_owner'] is not None:
                return False
            conn.execute('UPDATE campaign_items SET lease_until=? WHERE id=? AND token=?', (now + lease_seconds, item_id, token))
            return True

    def authorized(self, item_id, token):
        with self.connection() as conn:
            return self._authorized(conn, item_id, token, time.time()) is not None

    def emit(self, item_id, token, message, progress=None):
        with self.connection(True) as conn:
            row = self._authorized(conn, item_id, token, time.time())
            if row is not None:
                self.event(conn, item_id, row['stage'], message, progress)

    def record_preparation_progress(self, item_id, token, progress):
        progress_json = encode(validate_progress(progress))
        with self.connection(True) as conn:
            row = self._authorized(conn, item_id, token, time.time())
            if row is None or row['stage'] != 'preparing':
                return False
            conn.execute('UPDATE campaign_items SET prepare_progress=?,updated_at=? WHERE id=? AND token=?',
                         (progress_json, utcnow(), item_id, token))
            return True

    def preparation_activity(self, item_id, token, activity):
        activity = validate_activity(activity)
        activity['updatedAt'] = utcnow()
        with self.connection(True) as conn:
            row = self._authorized(conn, item_id, token, time.time())
            if row is None or row['stage'] != 'preparing':
                return False
            conn.execute('UPDATE campaign_items SET preparation_activity=?,updated_at=? WHERE id=? AND token=?',
                         (encode(activity), activity['updatedAt'], item_id, token))
            return True

    def defer_preparation(self, item_id, token, progress, delay, chain_key=None, chain_ids=None, now=None, wait_reason='shared_cache'):
        # Caller retains the maintenance SH through this CAS: published leaves
        # become durable references before maintenance can acquire its writer lock.
        progress_json = encode(validate_progress(progress))
        now = time.time() if now is None else now
        if not isinstance(delay, (int, float)) or not 0 < delay <= 60:
            raise ValueError('Preparation deferral delay must be positive and bounded.')
        with self.connection(True) as conn:
            row = self._authorized(conn, item_id, token, now)
            if row is None or row['stage'] != 'preparing':
                return False
            activity = {'state': 'waiting_shared', 'waitReason': wait_reason, 'retryAfter': now + delay,
                        'deferCount': row['prepare_defer_count'] + 1}
            if chain_key:
                activity['chainKey'] = chain_key
            if chain_ids:
                activity['chainIds'] = chain_ids
            activity = validate_activity(activity)
            activity['updatedAt'] = utcnow()
            conn.execute("""UPDATE campaign_items SET stage='waiting_input',token=NULL,lease_until=NULL,
                started=NULL,remote_owner=NULL,batch_id=NULL,prepare_progress=?,preparation_activity=?,
                prepare_not_before=?,prepare_defer_count=prepare_defer_count+1,prep_seconds=prep_seconds+?,
                error=NULL,failed_stage=NULL,updated_at=? WHERE id=? AND token=?""",
                (progress_json, encode(activity), now + delay, max(0, now-(row['started'] or now)), activity['updatedAt'], item_id, token))
            self.event(conn, item_id, 'waiting_input', 'Shared preparation deferred; completed chain references retained and resource reservation released.')
            return True

    def advance(self, item_id, token, target, prepared_path=None, candidate_path=None, result_path=None,
                error=None, cache_hit=False, now=None, preparation_reuse=None, preparation_cache_key=None, result_publication=None):
        now = time.time() if now is None else now
        with self.connection(True) as conn:
            row = self._authorized(conn, item_id, token, now)
            if row is None:
                return False
            allowed = {'preparing': {'ready', 'failed'}, 'predicting': {'validating', 'failed'},
                       'validating': {'completed', 'failed'}}
            if target not in allowed.get(row['stage'], set()):
                raise ConflictError('Invalid pipeline stage transition.')
            publication_json = None
            if result_publication is not None:
                if target != 'completed' or row['stage'] != 'validating' or not result_path:
                    raise ValueError('Result publication is allowed only with completed output.')
                item_root = self.path.parent.resolve()/'campaigns'/row['campaign_id']/row['id']
                publication_json = encode(checked_publication(result_publication, row, item_root, result_path, require_identity=False))
            if preparation_cache_key is not None:
                if (row['stage'] != 'preparing' or target != 'ready'
                    or not isinstance(preparation_cache_key, str)
                    or not re.fullmatch('[a-f0-9]{64}', preparation_cache_key)):
                    raise ValueError('Preparation cache identity must be a SHA256 key published with ready inputs.')
            reuse_json = None
            if preparation_reuse is not None:
                if row['stage'] != 'preparing' or target != 'ready':
                    raise ValueError('Preparation reuse metadata can only be published with ready inputs.')
                if preparation_reuse.get('strategy') != 'af3-chain-reuse-v1':
                    raise ValueError('Unknown preparation reuse strategy.')
                for name in ('totalChains', 'uniqueChains', 'newChainSearches', 'chainCacheHits', 'duplicateChainCopies'):
                    value = preparation_reuse.get(name)
                    if type(value) is not int or not 0 <= value <= 26:
                        raise ValueError('Invalid preparation reuse counter: ' + name)
                if type(preparation_reuse.get('wholeTargetCacheHit')) is not bool:
                    raise ValueError('Invalid whole-target preparation cache flag.')
                total, unique = preparation_reuse['totalChains'], preparation_reuse['uniqueChains']
                searches = preparation_reuse['newChainSearches'] + preparation_reuse['chainCacheHits']
                if (not 1 <= unique <= total or preparation_reuse['duplicateChainCopies'] != total - unique
                    or (searches != 0 if preparation_reuse['wholeTargetCacheHit'] else searches != unique)):
                    raise ValueError('Inconsistent preparation reuse counters.')
                reuse_json = encode(preparation_reuse)
                if len(reuse_json.encode()) > 64 * 1024:
                    raise ValueError('Preparation reuse metadata exceeds its bounded ledger size.')
            durations = {'preparing': 'prep_seconds', 'predicting': 'predict_seconds', 'validating': 'validate_seconds'}
            field = durations[row['stage']]
            elapsed = max(0, now - (row['started'] or now))
            conn.execute('''UPDATE campaign_items SET stage=?,token=NULL,lease_until=NULL,started=NULL,remote_owner=NULL,batch_id=NULL,
                prepared_path=COALESCE(?,prepared_path),candidate_path=COALESCE(?,candidate_path),
                result_path=COALESCE(?,result_path),result_publication=?,prepared_cache_key=COALESCE(?,prepared_cache_key),preparation_reuse=COALESCE(?,preparation_reuse),error=?,failed_stage=?,cache_hit=MAX(cache_hit,?),updated_at=?,''' + field + '=' + field + '+? WHERE id=? AND token=?',
                         (target, prepared_path, candidate_path, result_path, publication_json, preparation_cache_key, reuse_json, error,
                          row['stage'] if target == 'failed' else None, int(cache_hit), utcnow(), elapsed, item_id, token))
            if target == 'ready' and row['stage'] == 'preparing':
                activity = encode({'state': 'prepared', 'updatedAt': utcnow(), 'deferCount': row['prepare_defer_count']})
                conn.execute('UPDATE campaign_items SET prepare_progress=NULL,prepare_not_before=0,preparation_activity=? WHERE id=?', (activity, item_id))
            elif target == 'failed':
                conn.execute('UPDATE campaign_items SET preparation_activity=NULL WHERE id=?', (item_id,))
            self.event(conn, item_id, target, error or f'Stage published: {target}.', 100 if target == 'completed' else None)
            conn.execute('UPDATE campaigns SET updated_at=? WHERE id=?', (utcnow(), row['campaign_id']))
            return True

    def recover(self, all_running=False, limit=500, now=None):
        """Requeue a bounded page after crash/lease expiry; tokens are invalidated."""
        now = time.time() if now is None else now
        with self.connection(True) as conn:
            if all_running:
                condition, parameters = 'i.token IS NOT NULL', ()
            else:
                condition = "(i.token IS NOT NULL AND i.lease_until<=?) OR (c.status='cancelled' AND i.stage NOT IN ('completed','failed','cancelled'))"
                parameters = (now,)
            rows = conn.execute('SELECT i.*,c.status AS campaign_status FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id WHERE (' + condition + ') ORDER BY i.seq LIMIT ?', (*parameters, limit)).fetchall()
            for row in rows:
                if row['campaign_status'] == 'cancelled':
                    stage = 'cancelled'
                else:
                    stage = {'preparing': 'waiting_input', 'predicting': 'ready', 'validating': 'validating'}.get(row['stage'], row['stage'])
                conn.execute('UPDATE campaign_items SET stage=?,token=NULL,lease_until=NULL,started=NULL,remote_owner=NULL,batch_id=NULL,attempt=attempt+1,updated_at=? WHERE id=?', (stage, utcnow(), row['id']))
                conn.execute('UPDATE campaign_items SET preparation_activity=NULL,prepare_not_before=0 WHERE id=?', (row['id'],))
                self.event(conn, row['id'], stage, 'Lease revoked; safe stage recovery.' if stage != 'cancelled' else 'Campaign cancelled.')
            return len(rows)
