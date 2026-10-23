#!/usr/bin/env python3
"""Synthetic PAE storage/read benchmark. No prediction or GPU benchmark.

Generate a source larger than the old confidence-parser cap without keeping
that source or the complete matrix in Python memory. Temporary files are removed.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import resource
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from backend.result_artifacts import stream_confidence, read_pae


def value(row, col):
    return 0.123456789012345 + row * .001 + col * .000001


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--size', type=int, default=2048)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if not 129 <= args.size <= 5000:
        parser.error('--size must be between 129 and 5000')
    with tempfile.TemporaryDirectory(prefix='foldspace-pae-benchmark-') as directory:
        root = Path(directory).resolve()
        source = root/'synthetic-confidence.json'
        with source.open('w') as stream:
            stream.write('{"ptm":0.8,"pae":[')
            for row in range(args.size):
                if row:
                    stream.write(',')
                json.dump([value(row, col) for col in range(args.size)], stream, separators=(',', ':'))
            stream.write(']}')
        started = time.monotonic()
        result = stream_confidence(source, root, build_pae=True)
        indexed = time.monotonic()
        descriptor = result['paeArtifact']
        assert result['pae'] is None
        preview = read_pae(descriptor, root, max_size=256)
        preview_finished = time.monotonic()
        tile = read_pae(descriptor, root, row_start=7, col_start=20, rows=2, cols=3)
        assert tile['values'] == [[value(row, col) for col in range(20, 23)] for row in range(7, 9)]
        for row, values in zip(preview['rowIndices'], preview['values']):
            assert values == [value(row, col) for col in preview['colIndices']]
        report = dict(date=datetime.datetime.now(datetime.timezone.utc).isoformat(), status='passed',
                      scope='Synthetic JSON preprocessing and filesystem reads only. No MSA, protein prediction, GPU or cloud performance measurement.',
                      python=sys.version.split()[0], sourceShape=[args.size, args.size],
                      moduleSha256=hashlib.sha256((ROOT/'backend/result_artifacts.py').read_bytes()).hexdigest(),
                      sourceBytes=source.stat().st_size, sourceSha256=descriptor['sourceSha256'],
                      binaryBytes=descriptor['bytes'], binarySha256=descriptor['sha256'],
                      compactDescriptorBytes=len(json.dumps(descriptor).encode()),
                      indexedSeconds=round(indexed-started,3), previewSeconds=round(preview_finished-indexed,3),
                      previewShape=preview['shape'], exactValuesVerified=True,
                      peakRssPlatformUnits=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                      peakRssUnit='bytes' if sys.platform=='darwin' else 'KiB on Linux; platform-specific elsewhere',
                      temporaryFilesRemoved=True)
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
