#!/usr/bin/env python3
"""Validate administrator JSON, then exec the existing Foldspace controller.

--dry-run only builds a plan; --check reads declared files and checks token
presence, without creating state, contacting a server, or loading model weights.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ENV_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
MAX_CONFIG = 2 * 1024 * 1024


def read_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_CONFIG:
        raise ValueError('Configuration exceeds 2 MiB.')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate configuration key: ' + key)
            value[key] = item
        return value
    result = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError('Configuration must be a JSON object.')
    return result


def text(value, name):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError(name + ' must be a nonempty string without NUL.')
    return value


def absolute(value, name):
    value = text(value, name)
    if not Path(value).is_absolute():
        raise ValueError(name + ' must be an absolute path.')
    return value


def integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer in {minimum}..{maximum}.')
    return value


def argv_array(value, name):
    if not isinstance(value, list) or not value or len(value) > 256:
        raise ValueError(name + ' must be a nonempty JSON argv array of at most 256 strings.')
    return [text(item, name + ' argument') for item in value]


def base_config(config, role, allowed):
    if set(config) - allowed:
        raise ValueError('Unknown configuration keys: ' + ', '.join(sorted(set(config) - allowed)))
    if config.get('schemaVersion') != 1 or isinstance(config.get('schemaVersion'), bool):
        raise ValueError('schemaVersion must be 1.')
    if config.get('role') != role:
        raise ValueError('Configuration role must be ' + role + '.')
    return absolute(config.get('projectRoot'), 'projectRoot'), absolute(config.get('python'), 'python')


def readable_file(path, name):
    path = Path(absolute(str(path), name))
    if not path.is_file():
        raise ValueError(name + ' is not a regular file: ' + str(path))
    with path.open('rb') as handle:
        handle.read(1)


def executable(path, name):
    readable_file(path, name)
    if not os.access(path, os.X_OK):
        raise ValueError(name + ' is not executable: ' + str(path))


def check_python(path, environment, minimum=(3, 10)):
    executable(path, 'Python executable')
    result = subprocess.run([path, '-c', 'import sys,json;print(json.dumps(list(sys.version_info[:3])))'],
                            env=environment, text=True, capture_output=True, timeout=15, check=False)
    if result.returncode:
        raise ValueError('Configured Python interpreter cannot start: ' + path)
    try:
        version = json.loads(result.stdout)
    except ValueError as exc:
        raise ValueError('Configured Python did not return a version.') from exc
    if tuple(version[:2]) < minimum:
        raise ValueError(f'Configured Python requires >= {minimum[0]}.{minimum[1]}.')
    return version


def check_command_files(command, name):
    executable(absolute(command[0], name + ' executable'), name + ' executable')
    if len(command) > 1 and command[1].endswith('.py'):
        readable_file(command[1], name + ' Python script')
    for index, argument in enumerate(command):
        if argument in {'--alphafold-script', '--af3-script'}:
            if index + 1 >= len(command):
                raise ValueError(name + ' has a missing script argument.')
            readable_file(command[index + 1], name + ' AF3 script')
        if argument.startswith(('--alphafold-script=', '--af3-script=')):
            readable_file(argument.split('=', 1)[1], name + ' AF3 script')
        if argument.startswith('--') and '_binary_path=' in argument:
            executable(argument.split('=', 1)[1], name + ' tool executable')


def build_plan(config, environ=None):
    root, python = base_config(config, 'controller', {'schemaVersion', 'role', 'projectRoot', 'python', 'environment'})
    raw = config.get('environment')
    if not isinstance(raw, dict):
        raise ValueError('environment must be an object.')
    supplied = {}
    for key, value in raw.items():
        if not ENV_NAME.fullmatch(key) or not key.startswith('PROTEIN_STUDIO_') or any(part in key for part in ('TOKEN', 'SECRET', 'PASSWORD')):
            raise ValueError('Controller JSON environment accepts nonsecret PROTEIN_STUDIO_* settings only.')
        if isinstance(value, (list, dict)) and key.endswith(('_COMMAND', '_JSON')):
            value = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        supplied[key] = text(value, 'Environment ' + key)
    if supplied.get('PROTEIN_STUDIO_HOST') not in {'127.0.0.1', 'localhost', '::1'}:
        raise ValueError('This deployment binds the controller to loopback; expose only /worker/v1/ through the HTTPS proxy.')
    port = supplied.get('PROTEIN_STUDIO_PORT', '8765')
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError('PROTEIN_STUDIO_PORT must be in 1..65535.')
    if supplied.get('PROTEIN_STUDIO_DISTRIBUTED') != '1' or supplied.get('PROTEIN_STUDIO_PREDICTION_MODE') != 'workers':
        raise ValueError('AF3 deployment requires DISTRIBUTED=1 and PREDICTION_MODE=workers.')
    for key in ('PROTEIN_STUDIO_DATA_DIR', 'PROTEIN_STUDIO_DATABASE_CONFIG', 'PROTEIN_STUDIO_WORKER_CONFIG'):
        absolute(supplied.get(key), key)
    for key in ('PROTEIN_STUDIO_CPU_PREP_WORKERS', 'PROTEIN_STUDIO_GPU_WORKERS', 'PROTEIN_STUDIO_CPU_VALIDATE_WORKERS', 'PROTEIN_STUDIO_READY_HIGH_WATER'):
        if key in supplied and (not supplied[key].isdigit() or int(supplied[key]) < 1):
            raise ValueError(key + ' must be a positive integer string.')
    command_key = 'PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND'
    if command_key not in supplied:
        raise ValueError('An explicit AF3 PREPARE_COMMAND is required.')
    commands = {}
    for key, value in supplied.items():
        if key.endswith('_COMMAND'):
            try:
                commands[key] = argv_array(json.loads(value), key)
            except json.JSONDecodeError as exc:
                raise ValueError(key + ' must contain a JSON argv array.') from exc
            absolute(commands[key][0], key + ' executable')
    env = dict(os.environ if environ is None else environ)
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    env.update(supplied)
    return dict(role='controller', projectRoot=root, argv=[python, '-m', 'backend.server'],
                environment=env, configuredEnvironmentKeys=sorted(supplied), commands=commands)


def check_plan(plan):
    root = Path(plan['projectRoot'])
    readable_file(root / 'backend' / 'server.py', 'Controller module')
    version = check_python(plan['argv'][0], plan['environment'])
    env = plan['environment']
    for name in ('PROTEIN_STUDIO_DATABASE_CONFIG', 'PROTEIN_STUDIO_WORKER_CONFIG'):
        readable_file(env[name], name)
    for name, command in plan['commands'].items():
        check_command_files(command, name)
    check_python(plan['commands']['PROTEIN_STUDIO_ALPHAFOLD3_PREPARE_COMMAND'][0], env, minimum=(3, 12))
    probe = '''import json,os,re
from backend.databases import list_profiles
from backend.distributed import load_config
profiles=list_profiles(os.environ['PROTEIN_STUDIO_DATABASE_CONFIG'])
workers=load_config(os.environ['PROTEIN_STUDIO_WORKER_CONFIG'])['workers']
names=[w['tokenEnv'] for w in workers]
invalid=[n for n in names if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',n)]
missing=[n for n in names if len(os.environ.get(n,''))<24 or any(c.isspace() for c in os.environ.get(n,''))]
real=[p for p in profiles if p['engine']=='alphafold3']
print(json.dumps(dict(databaseProfiles=[dict(id=p['id'],ready=p['ready'],diagnostics=p['diagnostics']) for p in real],workerCount=len(workers),tokenEnvironmentNames=names,invalidTokenNames=invalid,missingTokenNames=missing,ready=bool(real) and all(p['ready'] for p in real) and bool(workers) and not missing and not invalid)))
'''
    result = subprocess.run([plan['argv'][0], '-c', probe], cwd=root, env=env,
                            text=True, capture_output=True, timeout=60, check=False)
    if result.returncode:
        # An exception can include arbitrary administrator file content. Keep
        # the launcher output bounded and never echo the environment or stderr.
        raise ValueError('Database/worker registry preflight failed; validate the registered JSON and declared paths.')
    report = json.loads(result.stdout)
    report.update(role='controller', pythonVersion=version, scope='configuration, declared paths and token presence; no model/GPU execution')
    return report


def safe_plan(plan):
    return dict(role=plan['role'], projectRoot=plan['projectRoot'], argv=plan['argv'],
                configuredEnvironmentKeys=plan['configuredEnvironmentKeys'],
                note='Dry run: no token values read or printed; no runtime/filesystem installation checks executed.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        plan = build_plan(read_json(args.config), environ={} if args.dry_run else None)
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
        print('Controller configuration error: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
