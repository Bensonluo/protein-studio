"""Exercise maintenance locks across real cache publication and private copies.

All filesystem operations use temporary directories. Reference structures are
public replays; the external command test writes synthetic results, without GPU.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend import cache_access, engine, staged
from backend.cache_access import exclusive_cache
from backend.tests.test_staged import FAKE
from backend.worker_pipeline import PipelineScheduler


def wait_for(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        threading.Event().wait(.01)
    raise AssertionError('Cache integration condition did not become true.')


class CachePublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)

    def launch(self, function):
        result, errors = [], []
        def run():
            try:
                result.append(function())
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, result, errors

    def joined(self, task):
        thread, result, errors = task
        thread.join(8)
        self.assertFalse(thread.is_alive(), 'Cache integration thread hung.')
        if errors:
            raise errors[0]
        return result[0] if result else None

    def assert_writer_waits(self, root, entered):
        # Wait for an actual queued writer, rather than relying on the thread
        # happening to be scheduled during a short sleep.
        def queued_or_entered():
            state = cache_access._registry.get(Path(root).resolve())
            if state is None:
                return entered.is_set()
            with state.condition:
                return state.waiting_writers > 0 or entered.is_set()
        wait_for(queued_or_entered)
        self.assertFalse(entered.is_set(), 'Maintenance entered while cache bytes were in use.')

    def reference_job(self):
        job = engine.load_example('crambin')
        job['cacheRoot'] = str(self.root / 'cache')
        return job

    def test_pipeline_lock_spans_atomic_cache_publish_and_ready_pointer_commit(self):
        published, return_prepare, entered = (threading.Event() for _ in range(3))
        descriptor, observed = {}, {}
        def prepare(job, directory, emit, cancelled):
            descriptor.update(staged.prepare_stage(job, directory, emit, cancelled))
            published.set()
            if not return_prepare.wait(6):
                raise AssertionError('Test did not release prepared result.')
            return descriptor
        module = SimpleNamespace(prepare_stage=prepare)
        with patch.dict(os.environ, {'PROTEIN_STUDIO_PREDICTION_MODE': 'workers'}):
            scheduler = PipelineScheduler(self.root / 'data', staged_module=module, prep_workers=1)
        scheduler.preparation_router = lambda conn, now: ('1', [])
        campaign = scheduler.store.create(dict(name='Publish race', engine='reference', status='active',
                                               settings={}, databaseProfile={}, executionProfile={}))
        item = scheduler.store.append(campaign['id'], [dict(targetId='one',
                submission=engine.validate_submission(engine.load_example('crambin')))])['items'][0]
        writer = None
        try:
            scheduler.start()
            self.assertTrue(published.wait(6))
            self.assertTrue((Path(descriptor['cacheDirectory']) / 'COMPLETE.json').is_file())
            before = scheduler.store.item(campaign['id'], item['id'], internal=True)
            self.assertEqual(before['stage'], 'preparing')
            self.assertIsNone(before['prepared_path'])
            self.assertIsNone(before['prepared_cache_key'])
            def maintenance():
                with exclusive_cache(scheduler.cache_root, timeout=6):
                    observed.update(scheduler.store.item(campaign['id'], item['id'], internal=True))
                    entered.set()
            writer = self.launch(maintenance)
            self.assert_writer_waits(scheduler.cache_root, entered)
            return_prepare.set()
            self.joined(writer)
            self.assertEqual(observed['stage'], 'ready')
            self.assertEqual(observed['prepared_cache_key'], descriptor['cacheKey'])
            persisted = json.loads(Path(observed['prepared_path']).read_text())
            self.assertEqual(persisted['cacheKey'], observed['prepared_cache_key'])
            self.assertEqual(persisted['cacheDirectory'], descriptor['cacheDirectory'])
        finally:
            return_prepare.set()
            scheduler.close()
            if writer:
                writer[0].join(8)

    def test_standalone_prepare_holds_lock_through_published_descriptor_return(self):
        published, release, entered = (threading.Event() for _ in range(3))
        job = self.reference_job()
        def emit(stage, *args, **kwargs):
            if stage == 'prepared':
                published.set()
                if not release.wait(6):
                    raise AssertionError('Test did not release standalone prepare.')
        preparation = self.launch(lambda: staged.prepare_stage(job, self.root / 'prep', emit, lambda: False))
        writer = None
        try:
            self.assertTrue(published.wait(6))
            key = staged.prepare_cache_key(job)
            self.assertTrue((Path(job['cacheRoot']) / key / 'COMPLETE.json').is_file())
            def maintenance():
                with exclusive_cache(job['cacheRoot'], timeout=6):
                    entered.set()
            writer = self.launch(maintenance)
            self.assert_writer_waits(job['cacheRoot'], entered)
            release.set()
            prepared = self.joined(preparation)
            self.joined(writer)
            self.assertEqual(prepared['cacheKey'], key)
        finally:
            release.set()
            preparation[0].join(8)
            if writer:
                writer[0].join(8)

    def test_legacy_copy_blocks_maintenance_but_private_reference_read_does_not(self):
        job = self.reference_job()
        prepared = staged.prepare_stage(job, self.root / 'prep', lambda *a: None, lambda: False)
        cache, output = Path(prepared['cacheDirectory']), self.root / 'predict'
        copying, copy_release, parsing, parse_release, entered = (threading.Event() for _ in range(5))
        real_copy, real_parse = staged.shutil.copytree, staged.parse_structure
        def copy(source, destination, *args, **kwargs):
            if Path(source) == cache / 'prepared':
                copying.set()
                if not copy_release.wait(6):
                    raise AssertionError('Test did not release cache copy.')
            return real_copy(source, destination, *args, **kwargs)
        def parse(path, *args, **kwargs):
            if Path(path).is_relative_to(output):
                parsing.set()
                if not parse_release.wait(6):
                    raise AssertionError('Test did not release private reference read.')
            return real_parse(path, *args, **kwargs)
        writer = None
        with patch.object(staged.shutil, 'copytree', side_effect=copy), patch.object(staged, 'parse_structure', side_effect=parse):
            prediction = self.launch(lambda: staged.predict_stage(job, prepared, output, lambda *a: None, lambda: False))
            try:
                self.assertTrue(copying.wait(6))
                def maintenance():
                    with exclusive_cache(job['cacheRoot'], timeout=6):
                        self.assertTrue((output / 'prepared').is_dir())
                        # Removing the shared name after the copy must not break
                        # a reader operating solely on its private attempt files.
                        cache.rename(self.root / 'quarantined-reference')
                        entered.set()
                writer = self.launch(maintenance)
                self.assert_writer_waits(job['cacheRoot'], entered)
                copy_release.set()
                self.assertTrue(parsing.wait(6))
                self.assertTrue(entered.wait(6), 'Post-copy parsing retained the maintenance lock.')
                self.assertTrue(prediction[0].is_alive())
                self.joined(writer)
                parse_release.set()
                result = self.joined(prediction)
                self.assertTrue(Path(result['structurePath']).is_file())
                self.assertFalse(result['provenance']['newPrediction'])
            finally:
                copy_release.set()
                parse_release.set()
                prediction[0].join(8)
                if writer:
                    writer[0].join(8)

    def test_external_prediction_process_runs_without_cache_read_lock(self):
        script = self.root / 'fake_staged.py'
        barrier = "else:\n    Path(os.environ['TEST_PREDICT_STARTED']).write_text('yes')\n    while not Path(os.environ['TEST_PREDICT_RELEASE']).exists(): time.sleep(.01)\n    (out/'devices.txt')"
        script.write_text(FAKE.replace("else:\n    (out/'devices.txt')", barrier))
        marker, release = self.root / 'predict-started', self.root / 'predict-release'
        env = {'TEST_PREP_COUNT': str(self.root / 'prep-count'), 'TEST_PREP_STARTED': str(self.root / 'prep-started'),
               'TEST_PREP_SLEEP': '', 'TEST_PREP_FAIL': '', 'TEST_BAD_PREP': '', 'TEST_INLINE_TEMPLATE': '',
               'TEST_PREDICT_STARTED': str(marker), 'TEST_PREDICT_RELEASE': str(release),
               'PROTEIN_STUDIO_GPU_GROUPS_JSON': '[["0"]]', 'PROTEIN_STUDIO_GPU_DEVICES': '',
               'PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND': json.dumps([sys.executable, str(script),
                   'prepare', 'alphafold3', '{input_json}', '{output_dir}', '{job_json}']),
               'PROTEIN_STUDIO_ALPHAFOLD3_PREDICT_COMMAND': json.dumps([sys.executable, str(script),
                   'predict', 'alphafold3', '{prepared_input}', '{output_dir}', '{job_json}', '{seed}'])}
        job = dict(engine='alphafold3', sequence='AG', seeds=[3], numSamples=1, cacheRoot=str(self.root / 'cache'),
                   databaseProfile=dict(id='test-local', engine='alphafold3', ready=True, root=str(self.root / 'db'),
                       profileFingerprint='test-fingerprint', prepareVersion='test-prepare-v1',
                       searchVersion='test-search-v1', searchParameters={}, templateParameters={}))
        with patch.dict(os.environ, env):
            prepared = staged.prepare_stage(job, self.root / 'prep', lambda *a: None, lambda: False)
            prediction = self.launch(lambda: staged.predict_stage(job, prepared, self.root / 'predict', lambda *a: None, lambda: False))
            try:
                wait_for(marker.exists)
                self.assertTrue(prediction[0].is_alive())
                with exclusive_cache(job['cacheRoot'], timeout=1):
                    Path(prepared['cacheDirectory']).rename(self.root / 'quarantined-af3')
                self.assertTrue(prediction[0].is_alive(), 'Synthetic prediction unexpectedly finished before release.')
                release.touch()
                result = self.joined(prediction)
                self.assertEqual(result['sequenceLength'], 2)
                self.assertTrue(Path(result['structurePath']).is_file())
            finally:
                release.touch()
                prediction[0].join(8)


if __name__ == '__main__':
    unittest.main()
