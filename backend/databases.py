"""Administrator-owned local database registry and bounded metadata preflight.

No downloads, remote URLs, recursive scans, or whole-database hashes are used.
The fingerprint combines declared releases/settings with explicit asset metadata;
it is not a content-integrity checksum of a multi-terabyte database.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import shutil

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / 'config' / 'databases.example.json'
REFERENCE = dict(engine='reference', release='bundled-public-fixtures', root=None,
                 prepareVersion='reference-replay-v1', searchVersion='not-applicable',
                 assets=[], searchParameters={}, templateParameters={}, sourceURLs=[])
AF3_ASSETS = {
    'uniref90_database_path': 'uniref90_2022_05.fa',
    'mgnify_database_path': 'mgy_clusters_2022_05.fa',
    'small_bfd_database_path': 'bfd-first_non_consensus_sequences.fasta',
    'uniprot_cluster_annot_database_path': 'uniprot_all_2021_04.fa',
    'seqres_database_path': 'pdb_seqres_2022_09_28.fasta',
    'pdb_database_path': 'mmcif_files',
    'ntrna_database_path': 'nt_rna_2023_02_23_clust_seq_id_90_cov_80_rep_seq.fasta',
    'rfam_database_path': 'rfam_14_9_clust_seq_id_90_cov_80_rep_seq.fasta',
    'rna_central_database_path': 'rnacentral_active_seq_id_90_cov_80_linclust.fasta',
}
AF3_TOOLS = ('jackhmmer_binary_path', 'nhmmer_binary_path', 'hmmalign_binary_path',
             'hmmsearch_binary_path', 'hmmbuild_binary_path')


def _af3_inventory(assets, executables):
    """Bounded plausibility check for the official unsharded v3.0 snapshot.

    These are conservative stub-detection thresholds, not official file sizes
    or a cryptographic completeness check. The explicit probe adds runtime and
    optional real CPU preparation checks.
    """
    diagnostics, inspections = [], []
    indexed = {asset['name']: asset for asset in assets}
    for key in AF3_ASSETS:
        asset = indexed.get(key)
        if not asset or not asset['required']:
            diagnostics.append(dict(severity='error',code='af3-missing-asset',asset=key,
                message=f'AF3 v3.0.4 requires the declared nonempty {key}, including RNA database paths for protein input.'))
            continue
        expected = 'directory' if key == 'pdb_database_path' else 'file'
        if asset['type'] != expected:
            diagnostics.append(dict(severity='error',code='af3-asset-layout',asset=key,
                message='This pinned profile requires the official unsharded FASTA files and flat mmcif_files directory.'))
            continue
        if not asset['ready']:
            continue
        path = Path(asset['resolvedPath'])
        try:
            if expected == 'file':
                with path.open('rb') as handle:
                    prefix = handle.read(4096)
                if path.stat().st_size < 1024*1024:
                    raise ValueError('File is below the conservative 1 MiB stub-detection floor; this is not an official-size check.')
                text = prefix.decode('ascii')
                lines = text.splitlines()
                if not lines or not lines[0].startswith('>') or not any(re.fullmatch('[A-Za-z*.-]+', line) for line in lines[1:] if line):
                    raise ValueError('File does not begin with a readable uncompressed FASTA header and sequence.')
                inspections.append(dict(asset=key,inspection='FASTA prefix + minimum-size plausibility',bytesRead=len(prefix)))
            else:
                samples=[]
                with os.scandir(path) as entries:
                    for index, entry in enumerate(entries):
                        if index >= 256 or len(samples) >= 8:
                            break
                        if re.fullmatch('[a-z0-9]{4,}\\.cif',entry.name) and entry.is_file():
                            candidate=Path(entry.path)
                            if candidate.stat().st_size < 128:
                                raise ValueError('mmCIF sample is empty or too small to hold a structure: '+entry.name)
                            with candidate.open('rb') as handle:
                                prefix=handle.read(4096)
                            if not re.search(rb'(?m)^data_\S+',prefix):
                                raise ValueError('mmCIF sample has no data_ block: '+entry.name)
                            samples.append(entry.name)
                if len(samples)<8:
                    raise ValueError('Expected a flat directory with many readable PDB_ID.cif files; fewer than 8 were found in the bounded sample.')
                inspections.append(dict(asset=key,inspection='bounded flat-directory/mmCIF header sample; not full inventory',samples=samples))
        except (OSError, UnicodeError, ValueError) as exc:
            diagnostics.append(dict(severity='error',code='af3-invalid-content',asset=key,path=str(path),message=str(exc)))
    for key in AF3_TOOLS:
        if key not in executables:
            diagnostics.append(dict(severity='error',code='af3-missing-tool',field=key,
                message=f'Declare installed {key} in executables; run af3_database_probe.py to verify its version and behavior.'))
    return diagnostics, inspections


MMSEQS_TOOLS = ('colabfold_search_binary', 'mmseqs_binary', 'af3_python')


def _search_backend(raw, engine):
    backend = raw.get('searchBackend')
    if backend is None:
        return None
    fields = {'id', 'device', 'version', 'colabfoldVersion', 'adapterVersion', 'databaseFormat',
              'gpuServer', 'pairingStrategy', 'templateStrategy'}
    if not isinstance(backend, dict) or set(backend) != fields or engine != 'alphafold3':
        raise ValueError('searchBackend must explicitly declare the supported AF3 MMseqs search identity and policies.')
    if backend['id'] != 'mmseqs2-colabfold' or backend['adapterVersion'] != 'mmseqs-af3-v1':
        raise ValueError('Unknown MMseqs AF3 adapter identity.')
    if backend['device'] not in {'cpu', 'gpu'} or type(backend['gpuServer']) is not bool:
        raise ValueError('MMseqs device must be cpu/gpu and gpuServer must be boolean.')
    expected_format = 'colabfold-gpu' if backend['device'] == 'gpu' else 'colabfold-expandable'
    if backend['databaseFormat'] != expected_format:
        raise ValueError('MMseqs databaseFormat must explicitly match the CPU or GPU database preparation.')
    if backend['gpuServer'] and backend['device'] != 'gpu':
        raise ValueError('MMseqs gpuServer requires a GPU search profile.')
    if backend['pairingStrategy'] not in {'none', 'native-uniprot'} or backend['templateStrategy'] not in {'none', 'native-af3'}:
        raise ValueError('MMseqs AF3 pairing/template policies must be explicit supported strategies.')
    if backend['colabfoldVersion'] != '1.6.3':
        raise ValueError('This MMseqs adapter is pinned to ColabFold 1.6.3 APIs.')
    for field in ('version', 'colabfoldVersion'):
        if not isinstance(backend[field], str) or not backend[field].strip() or len(backend[field]) > 128:
            raise ValueError('Declare the installed search backend version: ' + field)
    return copy.deepcopy(backend)


def _commands(raw, backend, diagnostics):
    commands, ready = {}, True
    for name in ('prepareCommand', 'finalizeCommand'):
        value = raw.get(name)
        if value is None:
            if backend:
                ready = False
                diagnostics.append(dict(severity='error', code='mmseqs-missing-command', field=name,
                                        message=f'Declare administrator {name} for the two-phase MMseqs adapter.'))
            continue
        if (not isinstance(value, list) or not value or len(value) > 128
            or any(not isinstance(x, str) or not x or '\0' in x for x in value)):
            raise ValueError(name + ' must be a nonempty bounded argv string array.')
        commands[name] = list(value)
        executable = shutil.which(value[0])
        missing_scripts = [x for x in value[1:] if x.endswith('.py') and '{' not in x and Path(x).is_absolute() and not Path(x).is_file()]
        if executable is None or missing_scripts:
            ready = False
            diagnostics.append(dict(severity='error', code='runner-not-installed', field=name,
                                    message=f'{name}: executable or explicit Python script is not installed.'))
    return commands, ready


def _mmseqs_inventory(backend, assets, executables, parameters):
    diagnostics = []
    if backend['device'] == 'gpu' and ('s' in parameters or parameters.get('prefilter_mode', 1) != 1):
        diagnostics.append(dict(severity='error', code='mmseqs-gpu-parameters',
            message='GPU gapless search uses prefilter_mode=1 and does not honor sensitivity -s; omit s.'))
    if backend['gpuServer']:
        diagnostics.append(dict(severity='error', code='unmanaged-gpu-server',
            message='gpuServer=true is not supported by this adapter: a query lease does not cover an external persistent GPU server lifetime. Use direct GPU search.'))
    indexed = {asset['name']: asset for asset in assets}
    required = {'uniref30_db': 'prefix'}
    if parameters.get('use_env', 1):
        required['environment_db'] = 'prefix'
    tools = {'colabfold_search_binary', 'mmseqs_binary'}
    if backend['pairingStrategy'] == 'native-uniprot':
        required['uniprot_cluster_annot_database_path'] = 'file'
        tools.update(('af3_python', 'jackhmmer_binary_path'))
    if backend['templateStrategy'] == 'native-af3':
        required.update(seqres_database_path='file', pdb_database_path='directory')
        tools.update(('af3_python', 'hmmsearch_binary_path', 'hmmbuild_binary_path'))
    for key, kind in required.items():
        asset = indexed.get(key)
        if not asset or not asset['required'] or asset['type'] != kind:
            diagnostics.append(dict(severity='error', code='mmseqs-missing-asset', asset=key,
                                    message=f'{key} must be a required {kind} asset for this search strategy.'))
        elif kind == 'prefix':
            minimum = {'', '.dbtype', '.index', '_seq', '_seq.dbtype', '_seq.index', '_aln', '_aln.dbtype', '_aln.index'}
            if not minimum.issubset(asset['requiredSuffixes']):
                diagnostics.append(dict(severity='error', code='mmseqs-incomplete-family', asset=key,
                    message='Declare the expandable ColabFold database family members; a single FASTA or prefix is insufficient.'))
    for tool in sorted(tools - set(executables)):
        diagnostics.append(dict(severity='error', code='mmseqs-missing-tool', field=tool,
                                message='Declare the installed executable path: ' + tool))
    for field in ('version', 'colabfoldVersion'):
        if 'REPLACE' in backend[field].upper():
            diagnostics.append(dict(severity='error', code='undeclared-version', field='searchBackend.'+field,
                                    message='Declare the actual installed ' + field + ' rather than an example placeholder.'))
    return diagnostics


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _load(config=None):
    if isinstance(config, dict):
        data, base, location = copy.deepcopy(config), ROOT, 'in-memory administrator configuration'
    else:
        path = Path(config or os.environ.get('PROTEIN_STUDIO_DATABASE_CONFIG') or DEFAULT_CONFIG).expanduser().resolve()
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('Database registry exceeds 2 MiB.')
        with path.open(encoding='utf-8') as handle:
            data = json.load(handle)
        base, location = path.parent, str(path)
    if not isinstance(data, dict) or data.get('schemaVersion', 1) != 1:
        raise ValueError('Database registry must use schemaVersion 1.')
    profiles = data.get('profiles')
    if not isinstance(profiles, dict) or len(profiles) > 128:
        raise ValueError('Database registry profiles must be an object with at most 128 entries.')
    profiles = dict(profiles)
    profiles.setdefault('reference', copy.deepcopy(REFERENCE))
    return profiles, base, location


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError(f'{label} must be a non-empty string.')
    return value


def _path(value, base):
    value = _text(value, 'Asset path')
    if '://' in value:
        raise ValueError('Database assets must use local filesystem paths, not URLs.')
    candidate = Path(value).expanduser()
    return (candidate if candidate.is_absolute() else base / candidate).resolve()


def _check_path(path, expected):
    result = dict(path=str(path), expectedType=expected, ready=False)
    try:
        information = path.stat()
        result.update(size=information.st_size, mtimeNs=information.st_mtime_ns)
        if expected == 'file':
            if not stat.S_ISREG(information.st_mode):
                result['status'] = 'wrong-type'
            elif information.st_size == 0:
                result['status'] = 'empty'
            else:
                with path.open('rb') as handle:
                    handle.read(1)
                result.update(ready=True, status='ready')
        elif expected == 'directory':
            if not stat.S_ISDIR(information.st_mode):
                result['status'] = 'wrong-type'
            else:
                # One directory entry establishes non-emptiness; do not scan
                # thousands of mmCIF records or calculate a recursive hash.
                with os.scandir(path) as entries:
                    first = next(entries, None)
                result.update(ready=first is not None, status='ready' if first is not None else 'empty')
                result['inspection'] = 'non-recursive-directory-entry-check'
    except FileNotFoundError:
        result['status'] = 'missing'
    except PermissionError:
        result['status'] = 'unreadable'
    except OSError as exc:
        result.update(status='unreadable', detail=str(exc))
    return result


def _asset(asset, root):
    if not isinstance(asset, dict):
        raise ValueError('Each database asset must be an object.')
    name = _text(asset.get('name'), 'Asset name')
    if not re.fullmatch('[A-Za-z][A-Za-z0-9_]*', name):
        raise ValueError('Asset names must be valid named command placeholders.')
    kind = asset.get('kind')
    if kind not in ('sequence', 'template'):
        raise ValueError(f'Asset {name}: kind must be sequence or template.')
    expected = asset.get('type', 'file')
    if expected not in ('file', 'directory', 'prefix'):
        raise ValueError(f'Asset {name}: type must be file, directory, or prefix.')
    required = asset.get('required', True)
    if not isinstance(required, bool):
        raise ValueError(f'Asset {name}: required must be boolean.')
    path = _path(asset.get('path'), root)
    if expected == 'prefix':
        suffixes = asset.get('requiredSuffixes')
        if not isinstance(suffixes, list) or not suffixes or len(suffixes) > 1024:
            raise ValueError(f'Asset {name}: prefix needs explicit requiredSuffixes.')
        if any(not isinstance(s, str) or '/' in s or '\\' in s or '\x00' in s for s in suffixes) or len(set(suffixes)) != len(suffixes):
            raise ValueError(f'Asset {name}: requiredSuffixes must be unique filename suffixes.')
        checks = [_check_path(Path(str(path) + suffix), 'file') for suffix in suffixes]
    else:
        suffixes = []
        checks = [_check_path(path, expected)]
    ready = all(check['ready'] for check in checks)
    return dict(name=name, kind=kind, type=expected, path=asset['path'], resolvedPath=str(path),
                required=required, requiredSuffixes=suffixes, release=asset.get('release'),
                declaredSha256=asset.get('sha256'), ready=ready,
                status='ready' if ready else next(check['status'] for check in checks if not check['ready']),
                checks=checks)


def _inspect(profile_id, raw, base, location):
    if not isinstance(profile_id, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', profile_id):
        raise ValueError('Database profile IDs must contain letters, numbers, dots, underscores, or hyphens.')
    if not isinstance(raw, dict):
        raise ValueError(f'Profile {profile_id} must be an object.')
    engine = raw.get('engine')
    if engine not in ('reference', 'alphafold2', 'alphafold3', 'colabfold'):
        raise ValueError(f'Profile {profile_id}: unsupported engine.')
    assets_raw = raw.get('assets', [])
    if not isinstance(assets_raw, list) or len(assets_raw) > 1024:
        raise ValueError('Profile assets must be a list with at most 1024 entries.')
    if engine == 'reference' and assets_raw:
        raise ValueError('Reference replay must not require a sequence database.')
    root = _path(raw['root'], base) if raw.get('root') else base
    assets = sorted((_asset(asset, root) for asset in assets_raw), key=lambda item: item['name'])
    if len({asset['name'] for asset in assets}) != len(assets):
        raise ValueError(f'Profile {profile_id}: asset names must be unique.')
    validation = raw.get('validationProfile')
    backend = _search_backend(raw, engine)
    if validation is not None and (validation not in {'af3-v3.0.4', 'af3-mmseqs-v1'} or engine != 'alphafold3'):
        raise ValueError('Supported strict validationProfiles are af3-v3.0.4 and af3-mmseqs-v1 for alphafold3.')
    if (backend is not None) != (validation == 'af3-mmseqs-v1'):
        raise ValueError('MMseqs searchBackend requires validationProfile=af3-mmseqs-v1, with explicit adapter policies.')
    tools_raw = raw.get('executables', {})
    if not isinstance(tools_raw, dict) or any(key not in AF3_TOOLS + MMSEQS_TOOLS for key in tools_raw):
        raise ValueError('executables must map supported AF3/MMseqs tool names to administrator-owned local paths.')
    executables, executable_checks = {}, []
    for name, value in sorted(tools_raw.items()):
        path = _path(value, base)
        check = _check_path(path, 'file')
        check['name'] = name
        if check['ready'] and not os.access(path, os.X_OK):
            check.update(ready=False, status='not-executable')
        executable_checks.append(check)
        executables[name] = str(path)
    parameters = {}
    for name in ('searchParameters', 'templateParameters'):
        parameters[name] = raw.get(name, {})
        if not isinstance(parameters[name], dict):
            raise ValueError(f'Profile {profile_id}: {name} must be an object.')
    sources = raw.get('sourceURLs', [])
    if not isinstance(sources, list) or any(not isinstance(url, str) or not url.startswith('https://') for url in sources):
        raise ValueError('sourceURLs must be HTTPS references; they are not fetched during preflight.')
    diagnostics = []
    commands, runner_ready = _commands(raw, backend, diagnostics)
    for check in executable_checks:
        if not check['ready']:
            diagnostics.append(dict(severity='error',code=check['status'],field=check['name'],path=check['path'],
                                    message=f"{check['name']}: {check['status']} ({check['path']})"))
    inspections = []
    if validation == 'af3-v3.0.4':
        extra, inspections = _af3_inventory(assets, executables)
        diagnostics.extend(extra)
    if backend:
        diagnostics.extend(_mmseqs_inventory(backend, assets, executables, parameters['searchParameters']))
    for name in ('release', 'prepareVersion', 'searchVersion'):
        value = raw.get(name)
        if engine != 'reference' and (not isinstance(value, str) or not value.strip() or 'REPLACE' in value.upper()):
            diagnostics.append(dict(severity='error', code='undeclared-version', field=name,
                                    message=f'Declare the actual {name} before using preparation caches.'))
    if engine != 'reference' and not any(a['required'] for a in assets):
        diagnostics.append(dict(severity='error', code='no-required-assets', message='Declare the databases and necessary index files used by this preparation profile.'))
    if raw.get('example', False):
        diagnostics.append(dict(severity='error', code='example-configuration', message='This is an example profile. Set real paths/versions and remove example=true after configuring the installation.'))
    for asset in assets:
        for check in asset['checks']:
            if not check['ready']:
                diagnostics.append(dict(severity='error' if asset['required'] else 'warning',
                                        code=check['status'], asset=asset['name'], path=check['path'],
                                        message=f"{asset['name']}: {check['status']} ({check['path']})"))
    # Explicit file members and directory metadata detect common replacements.
    # Dataset release declarations remain essential: no deep content check is
    # attempted, and a nested file edit may not change its parent directory.
    quick_manifest = [{key: asset[key] for key in ('name', 'kind', 'type', 'required', 'resolvedPath', 'requiredSuffixes', 'release', 'declaredSha256', 'checks')}
                      for asset in assets]
    identity = dict(schemaVersion=1, engine=engine, release=raw.get('release'),
                    prepareVersion=raw.get('prepareVersion'), searchVersion=raw.get('searchVersion'),
                    searchParameters=parameters['searchParameters'], templateParameters=parameters['templateParameters'],
                    root=str(root) if engine != 'reference' else None, assets=quick_manifest)
    # Keep existing reference/generic profile identities stable. New strict
    # checks and tool identity only enter profiles that explicitly configure them.
    if validation:
        identity['validationProfile'] = validation
    if executable_checks:
        identity['executables'] = executable_checks
    if backend:
        identity['searchBackend'] = backend
    identity.update(commands)
    fingerprint = hashlib.sha256(_canonical(identity).encode('utf-8')).hexdigest()
    result = dict(id=profile_id, name=raw.get('name', profile_id), engine=engine,
                release=raw.get('release'), prepareVersion=raw.get('prepareVersion'),
                searchVersion=raw.get('searchVersion'), root=str(root) if engine != 'reference' else None,
                paths={asset['name']: asset['resolvedPath'] for asset in assets}, assets=assets,
                validationProfile=validation, executables=executables, executableChecks=executable_checks,
                contentInspections=inspections,
                **parameters, profileFingerprint=fingerprint, quickManifest=quick_manifest,
                ready=not any(d['severity'] == 'error' for d in diagnostics), diagnostics=diagnostics,
                sourceURLs=sources, configPath=location,
                verification='declared versions + explicit file metadata/readability; no recursive/content integrity verification',
                noDatabaseRequired=engine == 'reference')
    result.update(commands)
    if backend:
        result['searchBackend'] = backend
        result['preparationStrategy'] = dict(id=backend['adapterVersion'], searchBackend=backend['id'],
            searchDevice=backend['device'], pairedCandidateSource=backend['pairingStrategy'],
            templateStrategy=backend['templateStrategy'], databaseFormat=backend['databaseFormat'],
            status='preflight-ready' if result['ready'] else 'unavailable', runnerConfigured=runner_ready,
            gpuReservationRequired=backend['device'] == 'gpu', verification='metadata-and-path-preflight')
    return result


def list_profiles(config=None):
    """Return trusted registry profiles with fresh filesystem preflight results."""
    profiles, base, location = _load(config)
    return [_inspect(name, profiles[name], base, location) for name in sorted(profiles)]


def get_profile(profile_id, config=None):
    """Resolve a registered ID; unknown IDs raise KeyError, bad config ValueError."""
    profiles, base, location = _load(config)
    if not isinstance(profile_id, str) or profile_id not in profiles:
        raise KeyError(f'Unknown database profile: {profile_id}')
    return _inspect(profile_id, profiles[profile_id], base, location)


if __name__ == '__main__':
    print(json.dumps(list_profiles(), ensure_ascii=False, indent=2))
