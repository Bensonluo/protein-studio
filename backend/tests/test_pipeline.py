"""Persistent pipeline tests; no real GPU, remote service, or model download."""
import json
import os
import resource
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.campaigns import CampaignStore
from backend.store import ConflictError
from backend.worker_pipeline import PipelineScheduler
from backend.tests import test_api as api_tests

PDB = api_tests.PDB


def submission(sequence='A', name='target'):
    return {'name': name, 'engine': 'reference', 'chains': [{'id': 'A', 'sequence': sequence}],
            'seeds': [1], 'numSamples': 1}


def spec(status='active'):
    return {'name': 'Pipeline test', 'engine': 'reference', 'status': status,
            'settings': {}, 'databaseProfile': {}, 'executionProfile': {}}


def wait_for(predicate, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError('Pipeline condition was not reached before timeout.')


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CampaignStore(Path(self.temp.name) / 'campaigns.sqlite3')
        self.campaign = self.store.create(spec())
        self.cid = self.campaign['id']

    def tearDown(self):
        self.temp.cleanup()

    def append(self, *names):
        return self.store.append(self.cid, [{'targetId': name, 'submission': submission()} for name in names])

    def test_append_is_idempotent_and_conflict_rolls_back_entire_chunk(self):
        first = self.append('one')['items'][0]
        again = self.append('one')
        self.assertEqual(again['duplicates'], 1)
        self.assertEqual(again['items'][0]['id'], first['id'])
        with self.assertRaises(ConflictError):
            self.store.append(self.cid, [{'targetId': 'two', 'submission': submission()},
                {'targetId': 'one', 'submission': submission('C')}])
        self.assertEqual(self.store.get(self.cid)['totalItems'], 1)
        self.store.control(self.cid, 'seal')
        self.assertEqual(self.append('one')['duplicates'], 1)
        with self.assertRaises(ConflictError):
            self.append('two')

    def test_ready_high_water_includes_inflight_preparation_not_manifest(self):
        self.append(*[str(i) for i in range(20)])
        self.assertIsNone(self.store.claim('predict'))
        first = self.store.claim('prepare', ready_cap=2)
        second = self.store.claim('prepare', ready_cap=2)
        self.assertIsNone(self.store.claim('prepare', ready_cap=2))
        self.assertTrue(self.store.advance(first['id'], first['token'], 'ready', prepared_path='/placeholder'))
        self.assertIsNone(self.store.claim('prepare', ready_cap=2))
        prediction = self.store.claim('predict')
        self.assertEqual(prediction['id'], first['id'])
        self.assertIsNotNone(self.store.claim('prepare', ready_cap=2))
        self.assertEqual(self.store.get(self.cid)['totalItems'], 20)
        self.assertEqual(second['stage'], 'preparing')

    def test_expired_fence_cannot_publish_and_recovery_uses_safe_stage(self):
        self.append('one')
        old = self.store.claim('prepare', lease_seconds=1, now=100)
        self.assertEqual(self.store.recover(now=102), 1)
        current = self.store.claim('prepare', lease_seconds=10, now=102)
        self.assertNotEqual(old['token'], current['token'])
        self.assertFalse(self.store.advance(old['id'], old['token'], 'ready', prepared_path='stale', now=102))
        self.assertTrue(self.store.advance(current['id'], current['token'], 'ready', prepared_path='good', now=103))
        prediction = self.store.claim('predict', now=104)
        self.assertEqual(self.store.recover(all_running=True), 1)
        recovered = self.store.item(self.cid, old['id'], internal=True)
        self.assertEqual(recovered['stage'], 'ready')
        self.assertEqual(recovered['prepared_path'], 'good')
        self.assertFalse(self.store.advance(prediction['id'], prediction['token'], 'validating', candidate_path='late', now=105))
        replacement = self.store.claim('predict', now=106)
        self.store.advance(replacement['id'], replacement['token'], 'validating', candidate_path='candidate', now=107)
        validation = self.store.claim('validate', now=108)
        self.store.recover(all_running=True)
        self.assertEqual(self.store.item(self.cid, old['id'])['stage'], 'validating')
        self.assertFalse(self.store.advance(validation['id'], validation['token'], 'completed', result_path='late', now=109))

    def test_pause_stops_claims_cancel_revokes_active_attempts_immediately(self):
        ids = self.append('one', 'two')['items']
        first = self.store.claim('prepare')
        self.store.control(self.cid, 'pause')
        self.assertIsNone(self.store.claim('prepare'))
        self.assertTrue(self.store.authorized(first['id'], first['token']))
        self.store.control(self.cid, 'cancel')
        self.assertFalse(self.store.authorized(first['id'], first['token']))
        self.assertFalse(self.store.advance(first['id'], first['token'], 'ready'))
        self.assertEqual(self.store.get(self.cid)['counts']['cancelled'], 2)
        self.assertEqual(len(self.store.items(self.cid, stage='cancelled')['items']), 2)
        self.assertEqual(self.store.items(self.cid, stage='waiting_input')['total'], 0)
        self.assertEqual(self.store.recover(), 2)
        with self.assertRaises(ConflictError):
            self.store.control(self.cid, 'resume')
        self.assertEqual(self.store.item(self.cid, ids[1]['id'])['stage'], 'cancelled')

    def test_lists_never_load_result_files_or_full_inputs(self):
        self.append('one')
        listing = self.store.items(self.cid)['items'][0]
        self.assertEqual(listing['name'], 'target')
        self.assertNotIn('input', listing)
        self.assertNotIn('result', listing)
        self.assertNotIn('pae', listing)
        self.assertEqual(self.store.items(self.cid, limit=1)['nextOffset'], None)

    def test_retry_only_failed_items_preserves_manifest_completed_and_fencing(self):
        original = self.append('prepare-failure', 'predict-failure', 'done')['items']
        preparation = self.store.claim('prepare')
        self.store.advance(preparation['id'], preparation['token'], 'failed', error='CPU temporary error')
        prediction_input = self.store.claim('prepare')
        prepared = Path(self.temp.name) / 'campaigns' / self.cid / prediction_input['id'] / 'prepared.json'
        prepared.parent.mkdir(parents=True)
        prepared.write_text('{"cacheHit":false}')
        self.store.advance(prediction_input['id'], prediction_input['token'], 'ready', prepared_path=str(prepared))
        prediction = self.store.claim('predict')
        self.store.advance(prediction['id'], prediction['token'], 'failed', error='GPU temporary error',
                           candidate_path='old-candidate', result_path='old-result')
        done = self.store.claim('prepare')
        self.store.advance(done['id'], done['token'], 'ready', prepared_path='unused')
        done = self.store.claim('predict')
        self.store.advance(done['id'], done['token'], 'validating', candidate_path='done-candidate')
        done = self.store.claim('validate')
        self.store.advance(done['id'], done['token'], 'completed', result_path='published-result')
        self.store.control(self.cid, 'seal')
        response = self.store.retry_failed(self.cid)
        self.assertEqual(response['retried'], 2)
        self.assertTrue(response['campaign']['sealed'])
        self.assertEqual(response['campaign']['counts']['completed'], 1)
        self.assertEqual(response['campaign']['counts']['waiting_input'], 1)
        self.assertEqual(response['campaign']['counts']['ready'], 1)
        self.assertEqual([(x['id'], x['targetId']) for x in self.store.items(self.cid)['items']],
                         [(x['id'], x['targetId']) for x in original])
        retried = self.store.item(self.cid, prediction['id'], internal=True)
        self.assertIsNone(retried['error'])
        self.assertIsNone(retried['candidate_path'])
        self.assertIsNone(retried['result_path'])
        self.assertFalse(self.store.advance(prediction['id'], prediction['token'], 'validating', candidate_path='stale'))
        replacement = self.store.claim('predict')
        self.assertNotEqual(replacement['token'], prediction['token'])
        self.assertFalse(self.store.advance(prediction['id'], prediction['token'], 'validating', candidate_path='stale'))
        self.assertEqual(self.store.retry_failed(self.cid)['retried'], 0)
        self.assertEqual(self.store.item(self.cid, done['id'], internal=True)['result_path'], 'published-result')

    def test_retry_missing_prepared_descriptor_restarts_preparation_and_cancel_rejects(self):
        self.append('one')
        item = self.store.claim('prepare')
        self.store.advance(item['id'], item['token'], 'ready', prepared_path='/missing/prepared.json')
        prediction = self.store.claim('predict')
        self.store.advance(prediction['id'], prediction['token'], 'failed', error='missing files')
        self.assertEqual(self.store.retry_failed(self.cid)['campaign']['counts']['waiting_input'], 1)
        self.assertFalse(self.store.authorized(prediction['id'], prediction['token']))
        self.store.control(self.cid, 'cancel')
        with self.assertRaises(ConflictError):
            self.store.retry_failed(self.cid)

    def test_profiles_stored_once_and_injected_at_claim_with_legacy_hash_compatibility(self):
        configuration = spec()
        configuration['databaseProfile'] = {'id': 'test-profile', 'assets': [{'name': 'UNIQUE_LARGE_ASSET_' + str(i)} for i in range(40)]}
        configuration['executionProfile'] = {'id': 'executor-version'}
        cid = self.store.create(configuration)['id']
        job = submission()
        job.update(databaseProfile=configuration['databaseProfile'], executionProfile=configuration['executionProfile'])
        first = self.store.append(cid, [{'targetId': 'one', 'submission': job}])['items'][0]
        with self.store.connection(True) as connection:
            value = connection.execute('SELECT input_json FROM campaign_items WHERE id=?', (first['id'],)).fetchone()[0]
            self.assertNotIn('UNIQUE_LARGE_ASSET', value)
            self.assertNotIn('databaseProfile', value)
            campaign_spec = connection.execute('SELECT specification FROM campaigns WHERE id=?', (cid,)).fetchone()[0]
            self.assertIn('UNIQUE_LARGE_ASSET', campaign_spec)
            # Simulate a workspace written by the earlier expanded-row version.
            connection.execute('UPDATE campaign_items SET input_json=?,input_hash=? WHERE id=?', (json.dumps(job), 'old-expanded-hash', first['id']))
        retransmission = self.store.append(cid, [{'targetId': 'one', 'submission': submission()}])
        self.assertEqual(retransmission['duplicates'], 1)
        claimed = self.store.claim('prepare')
        self.assertEqual(claimed['job']['databaseProfile'], configuration['databaseProfile'])
        self.assertEqual(claimed['job']['executionProfile'], configuration['executionProfile'])
        self.assertEqual(self.store.item(cid, first['id'])['input']['databaseProfile'], configuration['databaseProfile'])


class ControlledStages:
    def __init__(self):
        self.prediction_started = threading.Event()
        self.release_prediction = threading.Event()
        self.prepared = 0
        self.active_predict = 0
        self.maximum_predict = 0
        self.lock = threading.Lock()

    def staged_capabilities(self):
        return [{'engine': 'reference', 'staged': True, 'available': True}]

    def prepare_stage(self, job, work_dir, emit, cancelled):
        with self.lock:
            self.prepared += 1
        emit('prepare', 'Prepared fixture', 90)
        return {'cacheHit': False, 'targetId': job['targetId']}

    def predict_stage(self, job, prepared, work_dir, emit, cancelled):
        with self.lock:
            self.active_predict += 1
            self.maximum_predict = max(self.maximum_predict, self.active_predict)
        self.prediction_started.set()
        try:
            while not self.release_prediction.wait(.01):
                if cancelled():
                    raise RuntimeError('Cancelled')
            path = work_dir / 'model.pdb'
            path.write_text(PDB)
            return {'structurePath': str(path), 'chains': job['chains'], 'residues': [],
                    'artifacts': [{'name': 'model.pdb', 'path': str(path)}]}
        finally:
            with self.lock:
                self.active_predict -= 1


class SchedulerTests(unittest.TestCase):
    def test_cpu_pipeline_runs_ahead_without_taking_gpu_gate_and_stays_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            stages = ControlledStages()
            scheduler = PipelineScheduler(directory, stages, prep_workers=3, gpu_workers=1, ready_cap=2, lease_seconds=1)
            try:
                cid = scheduler.store.create(spec())['id']
                scheduler.store.append(cid, [{'targetId': str(i), 'submission': submission()} for i in range(8)])
                scheduler.store.control(cid, 'seal')
                scheduler.start()
                self.assertTrue(stages.prediction_started.wait(2))
                wait_for(lambda: scheduler.store.get(cid)['counts']['ready'] == 2)
                counts = scheduler.store.get(cid)['counts']
                self.assertEqual(counts['predicting'], 1)
                self.assertEqual(counts['waiting_input'], 5)
                # Long-running prediction survives lease duration via heartbeats.
                time.sleep(1.2)
                self.assertEqual(scheduler.store.get(cid)['counts']['predicting'], 1)
                self.assertEqual(stages.prepared, 3)
                stages.release_prediction.set()
                wait_for(lambda: scheduler.store.get(cid)['isComplete'])
                self.assertEqual(scheduler.store.get(cid)['counts']['completed'], 8)
                self.assertEqual(stages.maximum_predict, 1)
                item = scheduler.store.items(cid)['items'][0]
                result = scheduler.detail(cid, item['id'])['result']
                self.assertTrue(result['structureUrl'].startswith('/api/campaigns/'))
                self.assertEqual(scheduler.artifact(cid, item['id'], '__structure__').read_text(), PDB)
            finally:
                stages.release_prediction.set()
                scheduler.close()

    def test_reference_campaign_actual_cache_reuse_and_restart_persistence(self):
        from backend import engine
        from backend.databases import get_profile
        with tempfile.TemporaryDirectory() as directory:
            scheduler = PipelineScheduler(directory, prep_workers=2, gpu_workers=2, ready_cap=3)
            example = engine.load_example('ubiquitin-afdb')
            job = engine.validate_submission({'engine': 'reference', 'example': 'ubiquitin-afdb',
                'chains': example['chains'], 'seeds': [1], 'numSamples': 1})
            job['databaseProfile'] = get_profile('reference')
            configuration = spec()
            configuration['databaseProfile'] = job['databaseProfile']
            cid = scheduler.store.create(configuration)['id']
            scheduler.store.append(cid, [{'targetId': str(i), 'submission': job} for i in range(3)])
            scheduler.store.control(cid, 'seal')
            scheduler.start()
            try:
                campaign = wait_for(lambda: (c if c['isComplete'] else None) if (c := scheduler.store.get(cid)) else None, 15)
                self.assertEqual(campaign['counts']['completed'], 3, scheduler.store.items(cid))
                self.assertEqual(campaign['metrics']['cacheHits'], 2)
                items = scheduler.store.items(cid)['items']
                item = scheduler.detail(cid, items[0]['id'])
                self.assertGreater(len(item['result']['residues']), 200)
                from backend.result_artifacts import read_pae
                self.assertIsNone(item['result']['pae'])
                descriptor = item['result']['paeArtifact']
                self.assertEqual((descriptor['rows'], descriptor['cols']), (229, 229))
                item_root = scheduler.items_root/cid/items[0]['id']
                tile = read_pae(descriptor, item_root, row_start=3, col_start=5, rows=2, cols=3)
                original_confidence = json.loads((item_root/descriptor['sourceRelativePath']).read_text())
                matrix = original_confidence['pae']
                self.assertEqual(tile['values'], [row[5:8] for row in matrix[3:5]])
                self.assertFalse(item['result']['provenance']['newPrediction'])
                self.assertEqual(set(event['stage'] for event in item['events']),
                    {'waiting_input', 'preparing', 'ready', 'predicting', 'validating', 'completed'})
                with scheduler.store.connection() as connection:
                    row = connection.execute('SELECT input_json,result_path FROM campaign_items LIMIT 1').fetchone()
                    self.assertNotIn('pae', row['input_json'])
                    self.assertTrue(Path(row['result_path']).is_file())
            finally:
                scheduler.close()
            restarted = PipelineScheduler(directory)
            try:
                self.assertEqual(restarted.store.get(cid)['counts']['completed'], 3)
                restored = restarted.detail(cid, items[0]['id'])['result']
                self.assertEqual(restored['structureUrl'], item['result']['structureUrl'])
                self.assertEqual(read_pae(restored['paeArtifact'], restarted.items_root/cid/items[0]['id'],
                                          row_start=3, col_start=5, rows=2, cols=3)['values'], tile['values'])
            finally:
                restarted.close()

    def test_database_changed_before_execution_fails_without_using_old_cache(self):
        from backend.databases import get_profile
        with tempfile.TemporaryDirectory() as directory:
            scheduler = PipelineScheduler(directory, ControlledStages())
            job = submission()
            job['databaseProfile'] = get_profile('reference')
            job['databaseProfile']['profileFingerprint'] = 'stale'
            configuration = spec()
            configuration['databaseProfile'] = job['databaseProfile']
            cid = scheduler.store.create(configuration)['id']
            scheduler.store.append(cid, [{'targetId': 'one', 'submission': job}])
            scheduler.start()
            try:
                wait_for(lambda: scheduler.store.get(cid)['counts']['failed'] == 1)
                self.assertIn('changed after campaign', scheduler.store.items(cid)['items'][0]['error'])
            finally:
                scheduler.close()


class CampaignHTTPTests(unittest.TestCase):
    setUp = api_tests.APITests.setUp
    tearDown = api_tests.APITests.tearDown
    request = api_tests.APITests.request
    def test_campaign_http_atomic_import_pause_resume_detail_download(self):
        from backend import engine
        self.app.engine = engine
        example = engine.load_example('crambin')
        status, campaign = self.request('/api/campaigns', 'POST', {'name': 'API campaign', 'engine': 'reference',
            'settings': {'example': 'crambin', 'seeds': [1], 'numSamples': 1}, 'databaseProfile': 'reference', 'status': 'paused'})
        self.assertEqual(status, 201, campaign)
        base = '/api/campaigns/' + campaign['id']
        payload = {'items': [{'targetId': 'first', 'chains': example['chains']},
                              {'targetId': 'second', 'chains': example['chains']}]}
        status, imported = self.request(base + '/items', 'POST', payload)
        self.assertEqual(status, 201, imported)
        self.assertEqual(imported['inserted'], 2)
        self.assertEqual(self.request(base + '/items', 'POST', payload)[1]['duplicates'], 2)
        self.assertEqual(self.request(base)[1]['counts']['waiting_input'], 2)
        bad = {'items': [{'targetId': 'third', 'chains': example['chains']}, {'targetId': 'invalid', 'chains': []}]}
        self.assertEqual(self.request(base + '/items', 'POST', bad)[0], 400)
        self.assertEqual(self.request(base)[1]['totalItems'], 2)
        self.assertEqual(self.request(base + '/seal', 'POST', {})[0], 200)
        self.assertEqual(self.request(base + '/resume', 'POST', {})[0], 200)
        wait_for(lambda: self.request(base)[1]['isComplete'])
        status, item = self.request(base + '/items/' + imported['items'][0]['id'])
        self.assertEqual(status, 200, item)
        self.assertEqual(item['stage'], 'completed', item.get('error'))
        self.assertEqual(item['name'], 'first')
        self.assertEqual(item['chains'], example['chains'])
        self.assertEqual(self.request(item['result']['structureUrl'])[0], 200)
        self.assertEqual(self.request(item['result']['artifacts'][0]['url'])[0], 200)
        self.assertEqual(self.request(base + '/items?limit=1&stage=completed')[1]['nextOffset'], 1)
        self.assertEqual(self.request(base + '/items?stage=bad')[0], 400)
        self.assertEqual(self.request(base + '/items/' + item['id'] + '/artifact?name=..%2Fcampaigns.sqlite3')[0], 404)
        self.assertIsInstance(self.request('/api/databases')[1], list)
        self.assertIsInstance(self.request('/api/campaigns')[1], list)
        self.assertIn('readyCap', self.request('/api/pipeline/config')[1])
        status, retried = self.request(base + '/retry-failed', 'POST', {})
        self.assertEqual(status, 200, retried)
        self.assertEqual(retried['retried'], 0)
        self.assertEqual(retried['campaign']['counts']['completed'], 2)
        self.request(base + '/cancel', 'POST', {})
        self.assertEqual(self.request(base + '/retry-failed', 'POST', {})[0], 409)

    def test_not_ready_database_returns_readable_400(self):
        with patch('backend.databases.get_profile', return_value={'id': 'broken', 'engine': 'reference',
            'ready': False, 'diagnostics': [{'code': 'missing', 'message': 'missing required asset'}]}):
            status, response = self.request('/api/campaigns', 'POST', {'name': 'Missing DB', 'engine': 'reference',
                'databaseProfile': 'broken', 'status': 'paused'})
        self.assertEqual(status, 400, response)
        self.assertIn('missing required asset', response['error'])


@unittest.skipUnless(os.environ.get('PROTEIN_STUDIO_SCALE_TEST') == '1', 'explicit 100k ledger benchmark')
class ScaleTests(unittest.TestCase):
    def test_100000_metadata_items_bounded_chunks_and_last_page(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'campaigns.sqlite3'
            store = CampaignStore(path)
            cid = store.create(spec('paused'))['id']
            start = time.monotonic()
            for offset in range(0, 100000, 200):
                store.append(cid, [{'targetId': f'target-{i}', 'submission': submission('ACDEFGHIKLMNPQRSTVWY', f'target-{i}')}
                                   for i in range(offset, offset + 200)])
            insert_seconds = time.monotonic() - start
            start = time.monotonic()
            page = store.items(cid, limit=25, offset=99975)
            page_seconds = time.monotonic() - start
            start = time.monotonic()
            campaign = store.get(cid)
            stats_seconds = time.monotonic() - start
            self.assertEqual(page['total'], 100000)
            self.assertEqual(len(page['items']), 25)
            self.assertIsNone(page['nextOffset'])
            self.assertEqual(campaign['counts']['waiting_input'], 100000)
            result = {'items': 100000, 'chunkSize': 200, 'sequenceLength': 20,
                'insertSeconds': round(insert_seconds, 3), 'lastPageSeconds': round(page_seconds, 6),
                'statsSeconds': round(stats_seconds, 6), 'databaseBytes': path.stat().st_size,
                'peakRssPlatformUnits': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'scope': 'Local SQLite metadata only, paused campaign; no model predictions or GPU throughput measured.'}
            print('PIPELINE_SCALE_RESULT=' + json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    unittest.main()
