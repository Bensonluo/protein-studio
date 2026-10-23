"""Real HTTP metadata exports remain bounded and never include model arrays."""
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from backend.server import StudioApplication, StudioHTTPServer
from backend.tests.test_api import FakeEngine
from backend.tests.test_pipeline import spec, submission


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=StudioApplication(Path(self.temp.name),engine_module=FakeEngine())
        self.http=StudioHTTPServer(('127.0.0.1',0),self.app)
        self.thread=threading.Thread(target=self.http.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        self.thread.start()
        self.store=self.app.pipeline.store
        self.campaign=self.store.create(spec('paused'))
        self.cid=self.campaign['id']
        self.base=f'http://127.0.0.1:{self.http.server_port}/api/campaigns/{self.cid}/export'

    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join(2)
        self.app.close();self.temp.cleanup()

    def test_export_spans_many_pages_without_sequences_or_private_result_paths(self):
        self.store.append(self.cid,[{'targetId':f'item-{i}','submission':submission('A'*2000,'目标 '+str(i))} for i in range(451)])
        with self.store.connection(True) as conn:
            conn.execute("UPDATE campaign_items SET stage='completed',result_path='/private/result/huge-pae.json'")
        with urllib.request.urlopen(self.base) as response:
            self.assertIn('attachment',response.headers['Content-Disposition'])
            self.assertEqual(response.headers['X-Export-Scope'],'consistent-metadata-snapshot')
            lines=[json.loads(line) for line in response]
        self.assertEqual(len(lines),451)
        self.assertEqual(len({line['targetId'] for line in lines}),451)
        self.assertEqual(lines[-1]['targetId'],'item-450')
        self.assertEqual(lines[0]['name'],'目标 0')
        for line in lines:
            self.assertNotIn('resultPath',line);self.assertNotIn('input',line)
            self.assertNotIn('AAAA',json.dumps(line))
            self.assertIn('/structure',line['structureUrl'])
        self.assertFalse(list(Path(self.temp.name).glob('tmp*')))

    def test_failed_subset_and_cancelled_effective_stage(self):
        items=self.store.append(self.cid,[{'targetId':str(i),'submission':submission()} for i in range(3)])['items']
        with self.store.connection(True) as conn:
            conn.execute("UPDATE campaign_items SET stage='failed',error='sample error',failed_stage='predict' WHERE id=?",(items[0]['id'],))
        self.store.control(self.cid,'cancel')
        with urllib.request.urlopen(self.base+'?stage=failed') as r:failed=[json.loads(x) for x in r]
        with urllib.request.urlopen(self.base+'?stage=cancelled') as r:cancelled=[json.loads(x) for x in r]
        self.assertEqual(len(failed),1);self.assertEqual(failed[0]['error'],'sample error')
        self.assertEqual(len(cancelled),2);self.assertTrue(all(x['stage']=='cancelled' for x in cancelled))
        with urllib.request.urlopen(self.base+'?stage=ready') as r:self.assertEqual(r.read(),b'')

    def test_invalid_stage_rejected_before_download_headers(self):
        with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(self.base+'?stage=bogus')
        self.assertEqual(error.exception.code,400)
        self.assertNotIn('attachment',str(error.exception.headers))
