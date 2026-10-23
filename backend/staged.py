"""Persistent CPU preparation and GPU prediction for installed model runtimes.

Only administrator-owned database profiles and argv templates enter this module.
The campaign worker supplies cacheRoot; it must never come from raw HTTP input.
Cache files are immutable after atomic publication; flock provides cross-process
single-flight on a local POSIX filesystem. This is not a distributed lock service.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import uuid

from . import engine as e
from .cache_access import shared_cache
from .parsers import parse_confidence, parse_structure
from .preparation_control import CacheBusy, PreparationDeferred, validate_progress
from .msa_metrics import summarize_a3m

CACHE_SCHEMA = 1
CHAIN_NAMESPACE = 'af3-chain-v1'
SUPPORTED = ('reference', 'alphafold3', 'colabfold')


def _json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    with temporary.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _command(engine, stage, profile=None):
    variable = e._env_name(engine, stage.upper() + '_COMMAND')
    profile_command = (profile or {}).get(stage + 'Command') if stage in {'prepare', 'finalize'} else None
    raw = json.dumps(profile_command) if profile_command is not None else os.environ.get(variable)
    if not raw:
        raise ValueError(f'{variable} is not configured. This engine cannot run the staged profile.')
    try:
        command = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f'{variable} must be a JSON argv array.') from exc
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x and '\0' not in x for x in command):
        raise ValueError(f'{variable} must be a nonempty JSON string array.')
    if shutil.which(command[0]) is None:
        raise ValueError(f'Configured {stage} executable is not installed: {command[0]}')
    return command


def staged_capabilities(database_profile=None):
    result = []
    resident_workers = os.environ.get('PROTEIN_STUDIO_PREDICTION_MODE') == 'workers'
    for engine in e.ENGINE_IDS:
        errors = []
        supported = engine in SUPPORTED and (not resident_workers or engine in {'reference', 'alphafold3'})
        if engine in ('alphafold3', 'colabfold'):
            for stage in (('prepare',) if resident_workers else ('prepare', 'predict')):
                try:
                    profile = database_profile if database_profile and database_profile.get('engine') == engine else None
                    _command(engine, stage, profile)
                    if stage == 'prepare' and profile and profile.get('searchBackend'):
                        _command(engine, 'finalize', profile)
                        if profile['searchBackend'].get('device') == 'gpu':
                            from .preparation_resources import search_gpu_configuration
                            search_gpu_configuration(require=True)
                except ValueError as exc:
                    errors.append(str(exc))
        if resident_workers and engine == 'colabfold':
            errors.append('ColabFold currently uses the legacy staged adapter; no native resident adapter is enabled.')
        result.append(dict(engine=engine, staged=supported,
                           available=engine == 'reference' or (supported and not errors),
                           executionMode='resident-workers' if resident_workers and supported else ('staged' if engine in SUPPORTED else 'monolithic'),
                           diagnostics=errors,
                           modelLifetime=('persistent native model per worker GPU group' if resident_workers and engine == 'alphafold3'
                                          else 'persistent public-data replay' if resident_workers and engine == 'reference'
                                          else 'per-target subprocess' if engine != 'reference' else 'public structure replay')))
    return result


def _normalize(job, require_prediction=True):
    norm = e.validate_submission(job)
    if norm['engine'] not in SUPPORTED:
        raise ValueError('AlphaFold 2 is currently monolithic; use its existing job runner, not the staged campaign profile.')
    profile = job.get('databaseProfile') or {}
    if not isinstance(profile, dict):
        raise ValueError('databaseProfile must be a validated administrator profile object.')
    if norm['engine'] != 'reference':
        if profile.get('engine') != norm['engine'] or not profile.get('ready'):
            raise ValueError('A ready database profile for this engine is required before preparation.')
        if not profile.get('profileFingerprint') or not profile.get('prepareVersion'):
            raise ValueError('Database profile must declare profileFingerprint and prepareVersion.')
        claimed = job.get('profileFingerprint')
        if claimed and claimed != profile['profileFingerprint']:
            raise ValueError('Database profile fingerprint changed; re-plan this campaign with the new release.')
        _command(norm['engine'], 'prepare', profile)
        if profile.get('searchBackend'):
            if profile['searchBackend'].get('gpuServer'):
                raise ValueError('Persistent external MMseqs GPU servers are not supported by the per-search GPU lease adapter.')
            _command(norm['engine'], 'finalize', profile)
        if require_prediction:
            _command(norm['engine'], 'predict')
    norm['databaseProfile'] = profile
    norm['executionProfile'] = job.get('executionProfile') or {}
    if not isinstance(norm['executionProfile'], dict):
        raise ValueError('executionProfile must be an object.')
    return norm


def prepare_cache_key(job):
    """Seeds/samples/recycles are deliberately excluded from sequence preparation."""
    norm = _normalize(job, require_prediction=False)
    profile = norm['databaseProfile']
    identity = dict(schema=CACHE_SCHEMA, engine=norm['engine'], chains=norm['chains'],
                    example=norm.get('example'), prepareVersion=profile.get('prepareVersion', 'public-fixture-v1'),
                    profileFingerprint=profile.get('profileFingerprint', 'no-database'),
                    searchVersion=profile.get('searchVersion'),
                    searchParameters=profile.get('searchParameters', {}),
                    templateParameters=profile.get('templateParameters', {}),
                    executionPreparation=norm['executionProfile'].get('preparation', {}),
                    prepareCommand=_command(norm['engine'], 'prepare', profile) if norm['engine'] != 'reference' else None)
    if profile.get('searchBackend'):
        identity['searchBackend'] = profile['searchBackend']
        identity['finalizeCommand'] = _command(norm['engine'], 'finalize', profile)
    if profile.get('executables'):
        identity['executables'] = profile['executables']
    if norm['engine'] == 'reference':
        filename = e.EXAMPLES[norm['example']][0]
        identity['fixtureSha256'] = _digest(e.FIXTURES / filename)
        if norm['example'] == 'ubiquitin-afdb':
            identity['paeSha256'] = _digest(e.FIXTURES / 'P0CG47_pae.json')
    encoded = json.dumps(identity, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


@contextmanager
def _singleflight(root, key, emit, cancelled, blocking=True):
    root.mkdir(parents=True, exist_ok=True)
    with (root / (key + '.lock')).open('a+') as lock:
        announced = False
        while True:
            e._check_cancel(cancelled)
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not blocking:
                    raise CacheBusy(key)
                if not announced:
                    emit('waiting', 'Waiting for the same preparation cache key to finish.', 10)
                    announced = True
                threading.Event().wait(.1)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _inside(root, relative):
    path = root / relative
    if Path(relative).is_absolute() or '..' in Path(relative).parts or path.is_symlink():
        raise ValueError('Prepared manifest contains an unsafe path.')
    path.resolve().relative_to(root.resolve())
    return path


def _manifest(root, key):
    marker = root / 'COMPLETE.json'
    if not marker.is_file() or marker.is_symlink():
        return None
    data = json.loads(marker.read_text())
    if data.get('cacheKey') != key or data.get('schema') != CACHE_SCHEMA:
        raise ValueError('Preparation cache manifest does not match its key/version.')
    if any(path.is_symlink() for path in root.rglob('*')):
        raise ValueError('Preparation cache may not contain symbolic links.')
    actual_files = {str(path.relative_to(root)) for path in e._safe_files(root) if path != marker}
    expected_files = {artifact['name'] for artifact in data['artifacts']}
    if actual_files != expected_files:
        raise ValueError('Preparation cache is incomplete or modified; unexpected or missing artifacts.')
    for artifact in data['artifacts']:
        path = _inside(root, artifact['name'])
        if not path.is_file() or path.stat().st_size != artifact['bytes'] or _digest(path) != artifact['sha256']:
            raise ValueError('Preparation cache is incomplete or modified; remove the affected cache entry and retry.')
    _inside(root, data['inputRelativePath'])
    return data


def _descriptor(root, data, hit):
    result = dict(schema=CACHE_SCHEMA, engine=data['engine'], cacheKey=data['cacheKey'], cacheHit=hit,
                cacheDirectory=str(root), manifestPath=str(root / 'COMPLETE.json'),
                inputPath=str(root / data['inputRelativePath']), format=data['format'],
                databaseProfile=data.get('databaseProfile'), profileFingerprint=data.get('profileFingerprint'),
                artifacts=[dict(name=item['name'], path=str(root / item['name']), sha256=item['sha256'], bytes=item['bytes'])
                           for item in data['artifacts']], preparationSeconds=data['preparationSeconds'])
    if 'chainReuse' in data:
        result['chainReuse'] = copy.deepcopy(data['chainReuse'])
        if hit:
            # Entries describe the immutable assembly's original provenance.
            # Only the operation counters describe this whole-target lookup.
            result['chainReuse'].update(newChainSearches=0, chainCacheHits=0, wholeTargetCacheHit=True)
    return result


def _validate_af3(path, chains):
    data = json.loads(path.read_text())
    if data.get('dialect') != 'alphafold3' or not isinstance(data.get('sequences'), list):
        raise ValueError('Preparation did not produce an AlphaFold 3 input JSON.')
    actual = []
    for entry in data['sequences']:
        if set(entry) != {'protein'}:
            raise ValueError('This protein-only pipeline cannot accept non-protein prepared entities.')
        protein = entry['protein']
        ids = protein['id'] if isinstance(protein.get('id'), list) else [protein.get('id')]
        actual.extend(dict(id=chain, sequence=protein.get('sequence')) for chain in ids)
        for field in ('unpairedMsa', 'pairedMsa'):
            if not isinstance(protein.get(field), str):
                raise ValueError(f'Prepared AF3 input must contain inline {field}, including explicit empty strings when intended.')
        if not isinstance(protein.get('templates'), list):
            raise ValueError('Prepared AF3 input must contain explicit templates, including [] when no templates are used.')
        for template in protein['templates']:
            if not isinstance(template, dict) or not isinstance(template.get('mmcif'), str) or not template['mmcif'].strip():
                raise ValueError('Prepared AF3 templates must contain inline nonempty mmcif text, not a controller-local path.')
            query, target = template.get('queryIndices'), template.get('templateIndices')
            if (not isinstance(query, list) or not isinstance(target, list) or len(query) != len(target)
                or any(type(index) is not int or not 0 <= index < len(protein['sequence']) for index in query)
                or any(type(index) is not int or index < 0 for index in target)
                or len(set(query)) != len(query) or len(set(target)) != len(target)):
                raise ValueError('Prepared AF3 template residue mappings are invalid.')
    # AF3 Input.to_json groups equal full chain content into id:[A,C], even when the
    # requested order was A,B,C. Identity is chain ID + sequence, not JSON order.
    if (len({entry['id'] for entry in actual}) != len(actual)
        or {entry['id']:entry['sequence'] for entry in actual} != {entry['id']:entry['sequence'] for entry in chains}):
        raise ValueError('Prepared AF3 chains/IDs do not match the requested input.')
    def reject_paths(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith('Path') and item:
                    raise ValueError('Prepared AF3 input must be self-contained; external Path fields are not supported by this cache.')
                reject_paths(item)
        elif isinstance(value, list):
            for item in value:
                reject_paths(item)
    reject_paths(data)
    return data


def _validate_a3m(path, chains):
    # ColabFold's multimer header lists unique-chain lengths and copy counts;
    # the first FASTA sequence contains each unique chain exactly once.
    sequences = [c['sequence'] for c in chains]
    unique = list(dict.fromkeys(sequences))
    with path.open() as stream:
        first = next((line.strip() for line in stream if line.strip()), '')
    header = first[1:] if first.startswith('#') else None
    if header is not None:
        parts = header.split('\t')
        try:
            lengths = [int(x) for x in parts[0].split(',')]
            counts = [int(x) for x in parts[1].split(',')]
        except (ValueError, IndexError) as exc:
            raise ValueError('Invalid ColabFold A3M chain-length/cardinality header.') from exc
        if lengths != [len(seq) for seq in unique] or counts != [sequences.count(seq) for seq in unique]:
            raise ValueError('Prepared A3M chain lengths/cardinalities do not match the input.')
    else:
        if len(sequences) != 1:
            raise ValueError('Complex A3M must include ColabFold length/cardinality metadata.')
        lengths = [len(sequences[0])]
    # Paired output starts with a concatenated query. In unpaired complex mode,
    # each chain query instead appears in its own gap-padded block. Validate the
    # first non-gap segment for every chain, rather than rejecting valid unpaired
    # output merely because the first row has gaps for the other chains.
    seen = set()
    def check_row(row):
        row = re.sub('[a-z]', '', row)
        if len(row) != sum(lengths):
            raise ValueError('Prepared A3M has an inconsistent alignment width.')
        offset = 0
        for index, length in enumerate(lengths):
            segment = row[offset:offset + length]
            offset += length
            if index not in seen and segment != '-' * length:
                if segment != unique[index]:
                    raise ValueError('Prepared A3M query sequence does not match the input.')
                seen.add(index)
    with path.open() as stream:
        row = []
        for line in stream:
            line = line.strip()
            if line.startswith('>'):
                if row:
                    check_row(''.join(row))
                    if len(seen) == len(unique):
                        return
                row = []
            elif line and not line.startswith('#'):
                row.append(line)
        if row:
            check_row(''.join(row))
    if len(seen) != len(unique):
        raise ValueError('Prepared A3M is missing one or more chain query sequences.')


def _values(norm, fasta, input_json, job_json, profile_json, output):
    profile = norm['databaseProfile']
    values = dict(fasta=fasta, input_json=input_json, job_json=job_json, profile_json=profile_json,
                  output_dir=output, db_root=profile.get('root', ''), name='protein',
                  seed=norm['seeds'][0], num_samples=norm['numSamples'], num_recycles=norm['numRecycles'])
    for key, value in profile.get('paths', {}).items():
        if key not in values and re.fullmatch('[a-z_]+', key):
            values[key] = value
    return values


def _validate_chain_msa(text, sequence, field, cancelled=lambda: False):
    return summarize_a3m(text, sequence, 'unpaired' if field == 'unpairedMsa' else 'pairing-candidates', cancelled)


def _validate_af3_leaf(path, sequence, quality=None, cancelled=lambda: False):
    data = _validate_af3(path, [dict(id='A', sequence=sequence)])
    if len(data['sequences']) != 1:
        raise ValueError('AF3 chain preparation must contain exactly one canonical protein entity.')
    if type(data.get('version')) is not int or data['version'] not in (1, 2, 3, 4):
        raise ValueError('AF3 chain preparation requires a supported native input version (1–4).')
    # Sequence-only reuse is valid for this API's unmodified proteins. Reject
    # runner-injected chemistry rather than silently dropping it on assembly.
    if any(data.get(key) for key in ('bondedAtomPairs', 'userCCD', 'userCCDPath')):
        raise ValueError('AF3 chain preparation cannot introduce bonds or custom CCD chemistry.')
    protein = data['sequences'][0]['protein']
    if protein.get('modifications'):
        raise ValueError('AF3 chain preparation cannot introduce protein modifications.')
    for field, role in (('unpairedMsa', 'unpaired'), ('pairedMsa', 'paired')):
        summary = _validate_chain_msa(protein[field], sequence, field, cancelled)
        if quality is not None:
            quality[role] = summary
    return data


def _preparation_manifest(temporary, primary, norm, key, started, format_name, cancelled):
    if any(path.is_symlink() for path in temporary.rglob('*')):
        raise ValueError('Preparation outputs must be self-contained regular files, without symbolic links.')
    artifacts = []
    for path in e._safe_files(temporary):
        e._check_cancel(cancelled)
        artifacts.append(dict(name=str(path.relative_to(temporary)), bytes=path.stat().st_size, sha256=_digest(path)))
    return dict(schema=CACHE_SCHEMA, cacheKey=key, engine=norm['engine'],
                format=format_name, inputRelativePath=str(primary.relative_to(temporary)),
                artifacts=artifacts, preparationSeconds=time.monotonic() - started,
                databaseProfile=norm['databaseProfile'].get('id'),
                profileFingerprint=norm['databaseProfile'].get('profileFingerprint'))


def _chain_identity(norm, sequence):
    canonical = _normalize(dict(norm, chains=[dict(id='A', sequence=sequence)]), require_prediction=False)
    key = hashlib.sha256((CHAIN_NAMESPACE + '\0' + prepare_cache_key(canonical)).encode()).hexdigest()
    return canonical, key


def _execute_chain_preparation(command, norm, values, temporary, logs, key, chain_ids, emit, cancelled, timeout, cooperative):
    backend = norm['databaseProfile'].get('searchBackend')
    group = []
    def searching():
        emit(dict(type='preparation_activity', state='searching', chainKey=key, chainIds=chain_ids or ['A']))
        emit('preparation', 'Running chain MSA search and template preparation.', 20)
    if backend and backend.get('device') == 'gpu':
        from .gpu_access import try_gpu_group, GPUDevicesBusy
        from .preparation_resources import search_gpu_configuration
        groups = search_gpu_configuration(require=True)['groups']
        while True:
            e._check_cancel(cancelled)
            try:
                with try_gpu_group(groups, cancelled) as group:
                    searching()
                    search_wall = e._execute(command, temporary, logs / 'prepare-search.log', cancelled, timeout, group)
                break
            except GPUDevicesBusy as exc:
                if cooperative:
                    raise CacheBusy(key, wait_reason='search_gpu') from exc
                emit('waiting', 'Waiting for an explicitly configured search GPU group.', 10)
                threading.Event().wait(.1)
    else:
        searching()
        search_wall = e._execute(command, temporary, logs / ('prepare-search.log' if backend else 'prepare.log'), cancelled, timeout, [])
    if not backend:
        return search_wall, {}
    # The GPU lock ends before native UniProt/HMMER/template processing. The
    # second subprocess explicitly receives no CUDA devices, even on a GPU node.
    finalize = e._format_command(_command('alphafold3', 'finalize', norm['databaseProfile']), values)
    finalize_wall = e._execute(finalize, temporary, logs / 'prepare-finalize.log', cancelled, timeout, [])
    details = dict(searchBackend=copy.deepcopy(backend), msaSearchProcessWallSeconds=search_wall,
                   cpuFinalizeProcessWallSeconds=finalize_wall)
    _json(logs / 'search-execution.json', dict(searchArgv=command, finalizeArgv=finalize,
          assignedSearchGpuGroup=group, gpuLease=bool(group), **details))
    return search_wall + finalize_wall, details


def _prepare_af3_chain(norm, sequence, cache_root, work_dir, emit, cancelled, *, cooperative=False, chain_ids=None, producer_item_id=None):
    """Whole-target lock -> one leaf lock; never calls prepare_stage recursively."""
    started = time.monotonic()
    canonical, key = _chain_identity(norm, sequence)
    root = cache_root / CHAIN_NAMESPACE
    destination = root / key
    with _singleflight(root, key, emit, cancelled, blocking=not cooperative):
        existing = _manifest(destination, key)
        if existing:
            data = _validate_af3_leaf(_inside(destination, existing['inputRelativePath']), sequence, cancelled=cancelled)
            return data, key, True, existing
        if destination.exists():
            raise ValueError('Chain cache directory has no completion marker; remove this incomplete entry before retrying.')
        temporary = Path(tempfile.mkdtemp(prefix=key + '.partial-', dir=root))
        try:
            prep_job = dict(canonical, seeds=[1], numSamples=1, numRecycles=10)
            fasta, input_json, job_json = e._write_inputs(prep_job, temporary / 'input')
            profile_json = temporary / 'input/database-profile.json'
            _json(profile_json, canonical['databaseProfile'])
            output = temporary / 'prepared'
            output.mkdir()
            command = e._format_command(_command('alphafold3', 'prepare', canonical['databaseProfile']),
                                        _values(prep_job, fasta, input_json, job_json, profile_json, output))
            logs = work_dir / 'chains' / key
            _json(logs / 'prepare-execution.json', dict(argv=command, cacheKey=key, namespace=CHAIN_NAMESPACE,
                                                       gpuLease=False, searchGpuRequired=canonical['databaseProfile'].get('searchBackend', {}).get('device') == 'gpu',
                                                       databaseProfile=canonical['databaseProfile']))
            timeout = float(os.environ.get('PROTEIN_STUDIO_PREPARE_TIMEOUT_SECONDS', '86400'))
            if not 0 < timeout <= 7 * 86400:
                raise ValueError('Preparation timeout must be positive and at most 7 days.')
            search_wall, search_details = _execute_chain_preparation(
                command, prep_job, _values(prep_job, fasta, input_json, job_json, profile_json, output),
                temporary, logs, key, chain_ids, emit, cancelled, timeout, cooperative)
            candidates = [p for p in e._safe_files(output) if p.name.endswith('_data.json')]
            if len(candidates) != 1:
                raise ValueError('AF3 chain preparation must produce exactly one *_data.json.')
            primary = candidates[0]
            quality = {'schemaVersion': 1}
            data = _validate_af3_leaf(primary, sequence, quality, cancelled)
            manifest = _preparation_manifest(temporary, primary, canonical, key, started, 'alphafold3-data-json', cancelled)
            manifest.update(namespace=CHAIN_NAMESPACE, sequenceSha256=hashlib.sha256(sequence.encode()).hexdigest(),
                            msaQuality=quality, searchProcessWallSeconds=search_wall)
            manifest.update(search_details)
            if producer_item_id is not None:
                manifest['producerItemId'] = producer_item_id
            _json(temporary / 'COMPLETE.json', manifest)
            e._check_cancel(cancelled)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return data, key, False, manifest


def _assemble_af3(norm, cache_root, output, work_dir, emit, cancelled, *, cooperative=False, resume=None, producer_item_id=None):
    grouped = {}
    for chain in norm['chains']:
        grouped.setdefault(chain['sequence'], []).append(chain['id'])
    whole_key = prepare_cache_key(norm)
    identities = {sequence: dict(chainIds=ids, sequenceSha256=hashlib.sha256(sequence.encode()).hexdigest(),
                                cacheKey=_chain_identity(norm, sequence)[1], residueCount=len(sequence))
                  for sequence, ids in grouped.items()}
    historical = {}
    if resume:
        resume = validate_progress(resume)
        if resume['wholeCacheKey'] != whole_key:
            raise ValueError('Preparation checkpoint belongs to a different target or database identity.')
        expected = {entry['cacheKey']: entry for entry in identities.values()}
        for entry in resume['completedLeaves'] + resume['pendingLeaves']:
            identity = expected.get(entry['cacheKey'])
            if identity is None or any(entry.get(k) != v for k, v in identity.items()):
                raise ValueError('Preparation checkpoint does not match this target chain.')
        historical = {entry['cacheKey']: entry for entry in resume['completedLeaves']}
    proteins, entries, assembled = {}, {}, None

    def checkpoint():
        retained = dict(historical)
        retained.update({entry['cacheKey']: entry for entry in entries.values()})
        return validate_progress(dict(schema=1, wholeCacheKey=whole_key,
            completedLeaves=[retained[identities[s]['cacheKey']] for s in grouped if identities[s]['cacheKey'] in retained],
            pendingLeaves=[identities[s] for s in grouped if identities[s]['cacheKey'] not in retained]))

    if cooperative:
        # Pin intentions before any leaf publication. A hard process exit after
        # rename must not expose a completed leaf to maintenance without a ref.
        emit(dict(type='preparation_checkpoint', progress=checkpoint()))
    pending = list(grouped)
    while pending:
        advanced, busy, busy_reasons = False, [], {}
        for sequence in pending:
            e._check_cancel(cancelled)
            try:
                leaf, key, hit, manifest = _prepare_af3_chain(norm, sequence, cache_root, work_dir, emit, cancelled,
                                                            cooperative=cooperative, chain_ids=grouped[sequence],
                                                            producer_item_id=producer_item_id)
            except CacheBusy as exc:
                busy.append(sequence)
                busy_reasons[sequence] = exc.wait_reason
                continue
            if assembled is None:
                assembled = copy.deepcopy(leaf)
                assembled.update(name='protein', modelSeeds=[1], sequences=[])
            elif leaf['version'] != assembled['version']:
                raise ValueError('AF3 chain preparations use different native JSON versions; re-plan with a consistent runner version.')
            proteins[sequence] = leaf['sequences'][0]['protein']
            # Preserve this target's first resolution across durable defers. A
            # reread of its own published leaf is not a new cross-target hit.
            original = historical.get(key)
            owned = producer_item_id is not None and manifest.get('producerItemId') == producer_item_id
            entry = dict(identities[sequence], cacheHit=original['cacheHit'] if original else (hit and not owned))
            for field in ('msaQuality', 'searchProcessWallSeconds', 'searchBackend', 'msaSearchProcessWallSeconds', 'cpuFinalizeProcessWallSeconds'):
                if field in manifest:
                    entry[field] = copy.deepcopy(manifest[field])
            entries[sequence] = entry
            advanced = True
            if cooperative:
                emit(dict(type='preparation_checkpoint', progress=checkpoint()))
        if not busy:
            break
        if not advanced:
            identity = identities[busy[0]]
            raise PreparationDeferred(checkpoint(), identity['cacheKey'], identity['chainIds'], busy_reasons[busy[0]])
        pending = busy
    emit(dict(type='preparation_activity', state='assembling'))
    for chain in norm['chains']:
        protein = copy.deepcopy(proteins[chain['sequence']])
        protein['id'] = chain['id']
        assembled['sequences'].append(dict(protein=protein))
    primary = output / 'protein_data.json'
    _json(primary, assembled)
    _validate_af3(primary, norm['chains'])
    ordered = [entries[sequence] for sequence in grouped]
    stats = dict(strategy='af3-chain-reuse-v1', totalChains=len(norm['chains']), uniqueChains=len(grouped),
                 newChainSearches=sum(not entry['cacheHit'] for entry in ordered),
                 chainCacheHits=sum(entry['cacheHit'] for entry in ordered),
                 duplicateChainCopies=len(norm['chains']) - len(grouped), wholeTargetCacheHit=False, entries=ordered)
    return primary, stats


def prepare_stage(job, work_dir, emit, cancelled):
    cache_root = Path(job.get('cacheRoot') or os.environ.get('PROTEIN_STUDIO_PREP_CACHE', Path(work_dir).resolve() / 'cache')).resolve()
    with shared_cache(cache_root, cancelled):
        return _prepare_stage_locked(job, work_dir, emit, cancelled)


def prepare_cooperative(job, work_dir, emit, cancelled):
    cache_root = Path(job.get('cacheRoot') or os.environ.get('PROTEIN_STUDIO_PREP_CACHE', Path(work_dir).resolve() / 'cache')).resolve()
    with shared_cache(cache_root, cancelled):
        try:
            return _prepare_stage_locked(job, work_dir, emit, cancelled, cooperative=True)
        except CacheBusy as exc:
            progress = job.get('_preparationProgress') or dict(schema=1, wholeCacheKey=prepare_cache_key(job),
                                                              completedLeaves=[], pendingLeaves=[])
            if progress.get('wholeCacheKey') != prepare_cache_key(job):
                raise ValueError('Preparation checkpoint belongs to a different target or database identity.')
            raise PreparationDeferred(progress, exc.key) from exc


def _prepare_stage_locked(job, work_dir, emit, cancelled, cooperative=False):
    started = time.monotonic()
    norm = _normalize(job, require_prediction=False)
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    key = prepare_cache_key(job)
    cache_root = Path(job.get('cacheRoot') or os.environ.get('PROTEIN_STUDIO_PREP_CACHE', work_dir / 'cache')).resolve()
    destination = cache_root / key
    emit('preparation', 'Resolving content-addressed preparation artifacts.', 5)
    with _singleflight(cache_root, key, emit, cancelled, blocking=not cooperative):
        e._check_cancel(cancelled)
        existing = _manifest(destination, key)
        if existing:
            result = _descriptor(destination, existing, True)
            _json(work_dir / 'prepared.json', result)
            emit('prepared', 'Reusing verified preparation artifacts; no database search was repeated.', 100)
            return result
        # An incomplete destination never counts as a cache hit. Partial temporary
        # directories from another crashed process are also never published.
        if destination.exists():
            raise ValueError('Cache directory exists without a completion marker; remove this incomplete entry before retrying.')
        temporary = Path(tempfile.mkdtemp(prefix=key + '.partial-', dir=cache_root))
        try:
            prep_job = dict(norm, seeds=[1], numSamples=1,
                            numRecycles=10 if norm['engine'] == 'alphafold3' else 3)
            fasta, input_json, job_json = e._write_inputs(prep_job, temporary / 'input')
            profile_json = temporary / 'input' / 'database-profile.json'
            _json(profile_json, norm['databaseProfile'])
            output = temporary / 'prepared'
            output.mkdir()
            chain_reuse = None
            if norm['engine'] == 'reference':
                filename = e.EXAMPLES[norm['example']][0]
                source = e.FIXTURES / filename
                primary = output / filename
                shutil.copyfile(source, primary)
                parse_structure(primary)
                if norm['example'] == 'ubiquitin-afdb':
                    shutil.copyfile(e.FIXTURES / 'P0CG47_pae.json', output / 'P0CG47_pae.json')
                format_name = 'public-reference'
                emit('preparation', 'Validated and cached a public structure for replay; no MSA or model computation.', 80)
            elif norm['engine'] == 'alphafold3':
                primary, chain_reuse = _assemble_af3(norm, cache_root, output, work_dir, emit, cancelled,
                                                     cooperative=cooperative, resume=job.get('_preparationProgress'),
                                                     producer_item_id=job.get('_preparationOrigin') if cooperative else None)
                format_name = 'alphafold3-data-json'
            else:
                command = e._format_command(_command(norm['engine'], 'prepare', norm['databaseProfile']),
                                             _values(prep_job, fasta, input_json, job_json, profile_json, output))
                _json(work_dir / 'prepare-execution.json', dict(argv=command, cacheKey=key, gpuLease=False,
                                                               databaseProfile=norm['databaseProfile']))
                timeout = float(os.environ.get('PROTEIN_STUDIO_PREPARE_TIMEOUT_SECONDS', '86400'))
                if not 0 < timeout <= 7 * 86400:
                    raise ValueError('Preparation timeout must be positive and at most 7 days.')
                # Empty CUDA_VISIBLE_DEVICES explicitly keeps this CPU stage off
                # the GPU pool. GPU-MSA needs a separate resource stage/profile.
                e._execute(command, temporary, work_dir / 'prepare.log', cancelled, timeout, [])
                files = e._safe_files(output)
                candidates = [p for p in files if p.suffix == '.a3m']
                if len(candidates) != 1:
                    raise ValueError('ColabFold preparation must produce one unpacked target A3M, without intermediate A3M files.')
                primary = candidates[0]
                _validate_a3m(primary, norm['chains'])
                format_name = 'colabfold-a3m'
            manifest = _preparation_manifest(temporary, primary, norm, key, started, format_name, cancelled)
            if chain_reuse is not None:
                manifest['chainReuse'] = chain_reuse
            _json(temporary / 'COMPLETE.json', manifest)
            e._check_cancel(cancelled)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        result = _descriptor(destination, manifest, False)
        _json(work_dir / 'prepared.json', result)
        emit('prepared', 'Preparation artifacts validated and atomically published.', 100)
        return result


def predict_stage(job, prepared, work_dir, emit, cancelled):
    started = time.monotonic()
    norm = _normalize(job)
    key = prepare_cache_key(job)
    if prepared.get('cacheKey') != key or prepared.get('engine') != norm['engine']:
        raise ValueError('Prepared artifact does not match this job/profile.')
    cache = Path(prepared['cacheDirectory']).resolve()
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    e._check_cancel(cancelled)
    local_prepared = work_dir / 'prepared'
    if local_prepared.exists():
        raise ValueError('Prediction attempt directory already has prepared inputs; use a fresh attempt directory.')
    # Maintenance can proceed after this private copy; long GPU execution must
    # not retain a shared cache lock once all needed bytes are independent.
    with shared_cache(cache.parent, cancelled):
        manifest = _manifest(cache, key)
        if not manifest:
            raise ValueError('Prepared cache has no complete manifest.')
        primary = _inside(cache, manifest['inputRelativePath'])
        shutil.copytree(cache / 'prepared', local_prepared)
        local_primary = local_prepared / primary.relative_to(cache / 'prepared')
    fasta, input_json, job_json = e._write_inputs(norm, work_dir / 'input')
    profile_json = work_dir / 'input/database-profile.json'
    _json(profile_json, norm['databaseProfile'])
    _json(work_dir / 'preparation-manifest.json', manifest)
    if norm['engine'] == 'reference':
        example = e.load_example(norm['example'])
        source = 'afdb' if example['provenance']['structureType'] == 'predicted' else 'unknown'
        result = parse_structure(local_primary, source)
        if norm['example'] == 'ubiquitin-afdb':
            result.update(parse_confidence(local_prepared / 'P0CG47_pae.json'))
        result.update(structurePath=str(local_primary), structureFile=str(local_primary),
                      provenance=example['provenance'], rankedModels=[dict(rank=1, name=example['name'], structurePath=str(local_primary),
                      structureFormat=result['structureFormat'], meanPlddt=result.get('meanPlddt'))])
        result['timings'] = dict(replaySeconds=time.monotonic() - started)
        emit('prediction', 'Reading cached public structure coordinates; no new prediction.', 90)
    else:
        if norm['engine'] == 'alphafold3':
            data = _validate_af3(local_primary, norm['chains'])
            data['modelSeeds'] = norm['seeds']
            data['name'] = 'protein'
            _json(local_primary, data)
        else:
            _validate_a3m(local_primary, norm['chains'])
        output_root = work_dir / 'output'
        output_root.mkdir()
        timeout = float(os.environ.get('PROTEIN_STUDIO_TIMEOUT_SECONDS', '7200'))
        if not 0 < timeout <= 7 * 86400:
            raise ValueError('Prediction timeout must be positive and at most 7 days.')
        subprocess_seconds, runs = 0.0, []
        queue_start = time.monotonic()
        with e._gpu_lease(emit, cancelled) as group:
            queue_seconds = time.monotonic() - queue_start
            seeds = [norm['seeds'][0]] if norm['engine'] == 'alphafold3' else norm['seeds']
            for seed in seeds:
                output = output_root if norm['engine'] == 'alphafold3' else output_root / f'seed-{seed}'
                output.mkdir(exist_ok=True)
                values = _values(norm, fasta, input_json, job_json, profile_json, output)
                values.update(prepared_input=local_primary, prepared_dir=local_primary.parent, seed=seed)
                command = e._format_command(_command(norm['engine'], 'predict'), values)
                runs.append(dict(seed=seed, argv=command))
                _json(work_dir / 'predict-execution.json', dict(runs=runs, cacheKey=key, assignedGpuGroup=group,
                                                               requestedSettings=norm))
                emit('prediction', 'Executing model with verified prepared data; database search is disabled.', 30)
                remaining = timeout - subprocess_seconds
                if remaining <= 0:
                    raise TimeoutError('Prediction stage exceeded total subprocess time.')
                subprocess_seconds += e._execute(command, work_dir, work_dir / f'predict-seed-{seed}.log', cancelled, remaining, group)
        # GPU lease is released before CPU parsing and publication.
        emit('analysis', 'Validating real structures and available confidence outputs.', 90)
        result = e._collect_outputs(output_root, norm['engine'], work_dir, norm['chains'], cancelled=cancelled)
        result['returnedModelCount'] = len(result['rankedModels'])
        if norm['engine'] == 'alphafold3':
            expected = {(seed, sample) for seed in norm['seeds'] for sample in range(norm['numSamples'])}
            actual = set()
            for model in result['rankedModels']:
                match = re.search(r'seed-(\d+)_sample-(\d+)', str(Path(model['structurePath']).parent))
                if match:
                    actual.add((int(match[1]), int(match[2])))
            if actual != expected:
                raise RuntimeError(f'Incomplete staged AF3 output: expected {len(expected)} seed/sample pairs, found {len(actual)}.')
            result.update(requestedSampleCount=len(expected), actualSampleCount=len(actual), samplingComplete=True)
        result['provenance'] = dict(mode='prediction', newPrediction=True, structureType='predicted',
                                    sourceLabel=norm['engine'], sourceUrl=e.SOURCES[norm['engine']],
                                    engineVersion=os.environ.get(e._env_name(norm['engine'], 'VERSION'), 'administrator did not report version'),
                                    assignedGpuGroup=group, seeds=norm['seeds'], numSamples=norm['numSamples'], numRecycles=norm['numRecycles'],
                                    confidenceDefinition=result.get('confidenceDefinition'), executionManifest='predict-execution.json')
        result['timings'] = dict(externalProcessSeconds=subprocess_seconds, gpuQueueSeconds=queue_seconds)
    result['provenance'].update(pipelineMode='staged', preparationCacheKey=key,
                               databaseProfile=manifest.get('databaseProfile'), profileFingerprint=manifest.get('profileFingerprint'))
    result['timings'].update(totalSeconds=time.monotonic() - started, scope='Prediction-stage wall-clock, excluding cached preparation; not isolated GPU time.')
    result['artifacts'] = [dict(name=str(p.relative_to(work_dir)), path=str(p)) for p in e._safe_files(work_dir)]
    e._check_cancel(cancelled)
    return result


prepare = prepare_stage
predict = predict_stage
