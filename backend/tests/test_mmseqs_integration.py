"""Real adapter subprocess + cache/ledger/locks; synthetic CF tool, no GPU search.

The tool writes controlled A3M text. Device names are test lock identities only;
these tests establish isolation, argv, AF3 conversion and recovery contracts.
"""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend import databases, staged
from backend.cache_access import shared_cache
from backend.gpu_access import gpu_group_lease, try_gpu_group
from backend.preparation_control import PreparationDeferred
from backend.preparation_resources import search_gpu_configuration
from backend.worker_pipeline import PipelineScheduler

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT/'scripts/mmseqs_af3_runner.py'
CF = r'''
import json,os,sys
from pathlib import Path
source,db,out=map(Path,sys.argv[1:4]);out.mkdir(parents=True)
seq=''.join(source.read_text().splitlines()[1:])
with Path(os.environ['CF_TEST_RECORD']).open('a') as stream:
    stream.write(json.dumps({'argv':sys.argv[1:],'devices':os.environ.get('CUDA_VISIBLE_DEVICES')})+'\n')
if os.environ.get('CF_TEST_FAIL'):raise SystemExit(3)
(out/'protein.a3m').write_text('>query original\n'+seq+'\n>unpaired raw hit\n'+seq[0]+'a'+seq[1:]+'\n')
'''


class MMseqsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.record = self.root/'tools.jsonl'
        self.cf = self.root/'colabfold_search'
        self.cf.write_text('#!'+sys.executable+'\n'+CF)
        self.cf.chmod(0o700)
        self.env = patch.dict(os.environ, {'PROTEIN_STUDIO_PREDICTION_MODE':'workers',
            'PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND':'', 'PROTEIN_STUDIO_ALPHAFOLD3_FINALIZE_COMMAND':'',
            'PROTEIN_STUDIO_SEARCH_GPU_GROUPS_JSON':'', 'PROTEIN_STUDIO_GPU_GROUPS_JSON':'',
            'PROTEIN_STUDIO_GPU_DEVICES':'', 'PROTEIN_STUDIO_GPU_LOCK_DIR':str(self.root/'gpu-locks'),
            'CF_TEST_RECORD':str(self.record), 'CF_TEST_FAIL':''})
        self.env.start();self.addCleanup(self.env.stop)
        suffixes=['','.dbtype','.index','_seq','_seq.dbtype','_seq.index','_aln','_aln.dbtype','_aln.index']
        for suffix in suffixes:(self.root/('db'+suffix)).write_bytes(b'fake')
        self.raw = dict(engine='alphafold3', validationProfile='af3-mmseqs-v1',root=str(self.root),
            release='synthetic-db-v1', prepareVersion='synthetic-wrapper-v1', searchVersion='synthetic-MMseqs-v1',
            searchBackend=dict(id='mmseqs2-colabfold',device='cpu',version='synthetic-mmseqs',colabfoldVersion='1.6.3',
                adapterVersion='mmseqs-af3-v1',databaseFormat='colabfold-expandable',gpuServer=False,
                pairingStrategy='none',templateStrategy='none'),
            assets=[dict(name='uniref30_db',kind='sequence',type='prefix',path='db',requiredSuffixes=suffixes)],
            executables={'colabfold_search_binary':str(self.cf),'mmseqs_binary':sys.executable},
            searchParameters={'threads':1,'use_env':0}, templateParameters={})
        for phase,name in [('search','prepareCommand'),('finalize','finalizeCommand')]:
            self.raw[name]=[sys.executable,str(RUNNER),'--phase',phase,'--input','{input_json}',
                            '--profile-json','{profile_json}','--output-dir','{output_dir}']

    def profile(self):
        return databases.get_profile('test', {'profiles':{'test':self.raw}})

    def job(self, *sequences):
        return dict(name='target',engine='alphafold3',chains=[{'id':chr(65+i),'sequence':s} for i,s in enumerate(sequences or ('AG',))],
                    seeds=[1],numSamples=1,numRecycles=10,databaseProfile=self.profile(),cacheRoot=str(self.root/'cache'))

    def records(self):
        return [json.loads(x) for x in self.record.read_text().splitlines()] if self.record.exists() else []

    def use_gpu(self):
        self.raw['searchBackend'].update(device='gpu', databaseFormat='colabfold-gpu')
        os.environ['PROTEIN_STUDIO_SEARCH_GPU_GROUPS_JSON']='[["GPU-search-fixture"]]'

    def test_cpu_profile_runs_actual_two_phase_adapter_and_reuses_chain_with_explicit_policy(self):
        profile=self.profile()
        self.assertTrue(profile['ready'])
        self.assertEqual(profile['preparationStrategy']['verification'],'metadata-and-path-preflight')
        cap=next(x for x in staged.staged_capabilities(profile) if x['engine']=='alphafold3')
        self.assertTrue(cap['available'])
        first=staged.prepare_stage(self.job('AG','VC'),self.root/'one',lambda *a:None,lambda:False)
        second=staged.prepare_stage(self.job('AG','LK'),self.root/'two',lambda *a:None,lambda:False)
        self.assertEqual(len(self.records()),3)
        self.assertTrue(all(r['devices']=='' for r in self.records()))
        result=json.loads(Path(second['inputPath']).read_text())
        for entity in result['sequences']:
            self.assertEqual(entity['protein']['pairedMsa'],'')
            self.assertEqual(entity['protein']['templates'],[])
            self.assertIn('>unpaired raw hit',entity['protein']['unpairedMsa'])
        self.assertEqual(second['chainReuse']['chainCacheHits'],1)
        entry=first['chainReuse']['entries'][0]
        self.assertEqual(entry['searchBackend']['pairingStrategy'],'none')
        self.assertGreater(entry['msaSearchProcessWallSeconds'],0)
        self.assertGreater(entry['cpuFinalizeProcessWallSeconds'],0)
        self.assertAlmostEqual(entry['searchProcessWallSeconds'],entry['msaSearchProcessWallSeconds']+entry['cpuFinalizeProcessWallSeconds'])

    def test_gpu_search_sees_only_dedicated_group_and_releases_it_before_cpu_finalization(self):
        self.use_gpu()
        wrapper=self.root/'finalize.py'
        wrapper.write_text('import os,sys\nsys.path.insert(0,'+repr(str(ROOT))+')\n'
            'from backend.gpu_access import try_gpu_group\n'
            'assert os.environ.get("CUDA_VISIBLE_DEVICES")==""\n'
            'with try_gpu_group([["GPU-search-fixture"]]):pass\n'
            'os.execv(sys.executable,[sys.executable]+sys.argv[1:])\n')
        self.raw['finalizeCommand']=[sys.executable,str(wrapper)]+self.raw['finalizeCommand'][1:]
        result=staged.prepare_cooperative(self.job(),self.root/'gpu',lambda *a:None,lambda:False)
        self.assertEqual(self.records()[0]['devices'],'GPU-search-fixture')
        self.assertEqual(result['chainReuse']['entries'][0]['searchBackend']['device'],'gpu')
        with try_gpu_group([['GPU-search-fixture']]):pass
        with patch('backend.gpu_access.try_gpu_group',side_effect=AssertionError('Cache hit leased GPU')):
            hit=staged.prepare_cooperative(self.job(),self.root/'hit',lambda *a:None,lambda:False)
        self.assertTrue(hit['cacheHit'])
        self.assertEqual(len(self.records()),1)

    def test_busy_gpu_defers_fenced_pipeline_item_and_wait_reason_is_public(self):
        self.use_gpu()
        scheduler=PipelineScheduler(self.root,preparation_resources={})
        self.addCleanup(scheduler.close)
        c=scheduler.store.create(dict(name='gpu search',engine='alphafold3',status='active',
                                     databaseProfile=self.profile(),settings={},executionProfile={}))
        item=scheduler.store.append(c['id'],[dict(targetId='x',submission=self.job())])['items'][0]
        claim=scheduler.store.claim('prepare')
        with patch.object(scheduler,'_check_database_snapshot',lambda job:None):
            with gpu_group_lease(['GPU-search-fixture']),shared_cache(scheduler.cache_root):
                scheduler._execute_claim(claim,'prepare')
        public=scheduler.store.item(c['id'],item['id'])
        self.assertEqual(public['stage'],'waiting_input')
        self.assertEqual(public['preparationProgress']['waitReason'],'search_gpu')
        self.assertIsNone(public['error'])
        self.assertEqual(self.records(),[])
        self.assertFalse(scheduler.store.advance(item['id'],claim['token'],'failed',error='stale'))

    def test_gpu_configuration_missing_overlapping_or_unmanaged_server_fails_closed(self):
        self.use_gpu()
        os.environ['PROTEIN_STUDIO_SEARCH_GPU_GROUPS_JSON']=''
        with self.assertRaisesRegex(ValueError,'SEARCH_GPU'):
            staged.prepare_cooperative(self.job(),self.root/'missing',lambda *a:None,lambda:False)
        os.environ['PROTEIN_STUDIO_SEARCH_GPU_GROUPS_JSON']='[["GPU-search-fixture"]]'
        with patch.dict(os.environ,{'PROTEIN_STUDIO_GPU_GROUPS_JSON':'[["GPU-search-fixture"]]'}):
            with self.assertRaisesRegex(ValueError,'disjoint'):search_gpu_configuration(require=True)
        with patch.dict(os.environ,{'PROTEIN_STUDIO_GPU_LOCK_DIR':''}):
            with self.assertRaisesRegex(ValueError,'GPU_LOCK_DIR'):search_gpu_configuration(require=True)
        self.raw['searchBackend']['gpuServer']=True
        self.assertFalse(self.profile()['ready'])
        self.assertIn('unmanaged-gpu-server',{x['code'] for x in self.profile()['diagnostics']})
        self.assertEqual(self.records(),[])

    def test_search_failure_releases_gpu_and_publishes_no_complete_cache(self):
        self.use_gpu()
        with patch.dict(os.environ,{'CF_TEST_FAIL':'1'}),self.assertRaises(RuntimeError):
            staged.prepare_cooperative(self.job(),self.root/'failed',lambda *a:None,lambda:False)
        with try_gpu_group([['GPU-search-fixture']]):pass
        self.assertEqual(list((self.root/'cache').rglob('COMPLETE.json')),[])

    def test_every_scientific_backend_identity_and_finalize_command_changes_cache_key(self):
        baseline=staged.prepare_cache_key(self.job())
        changes=[('version','different-mmseqs'),('pairingStrategy','native-uniprot'),('templateStrategy','native-af3')]
        for field,value in changes:
            job=self.job();job['databaseProfile']['searchBackend'][field]=value
            self.assertNotEqual(baseline,staged.prepare_cache_key(job),field)
        for field in ('prepareCommand','finalizeCommand'):
            job=self.job();job['databaseProfile'][field]=job['databaseProfile'][field]+['extra-version-marker']
            self.assertNotEqual(baseline,staged.prepare_cache_key(job),field)
        job=self.job();job['databaseProfile']['searchParameters']['threads']=2
        self.assertNotEqual(baseline,staged.prepare_cache_key(job))
        self.use_gpu()
        self.assertNotEqual(baseline,staged.prepare_cache_key(self.job()))

    def test_native_policies_require_their_own_databases_and_tools(self):
        self.raw['searchBackend'].update(pairingStrategy='native-uniprot',templateStrategy='native-af3')
        profile=self.profile()
        self.assertFalse(profile['ready'])
        diagnostics=profile['diagnostics']
        self.assertTrue({'uniprot_cluster_annot_database_path','seqres_database_path','pdb_database_path'}.issubset(
            {d.get('asset') for d in diagnostics}))
        self.assertTrue({'af3_python','jackhmmer_binary_path','hmmsearch_binary_path','hmmbuild_binary_path'}.issubset(
            {d.get('field') for d in diagnostics}))

    def test_bad_backend_family_commands_and_gpu_parameters_are_not_ready(self):
        self.raw['assets'][0]['requiredSuffixes']=['','.dbtype']
        self.assertFalse(self.profile()['ready'])
        self.raw['prepareCommand'][0]='/missing/python'
        self.assertFalse(self.profile()['preparationStrategy']['runnerConfigured'])
        self.use_gpu();self.raw['searchParameters']['s']=8
        self.assertIn('mmseqs-gpu-parameters',{x['code'] for x in self.profile()['diagnostics']})
        self.raw['searchBackend']['colabfoldVersion']='0.0'
        with self.assertRaisesRegex(ValueError,'ColabFold'):self.profile()
