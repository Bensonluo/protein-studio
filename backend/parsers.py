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


IPSAE_PAE_CUTOFF = 10.0


def ipsae_row_score(values):
    """One aligned residue's ipSAE contribution (Dunbrack 2025, Eq. 14-15;
    the interface metric AlphaFold DB displays): the mean TM-like term over
    partner residues whose PAE falls below the cutoff, with d0 scaled by the
    count of qualifying residues (d0 = 1.0 under 27). None when nothing
    qualifies, which leaves the pair score at the floor 0."""
    qualifying = [value for value in values if value < IPSAE_PAE_CUTOFF]
    if not qualifying:
        return None
    length = len(qualifying)
    d0 = 1.24 * (length - 15) ** (1 / 3) - 1.8 if length >= 27 else 1.0
    return sum(1.0 / (1.0 + (value / d0) ** 2) for value in qualifying) / length


CONTACT_PAE_CUTOFF = 10.0
CONTACT_CA_DISTANCE = 15.0
CONTACT_MAX_RANGES = 512
CONTACT_LINE_DISTANCE = 8.0
CONTACT_MAX_LINES = 2048
LIS_PAE_CUTOFF = 12.0


def _index_ranges(indices):
    """Compress sorted residue indices into inclusive [start, end] runs."""
    runs = []
    for index in indices:
        if runs and index == runs[-1][1] + 1:
            runs[-1][1] = index
        else:
            runs.append([index, index])
    return runs


