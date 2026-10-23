"""Real local HTTP control-plane smoke; uses two bundled reference targets only."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from backend import engine
from backend.server import StudioApplication, StudioHTTPServer
from scripts import submit_campaign as cli


class CampaignCLIHTTPTest(unittest.TestCase):
    def test_two_reference_targets_complete_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            example=engine.load_example('ubiquitin')
            sequence=example['chains'][0]['sequence']
            fasta=root/'two.fa'
            fasta.write_text(f'>reference candidate A\n{sequence}\n>reference candidate B\n{sequence}\n')
            app=StudioApplication(root/'data',workers=1)
            httpd=StudioHTTPServer(('127.0.0.1',0),app)
            thread=threading.Thread(target=httpd.serve_forever,kwargs={'poll_interval':.01},daemon=True)
            thread.start();app.start()
            server=f'http://127.0.0.1:{httpd.server_port}'
            arguments=['--input',str(fasta),'--server',server,'--engine','reference','--example','ubiquitin','--chunk-size','1','--manifest',str(root/'manifest.jsonl')]
            try:
                first=cli.run(cli.parser().parse_args(arguments))
                campaign_id=first['campaignId']
                end=time.monotonic()+15
                while time.monotonic()<end:
                    campaign=cli.request_json(server,'/api/campaigns/'+campaign_id)
                    if campaign['isComplete']:break
                    time.sleep(.05)
                self.assertEqual(campaign['counts']['completed'],2,campaign)
                self.assertEqual(campaign['counts']['failed'],0)
                second=cli.run(cli.parser().parse_args(arguments+['--campaign-id',campaign_id]))
                self.assertEqual(second['inserted'],0)
                self.assertEqual(second['duplicates'],2)
                items=cli.request_json(server,'/api/campaigns/'+campaign_id+'/items')
                self.assertEqual(items['total'],2)
                detail=cli.request_json(server,'/api/campaigns/'+campaign_id+'/items/'+items['items'][0]['id'])
                self.assertTrue(detail.get('result'))
                evidence=dict(verifiedAt='2026-10-23',scope='Local real HTTP API, SQLite staged workers and bundled public structure replay; no model weights/GPU inference',initial=first,replay=second,finalCounts=campaign['counts'],cacheHits=campaign['metrics']['cacheHits'],totalItems=items['total'],artifactResultPresent=bool(detail.get('result')),temporaryDataRemoved=True)
                # Output evidence only when explicitly requested by the verification run.
                import os
                if os.environ.get('PROTEIN_STUDIO_WRITE_CAMPAIGN_EVIDENCE'):
                    destination=Path(__file__).resolve().parents[1]/'artifacts'/'campaign-cli-verification.json'
                    destination.parent.mkdir(exist_ok=True)
                    destination.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
            finally:
                httpd.shutdown();httpd.server_close();app.close();thread.join(1)


if __name__=='__main__': unittest.main()
