"""Real subprocess tests with explicit synthetic search/native-API stand-ins.

No MMseqs binary, sequence database, AF3 installation or GPU is exercised here.
The fixture factories can also be used by HTTP integration tests.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

from scripts import mmseqs_af3_runner as runner


def make_fake_search(root):
    path = Path(root) / 'fake-colabfold-search'
    path.write_text('#!' + sys.executable + '\n' + textwrap.dedent(r'''
        import json, os, pathlib, subprocess, sys, time
        query, dbroot, out = sys.argv[1:4]
        out = pathlib.Path(out)
        out.mkdir(parents=True, exist_ok=True)
        (out / 'command.json').write_text(json.dumps(dict(argv=sys.argv[1:], cuda=os.environ.get('CUDA_VISIBLE_DEVICES'))))
        mode = os.environ.get('FAKE_CF_MODE', 'ok')
        if mode == 'fail':
            raise SystemExit(7)
        if mode == 'missing':
            raise SystemExit(0)
        if mode == 'hang':
            child = subprocess.Popen([sys.executable, '-c',
                'from pathlib import Path; import time; p=Path(' + repr(str(out / 'heartbeat')) + ');\nwhile True:\n p.write_text(str(time.time())); time.sleep(.03)'])
            (out / 'grandchild.pid').write_text(str(child.pid))
            time.sleep(30)
        seq = ''.join(pathlib.Path(query).read_text().splitlines()[1:])
        if mode == 'wrong':
            seq = 'Y' * len(seq)
        if mode == 'width':
            second = seq + 'A'
        else:
            second = seq[0] + 'gg' + '-' * (len(seq) - 1)
        (out / 'protein.a3m').write_bytes(('>101 query\r\n' + seq + '\r\n>environment hit\r\n' + second + '\r\n').encode())
    '''))
    path.chmod(0o755)
    return path


def make_fake_native(root):
    """Return PYTHONPATH containing public-API stand-ins, recording actual calls."""
    base = Path(root) / 'fake-native'
    files = {
        'alphafold3/__init__.py': '',
        'alphafold3/constants/__init__.py': '',
        'alphafold3/constants/mmcif_names.py': "PROTEIN_CHAIN = 'polypeptide(L)'\n",
        'alphafold3/data/__init__.py': '',
        'alphafold3/data/msa_config.py': '''
from types import SimpleNamespace
DatabaseConfig = JackhmmerConfig = RunConfig = HmmsearchConfig = TemplateFilterConfig = SimpleNamespace
''',
        'alphafold3/data/msa.py': '''
import json, os
from pathlib import Path
from types import SimpleNamespace
def get_msa(*, target_sequence, run_config, chain_poly_type, deduplicate):
    cfg = run_config.config
    log = dict(kind='paired', deduplicate=deduplicate, cuda=os.environ.get('CUDA_VISIBLE_DEVICES'),
        jax=os.environ.get('JAX_PLATFORMS'), n_cpu=cfg.n_cpu, n_iter=cfg.n_iter,
        e_value=cfg.e_value, z_value=cfg.z_value, dom_z_value=cfg.dom_z_value,
        max_sequences=cfg.max_sequences, max_parallel_shards=cfg.max_parallel_shards,
        database=vars(cfg.database_config))
    with Path(os.environ['FAKE_NATIVE_LOG']).open('a') as f: f.write(json.dumps(log)+'\\n')
    a3m = '>query\\n'+target_sequence+'\\n>sp|P1|GENE_SPECIES/1-4\\n'+target_sequence+'\\n>sp|P1|GENE_SPECIES/1-4\\n'+target_sequence+'\\n'
    return SimpleNamespace(to_a3m=lambda:a3m)
''',
        'alphafold3/data/structure_stores.py': '''
class StructureStore:
    def __init__(self, path): self.path = path
''',
        'alphafold3/data/templates.py': '''
import json, os
from pathlib import Path
from types import SimpleNamespace
class Templates:
    @staticmethod
    def from_seq_and_a3m(**kwargs):
        config = kwargs['hmmsearch_config']
        filters = kwargs['filter_config']
        log = dict(kind='templates', cuda=os.environ.get('CUDA_VISIBLE_DEVICES'),
            msa=kwargs['msa_a3m'], max_hits=filters.max_hits,
            cutoff=str(filters.max_template_date), query_limit=kwargs['max_a3m_query_sequences'],
            hmmsearch=vars(config), structure_path=kwargs['structure_store'].path)
        with Path(os.environ['FAKE_NATIVE_LOG']).open('a') as f: f.write(json.dumps(log)+'\\n')
        hit = SimpleNamespace(query_to_hit_mapping={0:2, 2:5})
        structure = SimpleNamespace(to_mmcif=lambda:'data_synthetic_template\\n# synthetic test fixture, no real coordinates\\n')
        return SimpleNamespace(get_hits_with_structures=lambda:[(hit, structure)])
''',
    }
    for relative, text in files.items():
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return base


def make_profile(root, *, native=False, device='cpu'):
    root = Path(root)
    search = make_fake_search(root)
    return dict(root=str(root), paths=dict(uniref30_db=str(root / 'uniref30'),
        environment_db=str(root / 'environment'), uniprot_cluster_annot_database_path=str(root / 'uniprot.fa'),
        seqres_database_path=str(root / 'seqres.fa'), pdb_database_path=str(root / 'pdb')),
        executables=dict(colabfold_search_binary=str(search), mmseqs_binary=str(root / 'mmseqs'),
            af3_python=sys.executable, jackhmmer_binary_path=str(root / 'jackhmmer'),
            hmmsearch_binary_path=str(root / 'hmmsearch'), hmmbuild_binary_path=str(root / 'hmmbuild')),
        searchBackend=dict(id='mmseqs2-colabfold', device=device, version='18-8cc5c',
            colabfoldVersion='1.6.3', adapterVersion='mmseqs-af3-v1',
            databaseFormat='colabfold-gpu' if device == 'gpu' else 'colabfold-expandable',
            gpuServer=False, pairingStrategy='native-uniprot' if native else 'none',
            templateStrategy='native-af3' if native else 'none'),
        searchParameters=dict(threads=2, use_env=1, jackhmmer_n_cpu=3,
            jackhmmer_max_parallel_shards=2, uniprot_cluster_annot_z_value=101),
        templateParameters=dict(max_template_date='2026-01-01') if native else {})


class MMseqsAf3RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input = dict(name='target name', dialect='alphafold3', version=1,
                          modelSeeds=[7, 19], sequences=[dict(protein=dict(id=['C'], sequence='ACDE'))])
        self.profile = make_profile(self.root)
        self.output = self.root / 'output'
        self.input_path, self.profile_path = self.root / 'input.json', self.root / 'profile.json'
        self.input_path.write_text(json.dumps(self.input))
        self.profile_path.write_text(json.dumps(self.profile))

    def finalize(self):
        return runner.finalize(self.input, self.profile, self.output,
                               input_path=self.input_path, profile_path=self.profile_path)

    def test_two_real_process_phases_preserve_exact_a3m_and_identity(self):
        argv = [sys.executable, str(Path(runner.__file__)), '--input', str(self.input_path),
                '--profile-json', str(self.profile_path), '--output-dir', str(self.output)]
        result = subprocess.run(argv + ['--phase', 'search'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(list(self.output.glob('*_data.json')))
        original = (self.output / 'mmseqs-search/protein.a3m').read_bytes()
        result = subprocess.run(argv + ['--phase', 'finalize'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        prepared = json.loads((self.output / runner.DATA_NAME).read_text())
        protein = prepared['sequences'][0]['protein']
        self.assertEqual(prepared['modelSeeds'], [7, 19])
        self.assertEqual(protein['id'], ['C'])
        self.assertEqual(protein['unpairedMsa'].encode(), original)
        self.assertEqual(protein['pairedMsa'], '')
        self.assertEqual(protein['templates'], [])
        state = json.loads((self.output / runner.STATE_NAME).read_text())
        self.assertEqual(state['unpairedSha256'], hashlib.sha256(original).hexdigest())
        command = json.loads((self.output / 'mmseqs-search/command.json').read_text())
        self.assertEqual(command['cuda'], '')
        for flag, value in [('--pair-mode', 'unpaired'), ('--use-templates', '0'), ('--use-env-pairing', '0'), ('--gpu', '0')]:
            self.assertEqual(command['argv'][command['argv'].index(flag) + 1], value)

    def test_native_cpu_api_settings_pairing_duplicates_and_template_map(self):
        self.profile = make_profile(self.root, native=True, device='gpu')
        self.profile_path.write_text(json.dumps(self.profile))
        native = make_fake_native(self.root)
        log = self.root / 'native-calls.jsonl'
        with mock.patch.dict(os.environ, {'CUDA_VISIBLE_DEVICES': '3,7', 'PYTHONPATH': str(native), 'FAKE_NATIVE_LOG': str(log)}):
            runner.search(self.input, self.profile, self.output)
            result = self.finalize()
        command = json.loads((self.output / 'mmseqs-search/command.json').read_text())
        self.assertEqual(command['cuda'], '3,7')
        self.assertEqual(command['argv'][command['argv'].index('--gpu') + 1], '1')
        self.assertNotIn('-s', command['argv'])
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([x['kind'] for x in calls], ['paired', 'templates'])
        self.assertTrue(all(x['cuda'] == '' for x in calls))
        paired = calls[0]
        self.assertFalse(paired['deduplicate'])
        self.assertEqual((paired['n_cpu'], paired['max_sequences'], paired['n_iter'], paired['e_value']), (3, 50000, 1, 1e-4))
        self.assertEqual((paired['z_value'], paired['dom_z_value'], paired['max_parallel_shards']), (101, 101, 2))
        protein = result['sequences'][0]['protein']
        self.assertEqual(protein['pairedMsa'].count('>sp|P1|GENE_SPECIES'), 2)
        self.assertEqual(protein['templates'][0]['queryIndices'], [0, 2])
        self.assertEqual(protein['templates'][0]['templateIndices'], [2, 5])
        self.assertEqual(calls[1]['msa'], protein['unpairedMsa'])
        self.assertEqual(calls[1]['max_hits'], 4)
        self.assertIsNone(calls[1]['query_limit'])
        provenance = json.loads((self.output / 'msa-search-provenance.json').read_text())
        self.assertEqual(provenance['msaQuality']['paired']['hitRows'], 2)
        self.assertIsNone(provenance['finalPairedRows'])
        self.assertFalse(provenance['scientificEquivalenceToNativeAf3'])

    def test_changed_intermediate_input_or_profile_cannot_be_finalized(self):
        runner.search(self.input, self.profile, self.output)
        with (self.output / 'mmseqs-search/protein.a3m').open('a') as stream:
            stream.write('\n')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.finalize()
        self.input['modelSeeds'] = [29]
        with self.assertRaisesRegex(ValueError, 'does not belong'):
            self.finalize()
        self.assertFalse((self.output / runner.DATA_NAME).exists())

    def test_search_failures_never_publish_final_or_complete_state(self):
        for mode in ('fail', 'missing', 'wrong', 'width'):
            with self.subTest(mode=mode), mock.patch.dict(os.environ, {'FAKE_CF_MODE': mode}):
                out = self.root / mode
                with self.assertRaises((ValueError, RuntimeError)):
                    runner.search(self.input, self.profile, out)
                self.assertFalse((out / runner.STATE_NAME).exists())
                self.assertFalse((out / runner.DATA_NAME).exists())

    def test_profile_rejects_silent_scientific_switches_and_gpu_server(self):
        modifications = [lambda p:p['searchBackend'].update(gpuServer=True),
            lambda p:p['searchParameters'].update(pair_mode='paired'),
            lambda p:p['searchBackend'].update(pairingStrategy='colabfold-paired'),
            lambda p:p['searchBackend'].update(device='gpu'),
            lambda p:p['templateParameters'].update(use_templates=True)]
        for modify in modifications:
            profile = copy.deepcopy(self.profile)
            modify(profile)
            with self.assertRaises(ValueError):
                runner.validate_profile(profile)
        profile = make_profile(self.root, device='gpu')
        profile['searchParameters']['s'] = 7.5
        with self.assertRaisesRegex(ValueError, 'does not honor'):
            runner.validate_profile(profile)

    def test_gpu_requires_explicit_devices(self):
        self.profile = make_profile(self.root, device='gpu')
        with mock.patch.dict(os.environ, {'CUDA_VISIBLE_DEVICES': ''}):
            with self.assertRaisesRegex(ValueError, 'scheduler-assigned'):
                runner.search(self.input, self.profile, self.output)

    def test_input_rejects_complex_modifications_and_existing_scientific_data(self):
        for extra in [dict(modifications=[{'ptmType':'MSE','ptmPosition':1}]), dict(pairedMsa=''), dict(templates=[])]:
            data = copy.deepcopy(self.input)
            data['sequences'][0]['protein'].update(extra)
            with self.assertRaises(ValueError):
                runner.validate_input(data)
        data = copy.deepcopy(self.input)
        data['sequences'].append(copy.deepcopy(data['sequences'][0]))
        with self.assertRaisesRegex(ValueError, 'one independent'):
            runner.validate_input(data)

    def test_timeout_terminates_grandchild_not_only_cf_parent(self):
        with mock.patch.dict(os.environ, {'FAKE_CF_MODE': 'hang'}):
            with self.assertRaises(TimeoutError):
                runner.search(self.input, self.profile, self.output, timeout=.5)
        heartbeat = self.output / 'mmseqs-search/heartbeat'
        self.assertTrue(heartbeat.exists())
        before = heartbeat.read_text()
        time.sleep(.15)
        self.assertEqual(heartbeat.read_text(), before)
        self.assertFalse((self.output / runner.STATE_NAME).exists())

    def test_cancellation_reaps_search_and_prevents_publication(self):
        stopped = threading.Event()
        timer = threading.Timer(.3, stopped.set)
        timer.start()
        try:
            with mock.patch.dict(os.environ, {'FAKE_CF_MODE': 'hang'}):
                with self.assertRaises(runner.SearchCancelled):
                    runner.search(self.input, self.profile, self.output, cancelled=stopped.is_set)
        finally:
            timer.cancel()
        self.assertFalse((self.output / runner.STATE_NAME).exists())

    def test_fresh_attempt_required_and_source_symlinks_rejected(self):
        runner.search(self.input, self.profile, self.output)
        with self.assertRaisesRegex(ValueError, 'fresh output'):
            runner.search(self.input, self.profile, self.output)
        a3m = self.output / 'mmseqs-search/protein.a3m'
        actual = self.root / 'outside.a3m'
        a3m.rename(actual)
        a3m.symlink_to(actual)
        with self.assertRaisesRegex(ValueError, 'regular input'):
            self.finalize()

    def test_managed_child_keeps_engine_group_and_inherits_lock_fd(self):
        output = self.root / 'child.json'
        with (self.root / 'lock').open('wb') as lock:
            environment = {**os.environ, 'PROTEIN_STUDIO_MANAGED_PROCESS_GROUP': '1',
                           'PROTEIN_STUDIO_GPU_LOCK_FDS_JSON': json.dumps([lock.fileno()])}
            source = ('import json,os; from pathlib import Path; '
                      'Path(' + repr(str(output)) + ').write_text(json.dumps(dict('
                      'pgid=os.getpgrp(), inode=os.fstat(' + str(lock.fileno()) + ').st_ino)))')
            runner.run_command([sys.executable, '-c', source], self.root / 'child.log',
                               env=environment, timeout=2)
            record = json.loads(output.read_text())
            self.assertEqual(record['pgid'], os.getpgrp())
            self.assertEqual(record['inode'], os.fstat(lock.fileno()).st_ino)
        environment['PROTEIN_STUDIO_GPU_LOCK_FDS_JSON'] = '[true]'
        with self.assertRaisesRegex(ValueError, 'descriptor'):
            runner.run_command([sys.executable, '-c', 'pass'], self.root / 'bad.log', env=environment, timeout=2)

    def test_sharded_native_uniprot_requires_global_database_size(self):
        profile = make_profile(self.root, native=True)
        profile['paths']['uniprot_cluster_annot_database_path'] += '@8'
        profile['searchParameters'].pop('uniprot_cluster_annot_z_value')
        with self.assertRaisesRegex(ValueError, 'Sharded UniProt'):
            runner.validate_profile(profile)


if __name__ == '__main__':
    unittest.main()
