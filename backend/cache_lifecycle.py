"""Explicit, bounded-memory lifecycle management for controller preparation caches.

Inventories are metadata scans, not content-hash integrity audits. Only explicit
apply operations mutate cache files. Logical sizes never claim physically freed
blocks. Results, model weights and database directories are outside this scope.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import time
import uuid

from .cache_access import exclusive_cache
from .engine import PredictionCancelled
from .campaigns import checked_publication, PUBLICATION_LIMIT

KEY = re.compile(r'^[0-9a-f]{64}$')
ID = re.compile(r'^[0-9a-f]{32}$')
PARTIAL = re.compile(r'^[0-9a-f]{64}\.partial-[A-Za-z0-9_-]+$')
KINDS = ('whole', 'leaf', 'partial', 'quarantine', 'unknown')
CHAIN_DIR = 'af3-chain-v1'
MAX_JSON = 4 * 1024 * 1024


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


class ScanLimit(Exception):
    pass


class LifecycleCancelled(PredictionCancelled):
    pass


class Budget:
    def __init__(self, max_entries=None, max_seconds=None, cancelled=lambda: False):
        self.max_entries, self.max_seconds, self.cancelled = max_entries, max_seconds, cancelled
        self.start, self.visited = time.monotonic(), 0

    def check(self, visit=False):
        if self.cancelled():
            raise LifecycleCancelled('Cache maintenance was cancelled.')
        if self.max_seconds is not None and time.monotonic()-self.start >= self.max_seconds:
            raise ScanLimit('Scan time limit reached; statistics cover only fully scanned entries.')
        if visit:
            if self.max_entries is not None and self.visited >= self.max_entries:
                raise ScanLimit('Scan node limit reached; statistics cover only fully scanned entries.')
            self.visited += 1


class CacheLifecycle:
    def __init__(self, data_dir, cache_root=None):
        self.data_dir = Path(data_dir).resolve()
        self.cache_root = Path(cache_root or self.data_dir/'cache').resolve()
        self.path = self.data_dir/'cache-lifecycle.sqlite3'
        self.operation_root = self.data_dir/'cache-lifecycle-locks'
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.operation_root.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY,created REAL NOT NULL,summary TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS scan_refs(scan_id TEXT NOT NULL,kind TEXT NOT NULL,cache_key TEXT NOT NULL,
                    references_count INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(scan_id,kind,cache_key));
                CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY AUTOINCREMENT,scan_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,kind TEXT NOT NULL,logical_bytes INTEGER NOT NULL,file_count INTEGER NOT NULL,
                    modified REAL NOT NULL,fingerprint TEXT NOT NULL,state TEXT NOT NULL,protected INTEGER NOT NULL,
                    reasons TEXT NOT NULL,quarantine_id TEXT,UNIQUE(scan_id,relative_path));
                CREATE INDEX IF NOT EXISTS entry_scan ON entries(scan_id,kind,id);
                CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,scan_id TEXT NOT NULL,action TEXT NOT NULL,
                    created REAL NOT NULL,status TEXT NOT NULL,policy TEXT NOT NULL,digest TEXT,count INTEGER NOT NULL DEFAULT 0,
                    logical_bytes INTEGER NOT NULL DEFAULT 0,result TEXT);
                CREATE TABLE IF NOT EXISTS plan_entries(plan_id TEXT NOT NULL,seq INTEGER NOT NULL,entry_id INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,kind TEXT NOT NULL,fingerprint TEXT NOT NULL,logical_bytes INTEGER NOT NULL,
                    quarantine_id TEXT,state TEXT NOT NULL DEFAULT 'planned',error TEXT,PRIMARY KEY(plan_id,seq));
                CREATE TABLE IF NOT EXISTS quarantine(id TEXT PRIMARY KEY,original_path TEXT NOT NULL,kind TEXT NOT NULL,
                    payload_path TEXT NOT NULL,logical_bytes INTEGER NOT NULL,fingerprint TEXT NOT NULL,
                    quarantined REAL NOT NULL,state TEXT NOT NULL,plan_id TEXT NOT NULL,current_plan_id TEXT);
            ''')
            if 'current_plan_id' not in {row[1] for row in conn.execute('PRAGMA table_info(quarantine)')}:
                conn.execute('ALTER TABLE quarantine ADD COLUMN current_plan_id TEXT')

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=30000')
        conn.execute('PRAGMA temp_store=FILE')
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _update_scan(self, scan_id, summary):
        with self.connection() as conn:
            conn.execute('UPDATE scans SET summary=? WHERE id=?', (encoded(summary), scan_id))

    @staticmethod
    def _diagnostic(summary, code, message):
        summary['diagnosticCount'] = summary.get('diagnosticCount', 0)+1
        if len(summary['diagnostics']) < 100:
            summary['diagnostics'].append({'code': code, 'message': str(message)[:1000]})
        else:
            summary['diagnosticsTruncated'] = True

    @contextmanager
    def _operation_lock(self, operation_id, cancelled=lambda: False):
        # These stable files are never unlinked or replaced. An open flock,
        # rather than PID existence/reuse, is evidence that an owner is alive.
        path = self.operation_root / (operation_id + '.lock')
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                if cancelled():
                    raise LifecycleCancelled('Cache maintenance was cancelled.')
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(.05)
            yield
        finally:
            os.close(descriptor)

    @contextmanager
    def _probe_abandoned(self, operation_id):
        """GET only probes an existing lock. Hold it across the status CAS."""
        descriptor = None
        if not isinstance(operation_id, str) or not re.fullmatch(r'(scan|apply)-[a-f0-9]{32}', operation_id):
            yield True
            return
        try:
            try:
                descriptor = os.open(self.operation_root / (operation_id + '.lock'), os.O_RDWR)
            except FileNotFoundError:
                yield True
                return
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def snapshot(self):
        """Read catalog and bounded owner-lock probes; never traverse the cache."""
        with self.connection() as conn:
            scan = conn.execute('SELECT * FROM scans ORDER BY created DESC LIMIT 1').fetchone()
            operation = conn.execute("SELECT * FROM plans WHERE status!='planned' ORDER BY created DESC LIMIT 1").fetchone()
        latest = json.loads(scan['summary']) if scan else None
        if latest and latest['status'] in {'waiting_lock', 'scanning_references', 'scanning_entries'}:
            with self._probe_abandoned(latest.get('operationId')) as abandoned:
                if abandoned:
                    previous = scan['summary']
                    latest.update(status='interrupted', truncated=True, failClosed=True,
                                  inventoryComplete=False, finishedAt=time.time())
                    self._diagnostic(latest, 'owner_interrupted', 'The scan owner no longer holds its operation lock; run a new scan.')
                    with self.connection() as conn:
                        conn.execute('UPDATE scans SET summary=? WHERE id=? AND summary=?',
                                     (encoded(latest), scan['id'], previous))
                        latest = json.loads(conn.execute('SELECT summary FROM scans WHERE id=?', (scan['id'],)).fetchone()[0])
        if operation and operation['status'] == 'applying':
            receipt = json.loads(operation['result'] or '{}')
            with self._probe_abandoned(receipt.get('operationId')) as abandoned:
                if abandoned:
                    receipt.update(status='interrupted', finishedAt=time.time())
                    receipt.setdefault('diagnostics', []).append({'code': 'owner_interrupted',
                        'message': 'The apply owner stopped. Retry this exact plan to reconcile its durable journal.'})
                    with self.connection() as conn:
                        conn.execute("UPDATE plans SET status='interrupted',result=? WHERE id=? AND status='applying' AND result=?",
                                     (encoded(receipt), operation['id'], operation['result']))
                        operation = conn.execute('SELECT * FROM plans WHERE id=?', (operation['id'],)).fetchone()
        active = latest if latest and latest['status'] in {'waiting_lock', 'scanning_references', 'scanning_entries'} else None
        if operation and operation['status'] == 'applying':
            active = json.loads(operation['result'])
        return {'latestScan': latest, 'activeOperation': active,
                'latestOperation': self._public_plan(operation) if operation else None,
                'scope': 'controller-preparation-cache',
                'sizeBasis': 'logical regular-file lengths; not allocated blocks or measured filesystem reclamation'}

    @staticmethod
    def _page(limit, offset):
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise ValueError('limit must be 1–200 and offset must be a nonnegative integer.')

    @staticmethod
    def _public_entry(row):
        return {'id': row['id'], 'kind': row['kind'], 'relativePath': row['relative_path'],
                'logicalBytes': row['logical_bytes'], 'fileCount': row['file_count'], 'modifiedAt': row['modified'],
                'fingerprint': row['fingerprint'], 'state': row['state'], 'protected': bool(row['protected']),
                'protectionReasons': json.loads(row['reasons']), 'quarantineId': row['quarantine_id']}

    def entries(self, scan_id, limit=50, offset=0, kind=None):
        self._page(limit, offset)
        if kind is not None and kind not in KINDS:
            raise ValueError('Unknown cache entry kind.')
        where, params = 'scan_id=?', [scan_id]
        if kind:
            where += ' AND kind=?';params.append(kind)
        with self.connection() as conn:
            if not conn.execute('SELECT 1 FROM scans WHERE id=?',(scan_id,)).fetchone():raise KeyError('Cache scan not found.')
            total=conn.execute('SELECT COUNT(*) FROM entries WHERE '+where,params).fetchone()[0]
            items=[self._public_entry(row) for row in conn.execute('SELECT * FROM entries WHERE '+where+' ORDER BY id LIMIT ? OFFSET ?',params+[limit,offset])]
        return {'items':items,'total':total,'limit':limit,'offset':offset,
                'nextOffset':offset+len(items) if offset+len(items)<total else None}

    def _safe_path(self, relative, allow_leaf_symlink=False):
        path=Path(relative)
        if path.is_absolute() or not path.parts or '..' in path.parts:
            raise ValueError('Unsafe cache-relative path.')
        candidate=self.cache_root
        for index,part in enumerate(path.parts):
            candidate=candidate/part
            if candidate.is_symlink() and not (allow_leaf_symlink and index==len(path.parts)-1):
                raise ValueError('Cache paths may not traverse symbolic links.')
        return candidate

    @staticmethod
    def _read_json(path):
        if path.is_symlink() or not path.is_file() or path.stat().st_size>MAX_JSON:
            raise ValueError('Missing, unsafe, or oversized cache metadata.')
        with path.open('rb') as stream:
            raw=stream.read(MAX_JSON+1)
        if len(raw)>MAX_JSON:raise ValueError('Cache metadata exceeds bounded JSON size.')
        value=json.loads(raw)
        if not isinstance(value,dict):raise ValueError('Cache metadata must be an object.')
        return value

    def _manifest(self, directory, key, budget=None):
        data = self._read_json(directory/'COMPLETE.json')
        if data.get('schema') != 1 or data.get('cacheKey') != key or not isinstance(data.get('artifacts'), list):
            raise ValueError('Cache manifest identity/schema is invalid.')
        primary = data.get('inputRelativePath')
        if not isinstance(primary, str) or not primary or Path(primary).is_absolute() or '..' in Path(primary).parts:
            raise ValueError('Cache manifest primary path is invalid.')
        names = set()
        for artifact in data['artifacts']:
            if budget:
                budget.check()
            name = artifact.get('name') if isinstance(artifact, dict) else None
            if not isinstance(name, str) or not name or Path(name).is_absolute() or '..' in Path(name).parts or name in names:
                raise ValueError('Invalid or duplicate manifest artifact path.')
            names.add(name)
            path = self._safe_path(str((directory/name).relative_to(self.cache_root)))
            if (not path.is_file() or type(artifact.get('bytes')) is not int or artifact['bytes'] != path.stat().st_size
                    or not isinstance(artifact.get('sha256'), str) or not KEY.fullmatch(artifact['sha256'])):
                raise ValueError('Manifest artifact metadata or file is missing/invalid.')
        if primary not in names:
            raise ValueError('Manifest primary input is not a registered artifact.')
        return data

    def _item_path(self, row, raw):
        if not ID.fullmatch(row['campaign_id']) or not ID.fullmatch(row['id']):
            raise ValueError('Invalid item identity.')
        expected = self.data_dir/'campaigns'/row['campaign_id']/row['id']
        candidate = Path(raw)
        if not candidate.is_absolute() or '..' in candidate.parts:
            raise ValueError('Unsafe item publication path.')
        candidate.relative_to(expected)
        cursor = self.data_dir
        for part in candidate.relative_to(self.data_dir).parts:
            cursor = cursor/part
            if cursor.is_symlink():
                raise ValueError('Item publication path traverses a symbolic link.')
        return candidate, expected

    def _completed_output_exists(self, row, summary):
        try:
            path, item_root = self._item_path(row, row['result_path'])
            publication = row['result_publication'] if 'result_publication' in row.keys() else None
            if publication is not None:
                if not isinstance(publication, str) or len(publication.encode()) > PUBLICATION_LIMIT:
                    raise ValueError('Oversized or invalid compact result publication.')
                checked_publication(json.loads(publication), row, item_root, path)
                return True
            result = self._read_json(path)
            raw = result.get('structureFile') or result.get('structurePath')
            if not isinstance(raw, str) or not raw:
                raise ValueError('Completed result has no structure publication pointer.')
            structure = Path(raw)
            if not structure.is_absolute():
                structure = item_root/structure
            structure, _ = self._item_path(row, structure)
            if not structure.is_file() or structure.stat().st_size <= 0:
                raise ValueError('Completed structure publication is missing or empty.')
            return True
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            self._diagnostic(summary, 'completed_output_unverified',
                             f"Item {row['id']}: {exc}; retaining its preparation cache.")
            return False

    def _protected_references(self, ref_id, budget, summary):
        """One row/sidecar at a time; uncertainty protects every cache namespace."""
        path=self.data_dir/'campaigns.sqlite3'
        summary['referencesComplete']=False
        if not path.is_file():
            summary['referencesComplete']=True
            return
        source=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
        source.row_factory=sqlite3.Row
        try:
            columns={row[1] for row in source.execute('PRAGMA table_info(campaign_items)')}
            if not columns:raise ValueError('Campaign ledger has no recognized item table.')
            key_column='i.prepared_cache_key' if 'prepared_cache_key' in columns else 'NULL'
            reuse_column='i.preparation_reuse' if 'preparation_reuse' in columns else 'NULL'
            publication_column = 'i.result_publication' if 'result_publication' in columns else 'NULL'
            progress_column = 'i.prepare_progress' if 'prepare_progress' in columns else 'NULL'
            rows=source.execute('SELECT i.id,i.campaign_id,i.attempt,i.input_hash,i.prepared_path,i.stage,i.result_path,c.engine,'+key_column+' AS cache_key,'+reuse_column+' AS chain_reuse,'+publication_column+' AS result_publication,'+progress_column+' AS prepare_progress '
                "FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id WHERE c.status!='cancelled' "
                'AND (i.prepared_path IS NOT NULL'+(' OR i.prepared_cache_key IS NOT NULL' if 'prepared_cache_key' in columns else '')+(' OR i.prepare_progress IS NOT NULL' if 'prepare_progress' in columns else '')+') ORDER BY i.seq')
            with self.connection() as conn:
                conn.execute('DELETE FROM scan_refs WHERE scan_id=?',(ref_id,))
                for row in rows:
                    budget.check()
                    if row['stage'] == 'completed' and self._completed_output_exists(row, summary):
                        continue
                    try:
                        if row['prepare_progress']:
                            from .preparation_control import validate_progress
                            if len(row['prepare_progress'].encode()) > 64*1024:
                                raise ValueError('Oversized deferred preparation references.')
                            progress = validate_progress(json.loads(row['prepare_progress']))
                            conn.execute('INSERT INTO scan_refs VALUES(?,?,?,1) ON CONFLICT(scan_id,kind,cache_key) '
                                         'DO UPDATE SET references_count=references_count+1',(ref_id,'whole',progress['wholeCacheKey']))
                            for entry in progress['completedLeaves'] + progress['pendingLeaves']:
                                budget.check()
                                conn.execute('INSERT INTO scan_refs VALUES(?,?,?,1) ON CONFLICT(scan_id,kind,cache_key) '
                                             'DO UPDATE SET references_count=references_count+1',(ref_id,'leaf',entry['cacheKey']))
                        if row['cache_key'] is None and row['prepared_path'] is None:
                            continue
                        key=row['cache_key']
                        legacy=key is None
                        if key is None:
                            raw, _ = self._item_path(row, row['prepared_path'])
                            descriptor=self._read_json(raw)
                            key=descriptor.get('cacheKey')
                            if descriptor.get('cacheDirectory')!=str(self.cache_root/str(key)):
                                raise ValueError('Prepared descriptor points outside its whole-target cache key.')
                        if not isinstance(key,str) or not KEY.fullmatch(key):raise ValueError('Invalid prepared cache key.')
                        seen=conn.execute('SELECT 1 FROM scan_refs WHERE scan_id=? AND kind=? AND cache_key=?',(ref_id,'whole',key)).fetchone()
                        conn.execute('INSERT INTO scan_refs VALUES(?,?,?,1) ON CONFLICT(scan_id,kind,cache_key) '
                                     'DO UPDATE SET references_count=references_count+1',(ref_id,'whole',key))
                        if seen:continue
                        if not legacy and row['engine']!='alphafold3':continue
                        if not legacy and row['chain_reuse']:
                            if len(row['chain_reuse'])>64*1024:raise ValueError('Oversized chain reference metadata.')
                            reuse=json.loads(row['chain_reuse'])
                        else:
                            whole=self._safe_path(key)
                            reuse=self._manifest(whole,key,budget).get('chainReuse')
                        if reuse is not None:
                            if (not isinstance(reuse,dict) or reuse.get('strategy')!='af3-chain-reuse-v1'
                                or not isinstance(reuse.get('entries'),list) or len(reuse['entries'])>26):
                                raise ValueError('Unknown chain-reference metadata in the prepared manifest.')
                            for entry in reuse['entries']:
                                leaf=entry.get('cacheKey') if isinstance(entry,dict) else None
                                if not isinstance(leaf,str) or not KEY.fullmatch(leaf):raise ValueError('Invalid chain cache reference.')
                                conn.execute('INSERT INTO scan_refs VALUES(?,?,?,1) ON CONFLICT(scan_id,kind,cache_key) '
                                             'DO UPDATE SET references_count=references_count+1',(ref_id,'leaf',leaf))
                    except (OSError,ValueError,TypeError,UnicodeError) as exc:
                        summary['failClosed']=True
                        self._diagnostic(summary,'unknown_reference',f"Item {row['id']}: {exc}; all cache entries are protected.")
                summary['referencesComplete']=True
        except (sqlite3.Error,OSError,ValueError) as exc:
            summary['failClosed']=True
            self._diagnostic(summary,'reference_snapshot_failed',str(exc))
        finally:source.close()

    def _measure(self, path, budget):
        """Filesystem frontier and sorted fingerprint records live in SQLite temp tables."""
        with self.connection() as conn:
            conn.executescript('CREATE TEMP TABLE walk_queue(path TEXT PRIMARY KEY); CREATE TEMP TABLE walk_nodes(path TEXT PRIMARY KEY,signature TEXT);')
            budget.check(visit=True)
            conn.execute('INSERT INTO walk_queue VALUES(?)',('',))
            size,files,modified,unsafe=0,0,0.0,False
            while True:
                pending=conn.execute('SELECT path FROM walk_queue LIMIT 1').fetchone()
                if pending is None:break
                relative=pending[0];conn.execute('DELETE FROM walk_queue WHERE path=?',(relative,))
                budget.check()
                current=path/relative if relative else path
                st=current.lstat();modified=max(modified,st.st_mtime)
                mode=stat.S_IFMT(st.st_mode)
                if stat.S_ISREG(st.st_mode):size+=st.st_size;files+=1
                elif not stat.S_ISDIR(st.st_mode):unsafe=True
                signature=[relative,mode,st.st_size,st.st_mtime_ns,st.st_ino,st.st_dev]
                conn.execute('INSERT INTO walk_nodes VALUES(?,?)',(relative,encoded(signature)))
                if stat.S_ISDIR(st.st_mode):
                    with os.scandir(current) as children:
                        for child in children:
                            budget.check(visit=True)
                            child_rel=str(Path(relative)/child.name)
                            conn.execute('INSERT INTO walk_queue VALUES(?)',(child_rel,))
            digest=hashlib.sha256()
            for row in conn.execute('SELECT signature FROM walk_nodes ORDER BY path'):
                digest.update(row[0].encode());digest.update(b'\n')
            return {'bytes':size,'files':files,'modified':modified,'fingerprint':digest.hexdigest(),'unsafe':unsafe}

    @staticmethod
    def _kind(name):
        if KEY.fullmatch(name):return 'whole'
        if PARTIAL.fullmatch(name):return 'partial'
        return 'unknown'

    def _roots(self, budget):
        with os.scandir(self.cache_root) as roots:
            for root in roots:
                budget.check()
                if root.name in {'.maintenance.lock','.maintenance.intent.lock'} or re.fullmatch('[0-9a-f]{64}\\.lock',root.name):continue
                if root.name==CHAIN_DIR and root.is_dir(follow_symlinks=False):
                    with os.scandir(root.path) as leaves:
                        for leaf in leaves:
                            budget.check()
                            if re.fullmatch('[0-9a-f]{64}\\.lock',leaf.name):continue
                            kind=self._kind(leaf.name)
                            yield f'{CHAIN_DIR}/{leaf.name}',('leaf' if kind=='whole' else kind),None
                elif root.name=='.quarantine' and root.is_dir(follow_symlinks=False):
                    with os.scandir(root.path) as records:
                        for record in records:
                            budget.check()
                            with self.connection() as conn:
                                known=conn.execute('SELECT * FROM quarantine WHERE id=? AND state IN (\'active\',\'purge_interrupted\')',(record.name,)).fetchone()
                            if known and record.is_dir(follow_symlinks=False) and (Path(record.path)/'payload').exists():
                                yield known['payload_path'],'quarantine',known['id']
                            else:yield '.quarantine/'+record.name,'unknown',None
                else:yield root.name,self._kind(root.name),None

    def _reference_reason(self, conn, ref_id, relative, kind, quarantine_id=None):
        if kind=='quarantine':
            record=conn.execute('SELECT original_path,kind FROM quarantine WHERE id=?',(quarantine_id,)).fetchone()
            if not record:return 'unknown_quarantine_record'
            relative,kind=record['original_path'],record['kind']
        key=Path(relative).name
        if kind in {'whole','leaf'} and conn.execute('SELECT 1 FROM scan_refs WHERE scan_id=? AND kind=? AND cache_key=?',(ref_id,kind,key)).fetchone():
            return 'retained_campaign_reference'
        return None

    def _reconcile(self,budget=None):
        with self.connection() as conn:
            for row in conn.execute("SELECT * FROM quarantine WHERE state IN ('moving','restoring','purging')"):
                if budget:budget.check()
                original=self._safe_path(row['original_path']);payload=self._safe_path(row['payload_path'])
                if row['state']=='moving':state='active' if payload.exists() and not original.exists() else ('aborted' if original.exists() and not payload.exists() else 'conflict')
                elif row['state']=='restoring':state='restored' if original.exists() and not payload.exists() else ('active' if payload.exists() and not original.exists() else 'conflict')
                else:state='purged' if not payload.exists() else 'purge_interrupted'
                conn.execute('UPDATE quarantine SET state=? WHERE id=?',(state,row['id']))
                operation=row['current_plan_id'] or row['plan_id']
                if state in {'active','restored','purged'}:
                    if row['state']=='moving' and state=='active':
                        conn.execute("UPDATE plan_entries SET state='applied' WHERE plan_id=? AND relative_path=?",(row['plan_id'],row['original_path']))
                    elif state in {'restored','purged'}:
                        conn.execute("UPDATE plan_entries SET state='applied' WHERE plan_id=? AND quarantine_id=?",(operation,row['id']))

    def scan(self,max_entries=100000,max_seconds=30,cancelled=lambda:False,lock_timeout=60):
        scan_id = uuid.uuid4().hex
        with self._operation_lock('scan-' + scan_id, cancelled):
            return self._scan(scan_id, max_entries, max_seconds, cancelled, lock_timeout)

    def _scan(self,scan_id,max_entries,max_seconds,cancelled,lock_timeout):
        if type(max_entries) is not int or max_entries<1 or not isinstance(max_seconds,(int,float)) or isinstance(max_seconds,bool) or not 0<max_seconds<=86400:
            raise ValueError('Scan limits must be positive: max_entries and max_seconds (at most 86400).')
        summary={'id':scan_id,'operationId':'scan-'+scan_id,'status':'waiting_lock','startedAt':time.time(),'finishedAt':None,
                 'truncated':False,'scannedEntries':0,'completedEntries':0,'logicalBytes':0,'protectedEntries':0,
                 'byKind':{kind:{'entries':0,'logicalBytes':0} for kind in KINDS},'failClosed':False,
                 'referencesComplete':False,'inventoryComplete':False,'unreadableEntries':0,'diagnostics':[],'diagnosticCount':0,'scope':'controller-preparation-cache',
                 'sizeBasis':'logical regular-file lengths; metadata inventory, not a content integrity audit'}
        with self.connection() as conn:conn.execute('INSERT INTO scans VALUES(?,?,?)',(scan_id,time.time(),encoded(summary)))
        budget=None
        try:
            with exclusive_cache(self.cache_root,cancelled=cancelled,timeout=lock_timeout):
                budget=Budget(max_entries,max_seconds,cancelled)
                self._reconcile(budget)
                summary['status']='scanning_references';self._update_scan(scan_id,summary)
                self._protected_references(scan_id,budget,summary)
                summary['status']='scanning_entries';self._update_scan(scan_id,summary)
                last_update=time.monotonic()
                for relative,kind,qid in self._roots(budget):
                    budget.check()
                    reasons=[];state='complete'
                    try:
                        path=self._safe_path(relative,allow_leaf_symlink=True)
                        measure=self._measure(path,budget)
                        if measure['unsafe']:kind='unknown';reasons.append('symlink_or_special_file')
                        if kind=='partial' and not path.is_dir():kind='unknown';reasons.append('partial_is_not_directory')
                        if kind in {'whole','leaf'}:
                            try:self._manifest(path,path.name,budget)
                            except (ValueError,UnicodeError):kind='unknown';state='malformed';reasons.append('malformed_manifest')
                    except (OSError,ValueError) as exc:
                        summary['unreadableEntries'] += 1
                        summary['failClosed'] = True
                        self._diagnostic(summary,'entry_unreadable',f'{relative}: {exc}')
                        continue
                    if kind=='unknown':reasons.append('unmanaged_or_unsafe_entry')
                    if summary['failClosed'] or not summary['referencesComplete']:reasons.append('reference_snapshot_uncertain')
                    with self.connection() as conn:
                        reference=self._reference_reason(conn,scan_id,relative,kind,qid)
                        if reference:reasons.append(reference)
                        conn.execute('INSERT INTO entries(scan_id,relative_path,kind,logical_bytes,file_count,modified,fingerprint,state,protected,reasons,quarantine_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                     (scan_id,relative,kind,measure['bytes'],measure['files'],measure['modified'],measure['fingerprint'],state,int(bool(reasons)),encoded(reasons),qid))
                    summary['completedEntries']+=1
                    summary['logicalBytes']+=measure['bytes']
                    summary['byKind'][kind]['entries']+=1;summary['byKind'][kind]['logicalBytes']+=measure['bytes']
                    summary['protectedEntries']+=int(bool(reasons));summary['scannedEntries']=budget.visited
                    if time.monotonic()-last_update>=.25:
                        self._update_scan(scan_id,summary);last_update=time.monotonic()
                summary['status']='completed'
        except ScanLimit as exc:
            summary['truncated']=True;summary['status']='completed'
            if not summary['referencesComplete']:summary['failClosed']=True
            self._diagnostic(summary,'scan_limit',str(exc))
        except LifecycleCancelled as exc:
            summary['status']='cancelled';summary['truncated']=True
            if not summary['referencesComplete']:summary['failClosed']=True
            self._diagnostic(summary,'cancelled',str(exc))
        except Exception as exc:
            summary['status']='cancelled' if cancelled() else 'failed';summary['truncated']=True;summary['failClosed']=True
            self._diagnostic(summary,'scan_failed',str(exc))
        summary['scannedEntries']=budget.visited if budget else 0
        summary['inventoryComplete'] = (summary['status'] == 'completed' and not summary['truncated'] and not summary['unreadableEntries'])
        if summary['failClosed']:
            with self.connection() as conn:
                for row in conn.execute('SELECT id,reasons FROM entries WHERE scan_id=?', (scan_id,)):
                    reasons = json.loads(row['reasons'])
                    if 'inventory_or_references_uncertain' not in reasons:
                        reasons.append('inventory_or_references_uncertain')
                    conn.execute('UPDATE entries SET protected=1,reasons=? WHERE id=?', (encoded(reasons), row['id']))
            summary['protectedEntries'] = summary['completedEntries']
        summary['finishedAt']=time.time();self._update_scan(scan_id,summary)
        return summary

    @staticmethod
    def _public_plan(row):
        if row is None:raise KeyError('Cache plan not found.')
        result={'id':row['id'],'scanId':row['scan_id'],'action':row['action'],'createdAt':row['created'],
                'status':row['status'],'digest':row['digest'],'count':row['count'],'logicalBytes':row['logical_bytes'],
                'reclaimableBytes':row['logical_bytes'] if row['action']=='purge' else 0,
                'policy':json.loads(row['policy']),
                'eligibility':'Only planned catalog entries; live references and fingerprints are revalidated before apply.',
                'sizeBasis':'logical regular-file lengths; physical disk reclamation is not measured'}
        if row['result']:result['result']=json.loads(row['result'])
        return result

    def _plan_digest(self,conn,plan_id):
        plan=conn.execute('SELECT * FROM plans WHERE id=?',(plan_id,)).fetchone()
        digest=hashlib.sha256(encoded([plan_id,plan['scan_id'],plan['action'],json.loads(plan['policy'])]).encode())
        for row in conn.execute('SELECT seq,entry_id,relative_path,kind,fingerprint,logical_bytes,quarantine_id FROM plan_entries WHERE plan_id=? ORDER BY seq',(plan_id,)):
            digest.update(b'\n');digest.update(encoded(list(row)).encode())
        return digest.hexdigest()

    def plan(self,action='quarantine',scan_id=None,min_age_seconds=7*86400,retention_seconds=7*86400,
             kinds=None,quarantine_id=None,cancelled=lambda:False):
        if action not in {'quarantine','purge','restore'}:raise ValueError('Unknown cache plan action.')
        for name,value in [('min_age_seconds',min_age_seconds),('retention_seconds',retention_seconds)]:
            if type(value) is not int or not 0<=value<=10*365*86400:raise ValueError(name+' must be a nonnegative integer of at most 10 years.')
        if kinds is None:kinds=['whole','leaf','partial'] if action=='quarantine' else ['quarantine']
        if not isinstance(kinds,(list,tuple)) or not kinds or any(k not in KINDS for k in kinds):raise ValueError('Unknown cache entry kinds.')
        if action=='restore' and (not isinstance(quarantine_id,str) or not ID.fullmatch(quarantine_id)):
            raise ValueError('Restore requires one explicit quarantine_id.')
        if quarantine_id is not None and (not isinstance(quarantine_id,str) or not ID.fullmatch(quarantine_id)):
            raise ValueError('Invalid quarantine ID.')
        budget=Budget(cancelled=cancelled);now=time.time();plan_id=uuid.uuid4().hex
        with self.connection() as conn:
            scan=conn.execute('SELECT * FROM scans WHERE id=?',(scan_id,)).fetchone() if scan_id else conn.execute('SELECT * FROM scans ORDER BY created DESC LIMIT 1').fetchone()
            if scan is None:raise ValueError('Run an explicit cache scan before planning.')
            summary=json.loads(scan['summary']);scan_id=scan['id']
            if summary['status']!='completed':raise ValueError('A completed cache scan is required before planning.')
            if action!='restore' and (summary['failClosed'] or not summary['referencesComplete']):
                raise ValueError('Uncertain cache references protect all entries; repair the diagnostics before planning removal.')
            policy={'minAgeSeconds':min_age_seconds,'retentionSeconds':retention_seconds,'kinds':list(kinds),
                    'quarantineId':quarantine_id,'scanTruncated':summary['truncated'],
                    'ageBasis':'latest file/directory mtime, not access time; purge uses quarantine time'}
            conn.execute('INSERT INTO plans(id,scan_id,action,created,status,policy) VALUES(?,?,?,?,?,?)',
                         (plan_id,scan_id,action,now,'planned',encoded(policy)))
            count,total=0,0
            for entry in conn.execute('SELECT * FROM entries WHERE scan_id=? ORDER BY id',(scan_id,)):
                budget.check()
                if entry['kind'] not in kinds:continue
                if action=='quarantine':
                    if entry['kind'] not in {'whole','leaf','partial'} or entry['protected'] or now-entry['modified']<min_age_seconds:continue
                else:
                    if entry['kind']!='quarantine' or (quarantine_id and entry['quarantine_id']!=quarantine_id):continue
                    record=conn.execute('SELECT * FROM quarantine WHERE id=?',(entry['quarantine_id'],)).fetchone()
                    if not record or record['state'] not in {'active','purge_interrupted'}:continue
                    if action=='purge' and (entry['protected'] or now-record['quarantined']<retention_seconds):continue
                    if action=='restore' and record['state']!='active':raise ValueError('A partially purged quarantine entry cannot be restored.')
                count+=1;total+=entry['logical_bytes']
                conn.execute('INSERT INTO plan_entries(plan_id,seq,entry_id,relative_path,kind,fingerprint,logical_bytes,quarantine_id) VALUES(?,?,?,?,?,?,?,?)',
                             (plan_id,count,entry['id'],entry['relative_path'],entry['kind'],entry['fingerprint'],entry['logical_bytes'],entry['quarantine_id']))
            if action=='restore' and count!=1:raise ValueError('The selected quarantine entry is absent from this scan or cannot be restored.')
            digest=self._plan_digest(conn,plan_id)
            conn.execute('UPDATE plans SET digest=?,count=?,logical_bytes=? WHERE id=?',(digest,count,total,plan_id))
            result=self._public_plan(conn.execute('SELECT * FROM plans WHERE id=?',(plan_id,)).fetchone())
        return result

    def plan_details(self,plan_id,limit=50,offset=0):
        self._page(limit,offset)
        with self.connection() as conn:
            header=self._public_plan(conn.execute('SELECT * FROM plans WHERE id=?',(plan_id,)).fetchone())
            items=[]
            for row in conn.execute('SELECT * FROM plan_entries WHERE plan_id=? ORDER BY seq LIMIT ? OFFSET ?',(plan_id,limit,offset)):
                qid=row['quarantine_id']
                if qid is None and header['action']=='quarantine':
                    record=conn.execute('SELECT id FROM quarantine WHERE plan_id=? AND original_path=? ORDER BY quarantined DESC LIMIT 1',(plan_id,row['relative_path'])).fetchone()
                    qid=record['id'] if record else None
                items.append({'entryId':row['entry_id'],'relativePath':row['relative_path'],'kind':row['kind'],
                              'fingerprint':row['fingerprint'],'logicalBytes':row['logical_bytes'],
                              'quarantineId':qid,'state':row['state'],'error':row['error'],
                              'eligible':row['state'] in {'planned','applied'}})
        return {'plan':header,'items':items,'total':header['count'],'limit':limit,'offset':offset,
                'nextOffset':offset+len(items) if offset+len(items)<header['count'] else None}

    def _plan_rows(self,plan_id):
        offset=0
        while True:
            with self.connection() as conn:
                rows=conn.execute('SELECT * FROM plan_entries WHERE plan_id=? AND seq>? ORDER BY seq LIMIT 100',(plan_id,offset)).fetchall()
            if not rows:return
            for row in rows:
                offset=row['seq'];yield row

    def _verify_plan(self,plan,budget,refs):
        policy=json.loads(plan['policy']);action=plan['action'];now=time.time()
        for entry in self._plan_rows(plan['id']):
            budget.check()
            if entry['state']=='applied':continue
            path=self._safe_path(entry['relative_path'])
            measured=self._measure(path,budget)
            if measured['unsafe'] or measured['fingerprint']!=entry['fingerprint']:
                raise ValueError('Cache entry changed since the plan; scan and preview a new plan: '+entry['relative_path'])
            with self.connection() as conn:
                protected=self._reference_reason(conn,refs,entry['relative_path'],entry['kind'],entry['quarantine_id'])
                if action!='restore' and protected:raise ValueError('A cache entry became referenced after the plan; no removal was applied.')
                if action=='quarantine':
                    if entry['kind'] not in {'whole','leaf','partial'}:raise ValueError('Unknown entries cannot be quarantined.')
                    if now-measured['modified']<policy['minAgeSeconds']:raise ValueError('Cache entry is younger than the approved retention policy.')
                else:
                    record=conn.execute('SELECT * FROM quarantine WHERE id=?',(entry['quarantine_id'],)).fetchone()
                    if not record or record['state'] not in {'active','purge_interrupted'}:raise ValueError('Quarantine entry is no longer available.')
                    if record['payload_path']!=entry['relative_path']:raise ValueError('Quarantine identity changed.')
                    if action=='purge' and now-record['quarantined']<policy['retentionSeconds']:raise ValueError('Quarantine retention has not elapsed.')
                    if action=='restore':
                        if record['state']!='active':raise ValueError('Partially purged entries cannot be restored.')
                        if self._safe_path(record['original_path']).exists():raise ValueError('Restore would overwrite an existing cache entry.')

    @staticmethod
    def _fsync_directory(path):
        if os.name=='posix':
            descriptor=os.open(path,os.O_RDONLY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)

    def _mutate_one(self,plan,entry):
        action=plan['action'];path=self._safe_path(entry['relative_path']);qid=entry['quarantine_id']
        if action=='quarantine':
            qid=uuid.uuid4().hex;relative=f'.quarantine/{qid}/payload';destination=self._safe_path(relative)
            destination.parent.mkdir(parents=True,exist_ok=False)
            with self.connection() as conn:
                conn.execute('INSERT INTO quarantine(id,original_path,kind,payload_path,logical_bytes,fingerprint,quarantined,state,plan_id) VALUES(?,?,?,?,?,?,?,?,?)',
                             (qid,entry['relative_path'],entry['kind'],relative,entry['logical_bytes'],entry['fingerprint'],time.time(),'moving',plan['id']))
            os.rename(path,destination)
            self._fsync_directory(path.parent);self._fsync_directory(destination.parent)
            final_state='active'
        else:
            with self.connection() as conn:
                record=conn.execute('SELECT * FROM quarantine WHERE id=?',(qid,)).fetchone()
                conn.execute('UPDATE quarantine SET state=?,current_plan_id=? WHERE id=?',('restoring' if action=='restore' else 'purging',plan['id'],qid))
            if action=='restore':
                destination=self._safe_path(record['original_path'])
                destination.parent.mkdir(parents=True,exist_ok=True)
                if destination.exists():raise ValueError('Restore destination already exists; refusing overwrite.')
                os.rename(path,destination)
                self._fsync_directory(destination.parent)
            else:
                # Only a previously quarantined directory from this immutable
                # plan can reach rmtree. No scan/preview ever calls deletion.
                shutil.rmtree(path)
            self._fsync_directory(path.parent)
            try:path.parent.rmdir()
            except OSError:pass
            final_state='restored' if action=='restore' else 'purged'
        with self.connection() as conn:
            conn.execute('UPDATE quarantine SET state=? WHERE id=?',(final_state,qid))
            conn.execute("UPDATE plan_entries SET state='applied',error=NULL WHERE plan_id=? AND seq=?",(plan['id'],entry['seq']))
        return qid

    def apply(self,plan_id,digest,confirm=False,cancelled=lambda:False,lock_timeout=60):
        if confirm is not True:
            raise ValueError('Applying a cache plan requires explicit confirm=True.')
        if not isinstance(plan_id, str) or not ID.fullmatch(plan_id):
            raise ValueError('Invalid cache plan ID.')
        # Serializing the concrete plan also protects its status from an old
        # attempt's late receipt. EX protects the filesystem and reference gap.
        with self._operation_lock('apply-' + plan_id, cancelled):
            with exclusive_cache(self.cache_root,cancelled=cancelled,timeout=lock_timeout):
                with self.connection() as conn:
                    plan=conn.execute('SELECT * FROM plans WHERE id=?',(plan_id,)).fetchone()
                    if plan is None:raise KeyError('Cache plan not found.')
                    if not isinstance(digest,str) or digest!=plan['digest'] or digest!=self._plan_digest(conn,plan_id):
                        raise ValueError('Plan digest does not match the concrete approved plan.')
                    if plan['status']=='completed':return json.loads(plan['result'])
                    if plan['status'] not in {'planned','cancelled','partial','failed','applying','interrupted'}:
                        raise ValueError('This plan cannot be applied.')
                refs='apply-'+uuid.uuid4().hex
                result={'planId':plan_id,'operationId':'apply-'+plan_id,'action':plan['action'],
                        'status':'applying','startedAt':time.time(),'finishedAt':None,
                        'appliedEntries':0,'movedEntries':0,'restoredEntries':0,'purgedEntries':0,
                        'logicalBytes':0,'movedLogicalBytes':0,'restoredLogicalBytes':0,'purgedLogicalBytes':0,
                        'reclaimedBytes':None,'spaceReclaimedMeasured':False,'diagnostics':[]}
                with self.connection() as conn:
                    conn.execute("UPDATE plans SET status='applying',result=? WHERE id=?",(encoded(result),plan_id))
                try:
                    budget=Budget(cancelled=cancelled)
                    self._reconcile(budget)
                    protection={'failClosed':False,'referencesComplete':False,'diagnostics':[]}
                    self._protected_references(refs,budget,protection)
                    if plan['action']!='restore' and (protection['failClosed'] or not protection['referencesComplete']):
                        raise ValueError('Current references are uncertain; all cache entries are protected. No removal was applied.')
                    self._verify_plan(plan,budget,refs)
                    for entry in self._plan_rows(plan_id):
                        budget.check()
                        if entry['state']!='applied':self._mutate_one(plan,entry)
                        result['appliedEntries']+=1;result['logicalBytes']+=entry['logical_bytes']
                        prefix={'quarantine':'moved','restore':'restored','purge':'purged'}[plan['action']]
                        result[prefix+'Entries']+=1
                        result[prefix+'LogicalBytes']+=entry['logical_bytes']
                        with self.connection() as conn:
                            conn.execute('UPDATE plans SET result=? WHERE id=?',(encoded(result),plan_id))
                    result['status']='completed'
                except LifecycleCancelled as exc:
                    result['status']='cancelled';result['diagnostics'].append({'code':'cancelled','message':str(exc)})
                except Exception as exc:
                    result['status']='cancelled' if cancelled() else ('partial' if result['appliedEntries'] else 'failed')
                    result['diagnostics'].append({'code':'apply_failed','message':str(exc)[:1000]})
                result['finishedAt']=time.time()
                with self.connection() as conn:
                    conn.execute('UPDATE plans SET status=?,result=? WHERE id=?',(result['status'],encoded(result),plan_id))
                    conn.execute('DELETE FROM scan_refs WHERE scan_id=?',(refs,))
                return result
