#!/usr/bin/env python3
"""Inspect an installed AF3 v3.0.4 NVIDIA runtime; never download assets.

Run with the AF3 environment's Python. --load-model starts the actual resident
once and releases it after startup; it does not execute a prediction or warm JIT.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import importlib
import importlib.metadata
import importlib.util
import inspect
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.resident import ResidentProcess

SOURCE_BASE = 'https://github.com/google-deepmind/alphafold3/blob/v3.0.4/'
PINNED_COMMIT = '85c4d20505fd5cef05eac22b534d4e793971ae69'


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def add_runtime_arguments(parser):
    parser.add_argument('--af3-script', type=Path, required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--model-version', required=True, help='Administrator-declared installation/weight version.')
    parser.add_argument('--expected-script-sha256', help='Optional trusted deployment manifest digest.')
    parser.add_argument('--weights-file', default='af3.bin.zst', help='Single bundle basename for optional trusted SHA verification.')
    parser.add_argument('--expected-weights-sha256', help='Stream and verify this single bundle against a trusted SHA-256.')
    parser.add_argument('--weights-manifest', type=Path, help='Trusted schemaVersion=1 manifest listing every selected weight shard.')
    parser.add_argument('--device', type=int, default=0, help='Index within CUDA_VISIBLE_DEVICES.')
    parser.add_argument('--attention', choices=['triton', 'cudnn', 'xla'], default='triton')
    parser.add_argument('--num-samples', type=int, default=5)
    parser.add_argument('--num-recycles', type=int, default=10)
    parser.add_argument('--buckets', default='256,512,768,1024,1280,1536,2048,2560,3072,3584,4096,4608,5120')
    parser.add_argument('--max-template-date', default='2021-09-30')
    parser.add_argument('--compilation-cache', type=Path)
    parser.add_argument('--startup-timeout', type=float, default=600)


def resident_command(args):
    command = [sys.executable, str(ROOT / 'scripts/resident_runner.py'), '--engine', 'alphafold3',
               '--af3-script', str(args.af3_script.resolve()), '--model-dir', str(args.model_dir.resolve()),
               '--model-version', args.model_version, '--device', str(args.device), '--attention', args.attention,
               '--num-samples', str(args.num_samples), '--num-recycles', str(args.num_recycles),
               '--buckets', args.buckets, '--max-template-date', args.max_template_date]
    if args.compilation_cache:
        command.extend(['--compilation-cache', str(args.compilation_cache.resolve())])
    return command


def native_source(args):
    script = args.af3_script.resolve()
    digest = sha256(script)
    if args.expected_script_sha256 and digest != args.expected_script_sha256:
        raise ValueError('Native source does not match the trusted expected SHA-256.')
    details = dict(path=str(script), sha256=digest, expectedShaMatched=bool(args.expected_script_sha256),
                   requestedApiContract='v3.0.4', expectedSourceCommit=PINNED_COMMIT, releaseVerified=False)
    def git(*arguments):
        result = subprocess.run(['git', '-C', str(script.parent), *arguments], capture_output=True,
                                text=True, timeout=5, check=True)
        return result.stdout.strip()
    try:
        details['gitCommit'] = git('rev-parse', 'HEAD')
        if details['gitCommit'] != PINNED_COMMIT:
            raise ValueError('Installed source Git commit differs from the pinned AF3 v3.0.4 commit.')
        details['gitExactTag'] = git('describe', '--tags', '--exact-match', 'HEAD')
        details['scriptDirty'] = bool(git('status', '--porcelain', '--', str(script)))
        details['releaseVerified'] = details['gitExactTag'] == 'v3.0.4' and not details['scriptDirty']
        if details['scriptDirty']:
            raise ValueError('Installed run_alphafold.py differs from the pinned Git revision.')
    except (OSError, subprocess.SubprocessError):
        details['releaseVerificationNote'] = 'No clean exact v3.0.4 Git tag proof; inspect source digest and deployment manifest.'
    return details


def import_native(path):
    script = Path(path).resolve()
    sys.path.insert(0, str(script.parent))
    spec = importlib.util.spec_from_file_location('_protein_studio_probe_native', script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # Bind the exact keyword shape used by our adapter without executing a model.
    inspect.signature(module.make_model_config).bind(flash_attention_implementation='triton',
        num_diffusion_samples=5, num_recycles=10, return_embeddings=False, return_distogram=False)
    inspect.signature(module.ModelRunner).bind(config=None, device=None, model_dir=None)
    inspect.signature(module.process_fold_input).bind(fold_input=None, data_pipeline_config=None,
        model_runner=None, output_dir=None, buckets=None, ref_max_modified_date=None,
        conformer_max_iterations=None, resolve_msa_overlaps=True, fix_standalone_glycans=False,
        force_output_dir=False, compress_large_output_files=False, save_terms_of_use=True)
    return module


def nvidia_inventory(binary='nvidia-smi'):
    command = [binary, '--query-gpu=index,uuid,name,memory.total,memory.free,driver_version',
               '--format=csv,noheader,nounits']
    result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=True)
    devices = []
    for row in csv.reader(result.stdout.splitlines()):
        if not row:
            continue
        if len(row) != 6:
            raise ValueError('Unexpected nvidia-smi inventory columns.')
        index, uuid, name, total, free, driver = [value.strip() for value in row]
        devices.append(dict(index=int(index), uuid=uuid, name=name, memoryTotalMiB=float(total),
                            memoryFreeMiB=float(free), driverVersion=driver))
    if not devices:
        raise ValueError('nvidia-smi reported no NVIDIA GPUs.')
    return dict(devices=devices, scope='Physical nvidia-smi inventory; JAX visibility may be a subset or MIG device.')


def jax_inventory(args):
    jax = importlib.import_module('jax')
    devices = jax.local_devices(backend='gpu')
    if not devices or not 0 <= args.device < len(devices):
        raise ValueError('Requested visible GPU is unavailable to JAX.')
    selected = devices[args.device]
    capability = float(selected.compute_capability)
    if capability < 6:
        raise ValueError('GPU compute capability is below the native v3.0.4 minimum.')
    if 7 <= capability < 8 and (args.attention != 'xla' or
            '--xla_disable_hlo_passes=custom-kernel-fusion-rewriter' not in os.environ.get('XLA_FLAGS', '')):
        raise ValueError('GPU 7.x requires xla attention and the official XLA_FLAGS workaround.')
    rows = []
    for device in devices:
        rows.append(dict(id=device.id, platform=device.platform, deviceKind=device.device_kind,
                         processIndex=device.process_index, computeCapability=str(device.compute_capability)))
    return dict(version=jax.__version__, defaultBackend=jax.default_backend(), requestedBackend='gpu',
                devices=rows, selectedVisibleIndex=args.device, selectedDeviceKind=selected.device_kind)


def resource_inventory():
    chemical = importlib.import_module('alphafold3.constants.chemical_components')
    sets = importlib.import_module('alphafold3.constants.chemical_component_sets')
    cpp = importlib.import_module('alphafold3.cpp')
    paths = {'ccd': Path(chemical._CCD_PICKLE_FILE), 'chemicalComponentSets': Path(sets._CCD_SETS_CCD_PICKLE_FILE),
             'outputTerms': Path(cpp.__file__).parent / 'OUTPUT_TERMS_OF_USE.md'}
    resources = {}
    for name, path in paths.items():
        size = path.stat().st_size
        if not size:
            raise ValueError('Empty required AF3 resource: ' + str(path))
        resources[name] = dict(path=str(path.resolve()), bytes=size)
    ccd = chemical.Ccd()
    if 'ALA' not in ccd:
        raise ValueError('Installed CCD cannot resolve standard alanine.')
    return dict(files=resources, ccdEntries=len(ccd), alaninePresent=True, loadChecked=True)


def weight_inventory(native, directory):
    # Use the official selector, including its multiple-model/sharded format rules.
    files, compressed = native.params.select_model_files(model_dir=directory.resolve())
    rows = []
    for source in files:
        path = Path(source)
        with path.open('rb') as stream:
            if not stream.read(1):
                raise ValueError('Selected weight shard is empty: ' + str(path))
        rows.append(dict(path=str(path.resolve()), bytes=path.stat().st_size))
    if not rows:
        raise ValueError('No model parameter files selected.')
    return dict(files=rows, compressed=compressed, contentDecoded=False, integrity='Existence/readability only; --load-model decodes weights.')


def verify_weight_digest(args):
    if not args.weights_file or Path(args.weights_file).name != args.weights_file:
        raise ValueError('--weights-file must be a basename inside --model-dir.')
    path = args.model_dir.resolve() / args.weights_file
    if not path.resolve().is_relative_to(args.model_dir.resolve()):
        raise ValueError('Weight bundle resolves outside --model-dir.')
    actual = sha256(path)
    details = dict(path=str(path), bytes=path.stat().st_size, sha256=actual,
                   expectedSha256=args.expected_weights_sha256, expectedMatch=actual == args.expected_weights_sha256,
                   scope='This single bundle only, not a hash of a complete sharded weight set.')
    return details


def verify_weight_manifest(native, args):
    path = args.weights_manifest.resolve()
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('Expected weight manifest exceeds 1 MiB.')
    document = json.loads(path.read_text())
    files = document.get('files')
    if document.get('schemaVersion') != 1 or not isinstance(files, list) or not 1 <= len(files) <= 1024:
        raise ValueError('Weight manifest needs schemaVersion=1 and 1–1024 files.')
    normalized, seen = [], set()
    for item in files:
        name, digest = item.get('file'), item.get('sha256')
        if (not isinstance(name, str) or not name or Path(name).name != name or name in {'.', '..'}
            or '\\' in name or ':' in name or name in seen or not re.fullmatch('[a-f0-9]{64}', digest or '')):
            raise ValueError('Weight manifest entries need unique safe basenames and lowercase SHA-256 digests.')
        seen.add(name)
        normalized.append(dict(file=name, sha256=digest))
    normalized.sort(key=lambda item: item['file'])
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    manifest_digest = hashlib.sha256(canonical).hexdigest()
    if 'manifestSha256' in document and document['manifestSha256'] != manifest_digest:
        raise ValueError('Declared manifestSha256 does not match the canonical complete manifest.')
    selected, compressed = native.params.select_model_files(model_dir=args.model_dir.resolve())
    if {Path(file).name for file in selected} != seen:
        raise ValueError('Expected manifest file set differs from the official selected model/shard set.')
    results = []
    for expected in normalized:
        file = args.model_dir.resolve() / expected['file']
        if not file.resolve().is_relative_to(args.model_dir.resolve()):
            raise ValueError('Weight shard resolves outside --model-dir.')
        actual = sha256(file)
        results.append(dict(file=expected['file'], bytes=file.stat().st_size, sha256=actual,
                            expectedSha256=expected['sha256'], expectedMatch=actual == expected['sha256']))
    return dict(path=str(path), manifestSha256=manifest_digest,
                canonicalization='sorted file names; JSON list of {file,sha256}; UTF-8, ensure_ascii=False, separators=(comma,colon)',
                files=results, officialSelectedSetMatches=True, compressed=compressed,
                expectedMatch=all(item['expectedMatch'] for item in results))


def collect_probe(args):
    report = dict(schemaVersion=1, kind='af3-nvidia-runtime-probe', status='failed',
        checkedAt=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        environment=dict(python=sys.version, executable=sys.executable, platform=platform.platform(),
                         cwd=str(Path.cwd()), cudaVisibleDevices=os.environ.get('CUDA_VISIBLE_DEVICES'),
                         xlaFlags=os.environ.get('XLA_FLAGS'), libcifppDataDir=os.environ.get('LIBCIFPP_DATA_DIR')),
        declaredModelVersion=args.model_version, checks={},
        notExecuted=['database search', 'prediction', 'JIT shape warmup', 'scientific accuracy validation'],
        sources=[SOURCE_BASE + path for path in ['run_alphafold.py', 'pyproject.toml',
            'src/alphafold3/model/params.py', 'src/alphafold3/build_data.py']])
    def check(name, operation):
        started = time.monotonic()
        try:
            details = operation()
            report['checks'][name] = dict(status='passed', details=details, seconds=time.monotonic()-started)
            return details
        except Exception as exc:
            report['checks'][name] = dict(status='failed', error=str(exc)[:3000],
                errorType=type(exc).__name__, seconds=time.monotonic()-started)
            return None
    def python_check():
        if sys.version_info < (3, 12):
            raise RuntimeError('Official v3.0.4 requires Python >=3.12; use the AF3 environment Python.')
        return dict(version=platform.python_version())
    check('python', python_check)
    def config_check():
        buckets = [int(value) for value in args.buckets.split(',')]
        if not 1 <= args.num_samples <= 20 or not 1 <= args.num_recycles <= 50 or args.device < 0:
            raise ValueError('Invalid samples/recycles/visible device index for the resident adapter.')
        if not buckets or min(buckets) <= 0 or sorted(set(buckets)) != buckets:
            raise ValueError('Buckets must be strictly increasing positive integers.')
        datetime.date.fromisoformat(args.max_template_date)
        return dict(numSamples=args.num_samples, numRecycles=args.num_recycles, buckets=buckets, device=args.device)
    check('configuration', config_check)
    check('nativeSource', lambda: native_source(args))
    check('nvidia', lambda: nvidia_inventory(args.nvidia_smi))
    check('jax', lambda: jax_inventory(args))
    native_holder = []
    def native_check():
        native_holder.append(import_native(args.af3_script))
        try:
            version_module = importlib.import_module('alphafold3.version')
            imported_version = getattr(version_module, '__version__', None)
            imported_version_path = getattr(version_module, '__file__', None)
        except ImportError:
            imported_version, imported_version_path = None, None
        return dict(apiBindingCompatible=True, modulePath=str(Path(native_holder[0].__file__).resolve()),
                    importedAlphaFoldVersion=imported_version, importedVersionModulePath=imported_version_path,
                    dataPipeline='disabled by data_pipeline_config=None; featurisation still runs in prediction')
    check('nativeImport', native_check)
    check('resources', resource_inventory)
    if native_holder:
        check('weights', lambda: weight_inventory(native_holder[0], args.model_dir))
    else:
        report['checks']['weights'] = dict(status='not_run', reason='Native import failed; official selector unavailable.',
                                           directory=str(args.model_dir.resolve()), directoryExists=args.model_dir.is_dir())
    if args.expected_weights_sha256:
        details = check('weightDigest', lambda: verify_weight_digest(args))
        if details and not details['expectedMatch']:
            report['checks']['weightDigest'].update(status='failed', error='Weight bundle SHA-256 differs from the trusted manifest.')
        selected = report['checks']['weights'].get('details', {}).get('files', [])
        if details and {Path(item['path']).name for item in selected} != {args.weights_file}:
            report['checks']['weightDigest'].update(status='failed',
                error='Single-bundle SHA must identify the entire officially selected weight set; use --weights-manifest for shards.')
    if args.weights_manifest:
        if native_holder:
            details = check('weightManifest', lambda: verify_weight_manifest(native_holder[0], args))
            if details and not details['expectedMatch']:
                report['checks']['weightManifest'].update(status='failed', error='One or more weight shards differ from the trusted manifest.')
        else:
            report['checks']['weightManifest'] = dict(status='not_run', reason='Native selector unavailable; complete shard-set validation not performed.')
    packages = {}
    for name in ('alphafold3', 'jax', 'jaxlib', 'dm-haiku', 'tokamax', 'rdkit'):
        try: packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: packages[name] = None
    report['packageVersions'] = packages
    if args.load_model and all(item['status'] == 'passed' for item in report['checks'].values()):
        def load():
            with ResidentProcess(resident_command(args), args.report.resolve().parent / 'probe-resident',
                                 startup_timeout=args.startup_timeout) as resident:
                ready = resident.ready
                if ready.get('engine') != 'alphafold3' or ready.get('isDemo') or ready.get('modelLoads') != 1:
                    raise ValueError('Resident did not confirm exactly one native AF3 weight load.')
                return ready
        check('modelInitialization', load)
    else:
        reason = 'Not requested; pass --load-model to allocate weights on the GPU.' if not args.load_model else 'Required preflight checks failed.'
        report['checks']['modelInitialization'] = dict(status='not_run', reason=reason)
        report['notExecuted'].append('model weight initialization')
    required = [item for key, item in report['checks'].items() if key != 'modelInitialization' or args.load_model]
    report['status'] = 'passed' if all(item['status'] == 'passed' for item in required) else 'failed'
    report['inferenceValidated'] = False
    return report


def write_report(path, report):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_runtime_arguments(parser)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--load-model', action='store_true')
    parser.add_argument('--nvidia-smi', default='nvidia-smi')
    args = parser.parse_args()
    # Third-party import/CUDA logs must not corrupt the machine-readable stdout.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), 'w', buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    report = collect_probe(args)
    write_report(args.report, report)
    protocol.write(json.dumps(report, allow_nan=False) + '\n')
    protocol.flush()
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
