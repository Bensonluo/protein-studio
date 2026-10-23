"""Bounded-memory campaign input parsing; no network or model dependencies."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re


ITEM_FIELDS = {'targetId', 'name', 'chains', 'sequence', 'settings', 'seeds',
               'numSamples', 'numRecycles', 'example', 'priority', 'preset'}
SETTING_FIELDS = {'seeds', 'numSamples', 'numRecycles', 'example', 'priority', 'preset'}
FIXED_STRATEGY = 'fixed-target-plus-candidate-v1'
INDEPENDENT_STRATEGY = 'independent-v1'


def target_id(ordinal, header):
    return 't-' + hashlib.sha256(f'{ordinal}\0{header}'.encode('utf-8')).hexdigest()[:24]


def residue_limit():
    try:
        limit = int(os.environ.get('PROTEIN_STUDIO_MAX_RESIDUES', '5000'))
    except ValueError as exc:
        raise ValueError('PROTEIN_STUDIO_MAX_RESIDUES must be a positive integer.') from exc
    if limit < 1:
        raise ValueError('PROTEIN_STUDIO_MAX_RESIDUES must be a positive integer.')
    return limit


def canonical_chains_sha(chains):
    # Match browser JSON.stringify([{id,sequence},...]), including key order.
    value = [dict(id=c['id'], sequence=c['sequence']) for c in chains]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def bounded_lines(handle, max_residues):
    # Bound a malformed single JSONL/FASTA line too, not just total record count.
    maximum = max(1024 * 1024, max_residues * 8)
    number = 0
    while True:
        line = handle.readline(maximum + 1)
        if not line:
            return
        number += 1
        if len(line) > maximum:
            raise ValueError(f'Input line {number} exceeds the {maximum}-character record-line limit.')
        yield number, line


def detect_format(path, requested='auto', max_residues=None):
    if requested not in ('auto', 'fasta', 'jsonl'):
        raise ValueError('Input format must be auto, fasta, or jsonl.')
    with Path(path).open(encoding='utf-8-sig') as stream:
        first = next((line.strip() for _, line in bounded_lines(stream, max_residues or residue_limit()) if line.strip()), None)
    if first is None:
        raise ValueError('Input contains no targets; the file is empty.')
    content = 'fasta' if first.startswith('>') else 'jsonl' if first.startswith('{') else None
    suffix = Path(path).suffix.lower()
    indicated = 'jsonl' if suffix in ('.jsonl', '.ndjson') else 'fasta' if suffix in ('.fa', '.fasta', '.faa', '.fas') else None
    if requested == 'auto' and indicated and content and indicated != content:
        raise ValueError(f'Input extension indicates {indicated}, but contents indicate {content}; correct the file or specify --format explicitly.')
    selected = (content or indicated) if requested == 'auto' else requested
    if selected is None:
        raise ValueError('Cannot detect input format; use FASTA headers or JSONL objects.')
    if content and selected != content:
        raise ValueError(f'--format {selected} conflicts with {content} contents.')
    return selected


def _name(value, label, maximum=None):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError(f'{label} must be nonempty printable text.')
    if maximum is not None and len(value) > maximum:
        raise ValueError(f'{label} must contain at most {maximum} characters.')
    return value


def _sequence(value, label):
    if not isinstance(value, str):
        raise ValueError(f'{label} sequence must be text.')
    value = re.sub(r'\s+', '', value).upper()
    if not value or not re.fullmatch('[ACDEFGHIKLMNPQRSTVWY]+', value):
        raise ValueError(f'{label}: only the 20 standard amino-acid letters are supported.')
    return value


def iter_fasta_records(handle, max_residues):
    name, parts, length = None, [], 0
    for number, raw in bounded_lines(handle, max_residues):
        line = raw.strip()
        if not line:
            continue
        if line.startswith('>'):
            if name is not None:
                if not parts:
                    raise ValueError(f'FASTA {name!r} has no sequence.')
                yield name, ''.join(parts)
            name = _name(line[1:].strip(), f'FASTA line {number} header')
            parts, length = [], 0
        else:
            if name is None:
                raise ValueError(f'FASTA line {number}: sequence appears before its header.')
            sequence = _sequence(line, f'FASTA line {number}')
            length += len(sequence)
            if length > max_residues:
                raise ValueError(f'FASTA {name!r} exceeds the {max_residues}-residue limit.')
            parts.append(sequence)
    if name is not None:
        if not parts:
            raise ValueError(f'FASTA {name!r} has no sequence.')
        yield name, ''.join(parts)


def load_fixed_target(path, max_residues=None):
    maximum = max_residues or residue_limit()
    detect_format(path, 'fasta', maximum)
    chains, length = [], 0
    with Path(path).open(encoding='utf-8-sig') as handle:
        for _, sequence in iter_fasta_records(handle, maximum):
            if len(chains) >= 25:
                raise ValueError('Fixed target must contain 1–25 FASTA chains, leaving one chain for each candidate.')
            length += len(sequence)
            if length >= maximum:
                raise ValueError(f'Fixed target leaves no room for a candidate within the {maximum}-residue limit.')
            chains.append(dict(id=chr(65 + len(chains)), sequence=sequence))
    if not chains:
        raise ValueError('Fixed target contains no FASTA records.')
    return chains


def normalize_chains(raw, maximum):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 26:
        raise ValueError('Each target must contain 1–26 protein chains.')
    chains, seen, length = [], set(), 0
    for index, chain in enumerate(raw):
        if not isinstance(chain, dict) or set(chain) - {'id', 'sequence'}:
            raise ValueError('Scientific chain fields other than id/sequence are not supported; MSA, templates and modifications cannot be silently discarded.')
        chain_id = chain.get('id', chr(65 + index))
        if not isinstance(chain_id, str) or not re.fullmatch('[A-Z]{1,4}', chain_id) or chain_id in seen:
            raise ValueError('Chain IDs must be unique uppercase letters (1–4 characters).')
        seen.add(chain_id)
        sequence = _sequence(chain.get('sequence'), f'Chain {chain_id}')
        length += len(sequence)
        if length > maximum:
            raise ValueError(f'Target exceeds the configured {maximum}-residue limit.')
        chains.append(dict(id=chain_id, sequence=sequence))
    return chains


def validate_item_settings(item, engine=None):
    settings = item.get('settings', {})
    if not isinstance(settings, dict) or set(settings) - SETTING_FIELDS:
        raise ValueError('Target settings contain unsupported fields.')
    for values in (settings, {k: v for k, v in item.items() if k in SETTING_FIELDS}):
        if 'seeds' in values:
            seeds = values['seeds']
            if (not isinstance(seeds, list) or not 1 <= len(seeds) <= 20
                or any(type(s) is not int or not 0 <= s < 2**31 for s in seeds)
                or len(set(seeds)) != len(seeds)):
                raise ValueError('seeds must contain 1–20 distinct integer seeds from 0 to 2147483647.')
        for key, upper in (('numSamples', 5 if engine == 'colabfold' else 20), ('numRecycles', 50)):
            if key in values and (type(values[key]) is not int or not 1 <= values[key] <= upper):
                raise ValueError(f'{key} must be an integer from 1 to {upper}.')
        if 'priority' in values and values['priority'] not in ('normal', 'high'):
            raise ValueError('priority must be normal or high.')
        for key in ('example', 'preset'):
            if key in values:
                _name(values[key], key, 200)
    effective = dict(settings)
    effective.update({k: v for k, v in item.items() if k in SETTING_FIELDS})
    if engine == 'reference' and (effective.get('numSamples', 1) != 1 or effective.get('seeds', [1]) != [1]):
        raise ValueError('Reference replay requires numSamples=1 and seeds=[1]; it does not sample a model.')


def normalize_json_item(raw, ordinal, prefix, maximum, engine=None):
    if not isinstance(raw, dict) or set(raw) - ITEM_FIELDS:
        raise ValueError('JSONL target contains unsupported fields; provide API target metadata, chains/sequence, and supported settings only.')
    if ('chains' in raw) == ('sequence' in raw):
        raise ValueError('JSONL target needs exactly one of chains or sequence.')
    item = dict(raw)
    identity_name = raw.get('name') or f'target_{ordinal}'
    identifier = raw.get('targetId', target_id(ordinal, identity_name))
    _name(identifier, 'targetId', 200)
    # Match UI fallback IDs and API fallback display names.
    display_name = raw.get('name', identifier[:120])
    _name(display_name, 'name', 120)
    if 'chains' in raw:
        chains = raw['chains']
    else:
        sequence = raw['sequence']
        if not isinstance(sequence, str):
            raise ValueError('JSONL sequence must be text.')
        if sequence.lstrip().startswith('>'):
            records = iter_fasta_records(io.StringIO(sequence), maximum)
            chains = []
            for _, seq in records:
                if len(chains) >= 26:
                    raise ValueError('Each target must contain 1–26 protein chains.')
                chains.append(dict(id=chr(65 + len(chains)), sequence=seq))
        else:
            chains = [dict(id='A', sequence=sequence)]
        item.pop('sequence')
    item.update(targetId=identifier, name=(prefix + display_name)[:120], chains=normalize_chains(chains, maximum))
    _name(item['name'], 'name', 120)
    validate_item_settings(item, engine)
    return item


def iter_targets(handle, input_format, fixed_chains=(), prefix='', maximum=None, engine=None):
    maximum = maximum or residue_limit()
    if fixed_chains and input_format != 'fasta':
        raise ValueError('--fixed-target is supported only with candidate FASTA, not JSONL.')
    _name(prefix, 'name prefix') if prefix else None
    if input_format == 'fasta':
        fixed_length = sum(len(c['sequence']) for c in fixed_chains)
        for ordinal, (name, sequence) in enumerate(iter_fasta_records(handle, maximum - fixed_length), 1):
            chains = [dict(c) for c in fixed_chains] + [dict(id=chr(65 + len(fixed_chains)), sequence=sequence)]
            yield dict(targetId=target_id(ordinal, name), name=(prefix + name)[:120],
                       chains=normalize_chains(chains, maximum))
    else:
        ordinal = 0
        for line_number, line in bounded_lines(handle, maximum):
            if not line.strip():
                continue
            ordinal += 1
            try:
                raw = json.loads(line)
                yield normalize_json_item(raw, ordinal, prefix, maximum, engine)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f'JSONL line {line_number}: {exc}') from exc
