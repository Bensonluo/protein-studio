"""Authenticated worker pull protocol with per-target fencing and real byte transfers.

The controller remains one SQLite authority. Registered hardware is worker-reported,
not remote attestation. Fixture nodes never count as real GPU capacity.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import logging
import math
import os
import re
import shutil
import stat
import struct
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from .campaigns import STAGES, TERMINAL, encode
from .store import ConflictError, utcnow

ROOT = Path(__file__).resolve().parents[1]
IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')

# The same predicates govern remote claims, CPU preparation admission, and the
# cluster diagnostics. r.value is a small registered-worker document, never an
# input or result payload. The list is bounded by the administrator's 128 nodes.
ROUTE_TERMS = (
    "c.engine=json_extract(r.value,'$.engine')",
    "COALESCE(json_extract(c.specification,'$.placement'),'hybrid') IN ('hybrid',json_extract(r.value,'$.location'))",
    "i.sequence_length<=json_extract(r.value,'$.residueLimit')",
    "(json_type(r.value,'$.settings.numSamples') IS NULL OR json_extract(i.input_json,'$.numSamples')=json_extract(r.value,'$.settings.numSamples')) AND (json_type(r.value,'$.settings.numRecycles') IS NULL OR json_extract(i.input_json,'$.numRecycles')=json_extract(r.value,'$.settings.numRecycles'))",
    "COALESCE(json_extract(c.specification,'$.executionProfile.version'),'') IN ('',json_extract(r.value,'$.runtimeVersion'))",
)


def route_predicate(nodes, terms=None):
    if not nodes:
        return '0', []
    terms = ROUTE_TERMS if terms is None else terms
    return ('EXISTS(SELECT 1 FROM json_each(?) r WHERE ' + ' AND '.join('('+t+')' for t in terms) + ')',
            [encode(nodes)])


class AuthenticationError(ValueError):
    pass


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def safe_extract(bundle, destination, max_expanded=1024 ** 3, max_files=4096):
    """Extract only bounded regular ZIP entries; never trust extractall()."""
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    try:
        # Reject excessive central directories before ZipFile materializes them.
        with Path(bundle).open('rb') as source:
            source.seek(max(0,Path(bundle).stat().st_size-65557))
            tail=source.read(65557)
        offset=tail.rfind(b'PK\x05\x06')
        if offset<0 or len(tail)-offset<22:
            raise ValueError('ZIP end record is missing.')
        ending=struct.unpack_from('<4s4H2IH',tail,offset)
        if ending[1] or ending[2] or ending[3]!=ending[4] or ending[4]>max_files or ending[5]>max_files*2048:
            raise ValueError('ZIP central directory exceeds the safe entry/metadata limits.')
        with zipfile.ZipFile(bundle) as archive:
            entries = archive.infolist()
            if len(entries) > max_files:
                raise ValueError('Bundle has too many entries.')
            total, seen = 0, set()
            for entry in entries:
                name = entry.filename
                parts = PurePosixPath(name).parts
                mode = entry.external_attr >> 16
                if (not name or len(name)>512 or '\\' in name or '\x00' in name or name.startswith('/') or
                    '..' in parts or ':' in parts[0] or name in seen or entry.flag_bits & 1 or
                    (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))):
                    raise ValueError('Bundle contains an unsafe or duplicate path/type.')
                seen.add(name)
                total += entry.file_size
                if total > max_expanded or entry.file_size > max_expanded:
                    raise ValueError('Bundle exceeds expanded byte limit.')
                if entry.file_size > 1024 * 1024 and entry.file_size > max(1, entry.compress_size) * 1000:
                    raise ValueError('Bundle compression ratio is unsafe.')
            for entry in entries:
                target = destination.joinpath(*PurePosixPath(entry.filename).parts)
                if not target.resolve().is_relative_to(destination):
                    raise ValueError('Bundle path escapes its destination.')
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open('xb') as out:
                    remaining = entry.file_size
                    while remaining:
                        data = source.read(min(1024 * 1024, remaining))
                        if not data:
                            raise ValueError('Truncated ZIP member.')
                        out.write(data)
                        remaining -= len(data)
                    if source.read(1):
                        raise ValueError('ZIP size mismatch.')
        return destination
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def load_config(path=None):
    path = Path(path or os.environ.get('PROTEIN_STUDIO_WORKER_CONFIG') or ROOT / 'config/workers.example.json')
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('Worker registry exceeds 1 MiB.')
    data = json.loads(path.read_text())
    if data.get('schemaVersion') != 1 or not isinstance(data.get('workers'), list) or len(data['workers']) > 128:
        raise ValueError('Worker registry must have schemaVersion 1 and at most 128 workers.')
    ids = set()
    for node in data['workers']:
        if not IDENTIFIER.fullmatch(node.get('id', '')) or node['id'] in ids:
            raise ValueError('Worker IDs must be valid and unique.')
        ids.add(node['id'])
        if node.get('location') not in ('local', 'aliyun') or node.get('mode') not in ('fixture', 'real'):
            raise ValueError('Worker location/mode must be administrator-configured.')
        if not isinstance(node.get('tokenEnv'), str) or not node['tokenEnv']:
            raise ValueError('Every worker needs a tokenEnv administrator setting.')
        if not node.get('engines') or any(e not in ('reference', 'alphafold3') for e in node['engines']):
            raise ValueError('Resident workers currently support reference and alphafold3.')
        if node['mode'] == 'fixture' and node['engines'] != ['reference']:
            raise ValueError('Fixture credentials may only execute reference replay.')
        maximum=node.get('maxResidues',5000)
        if isinstance(maximum,bool) or not isinstance(maximum,int) or not 1<=maximum<=1000000:
            raise ValueError('Worker maxResidues must be a positive bounded integer.')
        node['maxResidues']=maximum
    for key, default, low, high in [('leaseSeconds', 120, 2, 86400), ('batchTargets', 8, 1, 64),
                                  ('batchResidues', 8192, 1, 1000000),
                                  ('maxBundleBytes', 268435456, 1024, 4 * 1024 ** 3),
                                  ('maxExpandedBytes', 1073741824, 1024, 16 * 1024 ** 3)]:
        value = data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError('Invalid worker registry limit: ' + key)
        data[key] = value
    return data


class DistributedController:
    def __init__(self, app, config=None, enabled=None):
        self.app, self.store = app, app.pipeline.store
        self.enabled = (os.environ.get('PROTEIN_STUDIO_DISTRIBUTED') == '1') if enabled is None else enabled
        self.config = load_config(config) if self.enabled else {'planned': {'localGpuCards': 16}, 'workers': []}
        self.root = app.data_dir / 'transfers'
        self.root.mkdir(exist_ok=True)
        self.stop_event = threading.Event()
        self.thread = None
        with self.store.connection(True) as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS worker_nodes(id TEXT PRIMARY KEY, session TEXT NOT NULL,
                    document TEXT NOT NULL,last_seen REAL NOT NULL,draining INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS worker_batches(id TEXT PRIMARY KEY,node_id TEXT NOT NULL,
                    signature TEXT NOT NULL,gpu_group TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS worker_batch_items(batch_id TEXT NOT NULL,item_id TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,target_id TEXT NOT NULL,token TEXT NOT NULL,attempt INTEGER NOT NULL,
                    status TEXT NOT NULL,delivery TEXT NOT NULL,PRIMARY KEY(batch_id,item_id));
                CREATE INDEX IF NOT EXISTS worker_batch_owner ON worker_batches(node_id,created_at);
                CREATE INDEX IF NOT EXISTS worker_item_token ON worker_batch_items(item_id,token);
                CREATE INDEX IF NOT EXISTS worker_active_items ON worker_batch_items(status,item_id,token);
                CREATE INDEX IF NOT EXISTS worker_batch_created ON worker_batches(created_at);
                CREATE TABLE IF NOT EXISTS worker_transfers(id TEXT PRIMARY KEY,node_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL,item_id TEXT NOT NULL,direction TEXT NOT NULL,bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS worker_transfer_created ON worker_transfers(created_at);
                CREATE TABLE IF NOT EXISTS worker_event_links(event_seq INTEGER PRIMARY KEY
                    REFERENCES campaign_events(seq),node_id TEXT NOT NULL,batch_id TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS campaign_event_time ON campaign_events(time,stage);
            ''')
        self.app.pipeline.preparation_router = self.preparation_route

    def _routing_nodes(self, conn, now):
        """Read only configured registrations and bounded group-occupancy metadata."""
        policies = {node['id']: node for node in self.config['workers']}
        if not policies:
            return []
        placeholders = ','.join('?' for _ in policies)
        occupied = {}
        for row in conn.execute('''SELECT DISTINCT b.node_id,b.gpu_group FROM worker_batches b
                JOIN campaign_items i ON i.batch_id=b.id
                WHERE i.stage='predicting' AND i.lease_until>? AND b.node_id IN ('''+placeholders+')',
                [now]+list(policies)):
            occupied.setdefault(row['node_id'], set()).add(row['gpu_group'])
        result = []
        for row in conn.execute('SELECT * FROM worker_nodes WHERE id IN ('+placeholders+')', list(policies)):
            node, policy = json.loads(row['document']), policies[row['id']]
            if node.get('engine') not in policy['engines'] or node.get('mode') != policy['mode']:
                continue
            node['location'] = policy['location']
            node['residueLimit'] = min(self.config['batchResidues'], policy['maxResidues'], node.get('maxResidues', policy['maxResidues']))
            node['online'] = now-row['last_seen'] < self.config['leaseSeconds']
            node['draining'] = bool(row['draining'])
            node['freeGroups'] = sum(encode(group) not in occupied.get(row['id'], set()) for group in node['gpuGroups'])
            result.append(node)
        return result

    def _data_plane_active(self):
        return self.enabled and self.app.pipeline.prediction_mode == 'workers'

    def preparation_route(self, conn, now):
        # Busy groups remain eligible: preparation should stay ahead of inference.
        nodes = self._routing_nodes(conn, now) if self._data_plane_active() else []
        return route_predicate([node for node in nodes if node['online'] and not node['draining']])

    def start(self):
        if self.enabled and self.thread is None:
            self.thread = threading.Thread(target=self._maintenance, daemon=True, name='distributed-lease-reaper')
            self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(3)

    def _maintenance(self):
        while not self.stop_event.wait(min(2, self.config['leaseSeconds'] / 3)):
            try:
                self.store.recover()
                self._refresh_batches()
                self.app.pipeline.wakeup.set()
            except Exception:
                logging.exception('Distributed lease maintenance failed; retrying on next tick.')

    def _refresh_batches(self):
        with self.store.connection(True) as conn:
            conn.execute('''UPDATE worker_batch_items SET status='expired',delivery='requeued'
                WHERE status='leased' AND NOT EXISTS(SELECT 1 FROM campaign_items i
                  WHERE i.id=worker_batch_items.item_id AND i.token=worker_batch_items.token
                  AND i.stage='predicting' AND i.lease_until>?)''', (time.time(),))

    def authenticate(self, authorization):
        if not self.enabled:
            raise KeyError('Worker data plane is disabled.')
        if os.environ.get('PROTEIN_STUDIO_PREDICTION_MODE', 'legacy') != 'workers':
            raise ConflictError('Worker data plane requires PROTEIN_STUDIO_PREDICTION_MODE=workers.')
        if not isinstance(authorization, str) or not authorization.startswith('Bearer '):
            raise AuthenticationError('Worker bearer credential required.')
        supplied = authorization[7:]
        for entry in self.config['workers']:
            expected = os.environ.get(entry['tokenEnv'], '')
            if len(expected) >= 24 and hmac.compare_digest(supplied, expected):
                return entry
        raise AuthenticationError('Invalid worker credential.')

    def register(self, node, payload):
        if payload.get('workerId', node['id']) != node['id']:
            raise AuthenticationError('Credential does not belong to this worker ID.')
        engine = payload.get('engine')
        if engine not in node['engines']:
            raise ValueError('Worker engine is not allowed by its administrator profile.')
        demo = node['mode'] == 'fixture'
        groups = payload.get('gpuGroups', [[]] if demo else None)
        if not isinstance(groups, list) or not groups or len(groups) > 64:
            raise ValueError('Register actual GPU groups (or [[]] for a fixture).')
        if any(not isinstance(group, list) or len(group) > 64 or any(not isinstance(d, str) or not d or len(d) > 100 for d in group) for group in groups):
            raise ValueError('GPU groups must contain device ID strings.')
        flat = [device for group in groups for device in group]
        if len(flat) != len(set(flat)) or (demo and groups != [[]]) or (not demo and any(not group for group in groups)):
            raise ValueError('Fixture nodes have no GPUs; real GPU groups must be nonempty and disjoint.')
        if not demo and engine == 'reference':
            raise ValueError('Reference replay must be explicitly registered as a fixture.')
        runtime = payload.get('runtimeVersion')
        if not isinstance(runtime, str) or not 1 <= len(runtime) <= 200:
            raise ValueError('Declare the installed runtime/model version.')
        for key in ('numSamples','numRecycles'):
            if key in payload and (isinstance(payload[key],bool) or not isinstance(payload[key],int) or not 1<=payload[key]<=1000):
                raise ValueError('Worker sampling settings must be positive bounded integers.')
        advertised=payload.get('maxResidues',node['maxResidues'])
        if isinstance(advertised,bool) or not isinstance(advertised,int) or advertised<1:
            raise ValueError('Registered residue eligibility must be a positive integer.')
        max_residues=min(node['maxResidues'],advertised)
        session, now = uuid.uuid4().hex, time.time()
        document = dict(id=node['id'], name=node.get('name', node['id']), location=node['location'], mode=node['mode'],
            demo=demo, engine=engine, devices=flat, gpuGroups=groups, runtimeVersion=runtime,
            maxResidues=max_residues,capacityEvidence='administrator/runner eligibility limit; not a measured VRAM guarantee',
            settings={k: payload[k] for k in ('numSamples', 'numRecycles') if k in payload},
            pid=payload.get('pid'), modelInitCount=0, modelLoads=0, loaded=False, processedTargets=0,
            evidence='fixture; no GPU execution' if demo else 'worker-reported, not remotely attested')
        with self.store.connection(True) as conn:
            old = conn.execute('SELECT session FROM worker_nodes WHERE id=?', (node['id'],)).fetchone()
            if old:
                # A new worker incarnation fences all unsubmitted work owned by
                # the old process. Already accepted/validating results remain.
                conn.execute("UPDATE campaign_items SET stage='ready',token=NULL,lease_until=NULL,remote_owner=NULL,batch_id=NULL,attempt=attempt+1 WHERE remote_owner=? AND stage='predicting'", (node['id'],))
            conn.execute('INSERT OR REPLACE INTO worker_nodes VALUES(?,?,?,?,0)', (node['id'], session, encode(document), now))
        self._refresh_batches()
        return {'workerId': node['id'], 'session': session, 'leaseSeconds': self.config['leaseSeconds'],
                'batchTargets': self.config['batchTargets'], 'batchResidues': self.config['batchResidues'],
                'maxBundleBytes': self.config['maxBundleBytes'], 'maxExpandedBytes': self.config['maxExpandedBytes'],
                'mode': node['mode'], 'location': node['location'], 'maxResidues':max_residues}

    def _node(self, conn, node, session):
        row = conn.execute('SELECT * FROM worker_nodes WHERE id=?', (node['id'],)).fetchone()
        if row is None or not hmac.compare_digest(row['session'], str(session)):
            raise AuthenticationError('Register a current worker session before claiming work.')
        return row, json.loads(row['document'])

    def heartbeat(self, node, payload):
        renewed, rejected, accepted = [], [], []
        now = time.time()
        with self.store.connection(True) as conn:
            row, document = self._node(conn, node, payload.get('session'))
            for key in ('pid', 'modelInitCount', 'modelLoads', 'processedTargets', 'restartCount'):
                value = payload.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10 ** 12:
                    document[key] = value
            if node['mode'] == 'fixture':
                document['modelLoads'] = 0
            document['loaded'] = bool(payload.get('loaded', document.get('loaded')))
            document['draining'] = bool(payload.get('draining', False))
            conn.execute('UPDATE worker_nodes SET last_seen=?,draining=?,document=? WHERE id=?',
                         (now, int(document['draining']), encode(document), node['id']))
            leases = payload.get('leases', [])
            if not isinstance(leases, list) or len(leases) > 64:
                raise ValueError('heartbeat leases must contain at most 64 targets.')
            for lease in leases:
                item_id, token = lease.get('itemId'), lease.get('leaseToken')
                changed = conn.execute("""UPDATE campaign_items SET lease_until=? WHERE id=? AND token=?
                    AND remote_owner=? AND stage='predicting' AND lease_until>?
                    AND EXISTS(SELECT 1 FROM campaigns c WHERE c.id=campaign_id AND c.status!='cancelled')""",
                    (now + self.config['leaseSeconds'], item_id, token, node['id'], now)).rowcount
                if changed and lease.get('deliveryState') in {'download','executing','upload'}:
                    conn.execute('UPDATE worker_batch_items SET delivery=? WHERE item_id=? AND token=?',
                                 (lease['deliveryState'],item_id,token))
                    if lease['deliveryState'] in {'executing','upload'}:
                        conn.execute("UPDATE worker_transfers SET status='verified-by-worker' WHERE item_id=? AND batch_id=(SELECT batch_id FROM campaign_items WHERE id=?) AND direction='download'",(item_id,item_id))
                if changed:
                    renewed.append(item_id)
                elif conn.execute("SELECT 1 FROM worker_batch_items bi JOIN worker_batches b ON b.id=bi.batch_id WHERE bi.item_id=? AND bi.token=? AND bi.status='accepted' AND b.node_id=?",(item_id,token,node['id'])).fetchone():
                    accepted.append(item_id)
                else:
                    rejected.append(item_id)
        return {'renewed': renewed, 'rejected': rejected, 'accepted': accepted, 'serverTime': now}

    def claim(self, node, payload):
        now = time.time()
        with self.store.connection(True) as conn:
            row, registration = self._node(conn, node, payload.get('session'))
            if row['draining']:
                return {'batch': None, 'reason': 'draining'}
            group = payload.get('gpuGroup', registration['gpuGroups'][0])
            if group not in registration['gpuGroups']:
                raise ValueError('Claim GPU group was not registered by this worker.')
            group_json = encode(group)
            busy = conn.execute('''SELECT 1 FROM worker_batches b JOIN campaign_items i ON i.batch_id=b.id
                WHERE b.node_id=? AND b.gpu_group=? AND i.stage='predicting' AND i.lease_until>? LIMIT 1''',
                (node['id'], group_json, now)).fetchone()
            if busy:
                return {'batch': None, 'reason': 'gpu_group_busy'}
            # A bounded look-ahead avoids materializing a 100k-target campaign.
            registration.update(location=node['location'], residueLimit=min(self.config['batchResidues'],node['maxResidues'],registration.get('maxResidues',node['maxResidues'])))
            compatible, parameters = route_predicate([registration])
            where="i.stage='ready' AND i.token IS NULL AND c.status='active' AND "+compatible
            rows = conn.execute('SELECT i.*,c.specification FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id WHERE '+where+' ORDER BY i.seq LIMIT 512', parameters).fetchall()
            selected, signature, residues = [], None, 0
            for item in rows:
                job = json.loads(item['input_json'])
                campaign = json.loads(item['specification'])
                job.update(databaseProfile=campaign.get('databaseProfile', {}), executionProfile=campaign.get('executionProfile', {}))
                required = job['executionProfile'].get('version')
                if required and required != registration['runtimeVersion']:
                    continue
                if any(job.get(key) != value for key, value in registration['settings'].items()):
                    continue
                bucket = 1 << max(0, (max(1, item['sequence_length']) - 1).bit_length())
                identity = dict(engine=job['engine'], profile=job['databaseProfile'].get('profileFingerprint'),
                    numSamples=job.get('numSamples'), numRecycles=job.get('numRecycles'), preset=job.get('preset'),
                    execution=job['executionProfile'].get('id'), runtimeVersion=registration['runtimeVersion'], lengthBucket=bucket)
                candidate_signature = encode(identity)
                if signature is not None and candidate_signature != signature:
                    continue
                if item['sequence_length'] + residues > self.config['batchResidues']:
                    continue
                signature = candidate_signature
                selected.append((item, job))
                residues += item['sequence_length']
                if len(selected) >= self.config['batchTargets']:
                    break
            if not selected:
                return {'batch': None, 'reason': 'no_compatible_ready_targets'}
            batch_id, stamp = uuid.uuid4().hex, utcnow()
            conn.execute('INSERT INTO worker_batches VALUES(?,?,?,?,?,?)', (batch_id, node['id'], signature, group_json, stamp, stamp))
            targets = []
            for item, job in selected:
                token = uuid.uuid4().hex
                conn.execute("""UPDATE campaign_items SET stage='predicting',token=?,lease_until=?,started=?,attempt=attempt+1,
                    remote_owner=?,batch_id=?,updated_at=? WHERE id=? AND stage='ready' AND token IS NULL""",
                    (token, now+self.config['leaseSeconds'], now, node['id'], batch_id, stamp, item['id']))
                conn.execute('INSERT INTO worker_batch_items VALUES(?,?,?,?,?,?,?,?)',
                    (batch_id, item['id'], item['campaign_id'], item['target_id'], token, item['attempt']+1, 'leased', 'claimed'))
                self.store.event(conn, item['id'], 'predicting', 'Remote worker claimed target: ' + node['id'])
                event_seq=conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                conn.execute('INSERT INTO worker_event_links VALUES(?,?,?)',(event_seq,node['id'],batch_id))
                targets.append(dict(itemId=item['id'], targetId=item['target_id'], campaignId=item['campaign_id'],
                    attempt=item['attempt']+1, leaseToken=token, job=job,
                    preparedUrl='/worker/v1/prepared/' + item['id'], resultUrl='/worker/v1/result/' + item['id']))
        return {'batch': dict(id=batch_id, batchId=batch_id, gpuGroup=group, signature=json.loads(signature),
                              totalResidues=residues, leaseSeconds=self.config['leaseSeconds'], targets=targets)}

    def _lease(self, node, item_id, token, conn=None):
        if conn is None:
            with self.store.connection() as connection:
                return self._lease(node, item_id, token, connection)
        item = conn.execute('''SELECT i.*,c.specification,c.status AS campaign_status FROM campaign_items i
             JOIN campaigns c ON c.id=i.campaign_id WHERE i.id=?''', (item_id,)).fetchone()
        if item is None:
            raise KeyError('Target not found.')
        if (item['remote_owner'] != node['id'] or item['token'] != token or item['stage'] != 'predicting'
            or not item['lease_until'] or item['lease_until'] <= time.time() or item['campaign_status'] == 'cancelled'):
            raise ConflictError('Target lease is stale, expired, cancelled, or belongs to another worker.')
        result = dict(item)
        job = json.loads(item['input_json'])
        campaign = json.loads(item['specification'])
        job.update(databaseProfile=campaign.get('databaseProfile', {}), executionProfile=campaign.get('executionProfile', {}))
        result['job'] = job
        return result

    def _transfer(self, node, item, direction, path, digest, status='verified'):
        with self.store.connection(True) as conn:
            conn.execute('INSERT INTO worker_transfers VALUES(?,?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,node['id'],item['batch_id'],item['id'],direction,Path(path).stat().st_size,digest,status,utcnow()))

    def prepared_bundle(self, node, item_id, token):
        from .cache_access import shared_cache
        with shared_cache(self.app.pipeline.cache_root, self.stop_event.is_set):
            return self._prepared_bundle_locked(node, item_id, token)

    def _prepared_bundle_locked(self, node, item_id, token):
        item = self._lease(node, item_id, token)
        self.app.pipeline._check_database_snapshot(item['job'])
        descriptor_path = self.app.pipeline._safe_item_path(item, item['prepared_path'])
        descriptor = json.loads(descriptor_path.read_text())
        cache = Path(descriptor['cacheDirectory']).resolve()
        if not cache.is_relative_to(self.app.pipeline.cache_root.resolve()):
            raise ValueError('Prepared cache directory is outside the controller cache.')
        from .staged import _manifest
        manifest = _manifest(cache, descriptor['cacheKey'])
        if manifest is None:
            raise ValueError('Preparation cache is incomplete.')
        output = self.root / (uuid.uuid4().hex + '.zip')
        metadata = dict(schemaVersion=1,itemId=item_id,cacheKey=descriptor['cacheKey'],
                        descriptor={k:v for k,v in descriptor.items() if k not in ('cacheDirectory','manifestPath','inputPath','artifacts')},
                        inputRelativePath=manifest['inputRelativePath'])
        try:
            with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr('manifest.json', encode(metadata))
                for file in [cache/'COMPLETE.json'] + [cache/a['name'] for a in manifest['artifacts']]:
                    bundle.write(file, 'cache/' + file.relative_to(cache).as_posix())
            if output.stat().st_size > self.config['maxBundleBytes']:
                raise OverflowError('Preparation bundle exceeds configured transfer limit.')
            self._lease(node,item_id,token)
            digest = digest_file(output)
            self._transfer(node,item,'download',output,digest,'prepared')
            with self.store.connection(True) as conn:
                conn.execute("UPDATE worker_batch_items SET delivery='download' WHERE item_id=? AND token=?", (item_id,token))
            return output, digest
        except BaseException:
            output.unlink(missing_ok=True)
            raise

    def _receipt(self,node,item_id,token,digest=None):
        with self.store.connection() as conn:
            row=conn.execute("""SELECT t.sha256 FROM worker_batch_items bi JOIN worker_batches b ON b.id=bi.batch_id
                JOIN worker_transfers t ON t.batch_id=bi.batch_id AND t.item_id=bi.item_id
                WHERE bi.item_id=? AND bi.token=? AND bi.status='accepted' AND b.node_id=?
                AND t.direction='upload' AND t.status='accepted' LIMIT 1""",(item_id,token,node['id'])).fetchone()
        if row is None: return None
        if digest is not None and not hmac.compare_digest(row['sha256'],digest):
            raise ConflictError('This attempt already accepted a different output bundle; it cannot be replaced.')
        return {'accepted':True,'alreadyAccepted':True,'itemId':item_id,'sha256':row['sha256']}

    def _execution_metadata(self, node, item, execution):
        """Validate declared resident identity; this is not GPU attestation."""
        if not isinstance(execution,dict):
            raise ValueError('Result execution metadata must be an object.')
        job=item['job']
        if job['engine']=='alphafold3':
            signature=execution.get('modelSignature')
            with self.store.connection() as conn:
                row=conn.execute('SELECT document FROM worker_nodes WHERE id=?',(node['id'],)).fetchone()
            registration=json.loads(row['document']) if row else {}
            if (node['mode']!='real' or execution.get('nativeExecutionMode')!='resident-native'
                or execution.get('isDemo') is not False or type(execution.get('modelLoads')) is not int
                or execution['modelLoads']!=1):
                raise ValueError('AF3 output requires a non-demo native resident with exactly one model load in this resident process.')
            if not isinstance(signature,dict) or signature.get('engine')!='alphafold3' or registration.get('engine')!='alphafold3':
                raise ValueError('AF3 result model signature differs from the registered engine.')
            version=signature.get('modelVersion')
            expected=job.get('executionProfile',{}).get('version')
            if (not isinstance(version,str) or not version or version!=registration.get('runtimeVersion')
                or (expected and version!=expected)):
                raise ValueError('AF3 result model version differs from the registered or campaign runtime.')
            for field in ('numSamples','numRecycles'):
                if type(signature.get(field)) is not int or signature[field]!=job[field]:
                    raise ValueError('AF3 result resident '+field+' differs from the requested target.')
            return dict(nativeExecutionMode='resident-native',isDemo=False,engineVersion=version,
                        seeds=list(job['seeds']),numSamples=job['numSamples'],numRecycles=job['numRecycles'],
                        modelLoads=1,modelSignature=signature)
        return dict(nativeExecutionMode='resident-reference',isDemo=True,modelLoads=0,
                    modelSignature=execution.get('modelSignature'))

    def accept_result(self, node, item_id, token, upload, expected_digest):
        digest = digest_file(upload)
        if not re.fullmatch('[a-f0-9]{64}', expected_digest or '') or not hmac.compare_digest(digest,expected_digest):
            raise ValueError('Uploaded bundle SHA-256 does not match its declared digest.')
        receipt=self._receipt(node,item_id,token,digest)
        if receipt: return receipt
        item = self._lease(node,item_id,token)
        destination = self.app.pipeline.items_root/item['campaign_id']/item_id/f"remote-{item['attempt']}-{token}"
        if destination.exists():
            raise ConflictError('This attempt already has received files; use a new retry attempt.')
        safe_extract(upload,destination,self.config['maxExpandedBytes'])
        published = False
        try:
            manifest_path=destination/'manifest.json'
            if manifest_path.stat().st_size>1024*1024:
                raise ValueError('Result metadata manifest exceeds 1 MiB.')
            metadata = json.loads(manifest_path.read_text())
            if metadata.get('itemId') != item_id or metadata.get('schemaVersion') != 1:
                raise ValueError('Result bundle target identity/schema mismatch.')
            execution=metadata.get('execution',{})
            native_evidence=self._execution_metadata(node,item,execution)
            payload = destination/'payload'
            from . import engine
            source = 'afdb' if item['job']['engine']=='reference' and item['job'].get('example')=='ubiquitin-afdb' else item['job']['engine']
            result = engine._collect_outputs(payload,source,destination,item['job']['chains'], cancelled=self.stop_event.is_set)
            if item['job']['engine']=='alphafold3':
                expected={(s,n) for s in item['job']['seeds'] for n in range(item['job']['numSamples'])}
                actual=set()
                for model in result['rankedModels']:
                    match=re.search(r'seed-(\d+)_sample-(\d+)',str(Path(model['structurePath']).parent))
                    if match: actual.add((int(match[1]),int(match[2])))
                if actual != expected:
                    raise ValueError('AF3 result seed/sample coverage differs from the requested target.')
                result.update(samplingComplete=True,actualSampleCount=len(actual),requestedSampleCount=len(expected))
            if item['job']['engine']=='reference':
                result['provenance']=engine.load_example(item['job']['example'])['provenance']
                if item['job'].get('example') == 'ubiquitin-afdb':
                    from .parsers import parse_confidence
                    confidence = list(payload.rglob('P0CG47_pae.json'))
                    if len(confidence) == 1:
                        result.update(parse_confidence(confidence[0]))
            else:
                result['provenance']=dict(mode='prediction',newPrediction=True,sourceLabel=item['job']['engine'])
            result['provenance'].update(workerId=node['id'],workerLocation=node['location'],batchId=item['batch_id'],
                transportSha256=digest,workerMode=node['mode'],demo=node['mode']=='fixture',executionMode='resident-worker')
            result['provenance'].update(native_evidence,transportMode='resident-worker')
            result['provenance'].update(location=node['location'],attempt=item['attempt'],
                residentPid=execution.get('residentPid'),clientGeneration=execution.get('clientGeneration'),
                residentSessionId=execution.get('residentSessionId'),nodeModelLoads=execution.get('nodeModelLoads'),
                runtimeEvidence='authenticated-worker declarations checked against registration and job; not remote GPU attestation')
            result['artifacts']=[{'path':str(p)} for p in payload.rglob('*') if p.is_file()]
            result['returnedModelCount']=len(result['rankedModels'])
            from .worker_pipeline import write_json
            candidate=destination/'candidate.json'
            write_json(candidate,result)
            with self.store.connection(True) as conn:
                fresh=self._lease(node,item_id,token,conn)
                elapsed=max(0,time.time()-(fresh['started'] or time.time()))
                conn.execute("""UPDATE campaign_items SET stage='validating',candidate_path=?,token=NULL,lease_until=NULL,
                    remote_owner=NULL,batch_id=NULL,started=NULL,predict_seconds=predict_seconds+?,updated_at=?
                    WHERE id=? AND token=?""",(str(candidate),elapsed,utcnow(),item_id,token))
                conn.execute("UPDATE worker_batch_items SET status='accepted',delivery='uploaded' WHERE item_id=? AND token=?",(item_id,token))
                self.store.event(conn,item_id,'validating','Verified remote output bundle accepted; independent CPU validation queued.')
                event_seq=conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                conn.execute('INSERT INTO worker_event_links VALUES(?,?,?)',(event_seq,node['id'],item['batch_id']))
                conn.execute('INSERT INTO worker_transfers VALUES(?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex,node['id'],item['batch_id'],item_id,'upload',Path(upload).stat().st_size,digest,'accepted',utcnow()))
            published = True
            self.app.pipeline.wakeup.set()
            return {'accepted':True,'itemId':item_id,'stage':'validating','sha256':digest}
        except BaseException:
            # No published pointer refers to a rejected/uncommitted receive dir.
            if not published:
                shutil.rmtree(destination,ignore_errors=True)
            raise

    def fail(self,node,payload):
        item_id,token=payload.get('itemId'),payload.get('leaseToken')
        with self.store.connection(True) as conn:
            self._lease(node,item_id,token,conn)
            conn.execute("""UPDATE campaign_items SET stage='failed',failed_stage='predicting',error=?,
                token=NULL,lease_until=NULL,started=NULL,remote_owner=NULL,batch_id=NULL,updated_at=? WHERE id=? AND token=?""",
                (str(payload.get('error','Worker execution failed'))[:2000],utcnow(),item_id,token))
            conn.execute("UPDATE worker_batch_items SET status='failed',delivery='failed' WHERE item_id=? AND token=?",(item_id,token))
            self.store.event(conn,item_id,'failed',str(payload.get('error','Worker execution failed'))[:2000])
        return {'itemId':item_id,'stage':'failed'}

    def _location_queue(self, conn, location, nodes):
        registered = [node for node in nodes if node['location'] == location]
        online = [node for node in registered if node['online']]
        available = [node for node in online if not node['draining']]
        free = [node for node in available if node['freeGroups']]
        labels = {
            'data_plane_disabled': 'Remote worker execution is disabled.',
            'no_registered_worker': 'No administrator-configured worker has registered at this location.',
            'no_online_worker': 'Registered workers have no fresh heartbeat.',
            'all_workers_draining': 'All online workers are draining.',
            'engine_mismatch': 'No online worker runs this engine.',
            'residue_limit': 'Input exceeds the compatible workers\' declared residue or batch limit.',
            'sampling_mismatch': 'Sampling or recycle settings do not match a compatible resident.',
            'runtime_mismatch': 'Required runtime version does not match a compatible resident.',
            'gpu_groups_busy': 'Compatible workers have no currently free registered execution group.',
        }
        fixed = ('data_plane_disabled' if not self._data_plane_active() else
                 'no_registered_worker' if not registered else 'no_online_worker' if not online else
                 'all_workers_draining' if not available else None)
        parameters = []
        if fixed:
            reason = "'"+fixed+"'"
        else:
            cases = []
            for terms, pool, code in [(ROUTE_TERMS[:2], available, 'engine_mismatch'),
                                      (ROUTE_TERMS[:3], available, 'residue_limit'),
                                      (ROUTE_TERMS[:4], available, 'sampling_mismatch'),
                                      (ROUTE_TERMS, available, 'runtime_mismatch'),
                                      (ROUTE_TERMS, free, 'gpu_groups_busy')]:
                condition, values = route_predicate(pool, terms)
                cases.append('WHEN NOT ('+condition+") THEN '"+code+"'")
                parameters.extend(values)
            reason = 'CASE '+' '.join(cases)+" ELSE 'eligible' END"
        rows = conn.execute('''SELECT reason,COUNT(*) AS n FROM (
            SELECT '''+reason+''' AS reason FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id
            WHERE c.status='active' AND i.stage='ready' AND i.token IS NULL
            AND COALESCE(json_extract(c.specification,'$.placement'),'hybrid') IN ('hybrid',?))
            GROUP BY reason''', parameters+[location]).fetchall()
        counts = {row['reason']: row['n'] for row in rows}
        total, eligible = sum(counts.values()), counts.get('eligible', 0)
        return dict(readyCandidates=total, eligibleReady=eligible, blockedReady=total-eligible,
            registeredWorkers=len(registered), onlineWorkers=len(online), acceptingWorkers=len(available),
            freeExecutionGroups=sum(node['freeGroups'] for node in available),
            reasons=[dict(code=code, label=labels[code], count=counts[code]) for code in labels if counts.get(code)],
            scope='All active, unleased ready targets whose placement allows this location. Hybrid targets may occur in both locations.',
            eligibility='Exact predicate snapshot; ignores batch ordering and is not a measured VRAM fit, throughput, or start-time guarantee.')

    def _pipeline_snapshot(self, conn, now):
        counts = {stage: 0 for stage in STAGES}
        active = {stage: 0 for stage in STAGES}
        paused = {stage: 0 for stage in STAGES}
        for row in conn.execute('''SELECT i.stage,c.status,COUNT(*) AS n FROM campaign_items i
                JOIN campaigns c ON c.id=i.campaign_id GROUP BY i.stage,c.status'''):
            stage = 'cancelled' if row['status'] == 'cancelled' and row['stage'] not in TERMINAL else row['stage']
            counts[stage] += row['n']
            if row['status'] == 'active':
                active[stage] += row['n']
            elif row['status'] == 'paused':
                paused[stage] += row['n']
        campaigns = conn.execute('''WITH unfinished AS (
            SELECT campaign_id,COUNT(*) AS n FROM campaign_items
            WHERE stage NOT IN ('completed','failed','cancelled') GROUP BY campaign_id)
            SELECT COUNT(*) AS total,SUM(c.status='active') AS active,SUM(c.status='paused') AS paused,
            SUM(c.status='cancelled') AS cancelled,SUM(c.sealed=1) AS sealed,SUM(c.sealed=0) AS unsealed,
            SUM(c.sealed=1 AND (c.status='cancelled' OR COALESCE(u.n,0)=0)) AS complete,
            SUM(c.status='active' AND COALESCE(u.n,0)>0) AS activeWithUnfinishedItems
            FROM campaigns c LEFT JOIN unfinished u ON u.campaign_id=c.id''').fetchone()
        campaign_counts = {key: campaigns[key] or 0 for key in campaigns.keys()}
        nodes = self._routing_nodes(conn, now)
        routed = [node for node in nodes if node['online'] and not node['draining']] if self._data_plane_active() else []
        predicate, parameters = route_predicate(routed)
        pipeline = self.app.pipeline
        if pipeline.prediction_mode != 'workers':
            predicate, parameters = '1', []
        routable = conn.execute("SELECT COUNT(*) FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id WHERE c.status='active' AND i.stage='ready' AND i.token IS NULL AND ("+predicate+')', parameters).fetchone()[0]
        used = routable+active['preparing']
        window = 300
        stamp = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        start, end = stamp(now-window), stamp(now)
        recent = dict(windowSeconds=window, **{'from': start, 'to': end},
            basis='Persisted transition events in this UTC window; target counts are distinct within each category. Recovered includes only lease-revocation safe-stage recovery, not routing deferral or session replacement.')
        for name, condition in [('completed', "stage='completed' AND message='Stage published: completed.'"),
                                ('failed', "stage='failed'"),
                                ('recovered', "message='Lease revoked; safe stage recovery.'")]:
            row = conn.execute('SELECT COUNT(*) AS events,COUNT(DISTINCT item_id) AS targets FROM campaign_events WHERE time>=? AND time<=? AND '+condition, (start, end)).fetchone()
            recent[name] = dict(row)
        return dict(asOf=end, scope='All persisted campaigns and targets in one SQLite read snapshot; independent of the recent batch/transfer lists. Cancelled campaigns count unfinished targets as cancelled.',
            totalItems=sum(counts.values()), counts=counts, campaigns=campaign_counts,
            queue=dict(readyActive=active['ready'], preparingActive=active['preparing'], routableReady=routable,
                blockedReady=active['ready']-routable, pausedReady=paused['ready'], pausedPreparing=paused['preparing'],
                highWaterMark=pipeline.ready_cap, used=used, availableSlots=max(0,pipeline.ready_cap-used),
                backpressured=used>=pipeline.ready_cap,
                scope='Preparation budget: active preparing plus active ready compatible with an online non-draining worker. Busy groups still allow preparation ahead; paused campaigns do not consume this budget.' if pipeline.prediction_mode == 'workers' else 'Active preparing plus active ready; paused campaigns do not consume this budget.'),
            configuration=dict(predictionMode=pipeline.prediction_mode, prepWorkers=pipeline.prep_workers,
                validationWorkers=pipeline.validation_workers, localPredictionWorkers=0 if pipeline.prediction_mode=='workers' else pipeline.gpu_workers,
                readyCap=pipeline.ready_cap, stageLeaseSeconds=pipeline.lease_seconds,
                workerLeaseSeconds=self.config.get('leaseSeconds',120), batchTargets=self.config.get('batchTargets',8),
                batchResidues=self.config.get('batchResidues',8192)),
            locations={location: self._location_queue(conn,location,nodes) for location in ('local','aliyun')}, recent=recent)

    def cluster(self):
        self._refresh_batches()
        now=time.time()
        with self.store.connection() as conn:
            conn.execute('BEGIN')
            nodes=[]
            for row in conn.execute('SELECT * FROM worker_nodes ORDER BY id LIMIT 128'):
                node=json.loads(row['document'])
                node.update(lastHeartbeat=row['last_seen'],draining=bool(row['draining']),status='online' if now-row['last_seen']<self.config.get('leaseSeconds',120) else 'offline')
                nodes.append(node)
            batches=[]
            for row in conn.execute('SELECT * FROM worker_batches ORDER BY created_at DESC LIMIT 50'):
                members=[]
                for member in conn.execute('''SELECT b.*,i.stage,i.token AS current_token,i.lease_until,c.status AS campaign_status,
                    c.name AS campaign_name,json_extract(i.input_json,'$.name') AS target_name
                    FROM worker_batch_items b JOIN campaign_items i ON i.id=b.item_id JOIN campaigns c ON c.id=i.campaign_id
                    WHERE b.batch_id=?''',(row['id'],)):
                    delivery='validated' if member['status']=='accepted' and member['stage']=='completed' else member['delivery']
                    members.append(dict(itemId=member['item_id'],campaignId=member['campaign_id'],targetId=member['target_id'],stage=member['stage'],
                        name=member['target_name'] or member['target_id'],campaignName=member['campaign_name'],
                        deliveryState=delivery,leaseActive=member['token']==member['current_token'] and (member['lease_until'] or 0)>now,
                        status=member['status']))
                accepted=sum(m['status']=='accepted' for m in members)
                remaining=sum(m['status']=='leased' for m in members)
                location=next((n['location'] for n in nodes if n['id']==row['node_id']),None)
                batches.append(dict(id=row['id'],nodeId=row['node_id'],location=location,status='running' if remaining else 'finished',
                    total=len(members),completed=accepted,remaining=remaining,createdAt=row['created_at'],signature=json.loads(row['signature']),
                    gpuGroup=json.loads(row['gpu_group']),members=members))
            transfers=[dict(id=r['id'],nodeId=r['node_id'],batchId=r['batch_id'],itemId=r['item_id'],direction=r['direction'],bytes=r['bytes'],
                sha256=r['sha256'],status=r['status'],createdAt=r['created_at']) for r in conn.execute('SELECT * FROM worker_transfers ORDER BY created_at DESC LIMIT 100')]
            events=[]
            for event in conn.execute('''SELECT e.*,l.node_id,l.batch_id FROM campaign_events e
                    LEFT JOIN worker_event_links l ON l.event_seq=e.seq
                    WHERE EXISTS(SELECT 1 FROM worker_batch_items bi WHERE bi.item_id=e.item_id)
                    AND (e.message LIKE 'Remote worker%' OR e.message LIKE 'Lease revoked%'
                    OR e.message LIKE 'Verified remote%' OR e.message LIKE 'Explicit retry%')
                    ORDER BY e.seq DESC LIMIT 30'''):
                # Recovery/retry belongs to the controller. Earlier database
                # versions did not persist event assignment; keep those messages
                # unlinked instead of guessing from an item's later assignments.
                origin='worker' if event['node_id'] else ('unlinked-historical' if event['message'].startswith(('Remote worker','Verified remote')) else 'controller')
                events.append(dict(id=event['seq'],timestamp=event['time'],type=event['stage'],message=event['message'],
                    itemId=event['item_id'],nodeId=event['node_id'],batchId=event['batch_id'],origin=origin))
            pipeline=self._pipeline_snapshot(conn,now)
        return dict(enabled=self.enabled,planned=self.config.get('planned',{'localGpuCards':16}),
            topology={'scheduler':'single-controller SQLite','transport':'authenticated pull + SHA-256 bundles'},nodes=nodes,batches=batches,transfers=transfers,events=events,pipeline=pipeline)

    def handle(self, handler):
        """Called before the local browser-origin guard; bearer is mandatory."""
        node=self.authenticate(handler.headers.get('Authorization'))
        if handler.headers.get('Origin'):
            raise AuthenticationError('Worker endpoints are not browser-origin endpoints.')
        parsed=urlsplit(handler.path)
        parts=unquote(parsed.path).strip('/').split('/')
        method=handler.command
        if method=='POST' and len(parts)==3:
            body=handler._read_json()
            routes={'register':self.register,'heartbeat':self.heartbeat,'claim':self.claim,'failure':self.fail}
            action=routes.get(parts[2])
            if not action: raise KeyError('Unknown worker endpoint.')
            handler._json(200,action(node,body))
            return
        if len(parts)!=4 or not re.fullmatch('[a-f0-9]{32}',parts[3]):
            raise KeyError('Unknown worker endpoint.')
        item_id,token=parts[3],handler.headers.get('X-Lease-Token','')
        if method=='GET' and parts[2]=='prepared':
            path,digest=self.prepared_bundle(node,item_id,token)
            try:
                with path.open('rb') as stream:
                    handler._headers(200,'application/zip',path.stat().st_size,X_Content_SHA256=digest)
                    shutil.copyfileobj(stream,handler.wfile,1024*1024)
                with self.store.connection(True) as conn:
                    conn.execute("UPDATE worker_transfers SET status='sent' WHERE item_id=? AND node_id=? AND sha256=? AND direction='download' AND status='prepared'",(item_id,node['id'],digest))
            finally:
                path.unlink(missing_ok=True)
            return
        if method=='POST' and parts[2]=='result':
            if self._receipt(node,item_id,token) is None:
                self._lease(node,item_id,token)
            if handler.headers.get('Transfer-Encoding'):
                handler.close_connection=True
                raise ValueError('Chunked uploads are not supported.')
            length=int(handler.headers.get('Content-Length','0'))
            if not 0<length<=self.config['maxBundleBytes']:
                handler.close_connection=True
                raise OverflowError('Result upload exceeds configured transfer size limit.')
            if handler.headers.get('Content-Type','').split(';')[0]!='application/zip':
                handler.close_connection=True
                raise TypeError('Result upload must use application/zip.')
            temporary=self.root/(uuid.uuid4().hex+'.upload')
            try:
                handler.connection.settimeout(60)
                with temporary.open('xb') as out:
                    remaining=length
                    while remaining:
                        block=handler.rfile.read(min(1024*1024,remaining))
                        if not block: raise ValueError('Truncated result upload.')
                        out.write(block)
                        remaining-=len(block)
                handler._json(200,self.accept_result(node,item_id,token,temporary,handler.headers.get('X-Content-SHA256')))
            finally:
                temporary.unlink(missing_ok=True)
            return
        raise KeyError('Unknown worker endpoint.')
