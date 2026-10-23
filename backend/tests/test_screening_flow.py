"""Fixed-target screening through real CLI/HTTP/SQLite and synthetic CPU searches.

The preparation subprocess, cache publication, resource admission and manifest
replay are real. Database readiness, routing eligibility and free disk bytes are
explicit test fixtures. This test never runs native AF3, accesses a sequence
library, registers a GPU worker, or claims GPU performance validation.
"""
import copy
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from backend import engine, staged
from backend.server import StudioApplication, StudioHTTPServer
from backend.tests.test_chain_reuse import RUNNER

ROOT = Path(__file__).resolve().parents[2]
LOG = logging.getLogger(__name__)


def wait_for(predicate, timeout=12):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=predicate()
        if value:return value
        time.sleep(.02)
    raise AssertionError('Combined screening flow did not reach its expected state.')


class ScreeningFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.counter=self.root/'synthetic-searches.txt'
        self.runner=self.root/'synthetic_prepare.py'
        self.runner.write_text(RUNNER)
        self.fixed=self.root/'fixed.fasta'
        self.fixed.write_text('>fixed-chain-one\nAG\n>fixed-chain-two\nVC\n')
        self.candidates=self.root/'candidates.fasta'
        self.candidates.write_text('>candidate-one\nLK\n>candidate-two\nFT\n>candidate-three\nDE\n')
        self.manifest=self.root/'submission.jsonl'
        self.profile=dict(id='screening-fixture',engine='alphafold3',ready=True,
                          root='/synthetic/no-real-sequence-library',profileFingerprint='synthetic-profile-v1',
                          prepareVersion='synthetic-preparation-v1',searchVersion='synthetic-search-v1',
                          searchParameters={'threads':8},templateParameters={'date':'2021-09-30'})
        environment={
            'PROTEIN_STUDIO_PREDICTION_MODE':'workers',
            'PROTEIN_STUDIO_DISTRIBUTED':'0',
            'PROTEIN_STUDIO_CPU_PREP_WORKERS':'3',
            'PROTEIN_STUDIO_CPU_VALIDATE_WORKERS':'1',
            'PROTEIN_STUDIO_MAX_RESIDUES':'5000',
            'PROTEIN_STUDIO_READY_HIGH_WATER':'8',
            'PROTEIN_STUDIO_PREPARATION_RESOURCES_JSON':json.dumps({
                'cpuBudget':32,'cpuPerPrepare':32,'diskMinFreeBytes':100,'diskPerPrepareBytes':100}),
            'PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND':json.dumps([
                sys.executable,str(self.runner),'{input_json}','{output_dir}']),
            'CHAIN_COUNTER':str(self.counter),'CHAIN_DELAY':'0.08',
            'CHAIN_BAD':'','CHAIN_FAIL_SEQ':'','CHAIN_SLEEP_SEQ':'',
        }
        self.environment=patch.dict(os.environ,environment)
        self.environment.start();self.addCleanup(self.environment.stop)
        def profile(profile_id,*args,**kwargs):
            if profile_id!=self.profile['id']:raise KeyError(profile_id)
            return copy.deepcopy(self.profile)
        self.registry=patch('backend.databases.get_profile',side_effect=profile)
        self.registry.start();self.addCleanup(self.registry.stop)
        self.gpu=patch.object(engine,'_gpu_lease',side_effect=AssertionError('Preparation must not lease a GPU'))
        self.gpu_mock=self.gpu.start();self.addCleanup(self.gpu.stop)
        self.predict=patch.object(staged,'predict_stage',side_effect=AssertionError('Test stops at ready; prediction is forbidden'))
        self.predict_mock=self.predict.start();self.addCleanup(self.predict.stop)
        self.app=StudioApplication(self.root/'data',staged_module=staged)
        self.addCleanup(self.app.close)
        # Eligibility is deliberately granted by this fixture. There are no live
        # or simulated GPU registrations, and workers mode creates no predictor.
        self.app.pipeline.preparation_router=lambda conn,now:('1',[])
        self.disk_sensor={'free':0}
        self.app.pipeline.preparation_resources._disk_usage=lambda path:SimpleNamespace(free=self.disk_sensor['free'])
        self.httpd=StudioHTTPServer(('127.0.0.1',0),self.app)
        self.addCleanup(self.httpd.server_close)
        self.http_thread=threading.Thread(target=self.httpd.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        self.http_thread.start()
        self.addCleanup(self._stop_http)
        self.app.start()
        self.base=f'http://127.0.0.1:{self.httpd.server_port}'
        self.samples=[]
        self.monitor_errors=[]
        self.monitor_stop=threading.Event()
        def monitor():
            while not self.monitor_stop.wait(.005):
                try:
                    state=self.app.pipeline.preparation_resources.snapshot()
                    self.samples.append((state['inflight']['count'],state['inflight']['cpuReserved']))
                except Exception as exc:
                    self.monitor_errors.append(exc)
                    return
        self.monitor=threading.Thread(target=monitor,daemon=True)
        self.monitor.start();self.addCleanup(self._stop_monitor)

    def _stop_http(self):
        self.httpd.shutdown();self.http_thread.join(2)

    def _stop_monitor(self):
        self.monitor_stop.set();self.monitor.join(2)

    def request(self,path,payload=None):
        raw=None if payload is None else json.dumps(payload).encode()
        request=Request(self.base+path,data=raw,headers={'Content-Type':'application/json'} if raw is not None else {})
        with urlopen(request,timeout=5) as response:
            return json.load(response)

    def cli(self,campaign_id=None):
        command=[sys.executable,str(ROOT/'scripts/submit_campaign.py'),'--server',self.base,
                 '--input',str(self.candidates),'--fixed-target',str(self.fixed),
                 '--engine','alphafold3','--database-profile',self.profile['id'],
                 '--manifest',str(self.manifest),'--leave-paused','--chunk-size','2']
        if campaign_id:command.extend(['--campaign-id',campaign_id])
        process=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=30)
        self.assertEqual(process.returncode,0,process.stderr+'\n'+process.stdout)
        return json.loads(process.stdout)

    def searches(self):
        return self.counter.read_text().splitlines() if self.counter.exists() else []

    def test_fixed_partner_cli_resource_admission_chain_cache_and_idempotent_replay(self):
        initial=self.cli()
        cid=initial['campaignId'];base='/api/campaigns/'+cid
        self.assertEqual((initial['inserted'],initial['duplicates'],initial['fixedTargetChains']),(3,0,2))
        self.assertFalse(initial['running'])
        self.assertEqual(initial['chunks'],2)
        imported=self.request(base)
        self.assertTrue(imported['sealed'])
        self.assertEqual(imported['status'],'paused')
        self.assertEqual(imported['totalItems'],3)
        initial_items=self.request(base+'/items')['items']
        original_ids={item['targetId']:item['id'] for item in initial_items}
        self.assertEqual(len(original_ids),3)
        self.assertTrue(all(item['stage']=='waiting_input' and item['attempt']==0 for item in initial_items))
        self.request(base+'/resume',{})
        wait_for(lambda:self.app.pipeline.preparation_resources.snapshot()['waiting']==3)
        blocked=self.request('/api/pipeline/config')['preparationResources']
        self.assertIn('disk_free',{reason['code'] for reason in blocked['blockedReasons']})
        self.assertEqual(blocked['inflight']['count'],0)
        blocked_items=self.request(base+'/items')['items']
        self.assertTrue(all(item['stage']=='waiting_input' and item['attempt']==0 for item in blocked_items))
        self.assertEqual(self.searches(),[])

        # Explicit synthetic sensor recovery. Actual host disk capacity is never
        # interpreted as a benchmark or changed by the test.
        self.disk_sensor['free']=10**9
        wait_for(lambda:self.request(base)['counts']['ready']==3)
        campaign=self.request(base)
        self.assertEqual(campaign['counts']['failed'],0)
        self.assertEqual(campaign['counts']['predicting'],0)
        self.assertEqual(campaign['counts']['completed'],0)
        self.assertFalse(campaign['isComplete'])
        self.assertCountEqual(self.searches(),['AG','VC','LK','FT','DE'])
        metrics=campaign['metrics']['preparationReuse']
        self.assertEqual(metrics['targets'],3)
        self.assertEqual(metrics['newChainSearches'],5)
        self.assertEqual(metrics['chainCacheHits'],4)
        self.assertEqual(metrics['totalChains'],9)
        self.assertEqual(metrics['uniqueChainOccurrences'],9)
        self.assertEqual(metrics['duplicateChainCopies'],0)
        self.assertEqual(metrics['wholeTargetCacheHits'],0)
        self.assertTrue(self.samples)
        self.assertEqual(max(count for count,cpu in self.samples),1)
        self.assertTrue(all(count<=1 and cpu<=32 for count,cpu in self.samples))
        self.assertFalse(self.monitor_errors)
        config=self.request('/api/pipeline/config')
        self.assertEqual(config['prepWorkers'],3)
        self.assertEqual(config['predictionMode'],'workers')
        self.assertFalse(any(thread.name.startswith('pipeline-predict') for thread in self.app.pipeline.threads))
        self.gpu_mock.assert_not_called();self.predict_mock.assert_not_called()

        prepared_inputs=[]
        summaries=self.request(base+'/items')['items']
        for item in summaries:
            detail=self.request(base+'/items/'+item['id'])
            self.assertEqual(detail['stage'],'ready')
            self.assertIsNone(detail['result'])
            chains=detail['chains']
            self.assertEqual([c['id'] for c in chains],['A','B','C'])
            self.assertEqual([c['sequence'] for c in chains[:2]],['AG','VC'])
            self.assertIn(chains[2]['sequence'],['LK','FT','DE'])
            self.assertEqual(detail['preparationReuse']['totalChains'],3)
            self.assertEqual(detail['preparationReuse']['uniqueChains'],3)
            self.assertNotIn('entries',item['preparationReuse'])
            internal=self.app.pipeline.store.item(cid,item['id'],internal=True)
            descriptor=json.loads(Path(internal['prepared_path']).read_text())
            self.assertEqual(descriptor['chainReuse'],detail['preparationReuse'])
            self.assertEqual([entry['chainIds'] for entry in descriptor['chainReuse']['entries']],[['A'],['B'],['C']])
            prepared_inputs.append((descriptor,chains))
        self.assertEqual(sorted(item['preparationReuse']['newChainSearches'] for item in summaries),[1,1,3])

        before_searches=list(self.searches())
        before_metrics=copy.deepcopy(metrics)
        replay=self.cli(cid)
        self.assertEqual((replay['inserted'],replay['duplicates'],replay['acknowledged']),(0,3,3))
        self.assertFalse(replay['running'])
        self.assertEqual(self.searches(),before_searches)
        after=self.request(base)
        self.assertEqual(after['status'],'paused')
        self.assertEqual(after['totalItems'],3)
        self.assertEqual(after['metrics']['preparationReuse'],before_metrics)
        after_items=self.request(base+'/items')['items']
        self.assertEqual({item['targetId']:item['id'] for item in after_items},original_ids)
        self.assertTrue(all(item['attempt']==1 and item['stage']=='ready' for item in after_items))
        journal=[json.loads(line) for line in self.manifest.read_text().splitlines()]
        self.assertEqual([record['duplicates'] for record in journal if record['event']=='import-complete'],[0,3])

        # Every complete prepared target carries its own inline MSA/templates.
        # Removing the original whole/leaf caches after shutdown proves the copy
        # has no runtime dependency on canonical leaf paths or the controller DB.
        copies=[]
        for index,(descriptor,chains) in enumerate(prepared_inputs):
            remote=self.root/'isolated-prepared-inputs'/str(index)
            shutil.copytree(descriptor['cacheDirectory'],remote)
            copies.append((remote,descriptor['cacheKey'],chains))
        self._stop_monitor()
        self.app.close()
        shutil.rmtree(self.app.pipeline.cache_root)
        for remote,key,chains in copies:
            manifest=staged._manifest(remote,key)
            data=staged._validate_af3(remote/manifest['inputRelativePath'],chains)
            self.assertEqual(data['version'],4)
            self.assertEqual(len(data['sequences']),3)
            for entity in data['sequences']:
                protein=entity['protein']
                self.assertIn('TaxID=9606',protein['pairedMsa'])
                self.assertEqual(protein['templates'][0]['mmcif'],'data_fixture\n# inline exact text\n')
        self.gpu_mock.assert_not_called();self.predict_mock.assert_not_called()
        LOG.info('Screening fixture passed: real CLI/HTTP/SQLite, 3 complete targets, 5 synthetic CPU searches, '
                 '4 chain hits, peak 1 preparation reservation, duplicate replay 3; native AF3/GPU not exercised.')


if __name__=='__main__':unittest.main()
