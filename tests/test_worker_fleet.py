import unittest

from scripts.launch_worker_fleet import plan


class WorkerFleetTests(unittest.TestCase):
    def config(self, number=16):
        return {'schemaVersion': 1, 'controllerURL': 'https://fold.example.test',
                'workers': [{'id': f'local-{i:02}', 'tokenEnv': f'LOCAL_{i:02}_TOKEN',
                             'engine': 'alphafold3', 'gpuDevices': [str(i)],
                             'runtimeVersion': 'AF3-test', 'residentCommand': ['python3', 'resident_runner.py']}
                            for i in range(number)]}

    def test_sixteen_disjoint_groups_generate_persistent_worker_commands(self):
        workers = plan(self.config())
        self.assertEqual(len(workers), 16)
        self.assertEqual(len({part for worker in workers for part in worker['gpuDevices']}), 16)
        self.assertTrue(all('backend.worker_client' in worker['argv'] for worker in workers))
        self.assertTrue(all('--samples' in worker['argv'] for worker in workers))

    def test_rejects_overlapping_groups_and_unsupported_native_multigpu(self):
        config = self.config(2)
        config['workers'][1]['gpuDevices'] = ['0']
        with self.assertRaisesRegex(ValueError, 'overlap'):
            plan(config)
        config = self.config(1)
        config['workers'][0]['gpuDevices'] = ['0', '1']
        with self.assertRaisesRegex(ValueError, 'one device'):
            plan(config)

    def test_rejects_inline_secrets_and_remote_plain_http(self):
        config = self.config(1)
        config['workers'][0]['tokenEnv'] = 'secret-value-instead-of-name'
        with self.assertRaises(ValueError):
            plan(config)
        config = self.config(1)
        config['controllerURL'] = 'http://cloud.example.test'
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            plan(config)

    def test_fixture_worker_has_no_gpu_and_is_explicitly_demo(self):
        config = self.config(1)
        config['workers'][0].update(engine='reference', gpuDevices=[])
        worker = plan(config)[0]
        self.assertIn('--demo', worker['argv'])
        self.assertEqual(worker['gpuDevices'], [])
