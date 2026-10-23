"""Probe/acceptance contracts using explicit stand-ins, never GPU benchmarks."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from scripts import af3_gpu_probe as probe
from scripts import verify_af3_inference as acceptance


MMCIF = '''data_test
_software.name 'AlphaFold 3 test double'
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.type_symbol
_atom_site.label_atom_id
_atom_site.label_alt_id
_atom_site.label_comp_id
_atom_site.label_asym_id
_atom_site.label_seq_id
_atom_site.Cartn_x
_atom_site.Cartn_y
_atom_site.Cartn_z
_atom_site.occupancy
_atom_site.B_iso_or_equiv
_atom_site.auth_seq_id
_atom_site.auth_asym_id
_atom_site.pdbx_PDB_model_num
ATOM 1 C CA . ALA A 1 1 2 3 1 91 1 A 1
ATOM 2 C CA . GLY A 2 4 5 6 1 81 2 A 1
#
'''


class AF3ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.models = self.root / 'models'
        self.models.mkdir()
        self.weight = self.models / 'af3.bin.zst'
        self.weight.write_bytes(b'explicit synthetic weight bytes for checksum tests')
        self.native = self.root / 'run_alphafold.py'
        self.native.write_text('def make_model_config(**kwargs): pass\nclass ModelRunner:\n def __init__(self,config,device,model_dir): pass\ndef process_fold_input(**kwargs): pass\n')
        self.args = argparse.Namespace(af3_script=self.native, model_dir=self.models, model_version='test-double',
            expected_script_sha256=None, weights_file='af3.bin.zst', expected_weights_sha256=None,
            weights_manifest=None, device=0, attention='triton', num_samples=1, num_recycles=3,
            buckets='256,512', max_template_date='2021-09-30', compilation_cache=None,
            startup_timeout=10, target_timeout=10, report=self.root / 'report.json',
            nvidia_smi='unused', load_model=False, output_dir=self.root / 'results', dry_run=False)
        self.fake_native = types.SimpleNamespace(__file__=str(self.native), params=types.SimpleNamespace(
            select_model_files=lambda model_dir: (list(Path(model_dir).glob('*.bin.zst')), True)))

    def manifest(self, entries):
        self.args.weights_manifest = self.root / 'weights.expected.json'
        self.args.weights_manifest.write_text(json.dumps(dict(schemaVersion=1, files=entries)))

    def prepared(self):
        paths = []
        for index in (1, 2):
            path = self.root / ('target-' + str(index) + '.json')
            path.write_text(json.dumps(dict(name='test', dialect='alphafold3', version=1, modelSeeds=[index],
                sequences=[dict(protein=dict(id='A', sequence='AG', unpairedMsa='>query\nAG\n', pairedMsa='', templates=[]))])))
            paths.append(path)
        self.args.prepared_target = paths

    def test_nvidia_inventory_preserves_real_query_units(self):
        completed = subprocess.CompletedProcess([], 0, '0, GPU-test, NVIDIA H100 80GB HBM3, 81559, 80000, 570.12\n', '')
        with patch.object(probe.subprocess, 'run', return_value=completed) as run:
            result = probe.nvidia_inventory('/admin/nvidia-smi')
        self.assertEqual(result['devices'][0]['memoryTotalMiB'], 81559)
        self.assertEqual(result['devices'][0]['name'], 'NVIDIA H100 80GB HBM3')
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_native_api_binding_and_source_digest_mismatch(self):
        module = probe.import_native(self.native)
        self.assertTrue(callable(module.process_fold_input))
        self.args.expected_script_sha256 = '0' * 64
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            probe.native_source(self.args)

    def test_single_bundle_hash_reports_actual_mismatch(self):
        self.args.expected_weights_sha256 = '0' * 64
        result = probe.verify_weight_digest(self.args)
        self.assertFalse(result['expectedMatch'])
        self.assertEqual(result['sha256'], probe.sha256(self.weight))
        self.args.weights_file = '../outside.bin'
        with self.assertRaisesRegex(ValueError, 'basename'):
            probe.verify_weight_digest(self.args)

    def test_manifest_hash_and_exact_shard_set(self):
        second = self.models / 'af3.1.bin.zst'
        second.write_bytes(b'second synthetic shard')
        entries = [dict(file=second.name, sha256=probe.sha256(second)), dict(file=self.weight.name, sha256=probe.sha256(self.weight))]
        self.manifest(entries)
        result = probe.verify_weight_manifest(self.fake_native, self.args)
        canonical = json.dumps(sorted(entries, key=lambda item: item['file']), ensure_ascii=False, separators=(',', ':')).encode()
        self.assertEqual(result['manifestSha256'], hashlib.sha256(canonical).hexdigest())
        self.assertTrue(result['expectedMatch'])
        declared = json.loads(self.args.weights_manifest.read_text())
        declared['manifestSha256'] = '0' * 64
        self.args.weights_manifest.write_text(json.dumps(declared))
        with self.assertRaisesRegex(ValueError, 'manifestSha256'):
            probe.verify_weight_manifest(self.fake_native, self.args)
        self.manifest(entries[:1])
        with self.assertRaisesRegex(ValueError, 'file set'):
            probe.verify_weight_manifest(self.fake_native, self.args)

    def test_manifest_mismatch_and_unsafe_name_fail(self):
        self.manifest([dict(file=self.weight.name, sha256='0' * 64)])
        self.assertFalse(probe.verify_weight_manifest(self.fake_native, self.args)['expectedMatch'])
        self.manifest([dict(file='../escape', sha256='0' * 64)])
        with self.assertRaisesRegex(ValueError, 'safe basenames'):
            probe.verify_weight_manifest(self.fake_native, self.args)

    def test_load_model_is_explicit_and_initializes_only_once(self):
        calls = []
        class StandInResident:
            def __init__(self, command, work_root, **kwargs):
                calls.append(command)
                self.ready = dict(engine='alphafold3', isDemo=False, modelLoads=1, pid=123)
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with patch.object(probe.sys, 'version_info', (3, 12)), \
             patch.object(probe, 'nvidia_inventory', return_value={'testDouble': True}), \
             patch.object(probe, 'jax_inventory', return_value={'testDouble': True}), \
             patch.object(probe, 'resource_inventory', return_value={'testDouble': True}), \
             patch.object(probe, 'import_native', return_value=self.fake_native), \
             patch.object(probe, 'ResidentProcess', StandInResident):
            result = probe.collect_probe(self.args)
            self.assertEqual(result['status'], 'passed')
            self.assertEqual(result['checks']['modelInitialization']['status'], 'not_run')
            self.assertEqual(calls, [])
            self.args.load_model = True
            result = probe.collect_probe(self.args)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['checks']['modelInitialization']['details']['modelLoads'], 1)
        self.assertFalse(result['inferenceValidated'])

    def test_unavailable_probe_cli_returns_nonzero_and_complete_json(self):
        report = self.root / 'unavailable.json'
        command = [sys.executable, str(probe.ROOT / 'scripts/af3_gpu_probe.py'), '--af3-script', str(self.root/'missing.py'),
                   '--model-dir', str(self.root/'missing-models'), '--model-version', 'not-installed',
                   '--nvidia-smi', str(self.root/'missing-nvidia-smi'), '--report', str(report), '--load-model']
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(completed.returncode, 0)
        value = json.loads(completed.stdout)
        self.assertEqual(value, json.loads(report.read_text()))
        self.assertEqual(value['checks']['modelInitialization']['status'], 'not_run')
        self.assertFalse(value['inferenceValidated'])

    def test_dry_run_validates_prepared_data_without_claiming_gpu_success(self):
        self.prepared()
        self.args.dry_run = True
        self.args.af3_script = self.root / 'not-installed.py'
        result = acceptance.run_acceptance(self.args)
        self.assertEqual(result['status'], 'dry_run')
        self.assertFalse(result['inferenceExecuted'])
        self.assertEqual([target['residueCount'] for target in result['targets']], [2, 2])
        self.assertEqual(result['batches'], [])

    def test_two_batches_check_native_outputs_and_same_resident_without_replay(self):
        self.prepared()
        test = self
        calls = []
        signature = dict(af3ScriptSha256=probe.sha256(self.native), modelVersion='test-double')
        class StandInResident:
            def __init__(self, *args, **kwargs):
                self.pid, self.generation = 1234, 1
                self.ready = dict(engine='alphafold3', isDemo=False, modelLoads=1, initializationCount=1,
                                  weightsLoaded=True, pid=self.pid, signature=signature)
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def predict_batch(self, targets):
                test.assertEqual(len(targets), 1)
                calls.append(targets[0]['targetId'])
                output = Path(targets[0]['outputDir'])
                output.mkdir(parents=True)
                structure = output / 'protein_model.cif'
                structure.write_text(MMCIF)
                (output/'protein_confidences.json').write_text(json.dumps({'pae': [[0, 3], [4, 0]], 'atom_plddts': [91,81]}))
                (output/'protein_summary_confidences.json').write_text(json.dumps({'ptm': .8, 'ranking_score': .7}))
                result = dict(provenance=dict(mode='prediction', newPrediction=True, executionMode='resident-native',
                    isDemo=False, modelLoads=1, residentPid=self.pid, residentSessionId='test-session', modelSignature=signature),
                    structurePath=str(structure), samplingComplete=True, actualSampleCount=1)
                path = output / 'result.json'
                path.write_text(json.dumps(result))
                return [dict(status='completed', resultJson=str(path))]
        with patch.object(acceptance, 'ResidentProcess', StandInResident):
            result = acceptance.run_acceptance(self.args)
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(calls, ['acceptance-1', 'acceptance-2'])
        self.assertTrue(result['samePidAcrossBatches'])
        self.assertEqual(result['modelLoadsPerProcess'], 1)
        self.assertEqual(result['batches'][0]['meanPlddt'], 86)
        self.assertIn('not_evaluated', result['scientificAccuracy'])
        path = Path(result['batches'][0]['resultJson'])
        invalid = json.loads(path.read_text()); invalid['provenance']['mode'] = 'reference'
        path.write_text(json.dumps(invalid))
        with self.assertRaisesRegex(ValueError, 'native resident'):
            acceptance.validate_native_result(path, {'chains':[{'id':'A','sequence':'AG'}], 'seeds':[1], 'numSamples':1},
                                              StandInResident().ready, path.parent)


if __name__ == '__main__':
    unittest.main()
