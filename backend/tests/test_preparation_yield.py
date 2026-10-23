"""Cooperative AF3 preparation using real synthetic CPU subprocesses, no GPU.

Locks, durable ledger, maintenance scan and subprocess-crash recovery are real;
MSA search output is the controlled test_chain_reuse fixture, not native AF3.
"""
from contextlib import contextmanager
import copy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from backend import staged
from backend.cache_access import shared_cache
from backend.cache_lifecycle import CacheLifecycle
from backend.preparation_control import PreparationDeferred
from backend.worker_pipeline import PipelineScheduler
from backend.tests.test_chain_reuse import RUNNER
from backend.tests.test_preparation_resources import wait_for


class PreparationYieldTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.counter = self.root/'searches.txt'
        script = self.root/'prepare.py'
        script.write_text(RUNNER)
        self.environment = patch.dict(os.environ, {
            'PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND': json.dumps([sys.executable, str(script), '{input_json}', '{output_dir}']),
            'PROTEIN_STUDIO_PREDICTION_MODE': 'workers', 'CHAIN_COUNTER': str(self.counter),
            'CHAIN_DELAY': '0', 'CHAIN_BAD': '', 'CHAIN_FAIL_SEQ': '', 'CHAIN_SLEEP_SEQ': '',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.scheduler = PipelineScheduler(self.root, prep_workers=1,
            preparation_resources={'cpuBudget': 32, 'cpuPerPrepare': 32})
        self.addCleanup(self.scheduler.close)
        self.scheduler.preparation_router = lambda conn, now: ('1', [])
        self.snapshot_patch = patch.object(self.scheduler, '_check_database_snapshot', lambda job: None)
        self.snapshot_patch.start()
        self.addCleanup(self.snapshot_patch.stop)
        self.profile = dict(id='synthetic', engine='alphafold3', ready=True,
            root='/never-a-real-database', profileFingerprint='fixture-database-v1',
            prepareVersion='synthetic-v1', searchVersion='fixture-v1', searchParameters={'threads': 1})
        self.campaign = self.scheduler.store.create(dict(name='Synthetic MSA', engine='alphafold3',
            status='active', settings={}, databaseProfile=self.profile, executionProfile={}))

    def job(self, *sequences):
        return dict(name='target', engine='alphafold3', chains=[dict(id=chr(65+i), sequence=s) for i,s in enumerate(sequences)],
                    seeds=[1], numSamples=1, numRecycles=10, databaseProfile=copy.deepcopy(self.profile),
                    cacheRoot=str(self.root/'cache'))

    def append(self, target, *sequences):
        return self.scheduler.store.append(self.campaign['id'], [dict(targetId=target, submission=self.job(*sequences))])['items'][0]

    def row(self, item):
        return self.scheduler.store.item(self.campaign['id'], item['id'], internal=True)

    def execute(self, claim):
        with shared_cache(self.scheduler.cache_root):
            self.scheduler._execute_claim(claim, 'prepare')

    def searches(self):
        return self.counter.read_text().splitlines() if self.counter.exists() else []

    @contextmanager
    def hold(self, key, leaf=True):
        root = self.root/'cache'/staged.CHAIN_NAMESPACE if leaf else self.root/'cache'
        root.mkdir(parents=True, exist_ok=True)
        with (root/(key+'.lock')).open('a+') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def leaf_key(self, sequence):
        return staged._chain_identity(staged._normalize(self.job(sequence), require_prediction=False), sequence)[1]

    def test_busy_fixed_chain_searches_other_chain_then_defers_and_retains_gc_pins(self):
        item = self.append('ab', 'AG', 'VC')
        claim = self.scheduler.store.claim('prepare')
        with self.hold(self.leaf_key('AG')):
            self.execute(claim)
        row = self.row(item)
        self.assertEqual(row['stage'], 'waiting_input')
        self.assertIsNone(row['token']); self.assertIsNone(row['error'])
        self.assertEqual(self.searches(), ['VC'])
        progress = json.loads(row['prepare_progress'])
        self.assertEqual(len(progress['completedLeaves']), 1)
        self.assertFalse(progress['completedLeaves'][0]['cacheHit'])
        self.assertFalse((self.root/'cache'/staged.prepare_cache_key(self.job('AG', 'VC'))).exists())
        self.assertFalse(list((self.root/'cache').glob('*.partial-*')))
        manager = CacheLifecycle(self.root)
        scan = manager.scan()
        self.assertFalse(scan['failClosed'])
        leaf = next(x for x in manager.entries(scan['id'])['items'] if x['kind'] == 'leaf')
        self.assertTrue(leaf['protected'])
        self.assertEqual(manager.plan(min_age_seconds=0)['count'], 0)
        self.assertFalse(self.scheduler.store.record_preparation_progress(item['id'], claim['token'], progress))
        self.assertFalse(self.scheduler.store.preparation_activity(item['id'], claim['token'], {'state': 'searching'}))
        self.assertFalse(self.scheduler.store.advance(item['id'], claim['token'], 'failed', error='late worker'))
        self.assertFalse(self.scheduler.store.defer_preparation(item['id'], claim['token'], progress, 1))
        self.assertEqual(self.scheduler.store.get(self.campaign['id'])['preparationStateCounts']['waitingShared'], 1)

    def test_deferral_releases_real_reservation_and_later_target_runs_before_busy_key(self):
        waiting = self.append('wait', 'AG', 'VC')
        following = self.append('independent', 'FT')
        with self.hold(self.leaf_key('AG')):
            self.scheduler.start()
            wait_for(lambda: self.row(following)['stage'] == 'ready')
            row = self.row(waiting)
            self.assertEqual(row['stage'], 'waiting_input')
            self.assertEqual(self.searches(), ['VC', 'FT'])
            self.assertLessEqual(self.scheduler.preparation_resources.snapshot()['inflight']['count'], 1)
            self.scheduler.store.control(self.campaign['id'], 'pause')
            wait_for(lambda: self.scheduler.preparation_resources.snapshot()['inflight']['count'] == 0)
        self.assertIsNone(self.scheduler.store.claim('prepare', now=time.time()+60))
        self.assertTrue(self.scheduler.close())

    def test_restart_reuses_leaf_preserving_original_accounting_and_qc(self):
        item = self.append('restart', 'AG', 'VC')
        with self.hold(self.leaf_key('AG')):
            self.execute(self.scheduler.store.claim('prepare'))
        self.scheduler.close()
        restarted = PipelineScheduler(self.root, preparation_resources={})
        try:
            with patch.object(restarted, '_check_database_snapshot', lambda job: None):
                claim = restarted.store.claim('prepare', now=time.time()+60)
                with shared_cache(restarted.cache_root):
                    restarted._execute_claim(claim, 'prepare')
            detail = restarted.store.item(self.campaign['id'], item['id'])
            self.assertEqual(detail['stage'], 'ready')
            self.assertEqual(detail['preparationReuse']['newChainSearches'], 2)
            self.assertEqual(detail['preparationReuse']['chainCacheHits'], 0)
            self.assertCountEqual(self.searches(), ['AG', 'VC'])
            self.assertEqual(detail['preparationProgress']['state'], 'prepared')
            self.assertIsNone(restarted.store.item(self.campaign['id'], item['id'], internal=True)['prepare_progress'])
            self.assertTrue(all(x['msaQuality']['paired']['finalPairedRows'] is None for x in detail['msaQuality']))
            self.assertTrue(all(x['searchProcessWallSeconds'] > 0 for x in detail['msaQuality']))
        finally:
            restarted.close()

    def test_failure_checkpoint_survives_explicit_retry_and_gc(self):
        item = self.append('fail', 'AG', 'VC')
        with patch.dict(os.environ, {'CHAIN_FAIL_SEQ': 'VC'}), self.assertLogs('protein-studio.pipeline', level='ERROR'):
            self.execute(self.scheduler.store.claim('prepare'))
        self.assertEqual(self.row(item)['stage'], 'failed')
        progress = json.loads(self.row(item)['prepare_progress'])
        self.assertEqual(len(progress['completedLeaves']), 1)
        manager = CacheLifecycle(self.root)
        scan = manager.scan()
        self.assertTrue(next(x for x in manager.entries(scan['id'])['items'] if x['kind'] == 'leaf')['protected'])
        self.scheduler.store.retry_failed(self.campaign['id'])
        self.execute(self.scheduler.store.claim('prepare'))
        detail = self.scheduler.store.item(self.campaign['id'], item['id'])
        self.assertEqual(detail['stage'], 'ready')
        self.assertEqual(detail['preparationReuse']['newChainSearches'], 2)
        self.assertEqual(self.searches(), ['AG', 'VC', 'VC'])

    def test_whole_busy_yields_without_search_and_preserves_whole_intent(self):
        job = self.job('AG', 'VC')
        key = staged.prepare_cache_key(job)
        with self.hold(key, leaf=False):
            started = time.monotonic()
            with self.assertRaises(PreparationDeferred) as caught:
                staged.prepare_cooperative(job, self.root/'direct', lambda *a: None, lambda: False)
        self.assertLess(time.monotonic()-started, 1)
        self.assertEqual(caught.exception.progress['wholeCacheKey'], key)
        self.assertEqual(self.searches(), [])
        item = self.append('whole', 'AG', 'VC')
        claim = self.scheduler.store.claim('prepare')
        self.scheduler.store.defer_preparation(item['id'], claim['token'], caught.exception.progress, 1)
        staged.prepare_stage(job, self.root/'other-producer', lambda *a: None, lambda: False)
        manager = CacheLifecycle(self.root)
        scan = manager.scan()
        whole = next(x for x in manager.entries(scan['id'])['items'] if x['kind'] == 'whole')
        self.assertTrue(whole['protected'])

    def test_crash_after_leaf_rename_is_pinned_and_producer_identity_restores_count(self):
        item = self.append('hard-crash', 'AG', 'VC')
        code = r'''
import os,sys
from pathlib import Path
from backend.worker_pipeline import PipelineScheduler
from backend.cache_access import shared_cache
from backend import staged
scheduler=PipelineScheduler(sys.argv[1],preparation_resources={})
scheduler._check_database_snapshot=lambda job:None
original=staged.os.replace
def crash_after_publish(source,destination):
    original(source,destination)
    if Path(destination).parent.name==staged.CHAIN_NAMESPACE:
        os._exit(17)
staged.os.replace=crash_after_publish
claim=scheduler.store.claim('prepare')
with shared_cache(scheduler.cache_root):scheduler._execute_claim(claim,'prepare')
'''
        child = subprocess.run([sys.executable, '-c', code, str(self.root)], capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 17, child.stderr)
        self.assertEqual(self.searches(), ['AG'])
        progress = json.loads(self.row(item)['prepare_progress'])
        self.assertEqual(len(progress['pendingLeaves']), 2)
        self.assertEqual(progress['completedLeaves'], [])
        manager = CacheLifecycle(self.root)
        scan = manager.scan()
        leaf = next(x for x in manager.entries(scan['id'])['items'] if x['kind'] == 'leaf')
        self.assertTrue(leaf['protected'])
        self.scheduler.store.recover(all_running=True)
        self.execute(self.scheduler.store.claim('prepare'))
        detail = self.scheduler.store.item(self.campaign['id'], item['id'])
        self.assertEqual(detail['preparationReuse']['newChainSearches'], 2)
        self.assertEqual(detail['preparationReuse']['chainCacheHits'], 0)
        self.assertEqual(self.searches(), ['AG', 'VC'])

    def test_legacy_leaf_does_not_invent_qc_and_new_source_hash_is_exact(self):
        first = staged.prepare_stage(self.job('AG'), self.root/'first', lambda *a: None, lambda: False)
        entry = first['chainReuse']['entries'][0]
        leaf = self.root/'cache'/staged.CHAIN_NAMESPACE/entry['cacheKey']
        marker = leaf/'COMPLETE.json'
        data = json.loads(marker.read_text())
        source = json.loads((leaf/data['inputRelativePath']).read_text())['sequences'][0]['protein']
        import hashlib
        self.assertEqual(entry['msaQuality']['paired']['sourceSha256'], hashlib.sha256(source['pairedMsa'].encode()).hexdigest())
        data.pop('msaQuality'); data.pop('searchProcessWallSeconds')
        marker.write_text(json.dumps(data))
        other = self.job('AG'); other['chains'][0]['id'] = 'Z'
        second = staged.prepare_cooperative(other, self.root/'second', lambda *a: None, lambda: False)
        self.assertNotIn('msaQuality', second['chainReuse']['entries'][0])
        self.assertNotIn('searchProcessWallSeconds', second['chainReuse']['entries'][0])
        self.assertEqual(self.searches(), ['AG'])

    def test_due_filter_pause_cancel_and_corrupt_progress_fail_closed(self):
        item = self.append('old', 'AG')
        claim = self.scheduler.store.claim('prepare')
        progress = {'schema': 1, 'wholeCacheKey': staged.prepare_cache_key(self.job('AG')), 'completedLeaves': [], 'pendingLeaves': []}
        self.scheduler.store.defer_preparation(item['id'], claim['token'], progress, 10)
        following = self.append('new', 'VC')
        next_claim = self.scheduler.store.claim('prepare')
        self.assertEqual(next_claim['id'], following['id'])
        self.scheduler.store.control(self.campaign['id'], 'cancel')
        self.assertIsNone(self.scheduler.store.claim('prepare', now=time.time()+100))
        self.assertFalse(self.scheduler.store.defer_preparation(next_claim['id'], next_claim['token'], progress, 1))
        self.assertEqual(self.scheduler.store.get(self.campaign['id'])['preparationStateCounts']['waitingShared'], 0)
        self.assertNotIn('preparationProgress', self.scheduler.store.item(self.campaign['id'], item['id']))
        # An unreadable checkpoint is a conservative GC pin, not permission to delete.
        with self.scheduler.store.connection(True) as conn:
            conn.execute("UPDATE campaigns SET status='active' WHERE id=?", (self.campaign['id'],))
            conn.execute("UPDATE campaign_items SET prepare_progress='broken' WHERE id=?", (item['id'],))
        manager = CacheLifecycle(self.root)
        self.assertTrue(manager.scan()['failClosed'])
