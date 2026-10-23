"""Bounded confidence parsing and lossless-to-float64 PAE artifacts.

The untouched source JSON remains the scientific source of record. Binary PAE
uses the application's existing float(value) semantics, never float32 or color
quantization. JSON events come from ijson, not a handwritten JSON parser.
"""
from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import sys
import uuid
from urllib.parse import quote

try:
    import ijson
except ModuleNotFoundError as exc:
    if exc.name == 'ijson':
        raise RuntimeError('Missing ijson dependency. Run Protein Studio with its .venv Python, or install requirements.txt in the configured resident Python environment.') from exc
    raise

MAX_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_DIMENSION = 5000
MAX_CELLS = 25_000_000
INLINE_CELLS = 128 * 128
MAX_PREVIEW = 256
MAX_TILE = 128
HASH = re.compile(r'^[0-9a-f]{64}$')


class PAELimitError(ValueError):
    """An optional visualization artifact exceeds its explicit processing cap."""


def _check(cancelled):
    if cancelled():
        if __package__:
            from .engine import PredictionCancelled
        else:
            from engine import PredictionCancelled
        raise PredictionCancelled('Confidence processing cancelled.')


def _safe(root, raw, *, exists=True):
    root = Path(root).resolve()
    path = Path(raw)
    if '..' in path.parts:
        raise ValueError('Scientific artifact paths may not traverse parents.')
    if not path.is_absolute():
        path = root / path
    relative = path.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError('Scientific artifacts may not traverse symbolic links.')
    if exists and not path.is_file():
        raise ValueError('Scientific artifact is missing or not a regular file.')
    return path


def _stat(path):
    value = path.stat()
    return _stat_value(value)


def _stat_value(value):
    return dict(bytes=value.st_size, mtimeNs=value.st_mtime_ns, inode=value.st_ino, device=value.st_dev)


def _digest(path, cancelled=lambda: False):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            _check(cancelled)
            digest.update(block)
    return digest.hexdigest()


def _number(value):
    if isinstance(value, bool) or value is None or isinstance(value, str):
        raise ValueError('Confidence values must be JSON numbers.')
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Confidence values must be finite.')
    return number


class _Reader:
    def __init__(self, stream, cancelled):
        self.stream, self.cancelled = stream, cancelled
        self.digest, self.count = hashlib.sha256(), 0

    def read(self, size=-1):
        _check(self.cancelled)
        if size < 0:
            size = 64 * 1024
        block = self.stream.read(size)
        self.count += len(block)
        if self.count > MAX_SOURCE_BYTES:
            raise PAELimitError('Confidence JSON exceeds the 1 GiB source limit.')
        self.digest.update(block)
        return block


def _publish_file(temporary, destination):
    """Publish once without replacing an existing content-addressed file."""
    try:
        os.link(temporary, destination)
    except FileExistsError:
        if destination.is_symlink() or _digest(temporary) != _digest(destination):
            raise ValueError('Existing immutable scientific artifact differs from its content identity.')
    finally:
        temporary.unlink(missing_ok=True)


