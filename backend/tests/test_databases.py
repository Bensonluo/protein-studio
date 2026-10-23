"""Small local fixtures verify database readiness and cache-identity rules."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import databases


class DatabaseRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'seq.fa').write_text('>tiny\nACDE\n')
        (self.root / 'templates').mkdir()
        (self.root / 'templates' / 'tiny.cif').write_text('data_tiny\n')
        self.profile = dict(engine='alphafold3', release='test-db-20261023', root=str(self.root),
                            prepareVersion='runner-test-1', searchVersion='hmmer-test-1',
                            searchParameters={'jackhmmer_n_cpu':2},
                            templateParameters={'max_template_date':'2021-09-30'},
                            assets=[dict(name='uniref90_database_path', kind='sequence', path='seq.fa'),
                                    dict(name='pdb_database_path', kind='template', type='directory', path='templates')])
        self.config = dict(schemaVersion=1, profiles={'tiny':self.profile})

    def get(self):
        return databases.get_profile('tiny', self.config)

    def test_ready_and_stable_metadata_fingerprint(self):
        first = self.get()
        self.assertTrue(first['ready'])
        self.assertEqual(first['profileFingerprint'], self.get()['profileFingerprint'])
        self.assertEqual(first['paths']['uniref90_database_path'], str(self.root/'seq.fa'))
        self.assertEqual(len(first['profileFingerprint']),64)
        self.assertEqual(first['diagnostics'],[])

    def test_reference_without_database(self):
        reference = databases.get_profile('reference', {'profiles':{}})
        self.assertTrue(reference['ready'])
        self.assertTrue(reference['noDatabaseRequired'])
        self.assertEqual(reference['paths'],{})

    def test_missing_required_and_optional_files(self):
        self.profile['assets'].append(dict(name='optional_rna',kind='sequence',path='not-here',required=False))
        self.assertTrue(self.get()['ready'])
        self.assertEqual(self.get()['diagnostics'][0]['severity'],'warning')
        (self.root/'seq.fa').unlink()
        result=self.get()
        self.assertFalse(result['ready'])
        self.assertIn('missing',[d['code'] for d in result['diagnostics']])

    def test_empty_file_empty_directory_and_wrong_type(self):
        (self.root/'seq.fa').write_text('')
        self.assertFalse(self.get()['ready'])
        (self.root/'seq.fa').unlink()
        (self.root/'seq.fa').mkdir()
        self.assertIn('wrong-type',[d['code'] for d in self.get()['diagnostics']])
        (self.root/'templates'/'tiny.cif').unlink()
        self.assertIn('empty',[d['code'] for d in self.get()['diagnostics']])

    def test_unreadable_file(self):
        original=Path.open
        def denied(path,*args,**kwargs):
            if path.name=='seq.fa':
                raise PermissionError('fixture denied')
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',denied):
            self.assertIn('unreadable',[d['code'] for d in self.get()['diagnostics']])

    def test_required_prefix_members(self):
        self.profile['assets']=[dict(name='mmseqs',kind='sequence',type='prefix',path='db',requiredSuffixes=['','.index','.dbtype'])]
        for suffix in ['', '.index']:
            (self.root/('db'+suffix)).write_text('fixture')
        result=self.get()
        self.assertFalse(result['ready'])
        self.assertEqual(result['diagnostics'][0]['path'],str(self.root/'db.dbtype'))
        (self.root/'db.dbtype').write_bytes(b'\0\0\0\0')
        self.assertTrue(self.get()['ready'])
        self.assertFalse((self.root/'db.idx').exists())

    def test_version_parameters_index_and_file_metadata_invalidate(self):
        before=self.get()['profileFingerprint']
        for key,value in [('release','db-new'),('prepareVersion','runner-new'),('searchVersion','tool-new')]:
            old=self.profile[key]
            self.profile[key]=value
            self.assertNotEqual(before,self.get()['profileFingerprint'])
            self.profile[key]=old
        self.profile['searchParameters']['jackhmmer_n_cpu']=4
        self.assertNotEqual(before,self.get()['profileFingerprint'])
        self.profile['searchParameters']['jackhmmer_n_cpu']=2
        os.utime(self.root/'seq.fa',ns=(1234000000,1234000000))
        self.assertNotEqual(before,self.get()['profileFingerprint'])

    def test_no_whole_file_read_or_recursive_scan(self):
        original=Path.open
        reads=[]
        class Guard:
            def __init__(self,handle): self.handle=handle
            def __enter__(self): return self
            def __exit__(self,*args): self.handle.close()
            def read(self,n=-1):
                reads.append(n)
                if n != 1: raise AssertionError('preflight attempted a full-file read')
                return self.handle.read(n)
        def guarded(path,*args,**kwargs): return Guard(original(path,*args,**kwargs))
        with patch.object(Path,'open',guarded),patch.object(Path,'rglob',side_effect=AssertionError('recursive scan')):
            self.assertTrue(self.get()['ready'])
        self.assertEqual(reads,[1])

    def test_relative_registry_root_and_environment(self):
        profile=copy.deepcopy(self.profile)
        profile['root']='.'
        config_path=self.root/'registry.json'
        config_path.write_text(json.dumps({'profiles':{'tiny':profile}}))
        with patch.dict(os.environ,{'PROTEIN_STUDIO_DATABASE_CONFIG':str(config_path)}):
            result=databases.get_profile('tiny')
        self.assertTrue(result['ready'])
        self.assertEqual(result['root'],str(self.root))

    def test_example_and_undeclared_versions_block(self):
        self.profile['example']=True
        self.profile['searchVersion']='REPLACE_ME'
        self.profile['release']='test-db-REPLACE_SNAPSHOT'
        result=self.get()
        self.assertFalse(result['ready'])
        self.assertEqual({d['code'] for d in result['diagnostics']},{'example-configuration','undeclared-version'})

    def test_bad_schema_and_unknown_profile(self):
        with self.assertRaises(KeyError): databases.get_profile('unknown',self.config)
        with self.assertRaises(ValueError): databases.list_profiles({'profiles':[]})
        self.profile['assets'][0]['path']='https://example.invalid/db'
        with self.assertRaises(ValueError): self.get()
        self.profile['assets'][0]['path']='seq.fa'
        self.profile['assets'].append(copy.deepcopy(self.profile['assets'][0]))
        with self.assertRaises(ValueError): self.get()

    def test_default_examples_are_not_claimed_ready(self):
        profiles=databases.list_profiles(databases.DEFAULT_CONFIG)
        self.assertTrue(next(p for p in profiles if p['id']=='reference')['ready'])
        self.assertTrue(all(not p['ready'] for p in profiles if p['engine']!='reference'))


if __name__=='__main__': unittest.main()
