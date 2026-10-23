"""Acknowledged scratch lifetime across real localhost worker transfers."""
import io
import json
from pathlib import Path
import unittest
import zipfile
from unittest.mock import patch

from backend.tests import test_distributed as fixtures
from backend.tests.test_pipeline import wait_for


class WorkerStorageTests(unittest.TestCase):
    def setUp(self):
        # Reuse the actual controller/HTTP/resident harness without inheriting
        # its unrelated test methods into this focused regression suite.
        self.harness = fixtures.DistributedHTTPTests(methodName='runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.tearDown)

    def test_acknowledged_target_is_deleted_without_touching_pending_sibling(self):
        h = self.harness
        campaign = h.campaign(2)
        worker = h.client()
        original = worker.resident.predict_batch
        observed = []

        def inspect(targets, cancelled=None, emit=None):
            roots = {target['targetId']: Path(target['outputDir']).parent for target in targets}

            def callback(event):
                if event.get('type') != 'target_result':
                    return emit(event)
                item = event['targetId']
                self.assertTrue(roots[item].is_dir())
                emit(event)
                self.assertFalse(roots[item].exists())
                for other, root in roots.items():
                    if other != item and other not in observed:
                        self.assertTrue((root / 'prepared.json').is_file())
                        self.assertTrue((root / 'output').is_dir())
                observed.append(item)

            return original(targets, cancelled=cancelled, emit=callback)

        with patch.object(worker.resident, 'predict_batch', side_effect=inspect):
            self.assertTrue(worker.run_once())
        self.assertEqual(len(observed), 2)
        self.assertEqual(list((worker.root / 'batches').iterdir()), [])
        wait_for(lambda: h.app.pipeline.store.get(campaign)['counts']['completed'] == 2)
        items = h.app.pipeline.store.items(campaign)['items']
        for item in items:
            result = h.app.pipeline.detail(campaign, item['id'])['result']
            self.assertTrue(result['artifacts'])
            artifact = result['artifacts'][0]
            self.assertTrue(h.app.pipeline.artifact(campaign, item['id'], artifact['name']).is_file())

    def test_lost_ack_keeps_bytes_for_idempotent_retry_then_cleans(self):
        h = self.harness
        campaign = h.campaign(1)
        worker = h.client()
        original = worker.request
        sent = []

        def lost_once(path, *args, **kwargs):
            if not path.startswith('/worker/v1/result/'):
                return original(path, *args, **kwargs)
            bundle = Path(kwargs['data'].name)
            item_root = bundle.parent
            self.assertTrue(bundle.is_file())
            self.assertTrue((item_root / 'prepared.json').is_file())
            self.assertTrue((item_root / 'output' / 'result.json').is_file())
            sent.append((bundle, kwargs['headers']['X-Content-SHA256']))
            response = original(path, *args, **kwargs)
            if len(sent) == 1:
                response.read()
                response.close()
                self.assertTrue(item_root.exists())
                raise OSError('Acknowledgement lost after controller committed')
            return response

        with patch.object(worker, 'request', side_effect=lost_once):
            self.assertTrue(worker.run_once())
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], sent[1], 'the same completed bytes must be retried')
        self.assertFalse(sent[0][0].parent.exists())
        wait_for(lambda: h.app.pipeline.store.get(campaign)['counts']['completed'] == 1)
        self.assertEqual(worker.processed, 1)
        uploads = [entry for entry in h.app.distributed.cluster()['transfers'] if entry['direction'] == 'upload']
        self.assertEqual(len(uploads), 1)

    def test_unconfirmed_success_response_retains_all_target_files(self):
        h = self.harness
        campaign = h.campaign(1)
        worker = h.client()
        original = worker.request
        bundles = []

        def unconfirmed(path, *args, **kwargs):
            if not path.startswith('/worker/v1/result/'):
                return original(path, *args, **kwargs)
            bundle = Path(kwargs['data'].name)
            bundles.append(bundle)
            # A 2xx response with the wrong digest is not an acceptance receipt.
            return io.BytesIO(json.dumps({'accepted': True, 'itemId': path.rsplit('/', 1)[1],
                                          'sha256': '0' * 64}).encode())

        with patch.object(worker, 'request', side_effect=unconfirmed):
            self.assertTrue(worker.run_once())
        self.assertEqual(len(bundles), 5)
        item_root = bundles[0].parent
        self.assertTrue(bundles[0].is_file())
        self.assertTrue((item_root / 'prepared.zip').is_file())
        self.assertTrue((item_root / 'prepared.json').is_file())
        self.assertTrue((item_root / 'output' / 'result.json').is_file())
        self.assertEqual(worker.processed, 0)
        self.assertEqual(h.app.pipeline.store.get(campaign)['counts']['completed'], 0)

    def test_cleanup_error_does_not_revoke_durable_acceptance(self):
        h = self.harness
        campaign = h.campaign(1)
        worker = h.client()
        with patch('backend.worker_client.shutil.rmtree', side_effect=PermissionError('test cleanup denied')):
            self.assertTrue(worker.run_once())
        wait_for(lambda: h.app.pipeline.store.get(campaign)['counts']['completed'] == 1)
        self.assertEqual(worker.processed, 1)
        self.assertTrue(list((worker.root / 'batches').rglob('result.zip')))

    def test_transfer_omits_derived_pae_but_preserves_original_confidence(self):
        h = self.harness
        campaign = h.campaign(1)
        worker = h.client()
        original_predict, original_request = worker.resident.predict_batch, worker.request
        observed = []

        def predict(targets, cancelled=None, emit=None):
            roots = {target['targetId']: Path(target['outputDir']) for target in targets}
            def callback(event):
                if event.get('type') == 'target_result':
                    root = roots[event['targetId']]
                    derived = root / '.result-artifacts'
                    derived.mkdir()
                    (derived / 'matrix.f64').write_bytes(b'controlled-derived-fixture')
                    (derived / 'manifest.json').write_text('{"fixture":true}')
                    (root / 'worker-original-confidence.json').write_text('{"fixture":"original"}')
                return emit(event)
            return original_predict(targets, cancelled=cancelled, emit=callback)

        def inspect(path, *args, **kwargs):
            if path.startswith('/worker/v1/result/'):
                with zipfile.ZipFile(kwargs['data'].name) as bundle:
                    names = bundle.namelist()
                    self.assertNotIn('payload/result.json', names)
                    self.assertFalse(any('/.result-artifacts/' in name for name in names))
                    self.assertIn('payload/worker-original-confidence.json', names)
                    observed.append(names)
            return original_request(path, *args, **kwargs)

        with patch.object(worker.resident, 'predict_batch', side_effect=predict), \
             patch.object(worker, 'request', side_effect=inspect):
            self.assertTrue(worker.run_once())
        self.assertEqual(len(observed), 1)
        wait_for(lambda: h.app.pipeline.store.get(campaign)['counts']['completed'] == 1)


if __name__ == '__main__':
    unittest.main()