def _binary_descriptor(temporary, root, source, source_sha, source_stat, shape, axis_kind, axis_data, minimum, maximum, cancelled):
    sha = _digest(temporary, cancelled)
    directory = temporary.parent
    binary = directory / ('pae-' + sha + '.f64')
    _publish_file(temporary, binary)
    descriptor = dict(schemaVersion=1, status='available', rows=shape[0], cols=shape[1], unit='angstrom',
        axisKind=axis_kind, axisOrder='as-stored', dtype='float64', byteOrder='little', layout='row-major',
        storageRelativePath=binary.relative_to(root).as_posix(), storagePath=str(binary),
        bytes=binary.stat().st_size, sha256=sha, storageStat=_stat(binary),
        sourceRelativePath=source.relative_to(root).as_posix(), sourcePath=str(source),
        sourceBytes=source_stat['bytes'], sourceSha256=source_sha, sourceStat=source_stat,
        minimum=minimum, maximum=maximum)
    if axis_data:
        descriptor['axisMetadata'] = axis_data
    # The manifest describes scientific identity, independently of relocation.
    manifest_data = {key: value for key, value in descriptor.items()
                     if key not in {'storageRelativePath', 'sourceRelativePath', 'storagePath', 'sourcePath', 'storageStat', 'sourceStat'}}
    encoded = json.dumps(manifest_data, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    manifest = directory / ('pae-' + hashlib.sha256(encoded).hexdigest() + '.json')
    tmp = directory / ('.manifest-' + uuid.uuid4().hex)
    with tmp.open('xb') as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    _publish_file(tmp, manifest)
    descriptor.update(manifestRelativePath=manifest.relative_to(root).as_posix(), manifestPath=str(manifest))
    return descriptor


def stream_confidence(path, root=None, *, build_pae=False, axis_kind='unknown', cancelled=lambda: False):
    """Read AF3/AF2/ColabFold/AFDB JSON with bounded events and optional PAE.

    Scalar-only mode retains at most a 5,000-element pLDDT vector. Matrix mode
    writes buffered float64 rows directly to disk, checking square shape and all
    values. A one-object AFDB array is supported; ambiguous multiple objects or
    duplicate scientific fields are rejected.
    """
    path = Path(path).absolute()
    root = Path(root or path.parent).resolve()
    path = _safe(root, path)
    before = _stat(path)
    if before['bytes'] > MAX_SOURCE_BYTES:
        raise PAELimitError('Confidence JSON exceeds the 1 GiB source limit.')
    if axis_kind not in {'unknown', 'token', 'residue'}:
        raise ValueError('Unknown PAE axis kind.')
    result, vector, axes, seen = {}, [], {}, set()
    active_vectors = set()
    vector_overflow = False
    matrix_prefix, row_count, column_count, row_size = None, 0, None, None
    matrix_done, matrix_seen, root_type, objects = False, False, None, 0
    minimum, maximum = math.inf, -math.inf
    cells, buffer, temporary, target = 0, array('d'), None, None
    if build_pae:
        directory = _safe(root, '.result-artifacts', exists=False)
        directory.mkdir(exist_ok=True)
        temporary = directory / ('.pae-' + uuid.uuid4().hex)
        target = temporary.open('xb')
    def flush():
        if buffer:
            if sys.byteorder != 'little':
                buffer.byteswap()
            target.write(buffer.tobytes())
            del buffer[:]
    try:
        with path.open('rb') as source:
            reader = _Reader(source, cancelled)
            for event_index, (prefix, event, value) in enumerate(ijson.parse(reader, use_float=False, buf_size=64 * 1024)):
                if event_index % 4096 == 0:
                    _check(cancelled)
                if root_type is None:
                    if prefix or event not in {'start_map', 'start_array'}:
                        raise ValueError('Confidence JSON must be an object or one-object list.')
                    root_type = event
                if root_type == 'start_array' and prefix == 'item':
                    if event == 'start_map':
                        objects += 1
                        if objects > 1:
                            raise ValueError('Confidence array must contain exactly one object.')
                    elif event not in {'end_map', 'map_key'}:
                        raise ValueError('Confidence array must contain exactly one object.')
                local = prefix[5:] if root_type == 'start_array' and prefix.startswith('item.') else prefix
                base = 'item' if root_type == 'start_array' else ''
                if prefix == base and event == 'map_key' and value in {
                        'pae', 'predicted_aligned_error', 'plddt', 'ptm', 'iptm', 'ranking_score', 'token_chain_ids', 'token_res_ids'}:
                    if value in seen:
                        raise ValueError('Duplicate scientific confidence field: ' + value)
                    seen.add(value)
                if local in {'plddt', 'token_chain_ids', 'token_res_ids'} and local in seen:
                    if event == 'start_array':
                        active_vectors.add(local)
                    elif event == 'end_array':
                        active_vectors.discard(local)
                    elif event != 'null':
                        raise ValueError('Confidence vectors and token axes must be arrays or null.')
                if local in {'ptm', 'iptm', 'ranking_score'} and local in seen and event not in {'number', 'integer', 'double', 'null'}:
                    raise ValueError('Confidence scalar metrics must be numeric or null.')
                if local in {'ptm', 'iptm', 'ranking_score'} and event in {'number', 'integer', 'double'}:
                    number = _number(value)
                    if local == 'ranking_score' or 0 <= number <= 1:
                        result['rankingScore' if local == 'ranking_score' else local] = number
                if local == 'plddt.item' and 'plddt' in active_vectors:
                    number = _number(value)
                    if not 0 <= number <= 100:
                        raise ValueError('pLDDT must be between 0 and 100.')
                    if len(vector) < MAX_DIMENSION and not vector_overflow:
                        vector.append(number)
                    else:
                        vector_overflow = True
                if build_pae and local in {'token_chain_ids.item', 'token_res_ids.item'} and local.split('.')[0] in active_vectors:
                    key = local.split('.')[0]
                    values = axes.setdefault(key, [])
                    if len(values) >= MAX_DIMENSION:
                        raise PAELimitError('Token-axis metadata exceeds the dimension limit.')
                    if event not in {'string', 'number', 'integer'}:
                        raise ValueError('Invalid token-axis metadata.')
                    if key == 'token_chain_ids' and (not isinstance(value, str) or len(value) > 128):
                        raise ValueError('Invalid token chain identifier.')
                    if key == 'token_res_ids' and (isinstance(value, str) or int(value) != value):
                        raise ValueError('Token residue identifiers must be integers.')
                    values.append(value if isinstance(value, str) else int(value))
                if not build_pae:
                    continue
                if local in {'pae', 'predicted_aligned_error'} and event not in {'start_array', 'end_array', 'null'}:
                    raise ValueError('PAE must be a numeric matrix or null.')
                if local in {'pae', 'predicted_aligned_error'} and event == 'start_array':
                    if matrix_seen:
                        raise ValueError('Multiple PAE matrices are ambiguous.')
                    matrix_seen, matrix_prefix = True, local
                elif matrix_prefix and local == matrix_prefix + '.item':
                    if event == 'start_array':
                        row_size = 0
                    elif event == 'end_array':
                        if row_size is None or not row_size:
                            raise ValueError('PAE rows must be nonempty numeric arrays.')
                        if column_count is None:
                            column_count = row_size
                        if row_size != column_count:
                            raise ValueError('PAE matrix is ragged.')
                        row_count += 1
                        row_size = None
                        if row_count > MAX_DIMENSION:
                            raise PAELimitError('PAE dimension exceeds 5,000.')
                    else:
                        raise ValueError('PAE rows must be arrays.')
                elif matrix_prefix and local.startswith(matrix_prefix + '.item.'):
                    if local != matrix_prefix + '.item.item' or event not in {'number', 'integer', 'double'} or row_size is None:
                        raise ValueError('PAE must contain only numeric matrix cells.')
                    number = _number(value)
                    if number < 0:
                        raise ValueError('PAE cannot contain negative errors.')
                    cells += 1
                    row_size += 1
                    if cells > MAX_CELLS or row_size > MAX_DIMENSION:
                        raise PAELimitError('PAE exceeds the 25-million-cell / 5,000-axis limit.')
                    minimum, maximum = min(minimum, number), max(maximum, number)
                    buffer.append(number)
                    if len(buffer) >= 8192:
                        flush()
                elif matrix_prefix and local == matrix_prefix and event == 'end_array':
                    if not row_count or row_count != column_count:
                        raise ValueError('PAE matrix must be nonempty and square.')
                    matrix_done = True
            if root_type == 'start_array' and objects != 1:
                raise ValueError('Confidence array must contain exactly one object.')
        if before != _stat(path) or reader.count != before['bytes']:
            raise ValueError('Confidence source changed during parsing.')
        if vector_overflow:
            result.setdefault('warnings', []).append('Confidence pLDDT vector exceeds its display limit; structure pLDDT and raw JSON remain available.')
        elif vector:
            result['plddt'] = vector
        if build_pae and matrix_done:
            flush()
            target.flush()
            os.fsync(target.fileno())
            target.close()
            target = None
            axis_data = None
            if axes:
                if any(len(values) != row_count for values in axes.values()):
                    raise ValueError('PAE token-axis metadata length differs from the matrix.')
                axis_kind, axis_data = 'token', axes
            descriptor = _binary_descriptor(temporary, root, path, reader.digest.hexdigest(), before,
                                            (row_count, column_count), axis_kind, axis_data, minimum, maximum, cancelled)
            result.update(paeArtifact=descriptor, pae=None, maxPae=maximum)
            if cells <= INLINE_CELLS:
                result['pae'] = read_pae(descriptor, root, row_start=0, col_start=0, rows=row_count, cols=column_count)['values']
        return result
    except ijson.JSONError as exc:
        raise ValueError('Confidence JSON is invalid or incomplete.') from exc
    finally:
        if target is not None:
            target.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def relocate_pae(result, old_root, new_root):
    """Rebase relative descriptors while retaining only paths inside new_root."""
    descriptor = result.get('paeArtifact')
    if not isinstance(descriptor, dict):
        return result
    for stem in ('storage', 'source', 'manifest'):
        relative = descriptor.get(stem + 'RelativePath')
        if relative:
            path = _safe(new_root, descriptor.get(stem + 'Path') or Path(old_root).resolve() / relative)
            descriptor[stem + 'Path'] = str(path)
            descriptor[stem + 'RelativePath'] = path.relative_to(Path(new_root).resolve()).as_posix()
    return result


def _attach(result, descriptor, root):
    artifacts = result.setdefault('artifacts', [])
    if not isinstance(artifacts, list):
        raise ValueError('Result artifacts must be a list.')
    known = {str(item.get('path')) for item in artifacts if isinstance(item, dict) and item.get('path')}
    for stem in ('storage', 'source', 'manifest'):
        raw = descriptor.get(stem + 'Path') or descriptor.get(stem + 'RelativePath')
        if raw:
            path = _safe(root, raw)
            descriptor[stem + 'RelativePath'] = path.relative_to(Path(root).resolve()).as_posix()
            descriptor[stem + 'Path'] = str(path)
            if str(path) not in known:
                artifacts.append(dict(path=str(path)))
                known.add(str(path))


def compact_pae(result, root, *, cancelled=lambda: False):
    """Mutate/return result: externalize large legacy PAE before deepcopy/JSON."""
    root = Path(root).resolve()
    descriptor = result.get('paeArtifact')
    if isinstance(descriptor, dict) and descriptor.get('status') in {'available', 'unavailable'}:
        _attach(result, descriptor, root)
        if descriptor.get('status') != 'available' or descriptor['rows'] * descriptor['cols'] > INLINE_CELLS:
            result['pae'] = None
        return result
    matrix = result.get('pae')
    if matrix is None:
        return result
    if not isinstance(matrix, list) or not matrix:
        raise ValueError('Inline PAE must be a nonempty bounded matrix.')
    n = len(matrix)
    for row in matrix:
        _check(cancelled)
        if not isinstance(row, list) or len(row) != n or any(_number(value) < 0 for value in row):
            raise ValueError('Inline PAE must be square, finite, and nonnegative.')
    if n * n <= INLINE_CELLS:
        return result
    directory = _safe(root, '.result-artifacts', exists=False)
    directory.mkdir(exist_ok=True)
    source = directory / ('inline-pae-' + uuid.uuid4().hex + '.json')
    # json.dump streams an already-existing inline matrix without another copy.
    with source.open('x', encoding='utf-8') as stream:
        json.dump({'pae': matrix}, stream, allow_nan=False, separators=(',', ':'))
    try:
        parsed = stream_confidence(source, root, build_pae=True, cancelled=cancelled)
    except PAELimitError as exc:
        result['pae'] = None
        result['paeArtifact'] = dict(schemaVersion=1, status='unavailable', reason=str(exc), rows=n, cols=n,
            unit='angstrom', axisKind='unknown', axisOrder='as-stored', sourcePath=str(source),
            sourceRelativePath=source.relative_to(root).as_posix(), sourceBytes=source.stat().st_size,
            sourceSha256=_digest(source, cancelled), sourceStat=_stat(source))
        result.setdefault('warnings', []).append('PAE display limit exceeded; matrix source JSON remains downloadable.')
        _attach(result, result['paeArtifact'], root)
        return result
    result['pae'] = None
    result['paeArtifact'] = parsed['paeArtifact']
    result['maxPae'] = parsed['maxPae']
    _attach(result, result['paeArtifact'], root)
    return result


def publish_pae_urls(result, root, base_url):
    """Validate registered paths, add lazy URLs, and remove private locations."""
    descriptor = result.get('paeArtifact')
    if not isinstance(descriptor, dict) or descriptor.get('status') not in {'available', 'unavailable'}:
        return result
    root = Path(root).resolve()
    registered = {item.get('name') for item in result.get('artifacts', []) if isinstance(item, dict)}
    for stem in ('storage', 'source', 'manifest'):
        raw = descriptor.get(stem + 'Path') or descriptor.get(stem + 'RelativePath')
        if not raw:
            continue
        path = _safe(root, raw)
        name = path.relative_to(root).as_posix()
        if name not in registered:
            raise ValueError('PAE artifact was not registered for this result: ' + name)
        descriptor[stem + 'RelativePath'] = name
        descriptor.pop(stem + 'Path', None)
    if descriptor['status'] == 'unavailable':
        if descriptor.get('sourceRelativePath'):
            descriptor['downloadUrl'] = base_url + '/artifact?name=' + quote(descriptor['sourceRelativePath'], safe='')
        return result
    _validate_descriptor(descriptor, root)
    descriptor.update(previewUrl=base_url + '/pae?maxSize=256', tileUrl=base_url + '/pae',
        downloadUrl=base_url + '/artifact?name=' + quote(descriptor['sourceRelativePath'], safe=''),
        binaryDownloadUrl=base_url + '/artifact?name=' + quote(descriptor['storageRelativePath'], safe=''))
    return result


def _validate_descriptor(descriptor, root):
    if (not isinstance(descriptor, dict) or descriptor.get('schemaVersion') != 1 or descriptor.get('status') != 'available'
            or descriptor.get('dtype') != 'float64' or descriptor.get('byteOrder') != 'little'
            or descriptor.get('layout') != 'row-major' or descriptor.get('axisOrder') != 'as-stored'
            or descriptor.get('unit') != 'angstrom' or descriptor.get('axisKind') not in {'token', 'residue', 'unknown'}):
        raise ValueError('Unsupported PAE artifact descriptor.')
    rows, cols = descriptor.get('rows'), descriptor.get('cols')
    if type(rows) is not int or type(cols) is not int or not 1 <= rows == cols <= MAX_DIMENSION or rows * cols > MAX_CELLS:
        raise ValueError('PAE artifact shape is invalid.')
    if descriptor.get('bytes') != rows * cols * 8 or not HASH.fullmatch(str(descriptor.get('sha256', ''))):
        raise ValueError('PAE artifact byte count or SHA256 is invalid.')
    path = _safe(root, descriptor.get('storagePath') or descriptor.get('storageRelativePath', ''))
    if path.stat().st_size != descriptor['bytes'] or _stat(path) != descriptor.get('storageStat'):
        raise ValueError('PAE artifact changed after publication.')
    source = _safe(root, descriptor.get('sourcePath') or descriptor.get('sourceRelativePath', ''))
    if source.stat().st_size != descriptor.get('sourceBytes') or _stat(source) != descriptor.get('sourceStat'):
        raise ValueError('Original confidence source changed after publication.')
    return path


def read_pae(descriptor, root, *, max_size=256, row_start=None, col_start=None, rows=None, cols=None):
    """Read an exact <=128² tile or sampled-exact <=256² preview, never N²."""
    path = _validate_descriptor(descriptor, root)
    n = descriptor['rows']
    exact = any(value is not None for value in (row_start, col_start, rows, cols))
    if exact:
        if (any(type(value) is not int for value in (row_start, col_start, rows, cols))
                or not 0 <= row_start < n or not 0 <= col_start < n or not 1 <= rows <= MAX_TILE
                or not 1 <= cols <= MAX_TILE or row_start + rows > n or col_start + cols > n):
            raise ValueError('PAE tile must be in bounds and at most 128×128.')
        row_indices, col_indices = list(range(row_start, row_start + rows)), list(range(col_start, col_start + cols))
    else:
        if type(max_size) is not int or not 1 <= max_size <= MAX_PREVIEW:
            raise ValueError('PAE preview max_size must be between 1 and 256.')
        size = min(n, max_size)
        row_indices = [min(n - 1, ((2 * index + 1) * n) // (2 * size)) for index in range(size)]
        col_indices = row_indices[:]
    values = []
    with path.open('rb') as stream:
        if _stat_value(os.fstat(stream.fileno())) != descriptor['storageStat']:
            raise ValueError('PAE artifact identity changed while opening it.')
        for row in row_indices:
            if exact:
                stream.seek((row * n + col_start) * 8)
                raw = stream.read(cols * 8)
                if len(raw) != cols * 8:
                    raise ValueError('PAE artifact was truncated during reading.')
                selected = list(struct.unpack('<' + 'd' * cols, raw))
            else:
                # A whole row is <=40KiB at the configured shape limit.
                stream.seek(row * n * 8)
                raw = stream.read(n * 8)
                if len(raw) != n * 8:
                    raise ValueError('PAE artifact was truncated during reading.')
                decoded = struct.unpack('<' + 'd' * n, raw)
                selected = [decoded[column] for column in col_indices]
            if any(not math.isfinite(value) or value < 0 for value in selected):
                raise ValueError('PAE artifact contains invalid scientific values.')
            values.append(selected)
        if _stat_value(os.fstat(stream.fileno())) != descriptor['storageStat']:
            raise ValueError('PAE artifact changed while reading it.')
        _validate_descriptor(descriptor, root)
    response = dict(sourceShape=[n, n], shape=[len(row_indices), len(col_indices)], method='exact' if exact else 'nearest-center',
                    rowIndices=row_indices, colIndices=col_indices, values=values, unit='angstrom', axisKind=descriptor.get('axisKind', 'unknown'))
    if exact:
        response.update(rowStart=row_start, colStart=col_start, rows=rows, cols=cols)
    return response
