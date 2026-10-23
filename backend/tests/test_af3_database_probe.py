"""Explicit synthetic CPU fixtures; these tests do not install or run real AF3."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend import databases
from scripts import af3_database_probe as probe


FAKE_AF3 = r'''
import json,os,sys
from pathlib import Path
if '--helpshort' in sys.argv:
    print('--[no]run_data_pipeline --[no]run_inference --[no]compress_large_output_files --json_path')
    raise SystemExit(1)
args=dict(arg[2:].split('=',1) for arg in sys.argv[1:])
assert args['run_data_pipeline']=='true' and args['run_inference']=='false'
assert os.environ['CUDA_VISIBLE_DEVICES']=='' and os.environ['JAX_PLATFORMS']=='cpu'
data=json.loads(Path(args['json_path']).read_text())
for entry in data['sequences']:
    p=entry['protein']
    p.update(unpairedMsa='>query\n'+p['sequence']+'\n',pairedMsa='',templates=[])
out=Path(args['output_dir']);out.mkdir(parents=True)
(out/'protein_data.json').write_text(json.dumps(data))
'''


class AF3DatabaseProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.db=self.root/'database'
        self.db.mkdir()
        assets=[]
        for key,name in databases.AF3_ASSETS.items():
            path=self.db/name
            if key=='pdb_database_path':
                path.mkdir()
                for index in range(8):
                    (path/f'{index:04d}.cif').write_text(f'data_{index:04d}\n# Explicit test fixture\n'+'# padding\n'*20)
            else:
                path.write_text('>fixture_only\n'+('ACDEFGHIKLMNPQRSTVWY\n'*53000))
            assets.append(dict(name=key,kind='template' if key in ('pdb_database_path','seqres_database_path') else 'sequence',
                type='directory' if key=='pdb_database_path' else 'file',path=name,required=True))
        self.tools={}
        for key in databases.AF3_TOOLS:
            executable=self.root/key
            executable.write_text(f'#!{sys.executable}\nprint("# HMMER 3.4 (fixture only)")\nprint("--seq_limit")\n')
            executable.chmod(0o755)
            self.tools[key]=str(executable)
        self.profile=dict(engine='alphafold3',validationProfile='af3-v3.0.4',root=str(self.db),
            release='SYNTHETIC_TEST_FIXTURE',prepareVersion='fixture',searchVersion='fixture',
            assets=assets,executables=self.tools,searchParameters={'jackhmmer_n_cpu':1,'nhmmer_n_cpu':1},
            templateParameters={'max_template_date':'2021-09-30'})
        self.config=dict(schemaVersion=1,profiles={'fixture':self.profile})
        self.script=self.root/'run_alphafold.py'
        self.script.write_text(FAKE_AF3)
        self.original_run=probe._run

    def fake_git(self,argv,*args,**kwargs):
        if argv[0]=='git':
            return dict(returncode=0,output='' if 'status' in argv else probe.AF3_COMMIT+'\n',seconds=0,outputTruncated=False)
        return self.original_run(argv,*args,**kwargs)

    def test_strict_inventory_detects_stubs_wrong_format_missing_rna_and_nonexecutable_tools(self):
        self.assertTrue(databases.get_profile('fixture',self.config)['ready'])
        path=self.db/databases.AF3_ASSETS['ntrna_database_path']
        path.write_text('>dummy\nACGT\n')
        result=databases.get_profile('fixture',self.config)
        self.assertFalse(result['ready'])
        self.assertTrue(any(d.get('asset')=='ntrna_database_path' and d['code']=='af3-invalid-content' for d in result['diagnostics']))
        path.write_text('<html>'+('x'*1100000))
        self.assertFalse(databases.get_profile('fixture',self.config)['ready'])
        self.profile['assets']=[a for a in self.profile['assets'] if a['name']!='ntrna_database_path']
        Path(self.tools['nhmmer_binary_path']).chmod(0o644)
        codes={d['code'] for d in databases.get_profile('fixture',self.config)['diagnostics']}
        self.assertIn('af3-missing-asset',codes)
        self.assertIn('not-executable',codes)

    def test_tool_replacement_changes_fingerprint_and_nested_pdb_layout_is_rejected(self):
        before=databases.get_profile('fixture',self.config)['profileFingerprint']
        executable=Path(self.tools['jackhmmer_binary_path'])
        executable.write_text(executable.read_text()+'# changed binary\n')
        self.assertNotEqual(before,databases.get_profile('fixture',self.config)['profileFingerprint'])
        cif=self.db/'mmcif_files'
        nested=cif/'nested'
        nested.mkdir()
        for path in list(cif.glob('*.cif')): path.rename(nested/path.name)
        result=databases.get_profile('fixture',self.config)
        self.assertFalse(result['ready'])
        self.assertTrue(any(d.get('asset')=='pdb_database_path' for d in result['diagnostics']))

    def test_probe_does_not_qualify_small_dummy_template_inventory_as_production(self):
        with patch.object(probe,'_run',side_effect=self.fake_git):
            result=probe.probe(self.config,'fixture',self.script,sys.executable)
        self.assertFalse(result['ready'])
        self.assertEqual(result['samplePreparation']['status'],'not-run')
        self.assertIn('incomplete-mmcif',{d['code'] for d in result['diagnostics']})
        self.assertTrue(result['runtime']['flagsPresent']['--run_data_pipeline'])

    def test_probe_version_mismatch_is_blocking_and_does_not_launch_sample(self):
        def wrong_git(argv,*args,**kwargs):
            if argv[0]=='git' and argv[-1]=='HEAD': return dict(returncode=0,output='f'*40+'\n')
            return self.fake_git(argv,*args,**kwargs)
        output=self.root/'not-created'
        with patch.object(probe,'MIN_MMCIF_RECORDS',8),patch.object(probe,'_run',side_effect=wrong_git):
            result=probe.probe(self.config,'fixture',self.script,sys.executable,sample_input='irrelevant',sample_output=output)
        self.assertFalse(result['ready'])
        self.assertIn('source-version',{d['code'] for d in result['diagnostics']})
        self.assertEqual(result['samplePreparation']['status'],'blocked')
        self.assertFalse(output.exists())

    def test_explicit_sample_runs_real_subprocess_and_rejects_prefilled_msa_bypass(self):
        sample=self.root/'sample.json'
        value=dict(dialect='alphafold3',version=1,name='sample',modelSeeds=[1],
                   sequences=[dict(protein=dict(id='A',sequence='ACDEFGHIK'))])
        sample.write_text(json.dumps(value))
        with patch.object(probe,'MIN_MMCIF_RECORDS',8),patch.object(probe,'_run',side_effect=self.fake_git):
            result=probe.probe(self.config,'fixture',self.script,sys.executable,sample_input=sample,sample_output=self.root/'sample-out')
        self.assertTrue(result['ready'],result)
        self.assertEqual(result['samplePreparation']['status'],'passed')
        self.assertTrue(Path(result['samplePreparation']['output']).is_file())
        self.assertEqual(result['samplePreparation']['chains'][0]['unpairedMsaRows'],1)
        value['sequences'][0]['protein']['unpairedMsa']=''
        sample.write_text(json.dumps(value))
        with patch.object(probe,'MIN_MMCIF_RECORDS',8),patch.object(probe,'_run',side_effect=self.fake_git):
            rejected=probe.probe(self.config,'fixture',self.script,sys.executable,sample_input=sample,sample_output=self.root/'no-search')
        self.assertFalse(rejected['ready'])
        self.assertIn('skip search',rejected['samplePreparation']['error'])

    def test_unpatched_jackhmmer_is_reported_as_fallback_not_false_incompatibility(self):
        tool=Path(self.tools['jackhmmer_binary_path'])
        tool.write_text(f'#!{sys.executable}\nprint("# HMMER 3.4")\n')
        with patch.object(probe,'MIN_MMCIF_RECORDS',8),patch.object(probe,'_run',side_effect=self.fake_git):
            result=probe.probe(self.config,'fixture',self.script,sys.executable)
        self.assertTrue(result['ready'],result)
        self.assertFalse(result['tools']['jackhmmer_binary_path']['seqLimitSupported'])
        self.assertEqual(next(d for d in result['diagnostics'] if d['code']=='jackhmmer-unpatched')['severity'],'warning')


if __name__=='__main__': unittest.main()
