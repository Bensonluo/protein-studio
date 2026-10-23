#!/usr/bin/env python3
"""JSONL resident AF3 v3.0.4 native API server, or explicit public-data replay.

One ModelRunner, one weight load and one persistent JIT callable per process.
No subprocess is launched for a target or subsequent batch. Stdout is protocol
only; all model Python/C-level stdout is redirected to stderr before imports.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import engine as e
from backend.parsers import parse_confidence, parse_structure
from backend.staged import _digest, _json, _manifest, _validate_af3

MAX_REQUEST = 8 * 1024 * 1024


def prepared_input(raw):
    path = Path(raw)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError('preparedPath must identify a local regular file.')
    if path.suffix == '.json':
        value = json.loads(path.read_text())
        if isinstance(value, dict) and 'cacheKey' in value and 'cacheDirectory' in value:
            root = Path(value['cacheDirectory']).resolve()
            manifest = _manifest(root, value['cacheKey'])
            if not manifest:
                raise ValueError('Prepared descriptor has no valid local completion manifest.')
            primary = root / manifest['inputRelativePath']
            if primary.resolve() != Path(value['inputPath']).resolve():
                raise ValueError('Prepared descriptor inputPath does not match its verified manifest.')
            return primary
    return path


class ReferenceBackend:
    def __init__(self, args):
        self.signature = dict(engine='reference', modelVersion='bundled-public-fixtures', numSamples=1,
                              numRecycles=None, mode='public-reference')
        self.model_loads = 0
        self.is_demo = True
        self.reference_delay = args.reference_delay

    def predict(self, job, source, output):
        if self.reference_delay:
            time.sleep(self.reference_delay)
        example = e.load_example(job['example'])
        filename = e.EXAMPLES[job['example']][0]
        if _digest(source) != _digest(e.FIXTURES / filename):
            raise ValueError('Reference input is not the exact bundled public structure; no new prediction was performed.')
        copied = output / filename
        shutil.copyfile(source, copied)
        kind = example['provenance']['structureType']
        result = parse_structure(copied, 'afdb' if kind == 'predicted' else 'unknown')
        if job['example'] == 'ubiquitin-afdb':
            pae = source.parent / 'P0CG47_pae.json'
            if not pae.is_file() or _digest(pae) != _digest(e.FIXTURES / 'P0CG47_pae.json'):
                raise ValueError('Reference AFDB input must include its actual bundled PAE JSON.')
            destination = output / pae.name
            shutil.copyfile(pae, destination)
            result.update(parse_confidence(destination))
        result.update(structurePath=str(copied), structureFile=str(copied), provenance=example['provenance'],
                      rankedModels=[dict(rank=1, name=example['name'], structurePath=str(copied),
                                         structureFormat=result['structureFormat'], meanPlddt=result.get('meanPlddt'))])
        return result


class AlphaFold3Backend:
    def __init__(self, args):
        if not args.af3_script or not args.model_dir or not args.model_version:
            raise ValueError('AF3 resident requires --af3-script, --model-dir and --model-version.')
        script = Path(args.af3_script).resolve()
        if not script.is_file():
            raise ValueError('Installed AF3 run_alphafold.py does not exist.')
        self.model_dir = Path(args.model_dir).resolve()
        if not self.model_dir.is_dir():
            raise ValueError('Installed AF3 model parameter directory does not exist.')
        sys.path.insert(0, str(script.parent))
        spec = importlib.util.spec_from_file_location('_protein_studio_af3_native', script)
        self.native = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.native
        spec.loader.exec_module(self.native)
        import jax
        from alphafold3.common import folding_input
        self.folding_input = folding_input
        if args.compilation_cache:
            jax.config.update('jax_compilation_cache_dir', args.compilation_cache)
        devices = jax.local_devices(backend='gpu')
        if not 0 <= args.device < len(devices):
            raise ValueError('Requested visible GPU index is unavailable.')
        self.device = devices[args.device]
        capability = float(self.device.compute_capability)
        if capability < 6:
            raise ValueError('AF3 requires GPU compute capability >= 6.0.')
        if 7 <= capability < 8:
            if args.attention != 'xla' or '--xla_disable_hlo_passes=custom-kernel-fusion-rewriter' not in os.environ.get('XLA_FLAGS', ''):
                raise ValueError('This GPU requires xla attention and the official custom-kernel-fusion workaround.')
        self.buckets = tuple(int(value) for value in args.buckets.split(','))
        if not self.buckets or any(value <= 0 for value in self.buckets) or sorted(set(self.buckets)) != list(self.buckets):
            raise ValueError('Compilation buckets must be positive strictly increasing integers.')
        self.template_date = datetime.date.fromisoformat(args.max_template_date)
        self.signature = dict(engine='alphafold3', modelVersion=args.model_version,
                              numSamples=args.num_samples, numRecycles=args.num_recycles,
                              modelDir=str(self.model_dir), attention=args.attention, buckets=list(self.buckets),
                              deviceIndex=args.device, apiVersion='v3.0.4',
                              af3ScriptSha256=_digest(script), maxTemplateDate=args.max_template_date,
                              resolveMsaOverlaps=True, fixStandaloneGlycans=False,
                              returnEmbeddings=False, returnDistogram=False)
        config = self.native.make_model_config(flash_attention_implementation=args.attention,
                                               num_diffusion_samples=args.num_samples, num_recycles=args.num_recycles,
                                               return_embeddings=False, return_distogram=False)
        self.runner = self.native.ModelRunner(config=config, device=self.device, model_dir=self.model_dir)
        # Force exactly one parameter load, then pin the cached tree on this GPU.
        # ModelRunner.model_params is a non-data cached_property in v3.0.4.
        # v3.0.4's loader creates jnp.array objects. Bind their initial placement
        # as well as the later tree transfer to the selected visible GPU.
        with jax.default_device(self.device):
            loaded = self.runner.model_params
        # Native run_inference calls metadata identifier.tobytes(); preserve
        # that small metadata subtree in the loader's native representation.
        params = jax.device_put({key: value for key, value in loaded.items() if key != '__meta__'}, self.device)
        if '__meta__' in loaded:
            params['__meta__'] = loaded['__meta__']
        jax.block_until_ready(params)
        self.runner.__dict__['model_params'] = params
        self.jitted_model = self.runner._model
        self.model_loads = 1
        self.is_demo = False

    def predict(self, job, source, output):
        data = _validate_af3(source, job['chains'])
        data['modelSeeds'] = job['seeds']
        data['name'] = 'protein'
        local = output / 'prepared_input.json'
        _json(local, data)
        fold_input = self.folding_input.Input.from_json(json.dumps(data), json_path=local)
        model_output = output / 'model-output'
        self.native.process_fold_input(
            fold_input=fold_input, data_pipeline_config=None, model_runner=self.runner,
            output_dir=model_output, buckets=self.buckets,
            ref_max_modified_date=self.template_date, conformer_max_iterations=None,
            resolve_msa_overlaps=True, fix_standalone_glycans=False,
            force_output_dir=False, compress_large_output_files=False, save_terms_of_use=True)
        # The controller independently indexes native confidence after upload.
        # Keep this GPU worker free of duplicate PAE sidecar construction.
        result = e._collect_outputs(model_output, 'alphafold3', output, job['chains'], build_pae=False)
        expected = {(seed, sample) for seed in job['seeds'] for sample in range(job['numSamples'])}
        actual = set()
        for candidate in result['rankedModels']:
            match = re.search(r'seed-(\d+)_sample-(\d+)', str(Path(candidate['structurePath']).parent))
            if match:
                actual.add((int(match[1]), int(match[2])))
        if actual != expected:
            raise RuntimeError(f'AF3 resident output is incomplete: expected {len(expected)} seed/sample pairs, received {len(actual)}.')
        result.update(actualSampleCount=len(actual), requestedSampleCount=len(expected),
                      returnedModelCount=len(result['rankedModels']), samplingComplete=True,
                      provenance=dict(mode='prediction', newPrediction=True, structureType='predicted',
                                      sourceLabel='AlphaFold 3 resident native API', sourceUrl=e.SOURCES['alphafold3'],
                                      engineVersion=self.signature['modelVersion'],
                                      seeds=job['seeds'], numSamples=job['numSamples'], numRecycles=job['numRecycles'],
                                      confidenceDefinition=result.get('confidenceDefinition')))
        return result


def normalized_target(target, backend, allowed_root):
    if not isinstance(target, dict) or not isinstance(target.get('targetId'), str) or not target['targetId']:
        raise ValueError('Target requires a nonempty targetId.')
    raw = target.get('job')
    job = e.validate_submission(raw)
    signature = backend.signature
    if job['engine'] != signature['engine'] or job['numSamples'] != signature['numSamples']:
        raise ValueError('Target engine/numSamples is incompatible with this resident model signature.')
    if signature['numRecycles'] is not None and job['numRecycles'] != signature['numRecycles']:
        raise ValueError('Target numRecycles is incompatible with this resident model signature.')
    for key in ('engineVersion', 'modelVersion'):
        if raw.get(key) and raw[key] != signature['modelVersion']:
            raise ValueError('Requested model version is incompatible with this resident.')
    supplied = raw.get('residentSignature')
    if supplied is not None and supplied != signature:
        raise ValueError('Requested residentSignature differs from the active model signature.')
    source = prepared_input(target['preparedPath']).resolve()
    output = Path(target['outputDir'])
    if not output.is_absolute() or output.is_symlink():
        raise ValueError('outputDir must be a local absolute directory without symlink redirection.')
    output = output.resolve()
    if allowed_root and (not source.is_relative_to(allowed_root) or not output.is_relative_to(allowed_root)):
        raise ValueError('Target input/output escaped --allowed-root.')
    if output.exists() and any(output.iterdir()):
        raise ValueError('Target output directory must be fresh or empty; retry with a new attempt directory.')
    output.mkdir(parents=True, exist_ok=True)
    return job, source, output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=['reference', 'alphafold3'], required=True)
    parser.add_argument('--reference-delay', type=float, default=0,
                        help='Public replay demonstration delay in seconds; never a GPU timing estimate.')
    parser.add_argument('--af3-script')
    parser.add_argument('--model-dir')
    parser.add_argument('--model-version')
    parser.add_argument('--num-samples', type=int)
    parser.add_argument('--num-recycles', type=int)
    parser.add_argument('--attention', choices=['triton', 'cudnn', 'xla'], default='triton')
    parser.add_argument('--device', type=int, default=0)
    parser.add_argument('--buckets', default='256,512,768,1024,1280,1536,2048,2560,3072,3584,4096,4608,5120')
    parser.add_argument('--max-template-date', default='2021-09-30')
    parser.add_argument('--compilation-cache')
    parser.add_argument('--allowed-root', type=Path)
    args = parser.parse_args()
    if not 0 <= args.reference_delay <= 30 or (args.reference_delay and args.engine != 'reference'):
        parser.error('--reference-delay must be 0–30 seconds and is only valid for reference replay.')
    if args.num_samples is None:
        args.num_samples = 1 if args.engine == 'reference' else 5
    if args.num_recycles is None:
        args.num_recycles = 3 if args.engine == 'reference' else 10
    if not 1 <= args.num_samples <= 20 or not 1 <= args.num_recycles <= 50:
        parser.error('samples must be 1–20 and recycles 1–50')
    # Keep an independent protocol FD; native libraries may write directly to fd1.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), 'w', buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    def event(kind, **payload):
        protocol.write(json.dumps(dict(event=kind, **payload), allow_nan=False) + '\n')
        protocol.flush()
    started = time.monotonic()
    try:
        backend = ReferenceBackend(args) if args.engine == 'reference' else AlphaFold3Backend(args)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        event('startup_failed', protocolVersion=1, error=str(exc)[:2000])
        return 1
    session_id = uuid.uuid4().hex
    allowed_root = args.allowed_root.resolve() if args.allowed_root else None
    event('ready', protocolVersion=1, pid=os.getpid(), sessionId=session_id,
          engine=args.engine, signature=backend.signature, initializationCount=1,
          modelLoads=backend.model_loads, isDemo=backend.is_demo,
          weightsLoaded=bool(backend.model_loads), compiledShapesWarmed=False,
          startupSeconds=time.monotonic() - started,
          cudaVisibleDevices=os.environ.get('CUDA_VISIBLE_DEVICES'))
    while True:
        line = sys.stdin.buffer.readline(MAX_REQUEST + 1)
        if not line:
            return 0
        request_id = None
        try:
            if len(line) > MAX_REQUEST or not line.endswith(b'\n'):
                raise ValueError('JSONL request exceeds limit or is unterminated.')
            request = json.loads(line)
            request_id = request.get('id')
            if not isinstance(request_id, str) or not request_id or request.get('operation') != 'predict':
                raise ValueError('Request needs id and operation=predict.')
            targets = request.get('targets')
            if not isinstance(targets, list) or not 1 <= len(targets) <= 256:
                raise ValueError('Request needs 1–256 targets.')
            ids = [target.get('targetId') if isinstance(target, dict) else None for target in targets]
            if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
                raise ValueError('Target IDs must be nonempty and unique.')
        except Exception as exc:
            event('request_failed', id=request_id, error=str(exc)[:2000])
            continue
        retired = False
        for target in targets:
            target_id = target['targetId']
            if retired:
                event('target_failed', id=request_id, targetId=target_id,
                      error='Resident retired after a device/resource failure; retry on a restarted worker.', retryable=True)
                continue
            event('target_started', id=request_id, targetId=target_id)
            target_started = time.monotonic()
            try:
                job, source, output = normalized_target(target, backend, allowed_root)
                result = backend.predict(job, source, output)
                result.setdefault('provenance', {}).update(
                    executionMode='resident-native' if not backend.is_demo else 'resident-reference',
                    residentPid=os.getpid(), residentSessionId=session_id, modelLoads=backend.model_loads,
                    modelSignature=backend.signature, isDemo=backend.is_demo,
                    databaseProfile=target['job'].get('databaseProfile', {}).get('id'),
                    profileFingerprint=target['job'].get('databaseProfile', {}).get('profileFingerprint'))
                result['timings'] = dict(residentTargetSeconds=time.monotonic() - target_started,
                                         scope='Native target wall-clock including featurisation/output; excludes resident startup.')
                _json(output / 'resident-execution.json', dict(pid=os.getpid(), sessionId=session_id,
                       signature=backend.signature, initializationCount=1, modelLoads=backend.model_loads,
                       sourcePreparedPath=str(source), requestedJob=target['job']))
                result['artifacts'] = [dict(name=str(path.relative_to(output)), path=str(path)) for path in e._safe_files(output)]
                result_json = output / 'result.json'
                _json(result_json, result)
                event('target_complete', id=request_id, targetId=target_id, resultJson=str(result_json))
                del result  # Do not retain the previous target's PAE while idle.
            except Exception as exc:
                traceback.print_exc(file=sys.stderr)
                message = str(exc)
                exhausted = isinstance(exc, MemoryError) or any(value in message.lower() for value in
                            ('out of memory', 'resource_exhausted'))
                retired = exhausted or any(value in message.lower() for value in
                          ('cuda_error', 'device lost', 'illegal memory access'))
                if exhausted:
                    message = ('Memory/resource capacity exhausted for this target. Change the input, '
                               'model settings, or hardware before a manual retry. ' + message)
                # Restarting the same model on the same device does not solve a
                # deterministic capacity failure. Only the not-yet-run targets
                # below may return directly to the queue after this retirement.
                event('target_failed', id=request_id, targetId=target_id, error=message[:2000],
                      retryable=retired and not exhausted)
                if retired:
                    event('worker_retiring', id=request_id, reason='device-or-resource-failure')
        event('batch_complete', id=request_id, targetCount=len(targets))
        if retired:
            return 2


if __name__ == '__main__':
    raise SystemExit(main())
