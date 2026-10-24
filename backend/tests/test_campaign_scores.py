"""Result score ledger tests; no real GPU, remote service, or model download."""
import json
import tempfile
import unittest
from pathlib import Path

from backend.campaigns import CampaignStore, normalize_result_scores, result_scores_from
from backend.tests.test_pipeline import submission, spec


def drive_to_validation(store, campaign_id, name, sequence='A'):
    store.append(campaign_id, [{'targetId': name, 'submission': submission(sequence, name)}])
    prepare = store.claim('prepare')
    store.advance(prepare['id'], prepare['token'], 'ready', prepared_path='/prepared.json')
    predict = store.claim('predict')
    store.advance(predict['id'], predict['token'], 'validating', candidate_path='/candidate.json')
    return store.claim('validate')


class ResultScoreLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = CampaignStore(self.root / 'campaigns.sqlite3')
        self.cid = self.store.create(spec())['id']

    def tearDown(self):
        self.temp.cleanup()

    def complete(self, name, scores=None, result_path=None, sequence='A'):
        claim = drive_to_validation(self.store, self.cid, name, sequence)
        path = result_path or str(self.root / 'unused.json')
        self.store.advance(claim['id'], claim['token'], 'completed', result_path=path, result_scores=scores)
        return claim['id']

    def test_completed_publishes_scores_and_sorts_desc_nulls_last(self):
        self.complete('low', {'rankingScore': 0.2, 'iptm': 0.5, 'ptm': 0.6, 'meanPlddt': 40.0})
        self.complete('high', {'rankingScore': 0.95, 'iptm': 0.9, 'ptm': 0.85, 'meanPlddt': 92.0,
                               'rankingMetric': '0.8 ipTM + 0.2 pTM'})
        self.complete('unscored', None)
        page = self.store.items(self.cid, sort='ranking')
        self.assertEqual([item['name'] for item in page['items']], ['high', 'low', 'unscored'])
        scores = page['items'][0]['resultScores']
        self.assertEqual(scores['rankingScore'], 0.95)
        self.assertEqual(scores['rankingMetric'], '0.8 ipTM + 0.2 pTM')
        self.assertNotIn('resultScores', page['items'][2])
        metrics = self.store.get(self.cid)['metrics']
        self.assertEqual(metrics['scoredTargets'], 2)
        self.assertEqual(metrics['iptmScoredTargets'], 2)
        self.assertEqual([item['name'] for item in self.store.items(self.cid, sort='iptm')['items']],
                         ['high', 'low', 'unscored'])
        self.assertEqual([item['name'] for item in self.store.items(self.cid, sort='plddt')['items']],
                         ['high', 'low', 'unscored'])
        detail = self.store.item(self.cid, page['items'][0]['id'])
        self.assertEqual(detail['resultScores']['rankingScore'], 0.95)

    def test_thresholds_filter_unscored_rows_and_default_order_unchanged(self):
        self.complete('gray', {'rankingScore': 0.7, 'iptm': 0.65})
        self.complete('confident', {'rankingScore': 0.95, 'iptm': 0.9})
        self.complete('plain', None)
        page = self.store.items(self.cid, min_iptm=0.8)
        self.assertEqual([item['name'] for item in page['items']], ['confident'])
        self.assertEqual(page['total'], 1)
        both = self.store.items(self.cid, min_iptm=0.6)
        self.assertEqual(both['total'], 2)
        default = self.store.items(self.cid)
        self.assertEqual([item['name'] for item in default['items']], ['gray', 'confident', 'plain'])

    def test_invalid_sort_thresholds_and_score_blocks_rejected(self):
        with self.assertRaises(ValueError):
            self.store.items(self.cid, sort='confidence')
        with self.assertRaises(ValueError):
            self.store.items(self.cid, min_iptm=1.5)
        with self.assertRaises(ValueError):
            self.store.items(self.cid, min_plddt=-1)
        with self.assertRaises(ValueError):
            self.store.items(self.cid, min_ptm=float('nan'))
        with self.assertRaises(ValueError):
            normalize_result_scores({'iptm': 2.0})
        with self.assertRaises(ValueError):
            normalize_result_scores({'unknownMetric': 1.0})
        with self.assertRaises(ValueError):
            normalize_result_scores({'rankingMetric': ''})
        self.assertEqual(normalize_result_scores({'rankingScore': -5}), {'rankingScore': -5.0})

    def test_scores_require_completed_output_and_retry_clears_them(self):
        claim = drive_to_validation(self.store, self.cid, 'one')
        with self.assertRaises(ValueError):
            self.store.advance(claim['id'], claim['token'], 'failed', result_scores={'iptm': 0.5})
        self.store.advance(claim['id'], claim['token'], 'completed',
                           result_path=str(self.root / 'r.json'), result_scores={'iptm': 0.5, 'ptm': 0.6})
        internal = self.store.item(self.cid, claim['id'], internal=True)
        self.assertEqual(internal['score_iptm'], 0.5)
        self.assertEqual(internal['score_checked'], 1)
        # retry_failed only requeues failed rows; stale scores there (for example
        # written before an upgrade) must not survive into the new attempt.
        failed = drive_to_validation(self.store, self.cid, 'two')
        self.store.advance(failed['id'], failed['token'], 'failed', error='boom')
        with self.store.connection(True) as conn:
            conn.execute('UPDATE campaign_items SET score_iptm=0.5,ranking_metric=?,score_checked=1 WHERE id=?',
                         ('stale', failed['id']))
        self.store.retry_failed(self.cid)
        cleared = self.store.item(self.cid, failed['id'], internal=True)
        self.assertIsNone(cleared['score_iptm'])
        self.assertIsNone(cleared['ranking_metric'])
        self.assertEqual(cleared['score_checked'], 0)

    def test_backfill_populates_legacy_completed_results_once(self):
        claim = drive_to_validation(self.store, self.cid, 'legacy')
        attempt = self.root / 'campaigns' / self.cid / claim['id'] / 'attempt-1'
        attempt.mkdir(parents=True)
        result_path = attempt / 'result.json'
        result_path.write_text(json.dumps({'meanPlddt': 88.4, 'ptm': 0.77, 'rankingMetric': 'reported ranking score'}))
        self.store.advance(claim['id'], claim['token'], 'completed', result_path=str(result_path))
        # Simulate a row completed before scores existed: checked flag cleared.
        with self.store.connection(True) as conn:
            conn.execute('UPDATE campaign_items SET score_checked=0 WHERE id=?', (claim['id'],))
        page = self.store.items(self.cid)
        scores = page['items'][0]['resultScores']
        self.assertEqual(scores['meanPlddt'], 88.4)
        self.assertEqual(scores['ptm'], 0.77)
        self.assertEqual(scores['rankingMetric'], 'reported ranking score')
        internal = self.store.item(self.cid, claim['id'], internal=True)
        self.assertEqual(internal['score_checked'], 1)
        again = self.store.items(self.cid)
        self.assertEqual(again['items'][0]['resultScores']['meanPlddt'], 88.4)

    def test_backfill_marks_unreadable_legacy_results_checked_without_scores(self):
        claim = drive_to_validation(self.store, self.cid, 'gone')
        self.store.advance(claim['id'], claim['token'], 'completed',
                           result_path=str(self.root / 'campaigns' / self.cid / claim['id'] / 'missing.json'))
        with self.store.connection(True) as conn:
            conn.execute('UPDATE campaign_items SET score_checked=0 WHERE id=?', (claim['id'],))
        page = self.store.items(self.cid)
        self.assertNotIn('resultScores', page['items'][0])
        internal = self.store.item(self.cid, claim['id'], internal=True)
        self.assertEqual(internal['score_checked'], 1)

    def test_extraction_skips_out_of_range_and_non_numeric_values(self):
        result = {'rankingScore': 1.2, 'iptm': 1.5, 'ptm': None, 'meanPlddt': 55.5,
                  'rankingMetric': 'x' * 65, 'structureFormat': 'pdb'}
        self.assertEqual(result_scores_from(result), {'rankingScore': 1.2, 'meanPlddt': 55.5})
        self.assertIsNone(result_scores_from({'structureFormat': 'pdb'}))

    def test_get_reports_confidence_band_counts(self):
        self.complete('vh', {'iptm': 0.9, 'meanPlddt': 95.0})
        self.complete('gray', {'iptm': 0.7, 'meanPlddt': 80.0})
        self.complete('low', {'iptm': 0.4, 'meanPlddt': 40.0})
        self.complete('plddtOnly', {'meanPlddt': 55.0})
        self.complete('plain', None)
        bands = self.store.get(self.cid)['metrics']['scoreBands']
        self.assertEqual(bands['iptm'], {'high': 1, 'gray': 1, 'low': 1})
        self.assertEqual(bands['plddt'], {'veryHigh': 1, 'confident': 1, 'low': 1, 'veryLow': 1})

    def complete_with_result_file(self, name, scores):
        claim = drive_to_validation(self.store, self.cid, name)
        attempt = self.store.path.parent / 'campaigns' / self.cid / claim['id'] / 'attempt-1'
        attempt.mkdir(parents=True)
        result_path = attempt / 'result.json'
        result_path.write_text(json.dumps(scores))
        self.store.advance(claim['id'], claim['token'], 'completed',
                           result_path=str(result_path), result_scores=scores)
        return claim['id']

    def test_backfill_scores_paged_drives_legacy_rows_to_completion(self):
        for i in range(3):
            self.complete_with_result_file(f'legacy{i}', {'meanPlddt': 40.0 + 10 * i})
        with self.store.connection(True) as conn:
            conn.execute('UPDATE campaign_items SET score_checked=0, score_plddt=NULL WHERE campaign_id=?',
                         (self.cid,))
        self.assertEqual(self.store.backfill_scores(self.cid, page_size=2), 3)
        page = self.store.items(self.cid, sort='plddt')
        self.assertEqual([item['name'] for item in page['items']], ['legacy2', 'legacy1', 'legacy0'])
        # A clean campaign exits after the count query without visiting rows.
        self.assertEqual(self.store.backfill_scores(self.cid, page_size=2), 0)


