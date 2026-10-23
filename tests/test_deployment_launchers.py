"""Deployment launchers: bounded plans, no token leaks, actual subprocess exec."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import run_controller as controller
from scripts import run_gpu_worker as worker


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'backend').mkdir()
        (self.root / 'backend' / '__init__.py').write_text('')
        (self.root / 'scripts').mkdir()
        self.resident = self.root / 'scripts' / 'resident_runner.py'
        self.resident.write_text('# test-only resident placeholder; never loaded\n')
        self.af3 = self.root / 'run_alphafold.py'
        self.af3.write_text('# test-only AF3 source placeholder\n')
        self.token = 'test-secret-not-to-print-' + 'z' * 32
        self.environment = dict(os.environ, TEST_FOLDSPACE_TOKEN=self.token)
        self.worker = dict(schemaVersion=1, role='gpu-worker', projectRoot=str(self.root), python=sys.executable,
            url='https://workers.example.org:4443', workerId='host-a-gpu0', tokenEnv='TEST_FOLDSPACE_TOKEN',
            engine='alphafold3', runtimeVersion='test-installation-only', workDir=str(self.root / 'work'),
            gpuDevices=['GPU-explicit-slot'], samples=5, recycles=10,
            residentStartupTimeout=1800, residentRequestTimeout=86400, httpTimeout=600,
            environment={'XLA_FLAGS': '--xla_gpu_enable_triton_gemm=false'},
            residentCommand=[sys.executable, str(self.resident), '--engine', 'alphafold3', '--af3-script', str(self.af3),
                '--model-dir', str(self.root / 'models'), '--model-version', 'test-installation-only',
                '--num-samples', '5', '--num-recycles', '10', '--device', '0'])
        self.registry = self.root / 'workers.json'
        self.registry.write_text(json.dumps({'schemaVersion': 1, 'workers': [dict(id='host-a-gpu0',name='GPU slot',
            tokenEnv='TEST_FOLDSPACE_TOKEN',location='local',mode='real',engines=['alphafold3'])]}))
        self.databases = self.root / 'databases.json'
        self.databases.write_text('{}')
        self.controller = dict(schemaVersion=1, role='controller', projectRoot=str(self.root),python=sys.executable,
            environment={'PROTEIN_STUDIO_HOST': '127.0.0.1', 'PROTEIN_STUDIO_PORT': '8765',
                'PROTEIN_STUDIO_DISTRIBUTED': '1', 'PROTEIN_STUDIO_PREDICTION_MODE':'workers',
                'PROTEIN_STUDIO_DATA_DIR':str(self.root / 'data'),
                'PROTEIN_STUDIO_DATABASE_CONFIG':str(self.databases),
                'PROTEIN_STUDIO_WORKER_CONFIG':str(self.registry),
                'PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND':[sys.executable,str(self.resident),'--alphafold-script',str(self.af3)]})

    def tearDown(self):
        self.temp.cleanup()

    def test_controller_environment_array_becomes_json_not_shell(self):
        self.controller['environment']['PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND'].append('$(must-not-execute)')
        plan=controller.build_plan(self.controller,self.environment)
        self.assertEqual(json.loads(plan['environment']['PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND'])[-1],'$(must-not-execute)')
        self.assertEqual(plan['argv'],[sys.executable,'-m','backend.server'])
        self.assertNotIn(self.token,json.dumps(controller.safe_plan(plan)))

    def test_controller_rejects_public_bind_secrets_and_shell_commands(self):
        for field,value in [('PROTEIN_STUDIO_HOST','0.0.0.0'),('PROTEIN_STUDIO_API_TOKEN','inline-secret'),
                            ('PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND','python run.py')]:
            cfg=copy.deepcopy(self.controller);cfg['environment'][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):controller.build_plan(cfg,{})

    def test_worker_sets_single_gpu_before_exec_and_timeout_argv(self):
        plan=worker.build_plan(self.worker,dict(self.environment,CUDA_VISIBLE_DEVICES='WRONG',PYTHONPATH='/untrusted'))
        self.assertEqual(plan['environment']['CUDA_VISIBLE_DEVICES'],'GPU-explicit-slot')
        self.assertNotIn('PYTHONPATH',plan['environment'])
        self.assertEqual(plan['argv'][plan['argv'].index('--resident-request-timeout')+1],'86400')
        self.assertNotIn(self.token,json.dumps(worker.safe_plan(plan)))
        self.assertEqual(plan['argv'][plan['argv'].index('--token-env')+1],'TEST_FOLDSPACE_TOKEN')

    def test_worker_rejects_extra_devices_unsafe_url_env_and_signature_mismatch(self):
        invalid=[{'gpuDevices':['0','1']},{'url':'http://cloud.example.org'},{'url':'https://a.example/path'},{'url':'https://a.example:99999'},
                 {'url':'https://user:password@a.example'},{'environment':{'PYTHONPATH':'/tmp'}},
                 {'environment':{'CUDA_VISIBLE_DEVICES':'7'}},{'samples':4},{'runtimeVersion':'different-runtime'},{'httpTimeout':0},
                 {'residentStartupTimeout':True},{'residentRequestTimeout':604801},{'tokenEnv':'XLA_FLAGS'}]
        for changes in invalid:
            cfg=copy.deepcopy(self.worker);cfg.update(changes)
            with self.subTest(changes=changes),self.assertRaises(ValueError):worker.build_plan(cfg,{})

    def test_missing_token_check_fails_without_loading_model_or_creating_workdir(self):
        (self.root/'backend'/'worker_client.py').write_text('raise RuntimeError("must not import")')
        report=worker.check_plan(worker.build_plan(self.worker,{}))
        self.assertFalse(report['ready']);self.assertFalse((self.root/'work').exists())
        report=worker.check_plan(worker.build_plan(self.worker,self.environment))
        self.assertTrue(report['ready']);self.assertNotIn(self.token,json.dumps(report))
        self.assertFalse((self.root/'models').exists())

    def test_worker_check_rejects_missing_script(self):
        (self.root/'backend'/'worker_client.py').write_text('')
        self.af3.unlink()
        with self.assertRaisesRegex(ValueError,'AF3 script'):worker.check_plan(worker.build_plan(self.worker,self.environment))

    def test_dry_run_does_not_check_installation_or_read_environment_tokens(self):
        cfg=copy.deepcopy(self.worker);cfg['python']='/not-installed/python';cfg['residentCommand'][0]='/not-installed/af3/python'
        file=self.root/'worker.json';file.write_text(json.dumps(cfg))
        stream=io.StringIO()
        with patch.dict(os.environ,{'TEST_FOLDSPACE_TOKEN':self.token}),contextlib.redirect_stdout(stream):
            self.assertEqual(worker.main(['--config',str(file),'--dry-run']),0)
        self.assertNotIn(self.token,stream.getvalue());self.assertIn('--resident-request-timeout',stream.getvalue())

    def test_actual_worker_launcher_execs_module_and_fixes_gpu(self):
        (self.root/'backend'/'worker_client.py').write_text('import os,sys,json\nprint(json.dumps({"argv":sys.argv[1:],"gpu":os.environ["CUDA_VISIBLE_DEVICES"],"tokenPresent":len(os.environ.get("TEST_FOLDSPACE_TOKEN",""))>=24}))\n')
        file=self.root/'worker.json';file.write_text(json.dumps(self.worker))
        result=subprocess.run([sys.executable,str(ROOT/'scripts/run_gpu_worker.py'),'--config',str(file)],
            env=self.environment,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr);data=json.loads(result.stdout)
        self.assertEqual(data['gpu'],'GPU-explicit-slot');self.assertTrue(data['tokenPresent'])
        self.assertNotIn(self.token,result.stdout+result.stderr)
        self.assertFalse((self.root/'models').exists())

    def test_controller_actual_registry_preflight_detects_missing_database_and_token(self):
        cfg=copy.deepcopy(self.controller);cfg['projectRoot']=str(ROOT)
        asset=self.root/'sequence.fa';asset.write_text('>sample\nAA\n')
        self.databases.write_text(json.dumps(dict(schemaVersion=1,profiles={'test-af3':dict(engine='alphafold3',release='test-v1',
            root=str(self.root),prepareVersion='test-v1',searchVersion='test-v1',assets=[dict(name='uniref90_database_path',
            kind='sequence',path='sequence.fa',required=True)])})))
        report=controller.check_plan(controller.build_plan(cfg,self.environment));self.assertTrue(report['ready'])
        self.assertEqual(report['tokenEnvironmentNames'],['TEST_FOLDSPACE_TOKEN'])
        report=controller.check_plan(controller.build_plan(cfg,{}));self.assertFalse(report['ready'])
        self.assertEqual(report['missingTokenNames'],['TEST_FOLDSPACE_TOKEN'])
        asset.unlink();report=controller.check_plan(controller.build_plan(cfg,self.environment));self.assertFalse(report['ready'])
        self.assertFalse((self.root/'data').exists())

    def test_actual_controller_launcher_exec_after_preflight(self):
        (self.root/'backend'/'server.py').write_text('import os,json\nprint(json.dumps({"role":"controller","mode":os.environ["PROTEIN_STUDIO_PREDICTION_MODE"]}))\n')
        (self.root/'backend'/'databases.py').write_text('def list_profiles(config):return [{"id":"test","engine":"alphafold3","ready":True,"diagnostics":[]}]\n')
        (self.root/'backend'/'distributed.py').write_text('import json\ndef load_config(path):return json.load(open(path))\n')
        file=self.root/'controller.json';file.write_text(json.dumps(self.controller))
        result=subprocess.run([sys.executable,str(ROOT/'scripts/run_controller.py'),'--config',str(file)],
            env=self.environment,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(json.loads(result.stdout)['mode'],'workers')
        self.assertNotIn(self.token,result.stdout+result.stderr)

    def test_generated_host_bundle_is_accepted_by_both_launchers(self):
        from scripts import generate_af3_deployment as generator
        import hashlib
        raw=json.loads((ROOT/'config/af3-production.inventory.example.json').read_text())
        raw['example']=False
        raw['controller']['workerURL']='https://test-controller.invalid:4443'
        raw['model']['weights'][0]['sha256']=hashlib.sha256(b'synthetic test manifest; not real model weights').hexdigest()
        inventory=generator.normalize(raw)
        with patch.object(generator.secrets,'token_urlsafe',return_value='TEST-ONLY-NOT-A-DEPLOYMENT-CREDENTIAL-123456789'):
            files,private=generator.generate(inventory)
        controller_plan=controller.build_plan(files['controller/controller.json'],{})
        self.assertEqual(controller_plan['argv'][-1],'backend.server')
        worker_count=0
        for name,config in files.items():
            if isinstance(config,dict) and config.get('role')=='gpu-worker':
                worker_plan=worker.build_plan(config,{})
                self.assertEqual(worker_plan['argv'][worker_plan['argv'].index('--url')+1],inventory['controller']['workerURL'])
                self.assertEqual(worker_plan['environment']['CUDA_VISIBLE_DEVICES'],config['gpuDevices'][0])
                self.assertNotIn('TEST-ONLY-NOT-A-DEPLOYMENT-CREDENTIAL',json.dumps(worker.safe_plan(worker_plan)))
                worker_count+=1
        self.assertEqual(worker_count,len(files['controller/workers.registry.json']['workers']))
        self.assertGreaterEqual(worker_count,16)
        self.assertTrue(private)

    def test_duplicate_json_keys_fail(self):
        file=self.root/'bad.json';file.write_text('{"role":"controller","role":"gpu-worker"}')
        with self.assertRaisesRegex(ValueError,'Duplicate'):controller.read_json(file)

    def test_nginx_exposes_worker_prefix_only_and_preserves_auth(self):
        config=(ROOT/'deploy/af3/controller.nginx.conf.example').read_text()
        self.assertIn('location ^~ /worker/v1/',config)
        self.assertIn('location / { return 404; }',config)
        self.assertIn('proxy_set_header Authorization $http_authorization;',config)
        self.assertIn('proxy_pass http://127.0.0.1:8765;',config)
        self.assertNotIn('proxy_pass http://127.0.0.1:8765/;',config)
        self.assertIn('proxy_request_buffering off;',config)


if __name__=='__main__':unittest.main()
