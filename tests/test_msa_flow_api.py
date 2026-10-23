"""Real HTTP/subprocess integration; all biological search data is synthetic."""
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.025)
    raise AssertionError('Synthetic MSA HTTP fixture did not reach the expected state.')


class MsaFlowHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='foldspace-msa-http-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stderr = (self.root / 'server.log').open('w+')
        self.addCleanup(self.stderr.close)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith('PROTEIN_STUDIO_')}
        self.process = subprocess.Popen(
            [sys.executable, '-u', str(ROOT / 'tests/msa_http_fixture.py'), str(self.root)],
            cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=self.stderr, text=True,
        )
        self.addCleanup(self.stop)
        selector = selectors.DefaultSelector()
        try:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=15):
                self.fail('MSA HTTP fixture startup timed out: ' + self.server_log())
            line = self.process.stdout.readline()
            self.assertTrue(line.startswith('READY '), line + self.server_log())
            self.metadata = json.loads(line[6:])
        finally:
            selector.close()
        self.base = f"http://127.0.0.1:{self.metadata['port']}"
        self.endpoint = '/api/campaigns/' + self.metadata['campaignId']

    def server_log(self):
        self.stderr.flush()
        return (self.root / 'server.log').read_text()

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.process.stdout.close()

    def request(self, path):
        with urlopen(self.base + path, timeout=5) as response:
            return json.load(response)

    def events(self):
        path = Path(self.metadata['eventsFile'])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_shared_dependency_yields_slot_and_publishes_qc_through_real_http(self):
        # AG deliberately stays locked. Whichever target loses the shared-chain
        # lock must search its own candidate, then give its slot to FT. Thread
        # scheduling does not determine which target first acquires AG.
        def independent_work_finished():
            finished = {event['sequence'] for event in self.events() if event['kind'] == 'end'}
            return 'FT' in finished and bool(finished & {'VC', 'LK'})
        wait_for(independent_work_finished)
        before_release = self.events()
        self.assertNotIn('AG', [event['sequence'] for event in before_release
                                if event['kind'] == 'end'])
        campaign = self.request(self.endpoint)
        states = campaign['preparationStateCounts']
        self.assertGreaterEqual(states['waitingShared'], 1)
        self.assertEqual(campaign['counts']['failed'], 0)
        items = self.request(self.endpoint + '/items')['items']
        waiting = [item for item in items if item.get('preparationProgress', {}).get('state') == 'waiting_shared']
        self.assertTrue(waiting)
        self.assertTrue(all(item['error'] is None and item['failedStage'] is None for item in waiting))
        self.assertTrue(all('prepare_progress' not in item for item in items))
        config = self.request('/api/pipeline/config')['preparationResources']
        self.assertLessEqual(config['inflight']['count'], 2)
        self.assertLessEqual(config['inflight']['cpuReserved'], 2)
        Path(self.metadata['releaseFile']).touch()
        ready = wait_for(lambda: (value if (value := self.request(self.endpoint))['counts']['ready'] == 3 else None))
        self.assertEqual(ready['counts']['failed'], 0)
        self.assertEqual(ready['counts']['predicting'], 0)
        self.assertEqual(ready['metrics']['preparationReuse']['newChainSearches'], 4)
        self.assertEqual(ready['metrics']['preparationReuse']['chainCacheHits'], 1)
        self.assertEqual(ready['preparationStateCounts'], {'searching': 0, 'waitingShared': 0, 'assembling': 0})
        final_events = self.events()
        self.assertCountEqual([event['sequence'] for event in final_events if event['kind'] == 'start'],
                              ['AG', 'VC', 'LK', 'FT'])
        active = peak = 0
        for event in sorted(final_events, key=lambda entry: entry['time']):
            active += 1 if event['kind'] == 'start' else -1
            peak = max(peak, active)
            self.assertGreaterEqual(active, 0)
        self.assertEqual(active, 0)
        self.assertLessEqual(peak, 2)
        self.assertEqual(peak, 2)

        details = []
        for item in self.request(self.endpoint + '/items')['items']:
            detail = self.request(self.endpoint + '/items/' + item['id'])
            details.append(detail)
            self.assertEqual(detail['preparationProgress']['state'], 'prepared')
            for entry in detail['preparationReuse']['entries']:
                self.assertGreater(entry['searchProcessWallSeconds'], 0)
                self.assertEqual(entry['msaQuality']['schemaVersion'], 1)
                for field, role in [('unpaired', 'unpaired'), ('paired', 'pairing-candidates')]:
                    quality = entry['msaQuality'][field]
                    self.assertEqual(quality['role'], role)
                    self.assertEqual((quality['rawRows'], quality['hitRows']), (3, 2))
                    self.assertEqual(quality['coverage']['mean'], .75)
                    self.assertEqual(quality['gapFraction'], .25)
                    self.assertEqual(quality['insertedResidues'], 2)
                    self.assertEqual(len(quality['sourceSha256']), 64)
                self.assertIsNone(entry['msaQuality']['paired']['finalPairedRows'])
            self.assertNotIn('>query', json.dumps(detail))

        with urlopen(self.base + self.endpoint + '/export', timeout=5) as response:
            exports = [json.loads(line) for line in response]
        self.assertEqual(len(exports), 3)
        self.assertTrue(all(item['preparationProgress']['state'] == 'prepared' for item in exports))
        self.assertTrue(all('prepare_progress' not in item and 'input' not in item for item in exports))
        origin = min(event['time'] for event in final_events)
        report = dict(
            status='passed',
            scope='Real localhost HTTP, SQLite leases, bounded CPU subprocesses and cache publication; '
                  'synthetic search outputs and database readiness; no native MSA, AF3, GPU or cloud execution.',
            prepareWorkers=2, estimatedCpuBudget=2, estimatedCpuPerPrepare=1,
            activeSearchPeak=peak, completedTargets=3, uniqueChainSearches=4,
            waitingTargetsObserved=len(waiting), beforeReleaseStateCounts=states,
            preparationReuse=ready['metrics']['preparationReuse'],
            events=[dict(kind=event['kind'], sequence=event['sequence'],
                         relativeSeconds=round(event['time'] - origin, 6)) for event in final_events],
            checks=[
                'One non-shared candidate and later independent FT finish while shared AG is still held.',
                'Shared-dependency waiting is public typed progress with no failed-stage error.',
                'Four unique searches feed three prepared targets; deferred own-search accounting survives.',
                'At most two CPU search subprocesses overlap under the two-unit estimate budget.',
                'Real detail endpoints expose exact MSA QC and historical process wall time without raw alignments.',
                'Metadata JSONL export includes public progress and excludes private checkpoints and sequences.',
            ],
        )
        (ROOT / 'artifacts').mkdir(exist_ok=True)
        (ROOT / 'artifacts/msa-flow-http-verification.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    unittest.main()
