"""Cache transport lock integration with actual localhost worker registrations."""
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import zipfile

from backend import cache_access
from backend.cache_access import exclusive_cache
from backend.distributed import digest_file
from backend.engine import PredictionCancelled
from backend.tests import test_distributed
from backend.tests.test_cache_publication import wait_for


class CacheTransportTests(unittest.TestCase):
    def setUp(self):
        # Reuse the real controller/HTTP fixture without inheriting its tests.
        self.fixture = test_distributed.DistributedHTTPTests(
            'test_two_fixture_nodes_real_transfers_resident_reuse_and_final_artifacts')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.app.distributed.config['leaseSeconds'] = 30
        self.fixture.campaign(1)
        registration, token = self.fixture.register()
        status, payload, _ = self.fixture.request('/worker/v1/claim', {'session': registration['session']}, token)
        self.assertEqual(status, 200, payload)
        self.target = payload['batch']['targets'][0]
        self.controller = self.fixture.app.distributed
        self.node = self.controller.authenticate('Bearer ' + token)
        self.cache_root = self.fixture.app.pipeline.cache_root

    def test_zip_holds_read_lock_until_all_cache_bytes_are_independent(self):
        writing, release, entered = (threading.Event() for _ in range(3))
        results, errors = [], []
        real_write = zipfile.ZipFile.write
        def blocked_write(archive, filename, *args, **kwargs):
            if not writing.is_set() and Path(filename).is_relative_to(self.cache_root):
                writing.set()
                if not release.wait(6):
                    raise AssertionError('Test did not release ZIP cache read.')
            return real_write(archive, filename, *args, **kwargs)
        def package():
            try:
                results.append(self.controller.prepared_bundle(self.node, self.target['itemId'], self.target['leaseToken']))
            except BaseException as exc:
                errors.append(exc)
        def maintenance():
            try:
                with exclusive_cache(self.cache_root, timeout=6):
                    entered.set()
            except BaseException as exc:
                errors.append(exc)
        package_thread = threading.Thread(target=package, daemon=True)
        maintenance_thread = threading.Thread(target=maintenance, daemon=True)
        with patch.object(zipfile.ZipFile, 'write', new=blocked_write):
            try:
                package_thread.start()
                self.assertTrue(writing.wait(6))
                maintenance_thread.start()
                def waiting():
                    state = cache_access._registry[self.cache_root.resolve()]
                    with state.condition:
                        return state.waiting_writers > 0 or entered.is_set()
                wait_for(waiting)
                self.assertFalse(entered.is_set(), 'Maintenance entered during ZIP cache reads.')
                release.set()
                package_thread.join(8)
                maintenance_thread.join(8)
                self.assertFalse(package_thread.is_alive())
                self.assertFalse(maintenance_thread.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(entered.is_set())
                bundle, digest = results[0]
                self.assertEqual(digest_file(bundle), digest)
                with zipfile.ZipFile(bundle) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertIn('manifest.json', archive.namelist())
                    self.assertIn('cache/COMPLETE.json', archive.namelist())
                    self.assertTrue(any(name.endswith('.pdb') for name in archive.namelist()))
            finally:
                release.set()
                package_thread.join(8)
                if maintenance_thread.ident is not None:
                    maintenance_thread.join(8)

    def test_controller_shutdown_cancels_bundle_before_touching_cache(self):
        self.controller.stop_event.set()
        with patch.object(self.controller, '_prepared_bundle_locked', side_effect=AssertionError('Read after shutdown')):
            with self.assertRaises(PredictionCancelled):
                self.controller.prepared_bundle(self.node, self.target['itemId'], self.target['leaseToken'])
        # Cancellation while acquiring must leave the maintenance lock usable.
        with exclusive_cache(self.cache_root, timeout=1):
            pass


if __name__ == '__main__':
    unittest.main()
