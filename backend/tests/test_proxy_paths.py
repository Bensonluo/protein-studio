"""Real HTTP mount-path regression tests; temporary data only, no GPU or downloads."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from backend.proxy_paths import normalize_base_path, strip_request_target, public_urls
from backend.server import StudioApplication, StudioHTTPServer

ROOT = Path(__file__).resolve().parents[2]
PDB = 'ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00 20.00           C  \nEND\n'
# Aligned companion for the 129x129 confidence fixture: one CA per residue.
PDB129 = ''.join(f'ATOM{i:7d}  CA  ALA A{i:4d}    {1.0+(i%9):8.3f}{2.0+(i%7):8.3f}{3.0+(i%5):8.3f}  1.00 20.00           C  \n'
                 for i in range(1, 130)) + 'END\n'


@contextmanager
def running_server(base):
    with tempfile.TemporaryDirectory() as temporary:
        app = StudioApplication(Path(temporary), base_path=base)
        httpd = StudioHTTPServer(('127.0.0.1', 0), app)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval':.01}, daemon=True)
        thread.start()
        try:
            yield app, f'http://127.0.0.1:{httpd.server_port}'
        finally:
            httpd.shutdown(); httpd.server_close(); app.close(); thread.join(2)


def request(origin, path, method='GET', payload=None):
    raw = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(origin + path, data=raw, method=method,
        headers={'Content-Type':'application/json'} if raw is not None else {})
    try:
        response = urllib.request.urlopen(req, timeout=8)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read()
        if 'application/json' in response.headers.get('Content-Type', ''):
            body = json.loads(body)
        return response.status, body, dict(response.headers)


class PathHelpersTests(unittest.TestCase):
    def test_config_rejects_ambiguous_or_external_prefixes(self):
        self.assertEqual(normalize_base_path('/foldspace/'), '/foldspace')
        self.assertEqual(normalize_base_path(''), '')
        for value in ('//evil.test', 'https://evil.test', '/a/../b', '/a//b', '/a?x=1', '/%2f', '/a\\b', '/x\n'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_base_path(value)

    def test_strip_is_segment_exact_and_preserves_query(self):
        self.assertEqual(strip_request_target('/foldspace/api/jobs?name=a%2Fb', '/foldspace'), '/api/jobs?name=a%2Fb')
        for path in ('/api/health', '/foldspacex/api/health', '/%66oldspace/api/health'):
            with self.subTest(path=path), self.assertRaises(KeyError):
                strip_request_target(path, '/foldspace')
        with self.assertRaises(ValueError):
            strip_request_target('https://evil.test/foldspace/api/jobs', '/foldspace')

    def test_serialization_only_changes_application_url_fields_and_not_original(self):
        original = {'result': {'structureUrl':'/api/jobs/abc/structure',
            'paeArtifact':{'previewUrl':'/api/jobs/abc/pae?maxSize=256', 'downloadUrl':'/api/jobs/abc/artifact?name=a%2Fb'},
            'sourceUrl':'https://example.org/api/science', 'note':'/api/this-is-text', 'path':'/private/data'},
            'url':'/other-project/a', 'external':{'url':'https://example.org/api/other'},
            'protocolRelative':{'url':'//example.org/api/other'}, 'worker':{'preparedUrl':'/worker/v1/prepared/item'}}
        translated = public_urls(original, '/foldspace')
        self.assertEqual(translated['result']['structureUrl'], '/foldspace/api/jobs/abc/structure')
        self.assertEqual(translated['result']['paeArtifact']['previewUrl'], '/foldspace/api/jobs/abc/pae?maxSize=256')
        self.assertEqual(translated['worker']['preparedUrl'], '/foldspace/worker/v1/prepared/item')
        self.assertEqual(original['result']['structureUrl'], '/api/jobs/abc/structure')
        for field in ('sourceUrl','note','path'):
            self.assertEqual(translated['result'][field], original['result'][field])
        for field in ('url','external','protocolRelative'):
            self.assertEqual(translated[field], original[field])


class MountedHTTPTests(unittest.TestCase):
    def test_root_and_prefix_serve_real_assets_sources_structure_and_pae(self):
        for base in ('', '/foldspace'):
            with self.subTest(base=base), running_server(base) as (app, origin):
                status, html, _ = request(origin, base + '/')
                self.assertEqual(status, 200)
                self.assertIn(f'<meta name="protein-studio-base-path" content="{base}">'.encode(), html)
                self.assertIn(b'src="./vendor/3Dmol-min.js"', html)
                self.assertNotIn(b'src="/vendor/', html)
                for asset in ('app.js', 'pae-viewer.js', 'styles.css', 'vendor/3Dmol-min.js', 'sources.json'):
                    status, body, _ = request(origin, base + '/' + asset)
                    self.assertEqual(status, 200, asset)
                    self.assertTrue(body)
                self.assertEqual(request(origin, base + '/api/health')[0], 200)
                # The asymmetric synthetic matrix verifies exact numbers across serialization and fetch.
                confidence = {'pae':[[float(i) + j / 1000 for j in range(129)] for i in range(129)]}
                status, job, _ = request(origin, base + '/api/import', 'POST',
                    {'name':'Mount-path fixture', 'structureText':PDB129, 'format':'pdb', 'source':'unknown', 'confidence':confidence})
                self.assertEqual(status, 201, job)
                jid = job['id']; result = job['result']
                self.assertEqual(result['structureUrl'], base + f'/api/jobs/{jid}/structure')
                self.assertEqual(request(origin, result['structureUrl'])[1].decode(), PDB129)
                self.assertEqual(request(origin, result['artifacts'][0]['url'])[0], 200)
                descriptor = result['paeArtifact']
                status, tile, _ = request(origin, descriptor['tileUrl'] + '?rowStart=1&colStart=2&rows=1&cols=2')
                self.assertEqual(status, 200); self.assertEqual(tile['values'], [[1.002, 1.003]])
                self.assertEqual(request(origin, descriptor['downloadUrl'])[0], 200)
                # Mounting is presentation-only: stored scientific results keep canonical internal paths.
                self.assertEqual(app.store.get(jid)['result']['structureUrl'], f'/api/jobs/{jid}/structure')
                self.assertEqual(request(origin, base + '/api/jobs')[1][0]['result']['structureUrl'], result['structureUrl'])
                if base:
                    for invalid in ('/', '/api/health', '/app.js', '/sources.json', '/foldspacex/api/health', '/foldspace/../app.js'):
                        self.assertEqual(request(origin, invalid)[0], 404, invalid)
                    self.assertEqual(request(origin, base)[0], 200)  # urllib follows the canonical trailing-slash redirect.
                else:
                    self.assertEqual(request(origin, '/foldspace/api/health')[0], 404)

    def test_campaign_export_urls_are_mounted_too(self):
        with running_server('/foldspace') as (app, origin):
            campaign = app.pipeline.store.create({'name':'Export fixture', 'engine':'reference', 'status':'paused',
                'settings':{}, 'databaseProfile':{}, 'executionProfile':{}})
            app.pipeline.store.append(campaign['id'], [{'targetId':'target-1', 'submission':{
                'name':'target-1','engine':'reference','chains':[{'id':'A','sequence':'A'}],'seeds':[1],'numSamples':1}}])
            status, raw, _ = request(origin, '/foldspace/api/campaigns/' + campaign['id'] + '/export')
            self.assertEqual(status, 200)
            item = json.loads(raw.decode().strip())
            self.assertTrue(item['resultDetailUrl'].startswith('/foldspace/api/campaigns/'))

    def test_environment_is_read_once_by_application(self):
        with patch.dict('os.environ', {'PROTEIN_STUDIO_BASE_PATH':'/foldspace'}), tempfile.TemporaryDirectory() as temporary:
            app = StudioApplication(temporary)
            try:
                self.assertEqual(app.base_path, '/foldspace')
            finally:
                app.close()


if __name__ == '__main__':
    unittest.main()
