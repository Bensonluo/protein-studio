"""Real HTTP + installed adapter fixture with synthetic search/native modules.

The caller owns the temporary root. No model, database, GPU or cloud is used.
The registry is actually inspected, commands actually run, and AF3 inputs pass
the production chain/cache/QC path. Fake tools are explicitly local test files.
"""
import copy
import json
import os
from pathlib import Path
import signal
import sys
import threading
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.server import StudioApplication, StudioHTTPServer
from backend.tests.test_mmseqs_af3_runner import make_profile, make_fake_native


def registry_profile(root, device):
    directory = root / device
    directory.mkdir()
    profile = make_profile(directory, native=True, device=device)
    assets = []
    suffixes = ['', '.dbtype', '.index', '_seq', '_seq.dbtype', '_seq.index',
                '_aln', '_aln.dbtype', '_aln.index']
    for name, value in profile['paths'].items():
        path = Path(value)
        kind = 'template' if name in {'seqres_database_path', 'pdb_database_path'} else 'sequence'
        asset = dict(name=name, path=str(path), kind=kind, required=True)
        if name in {'uniref30_db', 'environment_db'}:
            for suffix in suffixes:
                Path(str(path) + suffix).write_text('synthetic database member; not MMseqs data\n')
            asset.update(type='prefix', requiredSuffixes=suffixes)
        elif name == 'pdb_database_path':
            path.mkdir()
            (path / 'fixture.cif').write_text('data_synthetic\n# not a real template database\n')
            asset['type'] = 'directory'
        else:
            path.write_text('>synthetic\nACDE\n')
            asset['type'] = 'file'
        assets.append(asset)
    for name, value in profile['executables'].items():
        path = Path(value)
        if not path.exists():
            path.write_text('#!/bin/sh\nexit 0\n')
            path.chmod(0o755)
    command = [sys.executable, str(ROOT / 'scripts/mmseqs_af3_runner.py'),
               '--input', '{input_json}', '--profile-json', '{profile_json}',
               '--output-dir', '{output_dir}', '--phase']
    raw = copy.deepcopy(profile)
    raw.pop('paths')
    raw.update(name=f'Synthetic MMseqs {device.upper()} + native policies', engine='alphafold3',
               release='synthetic-colabfold-database-v1', prepareVersion='mmseqs-af3-v1',
               searchVersion='synthetic-search-not-a-runtime-version-probe',
               validationProfile='af3-mmseqs-v1', assets=assets,
               prepareCommand=command + ['search'], finalizeCommand=command + ['finalize'])
    return raw


def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    registry = root / 'registry.json'
    native = make_fake_native(root)
    registry.write_text(json.dumps(dict(schemaVersion=1, profiles={
        'fixture-cpu': registry_profile(root, 'cpu'),
        'fixture-gpu': registry_profile(root, 'gpu'),
    })))
    os.environ.update({
        'PROTEIN_STUDIO_DATABASE_CONFIG': str(registry),
        'PROTEIN_STUDIO_PREDICTION_MODE': 'workers',
        'PROTEIN_STUDIO_DISTRIBUTED': '0',
        'PROTEIN_STUDIO_CPU_PREP_WORKERS': '2',
        'PROTEIN_STUDIO_CPU_VALIDATE_WORKERS': '1',
        'PROTEIN_STUDIO_READY_HIGH_WATER': '8',
        'PROTEIN_STUDIO_GPU_GROUPS_JSON': '[["GPU-synthetic-inference"]]',
        'PROTEIN_STUDIO_SEARCH_GPU_GROUPS_JSON': '[["GPU-synthetic-search"]]',
        'PROTEIN_STUDIO_GPU_LOCK_DIR': str(root / 'gpu-locks'),
        'PROTEIN_STUDIO_PREPARATION_RESOURCES_JSON': json.dumps(dict(cpuBudget=4, cpuPerPrepare=2)),
        'PYTHONPATH': str(native),
        'FAKE_NATIVE_LOG': str(root / 'native-calls.jsonl'),
    })
    # No global AF3 prepare command: campaign creation must honor its trusted
    # registered profile's commands rather than the default capability status.
    os.environ.pop('PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND', None)
    app = StudioApplication(root / 'data')
    app.pipeline.preparation_router = lambda conn, now: ('1', [])
    http = StudioHTTPServer(('127.0.0.1', 0), app)
    thread = threading.Thread(target=http.serve_forever,
                              kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    app.start()
    base = f'http://127.0.0.1:{http.server_port}'

    def request(path, payload):
        with urlopen(Request(base + path, data=json.dumps(payload).encode(),
                             headers={'Content-Type': 'application/json'}), timeout=5) as response:
            return json.load(response)

    def stop(*args):
        threading.Thread(target=http.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        campaigns = {}
        for device, candidates in [('gpu', ['FGHI', 'KLMN']), ('cpu', ['FGHI'])]:
            campaign = request('/api/campaigns', dict(
                name=f'Synthetic MMseqs {device.upper()} AF3 compatibility', engine='alphafold3',
                databaseProfile='fixture-' + device, status='paused',
                settings={'seeds': [1], 'numSamples': 1},
            ))
            cid = campaigns[device] = campaign['id']
            request(f'/api/campaigns/{cid}/items', {'items': [dict(
                targetId=f'{device}-candidate-{index}', name=f'{device}-candidate-{index}',
                chains=[dict(id='A', sequence='ACDE'), dict(id='B', sequence=sequence)],
            ) for index, sequence in enumerate(candidates)]})
            request(f'/api/campaigns/{cid}/seal', {})
            request(f'/api/campaigns/{cid}/resume', {})
        print('READY ' + json.dumps(dict(port=http.server_port, campaigns=campaigns,
              nativeLog=str(root / 'native-calls.jsonl'), dataRoot=str(root / 'data'),
              scope='Real adapter/registry/HTTP; synthetic search and AF3 APIs; no GPU execution')), flush=True)
        thread.join()
    finally:
        http.shutdown()
        thread.join(2)
        http.server_close()
        app.close()


if __name__ == '__main__':
    main()
