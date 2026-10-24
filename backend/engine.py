"""Protein prediction adapters and an explicitly identified public-data replay.

Administrators configure argv JSON in PROTEIN_STUDIO_<ENGINE>_COMMAND. Templates
are substituted as individual argv elements, never as shell source. No executable,
database path or environment setting is accepted from an HTTP submission.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

if __package__:
    from .parsers import FileTooLargeError, parse_confidence, parse_structure
    from .result_artifacts import stream_confidence, compact_pae, attach_interface_contacts, PAELimitError
else:
    from parsers import FileTooLargeError, parse_confidence, parse_structure
    from result_artifacts import stream_confidence, compact_pae, attach_interface_contacts, PAELimitError

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures'
ENGINE_IDS = ('reference', 'alphafold2', 'alphafold3', 'colabfold')
SOURCES = {
    'alphafold2': 'https://github.com/google-deepmind/alphafold',
    'alphafold3': 'https://github.com/google-deepmind/alphafold3',
    'colabfold': 'https://github.com/sokrypton/ColabFold',
}
EXAMPLES = {
    'ubiquitin': ('1UBQ.pdb', 'Ubiquitin · 1UBQ', 'experimental', 'https://www.rcsb.org/structure/1UBQ'),
    'crambin': ('1CRN.pdb', 'Crambin · 1CRN', 'experimental', 'https://www.rcsb.org/structure/1CRN'),
    'ubiquitin-afdb': ('P0CG47.pdb', 'Polyubiquitin-B · P0CG47', 'predicted', 'https://alphafold.ebi.ac.uk/entry/P0CG47'),
}
_GPU_CONDITION = threading.Condition()
_GPU_CONFIGURATION = None
_GPU_LEASES = set()


class PredictionCancelled(RuntimeError):
    pass


def _check_cancel(cancelled):
    if cancelled():
        raise PredictionCancelled('Prediction cancelled.')


def _env_name(engine, suffix):
    return f'PROTEIN_STUDIO_{engine.upper()}_{suffix}'


def _gpu_groups():
    raw = os.environ.get('PROTEIN_STUDIO_GPU_GROUPS_JSON')
    devices = os.environ.get('PROTEIN_STUDIO_GPU_DEVICES')
    if raw:
        try:
            groups = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError('PROTEIN_STUDIO_GPU_GROUPS_JSON must be a JSON list of device groups.') from exc
    elif devices:
        groups = [[device.strip()] for device in devices.split(',')]
    else:
        return None
    if not isinstance(groups, list) or not groups or len(groups) > 256:
        raise ValueError('GPU groups must contain 1–256 non-empty groups.')
    normalized, seen = [], set()
    for group in groups:
        if not isinstance(group, list) or not group:
            raise ValueError('Each GPU group must be a non-empty list of device IDs.')
        current = []
        for device in group:
            if isinstance(device, bool) or not isinstance(device, (int, str)):
                raise ValueError('GPU device IDs must be integers or strings.')
            device = str(device)
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_\-/]*', device) or device in seen:
                raise ValueError('GPU IDs must be valid CUDA device identifiers and may not overlap between groups.')
            seen.add(device)
            current.append(device)
        normalized.append(tuple(current))
    return tuple(normalized)


@contextmanager
def _gpu_lease(emit, cancelled):
    """Thread admission plus cooperative node-local cross-process exclusion."""
    global _GPU_CONFIGURATION
    groups = _gpu_groups()
    if groups is None:
        yield None
        return
    slot = None
    announced = False
    with _GPU_CONDITION:
        if _GPU_CONFIGURATION != groups:
            if _GPU_LEASES:
                raise RuntimeError('GPU group configuration changed while jobs hold devices. Restart workers to reconfigure.')
            _GPU_CONFIGURATION = groups
        while slot is None:
            _check_cancel(cancelled)
            slot = next((i for i in range(len(groups)) if i not in _GPU_LEASES), None)
            if slot is not None:
                _GPU_LEASES.add(slot)
            else:
                if not announced:
                    emit('waiting', 'Waiting for an available administrator-configured GPU group.', 20)
                    announced = True
                _GPU_CONDITION.wait(timeout=0.1)
    try:
        from .gpu_access import gpu_group_lease
        with gpu_group_lease(list(groups[slot]), cancelled) as devices:
            yield devices
    finally:
        with _GPU_CONDITION:
            _GPU_LEASES.discard(slot)
            _GPU_CONDITION.notify_all()


def _configured_command(engine):
    value = os.environ.get(_env_name(engine, 'COMMAND'))
    if value:
        try:
            command = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f'{_env_name(engine, "COMMAND")} must contain a JSON argv array.') from exc
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x and '\x00' not in x for x in command):
            raise ValueError('Configured command must be a non-empty array of non-empty strings.')
        return command
    # Single-sequence mode avoids implicitly sending a submitted sequence to a
    # public MSA service. An administrator can explicitly configure another path.
    executable = shutil.which('colabfold_batch') if engine == 'colabfold' else None
    if executable:
        return [executable, '{fasta}', '{output_dir}', '--msa-mode', 'single_sequence',
                '--num-models', '{num_samples}', '--num-seeds', '1',
                '--random-seed', '{seed}', '--num-recycle', '{num_recycles}']
    return None


def list_engines() -> list:
    descriptions = {
        'reference': 'Replay bundled public structures; never predict a new sequence.',
        'alphafold2': 'Run an administrator-configured AlphaFold 2 installation with its own weights and databases.',
        'alphafold3': 'Run an administrator-configured AlphaFold 3 installation; access and weight terms are configured separately.',
        'colabfold': 'Run ColabFold locally; auto-detected CLI uses single-sequence mode. Full MSA requires administrator configuration.',
    }
    names = {'reference': 'Public structure replay', 'alphafold2': 'AlphaFold 2',
             'alphafold3': 'AlphaFold 3', 'colabfold': 'ColabFold'}
    result = []
    for engine in ENGINE_IDS:
        reason = None
        if engine == 'reference':
            available = all((FIXTURES / item[0]).is_file() for item in EXAMPLES.values())
        else:
            try:
                command = _configured_command(engine)
                available = bool(command and shutil.which(command[0]))
                if not available:
                    reason = f'Configure {_env_name(engine, "COMMAND")} with an installed executable.'
            except ValueError as exc:
                available, reason = False, str(exc)
        result.append(dict(id=engine, name=names[engine], available=available,
                           description=descriptions[engine], unavailableReason=reason,
                           version=os.environ.get(_env_name(engine, 'VERSION')),
                           defaultNumSamples=1 if engine in ('reference', 'alphafold2') else 5,
                           maxNumSamples=5 if engine == 'colabfold' else 20,
                           sampleMeaning={'reference': 'Historical artifact; sampling settings do not rerun a model.',
                                          'alphafold2': 'Multimer predictions per model per seed; monomer requires 1. Multiple model weights may produce more outputs.',
                                          'alphafold3': 'Diffusion samples per seed.',
                                          'colabfold': 'Number of model weights per seed (1–5), not diffusion samples.'}[engine],
                           readiness='reference' if engine == 'reference' else 'executable-only; weights, databases and GPU checked at execution'))
    return result


def load_example(name: str = 'ubiquitin-afdb') -> dict:
    aliases = {'1ubq': 'ubiquitin', '1crn': 'crambin', 'p0cg47': 'ubiquitin-afdb'}
    name = aliases.get(str(name).lower(), str(name).lower())
    if name not in EXAMPLES:
        raise ValueError('Unknown example. Choose ubiquitin-afdb, ubiquitin or crambin.')
    filename, title, kind, url = EXAMPLES[name]
    parsed = parse_structure(FIXTURES / filename, source='afdb' if kind == 'predicted' else 'unknown')
    provenance = dict(mode='reference', structureType=kind, sourceUrl=url,
                      sourceLabel='AlphaFold DB · P0CG47' if name == 'ubiquitin-afdb' else f'RCSB PDB · {Path(filename).stem}',
                      retrievedAt='2026-10-23', newPrediction=False)
    if name == 'ubiquitin-afdb':
        metadata = json.loads((FIXTURES / 'P0CG47_metadata.json').read_text())[0]
        provenance.update(databaseVersion=metadata.get('latestVersion'), entryId=metadata.get('entryId'),
                          structureUrl=metadata.get('pdbUrl'), paeUrl=metadata.get('paeDocUrl'),
                          modelDescription='AlphaFold DB historical prediction; database release is not the executable version.')
    return dict(id=name, example=name, name=title, engine='reference', chains=parsed['chains'],
                sequence=''.join(chain['sequence'] for chain in parsed['chains']),
                sequenceLength=parsed['sequenceLength'], seeds=[1], numSamples=1,
                description='Replay of an existing public prediction, including its published confidence.' if kind == 'predicted'
                else 'Replay of an experimental structure. Experimental B factors are not pLDDT.',
                provenance=provenance)


def _integer(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f'{label} must be an integer from {low} to {high}.')
    return value


def _parse_fasta(text):
    chains, seq = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith('>'):
            if seq:
                chains.append(''.join(seq))
                seq = []
        else:
            seq.append(line)
    if seq:
        chains.append(''.join(seq))
    return [dict(id=chr(65 + i), sequence=s) for i, s in enumerate(chains)]


def validate_submission(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError('Submission must be a JSON object.')
    engine = payload.get('engine', 'reference')
    if engine not in ENGINE_IDS:
        raise ValueError('Unknown prediction engine.')
    if engine == 'alphafold3' and any(field in payload for field in ('sequences', 'bondedAtomPairs', 'userCCD', 'userCCDPath')):
        raise ValueError('This submission API accepts standard protein id/sequence inputs; native AF3 chemical/custom input fields need a separate adapter.')
    example_name = payload.get('example', payload.get('exampleId', 'ubiquitin-afdb'))
    example = load_example(example_name) if engine == 'reference' else None
    raw_chains = payload.get('chains')
    if raw_chains is None and payload.get('sequence'):
        if not isinstance(payload['sequence'], str):
            raise ValueError('Sequence must be text.')
        raw_chains = _parse_fasta(payload['sequence'])
    if raw_chains is None and example:
        raw_chains = example['chains']
    if not isinstance(raw_chains, list) or not 1 <= len(raw_chains) <= 26:
        raise ValueError('Supply between 1 and 26 protein chains.')
    chains, seen = [], set()
    for i, chain in enumerate(raw_chains):
        if not isinstance(chain, dict):
            raise ValueError('Each chain must contain id and sequence.')
        if engine == 'alphafold3' and set(chain) - {'id', 'sequence'}:
            raise ValueError('AF3 automatic preparation accepts only chain id and sequence; custom MSA, templates and modifications are not supported by this input path.')
        chain_id = chain.get('id', chr(65 + i))
        if not isinstance(chain_id, str) or not re.fullmatch('[A-Z]{1,4}', chain_id) or chain_id in seen:
            raise ValueError('Chain IDs must be unique uppercase letters (1–4 characters).')
        sequence = chain.get('sequence')
        if not isinstance(sequence, str):
            raise ValueError('Each protein sequence must be text.')
        sequence = re.sub(r'\s+', '', sequence).upper()
        if not sequence or not re.fullmatch('[ACDEFGHIKLMNPQRSTVWY]+', sequence):
            raise ValueError('Use standard amino-acid letters only; ambiguous residues and non-protein molecules are not supported by this UI.')
        seen.add(chain_id)
        chains.append(dict(id=chain_id, sequence=sequence))
    length = sum(len(x['sequence']) for x in chains)
    max_length = int(os.environ.get('PROTEIN_STUDIO_MAX_RESIDUES', '5000'))
    if length > max_length:
        raise ValueError(f'Submission exceeds the configured {max_length}-residue limit.')
    if example and chains != example['chains']:
        raise ValueError('Reference mode only replays the exact example chains and sequence. Select a configured prediction engine for another input.')
    seeds = payload.get('seeds', [1])
    if not isinstance(seeds, list) or not 1 <= len(seeds) <= 20:
        raise ValueError('seeds must be a list of 1–20 integers.')
    seeds = [_integer(s, 'seed', 0, 2**31 - 1) for s in seeds]
    if len(set(seeds)) != len(seeds):
        raise ValueError('Seeds must be unique.')
    samples = _integer(payload.get('numSamples', 1 if engine in ('reference', 'alphafold2') else 5),
                       'numSamples', 1, 5 if engine == 'colabfold' else 20)
    recycles = _integer(payload.get('numRecycles', 10 if engine == 'alphafold3' else 3), 'numRecycles', 1, 50)
    if engine == 'alphafold2' and len(chains) == 1 and samples != 1:
        raise ValueError('AlphaFold 2 monomer uses one prediction per model per seed. Set numSamples=1; add seeds for additional runs.')
    if engine == 'reference' and (samples != 1 or seeds != [1]):
        raise ValueError('Reference replay does not sample a model; use seeds=[1] and numSamples=1.')
    name = payload.get('name', example['name'] if example else 'Protein prediction')
    if not isinstance(name, str) or not name.strip() or len(name) > 120 or any(ord(c) < 32 for c in name):
        raise ValueError('Name must be 1–120 printable characters.')
    priority = payload.get('priority', 'normal')
    if priority not in ('normal', 'high'):
        raise ValueError('priority must be normal or high.')
    norm = dict(name=name.strip(), engine=engine, chains=chains, seeds=seeds, numSamples=samples,
                numRecycles=recycles, sequenceLength=length, priority=priority,
                modelPreset='multimer' if len(chains) > 1 else 'monomer_ptm')
    if example:
        norm['example'] = example['id']
    return norm


def _write_inputs(job, directory):
    directory.mkdir(parents=True, exist_ok=True)
    name = 'protein'
    fasta = directory / f'{name}.fasta'
    chains = job['chains']
    if job['engine'] == 'colabfold':
        fasta.write_text(f'>{name}\n' + ':'.join(c['sequence'] for c in chains) + '\n')
    else:
        fasta.write_text(''.join(f'>{c["id"]}\n{c["sequence"]}\n' for c in chains))
    af3 = directory / 'input.json'
    # Version 1 suffices for this protein-only inline schema, including current
    # releases; no description/path/CCD fields from newer dialects are emitted.
    af3.write_text(json.dumps(dict(name=name, modelSeeds=job['seeds'],
                                  sequences=[{'protein': c} for c in chains],
                                  dialect='alphafold3', version=1), indent=2))
    job_json = directory / 'submission.json'
    job_json.write_text(json.dumps(job, indent=2))
    return fasta, af3, job_json


def _format_command(command, values):
    # Only exact recognized placeholder spellings are replaced. JSON payloads
    # and literal braces in administrator-supplied arguments remain untouched.
    return [re.sub(r'\{([a-z_]+)\}', lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), arg)
            for arg in command]


def _stop(process):
    if process.poll() is not None:
        return
    try:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=3)
    except ProcessLookupError:
        pass


def _close_stage_group(process):
    """Reap stage descendants even if their supervising wrapper already exited."""
    if os.name != 'posix':
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + .15
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(.01)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _execute(command, directory, log_path, cancelled, timeout, gpu_group=None):
    start = time.monotonic()
    with log_path.open('wb') as log:
        environment = os.environ.copy()
        environment['PROTEIN_STUDIO_MANAGED_PROCESS_GROUP'] = '1'
        environment['PROTEIN_STUDIO_GPU_LOCK_FDS_JSON'] = json.dumps(list(getattr(gpu_group, 'lock_fds', ())))
        if gpu_group is not None:
            environment['CUDA_VISIBLE_DEVICES'] = ','.join(gpu_group)
        process = subprocess.Popen(command, cwd=directory, stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, shell=False, env=environment,
                                   pass_fds=getattr(gpu_group, 'lock_fds', ()),
                                   start_new_session=os.name == 'posix')
        try:
            while process.poll() is None:
                _check_cancel(cancelled)
                if time.monotonic() - start > timeout:
                    raise TimeoutError(f'Prediction exceeded the configured timeout ({timeout:g} seconds). See engine log.')
                # Poll only process completion/cancellation; never pretend this
                # interval measures GPU progress.
                time.sleep(0.1)
            _check_cancel(cancelled)
            if process.returncode != 0:
                raise RuntimeError(f'Prediction executable exited with status {process.returncode}. See {log_path.name}.')
        except BaseException:
            _stop(process)
            raise
        finally:
            # A nested search stays in this group under the managed-stage
            # contract; do not unlock a device while an orphan search runs.
            _close_stage_group(process)
    return time.monotonic() - start


def _safe_files(directory):
    root = directory.resolve()
    files = []
    for path in directory.rglob('*'):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            path.resolve().relative_to(root)
        except ValueError:
            continue
        files.append(path)
        if len(files) > 10000:
            raise RuntimeError('Output contains more than 10,000 files; narrow the configured output directory.')
    return files


def _metrics_for(path, engine, *, build_pae=False, artifact_root=None, cancelled=lambda: False):
    candidates = []
    stem = path.stem
    if engine == 'alphafold3':
        prefix = stem[:-6] if stem.endswith('_model') else stem
        candidates = [path.with_name(prefix + '_confidences.json'), path.with_name(prefix + '_summary_confidences.json')]
    elif engine == 'colabfold':
        score_stem = re.sub(r'_(unrelaxed|relaxed)_', '_scores_', stem)
        candidates = [path.with_name(score_stem + '.json')]
    else:
        candidates = [path.with_suffix('.json')]
    metrics = {}
    def add_confidence(candidate, *, want_pae=build_pae):
        try:
            parsed = stream_confidence(candidate, artifact_root or candidate.parent,
                build_pae=want_pae, axis_kind='token' if engine == 'alphafold3' else 'residue', cancelled=cancelled)
        except PAELimitError as exc:
            metrics.setdefault('warnings', []).append(f'{candidate.name}: {exc} Original confidence JSON remains downloadable; valid structure and summary metrics are retained.')
            if want_pae:
                root = Path(artifact_root or candidate.parent).resolve()
                metrics['paeArtifact'] = dict(schemaVersion=1, status='unavailable', reason=str(exc),
                    sourcePath=str(candidate.resolve()), sourceRelativePath=candidate.resolve().relative_to(root).as_posix(),
                    unit='angstrom', axisKind='token' if engine == 'alphafold3' else 'residue', axisOrder='as-stored')
            return
        except (ValueError, OverflowError) as exc:
            metrics.setdefault('warnings', []).append(
                f'{candidate.name}: confidence could not be safely indexed ({exc}). '
                'The original file remains downloadable; structure pLDDT and available summary metrics are retained.')
            return
        warnings = parsed.pop('warnings', [])
        metrics.update(parsed)
        if warnings:
            metrics.setdefault('warnings', []).extend(warnings)

    if engine == 'alphafold3':
        full, summary = candidates
        # A bounded, valid summary supplies the common ranking metrics without
        # scanning every sample's N-squared full-confidence file. A partial or
        # invalid summary must not silently suppress the scalar fallback.
        if summary.is_file() and not summary.is_symlink():
            if summary.stat().st_size <= 1024 * 1024:
                add_confidence(summary, want_pae=False)
            else:
                metrics.setdefault('warnings', []).append(
                    f'{summary.name}: exceeds the 1 MiB summary limit; using full-confidence scalar fallback.')
        usable_ranking = metrics.get('rankingScore') is not None or (
            metrics.get('iptm') is not None and metrics.get('ptm') is not None)
        if build_pae or not usable_ranking:
            summary_metrics, metrics = metrics, {}
            if full.is_file() and not full.is_symlink():
                add_confidence(full)
            # Preserve the prior full-then-summary precedence: reported summary
            # fields are authoritative, while the full file fills absent ones.
            warnings = metrics.pop('warnings', []) + summary_metrics.pop('warnings', [])
            metrics.update(summary_metrics)
            if warnings:
                metrics['warnings'] = warnings
    else:
        for candidate in candidates:
            if candidate.is_file() and not candidate.is_symlink():
                add_confidence(candidate)
    if engine == 'alphafold2' and re.fullmatch(r'ranked_\d+', stem):
        debug = path.parent / 'ranking_debug.json'
        if debug.is_file() and not debug.is_symlink() and debug.stat().st_size <= 1024 * 1024:
            data = json.loads(debug.read_text())
            index = int(stem.split('_')[1])
            order = data.get('order', [])
            if index < len(order):
                scores = data.get('iptm+ptm', data.get('plddts', {}))
                score = scores.get(order[index])
                if isinstance(score, (float, int)):
                    metrics['rankingScore'] = float(score)
                # Optional safe exports, not native unsafe result_*.pkl files.
                exported = path.parent / f'result_{order[index]}.json'
                if exported.is_file() and not exported.is_symlink():
                    add_confidence(exported)
    return metrics


def _collect_outputs(directory, engine, work_dir, expected_chains=None, cancelled=lambda: False, *, build_pae=True):
    directory, work_dir = Path(directory).resolve(), Path(work_dir).resolve()
    files = _safe_files(directory)
    structures = [p for p in files if p.suffix.lower() in ('.pdb', '.cif', '.mmcif')]
    if engine == 'alphafold3':
        models = [p for p in structures if p.stem.endswith('_model')]
        samples = [p for p in models if re.search(r'seed-\d+_sample-\d+', str(p.parent))]
        structures = samples or models
    elif engine == 'alphafold2':
        structures = [p for p in structures if re.fullmatch(r'ranked_\d+', p.stem)] or [p for p in structures if p.name.startswith('unrelaxed_')]
    else:
        ranked = [p for p in structures if re.search(r'_rank_\d+', p.stem)]
        structures = ranked or structures
        # A relaxed and unrelaxed file can represent the same prediction. Prefer
        # relaxed when present rather than counting it as another sample.
        by_model = {}
        for path in sorted(structures):
            key = re.sub(r'_(unrelaxed|relaxed)_', '_structure_', str(path.with_suffix('')))
            old = by_model.get(key)
            if old is None or '_relaxed_' in path.name:
                by_model[key] = path
        structures = list(by_model.values())
    if not structures:
        raise RuntimeError('Executable finished without a supported prediction structure in its output directory.')
    models = []
    for path in sorted(structures):
        _check_cancel(cancelled)
        parsed = parse_structure(path, source=engine)
        if expected_chains is not None:
            if sorted(c['sequence'] for c in expected_chains) != sorted(c['sequence'] for c in parsed['chains']):
                raise RuntimeError(f'Prediction output protein chains do not match submitted sequences: {path.name}. Inspect the raw output; it was not accepted as this prediction.')
        metrics = _metrics_for(path, engine, artifact_root=work_dir, cancelled=cancelled)
        vector = metrics.pop('plddt', None)
        if vector is not None and len(vector) == len(parsed['residues']):
            parsed['plddt'] = vector
            parsed['meanPlddt'] = sum(vector) / len(vector)
            for residue, value in zip(parsed['residues'], vector):
                residue['plddt'] = value
            parsed['confidenceDefinition'] = 'Per-residue pLDDT from prediction confidence JSON'
        parsed.update(metrics)
        # Keep O(models) scalar ranking records, not every model's coordinate
        # and confidence arrays. The selected structure is parsed again below.
        lightweight = {key: parsed[key] for key in ('structureFormat', 'meanPlddt', 'ptm', 'iptm', 'rankingScore', 'warnings') if key in parsed}
        models.append((0, path, lightweight))
    # Never compare an AF3 score near 1 with a fallback pLDDT near 90. Select a
    # common metric across every candidate, then apply that metric consistently.
    missing_ranking = None
    if any(data.get('rankingScore') is not None for _, _, data in models):
        ranking_metric = 'reported ranking score'
        score_for = lambda data: data.get('rankingScore', float('-inf'))
        if not all(data.get('rankingScore') is not None for _, _, data in models):
            missing_ranking = 'Some models have no reported ranking score; they are placed after models with that score, without mixing pLDDT into the ranking.'
    elif all(data.get('iptm') is not None and data.get('ptm') is not None for _, _, data in models):
        ranking_metric = '0.8 ipTM + 0.2 pTM'
        score_for = lambda data: 0.8 * data['iptm'] + 0.2 * data['ptm']
    else:
        ranking_metric = 'mean residue pLDDT (fallback)'
        score_for = lambda data: data.get('meanPlddt') if data.get('meanPlddt') is not None else -1
    models = [(score_for(data), path, data) for _, path, data in models]
    models.sort(key=lambda item: item[0], reverse=True)
    _, top_path, top_metrics = models[0]
    result = parse_structure(top_path, source=engine)
    result.update(top_metrics)
    metrics = _metrics_for(top_path, engine, build_pae=build_pae, artifact_root=work_dir, cancelled=cancelled)
    vector = metrics.pop('plddt', None)
    if vector is not None and len(vector) == len(result['residues']):
        result['plddt'] = vector
        result['meanPlddt'] = sum(vector) / len(vector)
        for residue, value in zip(result['residues'], vector):
            residue['plddt'] = value
        result['confidenceDefinition'] = 'Per-residue pLDDT from prediction confidence JSON'
    result.update(metrics)
    # Interface contacts need residue-aligned PAE axes; the join is skipped
    # (never guessed) for token-kind axes where a residue can span tokens.
    attach_interface_contacts(result, metrics.get('paeArtifact'), work_dir)
    if missing_ranking:
        result.setdefault('warnings', []).append(missing_ranking)
    result.update(structurePath=str(top_path.resolve()), structureFile=str(top_path.resolve()), rankingMetric=ranking_metric,
                  rankedModels=[dict(rank=i, name=p.stem, structurePath=str(p.resolve()),
                                     structureFile=str(p.resolve()), structureFormat=data['structureFormat'],
                                     meanPlddt=data.get('meanPlddt'), ptm=data.get('ptm'), iptm=data.get('iptm'),
                                     rankingScore=data.get('rankingScore')) for i, (_, p, data) in enumerate(models, 1)])
    # Remote resident workers upload original outputs. The controller indexes
    # the selected PAE once; workers must not build a binary only to discard it.
    return compact_pae(result, work_dir, cancelled=cancelled) if build_pae else result


def run_prediction(job: dict, work_dir: Path, emit, cancelled) -> dict:
    start = time.monotonic()
    emit('validation', 'Validating protein input and engine configuration.', 5)
    job = validate_submission(job)
    _check_cancel(cancelled)
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    emit('preparation', 'Writing normalized input and provenance.', 15)
    fasta, af3_json, job_json = _write_inputs(job, work_dir / 'input')
    if job['engine'] == 'reference':
        example = load_example(job['example'])
        filename = EXAMPLES[job['example']][0]
        destination = work_dir / filename
        emit('inference', 'Replaying a published structure; no new folding or MSA computation is performed.', 45)
        shutil.copyfile(FIXTURES / filename, destination)
        _check_cancel(cancelled)
        kind = example['provenance']['structureType']
        result = parse_structure(destination, 'afdb' if kind == 'predicted' else 'unknown')
        if job['example'] == 'ubiquitin-afdb':
            pae = work_dir / 'P0CG47_pae.json'
            shutil.copyfile(FIXTURES / pae.name, pae)
            result.update(parse_confidence(pae))
        emit('analysis', 'Reading actual coordinates and available published confidence.', 85)
        result.update(structurePath=str(destination), structureFile=str(destination),
                      rankedModels=[dict(rank=1, name=example['name'], structurePath=str(destination),
                                         structureFormat=result['structureFormat'], meanPlddt=result['meanPlddt'])],
                      provenance=example['provenance'], timings={'replaySeconds': time.monotonic() - start})
    else:
        command = _configured_command(job['engine'])
        if command is None or shutil.which(command[0]) is None:
            raise RuntimeError(f'{job["engine"]} is not configured with an installed executable. No prediction was run.')
        output_root = work_dir / 'output'
        output_root.mkdir(exist_ok=True)
        timeout = float(os.environ.get('PROTEIN_STUDIO_TIMEOUT_SECONDS', '7200'))
        if not 0 < timeout <= 7 * 86400:
            raise ValueError('PROTEIN_STUDIO_TIMEOUT_SECONDS must be positive and at most 7 days.')
        seeds = [job['seeds'][0]] if job['engine'] == 'alphafold3' else job['seeds']
        seconds = 0.0
        manifests = []
        queue_start = time.monotonic()
        with _gpu_lease(emit, cancelled) as gpu_group:
            queue_seconds = time.monotonic() - queue_start
            for i, seed in enumerate(seeds):
                _check_cancel(cancelled)
                output = output_root if job['engine'] == 'alphafold3' else output_root / f'seed-{seed}'
                output.mkdir(exist_ok=True)
                values = dict(fasta=fasta, input_json=af3_json, output_dir=output, seed=seed,
                              num_samples=job['numSamples'], num_recycles=job['numRecycles'], name='protein',
                              model_preset=job['modelPreset'], job_json=job_json)
                argv = _format_command(command, values)
                group_label = f' GPU group {",".join(gpu_group)}.' if gpu_group is not None else ' Device allocation inherited from the runner.'
                emit('inference', f'Running {job["engine"]}; external process progress is not estimated.' + group_label, 25 + int(45 * i / len(seeds)))
                manifests.append(dict(seed=seed, argv=argv))
                (work_dir / 'execution.json').write_text(json.dumps(dict(engine=job['engine'], assignedGpuGroup=gpu_group,
                                                                         requestedSettings=job, runs=manifests), indent=2))
                remaining = timeout - seconds
                if remaining <= 0:
                    raise TimeoutError('Prediction exceeded the configured total subprocess time limit.')
                seconds += _execute(argv, work_dir, work_dir / f'engine-seed-{seed}.log', cancelled, remaining, gpu_group)
        emit('analysis', 'Parsing real structure files and available confidence outputs.', 85)
        result = _collect_outputs(output_root, job['engine'], work_dir, job['chains'], cancelled=cancelled)
        result['returnedModelCount'] = len(result['rankedModels'])
        if job['engine'] == 'alphafold3':
            expected_samples = {(seed, sample) for seed in job['seeds'] for sample in range(job['numSamples'])}
            returned_samples = set()
            for item in result['rankedModels']:
                match = re.search(r'seed-(\d+)_sample-(\d+)', str(Path(item['structurePath']).parent))
                if match:
                    returned_samples.add((int(match.group(1)), int(match.group(2))))
            result['requestedSampleCount'] = len(expected_samples)
            if returned_samples:
                if returned_samples != expected_samples:
                    raise RuntimeError(f'Incomplete AlphaFold 3 sampling: expected {len(expected_samples)} seed/sample pairs, found {len(returned_samples)}; missing={sorted(expected_samples - returned_samples)}, unexpected={sorted(returned_samples - expected_samples)}.')
                result['actualSampleCount'] = len(returned_samples)
                result['samplingComplete'] = True
            else:
                result['actualSampleCount'] = None
                result['samplingComplete'] = False
                result.setdefault('warnings', []).append('Only top-level AlphaFold 3 output is available. Actual sampling coverage is unknown; requested seeds/samples have not been verified.')
        # Refuse unrelated or partial structures, including accidental fixture
        # replay from a misconfigured external command.
        expected = sorted(c['sequence'] for c in job['chains'])
        actual = sorted(c['sequence'] for c in result['chains'])
        if expected != actual:
            raise RuntimeError('Prediction output protein chains do not match submitted sequences. Inspect the raw output; it was not accepted as this prediction.')
        result['provenance'] = dict(mode='prediction', structureType='predicted', sourceUrl=SOURCES[job['engine']],
                                    sourceLabel=job['engine'], newPrediction=True,
                                    engineVersion=os.environ.get(_env_name(job['engine'], 'VERSION'), 'not reported by administrator'),
                                    assignedGpuGroup=gpu_group,
                                    allocationMode='local GPU group lease' if gpu_group is not None else 'inherited from administrator runner',
                                    seeds=job['seeds'], numSamples=job['numSamples'], numRecycles=job['numRecycles'],
                                    sequenceSha256=hashlib.sha256(json.dumps(job['chains'], sort_keys=True).encode()).hexdigest(),
                                    executionManifest='execution.json',
                                    confidenceDefinition=result.get('confidenceDefinition'))
        result['timings'] = dict(externalProcessSeconds=seconds, gpuQueueSeconds=queue_seconds)
        if job['engine'] == 'alphafold2' and result.get('pae') is None:
            result.setdefault('warnings', []).append('Native AlphaFold 2 pickle outputs are not loaded. PAE/pTM require a safe result_<model>.json export; PDB pLDDT is available.')
    _check_cancel(cancelled)
    result['timings']['totalSeconds'] = time.monotonic() - start
    result['timings']['scope'] = 'Application wall-clock; not isolated GPU inference time.'
    result['artifacts'] = [dict(name=str(p.relative_to(work_dir)), path=str(p.resolve())) for p in _safe_files(work_dir)]
    emit('completed', 'Structure and provenance are ready.', 100)
    return result