def interface_contacts(rows, residues, lines=False):
    """Confident interface contacts per unordered chain pair, using the AFDB
    ipSAE pair convention (AlphaFold DB FAQ: PAE < 10 A and predicted-structure
    distance < 15 A between residues of different chains) with the ChimeraX
    alphafold-interfaces habit of reporting, per pair, the confident-pair count
    and the participating residues on each side. PAE is the lower of the two
    directed values; distance is C-alpha to C-alpha (the parser keeps exactly
    one CA per residue). rows yields the matrix rows in axis order; residues
    pair with rows by index. Returns {'contacts': [{a, b, pairs, resA, resB}]
    with inclusive zero-based index runs, 'truncated': bool} and only lists
    chain pairs with at least one confident contact.

    With lines=True also returns 'lines': [{i, j, pae}] for cross-chain residue
    pairs within 8 A (the ChimeraX alphafold contacts default and the CASP RR
    contact criterion), PAE-colored per pair like that tool's display and not
    filtered by any PAE cutoff. Pairs whose PAE cells are missing or
    non-finite cannot be colored and are skipped rather than guessed; the list
    is capped at CONTACT_MAX_LINES with 'linesTruncated': True.

    Also returns 'scores': the AFM-LIS / LIVIA local interface metrics per
    unordered chain pair (Kim et al. 2024; Kim & Perrimon 2026). Each cell's
    confidence is 1 - PAE/12 (the ROC-optimized cutoff), zero or above the
    cutoff cells are excluded; LIS is the mean confidence per direction
    averaged over the two directions (PAE is not symmetrized), cLIS restricts
    to the cells of pairs also within CONTACT_LINE_DISTANCE, and iLIS =
    sqrt(LIS * cLIS), so a confidently predicted but physically distant pair
    scores 0 - the geometric mean eliminates that false-positive class. LIA /
    cLIA count contributing cells (both directions, a pair confident both ways
    counts twice), iLIA = sqrt(LIA * cLIA) and iLISA = iLIS * iLIA. The
    contact distance is C-alpha here (one CA per residue) where LIVIA uses
    C-beta <= 8 A. Only chain pairs with at least one confident cell are
    listed."""
    size = len(residues)
    limit = CONTACT_CA_DISTANCE * CONTACT_CA_DISTANCE
    line_limit = CONTACT_LINE_DISTANCE * CONTACT_LINE_DISTANCE
    cells = {}
    for position, residue in enumerate(residues):
        cells.setdefault((int(residue['x'] // CONTACT_CA_DISTANCE), int(residue['y'] // CONTACT_CA_DISTANCE),
                          int(residue['z'] // CONTACT_CA_DISTANCE)), []).append(position)
    partners = {index: [] for index in range(size)}
    close_pairs = set()
    for position, residue in enumerate(residues):
        gx, gy, gz = int(residue['x'] // CONTACT_CA_DISTANCE), int(residue['y'] // CONTACT_CA_DISTANCE), \
            int(residue['z'] // CONTACT_CA_DISTANCE)
        for ox in (gx - 1, gx, gx + 1):
            for oy in (gy - 1, gy, gy + 1):
                for oz in (gz - 1, gz, gz + 1):
                    for other in cells.get((ox, oy, oz), ()):
                        if other <= position or residues[other]['chain'] == residue['chain']:
                            continue
                        dx = residues[other]['x'] - residue['x']
                        dy = residues[other]['y'] - residue['y']
                        dz = residues[other]['z'] - residue['z']
                        squared = dx * dx + dy * dy + dz * dz
                        if squared <= line_limit:
                            close_pairs.add((position, other))
                        if squared <= limit:
                            partners[position].append(other)
                            partners[other].append(position)
    directed = {}
    lis = {}
    for row_index, row in enumerate(rows):
        row_chain = residues[row_index]['chain']
        for column in partners[row_index]:
            value = row[column]
            if isinstance(value, (int, float)) and math.isfinite(value):
                directed[(row_index, column)] = value
        for column, residue in enumerate(residues):
            other_chain = residue['chain']
            if other_chain == row_chain:
                continue
            value = row[column]
            if not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or value >= LIS_PAE_CUTOFF:
                continue
            confidence = 1.0 - value / LIS_PAE_CUTOFF
            key = (row_chain, other_chain) if row_chain < other_chain else (other_chain, row_chain)
            entry = lis.setdefault(key, {'sum': [0.0, 0.0], 'count': [0, 0],
                                         'contact_sum': [0.0, 0.0], 'contact_count': [0, 0]})
            forward = 0 if row_chain == key[0] else 1
            entry['sum'][forward] += confidence
            entry['count'][forward] += 1
            if (row_index, column) in close_pairs or (column, row_index) in close_pairs:
                entry['contact_sum'][forward] += confidence
                entry['contact_count'][forward] += 1
    grouped = {}
    for (row_index, column), value in directed.items():
        if row_index > column:
            continue
        if min(value, directed.get((column, row_index), float('inf'))) < CONTACT_PAE_CUTOFF:
            key = (residues[row_index]['chain'], residues[column]['chain'])
            first, second = key if key[0] < key[1] else (key[1], key[0])
            entry = grouped.setdefault((first, second), {'pairs': 0, 'a': set(), 'b': set()})
            entry['pairs'] += 1
            side_a, side_b = (row_index, column) if residues[row_index]['chain'] == first else (column, row_index)
            entry['a'].add(side_a)
            entry['b'].add(side_b)
    truncated = False
    summary = []
    for (first, second), entry in sorted(grouped.items()):
        runs_a, runs_b = _index_ranges(sorted(entry['a'])), _index_ranges(sorted(entry['b']))
        if len(runs_a) > CONTACT_MAX_RANGES or len(runs_b) > CONTACT_MAX_RANGES:
            truncated = True
            runs_a, runs_b = runs_a[:CONTACT_MAX_RANGES], runs_b[:CONTACT_MAX_RANGES]
        summary.append({'a': first, 'b': second, 'pairs': entry['pairs'],
                        'resA': runs_a, 'resB': runs_b})
    score_rows = []
    for (first, second), entry in sorted(lis.items()):
        directions = [entry['sum'][side] / entry['count'][side] if entry['count'][side] else 0.0
                      for side in (0, 1)]
        contact_directions = [entry['contact_sum'][side] / entry['contact_count'][side]
                              if entry['contact_count'][side] else 0.0 for side in (0, 1)]
        lis_value = (directions[0] + directions[1]) / 2.0
        clis_value = (contact_directions[0] + contact_directions[1]) / 2.0
        ilis_value = math.sqrt(lis_value * clis_value)
        lia = entry['count'][0] + entry['count'][1]
        clia = entry['contact_count'][0] + entry['contact_count'][1]
        ilia_value = math.sqrt(lia * clia)
        score_rows.append({'a': first, 'b': second, 'lis': round(lis_value, 4),
                           'clis': round(clis_value, 4), 'ilis': round(ilis_value, 4),
                           'lia': lia, 'clia': clia, 'ilia': round(ilia_value, 1),
                           'ilisa': round(ilis_value * ilia_value, 2)})
    result = {'contacts': summary, 'scores': score_rows, 'truncated': truncated}
    if lines:
        line_records = []
        lines_truncated = False
        for first, second in sorted(close_pairs):
            if len(line_records) >= CONTACT_MAX_LINES:
                lines_truncated = True
                break
            pae = min(directed.get((first, second), float('inf')),
                      directed.get((second, first), float('inf')))
            if not math.isfinite(pae):
                continue
            line_records.append({'i': first, 'j': second, 'pae': round(pae, 2)})
        result['lines'] = line_records
        result['linesTruncated'] = lines_truncated
    return result


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
    # ⟨PAE⟩_int per unordered chain pair (Zhu et al. 2023): mean of both
    # directed inter-chain blocks. Only with token_chain_ids aligned to the
    # matrix axes; never guessed from structure order.
    chain_ids = data.get('token_chain_ids')
    if 'pae' in result and isinstance(chain_ids, list) and len(chain_ids) == len(result['pae']) \
            and all(isinstance(x, str) and x and len(x) <= 128 for x in chain_ids):
        if 2 <= len(set(chain_ids)) <= 64:
            sums, counts = {}, {}
            for i, first in enumerate(chain_ids):
                row = result['pae'][i]
                for j, second in enumerate(chain_ids):
                    if first == second:
                        continue
                    key = (first, second) if first < second else (second, first)
                    sums[key] = sums.get(key, 0.0) + row[j]
                    counts[key] = counts.get(key, 0) + 1
            result['interfacePae'] = [{'a': a, 'b': b, 'mean': sums[(a, b)] / counts[(a, b)]}
                                      for (a, b) in sorted(sums)]
            # ipSAE per chain pair (Dunbrack 2025, Eq. 14-16; the metric the
            # AlphaFold DB displays): per aligned residue the mean TM-like
            # term over partner residues with PAE < cutoff, d0 scaled by the
            # qualifying count, then max over each direction. Pairs without
            # confident contacts score 0.
            best = {}
            for i, first in enumerate(chain_ids):
                row = result['pae'][i]
                for second in set(chain_ids):
                    if second == first:
                        continue
                    score = ipsae_row_score([row[j] for j, other in enumerate(chain_ids) if other == second])
                    if score is not None:
                        key = (first, second)
                        if score > best.get(key, 0.0):
                            best[key] = score
            result['ipSae'] = [{'a': a, 'b': b,
                                'value': round(max(best.get((a, b), 0.0), best.get((b, a), 0.0)), 4)}
                               for a, b in sorted({(x, y) for x in set(chain_ids) for y in set(chain_ids) if x < y})]
    return result
