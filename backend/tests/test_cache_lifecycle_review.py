"""Independent maintenance race regression; only temporary reference caches."""
from contextlib import contextmanager
import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend import cache_access, engine, staged
from backend.cache_lifecycle import CacheLifecycle
from backend.campaigns import CampaignStore


SCAN_CHILD = '''
import json,sys
from pathlib import Path
from backend.cache_lifecycle import CacheLifecycle
manager=CacheLifecycle(Path(sys.argv[1]))
measure=manager._measure
paused=False
def barrier(*args,**kwargs):
    global paused
    if not paused:
        paused=True
        print('owner-active',flush=True)
        sys.stdin.readline()
    return measure(*args,**kwargs)
manager._measure=barrier
print(json.dumps(manager.scan()),flush=True)
'''


class LifecycleConcurrencyReviewTests(unittest.TestCase):
    def cache_fixture(self, root):
        manager = CacheLifecycle(root)
        job = engine.load_example('crambin')
        job['cacheRoot'] = str(manager.cache_root)
        prepared = staged.prepare_stage(job, root / 'prep', lambda *a: None, lambda: False)
        return manager, prepared

    def scan_child(self, root):
        child = subprocess.Popen([sys.executable, '-c', SCAN_CHILD, str(root)],
            cwd=Path(__file__).resolve().parents[2], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([child.stdout], [], [], 5)
            self.assertTrue(ready, 'Scan child never reached its cache-read barrier.')
            self.assertEqual(child.stdout.readline().strip(), 'owner-active')
        except BaseException:
            child.kill()
            child.communicate(timeout=5)
            raise
        return child

    def test_new_manager_does_not_interrupt_scan_owned_by_another_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, prepared = self.cache_fixture(root)
            child = self.scan_child(root)
            try:
                observer = CacheLifecycle(root)
                snapshot = observer.snapshot()
                self.assertEqual(snapshot['latestScan']['status'], 'scanning_entries')
                self.assertEqual(snapshot['activeOperation']['id'], snapshot['latestScan']['id'])
                self.assertIsNone(child.poll())
                output, error = child.communicate(input='continue\n', timeout=8)
                self.assertEqual(child.returncode, 0, error)
                self.assertEqual(json.loads(output)['status'], 'completed')
                terminal = observer.snapshot()
                self.assertEqual(terminal['latestScan']['status'], 'completed')
                self.assertIsNone(terminal['activeOperation'])
                self.assertTrue(Path(prepared['cacheDirectory']).exists())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=5)

    def test_killed_scan_owner_becomes_interrupted_without_deleting_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _, prepared = self.cache_fixture(root)
            child = self.scan_child(root)
            try:
                child.kill()
                child.communicate(timeout=5)
                observer = CacheLifecycle(root)
                snapshot = observer.snapshot()
                self.assertEqual(snapshot['latestScan']['status'], 'interrupted')
                self.assertIsNone(snapshot['activeOperation'])
                self.assertTrue(snapshot['latestScan']['failClosed'])
                self.assertFalse(snapshot['latestScan']['inventoryComplete'])
                self.assertTrue(Path(prepared['cacheDirectory']).is_dir())
                # The old interrupted owner does not prevent a fresh scan.
                self.assertEqual(observer.scan()['status'], 'completed')
                self.assertIsNone(observer.snapshot()['activeOperation'])
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=5)

    def test_scan_finishing_during_owner_probe_is_not_reported_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.cache_fixture(root)
            child = self.scan_child(root)
            observer = CacheLifecycle(root)
            real_probe = observer._probe_abandoned
            @contextmanager
            def finish_before_probe(operation_id):
                # snapshot has already selected the old running summary. The
                # owner now finishes normally before its flock is probed.
                output, error = child.communicate(input='continue\n', timeout=8)
                self.assertEqual(child.returncode, 0, error)
                self.assertEqual(json.loads(output)['status'], 'completed')
                with real_probe(operation_id) as abandoned:
                    yield abandoned
            try:
                with patch.object(observer, '_probe_abandoned', side_effect=finish_before_probe):
                    snapshot = observer.snapshot()
                self.assertEqual(snapshot['latestScan']['status'], 'completed',
                                 'A failed stale-status CAS must reread the owner-completed summary.')
                self.assertIsNone(snapshot['activeOperation'])
                self.assertEqual(observer.snapshot()['latestScan']['status'], 'completed')
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=5)

    def test_previous_attempt_receipt_cannot_erase_next_attempt_live_references(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            manager = CacheLifecycle(root)
            job = engine.load_example('crambin')
            job['cacheRoot'] = str(manager.cache_root)
            prepared = staged.prepare_stage(job, root / 'prep', lambda *a: None, lambda: False)
            scan = manager.scan()
            plan = manager.plan(scan_id=scan['id'], min_age_seconds=0)
            self.assertEqual(plan['count'], 1)
            # A live target appears after the preview. Every apply must recheck
            # this reference and reject the old removal plan.
            store = CampaignStore(root / 'campaigns.sqlite3')
            campaign = store.create(dict(name='Live reference', engine='reference', status='active',
                                         settings={}, databaseProfile={}, executionProfile={}))
            store.append(campaign['id'], [dict(targetId='one', submission=engine.validate_submission(job))])
            claimed = store.claim('prepare')
            store.advance(claimed['id'], claimed['token'], 'ready',
                          prepared_path=str(root / 'prep/prepared.json'), preparation_cache_key=prepared['cacheKey'])

            first_rechecked, allow_first_failure, first_finalizing, second_rechecked, first_finished = (threading.Event() for _ in range(5))
            results, errors = {}, []
            real_connection = manager.connection
            real_references = manager._protected_references
            real_verify = manager._verify_plan

            @contextmanager
            def delayed_receipt():
                held = getattr(cache_access._local, 'held', {})
                if (threading.current_thread().name == 'apply-first' and first_rechecked.is_set()
                        and manager.cache_root.resolve() not in held):
                    # Pause precisely after attempt one's EX is released, but
                    # before its final status + reference-table cleanup commit.
                    first_finalizing.set()
                    if not second_rechecked.wait(5):
                        raise AssertionError('Second apply never reached its reference snapshot.')
                with real_connection() as connection:
                    yield connection

            def references(*args, **kwargs):
                result = real_references(*args, **kwargs)
                if threading.current_thread().name == 'apply-second':
                    second_rechecked.set()
                    if not first_finished.wait(5):
                        raise AssertionError('First apply receipt never committed.')
                return result

            def verify(*args, **kwargs):
                if threading.current_thread().name == 'apply-first':
                    first_rechecked.set()
                    if not allow_first_failure.wait(5):
                        raise AssertionError('Test did not release first reference verification.')
                return real_verify(*args, **kwargs)

            def apply(label):
                try:
                    results[label] = manager.apply(plan['id'], plan['digest'], confirm=True)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    if label == 'first':
                        first_finished.set()

            first = threading.Thread(target=apply, args=('first',), name='apply-first', daemon=True)
            second = threading.Thread(target=apply, args=('second',), name='apply-second', daemon=True)
            with patch.object(manager, 'connection', side_effect=delayed_receipt), \
                 patch.object(manager, '_protected_references', side_effect=references), \
                 patch.object(manager, '_verify_plan', side_effect=verify):
                try:
                    first.start()
                    self.assertTrue(first_rechecked.wait(5), 'First apply did not reach its reference verification.')
                    second.start()
                    # The first attempt still owns the cache EX. A concurrent
                    # attempt cannot snapshot references inside that section.
                    self.assertFalse(second_rechecked.wait(.1))
                    allow_first_failure.set()
                    first.join(8)
                    second.join(8)
                    self.assertFalse(first.is_alive())
                    self.assertFalse(second.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual(results['first']['status'], 'failed')
                    self.assertEqual(results['second']['status'], 'failed',
                        'Prior apply cleanup erased the live reference snapshot of a concurrent retry.')
                    self.assertTrue(Path(prepared['cacheDirectory']).is_dir())
                finally:
                    allow_first_failure.set()
                    second_rechecked.set()
                    first_finished.set()
                    first.join(8)
                    if second.ident is not None:
                        second.join(8)


if __name__ == '__main__':
    unittest.main()
