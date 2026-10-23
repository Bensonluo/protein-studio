#!/usr/bin/env python3
"""Run a loopback controller and two real HTTP fixture worker processes.

Locations local/aliyun demonstrate routing labels on this computer, not an actual
cloud GPU. Model loads remain zero. Secrets are ephemeral environment variables.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def request(base, path, payload=None):
    encoded = None if payload is None else json.dumps(payload).encode()
    request = Request(base + path, data=encoded, headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def seed(base, count):
    example = request(base, '/api/examples/crambin')
    campaign = request(base, '/api/campaigns', {'name': '混合云常驻批次演示 · 公开结构回放',
        'engine': 'reference', 'databaseProfile': 'reference', 'settings': {'example': 'crambin', 'seeds': [1], 'numSamples': 1, 'numRecycles': 3},
        'status': 'paused'})
    for start in range(0, count, 100):
        items = [{'targetId': f'hybrid-demo-{i:04}', 'name': f'公开参考目标 {i + 1:03}', 'chains': example['chains']}
                 for i in range(start, min(count, start + 100))]
        request(base, f"/api/campaigns/{campaign['id']}/items", {'items': items})
    request(base, f"/api/campaigns/{campaign['id']}/seal", {})
    request(base, f"/api/campaigns/{campaign['id']}/resume", {})
    return campaign['id']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--seed-demo', action='store_true')
    parser.add_argument('--targets', type=int, default=48)
    parser.add_argument('--batch-targets', type=int, default=4)
    parser.add_argument('--lease-seconds', type=int, default=30)
    parser.add_argument('--reference-delay', type=float, default=0)
    args = parser.parse_args(argv)
    if not 1 <= args.targets <= 500 or not 1 <= args.batch_targets <= 32:
        raise ValueError('Demo supports 1–500 targets and batch size 1–32.')
    if not 3 <= args.lease_seconds <= 3600 or not 0 <= args.reference_delay <= 30:
        raise ValueError('Demo lease must be 3–3600 seconds and replay delay 0–30 seconds.')
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', args.port)) == 0:
            raise RuntimeError(f'Port {args.port} is already in use; stop that service or choose --port.')
    data = args.data_dir.resolve()
    demo_root = data / 'hybrid-demo'
    demo_root.mkdir(parents=True, exist_ok=True)
    configuration = {'schemaVersion': 1, 'leaseSeconds': args.lease_seconds, 'batchTargets': args.batch_targets,
        'batchResidues': 8192, 'maxBundleBytes': 268435456, 'maxExpandedBytes': 1073741824,
        'planned': {'localGpuCards': 16}, 'workers': [
        {'id': 'local-demo', 'name': '本地演示节点 · 无 GPU', 'tokenEnv': 'PS_LOCAL_DEMO_TOKEN',
         'location': 'local', 'mode': 'fixture', 'engines': ['reference']},
        {'id': 'aliyun-demo', 'name': '阿里云路径演示 · 本机回环', 'tokenEnv': 'PS_ALI_DEMO_TOKEN',
         'location': 'aliyun', 'mode': 'fixture', 'engines': ['reference']}]}
    config_path = demo_root / 'workers.json'
    config_path.write_text(json.dumps(configuration, ensure_ascii=False, indent=2))
    environment = os.environ.copy()
    environment.update(PROTEIN_STUDIO_DISTRIBUTED='1', PROTEIN_STUDIO_PREDICTION_MODE='workers',
        PROTEIN_STUDIO_WORKER_CONFIG=str(config_path), PROTEIN_STUDIO_DATA_DIR=str(data),
        PROTEIN_STUDIO_HOST='127.0.0.1', PROTEIN_STUDIO_PORT=str(args.port),
        PS_LOCAL_DEMO_TOKEN=secrets.token_urlsafe(32), PS_ALI_DEMO_TOKEN=secrets.token_urlsafe(32))
    base = f'http://127.0.0.1:{args.port}'
    processes, logs, stopping = [], [], False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for name, command in [('controller', [sys.executable, '-m', 'backend.server'])]:
            log = (demo_root / (name + '.log')).open('a')
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT))
        deadline = time.monotonic() + 30
        while True:
            if processes[0].poll() is not None:
                raise RuntimeError('Controller exited. Inspect data/hybrid-demo/controller.log.')
            try:
                request(base, '/api/health')
                break
            except (URLError, OSError):
                if time.monotonic() > deadline:
                    raise RuntimeError('Controller did not become ready within 30 seconds.')
                time.sleep(.1)
        for worker in configuration['workers']:
            log = (demo_root / (worker['id'] + '.log')).open('a')
            logs.append(log)
            command = [sys.executable, '-m', 'backend.worker_client', '--url', base,
                '--worker-id', worker['id'], '--token-env', worker['tokenEnv'], '--engine', 'reference',
                '--runtime-version', 'fixture-v1', '--samples', '1', '--recycles', '3',
                '--work-dir', str(demo_root / worker['id']), '--resident-command',
                json.dumps([sys.executable, str(ROOT / 'scripts/resident_runner.py'), '--engine', 'reference',
                            '--reference-delay', str(args.reference_delay)]), '--demo']
            processes.append(subprocess.Popen(command, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT))
        campaign = seed(base, args.targets) if args.seed_demo else None
        print(json.dumps({'url': base + '/#cluster', 'campaignId': campaign,
                          'actualCloudGpuConnected': False, 'mode': 'real loopback HTTP fixture workers',
                          'logs': str(demo_root)}, ensure_ascii=False), flush=True)
        while not stopping:
            if any(process.poll() is not None for process in processes):
                raise RuntimeError('A demo process exited. Inspect logs; pending leases remain recoverable.')
            time.sleep(.3)
    finally:
        # Stop workers first; the controller stays alive while they drain.
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
