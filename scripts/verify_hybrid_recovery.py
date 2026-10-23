#!/usr/bin/env python3
"""Pause an explicitly local fixture worker and verify per-target lease recovery.

Run only against start_hybrid_demo.py on a loopback port, with a short lease and
--reference-delay. No cloud APIs, model downloads, or real GPU tasks are used.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', default='http://127.0.0.1:8771')
    parser.add_argument('--targets', type=int, default=18)
    parser.add_argument('--output', type=Path, default=Path('artifacts/hybrid-recovery-verification.json'))
    args = parser.parse_args()
    base = args.server.rstrip('/')
    if os.name != 'posix' or urlsplit(base).hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise ValueError('Recovery signal test is restricted to a POSIX loopback fixture environment.')

    def request(path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = Request(base + path, data=data, headers={'Content-Type': 'application/json'})
        with urlopen(req, timeout=10) as response:
            return json.load(response)

    def wait(predicate, seconds=45):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.04)
        raise TimeoutError('Recovery verification condition was not reached.')

    initial = wait(lambda: (value if len((value := request('/api/cluster'))['nodes']) == 2 else None))
    local = next(node for node in initial['nodes'] if node['id'] == 'local-demo')
    if local.get('mode') != 'fixture' or not local.get('demo'):
        raise ValueError('The selected node must be an explicit fixture; refusing to signal a real GPU worker.')
    parent = int(subprocess.check_output(['ps', '-p', str(local['pid']), '-o', 'ppid='], text=True).strip())
    command = subprocess.check_output(['ps', '-p', str(parent), '-o', 'args='], text=True)
    if 'backend.worker_client' not in command or '--worker-id local-demo' not in command or base not in command:
        raise ValueError('Fixture resident parent does not match this exact test worker/controller.')
    example = request('/api/examples/crambin')
    campaign = request('/api/campaigns', {'name': '故障验收 · 仅恢复未交付目标', 'engine': 'reference',
        'databaseProfile': 'reference', 'settings': {'example': 'crambin', 'seeds': [1], 'numSamples': 1, 'numRecycles': 3}, 'status': 'paused'})
    cid = campaign['id']
    request(f'/api/campaigns/{cid}/items', {'items': [{'targetId': f'recovery-{i:03}', 'chains': example['chains']}
                                                   for i in range(args.targets)]})
    request(f'/api/campaigns/{cid}/seal', {})
    request(f'/api/campaigns/{cid}/resume', {})

    def partially_delivered():
        data = request('/api/cluster')
        for batch in data['batches']:
            if batch['nodeId'] == 'local-demo' and 0 < batch['completed'] < batch['total'] and any(m['campaignId'] == cid for m in batch['members']):
                return batch
        return None

    batch = wait(partially_delivered)
    accepted_ids = {member['itemId'] for member in batch['members']
                    if member['stage'] in {'validating', 'completed'} or member['deliveryState'] in {'uploaded', 'validated', 'completed'}}
    pending_ids = {member['itemId'] for member in batch['members']} - accepted_ids
    if not accepted_ids or not pending_ids:
        raise AssertionError('Need a partially delivered batch to test preservation.')
    started = time.monotonic()
    os.kill(parent, signal.SIGSTOP)
    try:
        final = wait(lambda: (value if (value := request(f'/api/campaigns/{cid}'))['counts']['completed'] == args.targets else None), 60)
        preserved, recovered = [], []
        for item_id in sorted(accepted_ids | pending_ids):
            item = request(f'/api/campaigns/{cid}/items/{item_id}')
            provenance = item['result']['provenance']
            record = dict(itemId=item_id, workerId=provenance['workerId'], batchId=provenance['batchId'], attempt=provenance['attempt'])
            if item_id in accepted_ids:
                if provenance['workerId'] != 'local-demo' or provenance['batchId'] != batch['id']:
                    raise AssertionError('Previously delivered target was unexpectedly recomputed.')
                preserved.append(record)
            else:
                if provenance['workerId'] != 'aliyun-demo' or provenance['batchId'] == batch['id']:
                    raise AssertionError('Unsubmitted target was not recovered by the other worker.')
                recovered.append(record)
    finally:
        os.kill(parent, signal.SIGCONT)
    # Give the old worker a chance to observe revoked tokens and stale uploads.
    time.sleep(1)
    after = request(f'/api/campaigns/{cid}')
    if after['counts']['completed'] != args.targets:
        raise AssertionError('Late old worker affected the completed manifest.')
    cluster = request('/api/cluster')
    report = {'date': '2026-10-23', 'scope': 'Actual POSIX SIGSTOP/SIGCONT on our loopback fixture worker; real HTTP leases and files; no cloud GPU',
              'campaignId': cid, 'targets': args.targets, 'completed': after['counts']['completed'],
              'pausedWorkerId': 'local-demo', 'preservedAccepted': preserved, 'recoveredUnsubmitted': recovered,
              'recoveryWallSeconds': round(time.monotonic() - started, 3),
              'timingScope': 'Control-plane/replay-delay fault injection, not protein inference performance',
              'lateAttemptCouldOverwrite': False, 'events': cluster.get('events', [])[:30],
              'actualGpuInference': False, 'actualCloudConnection': False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: report[key] for key in ['targets', 'completed', 'preservedAccepted', 'recoveredUnsubmitted', 'lateAttemptCouldOverwrite']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
