import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import staged
from backend.worker_pipeline import PipelineScheduler


class WorkerModeTests(unittest.TestCase):
    def command(self, engine, stage, profile=None):
        if stage == 'predict':
            raise ValueError('Prediction runtime is installed on the worker, not this controller.')
        return ['installed-cpu-prepare']

    def test_controller_can_prepare_without_local_gpu_runtime(self):
        job = {'name': 'prepared remotely', 'engine': 'alphafold3',
               'chains': [{'id': 'A', 'sequence': 'ACDEFG'}], 'seeds': [1],
               'numSamples': 5, 'numRecycles': 10,
               'databaseProfile': {'engine': 'alphafold3', 'ready': True,
                                   'prepareVersion': 'test-1', 'profileFingerprint': 'test-fingerprint'}}
        with patch.object(staged, '_command', side_effect=self.command):
            self.assertEqual(len(staged.prepare_cache_key(job)), 64)
            with self.assertRaisesRegex(ValueError, 'worker'):
                staged._normalize(job)
            with patch.dict(os.environ, {'PROTEIN_STUDIO_PREDICTION_MODE': 'workers'}):
                capabilities = {item['engine']: item for item in staged.staged_capabilities()}
                self.assertTrue(capabilities['alphafold3']['available'])
                self.assertFalse(capabilities['colabfold']['available'])
            with patch.dict(os.environ, {'PROTEIN_STUDIO_PREDICTION_MODE': 'legacy'}):
                capabilities = {item['engine']: item for item in staged.staged_capabilities()}
                self.assertFalse(capabilities['alphafold3']['available'])

    def test_workers_mode_never_starts_per_target_prediction_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'PROTEIN_STUDIO_PREDICTION_MODE': 'workers'}):
                scheduler = PipelineScheduler(Path(directory), prep_workers=1, gpu_workers=16)
                try:
                    scheduler.start()
                    self.assertFalse(any(thread.name.startswith('pipeline-predict-') for thread in scheduler.threads))
                    self.assertTrue(any(thread.name.startswith('pipeline-prepare-') for thread in scheduler.threads))
                    self.assertEqual(scheduler.config()['predictionMode'], 'workers')
                finally:
                    scheduler.close()
