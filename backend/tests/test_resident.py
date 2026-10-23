"""Resident lifecycle/native API tests using explicitly synthetic AF3 modules.

The fake installed runtime is only a test double; no GPU/model accuracy claim.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from backend.engine import FIXTURES, load_example
from backend.resident import ResidentProcess, ResidentProcessError, ResidentCancelled
from backend.staged import prepare_stage

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / 'scripts/resident_runner.py'
FAKE_JAX = '''
from contextlib import contextmanager
selected_device=None
class Device:
    compute_capability='8.0'
def local_devices(backend):
    assert backend=='gpu'
    return [Device()]
def device_put(value,device):return value
def block_until_ready(value):return value
@contextmanager
def default_device(device):
    global selected_device
    previous=selected_device
    selected_device=device
    try:yield
    finally:selected_device=previous
class Config:
    def update(self,*args):pass
config=Config()
'''
FAKE_NATIVE = r'''
from functools import cached_property
import json,os,time
from pathlib import Path

def make_model_config(**kwargs):return kwargs
class ModelRunner:
    def __init__(self,config,device,model_dir):
        self.config=config;self.model_dir=Path(model_dir);self.calls=0
        with (self.model_dir/'initializations.txt').open('a') as f:f.write(str(os.getpid())+'\n')
    @cached_property
    def model_params(self):
        import jax
        assert jax.selected_device is not None, 'weights loaded outside the selected-device context'
        with (self.model_dir/'loads.txt').open('a') as f:f.write('load\n')
        return {'weights':{'a':[1,2,3]},'__meta__':{'identifier':b'test-double'}}
    @cached_property
    def _model(self):
        with (self.model_dir/'jits.txt').open('a') as f:f.write('jit-object\n')
        return lambda x:x

def process_fold_input(fold_input,data_pipeline_config,model_runner,output_dir,**kwargs):
    assert data_pipeline_config is None
    assert model_runner._model is model_runner._model
    assert model_runner.model_params['weights']['a']==[1,2,3]
    model_runner.calls+=1
    with (model_runner.model_dir/'calls.txt').open('a') as f:
        f.write(json.dumps({'pid':os.getpid(),'runner':id(model_runner),'calls':model_runner.calls})+'\n')
    print('native log which must never contaminate JSONL stdout')
    os.write(1,b'C-level stdout also belongs in stderr\n')
    if fold_input['modelSeeds']==[99]:raise ValueError('synthetic one-target failure')
    if fold_input['modelSeeds']==[777]:os._exit(7)
    if fold_input['modelSeeds']==[888]:raise MemoryError('synthetic out of memory')
    if fold_input['modelSeeds']==[889]:raise RuntimeError('RESOURCE_EXHAUSTED: test allocator')
    if fold_input['modelSeeds']==[890]:raise RuntimeError('CUDA_ERROR: device lost')
    if fold_input['modelSeeds']==[666]:time.sleep(30)
    output_dir=Path(output_dir)
    def atom(n,aa,b):
        return f'ATOM  {n:5d}  CA  {aa:>3s} A{n:4d}    {float(n):8.3f}{0:8.3f}{0:8.3f}{1:6.2f}{b:6.2f}           C  \n'
    for seed in fold_input['modelSeeds']:
        for sample in range(model_runner.config['num_diffusion_samples']):
            out=output_dir/f'seed-{seed}_sample-{sample}';out.mkdir(parents=True)
            (out/'protein_model.pdb').write_text(atom(1,'ALA',90)+atom(2,'GLY',80))
            (out/'protein_summary_confidences.json').write_text(json.dumps({'ptm':.8,'ranking_score':.7}))
'''


class ResidentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.runtime = self.root / 'fake-runtime'
        self.runtime.mkdir()
        (self.runtime / 'jax.py').write_text(FAKE_JAX)
        common = self.runtime / 'alphafold3/common'
        common.mkdir(parents=True)
        (common.parent / '__init__.py').write_text('')
        (common / '__init__.py').write_text('')
        (common / 'folding_input.py').write_text('import json\nclass Input:\n    @classmethod\n    def from_json(cls,text,json_path=None): return json.loads(text)\n')
        self.native = self.runtime / 'run_alphafold.py'
        self.native.write_text(FAKE_NATIVE)
        self.models = self.root / 'models'
        self.models.mkdir()
        self.prepared = self.root / 'protein_data.json'
        self.prepared.write_text(json.dumps(dict(name='protein',dialect='alphafold3',version=1,modelSeeds=[1],
                 sequences=[dict(protein=dict(id='A',sequence='AG',unpairedMsa='>q\nAG\n',pairedMsa='',templates=[]))])))
        self.command = [sys.executable, str(RUNNER), '--engine', 'alphafold3', '--af3-script', str(self.native),
                        '--model-dir', str(self.models), '--model-version', 'synthetic-test-double-v1',
                        '--num-samples', '2', '--num-recycles', '3', '--allowed-root', str(self.root)]
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.tmp.cleanup()

    def client(self, command=None, request_timeout=10):
        client = ResidentProcess(command or self.command, self.root / ('control-' + str(len(self.clients))),
                                 env={'CUDA_VISIBLE_DEVICES': '4', 'PYTHONPYCACHEPREFIX': str(self.root / 'pycache')},
                                 startup_timeout=15, request_timeout=request_timeout)
        self.clients.append(client)
        return client

    def target(self, target_id, seed=1, **overrides):
        job = dict(engine='alphafold3', sequence='AG', seeds=[seed], numSamples=2, numRecycles=3)
        job.update(overrides)
        return dict(targetId=target_id, job=job, preparedPath=str(self.prepared), outputDir=str(self.root / target_id))

    def test_multiple_targets_and_batches_keep_one_pid_runner_weight_load_and_jit(self):
        client = self.client()
        ready = client.start()
        self.assertEqual(ready['modelLoads'], 1)
        self.assertFalse(ready['isDemo'])
        self.assertFalse(ready['compiledShapesWarmed'])
        self.assertEqual(ready['cudaVisibleDevices'], '4')
        events = []
        first = client.predict_batch([self.target('a', 3), self.target('b', 9)], emit=events.append, load_results=True)
        second = client.predict_batch([self.target('c', 42)])
        self.assertNotIn('result', second[0])
        self.assertTrue(Path(second[0]['resultJson']).is_file())
        self.assertEqual([item['status'] for item in first + second], ['completed'] * 3)
        self.assertEqual({item['pid'] for item in first + second}, {ready['pid']})
        self.assertEqual((self.models / 'loads.txt').read_text().splitlines(), ['load'])
        self.assertEqual((self.models / 'initializations.txt').read_text().splitlines(), [str(ready['pid'])])
        self.assertEqual((self.models / 'jits.txt').read_text().splitlines(), ['jit-object'])
        calls = [json.loads(line) for line in (self.models / 'calls.txt').read_text().splitlines()]
        self.assertEqual([item['calls'] for item in calls], [1, 2, 3])
        self.assertEqual(len({item['runner'] for item in calls}), 1)
        terminals = [event for event in events if event.get('type') == 'target_result']
        self.assertEqual(len(terminals), 2)
        self.assertEqual(terminals[0]['result']['actualSampleCount'], 2)
        self.assertIn('native log', Path(ready['stderrLog']).read_text())
        self.assertIn('C-level stdout', Path(ready['stderrLog']).read_text())

    def test_one_bad_target_and_incompatible_signature_do_not_reload_or_block_others(self):
        client = self.client()
        results = client.predict_batch([self.target('bad', 99), self.target('mismatch', numRecycles=4), self.target('good')])
        self.assertEqual([item['status'] for item in results], ['failed', 'failed', 'completed'])
        self.assertIn('signature', results[1]['error'])
        self.assertFalse(results[0]['retryable'])
        self.assertEqual((self.models / 'loads.txt').read_text().splitlines(), ['load'])
        self.assertFalse(client.broken)

    def test_process_death_preserves_completed_results_and_requires_explicit_restart(self):
        client = self.client()
        events = []
        with self.assertRaises(ResidentProcessError) as context:
            client.predict_batch([self.target('done'), self.target('dies', 777), self.target('never')], emit=events.append)
        self.assertEqual([item['targetId'] for item in context.exception.completed], ['done'])
        self.assertEqual(context.exception.pending_target_ids, ['dies', 'never'])
        self.assertTrue(any(event.get('type') == 'target_result' and event['targetId'] == 'done' for event in events))
        old_pid = client.pid
        with self.assertRaisesRegex(ResidentProcessError, 'restart'):
            client.predict_batch([self.target('must-not-auto-reload')])
        ready = client.restart()
        self.assertNotEqual(old_pid, ready['pid'])
        self.assertEqual(len((self.models / 'loads.txt').read_text().splitlines()), 2)
        self.assertEqual(client.predict_batch([self.target('after-restart')])[0]['status'], 'completed')

    def test_external_idle_process_death_is_not_silently_restarted(self):
        client = self.client()
        client.start()
        os.kill(client.pid, signal.SIGKILL)
        client.process.wait(timeout=5)
        with self.assertRaisesRegex(ResidentProcessError, 'restart'):
            client.predict_batch([self.target('later')])
        self.assertEqual((self.models / 'loads.txt').read_text().splitlines(), ['load'])

    def test_timeout_and_cancellation_kill_resident_without_automatic_retry(self):
        client = self.client(request_timeout=.2)
        with self.assertRaisesRegex(ResidentProcessError, 'timed out'):
            client.predict_batch([self.target('timeout', 666)])
        self.assertIsNotNone(client.process.poll())
        self.assertTrue(client.broken)
        second = self.client()
        second.start()
        start = time.monotonic()
        with self.assertRaises(ResidentCancelled):
            second.predict_batch([self.target('cancel', 666)], cancelled=lambda: time.monotonic() - start > .2)
        self.assertIsNotNone(second.process.poll())
        self.assertTrue(second.broken)

    def test_oom_retires_worker_and_marks_remaining_targets_retryable(self):
        client = self.client()
        results = client.predict_batch([self.target('oom', 888), self.target('remaining')])
        self.assertEqual([item['status'] for item in results], ['failed', 'failed'])
        self.assertFalse(results[0]['retryable'])
        self.assertIn('manual retry', results[0]['error'])
        self.assertTrue(results[1]['retryable'])
        calls = (self.models / 'calls.txt').read_text().splitlines()
        self.assertEqual(len(calls), 1, 'the remaining target must not run after OOM')
        self.assertTrue(client.broken)
        with self.assertRaisesRegex(ResidentProcessError, 'restart'):
            client.start()

    def test_resource_exhaustion_is_terminal_but_transient_device_failure_can_retry(self):
        capacity = self.client()
        result = capacity.predict_batch([self.target('allocator', 889)])[0]
        self.assertFalse(result['retryable'])
        self.assertTrue(capacity.broken)
        transient = self.client()
        result = transient.predict_batch([self.target('device', 890)])[0]
        self.assertTrue(result['retryable'])
        self.assertTrue(transient.broken)

    def test_reference_uses_real_prepared_descriptor_with_zero_model_loads(self):
        job = load_example('ubiquitin-afdb')
        job['cacheRoot'] = str(self.root / 'cache')
        prepared = prepare_stage(job, self.root / 'prepare', lambda *a: None, lambda: False)
        descriptor = self.root / 'prepared.json'
        descriptor.write_text(json.dumps(prepared))
        client = self.client([sys.executable, str(RUNNER), '--engine', 'reference'])
        ready = client.start()
        results = client.predict_batch([dict(targetId='public', job=job, preparedPath=str(descriptor), outputDir=str(self.root / 'public'))], load_results=True)
        self.assertEqual(ready['modelLoads'], 0)
        self.assertTrue(ready['isDemo'])
        self.assertFalse(ready['weightsLoaded'])
        result = results[0]['result']
        self.assertEqual(len(result['residues']), 229)
        self.assertEqual(len(result['pae']), 229)
        self.assertFalse(result['provenance']['newPrediction'])
        self.assertEqual(result['provenance']['modelLoads'], 0)

    def test_invalid_protocol_or_startup_error_never_looks_ready(self):
        client = self.client([sys.executable, '-c', 'print("not-json",flush=True)'])
        with self.assertRaisesRegex(ResidentProcessError, 'invalid protocol'):
            client.start()
        self.assertTrue(client.broken)
        bad = self.client([sys.executable, str(RUNNER), '--engine', 'alphafold3'])
        with self.assertRaisesRegex(ResidentProcessError, 'startup failed'):
            bad.start()


if __name__ == '__main__':
    unittest.main()
