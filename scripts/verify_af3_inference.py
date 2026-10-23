#!/usr/bin/env python3
"""Run two prepared protein targets in two batches through one native AF3 resident.

This executes real inference unless --dry-run is set. It checks operational and
output contracts, not accuracy against an experimental ground truth. It never
downloads models, databases, or targets and never substitutes public replay.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.engine import validate_submission
from backend.parsers import parse_structure, parse_confidence
from backend.resident import ResidentProcess
from backend.staged import _validate_af3
from scripts.af3_gpu_probe import (add_runtime_arguments, native_source, resident_command,
                                   sha256, verify_weight_digest, verify_weight_manifest, import_native, write_report)
from scripts.resident_runner import prepared_input


def target_job(path, args, index):
    source = prepared_input(str(Path(path).resolve()))
    data = json.loads(source.read_text())
    if not isinstance(data, dict) or data.get('dialect') != 'alphafold3':
        raise ValueError('Acceptance targets must be prepared AlphaFold 3 JSON, not reference structures.')
    chains = []
    for entity in data.get('sequences', []):
        if set(entity) != {'protein'}:
            raise ValueError('This acceptance path currently supports protein-only targets.')
        protein = entity['protein']
        ids = protein['id'] if isinstance(protein['id'], list) else [protein['id']]
        for chain_id in ids:
            chains.append(dict(id=chain_id, sequence=protein['sequence']))
    _validate_af3(source, chains)
    job = validate_submission(dict(name='AF3 acceptance target ' + str(index), engine='alphafold3', chains=chains,
        seeds=data.get('modelSeeds'), numSamples=args.num_samples, numRecycles=args.num_recycles))
    job['modelVersion'] = args.model_version
    evidence = dict(preparedPath=str(source), preparedSha256=sha256(source), chainCount=len(chains),
        residueCount=sum(len(chain['sequence']) for chain in chains),
        chains=[dict(id=chain['id'], residues=len(chain['sequence']),
            sequenceSha256=hashlib.sha256(chain['sequence'].encode()).hexdigest()) for chain in chains],
        seeds=job['seeds'], numSamples=job['numSamples'], numRecycles=job['numRecycles'])
    return source, job, evidence


def validate_native_result(path, job, ready, output):
    path, output = Path(path).resolve(), Path(output).resolve()
    if not path.is_relative_to(output) or path.stat().st_size > 256 * 1024 * 1024:
        raise ValueError('Result path is outside its output or exceeds the acceptance JSON bound; use two small acceptance targets.')
    result = json.loads(path.read_text())
    provenance = result.get('provenance', {})
    if (provenance.get('mode') != 'prediction' or provenance.get('newPrediction') is not True
        or provenance.get('executionMode') != 'resident-native' or provenance.get('isDemo') is not False
        or provenance.get('modelLoads') != 1 or provenance.get('residentPid') != ready['pid']
        or provenance.get('modelSignature') != ready['signature']):
        raise ValueError('Result does not prove the requested native resident prediction contract.')
    if not result.get('samplingComplete') or result.get('actualSampleCount') != len(job['seeds']) * job['numSamples']:
        raise ValueError('The requested seed/sample set is incomplete.')
    structure = Path(result['structurePath']).resolve()
    if not structure.is_relative_to(output) or structure.suffix not in ('.cif', '.mmcif'):
        raise ValueError('Native AF3 acceptance requires an actual mmCIF structure in this target output.')
    parsed = parse_structure(structure, 'alphafold3')
    if sorted(chain['sequence'] for chain in parsed['chains']) != sorted(chain['sequence'] for chain in job['chains']):
        raise ValueError('Returned protein chains do not match the prepared target.')
    confidence = parsed.get('plddt')
    if not confidence or any(not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100 for value in confidence):
        raise ValueError('Native structure lacks valid pLDDT values.')
    prefix = structure.name[:-len('model.cif')] if structure.name.endswith('model.cif') else None
    if prefix is None:
        raise ValueError('Unexpected native AF3 structure filename.')
    full = structure.with_name(prefix + 'confidences.json')
    summary = structure.with_name(prefix + 'summary_confidences.json')
    metrics = parse_confidence(summary)
    arrays = parse_confidence(full)
    if arrays.get('pae') is None or metrics.get('ptm') is None:
        raise ValueError('Native AF3 result must include its real full-confidence PAE and summary pTM.')
    text = structure.read_text()
    if 'AlphaFold' not in text or '_software.' not in text:
        raise ValueError('AF3 mmCIF software metadata is missing.')
    return dict(resultJson=str(path), structurePath=str(structure), structureSha256=sha256(structure),
        structureBytes=structure.stat().st_size, confidencePath=str(full), confidenceSha256=sha256(full),
        summaryPath=str(summary), meanPlddt=parsed.get('meanPlddt'), ptm=metrics['ptm'],
        iptm=metrics.get('iptm'), paeRows=len(arrays['pae']), actualSampleCount=result['actualSampleCount'],
        residentPid=provenance['residentPid'], residentSessionId=provenance.get('residentSessionId'),
        modelLoads=provenance['modelLoads'], executionMode=provenance['executionMode'])


def run_acceptance(args):
    report = dict(schemaVersion=1, kind='af3-two-batch-native-acceptance', status='failed',
        checkedAt=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        scientificAccuracy='not_evaluated_against_experimental_ground_truth', inferenceExecuted=False,
        declaredModelVersion=args.model_version, targets=[], batches=[])
    try:
        if len(args.prepared_target) != 2:
            raise ValueError('Provide exactly two --prepared-target arguments for two separate batches.')
        targets = []
        for index, path in enumerate(args.prepared_target, 1):
            source, job, evidence = target_job(path, args, index)
            report['targets'].append(evidence)
            targets.append((source, job))
        if args.dry_run:
            report.update(status='dry_run', notExecuted=['native import', 'GPU initialization', 'weight loading',
                'inference', 'two-batch resident validation'], plannedCommand=resident_command(args))
            return report
        report['nativeSource'] = native_source(args)
        native = import_native(args.af3_script) if (args.expected_weights_sha256 or args.weights_manifest) else None
        if args.expected_weights_sha256:
            report['weightDigest'] = verify_weight_digest(args)
            if not report['weightDigest']['expectedMatch']:
                raise ValueError('Weight bundle does not match the expected SHA-256.')
            selected, _ = native.params.select_model_files(model_dir=args.model_dir.resolve())
            if {Path(path).name for path in selected} != {args.weights_file}:
                raise ValueError('Single-bundle verification does not cover the selected weight set; use --weights-manifest.')
        if args.weights_manifest:
            report['weightManifest'] = verify_weight_manifest(native, args)
            if not report['weightManifest']['expectedMatch']:
                raise ValueError('Selected weight shards do not match the expected manifest.')
        root = args.output_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        if any((root / ('target-' + str(index))).exists() for index in (1, 2)):
            raise ValueError('Acceptance output already contains target directories; use a fresh output directory.')
        with ResidentProcess(resident_command(args), root / 'resident', startup_timeout=args.startup_timeout,
                             request_timeout=args.target_timeout) as resident:
            ready = resident.ready
            report['ready'] = ready
            if (ready.get('engine') != 'alphafold3' or ready.get('isDemo') or ready.get('modelLoads') != 1
                or ready.get('initializationCount') != 1 or not ready.get('weightsLoaded')
                or ready.get('signature', {}).get('af3ScriptSha256') != report['nativeSource']['sha256']):
                raise ValueError('Acceptance requires the actual AF3 resident with one verified initialization.')
            for index, (source, job) in enumerate(targets, 1):
                output = root / ('target-' + str(index))
                started = time.monotonic()
                report['inferenceExecuted'] = True
                result = resident.predict_batch([dict(targetId='acceptance-' + str(index), job=job,
                    preparedPath=str(source), outputDir=str(output))])[0]
                if result['status'] != 'completed':
                    raise RuntimeError(result.get('error', 'Native target failed.'))
                checked = validate_native_result(result['resultJson'], job, ready, output)
                checked.update(batch=index, seconds=time.monotonic()-started,
                               timingScope='native per-target wall time, includes featurisation/compilation/output; excludes resident startup')
                report['batches'].append(checked)
                if resident.pid != ready['pid'] or resident.generation != 1:
                    raise ValueError('Resident process was replaced between batches.')
            if len({batch['residentSessionId'] for batch in report['batches']}) != 1:
                raise ValueError('Two results do not belong to the same resident session.')
        report.update(status='passed', samePidAcrossBatches=True, modelInitializationCount=1,
                      modelLoadsPerProcess=1, notExecuted=['database search', 'experimental accuracy benchmark'])
    except Exception as exc:
        report.update(error=str(exc)[:4000], errorType=type(exc).__name__)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_runtime_arguments(parser)
    parser.add_argument('--prepared-target', type=Path, action='append', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--target-timeout', type=float, default=7200)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), 'w', buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    report = run_acceptance(args)
    write_report(args.report, report)
    protocol.write(json.dumps(report, allow_nan=False) + '\n')
    protocol.flush()
    return 0 if report['status'] in ('passed', 'dry_run') else 1


if __name__ == '__main__':
    raise SystemExit(main())
