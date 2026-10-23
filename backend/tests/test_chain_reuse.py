"""Real CPU subprocess/cache contracts; synthetic searches, no AF3/GPU claim."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from backend import engine
from backend.staged import (CHAIN_NAMESPACE, prepare_stage, prepare_cache_key,
                            _manifest, _validate_af3)

RUNNER = r'''
import json,os,sys,time
from pathlib import Path
source,output=map(Path,sys.argv[1:3]);output.mkdir(parents=True,exist_ok=True)
assert os.environ.get('CUDA_VISIBLE_DEVICES')==''
data=json.loads(source.read_text())
assert len(data['sequences'])==1
p=data['sequences'][0]['protein'];assert p['id']=='A'
seq=p['sequence']
fd=os.open(os.environ['CHAIN_COUNTER'],os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
os.write(fd,(seq+'\n').encode());os.close(fd)
if os.environ.get('CHAIN_SLEEP_SEQ')==seq:time.sleep(20)
time.sleep(float(os.environ.get('CHAIN_DELAY','0')))
if os.environ.get('CHAIN_FAIL_SEQ')==seq:raise SystemExit(4)
msa='>query original header\n'+seq+'\n>hit TaxID=9606\n'+seq[0]+'aa'+seq[1:]+'\n>hit TaxID=9606\n'+'-'*(len(seq)-1)+seq[-1]+'\n'
p.update(id=['A'],unpairedMsa=msa,pairedMsa=msa,templates=[dict(mmcif='data_fixture\n# inline exact text\n',queryIndices=[0],templateIndices=[2])],description='unchanged description',modifications=[])
data['version']=4
if os.environ.get('CHAIN_BAD')=='query':p['pairedMsa']='>bad\n'+'W'*len(seq)+'\n'
if os.environ.get('CHAIN_BAD')=='width':p['unpairedMsa']+='>broken\nA\n'
if os.environ.get('CHAIN_BAD')=='ptm':p['modifications']=[{'ptmType':'SEP','ptmPosition':1}]
if os.environ.get('CHAIN_BAD')=='ccd':data['userCCD']='data_extra\n'
if os.environ.get('CHAIN_BAD')=='bonds':data['bondedAtomPairs']=[[['A',1,'CA'],['A',2,'CA']]]
if os.environ.get('CHAIN_BAD')=='extra-entity':
    data['sequences'].append({'protein':dict(p,id=[])})
if os.environ.get('CHAIN_BAD')=='empty':p.update(unpairedMsa='',pairedMsa='')
(output/'protein_data.json').write_text(json.dumps(data))
'''


class ChainReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.counter = self.root / 'searches.txt'
        self.script = self.root / 'runner.py'
        self.script.write_text(RUNNER)
        self.command = [sys.executable, str(self.script), '{input_json}', '{output_dir}']
        self.env = patch.dict(os.environ, {
            'PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND': json.dumps(self.command),
            'CHAIN_COUNTER': str(self.counter), 'CHAIN_DELAY': '0', 'CHAIN_BAD': '',
            'CHAIN_FAIL_SEQ': '', 'CHAIN_SLEEP_SEQ': '',
        })
        self.env.start()
        self.index = 0

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def job(self, *sequences, ids=None):
        return dict(engine='alphafold3', name='target',
                    chains=[dict(id=(ids[i] if ids else chr(65+i)), sequence=seq)
                            for i, seq in enumerate(sequences)], seeds=[1, 9], numSamples=2,
                    cacheRoot=str(self.root / 'cache'),
                    databaseProfile=dict(id='synthetic', engine='alphafold3', ready=True,
                        root='/never-a-real-database', profileFingerprint='fixture-database-v1',
                        prepareVersion='synthetic-runner-v1', searchVersion='synthetic-search-v1',
                        searchParameters={'threads': 1}, templateParameters={'date':'2021-09-30'}))

    def run_prepare(self, job, cancelled=lambda: False):
        self.index += 1
        return prepare_stage(job, self.root / f'attempt-{self.index}', lambda *args: None, cancelled)

    def searches(self):
        return self.counter.read_text().splitlines() if self.counter.exists() else []

    def test_fixed_partner_across_two_targets_performs_three_searches(self):
        first = self.run_prepare(self.job('AG', 'VC'))
        second = self.run_prepare(self.job('AG', 'LK'))
        self.assertEqual(self.searches(), ['AG', 'VC', 'LK'])
        self.assertEqual(first['chainReuse']['newChainSearches'], 2)
        self.assertEqual(second['chainReuse']['newChainSearches'], 1)
        self.assertEqual(second['chainReuse']['chainCacheHits'], 1)
        self.assertEqual(second['chainReuse']['entries'][0]['chainIds'], ['A'])

    def test_two_by_three_combinations_perform_five_searches(self):
        records = [self.run_prepare(self.job(left, right))
                   for left in ('AG', 'VC') for right in ('LK', 'FT', 'DE')]
        self.assertCountEqual(self.searches(), ['AG', 'VC', 'LK', 'FT', 'DE'])
        self.assertEqual(sum(x['chainReuse']['newChainSearches'] for x in records), 5)
        self.assertEqual(sum(x['chainReuse']['chainCacheHits'] for x in records), 7)
        self.assertTrue(all(x['chainReuse']['uniqueChains'] == 2 for x in records))

    def test_homomer_remaps_ids_and_preserves_every_msa_and_template_value(self):
        job = self.job('AG', 'VC', 'AG', ids=['C', 'F', 'Z'])
        prepared = self.run_prepare(job)
        data = _validate_af3(Path(prepared['inputPath']), job['chains'])
        self.assertEqual(self.searches(), ['AG', 'VC'])
        self.assertEqual(data['version'], 4)
        self.assertEqual([x['protein']['id'] for x in data['sequences']], ['C', 'F', 'Z'])
        self.assertEqual(prepared['chainReuse']['duplicateChainCopies'], 1)
        self.assertEqual(prepared['chainReuse']['entries'][0]['chainIds'], ['C', 'Z'])
        entry = prepared['chainReuse']['entries'][0]
        leaf_root = self.root/'cache'/CHAIN_NAMESPACE/entry['cacheKey']
        leaf_manifest = _manifest(leaf_root, entry['cacheKey'])
        leaf = json.loads((leaf_root/leaf_manifest['inputRelativePath']).read_text())['sequences'][0]['protein']
        for index in (0, 2):
            actual = copy.deepcopy(data['sequences'][index]['protein'])
            actual['id'] = ['A']
            self.assertEqual(actual, leaf)
        other = self.run_prepare(self.job('AG', 'VC', 'AG', ids=['H', 'J', 'Q']))
        self.assertFalse(other['cacheHit'])
        self.assertEqual(other['chainReuse']['newChainSearches'], 0)
        self.assertEqual(other['chainReuse']['chainCacheHits'], 2)

    def test_single_chain_has_separate_namespace_and_does_not_recurse(self):
        with patch.object(engine, '_gpu_lease', side_effect=AssertionError('CPU preparation must not lease GPU')):
            prepared = self.run_prepare(self.job('AG'))
        self.assertEqual(self.searches(), ['AG'])
        entry = prepared['chainReuse']['entries'][0]
        self.assertNotEqual(entry['cacheKey'], prepared['cacheKey'])
        self.assertTrue((self.root/'cache'/CHAIN_NAMESPACE/entry['cacheKey']/'COMPLETE.json').is_file())
        self.assertEqual(prepared['chainReuse']['uniqueChains'], 1)

    def test_sampling_and_ids_do_not_invalidate_leaf_identity(self):
        first = self.run_prepare(self.job('AG', 'VC'))
        job = self.job('AG', 'VC', ids=['X', 'Y'])
        job.update(seeds=[12], numSamples=5, numRecycles=7)
        second = self.run_prepare(job)
        self.assertEqual(self.searches(), ['AG', 'VC'])
        self.assertNotEqual(first['cacheKey'], second['cacheKey'])
        self.assertEqual([x['cacheKey'] for x in first['chainReuse']['entries']],
                         [x['cacheKey'] for x in second['chainReuse']['entries']])

    def test_preparation_identity_changes_require_new_search(self):
        baseline = self.job('AG')
        self.run_prepare(baseline)
        mutations = [lambda p:p.update(profileFingerprint='new-database'),
                     lambda p:p.update(prepareVersion='new-runner'),
                     lambda p:p.update(searchVersion='new-search'),
                     lambda p:p.update(searchParameters={'threads':2}),
                     lambda p:p.update(templateParameters={'date':'2024-01-01'}),
                     lambda p:p.update(executables={'jackhmmer_binary_path':'/new/jackhmmer'})]
        for mutate in mutations:
            job = copy.deepcopy(baseline)
            mutate(job['databaseProfile'])
            with self.subTest(profile=job['databaseProfile']):
                self.assertEqual(self.run_prepare(job)['chainReuse']['newChainSearches'], 1)
        changed = copy.deepcopy(baseline)
        changed['executionProfile'] = {'preparation': {'revision':'next'}}
        self.run_prepare(changed)
        with patch.dict(os.environ, {'PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND':json.dumps(self.command+['runner-v2'])}):
            self.run_prepare(baseline)
        self.assertEqual(len(self.searches()), 9)

    def test_different_targets_concurrently_share_one_chain_search(self):
        jobs = [self.job('AG', 'VC'), self.job('AG', 'LK')]
        with patch.dict(os.environ, {'CHAIN_DELAY':'0.15'}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(prepare_stage, job, self.root/f'parallel-{i}', lambda *a:None, lambda:False)
                           for i,job in enumerate(jobs)]
                results = [f.result(timeout=15) for f in futures]
        self.assertCountEqual(self.searches(), ['AG', 'VC', 'LK'])
        self.assertEqual(sum(x['chainReuse']['newChainSearches'] for x in results), 3)
        self.assertEqual(sum(x['chainReuse']['chainCacheHits'] for x in results), 1)

    def test_cross_process_opposite_chain_order_has_no_deadlock_or_duplicate_search(self):
        code = ('import json,sys;from pathlib import Path;from backend.staged import prepare_stage;'
                'prepare_stage(json.loads(sys.argv[1]),Path(sys.argv[2]),lambda *a:None,lambda:False)')
        jobs = [self.job('AG', 'VC'), self.job('VC', 'AG')]
        with patch.dict(os.environ, {'CHAIN_DELAY':'0.15'}):
            children = [subprocess.Popen([sys.executable, '-c', code, json.dumps(job), str(self.root/f'process-{i}')],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                        for i,job in enumerate(jobs)]
            try:
                for child in children:
                    output,error = child.communicate(timeout=15)
                    self.assertEqual(child.returncode, 0, error)
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill();child.wait()
        self.assertCountEqual(self.searches(), ['AG', 'VC'])

    def test_failure_does_not_publish_incomplete_leaf_or_whole_and_retry_reuses_good_leaf(self):
        job = self.job('AG', 'VC')
        with patch.dict(os.environ, {'CHAIN_FAIL_SEQ':'VC'}):
            with self.assertRaises(RuntimeError):
                self.run_prepare(job)
        self.assertFalse((self.root/'cache'/prepare_cache_key(job)).exists())
        self.assertEqual(len(list((self.root/'cache'/CHAIN_NAMESPACE).glob('*/COMPLETE.json'))), 1)
        self.assertFalse(list((self.root/'cache').rglob('*.partial-*')))
        result = self.run_prepare(job)
        self.assertEqual(self.searches(), ['AG', 'VC', 'VC'])
        self.assertEqual(result['chainReuse']['chainCacheHits'], 1)

    def test_cancellation_does_not_publish_current_leaf_or_whole(self):
        job = self.job('AG', 'VC')
        with patch.dict(os.environ, {'CHAIN_SLEEP_SEQ':'VC'}):
            with self.assertRaises(engine.PredictionCancelled):
                self.run_prepare(job, lambda: 'VC' in self.searches())
        self.assertFalse((self.root/'cache'/prepare_cache_key(job)).exists())
        self.assertEqual(len(list((self.root/'cache'/CHAIN_NAMESPACE).glob('*/COMPLETE.json'))), 1)
        self.assertFalse(list((self.root/'cache').rglob('*.partial-*')))
        result = self.run_prepare(job)
        self.assertEqual(result['chainReuse']['chainCacheHits'], 1)
        self.assertEqual(self.searches(), ['AG', 'VC', 'VC'])

    def test_corrupted_shared_leaf_fails_closed_without_research(self):
        first = self.run_prepare(self.job('AG', 'VC'))
        key = first['chainReuse']['entries'][0]['cacheKey']
        leaf_root = self.root/'cache'/CHAIN_NAMESPACE/key
        manifest = _manifest(leaf_root,key)
        path = leaf_root/manifest['inputRelativePath']
        path.write_text(path.read_text()+' ')
        new = self.job('AG', 'LK')
        with self.assertRaisesRegex(ValueError, 'modified'):
            self.run_prepare(new)
        self.assertEqual(self.searches(), ['AG', 'VC'])
        self.assertFalse((self.root/'cache'/prepare_cache_key(new)).exists())

    def test_whole_hit_uses_zero_operation_counters_without_touching_leaf(self):
        job = self.job('AG', 'AG', 'VC')
        first = self.run_prepare(job)
        original_manifest = Path(first['manifestPath']).read_bytes()
        shutil.rmtree(self.root/'cache'/CHAIN_NAMESPACE)
        with patch('backend.staged._prepare_af3_chain', side_effect=AssertionError('Whole hit must not visit leaf cache')):
            hit = self.run_prepare(job)
        self.assertTrue(hit['cacheHit'])
        stats = hit['chainReuse']
        self.assertEqual((stats['newChainSearches'],stats['chainCacheHits']), (0,0))
        self.assertTrue(stats['wholeTargetCacheHit'])
        self.assertEqual(stats['duplicateChainCopies'], 1)
        self.assertEqual(stats['entries'], first['chainReuse']['entries'])
        self.assertEqual(Path(first['manifestPath']).read_bytes(), original_manifest)

    def test_legacy_whole_cache_without_chain_stats_is_reused_unchanged(self):
        job = self.job('AG')
        prepared = self.run_prepare(job)
        path = Path(prepared['manifestPath'])
        manifest = json.loads(path.read_text())
        manifest.pop('chainReuse')
        path.write_text(json.dumps(manifest))
        shutil.rmtree(self.root/'cache'/CHAIN_NAMESPACE)
        with patch('backend.staged._prepare_af3_chain', side_effect=AssertionError('Legacy whole hit must not visit leaves')):
            hit = self.run_prepare(job)
        self.assertTrue(hit['cacheHit'])
        self.assertNotIn('chainReuse', hit)
        self.assertEqual(self.searches(), ['AG'])

    def test_whole_bundle_is_relocatable_without_controller_or_leaf_cache(self):
        from backend.distributed import safe_extract
        job = self.job('AG', 'VC', 'AG')
        prepared = self.run_prepare(job)
        cache = Path(prepared['cacheDirectory'])
        bundle = self.root/'prepared.zip'
        with zipfile.ZipFile(bundle,'w') as archive:
            for path in cache.rglob('*'):
                if path.is_file():archive.write(path,path.relative_to(cache))
        remote = self.root/'worker'
        safe_extract(bundle,remote)
        shutil.rmtree(self.root/'cache')
        manifest = _manifest(remote,prepared['cacheKey'])
        data = _validate_af3(remote/manifest['inputRelativePath'],job['chains'])
        self.assertEqual(data['version'],4)
        self.assertEqual([p['protein']['id'] for p in data['sequences']], ['A','B','C'])
        self.assertIn('TaxID=9606',data['sequences'][0]['protein']['pairedMsa'])
        self.assertEqual(data['sequences'][0]['protein']['templates'][0]['mmcif'],'data_fixture\n# inline exact text\n')

    def test_bad_query_width_or_injected_chemistry_never_publishes(self):
        for bad in ('query','width','ptm','ccd','bonds','extra-entity'):
            with self.subTest(mode=bad):
                job = self.job('AG')
                job['cacheRoot'] = str(self.root/f'bad-{bad}')
                with patch.dict(os.environ, {'CHAIN_BAD':bad}):
                    with self.assertRaises(ValueError):
                        self.run_prepare(job)
                self.assertFalse(list(Path(job['cacheRoot']).rglob('COMPLETE.json')))

    def test_explicit_empty_msas_are_preserved(self):
        with patch.dict(os.environ, {'CHAIN_BAD':'empty'}):
            prepared = self.run_prepare(self.job('AG'))
        p = json.loads(Path(prepared['inputPath']).read_text())['sequences'][0]['protein']
        self.assertEqual(p['unpairedMsa'], '')
        self.assertEqual(p['pairedMsa'], '')


if __name__ == '__main__':
    unittest.main()
