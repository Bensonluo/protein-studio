"""Parser and external-process integration checks; no model/GPU installation.

Fake executables produce explicitly synthetic test structures. Production
reference fixtures are downloaded public scientific records, not test predictions.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.engine import (FIXTURES, PredictionCancelled, list_engines, load_example,
                            run_prediction, validate_submission, _gpu_lease, _collect_outputs)
from backend.parsers import parse_confidence, parse_structure


def pdb_line(serial, aa, chain, residue, x, confidence=88, alt=' ', insertion=' '):
    return (f'ATOM  {serial:5d}  CA {alt}{aa:>3s} {chain}{residue:4d}{insertion}   '
            f'{x:8.3f}{0:8.3f}{0:8.3f}{1:6.2f}{confidence:6.2f}           C  \n')


FAKE_EXECUTABLE = r"""
import json,sys,time,shutil,os
from pathlib import Path
mode=sys.argv[1]
out=Path(sys.argv[2]);out.mkdir(parents=True,exist_ok=True)
(out/'visible_devices.json').write_text(json.dumps({'devices':os.environ.get('CUDA_VISIBLE_DEVICES')}))
if os.environ.get('FAKE_TEST_DELAY'):time.sleep(float(os.environ['FAKE_TEST_DELAY']))
input_path=Path(sys.argv[3])
if mode=='fail':
    print('intentional test error');sys.exit(7)
if mode=='sleep':
    (out/'started').write_text('yes');time.sleep(30);sys.exit(0)
if mode=='empty':
    sys.exit(0)
if mode in ['af3','af3-partial']:
    submitted=json.loads(input_path.read_text())
    assert submitted['dialect']=='alphafold3'
    assert submitted['version']==1
    assert submitted['modelSeeds']==[3,9]
    assert submitted['sequences'][0]['protein']['sequence']=='AG'
    directory=out/'protein'/'seed-3_sample-0';directory.mkdir(parents=True)
    cif='''data_test
#
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
ATOM 1 C CA . ALA A 1 1.0 2.0 3.0 1 91 1 A 1
ATOM 2 C CA . GLY A 2 4.0 5.0 6.0 1 81 2 A 1
#
'''
    (directory/'protein_model.cif').write_text(cif)
    (directory/'protein_confidences.json').write_text(json.dumps({'pae':[[0,3],[4,0]],'atom_plddts':[91,81]}))
    (directory/'protein_summary_confidences.json').write_text(json.dumps({'ptm':.8,'iptm':.7,'ranking_score':.72}))
    if mode=='af3':
        for seed,sample in [(3,1),(9,0),(9,1)]:
            shutil.copytree(directory,out/'protein'/f'seed-{seed}_sample-{sample}')
elif mode in ['af2','colabfold','mismatch']:
    sequence=''.join(line.strip() for line in input_path.read_text().splitlines() if not line.startswith('>'))
    if mode=='colabfold':assert sequence=='A:G'
    else:assert sequence=='AG'
    def line(i,aa,chain,res,b):
        return f'ATOM  {i:5d}  CA  {aa:>3s} {chain}{res:4d}    {i*3.8:8.3f}{0:8.3f}{0:8.3f}{1:6.2f}{b:6.2f}           C  \n'
    # Actual newline, not the literal escaped test-template representation.
    text=line(1,'ALA','A',1,90)+line(2,'GLY' if mode!='mismatch' else 'ALA','B' if mode=='colabfold' else 'A',1 if mode=='colabfold' else 2,80)
    text=text.replace('\\n','\n')
    if mode=='colabfold':
        (out/'protein_unrelaxed_rank_001_alphafold2_multimer_v3_model_1_seed_000.pdb').write_text(text)
        (out/'protein_relaxed_rank_001_alphafold2_multimer_v3_model_1_seed_000.pdb').write_text(text)
        (out/'protein_scores_rank_001_alphafold2_multimer_v3_model_1_seed_000.json').write_text(json.dumps({'plddt':[92,82],'pae':[[0,2],[3,0]],'ptm':.8,'iptm':.9}))
    else:
        (out/'ranked_0.pdb').write_text(text)
        (out/'ranking_debug.json').write_text(json.dumps({'order':['model_1_ptm'],'plddts':{'model_1_ptm':85}}))
        (out/'result_model_1_ptm.json').write_text(json.dumps({'ptm':.75,'pae':[[0,1],[2,0]]}))
