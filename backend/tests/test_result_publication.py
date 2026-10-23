"""Compact publication/GC regressions. Synthetic files; no GPU or real cleanup."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.cache_lifecycle import CacheLifecycle, MAX_JSON
from backend.campaigns import CampaignStore, publication_file
from backend.worker_pipeline import PipelineScheduler, write_json
from backend.tests.test_pipeline import spec, submission


class ResultPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.pipeline = PipelineScheduler(self.root)
        self.addCleanup(self.pipeline.close)
        self.store = self.pipeline.store
        self.campaign = self.store.create(spec())
        self.cid = self.campaign['id']
        self.manager = CacheLifecycle(self.root)
        self.cache_key = hashlib.sha256(b'prepared fixture').hexdigest()
        directory = self.manager.cache_root/self.cache_key
        directory.mkdir()
        (directory/'input.json').write_text('{}')
        (directory/'COMPLETE.json').write_text(json.dumps({'schema': 1, 'cacheKey': self.cache_key,
            'inputRelativePath': 'input.json', 'artifacts': [{'name': 'input.json', 'bytes': 2,
            'sha256': hashlib.sha256(b'{}').hexdigest()}]}))

    def validation(self, sequence='A'):
        self.store.append(self.cid, [{'targetId': 'one', 'submission': submission(sequence)}])
        claim = self.store.claim('prepare')
        self.item_id = claim['id']
        self.item_root = self.root/'campaigns'/self.cid/self.item_id
        self.item_root.mkdir(parents=True)
        self.prepared = self.item_root/'prepared.json'
        self.prepared.write_text('{}')
        self.store.advance(claim['id'], claim['token'], 'ready', prepared_path=str(self.prepared), preparation_cache_key=self.cache_key)
        claim = self.store.claim('predict')
        self.store.advance(claim['id'], claim['token'], 'validating', candidate_path=str(self.item_root/'candidate.json'))
        return self.store.claim('validate')

    def output(self, large=False):
        structure = self.item_root/'model.cif'
        structure.write_text('data_fixture\n')
        result = self.item_root/'result.json'
        value = {'structureFile': 'model.cif', 'chains': [{'id': 'A', 'sequence': 'A'}],
                 'artifacts': [{'name': 'model.cif', 'size': structure.stat().st_size}]}
        if large:value['padding'] = 'x'*(MAX_JSON+10)
        write_json(result, value)
        return result, structure, {'schema': 1, 'result': publication_file(self.item_root, result),
                                   'structure': publication_file(self.item_root, structure)}

    def publish(self, claim, result, publication):
        return self.store.advance(claim['id'], claim['token'], 'completed', result_path=str(result), result_publication=publication)

    def cache_protected(self):
        scan = self.manager.scan()
        self.assertFalse(scan['failClosed'])
        return self.manager.entries(scan['id'])['items'][0]['protected']

    def test_large_result_gc_uses_atomic_index_without_reading_result_json(self):
        claim = self.validation(); result, _, publication = self.output(large=True)
        self.assertTrue(self.publish(claim, result, publication))
        row = self.store.item(self.cid, claim['id'], internal=True)
        stored = json.loads(row['result_publication'])
        self.assertEqual(row['stage'], 'completed')
        self.assertEqual(stored['itemId'], claim['id']); self.assertEqual(stored['attempt'], claim['attempt'])
        self.assertEqual(stored['inputHash'], row['input_hash'])
        self.assertLess(len(row['result_publication'].encode()), 4096)
        original = self.manager._read_json
        def read(path):
            self.assertNotEqual(path, result, 'GC opened the large result JSON')
            return original(path)
        with patch.object(self.manager, '_read_json', side_effect=read):
            self.assertFalse(self.cache_protected())

    def test_publication_failure_rolls_back_stage_and_pointer(self):
        claim = self.validation(); result, _, publication = self.output()
        publication['structure']['bytes'] += 1
        with self.assertRaises(ValueError):self.publish(claim, result, publication)
        row = self.store.item(self.cid, claim['id'], internal=True)
        self.assertEqual(row['stage'], 'validating'); self.assertEqual(row['token'], claim['token'])
        self.assertIsNone(row['result_path']); self.assertIsNone(row['result_publication'])

    def test_expired_attempt_cannot_publish_index_and_valid_replacement_can(self):
        claim = self.validation(); result, _, publication = self.output()
        self.store.recover(all_running=True)
        replacement = self.store.claim('validate')
        self.assertFalse(self.publish(claim, result, publication))
        self.assertTrue(self.publish(replacement, result, publication))
        stored = json.loads(self.store.item(self.cid, claim['id'], internal=True)['result_publication'])
        self.assertEqual(stored['attempt'], replacement['attempt'])

    def test_unexpected_stage_and_forged_identity_are_rejected(self):
        claim = self.validation(); result, _, publication = self.output()
        publication['itemId'] = '0'*32
        with self.assertRaises(ValueError):self.publish(claim, result, publication)
        del publication['itemId']
        with self.assertRaises(ValueError):
            self.store.advance(claim['id'], claim['token'], 'failed', result_path=str(result), result_publication=publication)
        self.assertIsNone(self.store.item(self.cid, claim['id'], internal=True)['result_publication'])

    def test_changed_or_missing_completed_file_pins_cache(self):
        claim = self.validation(); result, structure, publication = self.output()
        self.assertTrue(self.publish(claim, result, publication))
        structure.write_text('data_replaced\n')
        self.assertTrue(self.cache_protected())
        structure.unlink()
        self.assertTrue(self.cache_protected())

    def test_unknown_or_incomplete_index_never_falls_back_to_readable_small_json(self):
        claim = self.validation(); result, _, publication = self.output()
        self.assertTrue(self.publish(claim, result, publication))
        for broken in [{'schema': 999}, publication, {'schema': 1, 'itemId': 'wrong'}]:
            with self.subTest(broken=broken):
                with self.store.connection(True) as conn:
                    conn.execute('UPDATE campaign_items SET result_publication=? WHERE id=?', (json.dumps(broken), claim['id']))
                original = self.manager._read_json
                def read(path):
                    self.assertNotEqual(path, result, 'Bad index used legacy fallback')
                    return original(path)
                with patch.object(self.manager, '_read_json', side_effect=read):
                    self.assertTrue(self.cache_protected())

    def test_legacy_null_small_result_releases_but_large_stays_pinned(self):
        claim = self.validation(); result, _, _ = self.output()
        self.assertTrue(self.publish(claim, result, None))
        self.assertFalse(self.cache_protected())
        self.output(large=True)
        self.assertTrue(self.cache_protected())

    def test_symlink_escape_and_empty_structure_are_rejected(self):
        claim = self.validation(); result, structure, publication = self.output()
        structure.unlink()
        outside = self.root/'outside.cif'; outside.write_text('external')
        structure.symlink_to(outside)
        with self.assertRaises(ValueError):publication_file(self.item_root, structure)
        with self.assertRaises(ValueError):self.publish(claim, result, publication)
        structure.unlink(); structure.write_text('')
        with self.assertRaises(ValueError):publication_file(self.item_root, structure)
        unsafe = copy.deepcopy(publication); unsafe['structure']['relativePath'] = '../outside.cif'
        with self.assertRaises(ValueError):self.publish(claim, result, unsafe)

    def test_retry_failed_clears_old_index_along_with_result_pointer(self):
        claim = self.validation(); result, _, publication = self.output()
        self.store.advance(claim['id'], claim['token'], 'failed', error='synthetic failure')
        with self.store.connection(True) as conn:
            conn.execute('UPDATE campaign_items SET result_path=?,result_publication=? WHERE id=?', (str(result), json.dumps(publication), claim['id']))
        self.assertEqual(self.store.retry_failed(self.cid)['retried'], 1)
        row = self.store.item(self.cid, claim['id'], internal=True)
        self.assertIsNone(row['result_path']); self.assertIsNone(row['result_publication'])
        self.assertFalse(self.publish(claim, result, publication))

    def test_large_legacy_detail_is_bounded_and_original_download_avoids_detail(self):
        claim = self.validation(); result, _, _ = self.output(large=True)
        self.assertTrue(self.publish(claim, result, None))
        with patch.object(Path, 'read_text', side_effect=AssertionError('Whole JSON read')):
            response = self.pipeline.detail(self.cid, claim['id'])
            self.assertIsNone(response['result'])
            self.assertEqual(response['resultAvailability']['status'], 'legacy-large-result')
            self.assertIn('artifact?name=result.json', response['resultJsonUrl'])
        with patch.object(self.pipeline, 'detail', side_effect=AssertionError('Artifact used detail')):
            self.assertEqual(self.pipeline.artifact(self.cid, claim['id'], 'result.json'), result)
            with self.assertRaises(ValueError):self.pipeline.artifact(self.cid, claim['id'], '__structure__')
            with self.assertRaises(ValueError):self.pipeline.artifact(self.cid, claim['id'], '../outside')

    def test_validation_externalizes_pae_rebases_sidecars_and_publishes_small_index(self):
        from backend.result_artifacts import compact_pae, read_pae
        sequence = 'A'*129
        claim = self.validation(sequence)
        prediction = self.item_root/'synthetic-predict-attempt'/'predict'
        prediction.mkdir(parents=True)
        structure = prediction/'model.cif'; structure.write_text('data_fixture\n')
        matrix = [[row + column/1000 for column in range(129)] for row in range(129)]
        candidate_value = {'structurePath': str(structure), 'chains': [{'id': 'A', 'sequence': sequence}],
                           'pae': matrix, 'residues': [], 'artifacts': [{'path': str(structure)}]}
        compact_pae(candidate_value, prediction)
        candidate = prediction/'candidate.json'; write_json(candidate, candidate_value)
        claim['candidate_path'] = str(candidate)
        self.pipeline._execute_claim(claim, 'validate')
        row = self.store.item(self.cid, claim['id'], internal=True)
        self.assertEqual(row['stage'], 'completed', row['error'])
        self.assertIsNotNone(row['result_publication'])
        self.assertLess(Path(row['result_path']).stat().st_size, 65536)
        result = self.pipeline.detail(self.cid, claim['id'])['result']
        self.assertIsNone(result['pae'])
        descriptor = result['paeArtifact']
        self.assertTrue(descriptor['storageRelativePath'].startswith('synthetic-predict-attempt/predict/'))
        self.assertNotIn('storagePath', descriptor)
        self.assertEqual(descriptor['previewUrl'], f'/api/campaigns/{self.cid}/items/{claim["id"]}/pae?maxSize=256')
        registered = {item['name'] for item in result['artifacts']}
        for stem in ('storage', 'source', 'manifest'):
            relative = descriptor[stem+'RelativePath']
            self.assertIn(relative, registered)
            self.assertEqual(self.pipeline.artifact(self.cid, claim['id'], relative), self.item_root/relative)
        tile = read_pae(descriptor, self.item_root, row_start=5, col_start=7, rows=2, cols=3)
        self.assertEqual(tile['values'], [row[7:10] for row in matrix[5:7]])
        self.assertFalse(self.cache_protected())

    def test_structure_download_uses_index_even_when_json_is_large(self):
        claim = self.validation(); result, structure, publication = self.output(large=True)
        self.assertTrue(self.publish(claim, result, publication))
        with patch.object(self.pipeline, '_read_compact_result', side_effect=AssertionError('Structure parsed JSON')):
            self.assertEqual(self.pipeline.artifact(self.cid, claim['id'], '__structure__'), structure)


if __name__ == '__main__':unittest.main()
