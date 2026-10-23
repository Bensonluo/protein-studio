"""Isolated real HTTP/SQLite/subprocess fixture for MSA scheduling UI tests.

Search output and database readiness are synthetic. No AF3 library, GPU,
sequence database, cloud connection, or production data directory is used.
The caller owns the supplied temporary directory and releases the shared
search by creating ``releaseFile`` returned in the READY metadata.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import signal
import sys
import threading
from unittest.mock import patch
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import staged
from backend.server import StudioApplication, StudioHTTPServer


RUNNER = r'''
import json,os,sys,time
from pathlib import Path
source,output=map(Path,sys.argv[1:3]);output.mkdir(parents=True,exist_ok=True)
assert os.environ.get('CUDA_VISIBLE_DEVICES')==''
data=json.loads(source.read_text());p=data['sequences'][0]['protein'];seq=p['sequence']
def event(kind):
    fd=os.open(os.environ['MSA_FIXTURE_EVENTS'],os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    os.write(fd,(json.dumps(dict(kind=kind,sequence=seq,time=time.monotonic(),pid=os.getpid()))+'\n').encode());os.close(fd)
event('start')
if seq=='AG':
    deadline=time.monotonic()+60
    while not Path(os.environ['MSA_FIXTURE_RELEASE']).exists():
        if time.monotonic()>deadline:raise RuntimeError('Fixture release was not received')
        time.sleep(.02)
time.sleep(.08)
msa='>query\n'+seq+'\n>hit-one TaxID=9606\n'+seq[0]+'aa'+seq[1:]+'\n>hit-two TaxID=9606\n-'+seq[-1]+'\n'
p.update(id=['A'],unpairedMsa=msa,pairedMsa=msa,templates=[])
data['version']=4
(output/'protein_data.json').write_text(json.dumps(data))
event('end')
'''


def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    release = root / 'release-shared-search'
    events = root / 'search-events.jsonl'
    runner = root / 'synthetic-search.py'
    runner.write_text(RUNNER)
    environment = {
        'PROTEIN_STUDIO_PREDICTION_MODE': 'workers',
        'PROTEIN_STUDIO_DISTRIBUTED': '0',
        'PROTEIN_STUDIO_CPU_PREP_WORKERS': '2',
        'PROTEIN_STUDIO_CPU_VALIDATE_WORKERS': '1',
        'PROTEIN_STUDIO_READY_HIGH_WATER': '8',
        'PROTEIN_STUDIO_PREPARATION_RESOURCES_JSON': json.dumps({
            'cpuBudget': 2, 'cpuPerPrepare': 1,
        }),
        'PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND': json.dumps([
            sys.executable, str(runner), '{input_json}', '{output_dir}',
        ]),
        'MSA_FIXTURE_EVENTS': str(events),
        'MSA_FIXTURE_RELEASE': str(release),
    }
    profile = dict(
        id='msa-http-fixture', engine='alphafold3', ready=True,
        root='/synthetic/not-a-sequence-library',
        profileFingerprint='msa-http-fixture-database-v1',
        prepareVersion='synthetic-runner-v1', searchVersion='synthetic-search-v1',
        searchParameters={'threads': 1}, templateParameters={'date': '2021-09-30'},
    )

    def registered_profile(profile_id, *args, **kwargs):
        if profile_id != profile['id']:
            # Reference remains available for ordinary UI diagnostics. No real
            # local native profile is silently promoted to ready by this fixture.
            if profile_id == 'reference':
                return dict(id='reference', engine='reference', ready=True)
            raise KeyError(profile_id)
        return copy.deepcopy(profile)

    with patch.dict(os.environ, environment), patch(
        'backend.databases.get_profile', side_effect=registered_profile,
    ):
        app = StudioApplication(root / 'data', staged_module=staged)
        app.pipeline.preparation_router = lambda conn, now: ('1', [])
        http = StudioHTTPServer(('127.0.0.1', 0), app)
        thread = threading.Thread(target=http.serve_forever,
                                  kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        app.start()
        base = f'http://127.0.0.1:{http.server_port}'

        def request(path, payload):
            raw = json.dumps(payload).encode()
            with urlopen(Request(base + path, data=raw,
                                 headers={'Content-Type': 'application/json'}), timeout=5) as response:
                return json.load(response)

        def stop(*args):
            release.touch()
            threading.Thread(target=http.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            campaign = request('/api/campaigns', dict(
                name='MSA shared-chain synthetic HTTP fixture', engine='alphafold3',
                databaseProfile=profile['id'], status='paused',
                settings={'seeds': [1], 'numSamples': 1},
            ))
            cid = campaign['id']
            items = []
            for target_id, sequences in [('shared-one', ('AG', 'VC')),
                                         ('shared-two', ('AG', 'LK')),
                                         ('independent', ('FT',))]:
                items.append(dict(targetId=target_id, name=target_id,
                                  chains=[dict(id=chr(65 + i), sequence=seq)
                                          for i, seq in enumerate(sequences)]))
            request(f'/api/campaigns/{cid}/items', {'items': items})
            request(f'/api/campaigns/{cid}/seal', {})
            request(f'/api/campaigns/{cid}/resume', {})
            print('READY ' + json.dumps(dict(
                port=http.server_port, campaignId=cid,
                releaseFile=str(release), eventsFile=str(events),
                scope='real HTTP and CPU subprocess; synthetic searches; no GPU',
            )), flush=True)
            thread.join()
        finally:
            release.touch()
            http.shutdown()
            thread.join(2)
            http.server_close()
            app.close()


if __name__ == '__main__':
    main()
