"""Input semantics and bounded-memory scans; no model or network calls."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from scripts import screening_input as inputs
from scripts import submit_campaign as cli


class ScreeningInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def path(self, name, text):
        path = self.root / name
        path.write_text(text, encoding='utf-8')
        return path

    def items(self, text, kind='jsonl', **kwargs):
        return list(inputs.iter_targets(io.StringIO(text), kind, **kwargs))

    def test_fixed_chains_and_same_sequence_candidates_remain_independent(self):
        fixed = inputs.load_fixed_target(self.path('fixed.fa', '>heavy\nAC\n>light\nDE\n'))
        records = self.items('>candidate\nFGH\n>candidate\nFGH\n', 'fasta', fixed_chains=fixed)
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]['targetId'], records[1]['targetId'])
        self.assertEqual(records[0]['chains'], [{'id':'A','sequence':'AC'}, {'id':'B','sequence':'DE'}, {'id':'C','sequence':'FGH'}])
        records[0]['chains'][0]['sequence'] = 'XX'
        self.assertEqual(records[1]['chains'][0]['sequence'], 'AC')
        self.assertEqual(fixed[0]['sequence'], 'AC')

    def test_canonical_fixed_identity_matches_browser_compact_json(self):
        fixed = inputs.load_fixed_target(self.path('a.fa', '>old name\nac de\n'))
        other = inputs.load_fixed_target(self.path('b.fa', '>different name\nACDE\n'))
        self.assertEqual(fixed, other)
        expected = hashlib.sha256(b'[{"id":"A","sequence":"ACDE"}]').hexdigest()
        self.assertEqual(inputs.canonical_chains_sha(fixed), expected)

    def test_jsonl_unicode_bom_blank_lines_and_supported_per_target_settings(self):
        source = dict(targetId='complex-1', name='复合物', chains=[{'id':'X','sequence':'ac de'}, {'id':'YY','sequence':'FG'}],
                      settings={'numSamples':2,'numRecycles':7,'seeds':[2,8],'priority':'high','preset':'multimer'}, numSamples=3)
        path = self.path('complex.jsonl', '\ufeff\n' + json.dumps(source) + '\n\n')
        self.assertEqual(inputs.detect_format(path), 'jsonl')
        with path.open(encoding='utf-8-sig') as handle:
            item = next(inputs.iter_targets(handle, 'jsonl'))
        self.assertEqual(item['targetId'], 'complex-1')
        self.assertEqual(item['chains'][0], {'id':'X','sequence':'ACDE'})
        self.assertEqual(item['settings'], source['settings'])
        self.assertEqual(item['numSamples'], 3)

    def test_jsonl_fallback_ids_match_ui_and_sequence_shorthand_is_preserved_semantically(self):
        result = self.items(json.dumps({'sequence':'ACDE'}) + '\n' + json.dumps({'name':'two','sequence':'>one\nAC\n>two\nDE\n'}))
        self.assertEqual(result[0]['targetId'], inputs.target_id(1, 'target_1'))
        self.assertEqual(result[1]['targetId'], inputs.target_id(2, 'two'))
        self.assertEqual(result[1]['chains'], [{'id':'A','sequence':'AC'}, {'id':'B','sequence':'DE'}])

    def test_scientific_fields_unknown_settings_and_conflicting_inputs_are_rejected(self):
        baseline = {'chains':[{'id':'A','sequence':'ACDE'}]}
        for value in (
            dict(baseline, userCCD='chemical content'),
            {'chains':[{'id':'A','sequence':'ACDE','modifications':[]}]},
            {'chains':[{'id':'A','sequence':'ACDE','unpairedMsa':''}]},
            dict(baseline, sequence='ACDE'),
            dict(baseline, settings={'modelWeights':'other'}),
            dict(baseline, targetId='bad\nID'),
            dict(baseline, name=''),
            dict(baseline, numSamples=True),
            dict(baseline, seeds=[1,1]),
            dict(baseline, priority='urgent'),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.items(json.dumps(value))

    def test_limits_match_configured_engine_residue_and_chain_limits(self):
        with patch.dict(os.environ, {'PROTEIN_STUDIO_MAX_RESIDUES':'6'}):
            self.assertEqual(len(self.items('>one\nACDEFG\n', 'fasta')), 1)
            with self.assertRaisesRegex(ValueError, '6-residue'):
                self.items('>one\nACDEFGH\n', 'fasta')
            with self.assertRaisesRegex(ValueError, '6-residue'):
                self.items(json.dumps({'chains':[{'id':'A','sequence':'ACDE'}, {'id':'B','sequence':'FGH'}]}))
        twenty_six = [dict(id=chr(65+i),sequence='A') for i in range(26)]
        self.assertEqual(len(self.items(json.dumps({'chains':twenty_six}))[0]['chains']), 26)
        with self.assertRaises(ValueError):
            self.items(json.dumps({'chains':twenty_six+[dict(id='AA',sequence='A')]}))
        with self.assertRaises(ValueError):
            self.items(json.dumps({'chains':[dict(id='A',sequence='AC'),dict(id='A',sequence='DE')]}))
        with self.assertRaises(ValueError):
            self.items('>ambiguous\nABZ\n', 'fasta')

    def test_fixed_target_limits_and_jsonl_rejection(self):
        for text in ('', '>empty\n', ''.join(f'>c{i}\nA\n' for i in range(26))):
            with self.subTest(text=text), self.assertRaises(ValueError):
                inputs.load_fixed_target(self.path('fixed.fa', text))
        fixed = inputs.load_fixed_target(self.path('fixed.fa', ''.join(f'>c{i}\nA\n' for i in range(25))))
        item = self.items('>candidate\nACDE\n', 'fasta', fixed_chains=fixed)[0]
        self.assertEqual(item['chains'][-1]['id'], 'Z')
        with self.assertRaisesRegex(ValueError, 'only with candidate FASTA'):
            self.items('{"sequence":"A"}', fixed_chains=fixed)
        with self.assertRaisesRegex(ValueError, 'leaves no room'):
            inputs.load_fixed_target(self.path('fixed.fa', '>target\nACDE\n'), 4)

    def test_format_conflicts_empty_and_malformed_json_fail_early(self):
        path = self.path('wrong.jsonl', '>candidate\nACDE\n')
        with self.assertRaisesRegex(ValueError, 'extension indicates'):
            inputs.detect_format(path)
        self.assertEqual(inputs.detect_format(path, 'fasta'), 'fasta')
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            inputs.detect_format(path, 'jsonl')
        with self.assertRaisesRegex(ValueError, 'empty'):
            inputs.detect_format(self.path('empty.fa', '\n '))
        with self.assertRaisesRegex(ValueError, 'JSONL line 2'):
            self.items('{"sequence":"ACDE"}\n{malformed}')

    def test_input_iterator_does_not_read_ahead_entire_file(self):
        class NoReadAll(io.StringIO):
            def read(self, *args):
                raise AssertionError('must not call read-all')
        stream = NoReadAll('>first\nACDE\n>second\nFGHI\n>bad\n???\n')
        records = inputs.iter_targets(stream, 'fasta')
        self.assertEqual(next(records)['name'], 'first')
        self.assertEqual(next(records)['name'], 'second')
        with self.assertRaises(ValueError):
            next(records)

    def test_oversized_record_and_single_line_are_bounded(self):
        with self.assertRaisesRegex(ValueError, '5000-residue'):
            self.items('>large\n' + ('A'*1000+'\n')*6, 'fasta')
        with self.assertRaisesRegex(ValueError, 'record-line limit'):
            self.items('A'*(1024*1024+1), 'fasta')

    def test_100000_candidate_dry_scan_keeps_memory_bounded(self):
        path = self.root / '100000.fa'
        with path.open('w') as handle:
            for i in range(100000):
                handle.write(f'>candidate {i}\nACDEFGHIKLMNPQRSTVWY\n')
        fixed = self.path('fixed.fa', '>target\nACDE\n>partner\nFGHI\n')
        args = cli.parser().parse_args(['--input', str(path), '--fixed-target', str(fixed), '--engine', 'alphafold3', '--database-profile', 'test', '--dry-run'])
        tracemalloc.start()
        try:
            with patch.object(cli, 'request_json', side_effect=AssertionError('No network')):
                result = cli.run(args)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result['targets'], 100000)
        self.assertEqual(result['chunks'], 1000)
        self.assertEqual(result['fixedTargetChains'], 2)
        self.assertEqual(result['totalChains'], 300000)
        self.assertEqual(result['totalResidues'], 2800000)
        self.assertEqual(len(result['examples']), 2)
        self.assertLess(peak, 8*1024*1024, f'Peak Python allocations: {peak}')
        self.assertFalse(Path(str(path)+'.campaign.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
