#!/usr/bin/env python3
"""Two-phase local ColabFold/MMseqs2 -> self-contained single-chain AF3 input.

search: ColabFold 1.6.3 local unpaired search, optionally on assigned GPUs.
finalize: CPU-only AF3 3.0.4 UniProt candidates / templates, then atomic JSON.
No databases, models or software are downloaded. No public MSA API is used.
Native API/parameters are pinned to the official v3.0.4 pipeline.py/msa.py /
templates.py; ColabFold flags are pinned to v1.6.3 colabfold/mmseqs/search.py.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.msa_metrics import summarize_a3m

ADAPTER_VERSION = 'mmseqs-af3-v1'
AF3_VERSION = '3.0.4'
COLABFOLD_VERSION = '1.6.3'
MAX_SOURCE_BYTES = 128 * 1024 * 1024
STATE_NAME = 'mmseqs-search-state.json'
DATA_NAME = 'protein_data.json'
CF_FLAGS = {'threads', 'use_env', 'db_load_mode', 'prefilter_mode', 's', 'filter',
            'expand_eval', 'align_eval', 'diff', 'qsc', 'max_accept'}
NATIVE_FLAGS = {'jackhmmer_n_cpu', 'jackhmmer_max_parallel_shards',
                'uniprot_cluster_annot_z_value'}
SOURCES = [
    'https://github.com/sokrypton/ColabFold/blob/v1.6.3/colabfold/mmseqs/search.py',
    'https://github.com/soedinglab/MMseqs2/wiki#gpu-accelerated-search',
    'https://github.com/google-deepmind/alphafold3/blob/v3.0.4/src/alphafold3/data/pipeline.py',
    'https://github.com/google-deepmind/alphafold3/blob/v3.0.4/src/alphafold3/data/msa.py',
    'https://github.com/google-deepmind/alphafold3/blob/v3.0.4/src/alphafold3/data/templates.py',
]


class SearchCancelled(RuntimeError):
    pass


def _check(cancelled):
    if cancelled():
        raise SearchCancelled('Local MSA preparation cancelled; no final input was published.')


def _read(path, limit=MAX_SOURCE_BYTES):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f'Expected a regular input file of at most {limit} bytes: {path}')
    # newline="" preserves the original A3M including CRLF, for hashes and output.
    with path.open(encoding='utf-8', newline='') as stream:
        text = stream.read(limit + 1)
    if len(text.encode('utf-8')) > limit:
        raise ValueError(f'Input exceeds the {limit}-byte limit: {path}')
    return text


def _json(path):
    value = json.loads(_read(path))
    if not isinstance(value, dict):
        raise ValueError('Input/profile must be a JSON object.')
    return value


def _identity(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.partial')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError('Prepared input exceeds the bounded JSON input limit.')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_input(data):
    allowed = {'name', 'dialect', 'version', 'modelSeeds', 'sequences'}
    if set(data) - allowed:
        raise ValueError('Unsupported AF3 input fields: ' + ', '.join(sorted(set(data) - allowed)))
    if data.get('dialect') != 'alphafold3' or type(data.get('version')) is not int or data['version'] not in (1, 2, 3, 4):
        raise ValueError('Expected native AF3 dialect and supported input version 1..4.')
    if not isinstance(data.get('name'), str) or not data['name'].strip():
        raise ValueError('A nonempty AF3 input name is required.')
    seeds = data.get('modelSeeds')
    if not isinstance(seeds, list) or not seeds or any(type(seed) is not int or seed < 0 for seed in seeds):
        raise ValueError('modelSeeds must be nonempty nonnegative integers.')
    entities = data.get('sequences')
    if not isinstance(entities, list) or len(entities) != 1 or not isinstance(entities[0], dict) or set(entities[0]) != {'protein'}:
        raise ValueError('This adapter accepts one independent protein chain, never an already paired complex.')
    protein = entities[0]['protein']
    if not isinstance(protein, dict) or set(protein) - {'id', 'sequence', 'modifications'} or protein.get('modifications') not in (None, []):
        raise ValueError('Only unmodified protein id/sequence are accepted; existing MSA/templates/custom fields are not overwritten.')
    ids = protein.get('id')
    if isinstance(ids, list) and len(ids) == 1:
        ids = ids[0]
    if not isinstance(ids, str) or not ids or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' for c in ids):
        raise ValueError('Exactly one uppercase AF3 chain ID is required.')
    sequence = protein.get('sequence')
    if not isinstance(sequence, str) or not sequence or set(sequence) - set('ACDEFGHIKLMNPQRSTVWY'):
        raise ValueError('The query must contain standard uppercase protein residues.')
    return protein


def _positive_int(value, name, allow_none=False):
    if allow_none and value is None:
        return
    if type(value) is not int or value < 1:
        raise ValueError(name + ' must be a positive integer.')


def _required(mapping, name):
    value = mapping.get(name)
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError(f'{name} must be an administrator-configured absolute local path.')
    return value


def validate_profile(profile):
    backend = profile.get('searchBackend', {})
    if not isinstance(backend, dict) or backend.get('id') != 'mmseqs2-colabfold':
        raise ValueError('Expected searchBackend.id=mmseqs2-colabfold.')
    if backend.get('adapterVersion') != ADAPTER_VERSION or backend.get('colabfoldVersion') != COLABFOLD_VERSION:
        raise ValueError('Adapter requires mmseqs-af3-v1 and ColabFold 1.6.3 API declarations.')
    if not isinstance(backend.get('version'), str) or not backend['version']:
        raise ValueError('Declare the installed MMseqs2 version; declaration is not a runtime version probe.')
    device = backend.get('device')
    if device not in ('cpu', 'gpu') or type(backend.get('gpuServer')) is not bool:
        raise ValueError('Declare device cpu/gpu and boolean gpuServer.')
    if backend.get('databaseFormat') not in ('colabfold-expandable', 'colabfold-gpu'):
        raise ValueError('Generic padded sequence databases do not replace the ColabFold expandable database set.')
    if device == 'gpu' and backend['databaseFormat'] != 'colabfold-gpu':
        raise ValueError('GPU search requires a ColabFold GPU-compatible expandable database set.')
    if backend['gpuServer']:
        raise ValueError('gpuServer=true is not managed by this pipeline: a resident server retains GPU memory after the search lease ends. Use direct GPU search (gpuServer=false).')
    if backend.get('pairingStrategy') not in ('none', 'native-uniprot') or backend.get('templateStrategy') not in ('none', 'native-af3'):
        raise ValueError('Declare pairingStrategy none/native-uniprot and templateStrategy none/native-af3 explicitly.')
    params, templates = profile.get('searchParameters', {}), profile.get('templateParameters', {})
    if not isinstance(params, dict) or set(params) - CF_FLAGS - NATIVE_FLAGS:
        raise ValueError('Unsupported searchParameters; only documented adapter parameters are accepted.')
    if not isinstance(templates, dict) or set(templates) - {'max_template_date'}:
        raise ValueError('Only max_template_date is supported in templateParameters.')
    for key, value in params.items():
        if key in {'threads', 'max_accept', 'jackhmmer_n_cpu'}:
            _positive_int(value, key)
        elif key in {'jackhmmer_max_parallel_shards', 'uniprot_cluster_annot_z_value'}:
            _positive_int(value, key, allow_none=True)
        elif key in {'use_env', 'filter', 'db_load_mode', 'prefilter_mode', 'align_eval', 'diff'}:
            if type(value) not in (int, bool) or value < 0:
                raise ValueError(key + ' must be a nonnegative integer.')
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(key + ' must be a finite number.')
    if params.get('use_env', 1) not in (0, 1) or params.get('filter', 1) not in (0, 1, 2) or params.get('db_load_mode', 2) not in (0, 1, 2, 3) or params.get('prefilter_mode', 0) not in (0, 1, 2):
        raise ValueError('Invalid ColabFold switch value.')
    if device == 'gpu' and ('s' in params or ('prefilter_mode' in params and params['prefilter_mode'] != 1)):
        raise ValueError('GPU gapless search fixes prefilter_mode=1 and does not honor -s; omit -s.')
    paths, executables = profile.get('paths', {}), profile.get('executables', {})
    if not isinstance(paths, dict) or not isinstance(executables, dict):
        raise ValueError('paths and executables must be objects.')
    _required(paths, 'uniref30_db')
    if params.get('use_env', 1):
        _required(paths, 'environment_db')
    for key in ('colabfold_search_binary', 'mmseqs_binary'):
        _required(executables, key)
    if backend['pairingStrategy'] != 'none' or backend['templateStrategy'] != 'none':
        _required(executables, 'af3_python')
    if backend['pairingStrategy'] == 'native-uniprot':
        _required(executables, 'jackhmmer_binary_path')
        database = _required(paths, 'uniprot_cluster_annot_database_path')
        if '@' in database and params.get('uniprot_cluster_annot_z_value') is None:
            raise ValueError('Sharded UniProt requires the total uniprot_cluster_annot_z_value.')
    if backend['templateStrategy'] == 'native-af3':
        for key in ('hmmsearch_binary_path', 'hmmbuild_binary_path'):
            _required(executables, key)
        for key in ('seqres_database_path', 'pdb_database_path'):
            _required(paths, key)
        try:
            datetime.date.fromisoformat(templates['max_template_date'])
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError('native-af3 templates require explicit max_template_date YYYY-MM-DD.') from exc
    return backend


def build_search_command(profile, fasta, search_dir):
    backend = validate_profile(profile)
    paths, executables = profile['paths'], profile['executables']
    params = dict(profile.get('searchParameters', {}))
    root = profile.get('root') or str(Path(paths['uniref30_db']).parent)
    command = [executables['colabfold_search_binary'], str(fasta), root, str(search_dir),
               '--mmseqs', executables['mmseqs_binary'], '--db1', paths['uniref30_db'],
               '--pair-mode', 'unpaired', '--use-env-pairing', '0', '--use-templates', '0',
               '--unpack', '1', '--gpu', '1' if backend['device'] == 'gpu' else '0',
               '--gpu-server', str(int(backend['gpuServer']))]
    params.setdefault('use_env', 1)
    params.setdefault('threads', 8)
    params.setdefault('db_load_mode', 2)
    if backend['device'] == 'gpu':
        params['prefilter_mode'] = 1
    if params['use_env']:
        command.extend(['--db3', paths['environment_db']])
    for key in sorted(CF_FLAGS & params.keys()):
        command.extend(['-s' if key == 's' else '--' + key.replace('_', '-'), str(int(params[key])) if isinstance(params[key], bool) else str(params[key])])
    return command


def _stop_group(process, managed=False):
    # Children have their own session. The CLI signal handler keeps this parent
    # alive long enough to stop CF's MMseqs grandchildren as well.
    if managed:
        # The engine owns this group and cleans *all* descendants, including
        # after a wrapper hard exit. Never signal a child PID as a process group.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_command(command, log_path, *, env, timeout, cancelled=lambda: False):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be a positive finite number of seconds.')
    _check(cancelled)
    managed = env.get('PROTEIN_STUDIO_MANAGED_PROCESS_GROUP') == '1'
    descriptors = ()
    if managed:
        try:
            raw = json.loads(env.get('PROTEIN_STUDIO_GPU_LOCK_FDS_JSON', '[]'))
            if not isinstance(raw, list) or len(raw) > 256 or any(type(fd) is not int or fd < 3 for fd in raw) or len(set(raw)) != len(raw):
                raise ValueError('invalid descriptors')
            for fd in raw:
                os.fstat(fd)
            descriptors = tuple(raw)
        except (ValueError, OSError, TypeError) as exc:
            raise ValueError('Invalid inherited GPU lock descriptor list.') from exc
    started = time.monotonic()
    with Path(log_path).open('wb') as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, env=env, shell=False,
                                   start_new_session=not managed, pass_fds=descriptors)
        try:
            while process.poll() is None:
                _check(cancelled)
                if time.monotonic() - started > timeout:
                    raise TimeoutError(f'Local search exceeded {timeout:g} seconds; see {log_path}.')
                time.sleep(.05)
            _check(cancelled)
            if process.returncode:
                raise RuntimeError(f'Local tool exited {process.returncode}; see {log_path}.')
        except BaseException:
            _stop_group(process, managed=managed)
            raise
    return time.monotonic() - started


def _cpu_env():
    return {**os.environ, 'CUDA_VISIBLE_DEVICES': '', 'JAX_PLATFORMS': 'cpu'}


def _state(data, profile, output):
    state = _json(output / STATE_NAME)
    if state.get('schemaVersion') != 1 or state.get('adapterVersion') != ADAPTER_VERSION or state.get('inputSha256') != _identity(data) or state.get('profileSha256') != _identity(profile):
        raise ValueError('Search state does not belong to this input and profile; re-run search in a fresh attempt.')
    a3m = _read(output / 'mmseqs-search' / 'protein.a3m')
    if hashlib.sha256(a3m.encode()).hexdigest() != state.get('unpairedSha256'):
        raise ValueError('Intermediate unpaired A3M changed after search.')
    return state, a3m


def search(data, profile, output, *, timeout=86400, cancelled=lambda: False):
    protein, backend = validate_input(data), validate_profile(profile)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    search_dir = output / 'mmseqs-search'
    if search_dir.exists() or (output / STATE_NAME).exists() or (output / DATA_NAME).exists():
        raise ValueError('Search requires a fresh output directory; stale MMseqs intermediates must not be reused implicitly.')
    if backend['device'] == 'gpu':
        visible = os.environ.get('CUDA_VISIBLE_DEVICES', '').strip()
        if not visible or visible == '-1':
            raise ValueError('GPU search requires scheduler-assigned CUDA_VISIBLE_DEVICES; no implicit GPU 0 fallback.')
        environment = os.environ.copy()
    else:
        environment = _cpu_env()
    _check(cancelled)
    fasta = output / 'mmseqs-query.fasta'
    fasta.write_text('>protein\n' + protein['sequence'] + '\n', encoding='utf-8')
    command = build_search_command(profile, fasta, search_dir)
    seconds = run_command(command, output / 'mmseqs-search.log', env=environment,
                          timeout=timeout, cancelled=cancelled)
    a3m = _read(search_dir / 'protein.a3m')
    quality = summarize_a3m(a3m, protein['sequence'], 'unpaired', cancelled)
    if not quality['queryPresent']:
        raise ValueError('ColabFold produced an empty result rather than a query-bearing A3M.')
    _check(cancelled)
    state = dict(schemaVersion=1, adapterVersion=ADAPTER_VERSION, inputSha256=_identity(data),
                 profileSha256=_identity(profile), unpairedSha256=quality['sourceSha256'],
                 unpairedUtf8Bytes=quality['sourceUtf8Bytes'], quality=quality,
                 searchCommand=command, searchWallSeconds=seconds,
                 searchBackend=backend, assignedGpuDevices=environment.get('CUDA_VISIBLE_DEVICES', ''),
                 sources=SOURCES)
    _atomic_json(output / STATE_NAME, state)
    return state


def native_enrich(data, profile, unpaired):
    """Run in the administrator's installed AF3 v3.0.4 Python environment.

    The APIs below reproduce the native UniProt candidate settings and native
    template filter settings; the template profile is built from the *new CF
    unpaired MSA*, so results need not equal the original AF3 database pipeline.
    """
    protein, backend = validate_input(data), validate_profile(profile)
    from alphafold3.constants import mmcif_names
    from alphafold3.data import msa, msa_config, structure_stores
    from alphafold3.data import templates as templates_lib

    params, paths, tools = profile.get('searchParameters', {}), profile['paths'], profile['executables']
    sequence = protein['sequence']
    result = dict(pairedMsa='', templates=[], timings={})
    if backend['pairingStrategy'] == 'native-uniprot':
        started = time.monotonic()
        z_value = params.get('uniprot_cluster_annot_z_value')
        config = msa_config.RunConfig(config=msa_config.JackhmmerConfig(
            binary_path=tools['jackhmmer_binary_path'],
            database_config=msa_config.DatabaseConfig(name='uniprot_cluster_annot', path=paths['uniprot_cluster_annot_database_path']),
            n_cpu=params.get('jackhmmer_n_cpu', 8), n_iter=1, e_value=1e-4,
            z_value=z_value, dom_z_value=z_value, max_sequences=50_000,
            max_parallel_shards=params.get('jackhmmer_max_parallel_shards')),
            chain_poly_type=mmcif_names.PROTEIN_CHAIN, crop_size=None)
        result['pairedMsa'] = msa.get_msa(target_sequence=sequence, run_config=config,
                                         chain_poly_type=mmcif_names.PROTEIN_CHAIN,
                                         deduplicate=False).to_a3m()
        result['timings']['nativePairingWallSeconds'] = time.monotonic() - started
    if backend['templateStrategy'] == 'native-af3':
        started = time.monotonic()
        cutoff = datetime.date.fromisoformat(profile['templateParameters']['max_template_date'])
        tool_config = msa_config.HmmsearchConfig(hmmsearch_binary_path=tools['hmmsearch_binary_path'],
            hmmbuild_binary_path=tools['hmmbuild_binary_path'], filter_f1=.1, filter_f2=.1,
            filter_f3=.1, e_value=100, inc_e=100, dom_e=100, incdom_e=100, alphabet='amino')
        filters = msa_config.TemplateFilterConfig(max_subsequence_ratio=.95, min_align_ratio=.1,
            min_hit_length=10, deduplicate_sequences=True, max_hits=4, max_template_date=cutoff)
        hits = templates_lib.Templates.from_seq_and_a3m(query_sequence=sequence, msa_a3m=unpaired,
            max_template_date=cutoff, database_path=paths['seqres_database_path'],
            hmmsearch_config=tool_config, max_a3m_query_sequences=None,
            chain_poly_type=mmcif_names.PROTEIN_CHAIN,
            structure_store=structure_stores.StructureStore(paths['pdb_database_path']), filter_config=filters)
        for hit, structure in hits.get_hits_with_structures():
            mapping = hit.query_to_hit_mapping
            result['templates'].append(dict(mmcif=structure.to_mmcif(),
                queryIndices=list(mapping.keys()), templateIndices=list(mapping.values())))
        result['timings']['nativeTemplateWallSeconds'] = time.monotonic() - started
    return result


def _validate_enrichment(value, sequence, backend):
    if not isinstance(value.get('pairedMsa'), str) or not isinstance(value.get('templates'), list):
        raise ValueError('Native enrichment did not produce pairedMsa and templates.')
    quality = summarize_a3m(value['pairedMsa'], sequence, 'pairing-candidates')
    if backend['pairingStrategy'] == 'none' and value['pairedMsa']:
        raise ValueError('None pairing strategy unexpectedly produced candidates.')
    if backend['pairingStrategy'] == 'native-uniprot' and not quality['queryPresent']:
        raise ValueError('Native UniProt search returned no query-bearing MSA.')
    if backend['templateStrategy'] == 'none' and value['templates']:
        raise ValueError('None template strategy unexpectedly produced templates.')
    for template in value['templates']:
        if not isinstance(template, dict) or set(template) != {'mmcif', 'queryIndices', 'templateIndices'} or not isinstance(template['mmcif'], str) or not template['mmcif'].strip():
            raise ValueError('Templates must contain inline mmCIF and residue mappings.')
        query, target = template['queryIndices'], template['templateIndices']
        if (not isinstance(query, list) or not isinstance(target, list) or len(query) != len(target)
            or any(type(i) is not int or not 0 <= i < len(sequence) for i in query)
            or any(type(i) is not int or i < 0 for i in target)
            or len(set(query)) != len(query) or len(set(target)) != len(target)):
            raise ValueError('Template query/template residue mapping is invalid.')
    return quality


def finalize(data, profile, output, *, input_path, profile_path, timeout=86400, cancelled=lambda: False):
    protein, backend = validate_input(data), validate_profile(profile)
    output = Path(output).resolve()
    _check(cancelled)
    state, unpaired = _state(data, profile, output)
    if (output / DATA_NAME).exists():
        raise ValueError('A final prepared input already exists; do not overwrite a published result.')
    started = time.monotonic()
    enrichment = dict(pairedMsa='', templates=[], timings={})
    if backend['pairingStrategy'] != 'none' or backend['templateStrategy'] != 'none':
        command = [profile['executables']['af3_python'], str(Path(__file__).resolve()),
                   '--phase', 'native', '--input', str(Path(input_path).resolve()),
                   '--profile-json', str(Path(profile_path).resolve()), '--output-dir', str(output)]
        run_command(command, output / 'af3-native-enrichment.log', env=_cpu_env(),
                    timeout=timeout, cancelled=cancelled)
        enrichment = _json(output / 'native-enrichment.json')
    paired_quality = _validate_enrichment(enrichment, protein['sequence'], backend)
    prepared = {**data, 'sequences': [{'protein': {**protein, 'unpairedMsa': unpaired,
                                                 'pairedMsa': enrichment['pairedMsa'],
                                                 'templates': enrichment['templates']}}]}
    provenance = dict(schemaVersion=1, adapterVersion=ADAPTER_VERSION, searchBackend=backend,
        af3ApiTargetVersion=AF3_VERSION if backend['pairingStrategy'] != 'none' or backend['templateStrategy'] != 'none' else None,
        inputSha256=state['inputSha256'], profileSha256=state['profileSha256'],
        unpairedSource='ColabFold local UniRef30' + (' + environment' if profile.get('searchParameters', {}).get('use_env', 1) else ''),
        pairedSource='native AF3 UniProt raw pairing candidates' if backend['pairingStrategy'] == 'native-uniprot' else 'explicit empty MSA',
        templateSource='native AF3 template search using ColabFold unpaired MSA' if backend['templateStrategy'] == 'native-af3' else 'explicit empty templates',
        finalPairedRows=None, scientificEquivalenceToNativeAf3=False,
        timings={**enrichment.get('timings', {}), 'colabfoldSearchWallSeconds': state['searchWallSeconds'],
                 'finalizeWallSeconds': time.monotonic() - started},
        msaQuality=dict(schemaVersion=1, unpaired=state['quality'], paired=paired_quality), sources=SOURCES)
    _check(cancelled)
    _atomic_json(output / 'msa-search-provenance.json', provenance)
    _check(cancelled)
    _atomic_json(output / DATA_NAME, prepared)
    return prepared


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('search', 'finalize', 'native'), required=True,
                        help='native is the internal CPU helper in the installed AF3 Python environment.')
    parser.add_argument('--input', required=True)
    parser.add_argument('--profile-json', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--timeout-seconds', type=float, default=86400)
    args = parser.parse_args(argv)
    stopped = threading.Event()
    previous = {}
    # Internal native worker is itself monitored by finalize's process-group
    # cancellation. Leave its SIGTERM default so HMMER descendants are killed.
    if args.phase != 'native':
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stopped.set())
    try:
        data, profile = _json(args.input), _json(args.profile_json)
        validate_input(data)
        validate_profile(profile)
        output = Path(args.output_dir).resolve()
        if args.phase == 'search':
            search(data, profile, output, timeout=args.timeout_seconds, cancelled=stopped.is_set)
        elif args.phase == 'finalize':
            finalize(data, profile, output, input_path=args.input, profile_path=args.profile_json,
                     timeout=args.timeout_seconds, cancelled=stopped.is_set)
        else:
            os.environ.update({'CUDA_VISIBLE_DEVICES': '', 'JAX_PLATFORMS': 'cpu'})
            _, unpaired = _state(data, profile, output)
            result = native_enrich(data, profile, unpaired)
            _validate_enrichment(result, validate_input(data)['sequence'], profile['searchBackend'])
            _atomic_json(output / 'native-enrichment.json', result)
        return 0
    except (ValueError, RuntimeError, TimeoutError, OSError) as exc:
        print(f'MSA preparation failed: {exc}', file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