print('test executable complete')
"""


class ParserTests(unittest.TestCase):
    def test_experimental_fixture_has_no_prediction_confidence(self):
        for file, n in [('1UBQ.pdb', 76), ('1CRN.pdb', 46)]:
            parsed = parse_structure(FIXTURES / file)
            self.assertEqual(len(parsed['residues']), n)
            self.assertIsNone(parsed['plddt'])
            self.assertIsNone(parsed['meanPlddt'])
            self.assertIsNotNone(parsed['residues'][0]['bFactor'])
            self.assertNotIn('plddt', parsed['residues'][0])

    def test_afdb_fixture_real_confidence_and_metadata(self):
        example = load_example('ubiquitin-afdb')
        parsed = parse_structure(FIXTURES / 'P0CG47.pdb', 'afdb')
        pae = parse_confidence(FIXTURES / 'P0CG47_pae.json')['pae']
        self.assertEqual(example['sequenceLength'], 229)
        self.assertEqual(len(parsed['plddt']), 229)
        self.assertEqual(len(pae), 229)
        self.assertTrue(0 <= parsed['meanPlddt'] <= 100)
        self.assertEqual(example['provenance']['mode'], 'reference')
        self.assertEqual(example['provenance']['databaseVersion'], 6)

    def test_altloc_insertion_and_first_model(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.pdb'
            path.write_text('MODEL        1\n' + pdb_line(1, 'ALA', 'A', 2, 3, alt='B')
                            + pdb_line(2, 'ALA', 'A', 2, 4, alt='A')
                            + pdb_line(3, 'GLY', 'A', 2, 5, insertion='A') + 'ENDMDL\n'
                            + 'MODEL        2\n' + pdb_line(4, 'VAL', 'B', 1, 9))
            parsed = parse_structure(path)
            self.assertEqual(len(parsed['residues']), 2)
            self.assertEqual(parsed['residues'][0]['x'], 4)
            self.assertEqual(parsed['residues'][1]['insertionCode'], 'A')

    def test_confidence_rejects_nonfinite_ranges_and_ragged_matrix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'confidence.json'
            path.write_text('{"plddt":[90,101],"pae":[[0],[1,0]],"ptm":NaN,"iptm":2}')
            self.assertEqual(parse_confidence(path), {})
            path.write_text('{"plddt":[90,80],"pae":[[0,3],[1,0]],"ptm":0.7}')
            self.assertEqual(parse_confidence(path)['ptm'], .7)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.executable = self.root / 'fake_predict.py'
        self.executable.write_text(FAKE_EXECUTABLE)
        self.events = []
        self.environment = patch.dict(os.environ, {'PROTEIN_STUDIO_GPU_GROUPS_JSON': '', 'PROTEIN_STUDIO_GPU_DEVICES': ''})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def emit(self, *args, **kwargs):
        self.events.append(args)

    def config(self, engine, mode, input_token='{fasta}', timeout='10'):
        return patch.dict(os.environ, {
            f'PROTEIN_STUDIO_{engine.upper()}_COMMAND': json.dumps([sys.executable, str(self.executable), mode, '{output_dir}', input_token]),
            'PROTEIN_STUDIO_TIMEOUT_SECONDS': timeout,
        })

    def test_reference_replay_and_sequence_restriction(self):
        example = load_example('ubiquitin')
        result = run_prediction(example, self.root / 'job', self.emit, lambda: False)
        self.assertEqual(result['provenance']['mode'], 'reference')
        self.assertFalse(result['provenance']['newPrediction'])
        self.assertIsNone(result['plddt'])
        self.assertTrue(Path(result['structurePath']).is_file())
        self.assertEqual(self.events[-1][0], 'completed')
        with self.assertRaisesRegex(ValueError, 'exact example'):
            validate_submission(dict(engine='reference', example='ubiquitin', sequence='AAAA'))

    def test_afdb_replay_has_real_pae_without_predicting(self):
        result = run_prediction(load_example('ubiquitin-afdb'), self.root / 'job', self.emit, lambda: False)
        self.assertEqual(len(result['pae']), 229)
        self.assertTrue(result['meanPlddt'] > 90)
        self.assertEqual(result['provenance']['structureType'], 'predicted')
        self.assertFalse(result['provenance']['newPrediction'])

    def test_input_validation_and_no_http_command_configuration(self):
        with self.assertRaises(ValueError):
            validate_submission(dict(engine='alphafold3', sequence='A;rm -rf'))
        with self.assertRaises(ValueError):
            validate_submission(dict(engine='alphafold3', sequence='AG', seeds=[True]))
        with self.assertRaises(ValueError):
            validate_submission(dict(engine='alphafold3', sequence='AG', seeds=[1, 1]))
        parsed = validate_submission(dict(engine='alphafold3', sequence='>first\nAG\n>second\nVC', command=['anything']))
        self.assertEqual(len(parsed['chains']), 2)
        self.assertNotIn('command', parsed)

    def test_af3_external_process_and_mmcif_metrics(self):
        job = dict(engine='alphafold3', sequence='AG', seeds=[3, 9], numSamples=2)
        with self.config('alphafold3', 'af3', '{input_json}'):
            result = run_prediction(job, self.root / 'job', self.emit, lambda: False)
        self.assertEqual(result['structureFormat'], 'mmcif')
        self.assertEqual(result['plddt'], [91, 81])
        self.assertEqual(result['pae'], [[0, 3], [4, 0]])
        self.assertEqual(result['iptm'], .7)
        self.assertEqual(result['provenance']['mode'], 'prediction')
        self.assertEqual(result['actualSampleCount'], 4)
        self.assertTrue(result['samplingComplete'])
        self.assertTrue((self.root / 'job' / 'engine-seed-3.log').is_file())
        self.assertEqual(len(json.loads((self.root / 'job' / 'execution.json').read_text())['runs']), 1)

    def test_af2_each_seed_and_safe_json_export(self):
        job = dict(engine='alphafold2', sequence='AG', seeds=[2, 7], numSamples=1)
        with self.config('alphafold2', 'af2'):
            result = run_prediction(job, self.root / 'job', self.emit, lambda: False)
        self.assertEqual(len(result['rankedModels']), 2)
        self.assertEqual(result['ptm'], .75)
        self.assertEqual(result['plddt'], [90, 80])
        self.assertTrue((self.root / 'job/output/seed-7/ranked_0.pdb').is_file())

    def test_oversized_optional_confidence_retains_prediction_and_artifact(self):
        real_stat = Path.stat
        def oversized_confidence_stat(path, *args, **kwargs):
            stat = real_stat(path, *args, **kwargs)
            if path.name == 'protein_confidences.json':
                fields = list(stat)
                fields[6] = 65 * 1024 * 1024
                return os.stat_result(fields)
            return stat

        with self.config('alphafold3', 'af3', '{input_json}'), patch.object(Path, 'stat', oversized_confidence_stat):
            result = run_prediction(dict(engine='alphafold3', sequence='AG', seeds=[3, 9], numSamples=2),
                                    self.root / 'job', self.emit, lambda: False)
            raw = next(item for item in result['artifacts'] if item['name'].endswith('/protein_confidences.json'))
            # Direct parsing/import still rejects oversize files; only optional
            # confidence extraction in a valid prediction degrades gracefully.
            with self.assertRaisesRegex(ValueError, '64 MiB'):
                parse_confidence(Path(raw['path']))
        self.assertTrue(result['samplingComplete'])
        self.assertEqual(result['plddt'], [91, 81])
        self.assertIsNone(result['pae'])
        self.assertEqual(result['ptm'], .8)
        self.assertEqual(result['iptm'], .7)
        self.assertEqual(result['rankingScore'], .72)
        self.assertTrue(any('downloadable' in warning for warning in result['warnings']))
        self.assertTrue(Path(raw['path']).is_file())
        self.assertEqual(self.events[-1][0], 'completed')

    def test_af3_partial_sampling_does_not_complete(self):
        with self.config('alphafold3', 'af3-partial', '{input_json}'):
            with self.assertRaisesRegex(RuntimeError, 'Incomplete AlphaFold 3 sampling'):
                run_prediction(dict(engine='alphafold3', sequence='AG', seeds=[3, 9], numSamples=2),
                               self.root / 'job', self.emit, lambda: False)

    def test_colabfold_complex_fasta_and_deduplication(self):
        job = dict(engine='colabfold', chains=[dict(id='A', sequence='A'), dict(id='B', sequence='G')], numSamples=1)
        with self.config('colabfold', 'colabfold'):
            result = run_prediction(job, self.root / 'job', self.emit, lambda: False)
        self.assertEqual(len(result['rankedModels']), 1)
        self.assertIn('_relaxed_', result['structurePath'])
        self.assertEqual(result['plddt'], [92, 82])
        self.assertEqual(result['residues'][1]['plddt'], 82)

    def test_failures_missing_output_and_mismatched_sequence(self):
        for mode, message in [('fail', 'status 7'), ('empty', 'without a supported'), ('mismatch', 'do not match')]:
            with self.subTest(mode=mode), self.config('alphafold2', mode):
                with self.assertRaisesRegex(RuntimeError, message):
                    run_prediction(dict(engine='alphafold2', sequence='AG', numSamples=1),
                                   self.root / mode, self.emit, lambda: False)

    def test_timeout_terminates_process(self):
        start = time.monotonic()
        with self.config('alphafold2', 'sleep', timeout='0.2'):
            with self.assertRaises(TimeoutError):
                run_prediction(dict(engine='alphafold2', sequence='AG', numSamples=1),
                               self.root / 'job', self.emit, lambda: False)
        self.assertLess(time.monotonic() - start, 4)

    def test_cancel_terminates_process(self):
        start = time.monotonic()
        with self.config('alphafold2', 'sleep'):
            with self.assertRaises(PredictionCancelled):
                run_prediction(dict(engine='alphafold2', sequence='AG', numSamples=1),
                               self.root / 'job', self.emit, lambda: time.monotonic() - start > .2)
        self.assertLess(time.monotonic() - start, 4)

    def test_malformed_admin_configuration_is_unavailable(self):
        with patch.dict(os.environ, {'PROTEIN_STUDIO_ALPHAFOLD3_COMMAND': 'python run.py'}):
            engine = next(e for e in list_engines() if e['id'] == 'alphafold3')
            self.assertFalse(engine['available'])
            self.assertIn('JSON argv', engine['unavailableReason'])

    def test_concurrent_jobs_receive_disjoint_gpu_groups(self):
        job = dict(engine='alphafold2', sequence='AG', numSamples=1)
        with self.config('alphafold2', 'af2'), patch.dict(os.environ, {
            'PROTEIN_STUDIO_GPU_GROUPS_JSON': '[["3","4"],["5","6"]]', 'FAKE_TEST_DELAY': '0.3',
        }), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run_prediction, job, self.root / str(i), self.emit, lambda: False) for i in range(2)]
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual({tuple(r['provenance']['assignedGpuGroup']) for r in results}, {('3', '4'), ('5', '6')})
        visible = [json.loads((self.root / str(i) / 'output/seed-1/visible_devices.json').read_text())['devices'] for i in range(2)]
        self.assertEqual(set(visible), {'3,4', '5,6'})

    def test_waiting_for_gpu_is_cancellable_and_lease_is_released(self):
        job = dict(engine='alphafold2', sequence='AG', numSamples=1)
        with self.config('alphafold2', 'af2'), patch.dict(os.environ, {'PROTEIN_STUDIO_GPU_GROUPS_JSON': '[["0"]]'}):
            with _gpu_lease(self.emit, lambda: False):
                start = time.monotonic()
                with self.assertRaises(PredictionCancelled):
                    run_prediction(job, self.root / 'blocked', self.emit, lambda: time.monotonic() - start > .2)
            result = run_prediction(job, self.root / 'ready', self.emit, lambda: False)
        self.assertEqual(result['provenance']['assignedGpuGroup'], ['0'])
        self.assertIn('waiting', [event[0] for event in self.events])

    def test_unconfigured_pool_preserves_runner_device_allocation(self):
        with self.config('alphafold2', 'af2'), patch.dict(os.environ, {'CUDA_VISIBLE_DEVICES': '9'}):
            result = run_prediction(dict(engine='alphafold2', sequence='AG'), self.root / 'job', self.emit, lambda: False)
        visible = json.loads((self.root / 'job/output/seed-1/visible_devices.json').read_text())
        self.assertEqual(visible['devices'], '9')
        self.assertIsNone(result['provenance']['assignedGpuGroup'])

    def test_overlapping_gpu_groups_are_rejected(self):
        with self.config('alphafold2', 'af2'), patch.dict(os.environ, {'PROTEIN_STUDIO_GPU_GROUPS_JSON': '[[0],[0]]'}):
            with self.assertRaisesRegex(ValueError, 'overlap'):
                run_prediction(dict(engine='alphafold2', sequence='AG'), self.root / 'job', self.emit, lambda: False)

    def test_missing_af3_summary_does_not_outrank_official_score(self):
        output = self.root / 'output'
        for sample, confidence in [(0, 50), (1, 99)]:
            directory = output / f'seed-1_sample-{sample}'
            directory.mkdir(parents=True)
            (directory / 'protein_model.pdb').write_text(pdb_line(1, 'ALA', 'A', 1, 0, confidence))
        (output / 'seed-1_sample-0/protein_summary_confidences.json').write_text('{"ranking_score":0.1}')
        result = _collect_outputs(output, 'alphafold3', self.root, [{'id': 'A', 'sequence': 'A'}])
        self.assertIn('sample-0', result['structurePath'])
        self.assertTrue(result['warnings'])

    def test_non_top_sequence_mismatch_is_also_rejected(self):
        output = self.root / 'output'
        output.mkdir()
        (output / 'ranked_0.pdb').write_text(pdb_line(1, 'ALA', 'A', 1, 0, 99))
        (output / 'ranked_1.pdb').write_text(pdb_line(1, 'GLY', 'A', 1, 0, 50))
        with self.assertRaisesRegex(RuntimeError, 'do not match'):
            _collect_outputs(output, 'alphafold2', self.root, [{'id': 'A', 'sequence': 'A'}])

    def test_af2_adapter_patches_recycles_and_exports_live_confidence(self):
        # A minimal fake installed AF2 package exercises the real adapter
        # bootstrap; no AF2/GPU dependencies are imported in this test process.
        installed = self.root / 'installed'
        package = installed / 'alphafold/model'
        package.mkdir(parents=True)
        (installed / 'alphafold/__init__.py').write_text('')
        (package / '__init__.py').write_text('')
        (package / 'config.py').write_text(
            "def model_config(name):\n"
            "    result={'model':{'num_recycle':20}}\n"
            "    if 'multimer' not in name:result['data']={'common':{'num_recycle':3}}\n"
            "    return result\n")
        (package / 'model.py').write_text(
            "class RunModel:\n"
            "    def __init__(self,config,params):self.config=config\n"
            "    def predict(self,*args,**kwargs):\n"
            "        assert self.config['model']['num_recycle']==7\n"
            "        if 'data' in self.config:assert self.config['data']['common']['num_recycle']==7\n"
            "        return {'plddt':[91,81],'predicted_aligned_error':[[0,2],[3,0]],'ptm':.8,'ranking_confidence':86}\n")
        script = installed / 'run_alphafold.py'
        script.write_text(
            "import sys\nfrom alphafold.model import config,model\n"
            "flags=dict(x[2:].split('=',1) for x in sys.argv[1:])\n"
            "assert flags['benchmark']=='false'\n"
            "assert flags['random_seed']=='4'\n"
            "name='model_1_multimer_v3' if flags['model_preset']=='multimer' else 'model_1_ptm'\n"
            "runner=model.RunModel(config.model_config(name),{})\n"
            "for i in range(int(flags['num_multimer_predictions_per_model'])):runner.predict({},random_seed=4)\n")
        runner = Path(__file__).resolve().parents[2] / 'scripts/af2_runner.py'
        for multimer in [False, True]:
            with self.subTest(multimer=multimer):
                directory = self.root / str(multimer)
                directory.mkdir()
                fasta = directory / 'protein.fasta'
                fasta.write_text('>A\nAG\n')
                job = directory / 'job.json'
                job.write_text(json.dumps(dict(chains=[{'id': 'A', 'sequence': 'AG'}] * (2 if multimer else 1),
                                               seeds=[4], numSamples=2 if multimer else 1, numRecycles=7)))
                completed = subprocess.run([sys.executable, str(runner), '--job-json', str(job), '--fasta', str(fasta),
                                            '--output-dir', str(directory / 'result'), '--seed', '4',
                                            '--alphafold-script', str(script), '--', '--max_template_date=2021-01-01'],
                                           capture_output=True, text=True, timeout=5)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                exports = list((directory / 'result/protein').glob('result_*.json'))
                self.assertEqual(len(exports), 2 if multimer else 1)
                self.assertEqual(parse_confidence(exports[0])['pae'], [[0, 2], [3, 0]])


if __name__ == '__main__':
    unittest.main()
