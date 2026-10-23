"""Offline generation/launcher/probe contracts; no GPU or cloud connections."""
from __future__ import annotations
import argparse
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest.mock import patch

from scripts import generate_af3_deployment as generator
from scripts import af3_gpu_probe as probe
from scripts import run_controller, run_gpu_worker


class AF3DeploymentGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.inventory = json.loads((generator.ROOT / 'config/af3-production.inventory.example.json').read_text())
        self.inventory['example'] = False
        self.inventory['controller']['workerURL'] = 'https://controller.internal.test:4443'
        self.inventory['model']['weights'] = [dict(file='af3.bin.zst', sha256=hashlib.sha256(b'test weight identity').hexdigest())]

    def generate(self):
        normalized = generator.normalize(self.inventory)
        files, secrets = generator.generate(normalized)
        return normalized, files, secrets

    def test_local16_cloud1_tokens_match_registry_without_public_secret_values(self):
        normalized, files, private = self.generate()
        registry = files['controller/workers.registry.json']['workers']
        self.assertEqual(len(registry), 17)
        self.assertEqual(sum(worker['location'] == 'local' for worker in registry), 16)
        self.assertEqual(sum(worker['location'] == 'aliyun' for worker in registry), 1)
        control_secrets = private['controller/controller.env']
        self.assertEqual(len(control_secrets), 17)
        self.assertEqual(len(set(control_secrets.values())), 17)
        public = json.dumps(files)
        all_worker_ids = set()
        for path, worker in files.items():
            if not path.startswith('hosts/') or '/workers/' not in path or not path.endswith('.json'):
                continue
            name = worker['tokenEnv']
            secret = private[path[:-5] + '.env'][name]
            self.assertEqual(secret, control_secrets[name])
            self.assertNotIn(secret, public)
            self.assertGreaterEqual(len(secret), 24)
            self.assertEqual(worker['runtimeVersion'], normalized['model']['runtimeVersion'])
            match = next(entry for entry in registry if entry['id'] == worker['workerId'])
            self.assertEqual(match['tokenEnv'], name)
            all_worker_ids.add(worker['workerId'])
        self.assertEqual(all_worker_ids, set(files['acceptance.json']['expectedWorkers']))

    def test_bundle_permissions_and_existing_secrets_are_preserved(self):
        _, files, private = self.generate()
        output = self.root / 'private-deployment'
        generator.write_bundle(output, files, private)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
        for name in private:
            self.assertEqual(stat.S_IMODE((output / name).stat().st_mode), 0o600)
        for name in files:
            self.assertEqual(stat.S_IMODE((output / name).stat().st_mode), 0o640)
        before = (output / 'controller/controller.env').read_bytes()
        with self.assertRaisesRegex(ValueError, 'preserve its secrets'):
            generator.write_bundle(output, *generator.generate(generator.normalize(self.inventory)))
        self.assertEqual((output / 'controller/controller.env').read_bytes(), before)

    def test_dry_run_never_generates_or_writes_secrets(self):
        inventory_path = self.root / 'inventory.json'
        inventory_path.write_text(json.dumps(self.inventory))
        output = self.root / 'should-not-exist'
        stdout = io.StringIO()
        with patch.object(generator.secrets, 'token_urlsafe', side_effect=AssertionError('dry run generated a secret')), \
             contextlib.redirect_stdout(stdout):
            code = generator.main(['--inventory', str(inventory_path), '--output', str(output), '--dry-run'])
        report = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertFalse(report['secretsGenerated'])
        self.assertFalse(report['processesStarted'])
        self.assertFalse(output.exists())
        self.assertEqual(report['localGpuCards'], 16)
        self.assertEqual(report['cloudGpuCards'], 1)

    def test_all_generated_worker_and_controller_plans_match_runtime_and_gpu_slots(self):
        normalized, files, private = self.generate()
        controller = run_controller.build_plan(files['controller/controller.json'], environ=private['controller/controller.env'])
        self.assertEqual(controller['environment']['PROTEIN_STUDIO_PREDICTION_MODE'], 'workers')
        self.assertIn('{input_json}', controller['commands']['PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND'])
        self.assertEqual(controller['commands']['PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND'][0], '/opt/af3/venv/bin/python')
        plans = []
        for path, worker in files.items():
            if not path.startswith('hosts/') or '/workers/' not in path or not path.endswith('.json'):
                continue
            environment = private[path[:-5] + '.env']
            plan = run_gpu_worker.build_plan(worker, environ=environment)
            plans.append(plan)
            command = plan['residentCommand']
            self.assertEqual(run_gpu_worker.option_value(command, '--device'), '0')
            self.assertEqual(plan['environment']['CUDA_VISIBLE_DEVICES'], worker['gpuDevices'][0])
            self.assertEqual(run_gpu_worker.option_value(command, '--model-version'), normalized['model']['runtimeVersion'])
            self.assertEqual(run_gpu_worker.option_value(command, '--num-samples'), str(worker['samples']))
            self.assertEqual(run_gpu_worker.option_value(command, '--num-recycles'), str(worker['recycles']))
            self.assertEqual(run_gpu_worker.option_value(command, '--allowed-root'), worker['workDir'])
            self.assertIn('--xla_gpu_enable_triton_gemm=false', plan['environment']['XLA_FLAGS'])
            self.assertIn('--resident-startup-timeout', plan['argv'])
            secret = environment[worker['tokenEnv']]
            self.assertNotIn(secret, json.dumps(run_gpu_worker.safe_plan(plan)))
        self.assertEqual(len(plans), 17)
        self.assertEqual(len({plan['workerId'] for plan in plans}), 17)

    def test_generator_and_probe_manifest_identity_is_order_independent(self):
        model_dir = self.root / 'models'
        model_dir.mkdir()
        weights = []
        for name, content in [('af3.0.bin.zst', b'first test shard'), ('af3.1.bin.zst', b'second test shard')]:
            (model_dir / name).write_bytes(content)
            # Reversed insertion order deliberately differs from generator output.
            weights.append(dict(sha256=hashlib.sha256(content).hexdigest(), file=name))
        self.inventory['model']['weights'] = list(reversed(weights))
        normalized, files, _ = self.generate()
        manifest = self.root / 'expected.json'
        manifest.write_text(json.dumps(files['weights.expected.json']))
        native = types.SimpleNamespace(params=types.SimpleNamespace(
            select_model_files=lambda model_dir: (list(Path(model_dir).glob('*.bin.zst')), True)))
        result = probe.verify_weight_manifest(native, argparse.Namespace(weights_manifest=manifest, model_dir=model_dir))
        self.assertTrue(result['expectedMatch'])
        self.assertEqual(result['manifestSha256'], normalized['model']['weightsManifestSha256'])
        self.assertTrue(normalized['model']['runtimeVersion'].endswith('-w' + result['manifestSha256'][:12]))
        self.inventory['model']['weights'] = weights
        self.assertEqual(generator.normalize(self.inventory)['model']['runtimeVersion'], normalized['model']['runtimeVersion'])

    def test_rejects_incomplete_duplicate_gpu_maps_and_unresolved_placeholders(self):
        cases = []
        incomplete = copy.deepcopy(self.inventory); incomplete['hosts'][0]['gpuDevices'].pop(); cases.append(incomplete)
        duplicate = copy.deepcopy(self.inventory); duplicate['hosts'][0]['gpuDevices'][1] = '0'; cases.append(duplicate)
        duplicate_host = copy.deepcopy(self.inventory); duplicate_host['hosts'][1]['id'] = duplicate_host['hosts'][0]['id']; cases.append(duplicate_host)
        fake_hash = copy.deepcopy(self.inventory); fake_hash['model']['weights'][0]['sha256'] = '0' * 64; cases.append(fake_hash)
        placeholder = copy.deepcopy(self.inventory); placeholder['controller']['databaseRelease'] = 'REPLACE_ME'; cases.append(placeholder)
        example = copy.deepcopy(self.inventory); example['example'] = True; cases.append(example)
        domain = copy.deepcopy(self.inventory); domain['controller']['workerURL'] = 'https://foldspace.example.org'; cases.append(domain)
        for index, inventory in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ValueError):
                generator.normalize(inventory)


if __name__ == '__main__':
    unittest.main()
