"""Raw A3M scientific definitions and bounded-memory QC, without AF3/GPU."""
import hashlib
import json
from pathlib import Path
import tempfile
import tracemalloc
import unittest

from backend.engine import PredictionCancelled
from backend.msa_metrics import summarize_a3m


class MsaMetricsTests(unittest.TestCase):
    def test_exact_coverage_gap_fraction_and_nearest_rank(self):
        text = '>query\nACDE\n>half\nA--E\n>empty-hit\n----\n>full\nACDE\n'
        qc = summarize_a3m(text, 'ACDE', 'unpaired')
        self.assertEqual((qc['rawRows'], qc['hitRows']), (4, 3))
        self.assertEqual(qc['coverage']['mean'], .5)
        self.assertEqual([qc['coverage'][key] for key in ('p10', 'p50', 'p90')], [0, .5, 1])
        self.assertEqual(qc['coverage']['zeroRows'], 1)
        self.assertEqual(qc['coverage']['fullRows'], 1)
        self.assertEqual(qc['gapFraction'], .5)
        self.assertEqual(qc['alignedQueryLength'], 4)
        self.assertEqual(qc['status'], 'available')
        self.assertNotIn('finalPairedRows', qc)
        self.assertNotIn('Neff', json.dumps(qc))

    def test_insertions_and_unknown_residues_are_not_identity(self):
        text = '>q\nAqqCDE\n>inserted\nAaa--E\n>unknown\nX--Z\n'
        qc = summarize_a3m(text, 'ACDE', 'unpaired')
        self.assertEqual(qc['insertedResidues'], 2)  # query insertions excluded
        self.assertEqual(qc['nonStandardAlignedResidues'], 2)
        self.assertEqual(qc['coverage']['mean'], .5)
        self.assertEqual(qc['gapFraction'], .5)
        self.assertIn('Not sequence identity', qc['coverage']['definition'])

    def test_pairing_candidates_keep_duplicates_and_do_not_claim_final_pairs(self):
        text = '>q\nACDE\n>sp|one TaxID=9606\nACDE\n>sp|two TaxID=9606\nACDE\n'
        qc = summarize_a3m(text, 'ACDE', 'pairing-candidates')
        self.assertEqual(qc['role'], 'pairing-candidates')
        self.assertEqual(qc['scope'], 'raw-pre-featurisation')
        self.assertEqual(qc['rawRows'], 3)
        self.assertEqual(qc['hitRows'], 2)
        self.assertIsNone(qc['finalPairedRows'])
        self.assertEqual(qc['coverage']['fullRows'], 2)

    def test_explicit_empty_and_query_only_are_distinct_without_fake_coverage(self):
        for source in ('', iter(()), iter(('', ''))):
            qc = summarize_a3m(source, 'ACDE', 'pairing-candidates')
            self.assertEqual(qc['status'], 'explicit-empty')
            self.assertEqual((qc['rawRows'], qc['hitRows']), (0, 0))
            self.assertFalse(qc['queryPresent'])
            self.assertIsNone(qc['alignedQueryLength'])
            self.assertEqual(qc['sourceSha256'], hashlib.sha256(b'').hexdigest())
            self.assertIsNone(qc['coverage']['mean'])
            self.assertIsNone(qc['gapFraction'])
        qc = summarize_a3m('>q\nACDE', 'ACDE', 'unpaired')
        self.assertEqual(qc['status'], 'query-only')
        self.assertEqual((qc['rawRows'], qc['hitRows']), (1, 0))
        self.assertTrue(qc['queryPresent'])
        self.assertEqual(qc['alignedQueryLength'], 4)
        self.assertTrue(all(qc['coverage'][key] is None for key in ('mean', 'p10', 'p50', 'p90')))
        self.assertIsNone(qc['gapFraction'])

    def test_wrapped_crlf_and_chunk_boundaries_preserve_exact_original_utf8(self):
        text = '  >查询 metadata\r\n AC\r\nDE  \r\n>hit\r\nAxx-\r\n-E\r\n'
        expected = summarize_a3m(text, 'ACDE', 'unpaired')
        original = text.encode('utf-8')
        self.assertEqual(expected['sourceSha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(expected['sourceUtf8Bytes'], len(original))
        self.assertEqual(summarize_a3m(iter(text), 'ACDE', 'unpaired'), expected)
        self.assertEqual(summarize_a3m((text[i:i + 7] for i in range(0, len(text), 7)), 'ACDE', 'unpaired'), expected)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'unchanged.a3m'
            path.write_bytes(original)
            with path.open('r', encoding='utf-8', newline='') as stream:
                self.assertEqual(summarize_a3m(stream, 'ACDE', 'unpaired'), expected)
            self.assertEqual(path.read_bytes(), original)

    def test_line_iterables_never_invent_newline_bytes(self):
        with self.assertRaises(ValueError):
            summarize_a3m(iter(['>query', 'ACDE', '>hit', 'A--E']), 'ACDE', 'unpaired')
        qc = summarize_a3m(iter(['>q\n', 'ACDE\n']), 'ACDE', 'unpaired')
        self.assertEqual(qc['sourceSha256'], hashlib.sha256(b'>q\nACDE\n').hexdigest())

    def test_malformed_source_and_invalid_configuration_are_rejected(self):
        bad = [' \n\r\n', 'ACDE', '>q\nACDF', '>q\nACD-', '>q\nACD', '>q\nACDEE',
               '>q\nACDE\n>dangling', '>q\nACDE\n>h\nA---E', '>q\nACDE\n>h\nA.HE',
               '>q\nACDE\n>h\nAC1E', '>q\nACDE\n>h\nAC DE', '>q\nACDE\n>h\nAéDE',
               '>q\nACDE\n>h\n>next\nACDE']
        for text in bad:
            with self.subTest(text=text), self.assertRaises(ValueError):
                summarize_a3m(text, 'ACDE', 'unpaired')
        for query in ('', 'acde', 'ACDX', None):
            with self.subTest(query=query), self.assertRaises(ValueError):
                summarize_a3m('', query, 'unpaired')
        for role in ('paired', 'unrecognised', {}, None):
            with self.subTest(role=role), self.assertRaises(ValueError):
                summarize_a3m('', 'ACDE', role)
        for source in (b'>q\nACDE\n', None, iter(['>q\n', 123])):
            with self.assertRaises(ValueError):
                summarize_a3m(source, 'ACDE', 'unpaired')

    def test_cancellation_is_checked_even_for_empty_generator_chunks(self):
        with self.assertRaises(PredictionCancelled):
            summarize_a3m('>q\nACDE', 'ACDE', 'unpaired', cancelled=lambda: True)
        count = 0
        def chunks():
            nonlocal count
            yield '>q\nACDE\n'
            while True:
                count += 1
                yield ''
        with self.assertRaises(PredictionCancelled):
            summarize_a3m(chunks(), 'ACDE', 'unpaired', cancelled=lambda: count >= 4)
        self.assertEqual(count, 4)

    def test_hundred_thousand_rows_have_bounded_memory_and_exact_counts(self):
        count = 100_000
        def rows():
            yield '>query\nACDEFGHI\n'
            for index in range(count):
                yield f'>hit-{index}\n'
                yield 'ACDE----\n' if index % 2 else 'ACDEFGHI\n'
        tracemalloc.start()
        try:
            qc = summarize_a3m(rows(), 'ACDEFGHI', 'pairing-candidates')
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual((qc['rawRows'], qc['hitRows']), (count + 1, count))
        self.assertEqual(qc['coverage']['mean'], .75)
        self.assertEqual(qc['coverage']['p50'], .5)
        self.assertEqual(qc['coverage']['p90'], 1)
        self.assertEqual(qc['gapFraction'], .25)
        self.assertLess(peak, 2 * 1024 * 1024, f'MSA row retention is not bounded: {peak} bytes')

    def test_large_existing_text_does_not_create_a_full_splitlines_copy(self):
        text = '>query\nACDE\n' + '>hit\nACDE\n' * 100_000
        tracemalloc.start()
        try:
            qc = summarize_a3m(text, 'ACDE', 'unpaired')
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(qc['rawRows'], 100_001)
        self.assertLess(peak, 2 * 1024 * 1024, f'Existing text was duplicated: {peak} bytes')


if __name__ == '__main__':
    unittest.main()
