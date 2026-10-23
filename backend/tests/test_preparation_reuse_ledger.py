"""Preparation reuse accounting only; synthetic metadata is not GPU evidence."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import sys
import unittest
from unittest.mock import patch

from backend.engine import validate_submission
from backend.campaigns import CampaignStore
from backend.worker_pipeline import PipelineScheduler
from backend.tests.test_pipeline import ControlledStages, spec, submission, wait_for


def reuse(hit=False):
    return dict(strategy='af3-chain-reuse-v1', totalChains=3, uniqueChains=2,
                newChainSearches=0 if hit else 1, chainCacheHits=0 if hit else 1,
                duplicateChainCopies=1, wholeTargetCacheHit=hit,
                entries=[dict(chainIds=['A', 'C'], sequenceSha256='a' * 64,
                              cacheKey='b' * 64, cacheHit=True, residueCount=10)])


class PreparationReuseLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'campaigns.sqlite3'
        self.store = CampaignStore(self.path)
        self.cid = self.store.create(spec())['id']

    def tearDown(self):
        self.temp.cleanup()

    def append(self, name):
        return self.store.append(self.cid, [dict(targetId=name, submission=submission())])['items'][0]

    def test_counts_are_latest_published_target_metadata_and_list_is_compact(self):
        for name, hit in [('assembled', False), ('whole-hit', True)]:
            self.append(name)
            claim = self.store.claim('prepare')
            self.assertTrue(self.store.advance(claim['id'], claim['token'], 'ready',
                            prepared_path='prepared', cache_hit=hit, preparation_reuse=reuse(hit)))
        self.append('old-reference')
        claim = self.store.claim('prepare')
        self.store.advance(claim['id'], claim['token'], 'ready', cache_hit=True)
        rows = self.store.items(self.cid)['items']
        self.assertNotIn('entries', rows[0]['preparationReuse'])
        self.assertNotIn('preparationReuse', rows[2])
        self.assertEqual(self.store.item(self.cid, rows[0]['id'])['preparationReuse']['entries'], reuse()['entries'])
        # Polling must remain independent of potentially large/inaccessible files.
        actual = CampaignStore(self.path).get(self.cid)['metrics']
        self.assertEqual(actual['cacheHits'], 2)
        self.assertEqual(actual['preparationReuse'], dict(targets=2, totalChains=6,
            uniqueChainOccurrences=4, newChainSearches=1, chainCacheHits=1,
            duplicateChainCopies=2, wholeTargetCacheHits=1))

    def test_late_attempt_cannot_publish_reuse_or_change_counters(self):
        item = self.append('lease')
        stale = self.store.claim('prepare', now=100, lease_seconds=1)
        self.store.recover(now=102)
        fresh = self.store.claim('prepare', now=102, lease_seconds=10)
        self.assertFalse(self.store.advance(item['id'], stale['token'], 'ready',
                         preparation_reuse=reuse(), now=103))
        self.assertEqual(self.store.get(self.cid)['metrics']['preparationReuse']['targets'], 0)
        self.assertTrue(self.store.advance(item['id'], fresh['token'], 'ready',
                        preparation_reuse=reuse(True), now=103))
        self.assertEqual(self.store.get(self.cid)['metrics']['preparationReuse']['wholeTargetCacheHits'], 1)

    def test_invalid_counter_metadata_rolls_back_stage_publication(self):
        self.append('invalid')
        claim = self.store.claim('prepare')
        for mutate in [lambda r: r.update(newChainSearches=2),
                       lambda r: r.update(uniqueChains=4),
                       lambda r: r.update(newChainSearches=True),
                       lambda r: r.update(wholeTargetCacheHit=True)]:
            metadata = copy.deepcopy(reuse())
            mutate(metadata)
            with self.assertRaises(ValueError):
                self.store.advance(claim['id'], claim['token'], 'ready', preparation_reuse=metadata)
            self.assertEqual(self.store.item(self.cid, claim['id'])['stage'], 'preparing')

    def test_existing_ledger_migrates_without_inventing_chain_statistics(self):
        item = self.append('legacy')
        with sqlite3.connect(self.path) as conn:
            conn.execute('ALTER TABLE campaign_items DROP COLUMN preparation_reuse')
        migrated = CampaignStore(self.path)
        self.assertNotIn('preparationReuse', migrated.item(self.cid, item['id']))
        self.assertEqual(migrated.get(self.cid)['metrics']['preparationReuse']['targets'], 0)

    def test_advanced_af3_inputs_are_rejected_before_scientific_fields_are_dropped(self):
        base = dict(engine='alphafold3', chains=[dict(id='A', sequence='AG')])
        for field, value in [('unpairedMsa', '>query\nAG\n'), ('pairedMsa', ''),
                             ('templates', []), ('modifications', [])]:
            job = copy.deepcopy(base)
            job['chains'][0][field] = value
            with self.assertRaisesRegex(ValueError, 'automatic preparation'):
                validate_submission(job)
        for field in ('sequences', 'userCCD', 'userCCDPath', 'bondedAtomPairs'):
            with self.assertRaisesRegex(ValueError, 'separate adapter'):
                validate_submission(dict(base, **{field: []}))

    def test_scheduler_publishes_descriptor_metadata_with_fenced_ready_transition(self):
        class ReuseStages(ControlledStages):
            def prepare_stage(self, *args):
                return dict(super().prepare_stage(*args), chainReuse=reuse())
        stages = ReuseStages()
        scheduler = PipelineScheduler(self.temp.name, stages, prep_workers=1, gpu_workers=1, ready_cap=1)
        try:
            cid = scheduler.store.create(spec())['id']
            item = scheduler.store.append(cid, [dict(targetId='target', submission=submission())])['items'][0]
            scheduler.start()
            self.assertTrue(stages.prediction_started.wait(3))
            detail = scheduler.store.item(cid, item['id'])
            self.assertEqual(detail['preparationReuse'], reuse())
            stages.release_prediction.set()
            wait_for(lambda: scheduler.store.get(cid)['counts']['completed'] == 1)
            self.assertEqual(scheduler.store.get(cid)['metrics']['preparationReuse']['newChainSearches'], 1)
        finally:
            stages.release_prediction.set()
            scheduler.close()

    def test_actual_chain_preparation_flows_through_scheduler_and_ledger(self):
        from backend.tests.test_chain_reuse import RUNNER
        root = Path(self.temp.name)
        runner, counter = root / 'search.py', root / 'searches.txt'
        runner.write_text(RUNNER)
        profile = dict(id='synthetic-search', engine='alphafold3', ready=True,
                       profileFingerprint='fixture-v1', prepareVersion='fixture-runner-v1')
        environment = dict(PROTEIN_STUDIO_PREDICTION_MODE='workers', CHAIN_COUNTER=str(counter),
                           CHAIN_DELAY='0', CHAIN_BAD='', CHAIN_FAIL_SEQ='', CHAIN_SLEEP_SEQ='',
                           PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND=json.dumps(
                               [sys.executable, str(runner), '{input_json}', '{output_dir}']))
        with patch.dict(os.environ, environment), patch('backend.databases.get_profile', return_value=profile):
            scheduler = PipelineScheduler(root / 'actual-preparation', prep_workers=2, ready_cap=8)
            # Isolate CPU preparation from native GPU execution. No worker is
            # created; this predicate is a controlled admission fixture.
            scheduler.preparation_router = lambda conn, now: ('1', [])
            configuration = dict(spec(), engine='alphafold3', databaseProfile=profile)
            cid = scheduler.store.create(configuration)['id']
            jobs = [dict(targetId=f'{i}-{j}', submission=dict(engine='alphafold3',
                         name='complex', seeds=[1], numSamples=5,
                         chains=[dict(id='A', sequence=left), dict(id='B', sequence=right)]))
                    for i, left in enumerate(('AG', 'VC')) for j, right in enumerate(('LK', 'FT', 'DE'))]
            scheduler.store.append(cid, jobs)
            scheduler.start()
            try:
                wait_for(lambda: scheduler.store.get(cid)['counts']['ready'] == 6)
                stats = scheduler.store.get(cid)['metrics']['preparationReuse']
                self.assertEqual(stats, dict(targets=6, totalChains=12, uniqueChainOccurrences=12,
                    newChainSearches=5, chainCacheHits=7, duplicateChainCopies=0, wholeTargetCacheHits=0))
                self.assertCountEqual(counter.read_text().splitlines(), ['AG', 'VC', 'LK', 'FT', 'DE'])
                for item in scheduler.store.items(cid)['items']:
                    detail = scheduler.store.item(cid, item['id'])
                    self.assertEqual(len(detail['preparationReuse']['entries']), 2)
                    row = scheduler.store.item(cid, item['id'], internal=True)
                    descriptor = json.loads(Path(row['prepared_path']).read_text())
                    self.assertEqual(detail['preparationReuse'], descriptor['chainReuse'])
                    self.assertTrue(Path(descriptor['inputPath']).is_file())
                self.assertEqual(scheduler.store.get(cid)['counts']['predicting'], 0)
            finally:
                scheduler.close()


if __name__ == '__main__':
    unittest.main()
