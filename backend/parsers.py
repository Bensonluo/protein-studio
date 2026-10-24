"""Small, dependency-free readers for protein backbone coordinates and confidence.

Only the first coordinate model and one C-alpha per residue are displayed. The
original all-atom file remains the downloadable scientific artifact. Experimental
B factors are never interpreted as pLDDT unless the caller identifies a supported
prediction source explicitly. Pickle outputs are deliberately not deserialized.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

AA3 = dict(zip(
    'ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL'.split(),
    'ARNDCQEGHILKMFPSTWYV',
))
AA3.update({'MSE': 'M', 'SEC': 'U', 'PYL': 'O', 'UNK': 'X'})
PREDICTION_SOURCES = {'alphafold', 'alphafold2', 'alphafold3', 'colabfold', 'afdb'}
MAX_FILE_BYTES = 64 * 1024 * 1024


class FileTooLargeError(ValueError):
    """A file exceeds the in-memory parser limit."""


def _number(value, default=None):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _read(path: Path) -> str:
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise FileTooLargeError('Structure or confidence file exceeds the 64 MiB parser limit.')
    return path.read_text(encoding='utf-8', errors='strict')


def _pdb_atoms(text):
    annotations = []
    first_model = None
    for line in text.splitlines():
        record = line[:6].strip()
        if record == 'MODEL':
            model = line[10:14].strip() or '1'
            if first_model is None:
                first_model = model
            elif model != first_model:
                break
        elif record == 'ENDMDL':
            break
        elif record == 'HELIX':
            try:
                annotations.append((line[19], int(line[21:25]), int(line[33:37]), 'helix'))
            except ValueError:
                pass
        elif record == 'SHEET':
            try:
                annotations.append((line[21], int(line[22:26]), int(line[33:37]), 'sheet'))
            except ValueError:
                pass
        elif record in ('ATOM', 'HETATM') and line[12:16].strip() == 'CA':
            aa = AA3.get(line[17:20].strip())
            if aa is None:
                continue
            try:
                residue = int(line[22:26])
            except ValueError:
                continue
            chain = line[21:22].strip() or 'A'
            secondary = next((kind for c, start, end, kind in annotations
                              if c == chain and start <= residue <= end), None)
            yield dict(chain=chain, residueNumber=residue,
                       insertionCode=line[26:27].strip(), aa=aa,
                       alt=line[16:17].strip(), occupancy=_number(line[54:60], 0),
                       bFactor=_number(line[60:66]), x=_number(line[30:38]),
                       y=_number(line[38:46]), z=_number(line[46:54]), secondary=secondary)


def _cif_tokens(text):
    """CIF 1.1 tokens, including quotes, comments and semicolon text fields."""
    i, size = 0, len(text)
    while i < size:
        if text[i].isspace():
            i += 1
            continue
        if text[i] == '#':
            end = text.find('\n', i)
            i = size if end < 0 else end + 1
            continue
        if text[i] == ';' and (i == 0 or text[i - 1] == '\n'):
            end = text.find('\n;', i + 1)
            if end < 0:
                raise ValueError('Unterminated mmCIF multiline value.')
            yield text[i + 1:end]
            i = end + 2
            continue
        if text[i] in "\"'":
            quote, start = text[i], i + 1
            i += 1
            while i < size:
                if text[i] == quote and (i + 1 == size or text[i + 1].isspace()):
                    break
                i += 1
            if i == size:
                raise ValueError('Unterminated mmCIF quoted value.')
            yield text[start:i]
            i += 1
            continue
        start = i
        while i < size and not text[i].isspace():
            i += 1
        yield text[start:i]


def _cif_atoms(text):
    tokens = iter(_cif_tokens(text))
    pending = None
    first_model = None
    while True:
        token = pending if pending is not None else next(tokens, None)
        pending = None
        if token is None:
            break
        if token.lower() != 'loop_':
            continue
        headers = []
        token = next(tokens, None)
        while token is not None and token.startswith('_'):
            headers.append(token)
            token = next(tokens, None)
        if not headers:
            raise ValueError('mmCIF loop has no column names.')
        atom_loop = headers[0].startswith('_atom_site.')
        row = []
        while token is not None:
            if not row and (token.startswith('_') or token.lower() in ('loop_', 'stop_')
                            or token.lower().startswith(('data_', 'save_'))):
                pending = token
                break
            row.append(token)
            if len(row) == len(headers):
                if atom_loop:
                    fields = {key.split('.', 1)[1]: value for key, value in zip(headers, row)}
                    def get(*keys, default=None):
                        return next((fields[k] for k in keys if fields.get(k) not in (None, '.', '?')), default)
                    model = get('pdbx_PDB_model_num', default='1')
                    if first_model is None:
                        first_model = model
                    atom = get('label_atom_id', 'auth_atom_id')
                    aa = AA3.get(get('label_comp_id', 'auth_comp_id'))
                    seq = get('auth_seq_id', 'label_seq_id')
                    if atom == 'CA' and aa and model == first_model:
                        try:
                            residue = int(seq)
                        except (ValueError, TypeError):
                            residue = None
                        if residue is not None:
                            yield dict(chain=get('auth_asym_id', 'label_asym_id', default='A'),
                                       residueNumber=residue, insertionCode=get('pdbx_PDB_ins_code', default=''),
                                       aa=aa, alt=get('label_alt_id', default=''),
                                       occupancy=_number(get('occupancy'), 0), bFactor=_number(get('B_iso_or_equiv')),
                                       x=_number(get('Cartn_x')), y=_number(get('Cartn_y')),
                                       z=_number(get('Cartn_z')), secondary=None)
                row = []
            token = next(tokens, None)
        if row:
            raise ValueError('Incomplete mmCIF loop row.')


def parse_structure(path: Path, source: str = 'unknown') -> dict:
    path = Path(path)
    text = _read(path)
    is_cif = path.suffix.lower() in {'.cif', '.mmcif'} or text.lstrip().startswith('data_')
    atoms = _cif_atoms(text) if is_cif else _pdb_atoms(text)
    chosen = {}
    for atom in atoms:
        if any(atom[k] is None for k in ('x', 'y', 'z')):
            continue
        key = (atom['chain'], atom['residueNumber'], atom['insertionCode'])
        preference = (atom['alt'] in ('', 'A'), atom['occupancy'])
        old = chosen.get(key)
        if old is None or preference > old[0]:
            chosen[key] = (preference, atom)
    if not chosen:
        raise ValueError('No finite protein C-alpha coordinates found in the structure.')
    residues, sequences = [], {}
    predicted = source.lower() in PREDICTION_SOURCES
    for index, (_, atom) in enumerate(chosen.values(), 1):
        atom.pop('alt')
        atom.pop('occupancy')
        atom['index'] = index
        if predicted and atom['bFactor'] is not None and 0 <= atom['bFactor'] <= 100:
            atom['plddt'] = atom['bFactor']
        residues.append(atom)
        sequences.setdefault(atom['chain'], []).append(atom['aa'])
    plddt = [r.get('plddt') for r in residues] if predicted else None
    if plddt is not None and any(v is None for v in plddt):
        plddt = None
    return dict(structureFormat='mmcif' if is_cif else 'pdb', residues=residues,
                sequenceLength=len(residues), chains=[dict(id=k, sequence=''.join(v)) for k, v in sequences.items()],
                plddt=plddt, pae=None, meanPlddt=sum(plddt) / len(plddt) if plddt else None,
                ptm=None, iptm=None, confidenceDefinition='C-alpha pLDDT' if plddt else None)


def parse_confidence(path: Path) -> dict:
    """Read AF3/ColabFold/AFDB-style JSON without executing Python objects."""
    data = json.loads(_read(Path(path)), parse_constant=lambda value: None)
    if isinstance(data, list):
        data = data[0] if data and isinstance(data[0], dict) else {}
    if not isinstance(data, dict):
        raise ValueError('Confidence JSON must be an object or a one-object list.')
    result = {}
    for field in ('ptm', 'iptm', 'ranking_score'):
        value = _number(data.get(field))
        if value is not None:
            if field != 'ranking_score' and not 0 <= value <= 1:
                continue
            result['rankingScore' if field == 'ranking_score' else field] = value
    vector = data.get('plddt')
    if isinstance(vector, list) and vector and all(_number(x) is not None and 0 <= float(x) <= 100 for x in vector):
        result['plddt'] = [float(x) for x in vector]
    # AF3 multichain convention (output.md / EBI): chain_ptm / chain_iptm are
    # [num_chains] vectors; chain_iptm[i] is chain i's average interface
    # confidence against all other chains. Bounded to keep records small.
    for field, key in (('chain_ptm', 'chainPtm'), ('chain_iptm', 'chainIptm')):
        vector = data.get(field)
        if isinstance(vector, list) and 0 < len(vector) <= 64 and all(
                _number(x) is not None and 0 <= float(x) <= 1 for x in vector):
            result[key] = [float(x) for x in vector]
    matrix = data.get('pae', data.get('predicted_aligned_error'))
    if isinstance(matrix, list) and matrix:
        n = len(matrix)
        if n * n > 4_000_000:
            result['warnings'] = ['PAE exceeds the 2,000-token visualization limit; original JSON remains downloadable.']
        elif all(isinstance(row, list) and len(row) == n for row in matrix):
            if all(_number(x) is not None and float(x) >= 0 for row in matrix for x in row):
                result['pae'] = [[float(x) for x in row] for row in matrix]
    return result
