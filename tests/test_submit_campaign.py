import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from scripts import submit_campaign as cli


class FakeAPI:
    def __init__(self):
        self.campaign=None
        self.items={}
        self.calls=[]
        self.chunk_sizes=[]
    def __call__(self,server,path,payload=None):
        self.calls.append((path,copy.deepcopy(payload)))
        if path=='/api/campaigns':
            self.campaign=copy.deepcopy(payload)
            self.campaign.update(id='a'*32,sealed=False)
            self.campaign['databaseProfile']={'id':payload['databaseProfile']}
            return copy.deepcopy(self.campaign)
        if payload is None: return copy.deepcopy(self.campaign)
        action=path.rsplit('/',1)[-1]
        if action in ('pause','resume','seal'):
            if action=='seal': self.campaign['sealed']=True
            else: self.campaign['status']='paused' if action=='pause' else 'active'
            return copy.deepcopy(self.campaign)
        if action=='items':
            incoming=payload['items']; self.chunk_sizes.append(len(incoming))
            for item in incoming:
                prior=self.items.get(item['targetId'])
                if prior and prior['payload']!=item: raise RuntimeError('HTTP 409 conflicting target')
                if not prior and self.campaign['sealed']: raise RuntimeError('HTTP 409 sealed campaign')
            returned=[]; inserted=0
            for item in incoming:
                if item['targetId'] not in self.items:
                    self.items[item['targetId']]={'payload':item,'id':f'item{len(self.items)}'}
                    inserted+=1
                returned.append({'id':self.items[item['targetId']]['id'],'targetId':item['targetId']})
            return dict(items=returned,count=len(incoming),inserted=inserted,duplicates=len(incoming)-inserted,totalItems=len(self.items))
        raise AssertionError(path)


class CampaignCLITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.fasta=self.root/'candidates.fa'
        self.fasta.write_text('>candidate one\nACDE\n>candidate two\nACDE\n>candidate one\nACDE\n')
        self.manifest=self.root/'accepted.jsonl'
        self.api=FakeAPI()
    def args(self,extra=None):
        return cli.parser().parse_args(['--input',str(self.fasta),'--engine','reference','--manifest',str(self.manifest)]+(extra or []))
    def runcli(self,extra=None):
        with patch.object(cli,'request_json',self.api):return cli.run(self.args(extra))
    def test_ids_match_ui_and_keep_distinct_candidates(self):
        self.assertEqual(cli.target_id(1,'candidate one'),'t-'+hashlib.sha256(b'1\0candidate one').hexdigest()[:24])
        result=self.runcli()
        self.assertEqual(result['inserted'],3)
        self.assertEqual(len(self.api.items),3)
        self.assertEqual(len({r['payload']['chains'][0]['sequence'] for r in self.api.items.values()}),1)
    def test_dry_run_no_network_no_manifest(self):
        with patch.object(cli,'request_json',side_effect=AssertionError('network')):
            result=cli.run(self.args(['--dry-run']))
        self.assertEqual(result['targets'],3)
        self.assertFalse(self.manifest.exists())
    def test_205_streamed_in_chunks_and_replay_idempotent(self):
        self.fasta.write_text(''.join(f'>candidate {i}\nACDE\n' for i in range(205)))
        first=self.runcli()
        self.assertEqual(self.api.chunk_sizes,[100,100,5])
        second=self.runcli(['--campaign-id',first['campaignId']])
        self.assertEqual(second['inserted'],0)
        self.assertEqual(second['duplicates'],205)
        self.assertEqual(len(self.api.items),205)
        events=[json.loads(line) for line in self.manifest.read_text().splitlines()]
        chunks=[x for x in events if x['event']=='chunk-accepted']
        self.assertEqual(len(chunks),6)
        self.assertEqual(chunks[0]['firstOrdinal'],1)
        self.assertEqual(chunks[2]['lastOrdinal'],205)
    def test_default_seals_then_runs(self):
        self.runcli()
        actions=[p.rsplit('/',1)[-1] for p,_ in self.api.calls]
        self.assertLess(actions.index('seal'),actions.index('resume'))
        self.assertTrue(self.api.campaign['sealed'])
    def test_streaming_starts_after_first_chunk(self):
        self.runcli(['--chunk-size','1','--start-streaming'])
        actions=[p.rsplit('/',1)[-1] for p,_ in self.api.calls]
        self.assertEqual(actions[:5],['campaigns','pause','items','resume','items'])
    def test_parse_failure_pauses_without_sealing(self):
        self.fasta.write_text('>first\nACDE\n>second\nACDE\n>bad\nBAD?\n')
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(ValueError):self.runcli(['--chunk-size','1','--start-streaming'])
        self.assertEqual(self.api.campaign['status'],'paused')
        self.assertFalse(self.api.campaign['sealed'])
        self.assertEqual(len(self.api.items),2)
    def test_changed_file_detected_before_replay(self):
        result=self.runcli()
        self.fasta.write_text('>changed\nACDE\n')
        with self.assertRaisesRegex(ValueError,'inputSha256 differs'):
            self.runcli(['--campaign-id',result['campaignId']])
    def test_config_conflict_and_duplicate_new_import(self):
        result=self.runcli()
        with self.assertRaisesRegex(ValueError,'samples conflicts'):
            self.runcli(['--campaign-id',result['campaignId'],'--samples','2'])
        with self.assertRaisesRegex(ValueError,'Manifest already exists'):self.runcli()
    def test_limit_and_leave_paused(self):
        result=self.runcli(['--limit','2','--leave-paused'])
        self.assertEqual(result['inserted'],2)
        self.assertFalse(result['running'])
        self.assertEqual(self.api.campaign['status'],'paused')
    def test_reject_invalid_settings_and_execution_commands(self):
        with self.assertRaises(ValueError):self.runcli(['--chunk-size','101'])
        with self.assertRaises(ValueError):self.runcli(['--recycles','0'])
        with self.assertRaises(Exception):cli.execution_profile('{"command":"rm"}')
    def test_af3_defaults_and_placement_match_resident_campaign_contract(self):
        args=self.args(['--engine','alphafold3','--database-profile','af3-real','--placement','aliyun'])
        value=cli.settings(args)
        self.assertEqual(value['settings']['numRecycles'],10)
        self.assertEqual(value['settings']['numSamples'],5)
        self.assertEqual(value['placement'],'aliyun')
    def test_resume_preserves_placement_and_rejects_conflicting_route(self):
        result=self.runcli(['--placement','local'])
        self.runcli(['--campaign-id',result['campaignId']])
        self.assertEqual(self.api.campaign['placement'],'local')
        with self.assertRaisesRegex(ValueError,'placement conflicts'):
            self.runcli(['--campaign-id',result['campaignId'],'--placement','aliyun'])

    def test_fixed_target_builds_one_complex_per_candidate_and_replays(self):
        fixed=self.root/'fixed.fa';fixed.write_text('>target\nFGHI\n>target partner\nKLMN\n')
        first=self.runcli(['--fixed-target',str(fixed),'--chunk-size','2'])
        self.assertEqual((first['inserted'],first['fixedTargetChains']),(3,2))
        self.assertEqual(self.api.chunk_sizes,[2,1])
        for row in self.api.items.values():
            self.assertEqual(row['payload']['chains'],[
                {'id':'A','sequence':'FGHI'},{'id':'B','sequence':'KLMN'},{'id':'C','sequence':'ACDE'}])
        replay=self.runcli(['--fixed-target',str(fixed),'--campaign-id',first['campaignId']])
        self.assertEqual((replay['inserted'],replay['duplicates']),(0,3))
        identities=[json.loads(line) for line in self.manifest.read_text().splitlines() if json.loads(line)['event'] in ('created','attached')]
        self.assertEqual(identities[0]['constructionStrategy'],'fixed-target-plus-candidate-v1')
        self.assertEqual(identities[0]['fixedTargetChainsSha256'],identities[1]['fixedTargetChainsSha256'])

    def test_changed_fixed_target_blocks_before_any_post_even_unchanged_candidates(self):
        fixed=self.root/'fixed.fa';fixed.write_text('>target\nFGHI\n')
        result=self.runcli(['--fixed-target',str(fixed)])
        candidate_bytes=self.fasta.read_bytes()
        fixed.write_text('>target\nKLMN\n')
        self.api.calls.clear()
        with self.assertRaisesRegex(ValueError,'fixedTargetSha256 differs'):
            self.runcli(['--fixed-target',str(fixed),'--campaign-id',result['campaignId']])
        self.assertEqual(self.fasta.read_bytes(),candidate_bytes)
        self.assertTrue(all(payload is None for _,payload in self.api.calls))

    def test_fixed_header_change_keeps_normalized_sha_but_raw_identity_is_strict(self):
        fixed=self.root/'fixed.fa';fixed.write_text('>old title\nFGHI\n')
        result=self.runcli(['--fixed-target',str(fixed)])
        fixed.write_text('>new title\nfghi\n')
        with self.assertRaisesRegex(ValueError,'fixedTargetSha256 differs'):
            self.runcli(['--fixed-target',str(fixed),'--campaign-id',result['campaignId']])

    def test_adding_removing_fixed_target_or_missing_original_manifest_is_rejected(self):
        fixed=self.root/'fixed.fa';fixed.write_text('>target\nFGHI\n')
        result=self.runcli()
        self.api.calls.clear()
        with self.assertRaisesRegex(ValueError,'constructionStrategy differs'):
            self.runcli(['--fixed-target',str(fixed),'--campaign-id',result['campaignId']])
        self.assertTrue(all(payload is None for _,payload in self.api.calls))
        self.manifest.unlink()
        with self.assertRaisesRegex(ValueError,'original manifest'):
            self.runcli(['--fixed-target',str(fixed),'--campaign-id',result['campaignId']])

    def test_legacy_single_fasta_manifest_remains_compatible(self):
        result=self.runcli()
        events=[json.loads(line) for line in self.manifest.read_text().splitlines()]
        added=('inputFormat','constructionStrategy','chainIdPolicy','fixedTargetSha256','fixedTargetChainsSha256')
        for event in events:
            for key in added:event.pop(key,None)
        self.manifest.write_text(''.join(json.dumps(x)+'\n' for x in events))
        replay=self.runcli(['--campaign-id',result['campaignId']])
        self.assertEqual(replay['duplicates'],3)

    def test_jsonl_streams_multichain_settings_and_idempotent_chunks(self):
        self.fasta=self.root/'complexes.jsonl'
        with self.fasta.open('w') as stream:
            for i in range(205):
                stream.write(json.dumps(dict(targetId=f'complex-{i}',name=f'complex {i}',
                    chains=[dict(id='T',sequence='FGHI'),dict(id='C',sequence='ACDE')],
                    settings=dict(seeds=[7,9],numSamples=2,numRecycles=6),priority='high'))+'\n')
        first=self.runcli(['--engine','alphafold3','--database-profile','af3-test'])
        self.assertEqual(self.api.chunk_sizes,[100,100,5])
        self.assertEqual(self.api.items['complex-0']['payload']['settings']['numRecycles'],6)
        self.assertEqual(self.api.items['complex-0']['payload']['priority'],'high')
        replay=self.runcli(['--engine','alphafold3','--database-profile','af3-test','--campaign-id',first['campaignId']])
        self.assertEqual(replay['duplicates'],205)

    def test_jsonl_late_invalid_scientific_field_pauses_without_sealing(self):
        self.fasta=self.root/'complexes.jsonl'
        valid={'chains':[{'id':'A','sequence':'ACDE'}]}
        bad={'chains':[{'id':'A','sequence':'ACDE','templates':[]}]}
        self.fasta.write_text(json.dumps(valid)+'\n'+json.dumps(bad)+'\n')
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(ValueError,'Scientific chain fields'):
                self.runcli(['--chunk-size','1','--start-streaming'])
        self.assertEqual(len(self.api.items),1)
        self.assertEqual(self.api.campaign['status'],'paused')
        self.assertFalse(self.api.campaign['sealed'])

    def test_fixed_plus_jsonl_and_extension_conflicts_fail_before_network(self):
        fixed=self.root/'fixed.fa';fixed.write_text('>target\nFGHI\n')
        self.fasta=self.root/'complexes.jsonl';self.fasta.write_text('{"sequence":"ACDE"}\n')
        with self.assertRaisesRegex(ValueError,'candidate FASTA'):
            self.runcli(['--fixed-target',str(fixed)])
        self.fasta.write_text('>candidate\nACDE\n')
        with self.assertRaisesRegex(ValueError,'extension indicates'):
            self.runcli()
        self.assertEqual(self.api.calls,[])

    def test_dry_run_exact_complex_sizes_and_limit(self):
        fixed=self.root/'fixed.fa';fixed.write_text('>target\nFGHI\n>partner\nKLMN\n')
        result=self.runcli(['--fixed-target',str(fixed),'--dry-run','--limit','2'])
        self.assertEqual((result['combinations'],result['totalChains'],result['totalResidues']),(2,6,24))
        self.assertEqual(result['fixedTargetResidues'],8)
        self.assertEqual(result['examples'][0]['chainCount'],3)
        self.assertFalse(self.manifest.exists())
        self.assertEqual(self.api.calls,[])

    def test_100000_candidates_submit_with_only_one_chunk_retained(self):
        with self.fasta.open('w') as stream:
            for index in range(100000):
                stream.write(f'>candidate {index}\nACDE\n')
        fixed=self.root/'fixed.fa';fixed.write_text('>target\nFGHI\n')
        campaign=None
        accepted=largest=0
        def bounded_api(server,path,payload=None):
            nonlocal campaign,accepted,largest
            if path=='/api/campaigns':
                campaign=dict(payload,id='b'*32,databaseProfile={'id':payload['databaseProfile']})
                return campaign
            if path.endswith('/items'):
                rows=payload['items'];largest=max(largest,len(rows))
                returned=[{'targetId':row['targetId'],'id':f'item-{accepted+i}'} for i,row in enumerate(rows)]
                accepted+=len(rows)
                return dict(items=returned,count=len(rows),inserted=len(rows),duplicates=0)
            return campaign
        tracemalloc.start()
        try:
            with patch.object(cli,'request_json',bounded_api):
                result=cli.run(self.args(['--fixed-target',str(fixed)]))
            _,peak=tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual((accepted,largest,result['chunks']),(100000,100,1000))
        self.assertLess(peak,8*1024*1024,f'Peak Python allocations: {peak}')
        self.assertGreater(self.manifest.stat().st_size,100000)


if __name__=='__main__':unittest.main()
