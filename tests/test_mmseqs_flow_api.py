"""Registered two-phase MMseqs adapter through real HTTP; synthetic tools only."""
import hashlib
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


class MmseqsFlowHttpTests(unittest.TestCase):
    def test_registered_cpu_and_gpu_profiles_run_native_policies_and_publish_af3_inputs(self):
        with tempfile.TemporaryDirectory(prefix='foldspace-mmseqs-http-') as directory:
            root = Path(directory)
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith('PROTEIN_STUDIO_')}
            with (root / 'server.log').open('w+') as log:
                server = subprocess.Popen([sys.executable, '-u', str(ROOT / 'tests/mmseqs_http_fixture.py'), str(root)],
                                          cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=log, text=True)
                try:
                    selector = selectors.DefaultSelector()
                    try:
                        selector.register(server.stdout, selectors.EVENT_READ)
                        self.assertTrue(selector.select(timeout=15), 'Fixture startup timeout')
                        line = server.stdout.readline()
                        self.assertTrue(line.startswith('READY '), line + (root / 'server.log').read_text())
                        metadata = json.loads(line[6:])
                    finally:
                        selector.close()
                    base = f"http://127.0.0.1:{metadata['port']}"
                    def get(path):
                        with urlopen(base + path, timeout=5) as response:
                            return json.load(response)
                    profiles = {p['id']: p for p in get('/api/databases')}
                    for device in ('cpu', 'gpu'):
                        profile = profiles['fixture-' + device]
                        self.assertTrue(profile['ready'], profile['diagnostics'])
                        self.assertTrue(profile['campaignCapability']['available'])
                        self.assertEqual(profile['preparationStrategy']['searchDevice'], device)
                        self.assertEqual(profile['preparationStrategy']['pairedCandidateSource'], 'native-uniprot')
                        self.assertEqual(profile['preparationStrategy']['templateStrategy'], 'native-af3')
                    campaigns = {}
                    deadline = time.monotonic() + 20
                    while True:
                        campaigns = {device: get('/api/campaigns/' + cid)
                                     for device, cid in metadata['campaigns'].items()}
                        self.assertTrue(all(c['counts']['failed'] == 0 for c in campaigns.values()),
                                        (root / 'server.log').read_text())
                        if campaigns['gpu']['counts']['ready'] == 2 and campaigns['cpu']['counts']['ready'] == 1:
                            break
                        if time.monotonic() > deadline:
                            self.fail('Preparation timeout: ' + (root / 'server.log').read_text())
                        time.sleep(.04)
                    all_keys, phase_evidence = {}, []
                    for device, campaign in campaigns.items():
                        count = 2 if device == 'gpu' else 1
                        metrics = campaign['metrics']['preparationReuse']
                        self.assertEqual(metrics['newChainSearches'], count + 1)
                        self.assertEqual(metrics['chainCacheHits'], count - 1)
                        self.assertEqual(campaign['counts']['predicting'], 0)
                        self.assertEqual(campaign['databaseProfile']['preparationStrategy']['searchDevice'], device)
                        items = get('/api/campaigns/' + campaign['id'] + '/items')['items']
                        keys = set()
                        for item in items:
                            detail = get('/api/campaigns/' + campaign['id'] + '/items/' + item['id'])
                            for entry in detail['preparationReuse']['entries']:
                                keys.add(entry['cacheKey'])
                                quality = entry['msaQuality']
                                self.assertEqual(quality['unpaired']['rawRows'], 2)
                                self.assertEqual(quality['paired']['rawRows'], 3)
                                self.assertEqual(quality['paired']['role'], 'pairing-candidates')
                                self.assertEqual(entry['searchBackend']['device'], device)
                                self.assertGreater(entry['msaSearchProcessWallSeconds'], 0)
                                self.assertGreater(entry['cpuFinalizeProcessWallSeconds'], 0)
                                leaf = Path(metadata['dataRoot']) / 'cache/af3-chain-v1' / entry['cacheKey']
                                manifest = json.loads((leaf / 'COMPLETE.json').read_text())
                                data = json.loads((leaf / manifest['inputRelativePath']).read_text())
                                protein = data['sequences'][0]['protein']
                                self.assertIn('\r\n', protein['unpairedMsa'])
                                self.assertEqual(hashlib.sha256(protein['unpairedMsa'].encode()).hexdigest(),
                                                 quality['unpaired']['sourceSha256'])
                                self.assertEqual(protein['pairedMsa'].count('>sp|P1|GENE_SPECIES'), 2)
                                self.assertEqual(protein['templates'][0]['queryIndices'], [0, 2])
                                self.assertEqual(protein['templates'][0]['templateIndices'], [2, 5])
                                # Fake CF actually records the visible devices
                                # from its subprocess environment, not UI data.
                                commands = list(leaf.rglob('command.json'))
                                self.assertEqual(len(commands), 1)
                                command = json.loads(commands[0].read_text())
                                self.assertEqual(command['cuda'], 'GPU-synthetic-search' if device == 'gpu' else '')
                                self.assertEqual(command['argv'][command['argv'].index('--gpu') + 1],
                                                 '1' if device == 'gpu' else '0')
                        all_keys[device] = keys
                        phase_evidence.append(dict(device=device, preparedTargets=count,
                                                   uniqueLeafArtifacts=len(keys), reuse=metrics))
                    self.assertTrue(all_keys['cpu'].isdisjoint(all_keys['gpu']))
                    native = [json.loads(line) for line in Path(metadata['nativeLog']).read_text().splitlines()]
                    self.assertEqual(len(native), 10)  # five chains, paired+template
                    self.assertTrue(all(event['cuda'] == '' for event in native))
                    self.assertTrue(all(event['deduplicate'] is False for event in native if event['kind'] == 'paired'))
                    report = dict(status='passed',
                        scope='Real registry inspection, HTTP campaign creation, two adapter subprocess phases, '
                              'chain caches and AF3 QC. CF executable, DB file members and native AF3 APIs are synthetic; no GPU kernels or scientific prediction.',
                        count=7, checks=[
                            'Trusted profile commands work without a global AF3 prepare command.',
                            'CPU and GPU registered policies remain frozen in their campaigns.',
                            'Five unique chain preparations feed three targets with shared-chain reuse.',
                            'Actual search subprocess receives only its assigned synthetic GPU group; CPU search receives none.',
                            'Native paired/template helpers run with no CUDA devices and retain duplicate UniProt headers.',
                            'AF3 inputs retain exact CRLF unpaired MSA, separate paired candidates and zero-based template mappings.',
                            'CPU/GPU identities produce distinct caches; detail exposes separate search/finalize time and actual QC.',
                        ], phases=phase_evidence, nativeCalls=len(native), nativeCudaDevices=sorted({x['cuda'] for x in native}))
                    (ROOT / 'artifacts/mmseqs-flow-http-verification.json').write_text(
                        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
                finally:
                    if server.poll() is None:
                        server.terminate()
                        try:
                            server.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            server.kill()
                            server.wait(timeout=3)
                    server.stdout.close()


if __name__ == '__main__':
    unittest.main()
