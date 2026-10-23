"""Real loopback maintenance API tests; mutations target temporary caches only."""
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from backend import engine, staged
from backend.tests import test_api as base


class StorageAPITests(unittest.TestCase):
    setUp = base.APITests.setUp
    tearDown = base.APITests.tearDown
    request = base.APITests.request
    def wait_job(self, identifier, expected='completed'):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            code, snapshot = self.request('/api/storage')
            self.assertEqual(code, 200, snapshot)
            job = snapshot['job']
            if job and job['id'] == identifier and job['status'] != 'running':
                self.assertEqual(job['status'], expected, job)
                return job
            time.sleep(.02)
        self.fail('Maintenance operation did not finish.')

    def action(self, route, body):
        code, result = self.request('/api/storage/' + route, 'POST', body)
        self.assertEqual(code, 202, result)
        return self.wait_job(result['id'])['result']

    def orphan(self):
        job = engine.load_example('crambin')
        job['cacheRoot'] = str(self.app.pipeline.cache_root)
        return staged.prepare_stage(job, self.root / 'prepare', lambda *args: None, lambda: False)

    def test_gets_only_read_catalog_and_never_start_a_scan(self):
        with patch.object(self.app.storage.manager, 'scan', side_effect=AssertionError('GET must not scan')):
            for _ in range(3):
                code, result = self.request('/api/storage')
                self.assertEqual(code, 200)
                self.assertIsNone(result['state']['latestScan'])
                self.assertIsNone(result['job'])
                self.assertFalse(result['capabilities']['purge'])
            self.assertEqual(self.request('/api/storage/entries?scanId=' + 'a'*32)[0], 404)

    def test_explicit_scan_plan_quarantine_and_restore_preserve_exact_bytes(self):
        prepared = self.orphan()
        original = Path(prepared['cacheDirectory'])
        before = {str(p.relative_to(original)): p.read_bytes() for p in original.rglob('*') if p.is_file()}
        scan = self.action('scan', dict(maxEntries=10000, maxSeconds=10))
        self.assertEqual(scan['status'], 'completed')
        self.assertFalse(scan['failClosed'], scan)
        listing = self.request('/api/storage/entries?scanId='+scan['id']+'&kind=whole')[1]
        self.assertEqual(listing['total'], 1)
        self.assertFalse(listing['items'][0]['protected'])
        plan = self.action('plan', dict(action='quarantine', scanId=scan['id'], minAgeSeconds=0, kinds=['whole']))
        self.assertEqual(plan['count'], 1)
        self.assertEqual(plan['reclaimableBytes'], 0)
        self.assertTrue(original.exists(), 'Planning must not move artifacts.')
        self.assertEqual(self.request('/api/storage/apply', 'POST', dict(planId=plan['id'], digest=plan['digest'], confirm=False))[0], 400)
        self.assertEqual(self.request('/api/storage/apply', 'POST', dict(planId=plan['id'], digest='0'*64, confirm=True))[0], 409)
        self.action('apply', dict(planId=plan['id'], digest=plan['digest'], confirm=True))
        self.assertFalse(original.exists())
        scan = self.action('scan', dict(maxEntries=10000, maxSeconds=10))
        quarantined = self.request('/api/storage/entries?scanId='+scan['id']+'&kind=quarantine')[1]
        self.assertEqual(quarantined['total'], 1)
        undo = self.action('plan', dict(action='restore', scanId=scan['id'], quarantineId=quarantined['items'][0]['quarantineId']))
        self.assertEqual(undo['count'], 1)
        self.action('apply', dict(planId=undo['id'], digest=undo['digest'], confirm=True))
        self.assertEqual({str(p.relative_to(original)):p.read_bytes() for p in original.rglob('*') if p.is_file()}, before)

    def test_purge_and_bad_input_are_rejected_before_starting_work(self):
        for body in [dict(action='purge'), dict(action='invented'), dict(action='quarantine', minAgeSeconds=True),
                     dict(action='quarantine', kinds=['results'])]:
            self.assertEqual(self.request('/api/storage/plan', 'POST', body)[0], 400)
        for body in [dict(maxEntries=0), dict(maxEntries=True), dict(maxSeconds=301), dict(path='/outside')]:
            self.assertEqual(self.request('/api/storage/scan', 'POST', body)[0], 400)
        self.assertEqual(self.request('/api/storage/cancel', 'POST', dict(jobId='bad'))[0], 400)
        self.assertIsNone(self.request('/api/storage')[1]['job'])

    def test_cancelled_scan_waiting_for_cache_access_is_reported_as_cancelled(self):
        from backend.cache_access import shared_cache
        with shared_cache(self.app.pipeline.cache_root):
            code, job = self.request('/api/storage/scan', 'POST', dict(maxSeconds=10))
            self.assertEqual(code, 202)
            code, busy = self.request('/api/storage/scan', 'POST', {})
            self.assertEqual(code, 409, busy)
            code, stopped = self.request('/api/storage/cancel', 'POST', dict(jobId=job['id']))
            self.assertEqual(code, 200)
            self.assertTrue(stopped['cancelRequested'])
            self.wait_job(job['id'], 'cancelled')

    def test_partial_receipt_is_failed_but_preserves_completed_changes(self):
        self.orphan()
        scan = self.action('scan', dict(maxEntries=10000, maxSeconds=10))
        plan = self.action('plan', dict(action='quarantine', scanId=scan['id'], minAgeSeconds=0))
        receipt = dict(status='partial', action='quarantine', movedEntries=1, logicalBytes=27,
                       diagnostics=[dict(code='apply_failed', message='second entry became unavailable')])
        with patch.object(self.app.storage.manager, 'apply', return_value=receipt):
            code, job = self.request('/api/storage/apply', 'POST',
                                     dict(planId=plan['id'], digest=plan['digest'], confirm=True))
            self.assertEqual(code, 202)
            finished = self.wait_job(job['id'], 'failed')
        self.assertEqual(finished['result'], receipt)
        self.assertIn('second entry became unavailable', finished['error'])

    def test_purge_requires_enabled_capability_again_when_exact_plan_is_applied(self):
        self.orphan()
        scan = self.action('scan', dict(maxEntries=10000, maxSeconds=10))
        plan = self.action('plan', dict(action='quarantine', scanId=scan['id'], minAgeSeconds=0))
        self.action('apply', dict(planId=plan['id'], digest=plan['digest'], confirm=True))
        scan = self.action('scan', dict(maxEntries=10000, maxSeconds=10))
        entry = self.request('/api/storage/entries?scanId='+scan['id']+'&kind=quarantine')[1]['items'][0]
        payload = self.app.pipeline.cache_root / entry['relativePath']
        self.app.storage.purge_enabled = True
        plan = self.action('plan', dict(action='purge', scanId=scan['id'], retentionSeconds=0))
        self.app.storage.purge_enabled = False
        body = dict(planId=plan['id'], digest=plan['digest'], confirm=True)
        self.assertEqual(self.request('/api/storage/apply', 'POST', body)[0], 400)
        self.assertTrue(payload.exists())
        self.app.storage.purge_enabled = True
        receipt = self.action('apply', body)
        self.assertEqual(receipt['purgedEntries'], 1)
        self.assertFalse(receipt['spaceReclaimedMeasured'])
        self.assertIsNone(receipt['reclaimedBytes'])
        self.assertFalse(payload.exists())