class CampaignScoreHTTPTests(unittest.TestCase):
    def setUp(self):
        import threading
        from backend.server import StudioApplication, StudioHTTPServer
        from backend.tests.test_api import FakeEngine
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.web = self.root / 'web'
        self.web.mkdir()
        (self.web / 'index.html').write_text('<!doctype html><title>Test</title>')
        self.app = StudioApplication(self.root / 'data', self.web, FakeEngine())
        self.httpd = StudioHTTPServer(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        # No app.start(): these routes are served fine without pipeline workers,
        # and started workers would race the manual store.claim() calls below.
        self.base = f'http://127.0.0.1:{self.httpd.server_port}'

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.close()
        self.thread.join(1)
        self.temp.cleanup()

    def request(self, path):
        import urllib.request
        with urllib.request.urlopen(self.base + path) as response:
            return json.loads(response.read())

    def test_items_route_score_query(self):
        import urllib.error
        campaign = self.app.pipeline.store.create(spec())
        cid = campaign['id']
        store = self.app.pipeline.store
        for name, iptm in (('weak', 0.3), ('strong', 0.9)):
            claim = drive_to_validation(store, cid, name)
            store.advance(claim['id'], claim['token'], 'completed',
                          result_path=str(self.root / (name + '.json')), result_scores={'iptm': iptm})
        page = self.request(f'/api/campaigns/{cid}/items?sort=iptm&min_iptm=0.8')
        self.assertEqual([item['name'] for item in page['items']], ['strong'])
        self.assertEqual(page['items'][0]['resultScores']['iptm'], 0.9)
        with self.assertRaises(urllib.error.HTTPError):
            self.request(f'/api/campaigns/{cid}/items?sort=bogus')
        with self.assertRaises(urllib.error.HTTPError):
            self.request(f'/api/campaigns/{cid}/items?min_iptm=notanumber')

    def test_export_route_scores_sorted_filtered_and_backfills_legacy(self):
        import urllib.error
        campaign = self.app.pipeline.store.create(spec())
        cid = campaign['id']
        store = self.app.pipeline.store

        def complete(name, iptm):
            claim = drive_to_validation(store, cid, name)
            attempt = store.path.parent / 'campaigns' / cid / claim['id'] / 'attempt-1'
            attempt.mkdir(parents=True)
            result_path = attempt / 'result.json'
            result_path.write_text(json.dumps({'iptm': iptm, 'meanPlddt': iptm * 100}))
            store.advance(claim['id'], claim['token'], 'completed',
                          result_path=str(result_path), result_scores={'iptm': iptm, 'meanPlddt': iptm * 100})

        for name, iptm in (('weak', 0.35), ('mid', 0.72), ('strong', 0.93)):
            complete(name, iptm)

        def export(query=''):
            import urllib.request
            with urllib.request.urlopen(self.base + f'/api/campaigns/{cid}/export' + (f'?{query}' if query else '')) as response:
                return [json.loads(line) for line in response.read().decode().splitlines()]

        rows = export('sort=iptm')
        self.assertEqual([row['name'] for row in rows], ['strong', 'mid', 'weak'])
        self.assertTrue(all(row.get('resultScores', {}).get('iptm') is not None for row in rows))
        self.assertEqual(rows[0]['resultScores']['meanPlddt'], 93.0)
        self.assertTrue(rows[0]['structureUrl'])
        self.assertNotIn('resultPath', rows[0])
        self.assertEqual([row['name'] for row in export('sort=iptm&min_iptm=0.8')], ['strong'])
        # Rows completed before the score ledger existed are backfilled pre-snapshot.
        with store.connection(True) as conn:
            conn.execute('UPDATE campaign_items SET score_checked=0, score_iptm=NULL, score_plddt=NULL WHERE campaign_id=?',
                         (cid,))
        self.assertEqual([row['name'] for row in export('sort=iptm&min_iptm=0.8')], ['strong'])
        self.assertTrue(all(row.get('resultScores', {}).get('iptm') is not None for row in export('sort=plddt')))
        with self.assertRaises(urllib.error.HTTPError):
            export('sort=bogus')
        with self.assertRaises(urllib.error.HTTPError):
            export('min_iptm=notanumber')


if __name__ == '__main__':
    unittest.main()
