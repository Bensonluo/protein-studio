#!/usr/bin/env python3
"""Launch one explicitly mapped native AF3 GPU slot from administrator JSON."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
try:
    from .run_controller import (ENV_NAME, absolute, argv_array, base_config, check_command_files,
                                check_python, integer, read_json, readable_file, text)
except ImportError:
    from run_controller import (ENV_NAME, absolute, argv_array, base_config, check_command_files,
                               check_python, integer, read_json, readable_file, text)

GPU_ENVIRONMENT = {'XLA_FLAGS', 'XLA_PYTHON_CLIENT_PREALLOCATE', 'XLA_CLIENT_MEM_FRACTION',
                   'JAX_COMPILATION_CACHE_DIR', 'JAX_LOG_COMPILES', 'OMP_NUM_THREADS',
                   'TF_CPP_MIN_LOG_LEVEL', 'SSL_CERT_FILE'}
TIMEOUTS = {'residentStartupTimeout': ('--resident-startup-timeout', 600, 86400),
            'residentRequestTimeout': ('--resident-request-timeout', 7200, 604800),
            'httpTimeout': ('--http-timeout', 60, 3600)}


def option_value(command, flag):
    matches = []
    for index, value in enumerate(command):
        if value == flag:
            if index + 1 == len(command):
                raise ValueError(flag + ' requires a value.')
            matches.append(command[index + 1])
        elif value.startswith(flag + '='):
            matches.append(value.split('=', 1)[1])
    if len(matches) > 1:
        raise ValueError('Duplicate resident option: ' + flag)
    return matches[0] if matches else None


def build_plan(config, environ=None, token_env=None):
    fields = {'schemaVersion', 'role', 'projectRoot', 'python', 'url', 'workerId', 'tokenEnv', 'engine',
              'runtimeVersion', 'workDir', 'gpuDevices', 'samples', 'recycles', 'residentCommand',
              'environment', *TIMEOUTS}
    root, python = base_config(config, 'gpu-worker', fields)
    url = text(config.get('url'), 'url')
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('url must be a plain HTTP(S) origin without credentials, path, query or fragment.')
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('Controller port must be in 1..65535.')
    if parsed.scheme != 'https' and parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise ValueError('Remote workers require HTTPS; this launcher has no unsafe HTTP override.')
    token_env = token_env or config.get('tokenEnv')
    if not isinstance(token_env, str) or not ENV_NAME.fullmatch(token_env) or token_env in GPU_ENVIRONMENT or token_env in {'PATH', 'PYTHONPATH', 'PYTHONHOME', 'CUDA_VISIBLE_DEVICES'}:
        raise ValueError('tokenEnv must be a dedicated valid environment variable name.')
    worker_id = text(config.get('workerId'), 'workerId')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', worker_id):
        raise ValueError('workerId must use 1..80 letters, digits, dots, hyphens or underscores.')
    if config.get('engine') != 'alphafold3':
        raise ValueError('This deployment launcher runs the native alphafold3 engine.')
    devices = config.get('gpuDevices')
    if not isinstance(devices, list) or len(devices) != 1 or not isinstance(devices[0], str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]+', devices[0]):
        raise ValueError('gpuDevices must contain one explicit device index or GPU/MIG identifier; create one worker per native AF3 GPU slot.')
    samples = integer(config.get('samples'), 'samples', 1, 20)
    recycles = integer(config.get('recycles'), 'recycles', 1, 50)
    command = argv_array(config.get('residentCommand'), 'residentCommand')
    absolute(command[0], 'residentCommand interpreter')
    if len(command) < 2 or not command[1].endswith('/scripts/resident_runner.py'):
        raise ValueError('residentCommand must invoke the deployed scripts/resident_runner.py directly.')
    if option_value(command, '--engine') != 'alphafold3':
        raise ValueError('residentCommand --engine must be alphafold3.')
    for field, flag in ((samples, '--num-samples'), (recycles, '--num-recycles')):
        if option_value(command, flag) != str(field):
            raise ValueError(flag + ' must match worker settings explicitly.')
    if option_value(command, '--device') not in (None, '0'):
        raise ValueError('One mapped CUDA_VISIBLE_DEVICES slot uses resident --device 0.')
    work_dir = absolute(config.get('workDir'), 'workDir')
    allowed_root = option_value(command, '--allowed-root')
    if allowed_root is not None and not Path(work_dir).resolve().is_relative_to(Path(absolute(allowed_root, 'resident allowed root')).resolve()):
        raise ValueError('workDir must be inside the resident --allowed-root directory.')
    version = text(config.get('runtimeVersion'), 'runtimeVersion')
    if option_value(command, '--model-version') != version:
        raise ValueError('residentCommand --model-version must match runtimeVersion exactly.')
    absolute(option_value(command, '--af3-script'), 'AF3 source script')
    absolute(option_value(command, '--model-dir'), 'AF3 model directory')
    configured = config.get('environment', {})
    if not isinstance(configured, dict) or set(configured) - GPU_ENVIRONMENT:
        raise ValueError('worker.environment contains unsupported keys; GPU/runtime variables are explicitly allowlisted.')
    configured = {key: text(value, 'Environment ' + key) for key, value in configured.items()}
    env = dict(os.environ if environ is None else environ)
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    env.update(configured)
    env['CUDA_VISIBLE_DEVICES'] = devices[0]
    argv = [python, '-m', 'backend.worker_client', '--url', url.rstrip('/'), '--worker-id', worker_id,
            '--token-env', token_env, '--engine', 'alphafold3', '--runtime-version', version,
            '--work-dir', work_dir, '--gpu-devices', devices[0], '--samples', str(samples),
            '--recycles', str(recycles), '--resident-command', json.dumps(command, ensure_ascii=False)]
    for key, (flag, default, maximum) in TIMEOUTS.items():
        value = integer(config.get(key, default), key, 1, maximum)
        argv.extend([flag, str(value)])
    return dict(role='gpu-worker', projectRoot=root, argv=argv, environment=env, tokenEnv=token_env,
                residentCommand=command, gpuDevices=devices, workerId=worker_id,
                configuredEnvironmentKeys=sorted(configured))


def check_plan(plan):
    root = Path(plan['projectRoot'])
    readable_file(root / 'backend' / 'worker_client.py', 'Worker module')
    version = check_python(plan['argv'][0], plan['environment'])
    check_command_files(plan['residentCommand'], 'AF3 resident')
    resident_version = check_python(plan['residentCommand'][0], plan['environment'], minimum=(3, 12))
    if 'SSL_CERT_FILE' in plan['environment']:
        readable_file(plan['environment']['SSL_CERT_FILE'], 'TLS CA certificate file')
    secret = plan['environment'].get(plan['tokenEnv'], '')
    present = len(secret) >= 24 and not any(char.isspace() for char in secret)
    return dict(role='gpu-worker', ready=present, workerId=plan['workerId'], gpuDevices=plan['gpuDevices'],
                pythonVersion=version, residentPythonVersion=resident_version,
                tokenEnvironmentName=plan['tokenEnv'], tokenConfigured=present,
                scope='launcher paths and token presence only; run the separate AF3 probe for driver/JAX/GPU/weights')


def safe_plan(plan):
    return dict(role=plan['role'], projectRoot=plan['projectRoot'], argv=plan['argv'],
                gpuDevices=plan['gpuDevices'], configuredEnvironmentKeys=plan['configuredEnvironmentKeys'],
                tokenEnvironmentName=plan['tokenEnv'],
                note='Dry run: token values are not read or printed; no GPU, model or network execution.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--token-env', help='Override the variable NAME, never supply a token value here.')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--check', action='store_true')
    modes.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        plan = build_plan(read_json(args.config), environ={} if args.dry_run else None, token_env=args.token_env)
        if args.dry_run:
            print(json.dumps(safe_plan(plan), ensure_ascii=False, indent=2))
            return 0
        report = check_plan(plan)
        if args.check or not report['ready']:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report['ready']:
            return 2
        if args.check:
            return 0
        os.chdir(plan['projectRoot'])
        os.execve(plan['argv'][0], plan['argv'], plan['environment'])
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print('GPU worker configuration error: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
