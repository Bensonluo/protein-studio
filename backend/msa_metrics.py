"""Lossless-source, bounded-memory QC for raw standard-protein A3M inputs.

The function does not edit, filter, deduplicate, pair, or return MSA sequences.
``text_or_lines`` is either the original text or an iterable of exact text
chunks. Iterable chunks are concatenated verbatim: callers must retain line
terminators (e.g. iterate an open file with ``newline=''``). No newline is added
between chunks. The SHA-256 covers that exact concatenation encoded as UTF-8.
"""
from __future__ import annotations

import hashlib


_STANDARD = frozenset('ACDEFGHIKLMNPQRSTVWY')
_CHUNK_SIZE = 64 * 1024
_COVERAGE_DEFINITION = (
    'Non-gap aligned positions / queryLength for non-query rows; '
    'lowercase insertions excluded; duplicate rows retained. Not sequence identity.'
)


def _check(cancelled):
    if cancelled():
        from .engine import PredictionCancelled
        raise PredictionCancelled('MSA quality summary cancelled.')


def _chunks(text_or_lines, cancelled):
    source = (text_or_lines,) if isinstance(text_or_lines, str) else text_or_lines
    try:
        iterator = iter(source)
    except TypeError as exc:
        raise ValueError('MSA must be text or an iterable of exact text chunks.') from exc
    for text in iterator:
        _check(cancelled)
        if not isinstance(text, str):
            raise ValueError('MSA chunks must be strings, with original line terminators retained.')
        for start in range(0, len(text), _CHUNK_SIZE):
            yield text[start:start + _CHUNK_SIZE]


def summarize_a3m(text_or_lines, query_sequence, role, cancelled=lambda: False):
    """Validate and summarize raw A3M in one pass, using O(query length) memory.

    ``rawRows`` counts FASTA sequence records, including the first query, not
    physical text lines. ``hitRows`` excludes that first record but retains
    duplicates and gap-only rows. Coverage, gap fraction, insertion and unknown
    residue counts concern non-query rows only. Uppercase non-standard amino
    acids count as non-gap positions and separately as nonStandardAlignedResidues.

    Empty text is an explicit empty MSA, distinct from a query-only record. It
    does not count the query that native AF3 may later introduce. ``pairedMsa``
    must use role ``pairing-candidates``: its rows are not final paired features.
    Invalid source raises ValueError; no partial QC or rewritten text is returned.
    """
    if not isinstance(query_sequence, str) or not query_sequence or any(
            residue not in _STANDARD for residue in query_sequence):
        raise ValueError('MSA query must contain nonempty standard uppercase protein residues.')
    if not isinstance(role, str) or role not in {'unpaired', 'pairing-candidates'}:
        raise ValueError('MSA role must be unpaired or pairing-candidates.')
    _check(cancelled)
    length = len(query_sequence)
    # Each row's non-gap count is an integer in [0, length]. This exact
    # histogram supports quantiles without retaining a value for every hit.
    histogram = [0] * (length + 1)
    digest, source_bytes = hashlib.sha256(), 0
    raw_rows, nongap_sum, gap_sum, insertions, nonstandard = 0, 0, 0, 0, 0
    have_header, line_mode, trailing_space = False, None, False
    aligned, nongap, row_insertions, row_nonstandard = 0, 0, 0, 0

    def finish_record():
        nonlocal raw_rows, nongap_sum, gap_sum, insertions, nonstandard
        if aligned != length:
            raise ValueError('A3M record has an inconsistent alignment width after excluding lowercase insertions.')
        if raw_rows:
            histogram[nongap] += 1
            nongap_sum += nongap
            gap_sum += length - nongap
            insertions += row_insertions
            nonstandard += row_nonstandard
        raw_rows += 1

    for chunk in _chunks(text_or_lines, cancelled):
        _check(cancelled)
        encoded = chunk.encode('utf-8')
        digest.update(encoded)
        source_bytes += len(encoded)
        for char in chunk:
            # CRLF and lone CR are accepted without changing their source hash.
            if char in '\r\n':
                line_mode, trailing_space = None, False
                continue
            if line_mode == 'header':
                continue
            if line_mode is None:
                if char.isspace():
                    continue
                if char == '>':
                    if have_header:
                        finish_record()
                    have_header, line_mode = True, 'header'
                    aligned = nongap = row_insertions = row_nonstandard = 0
                    continue
                if not have_header:
                    raise ValueError('A3M sequence data precedes its header.')
                line_mode = 'sequence'
            if char.isspace():
                trailing_space = True
                continue
            if trailing_space:
                raise ValueError('A3M sequence lines cannot contain internal whitespace.')
            if 'a' <= char <= 'z':
                row_insertions += 1
                continue
            if char != '-' and not 'A' <= char <= 'Z':
                raise ValueError('A3M sequence contains a non-protein alignment character.')
            if aligned >= length:
                raise ValueError('A3M record has an inconsistent alignment width after excluding lowercase insertions.')
            if raw_rows == 0 and char != query_sequence[aligned]:
                raise ValueError('A3M query does not match the chain sequence.')
            aligned += 1
            if char != '-':
                nongap += 1
                row_nonstandard += char not in _STANDARD
    _check(cancelled)
    if have_header:
        finish_record()
    elif source_bytes:
        raise ValueError('Nonempty A3M is missing its query header.')

    hits = max(raw_rows - 1, 0)
    def quantile(percent):
        if not hits:
            return None
        rank = (hits * percent + 99) // 100
        cumulative = 0
        for count, frequency in enumerate(histogram):
            cumulative += frequency
            if cumulative >= rank:
                return count / length
        raise AssertionError('MSA coverage histogram did not account for every hit.')

    result = dict(
        schemaVersion=1, format='a3m', role=role, scope='raw-pre-featurisation',
        status='explicit-empty' if not raw_rows else 'query-only' if not hits else 'available',
        sourceSha256=digest.hexdigest(), sourceUtf8Bytes=source_bytes,
        queryLength=length, alignedQueryLength=length if raw_rows else None,
        queryPresent=bool(raw_rows), rawRows=raw_rows, hitRows=hits,
        coverage=dict(definition=_COVERAGE_DEFINITION, quantileMethod='nearest-rank',
                      mean=nongap_sum / (hits * length) if hits else None,
                      p10=quantile(10), p50=quantile(50), p90=quantile(90),
                      zeroRows=histogram[0], fullRows=histogram[length]),
        gapFraction=gap_sum / (hits * length) if hits else None,
        insertedResidues=insertions, nonStandardAlignedResidues=nonstandard)
    if role == 'pairing-candidates':
        result['finalPairedRows'] = None
    return result
