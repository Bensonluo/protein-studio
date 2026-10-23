"""Cache administration regression using temporary controller data only."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from unittest.mock import patch

from backend.cache_access import shared_cache
from backend.cache_lifecycle import Budget, CacheLifecycle, LifecycleCancelled, MAX_JSON
from backend.campaigns import CampaignStore


class CacheLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.manager = CacheLifecycle(self.root)
        self.store = CampaignStore(self.root/'campaigns.sqlite3')
        self.counter = 0

    def cache(self, label, kind='whole', leaves=None):
        key = hashlib.sha256(label.encode()).hexdigest()
        directory = self.manager.cache_root/('af3-chain-v1' if kind == 'leaf' else '')/key
        directory.mkdir(parents=True)
        content = json.dumps({'sequence': label}).encode()
        (directory/'input.json').write_bytes(content)
        manifest = {'schema': 1, 'cacheKey': key, 'inputRelativePath': 'input.json',
                    'artifacts': [{'name': 'input.json', 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}]}
        if leaves is not None:
            manifest['chainReuse'] = {'strategy': 'af3-chain-reuse-v1', 'entries': [{'cacheKey': leaf} for leaf in leaves]}
        (directory/'COMPLETE.json').write_text(json.dumps(manifest))
        return key, directory

    def reference(self, key, stage='ready', status='active', leaves=None, legacy=False, result='missing'):
        self.counter += 1
        campaign = self.store.create({'name': 'Campaign', 'engine': 'alphafold3' if leaves is not None else 'reference',
                                      'status': 'active' if status == 'cancelled' else status, 'settings': {}, 'databaseProfile': {}, 'executionProfile': {}})
        item = self.store.append(campaign['id'], [{'targetId': str(self.counter), 'submission': {'name': 'Target', 'chains': [{'id': 'A', 'sequence': 'AG'}]}}])['items'][0]
        item_root = self.root/'campaigns'/campaign['id']/item['id']
        item_root.mkdir(parents=True)
        prepared = item_root/'prepared.json'
        prepared.write_text(json.dumps({'cacheKey': key, 'cacheDirectory': str(self.manager.cache_root/key)}))
        result_path = item_root/'result.json'
        if result == 'valid':
            (item_root/'model.cif').write_text('data_model\n')
            result_path.write_text(json.dumps({'structureFile': 'model.cif'}))
        elif result == 'oversized':
            result_path.write_text(' '*(MAX_JSON+1))
        reuse = {'strategy': 'af3-chain-reuse-v1', 'entries': [{'cacheKey': leaf} for leaf in leaves]} if leaves is not None else None
        with self.store.connection(True) as conn:
            conn.execute('UPDATE campaigns SET status=? WHERE id=?', (status, campaign['id']))
            conn.execute('UPDATE campaign_items SET stage=?,prepared_path=?,prepared_cache_key=?,preparation_reuse=?,result_path=? WHERE id=?',
                         (stage, str(prepared), None if legacy else key, json.dumps(reuse) if reuse else None, str(result_path), item['id']))
        return item, prepared, result_path

    def scan_entries(self, **kwargs):
        scan = self.manager.scan(**kwargs)
        return scan, {x['relativePath']: x for x in self.manager.entries(scan['id'], limit=200)['items']}

    def plan(self, action='quarantine', **kwargs):
        self.manager.scan()
        return self.manager.plan(action=action, min_age_seconds=0, retention_seconds=0, **kwargs)

    def apply(self, plan):
        return self.manager.apply(plan['id'], plan['digest'], confirm=True)

    def test_inventory_kinds_and_polling_does_not_walk(self):
        whole, _ = self.cache('whole')
        leaf, _ = self.cache('leaf', 'leaf')
        partial = self.manager.cache_root/(whole+'.partial-abc')
        partial.mkdir(); (partial/'unfinished').write_bytes(b'abc')
        (self.manager.cache_root/'do-not-manage').write_bytes(b'foreign')
        outside = self.root/'outside'; outside.write_bytes(b'never follow')
        (self.manager.cache_root/'link').symlink_to(outside)
        scan, entries = self.scan_entries()
        self.assertTrue(scan['inventoryComplete'])
        self.assertEqual(scan['completedEntries'], 5)
        self.assertEqual(scan['byKind']['unknown']['entries'], 2)
        self.assertTrue(entries['link']['protected'])
        self.assertEqual(entries['link']['logicalBytes'], 0)
        with patch('backend.cache_lifecycle.os.scandir', side_effect=AssertionError('GET walked filesystem')):
            self.assertEqual(self.manager.snapshot()['latestScan']['id'], scan['id'])
            self.assertEqual(self.manager.entries(scan['id'], limit=2)['nextOffset'], 2)
        self.assertEqual(outside.read_bytes(), b'never follow')

    def test_active_paused_failed_pin_whole_and_leaf_cancelled_releases(self):
        leaf, _ = self.cache('shared', 'leaf')
        for label, stage, status in [('ready', 'ready', 'active'), ('paused', 'ready', 'paused'), ('failed', 'failed', 'active')]:
            key, _ = self.cache(label, leaves=[leaf])
            self.reference(key, stage, status, [leaf])
        free, _ = self.cache('cancelled')
        self.reference(free, status='cancelled')
        scan, entries = self.scan_entries()
        self.assertTrue(scan['referencesComplete']); self.assertFalse(scan['failClosed'])
        self.assertEqual(scan['protectedEntries'], 4)
        self.assertFalse(entries[free]['protected'])
        plan = self.manager.plan(min_age_seconds=0)
        self.assertEqual(plan['count'], 1)

    def test_completed_only_releases_cache_when_publication_exists(self):
        valid, _ = self.cache('valid'); missing, _ = self.cache('missing'); huge, _ = self.cache('huge')
        self.reference(valid, 'completed', result='valid')
        self.reference(missing, 'completed')
        self.reference(huge, 'completed', result='oversized')
        scan, entries = self.scan_entries()
        self.assertFalse(entries[valid]['protected'])
        self.assertTrue(entries[missing]['protected']); self.assertTrue(entries[huge]['protected'])
        self.assertFalse(scan['failClosed'])
        self.assertEqual(sum(d['code']=='completed_output_unverified' for d in scan['diagnostics']), 2)

    def test_completed_symlinked_result_does_not_release_cache(self):
        key, _ = self.cache('unsafe completed')
        _, _, result = self.reference(key, 'completed', result='valid')
        external = self.root/'external.json'; external.write_bytes(result.read_bytes())
        result.unlink(); result.symlink_to(external)
        _, entries = self.scan_entries()
        self.assertTrue(entries[key]['protected'])

    def test_legacy_sidecar_and_unknown_reference_fail_closed(self):
        key, _ = self.cache('legacy')
        _, sidecar, _ = self.reference(key, legacy=True)
        scan, entries = self.scan_entries()
        self.assertFalse(scan['failClosed']); self.assertTrue(entries[key]['protected'])
        sidecar.write_text('{"cacheDirectory":"/outside"}')
        other, _ = self.cache('unrelated')
        scan, entries = self.scan_entries()
        self.assertTrue(scan['failClosed']); self.assertTrue(entries[other]['protected'])
        with self.assertRaises(ValueError):self.manager.plan(min_age_seconds=0)

    def test_unreadable_entry_marks_inventory_incomplete_and_prevents_plan(self):
        one, _ = self.cache('readable'); bad, bad_path = self.cache('unreadable')
        original = self.manager._measure
        def measure(path, budget):
            if path == bad_path:raise PermissionError('Synthetic unreadable directory')
            return original(path, budget)
        with patch.object(self.manager, '_measure', side_effect=measure):
            scan, entries = self.scan_entries()
        self.assertEqual(scan['unreadableEntries'], 1)
        self.assertFalse(scan['inventoryComplete']); self.assertTrue(scan['failClosed'])
        self.assertTrue(entries[one]['protected'])
        with self.assertRaises(ValueError):self.manager.plan(min_age_seconds=0)

    def test_manifest_missing_artifact_is_unknown_protected(self):
        key, directory = self.cache('lost artifact')
        (directory/'input.json').unlink()
        scan, entries = self.scan_entries()
        self.assertEqual(entries[key]['kind'], 'unknown')
        self.assertTrue(entries[key]['protected'])
        self.assertEqual(self.manager.plan(min_age_seconds=0)['count'], 0)

    def test_manifest_artifact_loop_obeys_cancel_without_treating_it_as_bad_metadata(self):
        key, directory = self.cache('manifest cancellation')
        marker = directory/'COMPLETE.json'
        manifest = json.loads(marker.read_text())
        for n in range(4):
            name = f'extra-{n}'
            (directory/name).write_bytes(b'x')
            manifest['artifacts'].append({'name': name, 'bytes': 1, 'sha256': hashlib.sha256(b'x').hexdigest()})
        marker.write_text(json.dumps(manifest))
        checks = []
        def cancelled():
            checks.append(True)
            return len(checks) == 2
        with self.assertRaises(LifecycleCancelled):
            self.manager._manifest(directory, key, Budget(cancelled=cancelled))
        self.assertEqual(len(checks), 2)
        original = self.manager._manifest
        def cancel_during_manifest(path, cache_key, budget=None):
            self.assertIsNotNone(budget)
            checks.clear()
            budget.cancelled = cancelled
            return original(path, cache_key, budget)
        with patch.object(self.manager, '_manifest', side_effect=cancel_during_manifest):
            result = self.manager.scan()
        self.assertEqual(result['status'], 'cancelled')
        self.assertFalse(result['inventoryComplete'])
        self.assertFalse(any(item['code']=='entry_unreadable' for item in result['diagnostics']))

    def test_scan_bounds_and_cancellation(self):
        for n in range(5): self.cache(str(n))
        scan = self.manager.scan(max_entries=4)
        self.assertTrue(scan['truncated']); self.assertFalse(scan['inventoryComplete'])
        self.assertLessEqual(scan['scannedEntries'], 4)
        self.assertLess(scan['completedEntries'], 5)
        with self.assertRaises(LifecycleCancelled): self.manager.scan(cancelled=lambda: True)
        self.assertFalse(self.manager.snapshot()['activeOperation'])

    def test_many_references_stream_without_loading_target_payloads_and_obey_scan_budget(self):
        leaf, _ = self.cache('one shared leaf', 'leaf')
        key, _ = self.cache('one shared whole', leaves=[leaf])
        campaign = self.store.create({'name': '1000 references', 'engine': 'alphafold3',
                                      'status': 'paused', 'settings': {}, 'databaseProfile': {}, 'executionProfile': {}})
        payload = {'chains': [{'id': 'A', 'sequence': 'AG'}], 'unrelatedMetadata': 'x'*16000}
        for start in range(0, 1000, 200):
            self.store.append(campaign['id'], [{'targetId': str(n), 'submission': payload} for n in range(start, start+200)])
        reuse = json.dumps({'strategy': 'af3-chain-reuse-v1', 'entries': [{'cacheKey': leaf}]})
        with self.store.connection(True) as conn:
            conn.execute("UPDATE campaign_items SET stage='ready',prepared_cache_key=?,preparation_reuse=? WHERE campaign_id=?",
                         (key, reuse, campaign['id']))
        # This fixture has >16 MB of input payloads in SQLite. Inventory only
        # selects compact reference columns and deduplicates the shared keys.
        tracemalloc.start()
        try:
            with patch.object(self.manager, '_read_json', wraps=self.manager._read_json) as reads:
                scan = self.manager.scan(max_entries=4)
                self.assertLessEqual(reads.call_count, 2)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 8*1024*1024)
        self.assertTrue(scan['referencesComplete']); self.assertTrue(scan['truncated'])
        self.assertLessEqual(scan['scannedEntries'], 4)
        with self.manager.connection() as conn:
            reference = conn.execute("SELECT references_count FROM scan_refs WHERE scan_id=? AND kind='whole'", (scan['id'],)).fetchone()
            self.assertEqual(reference[0], 1000)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM scan_refs WHERE scan_id=?', (scan['id'],)).fetchone()[0], 2)

    def test_reference_added_after_plan_prevents_any_mutation(self):
        key, directory = self.cache('later referenced')
        plan = self.plan()
        self.reference(key)
        result = self.apply(plan)
        self.assertEqual(result['status'], 'failed'); self.assertEqual(result['appliedEntries'], 0)
        self.assertTrue(directory.is_dir())

    def test_changed_fingerprint_revalidates_entire_plan_before_mutation(self):
        _, first = self.cache('first'); _, second = self.cache('second')
        plan = self.plan()
        (second/'extra').write_bytes(b'changed after preview')
        self.assertEqual(self.apply(plan)['status'], 'failed')
        self.assertTrue(first.is_dir()); self.assertTrue(second.is_dir())

    def test_quarantine_restore_and_purge_are_distinct_actions(self):
        _, directory = self.cache('reversible')
        original = (directory/'input.json').read_bytes()
        plan = self.plan(); result = self.apply(plan)
        self.assertEqual(result['status'], 'completed'); self.assertFalse(directory.exists())
        self.assertGreater(result['movedLogicalBytes'], 0); self.assertEqual(result['purgedLogicalBytes'], 0)
        self.assertIsNone(result['reclaimedBytes'])
        self.assertEqual(self.apply(plan), result)
        qid = self.manager.plan_details(plan['id'])['items'][0]['quarantineId']
        restore = self.plan('restore', quarantine_id=qid)
        restored = self.apply(restore)
        self.assertEqual(restored['restoredLogicalBytes'], result['movedLogicalBytes'])
        self.assertEqual((directory/'input.json').read_bytes(), original)
        again = self.apply(self.plan())
        self.manager.scan()
        withheld = self.manager.plan(action='purge', retention_seconds=86400)
        self.assertEqual(withheld['count'], 0)
        purge = self.manager.plan(action='purge', retention_seconds=0)
        purged = self.apply(purge)
        self.assertEqual(purged['purgedLogicalBytes'], again['movedLogicalBytes'])
        self.assertEqual(purged['purgedEntries'], 1)
        self.assertIsNone(purged['reclaimedBytes'])

    def test_restore_never_overwrites_new_cache(self):
        key, directory = self.cache('destination')
        plan = self.plan(); self.apply(plan)
        qid = self.manager.plan_details(plan['id'])['items'][0]['quarantineId']
        restore = self.plan('restore', quarantine_id=qid)
        directory.mkdir(); (directory/'new').write_text('keep')
        self.assertEqual(self.apply(restore)['status'], 'failed')
        self.assertEqual((directory/'new').read_text(), 'keep')

    def test_rename_before_receipt_crash_reconciles_and_retry_is_idempotent(self):
        _, directory = self.cache('crash recovery')
        plan = self.plan()
        with patch.object(self.manager, '_fsync_directory', side_effect=OSError('injected after rename')):
            self.assertEqual(self.apply(plan)['status'], 'failed')
        self.assertFalse(directory.exists())
        self.manager.scan()
        retried = self.apply(plan)
        self.assertEqual(retried['status'], 'completed'); self.assertEqual(retried['movedEntries'], 1)
        self.assertIsNotNone(self.manager.plan_details(plan['id'])['items'][0]['quarantineId'])
        self.assertEqual(self.apply(plan), retried)

    def test_scan_waits_publish_to_ready_reader_and_pins_new_reference(self):
        key, directory = self.cache('publish gap')
        begun, release = threading.Event(), threading.Event()
        result = []
        def publisher():
            with shared_cache(self.manager.cache_root):
                begun.set(); release.wait(3); self.reference(key)
        thread = threading.Thread(target=publisher); thread.start(); self.assertTrue(begun.wait(2))
        scanner = threading.Thread(target=lambda: result.append(self.manager.scan()))
        scanner.start()
        deadline = time.time()+2
        while time.time()<deadline:
            state = self.manager.snapshot()['latestScan']
            if state and state['status']=='waiting_lock':break
            time.sleep(.01)
        self.assertEqual(state['status'], 'waiting_lock')
        self.assertTrue(self.manager.snapshot()['activeOperation'])
        release.set(); thread.join(3); scanner.join(3)
        self.assertFalse(thread.is_alive()); self.assertFalse(scanner.is_alive())
        self.assertTrue(self.manager.entries(result[0]['id'])['items'][0]['protected'])

    def test_exact_confirmation_digest_and_cli_default_dry_run(self):
        _, directory = self.cache('cli')
        plan = self.plan()
        with self.assertRaises(ValueError):self.manager.apply(plan['id'], plan['digest'])
        with self.assertRaises(ValueError):self.manager.apply(plan['id'], '0'*64, confirm=True)
        command = [sys.executable, str(Path(__file__).resolve().parents[2]/'scripts/manage_cache.py'), '--data-dir', str(self.root),
                   'apply', '--plan-id', plan['id'], '--digest', plan['digest']]
        process = subprocess.run(command, text=True, capture_output=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(json.loads(process.stdout)['dryRun']); self.assertTrue(directory.is_dir())
        process = subprocess.run(command+['--apply'], text=True, capture_output=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertFalse(directory.exists())


if __name__ == '__main__':unittest.main()
