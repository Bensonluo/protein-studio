#!/usr/bin/env python3
"""Launch one persistent worker per explicitly configured GPU group on this host.

No instances are provisioned. Use --dry-run to inspect commands without starting
processes or reading token values. Tokens stay in inherited environment variables.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def plan(config, base=ROOT, executable=None):
    if not isinstance(config, dict) or config.get('schemaVersion') != 1:
        raise ValueError('Fleet configuration requires schemaVersion=1.')
    workers = config.get('workers')
    if not isinstance(workers, list) or not 1 <= len(workers) <= 64:
        raise ValueError('Explicitly configure 1–64 workers on this host.')
    controller = config.get('controllerURL')
    if not isinstance(controller, str) or not controller.startswith(('https://', 'http://127.0.0.1:', 'http://localhost:')):
        raise ValueError('Use HTTPS for remote controllers, or a loopback HTTP URL.')
    seen_ids, assigned = set(), set()
    output = []
    for worker in workers:
        identifier, token_env = worker.get('id'), worker.get('tokenEnv')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', identifier) or identifier in seen_ids:
            raise ValueError('Worker IDs must be unique safe identifiers.')
        if not isinstance(token_env, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]+', token_env):
            raise ValueError('Each worker needs a tokenEnv variable name, never an inline token.')
        seen_ids.add(identifier)
        engine = worker.get('engine', 'alphafold3')
        if engine not in {'alphafold3', 'reference'}:
            raise ValueError('Native resident fleet supports alphafold3 and reference.')
        devices = worker.get('gpuDevices', [])
        if not isinstance(devices, list) or any(not isinstance(d, str) or not d or ',' in d for d in devices) or len(set(devices)) != len(devices):
            raise ValueError('gpuDevices must be a list of distinct device IDs.')
        if engine != 'reference' and not devices:
            raise ValueError('Real model workers require an explicit GPU group.')
        if engine == 'alphafold3' and len(devices) != 1:
            raise ValueError('The native AF3 resident adapter uses one device per process; give each of the 16 GPUs its own worker.')
        if assigned.intersection(devices):
            raise ValueError('GPU groups overlap on this host.')
        assigned.update(devices)
        resident = worker.get('residentCommand')
        if not isinstance(resident, list) or not resident or any(not isinstance(part, str) or not part or '\0' in part for part in resident):
            raise ValueError('residentCommand must be a JSON argv array.')
        work_dir = Path(worker.get('workDir') or base / 'data/workers' / identifier).expanduser()
        if not work_dir.is_absolute():
            work_dir = Path(base) / work_dir
        samples = worker.get('samples', 1 if engine == 'reference' else 5)
        recycles = worker.get('recycles', 3 if engine == 'reference' else 10)
        if isinstance(samples, bool) or not isinstance(samples, int) or not 1 <= samples <= 20:
            raise ValueError('samples must be an integer from 1 to 20.')
        if isinstance(recycles, bool) or not isinstance(recycles, int) or not 1 <= recycles <= 50:
            raise ValueError('recycles must be an integer from 1 to 50.')
        argv = [executable or sys.executable, '-m', 'backend.worker_client', '--url', controller,
                '--worker-id', identifier, '--token-env', token_env, '--engine', engine,
                '--runtime-version', worker.get('runtimeVersion', 'REPLACE_WITH_INSTALLED_VERSION'),
                '--samples', str(samples), '--recycles', str(recycles),
                '--work-dir', str(work_dir), '--resident-command', json.dumps(resident)]
        if devices:
            argv.extend(['--gpu-devices', ','.join(devices)])
        for key,flag in [('residentStartupTimeout','--resident-startup-timeout'),
                         ('residentRequestTimeout','--resident-request-timeout'),('httpTimeout','--http-timeout')]:
            if key in worker:
                value=worker[key]
                if isinstance(value,bool) or not isinstance(value,(float,int)) or not 0<value<=604800:
                    raise ValueError(key+' must be a positive timeout at most seven days.')
                argv.extend([flag,str(value)])
        if engine == 'reference':
            argv.append('--demo')
        output.append({'id': identifier, 'gpuDevices': devices, 'tokenEnv': token_env,
                       'argv': argv, 'workDir': str(work_dir), 'engine': engine})
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--restart-limit', type=int, default=3)
    parser.add_argument('--restart-backoff', type=float, default=5)
    parser.add_argument('--shutdown-grace', type=float, default=300)
    args = parser.parse_args(argv)
    if not 0 <= args.restart_limit <= 10 or not 0 <= args.restart_backoff <= 300 or not 1 <= args.shutdown_grace <= 3600:
        raise ValueError('Invalid restart or shutdown limits.')
    configuration = json.loads(args.config.read_text())
    commands = plan(configuration, args.config.resolve().parent)
    if args.dry_run:
        print(json.dumps({'workers': commands, 'launched': False, 'tokensRead': False}, ensure_ascii=False, indent=2))
        return 0
    for command in commands:
        if not os.environ.get(command['tokenEnv']):
            raise ValueError('Required worker token environment variable is not set: ' + command['tokenEnv'])
    children, stopping = [], False
    restarts, next_restart = [0] * len(commands), [0.0] * len(commands)

    def launch(command):
        environment = os.environ.copy()
        environment['CUDA_VISIBLE_DEVICES'] = ','.join(command['gpuDevices'])
        return subprocess.Popen(command['argv'], cwd=ROOT, env=environment)

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        for process in children:
            if process.poll() is None:
                process.terminate()

    for kind in (signal.SIGINT, signal.SIGTERM):
        signal.signal(kind, stop)
    try:
        for command in commands:
            children.append(launch(command))
            print(f"Started {command['id']} on configured GPU group {command['gpuDevices']}", flush=True)
        while not stopping:
            for index, process in enumerate(children):
                if process.poll() is None or restarts[index] >= args.restart_limit:
                    continue
                if not next_restart[index]:
                    next_restart[index] = time.monotonic() + args.restart_backoff * (restarts[index] + 1)
                    print(f"{commands[index]['id']} exited; restarting only this slot after backoff.", flush=True)
                elif time.monotonic() >= next_restart[index]:
                    children[index] = launch(commands[index])
                    restarts[index] += 1
                    next_restart[index] = 0.0
            if all(process.poll() is not None and restarts[index] >= args.restart_limit for index, process in enumerate(children)):
                raise RuntimeError('All fleet slots exhausted their configured restart limit.')
            time.sleep(.3)
    finally:
        stop(None, None)
        deadline = time.monotonic() + args.shutdown_grace
        for process in children:
            try:
                process.wait(timeout=max(.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
