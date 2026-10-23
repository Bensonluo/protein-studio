"""Bounded CPU preparation / GPU execution / CPU validation workers.

This is a single-controller, local SQLite scheduler. An external command may
execute remotely; remote provisioning and cancellation remain that runner's job.
"""
from __future__ import annotations

import copy
import importlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

from .campaigns import CampaignStore, publication_file, checked_publication, PUBLICATION_LIMIT
from .preparation_resources import PreparationResourceGuard
from .preparation_control import PreparationDeferred, retry_delay
from .cache_access import shared_cache
from contextlib import nullcontext

LOG = logging.getLogger('protein-studio.pipeline')


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    if os.name == 'posix':
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class PipelineScheduler:
    def __init__(self, data_dir, staged_module=None, prep_workers=2, gpu_workers=1,
                 validation_workers=1, ready_cap=64, lease_seconds=60, prediction_gate=None,
                 preparation_resources=None):
        self.root = Path(data_dir).resolve()
        self.store = CampaignStore(self.root / 'campaigns.sqlite3')
        self.items_root = self.root / 'campaigns'
        self.items_root.mkdir(exist_ok=True)
        self.cache_root = self.root / 'cache'
        self.cache_root.mkdir(exist_ok=True)
        self.preparation_resources = PreparationResourceGuard(
            [self.cache_root, self.items_root], config=preparation_resources)
        self.staged_module = staged_module
        self.prediction_mode = os.environ.get('PROTEIN_STUDIO_PREDICTION_MODE', 'legacy')
        if self.prediction_mode not in {'legacy', 'workers'}:
            raise ValueError('PROTEIN_STUDIO_PREDICTION_MODE must be legacy or workers.')
        self.max_residues_per_target = int(os.environ.get('PROTEIN_STUDIO_MAX_RESIDUES', '5000'))
        if self.max_residues_per_target < 1:
            raise ValueError('PROTEIN_STUDIO_MAX_RESIDUES must be a positive integer.')
        self.prep_workers = max(1, min(int(prep_workers), 64))
        self.gpu_workers = max(1, min(int(gpu_workers), 64))
        self.validation_workers = max(1, min(int(validation_workers), 16))
        self.ready_cap = max(1, int(ready_cap))
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.prediction_gate = prediction_gate or threading.BoundedSemaphore(self.gpu_workers)
        self.stop_event = threading.Event()
        self.wakeup = threading.Event()
        self.threads = []
        self.active = {}
        self.active_lock = threading.Lock()
        # DistributedController installs a transaction-local eligibility query.
        # Before registration/configuration, worker mode keeps inputs waiting.
        self.preparation_router = (lambda conn, now: ('0', [])) if self.prediction_mode == 'workers' else None
        # Only constructed under the application's exclusive data-directory lock.
        while self.store.recover(all_running=True):
            pass

    def _module(self):
        return self.staged_module or importlib.import_module('backend.staged')

    def config(self):
        try:
            module = self._module()
            capabilities = module.staged_capabilities() if hasattr(module, 'staged_capabilities') else []
            error = None
        except (ImportError, ValueError, OSError) as exc:
            capabilities, error = [], str(exc)
        return {'prepWorkers': self.prep_workers, 'gpuWorkers': self.gpu_workers,
                'validationWorkers': self.validation_workers, 'readyCap': self.ready_cap,
                'maxResiduesPerTarget': self.max_residues_per_target,
                'leaseSeconds': self.lease_seconds, 'capabilities': capabilities,
                'diagnostic': error, 'scheduler': 'single-controller SQLite',
                'predictionMode': self.prediction_mode,
                'preparationResources': self.preparation_resources.snapshot(),
                'backpressure': ('online-compatible active ready + in-flight preparing; unroutable inputs wait with cache retained'
                                 if self.prediction_mode == 'workers' else 'active ready + in-flight preparing; total campaign size is not capped'),
                'resultStorage': 'attempt files; metadata and published pointers in SQLite'}

    def start(self):
        if self.threads:
            return
        for kind, number in [('prepare', self.prep_workers), ('predict', self.gpu_workers), ('validate', self.validation_workers)]:
            if kind == 'predict' and self.prediction_mode == 'workers':
                continue
            for index in range(number):
                self.threads.append(threading.Thread(target=self._worker, args=(kind,),
                                                    name=f'pipeline-{kind}-{index}', daemon=True))
        self.threads.append(threading.Thread(target=self._maintain, name='pipeline-leases', daemon=True))
        for thread in self.threads:
            thread.start()

    def close(self):
        self.stop_event.set()
        self.preparation_resources.close()
        self.wakeup.set()
        for thread in self.threads:
            thread.join(timeout=6)
        return not any(thread.is_alive() for thread in self.threads)

    def _maintain(self):
        interval = max(.2, min(5, self.lease_seconds / 3))
        while not self.stop_event.wait(interval):
            with self.active_lock:
                leases = list(self.active.items())
            for (item_id, token), lost in leases:
                try:
                    if not self.store.heartbeat(item_id, token, self.lease_seconds):
                        lost.set()
                except Exception:
                    LOG.exception('Lease renewal failed for %s', item_id)
                    lost.set()
            try:
                if self.store.recover():
                    self.wakeup.set()
            except Exception:
                LOG.exception('Lease recovery failed')

    def _emit(self, claim, *args, **kwargs):
        event = dict(args[0]) if args and isinstance(args[0], dict) else dict(kwargs)
        event_type = event.get('type')
        if event_type in {'preparation_checkpoint', 'preparation_activity'}:
            method = self.store.record_preparation_progress if event_type == 'preparation_checkpoint' else self.store.preparation_activity
            value = event['progress'] if event_type == 'preparation_checkpoint' else event
            if not method(claim['id'], claim['token'], value):
                from .engine import PredictionCancelled
                raise PredictionCancelled('Preparation publication lease was revoked.')
            return
        message = event.get('message', args[1] if len(args) > 1 else '')
        progress = event.get('progress', args[2] if len(args) > 2 else None)
        self.store.emit(claim['id'], claim['token'], message, progress)

    def _attempt_directory(self, claim, kind):
        directory = self.items_root / claim['campaign_id'] / claim['id'] / f"attempt-{claim['attempt']}-{claim['token']}" / kind
        directory.mkdir(parents=True, exist_ok=False)
        return directory

    @staticmethod
    def _check_database_snapshot(job):
        snapshot = job.get('databaseProfile')
        if not snapshot:
            return
        from .databases import get_profile
        current = get_profile(snapshot['id'])
        if not current.get('ready'):
            raise ValueError('Database profile is no longer ready; restore its registered assets before resubmitting.')
        if current.get('profileFingerprint') != snapshot.get('profileFingerprint'):
            raise ValueError('Database profile changed after campaign creation; create a campaign with the new version to avoid stale prepared inputs.')

    def _safe_item_path(self, claim, raw):
        root = (self.items_root / claim['campaign_id'] / claim['id']).resolve()
        path = Path(raw).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('Stage file must be a regular file inside its item directory.')
        return path

    def _validate_result(self, claim, result, cancelled=lambda: False):
        if not isinstance(result, dict):
            raise ValueError('Prediction result must be a JSON object.')
        from .result_artifacts import compact_pae, relocate_pae, publish_pae_urls
        root = (self.items_root / claim['campaign_id'] / claim['id']).resolve()
        if result.get('paeArtifact') and claim.get('candidate_path'):
            relocate_pae(result, Path(claim['candidate_path']).parent, root)
        compact_pae(result, root, cancelled=cancelled)
        # Large PAE is already an external sidecar before this defensive copy.
        result = copy.deepcopy(result)
        base = f"/api/campaigns/{claim['campaign_id']}/items/{claim['id']}"
        registry = {}
        def register(raw):
            candidate = Path(raw)
            if not candidate.is_absolute():
                # Staged contract requires absolute output paths; relative ones
                # are interpreted only against this item's own root.
                candidate = root / candidate
            path = self._safe_item_path(claim, candidate)
            relative = path.relative_to(root).as_posix()
            registry[relative] = {'name': relative, 'size': path.stat().st_size,
                                  'url': base + '/artifact?name=' + quote(relative, safe='')}
            return relative
        structure = result.get('structurePath') or result.get('structure_path') or result.get('structureFile')
        if not structure:
            raise ValueError('Prediction has no structure file.')
        structure_name = register(structure)
        for artifact in result.get('artifacts', []):
            if isinstance(artifact, dict):
                path = artifact.get('path') or artifact.get('file')
                if path:
                    register(path)
            elif isinstance(artifact, str):
                register(artifact)
        for model in result.get('rankedModels', []):
            path = model.get('structurePath') or model.get('structureFile')
            if path:
                relative = register(path)
                model['structureUrl'] = registry[relative]['url']
                model['structureFile'] = relative
        expected = sorted(c['sequence'] for c in claim['job'].get('chains', []))
        actual = sorted(c['sequence'] for c in result.get('chains', []))
        if expected and actual != expected:
            raise ValueError('Validated structure sequences differ from the submitted target.')
        result['structureFile'] = structure_name
        result['structureUrl'] = base + '/structure'
        result['artifacts'] = list(registry.values())
        publish_pae_urls(result, root, base)
        return result

    def _worker(self, kind):
        while not self.stop_event.is_set():
            self.wakeup.clear()
            reservation, claim, delay = None, None, .2
            try:
                if kind == 'prepare':
                    reservation = self.preparation_resources.acquire(self.stop_event.is_set)
                    if reservation is None:
                        return
                if self.stop_event.is_set():
                    return
                # Keep publication and the ready pointer in one maintenance read
                # section. Acquire before claim, so maintenance wait is not a lease.
                access = shared_cache(self.cache_root, self.stop_event.is_set) if kind == 'prepare' else nullcontext()
                with access:
                    claim = self.store.claim(kind, self.ready_cap, self.lease_seconds,
                                             preparation_route=self.preparation_router if kind == 'prepare' and self.prediction_mode == 'workers' else None)
                    if claim is not None:
                        if reservation is not None:
                            self.preparation_resources.bind(reservation, claim['id'], claim['campaign_id'])
                        self._execute_claim(claim, kind)
            except Exception:
                if not self.stop_event.is_set():
                    LOG.exception('Cannot run %s stage', kind)
                delay = .5
            finally:
                if reservation is not None:
                    self.preparation_resources.release(reservation)
            if claim is None:
                # Release empty-queue reservations before sleeping. The existing
                # bounded SQLite poll remains independent of resource admission.
                self.wakeup.wait(delay)

    def _execute_claim(self, claim, kind):
        lost = threading.Event()
        key = (claim['id'], claim['token'])
        with self.active_lock:
            self.active[key] = lost
        def cancelled():
            return self.stop_event.is_set() or lost.is_set() or not self.store.authorized(*key)
        try:
            directory = self._attempt_directory(claim, kind)
            job = copy.deepcopy(claim['job'])
            job.update(campaignId=claim['campaign_id'], targetId=claim['target_id'], cacheRoot=str(self.cache_root),
                       _preparationOrigin=claim['id'])
            if kind in {'prepare', 'predict'}:
                self._check_database_snapshot(job)
            emit = lambda *args, **kwargs: self._emit(claim, *args, **kwargs)
            if kind == 'prepare':
                module = self._module()
                prepare = getattr(module, 'prepare_cooperative', module.prepare_stage)
                prepared = prepare(job, directory, emit, cancelled)
                if cancelled():
                    return
                path = directory / 'prepared.json'
                write_json(path, prepared)
                self.store.advance(*key, 'ready', prepared_path=str(path), cache_hit=bool(prepared.get('cacheHit')),
                                   preparation_reuse=prepared.get('chainReuse'), preparation_cache_key=prepared.get('cacheKey'))
            elif kind == 'predict':
                prepared_path = self._safe_item_path(claim, claim['prepared_path'])
                prepared = json.loads(prepared_path.read_text())
                acquired = False
                try:
                    while not cancelled():
                        if self.prediction_gate.acquire(timeout=.2):
                            acquired = True
                            break
                    if not acquired:
                        return
                    result = self._module().predict_stage(job, prepared, directory, emit, cancelled)
                finally:
                    if acquired:
                        self.prediction_gate.release()
                if cancelled():
                    return
                candidate = directory / 'candidate.json'
                write_json(candidate, result)
                self.store.advance(*key, 'validating', candidate_path=str(candidate))
            else:
                candidate = self._safe_item_path(claim, claim['candidate_path'])
                result = self._validate_result(claim, json.loads(candidate.read_text()), cancelled=cancelled)
                if cancelled():
                    return
                path = directory / 'result.json'
                write_json(path, result)
                item_root = self.items_root/claim['campaign_id']/claim['id']
                publication = {'schema': 1, 'result': publication_file(item_root, path),
                               'structure': publication_file(item_root, item_root/result['structureFile'])}
                self.store.advance(*key, 'completed', result_path=str(path), result_publication=publication)
        except PreparationDeferred as exc:
            if kind == 'prepare' and not cancelled():
                self.store.defer_preparation(*key, exc.progress, retry_delay(claim['id'], claim.get('prepare_defer_count', 0)),
                                             chain_key=exc.chain_key, chain_ids=exc.chain_ids, wait_reason=exc.wait_reason)
        except Exception as exc:
            if not cancelled():
                LOG.exception('Campaign stage failed: %s %s', claim['id'], kind)
                self.store.advance(*key, 'failed', error=str(exc)[:2000])
        finally:
            with self.active_lock:
                self.active.pop(key, None)
            self.wakeup.set()

    @staticmethod
    def _read_compact_result(path):
        limit = 4*1024*1024
        if path.stat().st_size > limit:
            return None
        with path.open('rb') as stream:
            content = stream.read(limit+1)
        if len(content) > limit:
            return None
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError('Published result must be a JSON object.')
        return result

    def detail(self, campaign_id, item_id):
        item = self.store.item(campaign_id, item_id)
        if item is None:
            raise KeyError('Campaign item not found.')
        item['name'] = item['input'].get('name') or item['targetId']
        item['engine'] = item['input'].get('engine')
        item['chains'] = item['input'].get('chains', [])
        item['result'] = None
        if item['resultPath']:
            path = self._safe_item_path({'campaign_id': campaign_id, 'id': item_id}, item['resultPath'])
            relative = path.relative_to(self.items_root/campaign_id/item_id).as_posix()
            base = f'/api/campaigns/{campaign_id}/items/{item_id}'
            item['resultJsonUrl'] = base+'/artifact?name='+quote(relative, safe='')
            item['result'] = self._read_compact_result(path)
            if item['result'] is None:
                item['resultAvailability'] = {'status': 'legacy-large-result', 'bytes': path.stat().st_size,
                    'message': 'This existing result exceeds the compact detail limit. Download its original JSON; no automatic rewrite was performed.',
                    'downloadUrl': item['resultJsonUrl']}
        item.pop('resultPath', None)
        return item

    def artifact(self, campaign_id, item_id, name):
        # Downloads never construct the full detail response. The ledger's
        # publication pointer directly authorizes its own original JSON file.
        row = self.store.item(campaign_id, item_id, internal=True)
        if row is None or not row['result_path']:
            raise KeyError('Campaign result not found.')
        claim = {'campaign_id': campaign_id, 'id': item_id}
        root = self.items_root/campaign_id/item_id
        path = self._safe_item_path(claim, row['result_path'])
        if name == path.relative_to(root).as_posix():
            return path
        if name == '__structure__' and row.get('result_publication') is not None:
            raw = row['result_publication']
            if not isinstance(raw, str) or len(raw.encode()) > PUBLICATION_LIMIT:
                raise ValueError('Invalid compact result publication.')
            publication = checked_publication(json.loads(raw), row, root, path)
            return self._safe_item_path(claim, root/publication['structure']['relativePath'])
        result = self._read_compact_result(path)
        if result is None:
            raise ValueError('Legacy result is too large for artifact registry lookup; download the original result JSON.')
        if name == '__structure__':
            name = result.get('structureFile')
        names = {artifact['name'] for artifact in result.get('artifacts', []) if isinstance(artifact, dict) and 'name' in artifact}
        if not name or name not in names:
            raise KeyError('Campaign artifact not found.')
        return self._safe_item_path(claim, root/name)
