#!/usr/bin/env python3
"""Stream a multi-record FASTA into independent Protein Studio prediction jobs.

Every FASTA record is one single-chain target, not one chain in a complex.
Only Python's standard library is required. Resume uses a disk-backed index and
an append-only acknowledgement manifest; requests are never retried implicitly.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


AMINO_ACIDS = re.compile(r'^[ACDEFGHIKLMNPQRSTVWY]+$')
FINGERPRINT_VERSION = 1


@dataclass(frozen=True)
class FastaRecord:
    name: str
    sequence: str
    line: int


def iter_fasta(handle):
    """Keep only the current record in memory; reject ambiguous input early."""
    name, parts, header_line = None, [], 0
    for number, raw in enumerate(handle, 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith('>'):
            if name is not None:
                if not parts:
                    raise ValueError(f'FASTA line {header_line}: {name!r} has no sequence.')
                yield FastaRecord(name, ''.join(parts), header_line)
            name, parts, header_line = line[1:].strip(), [], number
            if not name or any(ord(char) < 32 for char in name):
                raise ValueError(f'FASTA line {number}: provide a non-empty printable record name.')
        else:
            if name is None:
                raise ValueError(f'FASTA line {number}: sequence appears before its >record header.')
            sequence = ''.join(line.split()).upper()
            if not AMINO_ACIDS.fullmatch(sequence):
                raise ValueError(
                    f'FASTA line {number}: only standard amino-acid letters are supported. '
                    'Each record is one independent single-chain target; use the single-job '
                    'multi-chain form for a complex.'
                )
            parts.append(sequence)
    if name is not None:
        if not parts:
            raise ValueError(f'FASTA line {header_line}: {name!r} has no sequence.')
        yield FastaRecord(name, ''.join(parts), header_line)


def normalize_server(value):
    value = value.strip()
    bare = '://' not in value
    parsed = urlsplit('http://' + value if bare else value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('Server port is invalid.') from exc
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/')):
        raise ValueError('--server must be an HTTP(S) server address without credentials or a path.')
    host = parsed.hostname.lower()
    host = f'[{host}]' if ':' in host else host
    port = 8765 if bare and port is None else port
    netloc = f'{host}:{port}' if port is not None else host
    return urlunsplit((parsed.scheme, netloc, '', '', ''))


def parse_seeds(value):
    try:
        seeds = [int(part.strip()) for part in value.split(',')]
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Use comma-separated integer seeds, such as 1,2,3.') from exc
    if not 1 <= len(seeds) <= 20 or len(set(seeds)) != len(seeds) or any(not 0 <= x < 2**31 for x in seeds):
        raise argparse.ArgumentTypeError('Provide 1–20 distinct seeds from 0 to 2147483647.')
    return seeds


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class ResumeIndex:
    """Temporary on-disk index: memory use does not grow with manifest length."""
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.execute('PRAGMA journal_mode=OFF')
        self.connection.execute('PRAGMA synchronous=OFF')
        self.connection.execute('CREATE TABLE accepted (fingerprint TEXT PRIMARY KEY)')
        self.connection.execute('CREATE TABLE occurrences (base TEXT PRIMARY KEY, count INTEGER NOT NULL)')

    def close(self):
        self.connection.close()

    def load(self, path):
        if not path.exists():
            return
        with path.open(encoding='utf-8') as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f'Manifest line {number} is invalid JSON; preserve and repair this file before resuming.') from exc
                if not isinstance(item, dict) or item.get('event') != 'accepted':
                    raise ValueError(f'Manifest line {number} is not an accepted-job record.')
                if item.get('fingerprintVersion') != FINGERPRINT_VERSION:
                    raise ValueError(f'Manifest line {number} uses an unsupported fingerprint version.')
                fingerprint = item.get('fingerprint', '')
                if not isinstance(fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', fingerprint) or not item.get('jobId'):
                    raise ValueError(f'Manifest line {number} has no valid fingerprint/jobId.')
                self.connection.execute('INSERT OR IGNORE INTO accepted VALUES (?)', (fingerprint,))
        self.connection.commit()

    def occurrence(self, base):
        row = self.connection.execute('SELECT count FROM occurrences WHERE base=?', (base,)).fetchone()
        number = row[0] + 1 if row else 1
        self.connection.execute('INSERT OR REPLACE INTO occurrences VALUES (?,?)', (base, number))
        return number

    def contains(self, fingerprint):
        return self.connection.execute('SELECT 1 FROM accepted WHERE fingerprint=?', (fingerprint,)).fetchone() is not None

    def add(self, fingerprint):
        self.connection.execute('INSERT OR IGNORE INTO accepted VALUES (?)', (fingerprint,))


def prepare(record, args, index):
    sequence_sha = hashlib.sha256(record.sequence.encode('ascii')).hexdigest()
    name = (args.name_prefix + record.name).strip()[:120]
    if not name:
        raise ValueError('Task name cannot be empty.')
    payload = dict(name=name, engine=args.engine, chains=[dict(id='A', sequence=record.sequence)],
                   seeds=args.seeds, numSamples=args.samples, numRecycles=args.recycles)
    identity = dict(recordName=record.name, inputSha256=sequence_sha, namePrefix=args.name_prefix,
                    configurationId=args.configuration_id,
                    engine=args.engine, seeds=args.seeds, numSamples=args.samples,
                    numRecycles=args.recycles, server=args.server)
    base = digest(identity)
    occurrence = index.occurrence(base)
    fingerprint = digest(dict(version=FINGERPRINT_VERSION, identity=identity, occurrence=occurrence))
    metadata = dict(fingerprintVersion=FINGERPRINT_VERSION, fingerprint=fingerprint,
                    inputSha256=sequence_sha, recordName=record.name,
                    occurrence=occurrence, sequenceLength=len(record.sequence),
                    name=name, server=args.server,
                    configuration={k: identity[k] for k in ('engine', 'seeds', 'numSamples', 'numRecycles', 'namePrefix', 'configurationId')})
    return payload, metadata


def post_batch(server, payloads, timeout=60):
    data = json.dumps({'jobs': payloads}, ensure_ascii=False).encode('utf-8')
    if len(data) > 12 * 1024 * 1024:
        raise ValueError('Chunk exceeds the API 12 MiB body limit; reduce --chunk-size.')
    request = Request(server + '/api/jobs/batch', data=data,
                      headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as exc:
        detail = exc.read(4096).decode('utf-8', errors='replace')
        raise RuntimeError(f'HTTP {exc.code}: {detail}. This chunk was not acknowledged; no automatic retry was made.') from exc
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            'Submission outcome is unknown: inspect the server queue before resubmitting this chunk. '
            f'No automatic retry was made. Cause: {exc}'
        ) from exc
    jobs = result.get('jobs') if isinstance(result, dict) else None
    if not isinstance(jobs, list) or len(jobs) != len(payloads) or result.get('count') != len(payloads):
        raise RuntimeError('Unexpected batch acknowledgement; inspect the server queue before resubmitting.')
    ids = []
    for item in jobs:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not re.fullmatch('[A-Za-z0-9_-]+', item['id']):
            raise RuntimeError('Invalid job ID in acknowledgement; inspect the server queue before resubmitting.')
        ids.append(item['id'])
    if len(set(ids)) != len(ids):
        raise RuntimeError('Duplicate job IDs in acknowledgement; inspect the server queue before resubmitting.')
    return jobs


@contextmanager
def manifest_writer(path, dry_run):
    if dry_run:
        yield None
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        if os.name == 'posix':
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError('Another submitter holds this manifest. Use one writer per manifest.') from exc
        yield handle


def execute(args):
    examined = skipped = planned = accepted = chunks = 0
    preview, pending = [], []
    with tempfile.TemporaryDirectory(prefix='protein-batch-index-') as temp, manifest_writer(args.manifest, args.dry_run) as manifest:
        index = ResumeIndex(Path(temp) / 'resume.sqlite3')
        try:
            if args.resume:
                index.load(args.manifest)

            def submit():
                nonlocal accepted, chunks
                if not pending:
                    return
                jobs = post_batch(args.server, [payload for payload, _ in pending])
                for job, (_, metadata) in zip(jobs, pending):
                    entry = dict(metadata, event='accepted', jobId=job['id'],
                                 acceptedAt=datetime.now(timezone.utc).isoformat())
                    manifest.write(json.dumps(entry, ensure_ascii=False) + '\n')
                    index.add(metadata['fingerprint'])
                manifest.flush()
                os.fsync(manifest.fileno())
                index.connection.commit()
                accepted += len(pending)
                chunks += 1
                print(f'Accepted {accepted} jobs in {chunks} chunks; skipped {skipped}. Manifest: {args.manifest}', file=sys.stderr)
                pending.clear()

            with args.input.open(encoding='utf-8-sig') as handle:
                records = iter_fasta(handle)
                while args.limit is None or examined < args.limit:
                    try:
                        record = next(records)
                    except StopIteration:
                        break
                    examined += 1
                    payload, metadata = prepare(record, args, index)
                    if args.resume and index.contains(metadata['fingerprint']):
                        skipped += 1
                        continue
                    planned += 1
                    if len(preview) < 3:
                        preview.append(metadata)
                    if not args.dry_run:
                        pending.append((payload, metadata))
                        if len(pending) == args.chunk_size:
                            submit()
            if examined == 0:
                raise ValueError('Input FASTA contains no records.')
            if not args.dry_run:
                submit()
        finally:
            index.close()
    return dict(mode='dry-run' if args.dry_run else 'submitted', server=args.server,
                input=str(args.input), manifest=str(args.manifest), recordsRead=examined,
                skippedPreviouslyAccepted=skipped, plannedJobs=planned, acceptedJobs=accepted,
                chunks=math.ceil(planned / args.chunk_size) if args.dry_run else chunks,
                independentTargets=True, preview=preview)


def parser():
    value = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    value.add_argument('--server', default='http://127.0.0.1:8765', help='API server; bare localhost uses port 8765.')
    value.add_argument('--engine', required=True, choices=('alphafold2', 'alphafold3', 'colabfold'))
    value.add_argument('--input', type=Path, required=True, help='Multi-record FASTA; each record is one independent target.')
    value.add_argument('--name-prefix', default='', help='Prefix for display names; full original record name remains in the fingerprint.')
    value.add_argument('--configuration-id', default='unspecified', help='Stable model/weights/database configuration label; change it when the execution environment changes.')
    value.add_argument('--seeds', type=parse_seeds, default=[1], help='Comma-separated unique seeds (default: 1).')
    value.add_argument('--samples', type=int, help='AF2 monomer: 1; AF3 diffusion samples; ColabFold model count 1–5.')
    value.add_argument('--recycles', type=int, help='Default: AF3 10; other engines 3.')
    value.add_argument('--chunk-size', type=int, default=100, help='1–100 jobs per HTTP request (default: 100).')
    value.add_argument('--manifest', type=Path, help='Append-only acknowledgement JSONL; default: INPUT.submissions.jsonl.')
    value.add_argument('--resume', action='store_true', help='Skip exact accepted candidate/configuration fingerprints in the manifest.')
    value.add_argument('--dry-run', action='store_true', help='Validate and count input, print up to 3 examples; no HTTP or manifest writes.')
    value.add_argument('--limit', type=int, help='Examine only the first N FASTA records, including records skipped by resume.')
    return value


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    try:
        args.server = normalize_server(args.server)
        args.input = args.input.expanduser().resolve()
        args.manifest = (args.manifest or Path(str(args.input) + '.submissions.jsonl')).expanduser().resolve()
        if args.input == args.manifest:
            raise ValueError('Input FASTA and acknowledgement manifest must use different paths.')
        if not args.input.is_file():
            raise ValueError('Input FASTA does not exist.')
        if not 1 <= args.chunk_size <= 100:
            raise ValueError('--chunk-size must be from 1 to 100.')
        if args.limit is not None and args.limit < 1:
            raise ValueError('--limit must be positive.')
        if any(ord(c) < 32 for c in args.name_prefix):
            raise ValueError('--name-prefix must be printable text.')
        args.samples = args.samples if args.samples is not None else (1 if args.engine == 'alphafold2' else 5)
        args.recycles = args.recycles if args.recycles is not None else (10 if args.engine == 'alphafold3' else 3)
        if not 1 <= args.samples <= (5 if args.engine == 'colabfold' else 20):
            raise ValueError('--samples is outside the selected engine range.')
        if args.engine == 'alphafold2' and args.samples != 1:
            raise ValueError('Each record is an AF2 monomer; use --samples 1 and additional --seeds for extra runs.')
        if not 1 <= args.recycles <= 50:
            raise ValueError('--recycles must be from 1 to 50.')
        result = execute(args)
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
