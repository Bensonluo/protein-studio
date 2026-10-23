"""Real subprocess stage/cache tests with synthetic outputs, no model/database GPU."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import shutil
import zipfile
from unittest.mock import patch

from backend import engine
from backend.staged import (prepare_stage, predict_stage, prepare_cache_key,
                            staged_capabilities, _validate_a3m, _validate_af3, _manifest)

FAKE = r'''
import json,os,sys,time
from pathlib import Path
stage,engine,source,out,jobfile=sys.argv[1:6]
source=Path(source);out=Path(out);out.mkdir(parents=True,exist_ok=True)
job=json.loads(Path(jobfile).read_text())
if stage=='prepare':
    assert os.environ.get('CUDA_VISIBLE_DEVICES')==''
    with open(os.environ['TEST_PREP_COUNT'],'a') as f:f.write('prepare\n')
    Path(os.environ['TEST_PREP_STARTED']).write_text('yes')
    if os.environ.get('TEST_PREP_SLEEP'):time.sleep(30)
    if os.environ.get('TEST_PREP_FAIL'):raise SystemExit(4)
    if engine=='alphafold3':
        data=json.loads(source.read_text())
        for item in data['sequences']:
            p=item['protein'];p.update(unpairedMsa='>query\n'+p['sequence']+'\n',pairedMsa='',templates=[])
            if os.environ.get('TEST_INLINE_TEMPLATE'):
                p['templates']=[dict(mmcif='data_fixture\n_atom_site.group_PDB ATOM\n',queryIndices=[0],templateIndices=[0])]
        if os.environ.get('TEST_BAD_PREP'):data['sequences'][0]['protein'].pop('pairedMsa')
        (out/'protein_data.json').write_text(json.dumps(data))
    else:
        (out/'protein.a3m').write_text('#2\t1\n>protein\nAG\n>hit\nAG\n')
else:
    (out/'devices.txt').write_text(os.environ.get('CUDA_VISIBLE_DEVICES','inherited'))
    if engine=='alphafold3':
        prepared=json.loads(source.read_text())
        assert prepared['modelSeeds']==job['seeds']
        assert prepared['sequences'][0]['protein']['unpairedMsa'].startswith('>query')
        pairs=[(seed,sample) for seed in job['seeds'] for sample in range(job['numSamples'])]
    else:
        assert list(source.glob('*.a3m'))
        pairs=[(int(sys.argv[6]),0)]
    def atom(n,aa,b):
        return f'ATOM  {n:5d}  CA  {aa:>3s} A{n:4d}    {float(n):8.3f}{0:8.3f}{0:8.3f}{1:6.2f}{b:6.2f}           C  \n'
    text=atom(1,'ALA',90)+atom(2,'GLY',80)
    for seed,sample in pairs:
        if engine=='alphafold3':
            directory=out/'protein'/f'seed-{seed}_sample-{sample}';directory.mkdir(parents=True)
            (directory/'protein_model.pdb').write_text(text)
            (directory/'protein_summary_confidences.json').write_text(json.dumps({'ptm':.8,'ranking_score':.7}))
        else:
            (out/f'protein_unrelaxed_rank_001_alphafold2_ptm_model_1_seed_{seed:03d}.pdb').write_text(text)
'''


class StagedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script = self.root / 'fake.py'
        self.script.write_text(FAKE)
        self.count = self.root / 'count.txt'
        self.started = self.root / 'started.txt'
        env = {'TEST_PREP_COUNT': str(self.count), 'TEST_PREP_STARTED': str(self.started),
               'TEST_PREP_SLEEP': '', 'TEST_PREP_FAIL': '', 'TEST_BAD_PREP': '',
               'PROTEIN_STUDIO_GPU_GROUPS_JSON': '[["0"]]', 'PROTEIN_STUDIO_GPU_DEVICES': ''}
        for engine_name in ('alphafold3', 'colabfold'):
            prep_input = '{input_json}' if engine_name == 'alphafold3' else '{fasta}'
            pred_input = '{prepared_input}' if engine_name == 'alphafold3' else '{prepared_dir}'
            prefix = 'PROTEIN_STUDIO_' + engine_name.upper()
            env[prefix + '_PREPARE_COMMAND'] = json.dumps([sys.executable, str(self.script), 'prepare', engine_name, prep_input, '{output_dir}', '{job_json}'])
            env[prefix + '_PREDICT_COMMAND'] = json.dumps([sys.executable, str(self.script), 'predict', engine_name, pred_input, '{output_dir}', '{job_json}', '{seed}'])
        self.env = patch.dict(os.environ, env)
        self.env.start()
        self.events = []

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def emit(self, *args, **kwargs):
        self.events.append(args)

    def job(self, model='alphafold3'):
        return dict(engine=model, sequence='AG', seeds=[3, 9], numSamples=2,
                    cacheRoot=str(self.root / 'cache'), databaseProfile=dict(
                        id='test-local', engine=model, ready=True, root=str(self.root / 'db'),
                        profileFingerprint='test-fingerprint-1', prepareVersion='test-prepare-v1',
                        searchVersion='test-search-v1', searchParameters={'threads': 8},
                        templateParameters={'max_template_date': '2021-09-30'}))

    def test_public_reference_is_persistent_replay(self):
        job = engine.load_example('ubiquitin-afdb')
        job['cacheRoot'] = str(self.root / 'cache')
        with patch.object(engine, '_gpu_lease', side_effect=AssertionError('Reference must never lease GPU')):
            prepared = prepare_stage(job, self.root / 'prep', self.emit, lambda: False)
            hit = prepare_stage(job, self.root / 'prep-again', self.emit, lambda: False)
            result = predict_stage(job, json.loads(json.dumps(prepared)), self.root / 'pred', self.emit, lambda: False)
        self.assertTrue(hit['cacheHit'])
        self.assertFalse(prepared['cacheHit'])
        self.assertFalse(result['provenance']['newPrediction'])
        self.assertEqual(len(result['residues']), 229)
        self.assertEqual(len(result['pae']), 229)
        self.assertTrue(Path(result['structurePath']).is_relative_to((self.root / 'pred').resolve()))

    def test_cache_key_covers_preparation_inputs_but_not_sampling(self):
        job = self.job()
        key = prepare_cache_key(job)
        changed = copy.deepcopy(job)
        changed.update(seeds=[99], numSamples=1, numRecycles=20)
        self.assertEqual(key, prepare_cache_key(changed))
        mutations = [lambda j: j.update(chains=[dict(id='B', sequence='AG')]),
                     lambda j: j.update(sequence='AV'),
                     lambda j: j['databaseProfile'].update(profileFingerprint='new-db-release'),
                     lambda j: j['databaseProfile'].update(prepareVersion='new-prep-code'),
                     lambda j: j['databaseProfile'].update(executables={'jackhmmer_binary_path':'/different/hmmer'}),
                     lambda j: j['databaseProfile']['searchParameters'].update(threads=16),
                     lambda j: j['databaseProfile']['templateParameters'].update(max_template_date='2024-01-01')]
        for mutate in mutations:
            changed = copy.deepcopy(job)
            mutate(changed)
            self.assertNotEqual(key, prepare_cache_key(changed))
        with patch.dict(os.environ, {'PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND': json.dumps([sys.executable, str(self.script), 'different'])}):
            self.assertNotEqual(key, prepare_cache_key(job))

    def test_inline_templates_and_msa_remain_usable_after_zip_relocation(self):
        from backend.distributed import safe_extract
        with patch.dict(os.environ, {'TEST_INLINE_TEMPLATE':'1'}):
            prepared=prepare_stage(self.job(),self.root/'prep-template',self.emit,lambda:False)
        cache=Path(prepared['cacheDirectory'])
        bundle=self.root/'prepared.zip'
        with zipfile.ZipFile(bundle,'w') as archive:
            for path in cache.rglob('*'):
                if path.is_file(): archive.write(path,path.relative_to(cache))
        remote=self.root/'separate-worker-filesystem'
        safe_extract(bundle,remote)
        shutil.rmtree(cache)
        manifest=_manifest(remote,prepared['cacheKey'])
        data=_validate_af3(remote/manifest['inputRelativePath'],[dict(id='A',sequence='AG')])
        protein=data['sequences'][0]['protein']
        self.assertEqual(protein['unpairedMsa'],'>query\nAG\n')
        self.assertEqual(protein['templates'][0]['mmcif'],'data_fixture\n_atom_site.group_PDB ATOM\n')
        self.assertFalse(cache.exists())

    def test_af3_grouped_identical_chains_validate_by_identity_and_reject_bad_templates(self):
        def protein(ids,sequence):
            return dict(protein=dict(id=ids,sequence=sequence,unpairedMsa='',pairedMsa='',templates=[]))
        data=dict(dialect='alphafold3',version=4,sequences=[protein(['A','C'],'AG'),protein('B','VC')])
        path=self.root/'grouped_data.json'
        path.write_text(json.dumps(data))
        chains=[dict(id='A',sequence='AG'),dict(id='B',sequence='VC'),dict(id='C',sequence='AG')]
        self.assertEqual(_validate_af3(path,chains),data)
        data['sequences'][0]['protein']['templates']=[{'mmcifPath':'/controller/database/1abc.cif','queryIndices':[0],'templateIndices':[0]}]
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'inline'):
            _validate_af3(path,chains)
        data['sequences'][0]['protein']['templates']=[{'mmcif':'data_template\n','queryIndices':[2],'templateIndices':[0]}]
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'mappings'):
            _validate_af3(path,chains)

    def test_concurrent_preparation_runs_one_subprocess_without_gpu_lease(self):
        job = self.job()
        with patch.object(engine, '_gpu_lease', side_effect=AssertionError('Preparation must not lease a GPU')):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(prepare_stage, job, self.root / f'prep-{i}', self.emit, lambda: False) for i in range(2)]
                outputs = [future.result() for future in futures]
        self.assertEqual(self.count.read_text().splitlines(), ['prepare'])
        self.assertEqual(sorted(item['cacheHit'] for item in outputs), [False, True])
        self.assertEqual(outputs[0]['inputPath'], outputs[1]['inputPath'])

    def test_singleflight_and_completed_cache_survive_process_boundaries(self):
        jobfile = self.root / 'job.json'
        jobfile.write_text(json.dumps(self.job()))
        code = ('import json,sys; from pathlib import Path; '
                'from backend.staged import prepare_stage; '
                'r=prepare_stage(json.loads(Path(sys.argv[1]).read_text()),Path(sys.argv[2]),lambda *a:None,lambda:False); '
                'print(json.dumps(r))')
        def launch(number):
            completed = subprocess.run([sys.executable, '-c', code, str(jobfile), str(self.root / f'process-{number}')],
                                       cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=20)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(completed.stdout)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(launch, [1, 2]))
        self.assertEqual(sorted(item['cacheHit'] for item in results), [False, True])
        self.assertTrue(launch(3)['cacheHit'])
        self.assertEqual(self.count.read_text().splitlines(), ['prepare'])

    def test_af3_prediction_rewrites_seeds_without_mutating_shared_cache(self):
        job = self.job()
        prepared = prepare_stage(job, self.root / 'prep', self.emit, lambda: False)
        self.assertEqual(json.loads(Path(prepared['inputPath']).read_text())['modelSeeds'], [1])
        result = predict_stage(job, prepared, self.root / 'pred', self.emit, lambda: False)
        self.assertEqual(result['actualSampleCount'], 4)
        self.assertEqual(result['provenance']['assignedGpuGroup'], ['0'])
        self.assertEqual((self.root / 'pred/output/devices.txt').read_text(), '0')
        self.assertEqual(result['plddt'], [90, 80])
        self.assertEqual(json.loads(Path(prepared['inputPath']).read_text())['modelSeeds'], [1])
        copied = next((self.root / 'pred/prepared').rglob('*_data.json'))
        self.assertEqual(json.loads(copied.read_text())['modelSeeds'], [3, 9])
        next_job = copy.deepcopy(job)
        next_job['seeds'] = [5]
        hit = prepare_stage(next_job, self.root / 'prep-2', self.emit, lambda: False)
        self.assertTrue(hit['cacheHit'])
        second = predict_stage(next_job, hit, self.root / 'pred-2', self.emit, lambda: False)
        self.assertEqual(second['actualSampleCount'], 2)
        self.assertEqual(self.count.read_text().splitlines(), ['prepare'])

    def test_colabfold_reuses_a3m_for_multiple_seeds(self):
        job = self.job('colabfold')
        prepared = prepare_stage(job, self.root / 'prep', self.emit, lambda: False)
        result = predict_stage(job, prepared, self.root / 'pred', self.emit, lambda: False)
        self.assertEqual(prepared['format'], 'colabfold-a3m')
        self.assertEqual(result['returnedModelCount'], 2)
        self.assertTrue((self.root / 'pred/prepared/protein.a3m').exists())
        self.assertEqual(len(json.loads((self.root / 'pred/predict-execution.json').read_text())['runs']), 2)

    def test_cancelled_preparation_never_publishes_completion(self):
        with patch.dict(os.environ, {'TEST_PREP_SLEEP': '1'}):
            with self.assertRaises(engine.PredictionCancelled):
                prepare_stage(self.job(), self.root / 'prep', self.emit, lambda: self.started.exists())
        self.assertEqual(list((self.root / 'cache').rglob('COMPLETE.json')), [])
        self.assertEqual(list((self.root / 'cache').glob('*.partial-*')), [])
        prepared = prepare_stage(self.job(), self.root / 'retry', self.emit, lambda: False)
        self.assertFalse(prepared['cacheHit'])
        self.assertTrue(Path(prepared['manifestPath']).is_file())

    def test_failed_or_incomplete_preparation_not_cached(self):
        for flag, error in [('TEST_PREP_FAIL', RuntimeError), ('TEST_BAD_PREP', ValueError)]:
            with patch.dict(os.environ, {flag: '1'}):
                with self.assertRaises(error):
                    prepare_stage(self.job(), self.root / flag, self.emit, lambda: False)
        self.assertEqual(list((self.root / 'cache').rglob('COMPLETE.json')), [])

    def test_corrupt_cache_is_not_reused_or_predicted(self):
        job = self.job()
        prepared = prepare_stage(job, self.root / 'prep', self.emit, lambda: False)
        Path(prepared['inputPath']).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'incomplete or modified'):
            prepare_stage(job, self.root / 'retry', self.emit, lambda: False)
        with self.assertRaisesRegex(ValueError, 'incomplete or modified'):
            predict_stage(job, prepared, self.root / 'pred', self.emit, lambda: False)

    def test_changed_database_profile_cannot_consume_previous_preparation(self):
        job = self.job()
        prepared = prepare_stage(job, self.root / 'prep', self.emit, lambda: False)
        job['databaseProfile']['profileFingerprint'] = 'release-2'
        with self.assertRaisesRegex(ValueError, 'does not match'):
            predict_stage(job, prepared, self.root / 'pred', self.emit, lambda: False)
        fresh = prepare_stage(job, self.root / 'new', self.emit, lambda: False)
        self.assertNotEqual(fresh['cacheKey'], prepared['cacheKey'])

    def test_monolithic_af2_and_unready_profile_rejected(self):
        job = self.job('alphafold2')
        job['numSamples'] = 1
        with self.assertRaisesRegex(ValueError, 'monolithic'):
            prepare_stage(job, self.root / 'prep', self.emit, lambda: False)
        job = self.job()
        job['databaseProfile']['ready'] = False
        with self.assertRaisesRegex(ValueError, 'ready database profile'):
            prepare_stage(job, self.root / 'prep', self.emit, lambda: False)
        capabilities = {item['engine']: item for item in staged_capabilities()}
        self.assertFalse(capabilities['alphafold2']['staged'])
        self.assertTrue(capabilities['alphafold3']['available'])

    def test_a3m_homomer_cardinality_and_sequence_validation(self):
        path = self.root / 'complex.a3m'
        path.write_text('#2\t2\n>q\nAG\n')
        chains = [dict(id='A', sequence='AG'), dict(id='B', sequence='AG')]
        _validate_a3m(path, chains)
        path.write_text('#2\t1\n>q\nAG\n')
        with self.assertRaisesRegex(ValueError, 'cardinalities'):
            _validate_a3m(path, chains)
        path.write_text('#2,2\t1,1\n>chain-A\nAG--\n>hit-A\nAV--\n>chain-B\n--VC\n')
        _validate_a3m(path, [dict(id='A', sequence='AG'), dict(id='B', sequence='VC')])


class RunnerArgumentTests(unittest.TestCase):
    def load_runner(self, name):
        path = Path(__file__).resolve().parents[2] / 'scripts' / name
        spec = importlib.util.spec_from_file_location(name[:-3], path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_af3_helpers_use_real_stage_flags_and_managed_sampling(self):
        import argparse
        runner = self.load_runner('af3_staged_runner.py')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'input.json'
            source.write_text('{}')
            profile = root / 'profile.json'
            profile.write_text(json.dumps(dict(root='/db', paths={'seqres_database_path': '/db/seqres'},
                                              executables={'jackhmmer_binary_path':'/hmmer/bin/jackhmmer','nhmmer_binary_path':'/hmmer/bin/nhmmer'},
                                              searchParameters={'jackhmmer_n_cpu': 8}, templateParameters={'max_template_date': '2021-09-30'})))
            job = root / 'job.json'
            job.write_text(json.dumps(dict(numRecycles=10, numSamples=5)))
            args = argparse.Namespace(stage='prepare', input=str(source), output_dir='/out', profile_json=str(profile),
                                      job_json=str(job), python=sys.executable, alphafold_script='/opt/af3/run_alphafold.py')
            command = runner.build_command(args, [])
            self.assertIn('--run_inference=false', command)
            self.assertIn('--seqres_database_path=/db/seqres', command)
            self.assertIn('--jackhmmer_n_cpu=8', command)
            self.assertIn('--jackhmmer_binary_path=/hmmer/bin/jackhmmer',command)
            self.assertIn('--nhmmer_binary_path=/hmmer/bin/nhmmer',command)
            args.stage = 'predict'
            command = runner.build_command(args, [])
            self.assertIn('--run_data_pipeline=false', command)
            self.assertIn('--num_diffusion_samples=5', command)
            self.assertNotIn('--db_dir=/db', command)
            with self.assertRaises(ValueError):
                runner.build_command(args, ['--run_data_pipeline=true'])

    def test_colabfold_helper_uses_local_search_and_no_implicit_template_server(self):
        import argparse
        runner = self.load_runner('colabfold_staged_runner.py')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / 'profile.json'
            profile.write_text(json.dumps(dict(root='/db', paths={'uniref30_db': '/db/uniref'}, searchParameters={'threads': 4})))
            job = root / 'job.json'
            job.write_text(json.dumps(dict(numRecycles=3, numSamples=2)))
            args = argparse.Namespace(stage='prepare', input='/a.fasta', output_dir='/out', profile_json=str(profile),
                                      job_json=str(job), executable=None, seed=7)
            command = runner.build_command(args)
            self.assertEqual(command[:4], ['colabfold_search', '/a.fasta', '/db', '/out'])
            self.assertEqual(command[command.index('--gpu') + 1], '0')
            args.stage = 'predict'
            args.input = '/prepared'
            args.model_dir = '/models/colabfold'
            command = runner.build_command(args)
            self.assertEqual(command[:3], ['colabfold_batch', '/prepared', '/out'])
            self.assertNotIn('--templates', command)
            self.assertEqual(command[command.index('--random-seed') + 1], '7')
            self.assertEqual(command[command.index('--data') + 1], '/models/colabfold')
            bad_profile = json.loads(profile.read_text())
            bad_profile['templateParameters'] = {'max_templete_hits': 5}
            profile.write_text(json.dumps(bad_profile))
            with self.assertRaisesRegex(ValueError, 'Unsupported ColabFold template'):
                runner.build_command(args)

    def test_checked_in_database_and_stage_examples_match_runner_interfaces(self):
        import argparse
        import shlex
        from backend.databases import get_profile
        root = Path(__file__).resolve().parents[2]
        registry = root / 'config/databases.example.json'
        config = json.loads((root / 'config/staged-engines.example.json').read_text())
        exporter = self.load_runner('staged_env.py')
        af3 = self.load_runner('af3_staged_runner.py')
        cf = self.load_runner('colabfold_staged_runner.py')
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            source = temp / 'input.json'
            source.write_text('{}')
            for name, profile_id, runner in [('af3-local-staged', 'af3-protein-local', af3),
                                             ('colabfold-local-staged', 'colabfold-local', cf)]:
                env_lines = exporter.exports(config, name).splitlines()
                for line in env_lines:
                    parts = shlex.split(line)
                    self.assertEqual(parts[0], 'export')
                    key, value = parts[1].split('=', 1)
                    if key.endswith('_COMMAND'):
                        self.assertIsInstance(json.loads(value), list)
                profile = get_profile(profile_id, registry)
                profile_file = temp / (name + '.json')
                profile_file.write_text(json.dumps(profile))
                args = argparse.Namespace(stage='prepare', input=str(source), output_dir='/output',
                                          profile_json=str(profile_file), job_json=None, python=sys.executable,
                                          alphafold_script='/opt/af3/run_alphafold.py', executable=None, seed=None)
                command = runner.build_command(args, []) if runner is af3 else runner.build_command(args)
                self.assertTrue(command)
                if runner is af3:
                    for asset_name in profile['paths']:
                        self.assertIn(asset_name, af3.PATH_FLAGS)
                        self.assertIn('--' + asset_name + '=' + profile['paths'][asset_name], command)
                else:
                    self.assertEqual(command[command.index('--db1') + 1], profile['paths']['uniref30_db'])
                    self.assertEqual(command[command.index('--db3') + 1], profile['paths']['environment_db'])


if __name__ == '__main__':
    unittest.main()
