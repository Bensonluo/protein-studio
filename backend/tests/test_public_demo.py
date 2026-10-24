"""Real HTTP and bundled public structures; isolated data, no GPU or cloud."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts.serve_public_demo import (
    build_public_app, PublicDemoHTTPServer, check_demo_directory, normalize_origin,
)


class PublicDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = patch.dict(os.environ, {key: value for key, value in os.environ.items()
            if not key.startswith('PROTEIN_STUDIO_')}, clear=True)
        cls.environment.start()
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)/'public'
        cls.app = build_public_app(cls.root, 'https://demo.example.invalid', base_path='/foldspace')
        cls.server = PublicDemoHTTPServer(('127.0.0.1', 0), cls.app)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)
        cls.app.close()
        cls.temporary.cleanup()
        cls.environment.stop()

    def request(self, path, method='GET', headers=None, payload=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        request_headers = {'Host': 'demo.example.invalid', **(headers or {})}
        body = json.dumps(payload) if payload is not None else None
        if body is not None:
            request_headers['Content-Type'] = 'application/json'
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        status, result_headers, content = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, result_headers, content

    def json_request(self, path, **kwargs):
        status, headers, content = self.request(path, **kwargs)
        return status, json.loads(content)

    def test_real_reference_results_completed_with_structure_pae_and_campaign_artifacts(self):
        status, system = self.json_request('/foldspace/api/system', headers={'Origin': 'https://demo.example.invalid'})
        self.assertEqual(status, 200)
        self.assertTrue(system['publicDemo']['readOnly'])
        self.assertFalse(system['publicDemo']['gpuInference'])
        status, jobs = self.json_request('/foldspace/api/jobs')
        self.assertEqual(status, 200)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['status'], 'completed')
        status, detail = self.json_request('/foldspace/api/jobs/'+jobs[0]['id'])
        self.assertEqual([detail['result']['paeArtifact']['rows'], detail['result']['paeArtifact']['cols']], [229, 229])
        status, _, structure = self.request(detail['result']['structureUrl'])
        self.assertEqual(status, 200)
        self.assertIn(b'ATOM', structure)
        status, tile = self.json_request(detail['result']['paeArtifact']['tileUrl']+'?rowStart=0&colStart=0&rows=2&cols=2')
        self.assertEqual(status, 200)
        self.assertEqual(len(tile['values']), 2)
        status, campaigns = self.json_request('/foldspace/api/campaigns')
        self.assertEqual(status, 200)
        self.assertEqual(len(campaigns), 1)
        campaign = campaigns[0]
        self.assertTrue(campaign['sealed'])
        self.assertTrue(campaign['isComplete'])
        self.assertEqual(campaign['counts']['completed'], 3)
        status, items = self.json_request('/foldspace/api/campaigns/'+campaign['id']+'/items')
        for item in items['items']:
            base = '/foldspace/api/campaigns/'+campaign['id']+'/items/'+item['id']
            status, result = self.json_request(base)
            self.assertEqual(status, 200)
            self.assertEqual(result['stage'], 'completed')
            status, _, structure = self.request(result['result']['structureUrl'])
            self.assertEqual(status, 200)
            self.assertIn(b'ATOM', structure)
        status, storage = self.json_request('/foldspace/api/storage')
        self.assertEqual(status, 200)
        self.assertEqual(storage['state']['latestScan']['status'], 'completed')

    def test_all_public_writes_and_worker_routes_rejected_without_changing_records(self):
        job = self.app.store.list()[0]
        campaign = self.app.pipeline.store.list()[0]
        paths = [('/foldspace/api/jobs', 'POST'), ('/foldspace/api/import', 'POST'),
            ('/foldspace/api/jobs/'+job['id'], 'DELETE'),
            ('/foldspace/api/campaigns/'+campaign['id']+'/cancel', 'POST'),
            ('/foldspace/api/storage/scan', 'POST'), ('/foldspace/api/storage/apply', 'POST'),
            ('/foldspace/api/jobs', 'PUT'), ('/foldspace/api/jobs', 'PATCH'),
            ('/foldspace/worker/v1/register', 'POST'), ('/foldspace/worker/v1/claim', 'GET'),
            ('/foldspace/%77orker/v1/claim', 'GET')]
        for path, method in paths:
            with self.subTest(path=path, method=method):
                status, _, _ = self.request(path, method, payload={})
                self.assertEqual(status, 403)
        self.assertEqual(len(self.app.store.list()), 1)
        self.assertEqual(self.app.store.get(job['id'])['status'], 'completed')
        self.assertEqual(self.app.pipeline.store.get(campaign['id'])['counts']['completed'], 3)

    def test_public_origin_is_explicit_https_and_forwarded_headers_cannot_override_it(self):
        for origin in ['http://demo.example.invalid', 'https://evil.example.invalid',
                       'https://demo.example.invalid:0', 'null']:
            with self.subTest(origin=origin):
                status, _, _ = self.request('/foldspace/api/jobs', headers={'Origin': origin,
                    'X-Forwarded-Proto': 'https', 'X-Forwarded-Host': 'demo.example.invalid'})
                self.assertEqual(status, 403)
        status, _, _ = self.request('/foldspace/api/jobs', headers={'Host': 'evil.example.invalid',
            'Origin': 'https://demo.example.invalid', 'X-Forwarded-Host': 'demo.example.invalid'})
        self.assertEqual(status, 403)
        self.assertEqual(self.request('/foldspace/api/health', headers={'Host': '127.0.0.1:'+str(self.server.server_port)})[0], 200)
        self.assertEqual(self.request('/foldspace/api/jobs', headers={'Host': '127.0.0.1:'+str(self.server.server_port)})[0], 403)
        self.assertEqual(self.request('/api/jobs')[0], 404)
        self.assertEqual(self.request('/foldspacex/api/jobs')[0], 404)

    def test_restart_keeps_the_seed_ids_and_original_structure_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = build_public_app(Path(temporary)/'demo', 'https://demo.example.invalid')
            job = app.store.list()[0]
            structure = app.artifact(job['id'], '__structure__')
            digest = hashlib.sha256(structure.read_bytes()).hexdigest()
            campaign = app.pipeline.store.list()[0]
            app.close()
            app = build_public_app(Path(temporary)/'demo', 'https://demo.example.invalid')
            try:
                self.assertEqual([row['id'] for row in app.store.list()], [job['id']])
                self.assertEqual([row['id'] for row in app.pipeline.store.list()], [campaign['id']])
                self.assertEqual(hashlib.sha256(app.artifact(job['id'], '__structure__').read_bytes()).hexdigest(), digest)
            finally:
                app.close()

    def test_refuses_unmarked_private_directory_and_external_execution_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary)/'existing'
            private.mkdir()
            sentinel = private/'private-data.txt'
            sentinel.write_text('do not publish or change')
            with self.assertRaisesRegex(ValueError, 'nonempty unmarked'):
                build_public_app(private, 'https://demo.example.invalid')
            self.assertEqual(sentinel.read_text(), 'do not publish or change')
            self.assertEqual(list(private.iterdir()), [sentinel])
            with patch.dict(os.environ, {'PROTEIN_STUDIO_DISTRIBUTED': '1'}):
                with self.assertRaisesRegex(ValueError, 'isolated reference'):
                    build_public_app(Path(temporary)/'unused', 'https://demo.example.invalid')
            self.assertFalse((Path(temporary)/'unused').exists())
        for invalid in ['http://demo.example.invalid', 'https://demo.example.invalid/foldspace',
                        'https://user:password@demo.example.invalid', 'https://demo.example.invalid:0']:
            with self.subTest(origin=invalid), self.assertRaises(ValueError):
                normalize_origin(invalid)


if __name__ == '__main__':
    unittest.main()
