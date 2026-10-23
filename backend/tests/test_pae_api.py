"""Real loopback PAE API checks with synthetic matrices in temporary data."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.server import StudioApplication, StudioHTTPServer
from backend.tests import test_api as base
from backend.tests.test_pipeline import spec, submission
from backend.worker_pipeline import write_json


class PAEAPITests(unittest.TestCase):
    request = base.APITests.request

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.app = StudioApplication(self.root/'data', engine_module=base.FakeEngine())
        # No model, prediction thread or resource provisioning is started.
        self.httpd = StudioHTTPServer(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval':.01}, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.httpd.server_port}'
        self.n = 129
        self.matrix = [[(r*131+c)/1000 for c in range(self.n)] for r in range(self.n)]

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.app.close()
        self.thread.join(2); self.temp.cleanup()

    def result(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        structure = directory/'model.pdb'
        structure.write_text(base.PDB)
        return dict(structurePath=str(structure), structureFormat='pdb', chains=[dict(id='A', sequence='A')],
                    pae=self.matrix, residues=[], provenance=dict(mode='synthetic-api-test'))

    def job(self):
        job = self.app.store.create(submission())
        self.app.store.claim()
        directory = self.app.jobs_dir/job['id']
        result, artifacts = self.app._register_artifacts(job['id'], self.result(directory), directory)
        self.app.store.finish(job['id'], 'completed', result=result, artifacts=artifacts)
        return job['id'], directory, result

    def test_job_detail_is_compact_and_preview_and_exact_tile_keep_values(self):
        identifier, directory, result = self.job()
        code, job = self.request('/api/jobs/'+identifier)
        self.assertEqual(code, 200)
        self.assertIsNone(job['result']['pae'])
        self.assertLess(len(json.dumps(job)), 64*1024)
        self.assertNotIn('storagePath', job['result']['paeArtifact'])
        code, preview = self.request('/api/jobs/'+identifier+'/pae?maxSize=7')
        self.assertEqual(code, 200, preview)
        indices = [((2*i+1)*self.n)//14 for i in range(7)]
        self.assertEqual(preview['method'], 'nearest-center')
        self.assertEqual(preview['rowIndices'], indices)
        self.assertEqual(preview['values'], [[self.matrix[r][c] for c in indices] for r in indices])
        code, tile = self.request('/api/jobs/'+identifier+'/pae?rowStart=3&colStart=8&rows=2&cols=3')
        self.assertEqual(code, 200, tile)
        self.assertEqual(tile['method'], 'exact')
        self.assertEqual(tile['values'], [row[8:11] for row in self.matrix[3:5]])
        self.assertNotEqual(tile['values'][0][0], self.matrix[8][3], 'PAE is not assumed symmetric.')

    def test_http_rejects_unbounded_ambiguous_or_out_of_range_windows(self):
        identifier, _, _ = self.job()
        route = '/api/jobs/'+identifier+'/pae?'
        for query in ['maxSize=257', 'maxSize=-1', 'maxSize=', 'maxSize=7&maxSize=8', 'unknown=1',
                      'rows=3', 'maxSize=7&rowStart=0&colStart=0&rows=1&cols=1',
                      'rowStart=128&colStart=0&rows=2&cols=1', 'rowStart=0&colStart=0&rows=129&cols=1']:
            with self.subTest(query=query):
                self.assertEqual(self.request(route+query)[0], 400)

    def test_original_download_does_not_read_matrix_and_changed_binary_is_rejected(self):
        identifier, directory, result = self.job()
        descriptor = result['paeArtifact']
        with patch('backend.result_artifacts.read_pae', side_effect=AssertionError('Download must stream original file')):
            code, raw = self.request(descriptor['downloadUrl'])
            self.assertEqual(code, 200)
            self.assertEqual(raw['pae'], self.matrix)
        binary = directory/descriptor['storageRelativePath']
        binary.write_bytes(binary.read_bytes()+b'changed')
        self.assertEqual(self.request('/api/jobs/'+identifier+'/pae?maxSize=4')[0], 400)

    def test_campaign_endpoint_uses_published_compact_result(self):
        store = self.app.pipeline.store
        campaign = store.create(spec())['id']
        store.append(campaign, [dict(targetId='synthetic', submission=submission())])
        claim = store.claim('prepare'); store.advance(claim['id'], claim['token'], 'ready')
        claim = store.claim('predict'); store.advance(claim['id'], claim['token'], 'validating')
        claim = store.claim('validate')
        directory = self.app.pipeline.items_root/campaign/claim['id']
        result = self.app.pipeline._validate_result(claim, self.result(directory))
        path = directory/'result.json'; write_json(path, result)
        store.advance(claim['id'], claim['token'], 'completed', result_path=str(path))
        route = f'/api/campaigns/{campaign}/items/{claim["id"]}'
        code, item = self.request(route)
        self.assertEqual(code, 200, item)
        self.assertIsNone(item['result']['pae'])
        code, tile = self.request(route+'/pae?rowStart=1&colStart=4&rows=1&cols=2')
        self.assertEqual(code, 200, tile)
        self.assertEqual(tile['values'], [self.matrix[1][4:6]])
        self.assertEqual(self.request(route+'/structure')[0], 200)


if __name__ == '__main__':
    unittest.main()
