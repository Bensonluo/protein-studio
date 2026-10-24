"""Scientific values, bounded PAE memory, paths, and top-model extraction."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from backend import engine, parsers, result_artifacts as artifacts


class ResultArtifactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def source(self, data, name='confidence.json'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def af3_sample(self, index, summary, full=None):
        name = f'seed-1_sample-{index}'
        directory = self.root / name
        directory.mkdir()
        path = directory / 'protein_model.pdb'
        path.write_text(
            'ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00 90.00           C  \n'
            'ATOM      2  CA  GLY A   2       1.000   0.000   0.000  1.00 80.00           C  \n')
        if summary is not None:
            self.source(summary, name + '/protein_summary_confidences.json')
        self.source(full or {'pae': [[0, index + 1], [3, 0]]}, name + '/protein_confidences.json')
        return path

    def test_float64_roundtrip_preserves_direction_precision_and_source_bytes(self):
        matrix = [[0, 0.12345678901234567], [31.749999999999996, 0]]
        source = self.source({'pae': matrix, 'token_chain_ids': ['A', 'B'], 'token_res_ids': [1, 1], 'ptm': .7})
        original = source.read_bytes()
        result = artifacts.stream_confidence(source, self.root, build_pae=True)
        desc = result['paeArtifact']
        self.assertEqual(result['pae'], matrix)
        self.assertEqual(artifacts.read_pae(desc, self.root)['values'], matrix)
        self.assertEqual(desc['axisKind'], 'token')
        self.assertEqual(desc['bytes'], 32)
        self.assertEqual(desc['sourceSha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(source.read_bytes(), original)
        binary = self.root / desc['storageRelativePath']
        self.assertEqual(desc['sha256'], hashlib.sha256(binary.read_bytes()).hexdigest())

    def test_afdb_single_object_list_and_scalar_only_mode(self):
        source = self.source([{'predicted_aligned_error': [[0, 2], [5, 0]], 'plddt': [91, 87], 'ptm': .9}])
        scalar = artifacts.stream_confidence(source, self.root)
        self.assertEqual(scalar, {'plddt': [91., 87.], 'ptm': .9})
        self.assertFalse((self.root / '.result-artifacts').exists())
        result = artifacts.stream_confidence(source, self.root, build_pae=True, axis_kind='residue')
        self.assertEqual(result['pae'], [[0, 2], [5, 0]])

    def test_chain_confidence_vectors_follow_af3_semantics(self):
        source = self.source({'ptm': .8, 'chain_ptm': [.82, .55], 'chain_iptm': [.71, .48]})
        result = artifacts.stream_confidence(source, self.root)
        self.assertEqual(result['chainPtm'], [.82, .55])
        self.assertEqual(result['chainIptm'], [.71, .48])
        trimmed = artifacts.stream_confidence(self.source({'chain_ptm': [.5] * 100}), self.root)
        self.assertEqual(len(trimmed['chainPtm']), 64)
        self.assertTrue(any('64-chain' in warning for warning in trimmed['warnings']))
        duplicate = self.root / 'duplicate-chains.json'
        duplicate.write_text('{"chain_ptm": [.5], "ptm": 0.9, "chain_ptm": [.6]}', encoding='utf-8')
        for bad in (self.source({'chain_ptm': [.5, 1.5]}),
                    self.source({'chain_iptm': [.5, 'x']}), duplicate):
            with self.assertRaises(ValueError):
                artifacts.stream_confidence(bad, self.root)
        parsed = parsers.parse_confidence(self.source({'chain_ptm': [.9, .4], 'chain_iptm': [.7, .3]}))
        self.assertEqual(parsed['chainPtm'], [.9, .4])
        self.assertEqual(parsed['chainIptm'], [.7, .3])
        self.assertNotIn('chainPtm', parsers.parse_confidence(self.source({'chain_ptm': [.9, 1.4]})))

    def test_interface_pae_means_follow_zhu_2023_convention(self):
        source = self.source({'pae': [[0, 2], [5, 0]], 'token_chain_ids': ['A', 'B']})
        result = artifacts.stream_confidence(source, self.root, build_pae=True)
        self.assertEqual(result['interfacePae'], [{'a': 'A', 'b': 'B', 'mean': 3.5}])
        single = artifacts.stream_confidence(
            self.source({'pae': [[0, 1], [2, 0]], 'token_chain_ids': ['A', 'A']}), self.root, build_pae=True)
        self.assertNotIn('interfacePae', single)
        self.assertNotIn('warnings', single)
        many = [f'c{index}' for index in range(65)]
        oversized = artifacts.stream_confidence(
            self.source({'pae': [[0.0] * 65] * 65, 'token_chain_ids': many}), self.root, build_pae=True)
        self.assertNotIn('interfacePae', oversized)
        self.assertTrue(any('Interface PAE summary skipped' in warning for warning in oversized['warnings']))
        parsed = parsers.parse_confidence(self.source({'pae': [[0, 2], [5, 0]], 'token_chain_ids': ['A', 'B']}))
        self.assertEqual(parsed['interfacePae'], [{'a': 'A', 'b': 'B', 'mean': 3.5}])
        self.assertNotIn('interfacePae', parsers.parse_confidence(
            self.source({'pae': [[0, 2], [5, 0]], 'token_chain_ids': ['A', 'B', 'C']})))
        self.assertNotIn('interfacePae', parsers.parse_confidence(
            self.source({'pae': [[0, 2], [5, 0]], 'token_chain_ids': [1, 2]})))

    def test_interface_ipsae_follows_dunbrack_2025_convention(self):
        result = artifacts.stream_confidence(
            self.source({'pae': [[0, 3], [3, 0]], 'token_chain_ids': ['A', 'B']}), self.root, build_pae=True)
        # 3 A < cutoff with one qualifying partner: d0 = 1.0, term 1/(1+3^2) = 0.1.
        self.assertEqual(result['ipSae'], [{'a': 'A', 'b': 'B', 'value': 0.1}])
        high = artifacts.stream_confidence(
            self.source({'pae': [[0, 30], [30, 0]], 'token_chain_ids': ['A', 'B']}), self.root, build_pae=True)
        self.assertEqual(high['ipSae'], [{'a': 'A', 'b': 'B', 'value': 0.0}])
        directed = artifacts.stream_confidence(
            self.source({'pae': [[0, 1], [9, 0]], 'token_chain_ids': ['A', 'B']}), self.root, build_pae=True)
        # A->B sees 1 A (score 0.5) while B->A sees 9 A (score 1/82): the pair keeps the max direction.
        self.assertEqual(directed['ipSae'], [{'a': 'A', 'b': 'B', 'value': 0.5}])
        sized = [[0.0] * 31 for _ in range(31)]
        for index in range(1, 31):
            sized[0][index] = sized[index][0] = 3.0
        grown = artifacts.stream_confidence(
            self.source({'pae': sized, 'token_chain_ids': ['A'] + ['B'] * 30}), self.root, build_pae=True)
        # 30 qualifying partners cross into the d0 formula: 1.24*cbrt(15)-1.8.
        self.assertAlmostEqual(grown['ipSae'][0]['value'], 0.1496, places=3)
        self.assertNotIn('ipSae', artifacts.stream_confidence(
            self.source({'pae': [[0, 1], [2, 0]], 'token_chain_ids': ['A', 'A']}), self.root, build_pae=True))
        parsed = parsers.parse_confidence(
            self.source({'pae': [[0, 3], [3, 0]], 'token_chain_ids': ['A', 'B']}))
        self.assertEqual(parsed['ipSae'], [{'a': 'A', 'b': 'B', 'value': 0.1}])
        self.assertNotIn('ipSae', parsers.parse_confidence(
            self.source({'pae': [[0, 2], [5, 0]], 'token_chain_ids': ['A', 'B', 'C']})))

    def test_interface_contacts_follow_afdb_pair_convention(self):
        residues = [{'chain': 'A', 'x': 0.0, 'y': 0.0, 'z': 0.0},
                    {'chain': 'A', 'x': 1.0, 'y': 0.0, 'z': 0.0},
                    {'chain': 'B', 'x': 10.0, 'y': 0.0, 'z': 0.0},
                    {'chain': 'B', 'x': 40.0, 'y': 0.0, 'z': 0.0}]
        close = parsers.interface_contacts([[0, 0, 5, 0], [0, 0, 5, 0], [5, 5, 0, 0], [0, 0, 0, 0]], residues)
        self.assertEqual(close, {'contacts': [{'a': 'A', 'b': 'B', 'pairs': 2,
                                               'resA': [[0, 1]], 'resB': [[2, 2]]}], 'truncated': False})
        # The pair qualifies on the lower directed PAE only: 3 vs 12.
        directed = parsers.interface_contacts([[0, 0, 3, 0], [0, 0, 12, 0], [12, 12, 0, 0], [0, 0, 0, 0]], residues)
        self.assertEqual(directed['contacts'], [{'a': 'A', 'b': 'B', 'pairs': 1,
                                                 'resA': [[0, 0]], 'resB': [[2, 2]]}])
        distant = parsers.interface_contacts([[0, 0, 5, 0], [0, 0, 5, 0], [5, 5, 0, 0], [0, 0, 0, 0]],
                                              [{'chain': 'A', 'x': 0.0, 'y': 0.0, 'z': 0.0},
                                               {'chain': 'A', 'x': 1.0, 'y': 0.0, 'z': 0.0},
                                               {'chain': 'B', 'x': 40.0, 'y': 0.0, 'z': 0.0},
                                               {'chain': 'B', 'x': 41.0, 'y': 0.0, 'z': 0.0}])
        self.assertEqual(distant['contacts'], [])
        alternating = []
        for step in range(600):
            alternating.extend(({'chain': 'A', 'x': 0.0, 'y': step * 100.0, 'z': 0.0},
                                {'chain': 'B', 'x': 10.0, 'y': step * 100.0, 'z': 0.0}))
        rows = []
        for index in range(1200):
            rows.append([5.0 if abs(column - index) == 1 and min(column, index) % 2 == 0 else 0.0
                         for column in range(1200)])
        crowded = parsers.interface_contacts(rows, alternating)
        self.assertTrue(crowded['truncated'])
        self.assertEqual(len(crowded['contacts'][0]['resA']), 512)

    def test_interface_lines_follow_chimerax_contacts_convention(self):
        # ChimeraX alphafold contacts: cross-chain pairs within 8 A get lines
        # colored by the pair's lower directed PAE - no PAE cutoff on distance.
        residues = [{'chain': 'A', 'x': 0.0, 'y': 0.0, 'z': 0.0},
                    {'chain': 'A', 'x': 1.0, 'y': 0.0, 'z': 0.0},
                    {'chain': 'B', 'x': 7.5, 'y': 0.0, 'z': 0.0},
                    {'chain': 'B', 'x': 40.0, 'y': 0.0, 'z': 0.0}]
        matrix = [[0, 0, 3, 0], [0, 0, 12, 0], [12, 12, 0, 0], [0, 0, 0, 0]]
        close = parsers.interface_contacts(matrix, residues, lines=True)
        # Distance-only selection: the 12 A-PAE pair still draws; its color
        # carries the confidence (ChimeraX has no PAE cutoff on lines).
        self.assertEqual(close['lines'], [{'i': 0, 'j': 2, 'pae': 3.0}, {'i': 1, 'j': 2, 'pae': 12.0}])
        self.assertFalse(close['linesTruncated'])
        # 9 A still counts as an ipSAE-style contact (15 A) but draws no line.
        beyond = parsers.interface_contacts(matrix,
                                            [{'chain': 'A', 'x': 0.0, 'y': 0.0, 'z': 0.0},
                                             {'chain': 'A', 'x': 1.0, 'y': 0.0, 'z': 0.0},
                                             {'chain': 'B', 'x': 9.5, 'y': 0.0, 'z': 0.0},
                                             {'chain': 'B', 'x': 40.0, 'y': 0.0, 'z': 0.0}], lines=True)
        self.assertEqual(beyond['contacts'][0]['pairs'], 1)
        self.assertEqual(beyond['lines'], [])
        # Pairs whose PAE cells are missing cannot be colored and are skipped.
        missing = parsers.interface_contacts([[None, None], [None, 0.0]],
                                              [{'chain': 'A', 'x': 0.0, 'y': 0.0, 'z': 0.0},
                                               {'chain': 'B', 'x': 1.0, 'y': 0.0, 'z': 0.0}], lines=True)
        self.assertEqual(missing['lines'], [])
        self.assertEqual(missing['contacts'], [])
        # A crowded 8 A lattice caps the exported lines honestly.
        crowded = []
        for step in range(600):
            crowded.extend(({'chain': 'A', 'x': 0.0, 'y': step * 1.0, 'z': 0.0},
                            {'chain': 'B', 'x': 7.0, 'y': step * 1.0, 'z': 0.0}))
        rows = [[5.0] * 1200 for _ in range(1200)]
        packed = parsers.interface_contacts(rows, crowded, lines=True)
        self.assertEqual(len(packed['lines']), 2048)
        self.assertTrue(packed['linesTruncated'])
        # The default call keeps the pre-lines contract shape.
        legacy = parsers.interface_contacts(matrix, residues)
        self.assertNotIn('lines', legacy)
        self.assertNotIn('linesTruncated', legacy)

    def test_attach_interface_contacts_requires_aligned_residue_axes(self):
        result = artifacts.stream_confidence(
            self.source({'pae': [[0, 3], [3, 0]]}), self.root, build_pae=True, axis_kind='residue')
        payload = {'residues': [{'chain': 'A', 'x': 0.0, 'y': 0.0, 'z': 0.0},
                                {'chain': 'B', 'x': 7.0, 'y': 0.0, 'z': 0.0}]}
        artifacts.attach_interface_contacts(payload, result['paeArtifact'], self.root)
        self.assertEqual(payload['interfaceContacts'], [{'a': 'A', 'b': 'B', 'pairs': 1,
                                                         'resA': [[0, 0]], 'resB': [[1, 1]]}])
        self.assertEqual(payload['interfaceLines'], [{'i': 0, 'j': 1, 'pae': 3.0}])
        # Row count differing from residues, or a token-kind axis where a
        # residue can span tokens, never guesses a join.
        for bad in ({'residues': payload['residues'][:1]}, {'residues': payload['residues'] + [None]}):
            artifacts.attach_interface_contacts(dict(bad), result['paeArtifact'], self.root)
            self.assertNotIn('interfaceContacts', bad)
            self.assertNotIn('interfaceLines', bad)
        token = artifacts.stream_confidence(
            self.source({'pae': [[0, 3], [3, 0]], 'token_chain_ids': ['A', 'B']}), self.root, build_pae=True)
        skipped = {'residues': payload['residues']}
        artifacts.attach_interface_contacts(skipped, token['paeArtifact'], self.root)
        self.assertNotIn('interfaceContacts', skipped)
        self.assertNotIn('interfaceLines', skipped)

    def test_invalid_matrices_never_publish_or_invent_values(self):
        bad = [[], [[0, 1]], [[0], [2, 0]], [[0, None], [1, 0]], [[0, True], [1, 0]],
               [[0, -1], [1, 0]], [[0, '1'], [1, 0]], [[[0], 1], [1, 0]]]
        for matrix in bad:
            with self.subTest(matrix=matrix):
                source = self.source({'pae': matrix})
                with self.assertRaises(ValueError):
                    artifacts.stream_confidence(source, self.root, build_pae=True)
                self.assertFalse(list((self.root / '.result-artifacts').glob('*.f64')))
                self.assertFalse(list((self.root / '.result-artifacts').glob('.pae-*')))
        for raw in ('{"pae":[[0,NaN],[1,0]]}', '{"pae":[[0,1e1000],[1,0]]}',
                    '{"pae":[[0]],"pae":[[1]]}', '[{"pae":[[0]]},{"pae":[[1]]}]'):
            source.write_text(raw)
            with self.assertRaises((ValueError, OverflowError)):
                artifacts.stream_confidence(source, self.root, build_pae=True)

    def test_shape_byte_limits_and_cancellation_cleanup(self):
        source = self.source({'pae': [[0, 1, 2]] * 3})
        with patch.object(artifacts, 'MAX_DIMENSION', 2):
            with self.assertRaisesRegex(ValueError, 'limit|dimension|Dimension'):
                artifacts.stream_confidence(source, self.root, build_pae=True)
        with patch.object(artifacts, 'MAX_SOURCE_BYTES', 2):
            with self.assertRaisesRegex(ValueError, 'source limit'):
                artifacts.stream_confidence(source, self.root, build_pae=True)
        with self.assertRaises(engine.PredictionCancelled):
            artifacts.stream_confidence(source, self.root, build_pae=True, cancelled=lambda: True)
        self.assertFalse(list((self.root / '.result-artifacts').glob('.pae-*')))

    def test_optional_limits_keep_structure_metrics_and_original_download(self):
        directory = self.root / 'seed-1_sample-0'
        directory.mkdir()
        directory.joinpath('protein_model.pdb').write_text(
            'ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00 90.00           C  \n')
        self.source({'ranking_score': .9, 'ptm': .8}, 'seed-1_sample-0/protein_summary_confidences.json')
        source = self.source({'pae': [[0, 1, 2]] * 3}, 'seed-1_sample-0/protein_confidences.json')
        with patch.object(artifacts, 'MAX_DIMENSION', 2):
            result = engine._collect_outputs(self.root, 'alphafold3', self.root, [{'id': 'A', 'sequence': 'A'}])
        self.assertTrue(Path(result['structurePath']).is_file())
        self.assertEqual(result['ptm'], .8)
        self.assertEqual(result['rankingScore'], .9)
        self.assertIsNone(result['pae'])
        self.assertEqual(result['paeArtifact']['status'], 'unavailable')
        self.assertIn(str(source), [item['path'] for item in result['artifacts']])
        with patch.object(artifacts, 'MAX_DIMENSION', 2):
            scalar = artifacts.stream_confidence(self.source({'plddt': [90, 80, 70], 'ptm': .7}), self.root)
        self.assertEqual(scalar['ptm'], .7)
        self.assertNotIn('plddt', scalar)
        self.assertTrue(scalar['warnings'])

    def test_preview_indices_exact_tiles_and_strict_bounds(self):
        matrix = [[float(row * 10 + col) for col in range(9)] for row in range(9)]
        descriptor = artifacts.stream_confidence(self.source({'pae': matrix}), self.root, build_pae=True)['paeArtifact']
        preview = artifacts.read_pae(descriptor, self.root, max_size=3)
        self.assertEqual(preview['rowIndices'], [1, 4, 7])
        self.assertEqual(preview['colIndices'], [1, 4, 7])
        self.assertEqual(preview['values'], [[matrix[r][c] for c in [1, 4, 7]] for r in [1, 4, 7]])
        tile = artifacts.read_pae(descriptor, self.root, row_start=2, col_start=3, rows=2, cols=3)
        self.assertEqual(tile['method'], 'exact')
        self.assertEqual(tile['values'], [row[3:6] for row in matrix[2:4]])
        for args in ({'max_size': 257}, {'max_size': True}, {'row_start': -1, 'col_start': 0, 'rows': 1, 'cols': 1},
                     {'row_start': 0}, {'row_start': 8, 'col_start': 0, 'rows': 2, 'cols': 1}):
            with self.assertRaises(ValueError):
                artifacts.read_pae(descriptor, self.root, **args)

    def test_path_rebasing_registration_and_mutated_source_rejection(self):
        source = self.source({'pae': [[0, 1], [2, 0]]}, 'attempt/confidence.json')
        result = artifacts.stream_confidence(source, source.parent, build_pae=True)
        artifacts.compact_pae(result, self.root)
        desc = result['paeArtifact']
        self.assertTrue(desc['storageRelativePath'].startswith('attempt/'))
        with self.assertRaisesRegex(ValueError, 'registered'):
            artifacts.publish_pae_urls(result, self.root, '/api/jobs/one')
        result['artifacts'] = [dict(name=Path(a['path']).relative_to(self.root).as_posix()) for a in result['artifacts']]
        artifacts.publish_pae_urls(result, self.root, '/api/jobs/one')
        self.assertNotIn('storagePath', desc)
        self.assertNotIn('sourcePath', desc)
        self.assertEqual(desc['previewUrl'], '/api/jobs/one/pae?maxSize=256')
        self.assertEqual(artifacts.read_pae(desc, self.root)['values'], [[0, 1], [2, 0]])
        source.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'source changed'):
            artifacts.read_pae(desc, self.root)

    def test_symlinks_and_parent_escape_are_rejected(self):
        source = self.source({'pae': [[0]]})
        link = self.root / 'link.json'
        link.symlink_to(source)
        with self.assertRaisesRegex(ValueError, 'symbolic'):
            artifacts.stream_confidence(link, self.root, build_pae=True)
        result = artifacts.stream_confidence(source, self.root, build_pae=True)
        desc = copy.deepcopy(result['paeArtifact'])
        desc.pop('storagePath')
        desc['storageRelativePath'] = '../outside.f64'
        with self.assertRaises(ValueError):
            artifacts.read_pae(desc, self.root)

    def test_large_inline_compacts_before_copy_and_repeated_call_is_idempotent(self):
        n = 129
        matrix = [[float(row + col) for col in range(n)] for row in range(n)]
        result = {'pae': matrix}
        self.assertIs(artifacts.compact_pae(result, self.root), result)
        self.assertIsNone(result['pae'])
        descriptor = copy.deepcopy(result['paeArtifact'])
        count = len(result['artifacts'])
        artifacts.compact_pae(result, self.root)
        self.assertEqual(result['paeArtifact'], descriptor)
        self.assertEqual(len(result['artifacts']), count)
        self.assertEqual(artifacts.read_pae(descriptor, self.root, row_start=128, col_start=127, rows=1, cols=2)['values'], [[255., 256.]])

    def test_million_cell_source_and_preview_have_bounded_python_memory(self):
        n = 1024
        source = self.root / 'large.json'
        row = '[' + ','.join('0.12345678901234567' if col % 2 else '31.75' for col in range(n)) + ']'
        with source.open('w') as stream:
            stream.write('{"pae":[')
            for index in range(n):
                stream.write((',' if index else '') + row)
            stream.write(']}')
        tracemalloc.start()
        try:
            result = artifacts.stream_confidence(source, self.root, build_pae=True)
            preview = artifacts.read_pae(result['paeArtifact'], self.root)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertIsNone(result['pae'])
        self.assertEqual(result['paeArtifact']['bytes'], n * n * 8)
        self.assertEqual(preview['shape'], [256, 256])
        self.assertLess(peak, 16 * 1024 * 1024, f'Unexpected N² Python object retention: {peak}')

    def test_engine_indexes_only_ranked_top_model_and_keeps_small_rank_records(self):
        scores = [.6, .9, -.1, .1, .7]
        for index, rank in enumerate(scores):
            self.af3_sample(index, {'ranking_score': rank, 'ptm': .7})
        actual = engine.stream_confidence
        calls = []
        def track(path, *args, **kwargs):
            calls.append((Path(path), kwargs.get('build_pae')))
            return actual(path, *args, **kwargs)
        with patch.object(engine, 'stream_confidence', side_effect=track):
            result = engine._collect_outputs(self.root, 'alphafold3', self.root, [{'id': 'A', 'sequence': 'AG'}])
        self.assertEqual(result['pae'], [[0, 2], [3, 0]])
        full_reads = [path for path, build in calls if path.name == 'protein_confidences.json']
        self.assertEqual(len(full_reads), 1)
        self.assertIn('sample-1', str(full_reads[0]))
        self.assertEqual(result['rankingMetric'], 'reported ranking score')
        self.assertEqual([m['rankingScore'] for m in result['rankedModels']], sorted(scores, reverse=True))
        self.assertTrue(all(not build for path, build in calls if path.name.endswith('_summary_confidences.json')))
        self.assertTrue(all('pae' not in model and 'residues' not in model for model in result['rankedModels']))

    def test_worker_scalar_collection_has_no_full_reads_or_derived_artifacts(self):
        for index, rank in enumerate([.4, .8, .6]):
            self.af3_sample(index, {'ranking_score': rank, 'ptm': .7})
        actual = engine.stream_confidence
        calls = []
        def track(path, *args, **kwargs):
            self.assertTrue(Path(path).name.endswith('_summary_confidences.json'))
            self.assertFalse(kwargs['build_pae'])
            calls.append(Path(path))
            return actual(path, *args, **kwargs)
        with patch.object(engine, 'stream_confidence', side_effect=track), \
                patch.object(engine, 'compact_pae', side_effect=AssertionError('Worker must not compact PAE')):
            result = engine._collect_outputs(self.root, 'alphafold3', self.root,
                [{'id': 'A', 'sequence': 'AG'}], build_pae=False)
        self.assertEqual(result['rankingScore'], .8)
        self.assertEqual(result['plddt'], [90., 80.])
        self.assertEqual(len(result['rankedModels']), 3)
        self.assertIsNone(result['pae'])
        self.assertNotIn('paeArtifact', result)
        self.assertTrue(calls)
        self.assertFalse((self.root / '.result-artifacts').exists())

    def test_incomplete_or_invalid_summaries_fall_back_without_matrix_indexing(self):
        cases = [None, {}, {'ptm': .7}, {'ranking_score': 'not-a-number'},
                 {'ranking_score': .1, 'padding': 'x' * (1024 * 1024)}]
        for index, summary in enumerate(cases):
            path = self.af3_sample(index, summary,
                {'ranking_score': .95, 'ptm': .9, 'iptm': .8, 'plddt': [92, 88], 'pae': [[0, 1], [2, 0]]})
            with self.subTest(summary=index), patch.object(engine, 'stream_confidence', wraps=engine.stream_confidence) as read:
                metrics = engine._metrics_for(path, 'alphafold3', artifact_root=self.root)
            full_calls = [c for c in read.call_args_list if Path(c.args[0]).name == 'protein_confidences.json']
            self.assertEqual(len(full_calls), 1)
            self.assertFalse(full_calls[0].kwargs['build_pae'])
            self.assertEqual(metrics['rankingScore'], .95)
            self.assertEqual(metrics['ptm'], .7 if index == 2 else .9)
            self.assertEqual(metrics['plddt'], [92., 88.])
        self.assertFalse((self.root / '.result-artifacts').exists())

    def test_valid_summary_iptm_ptm_preserves_common_metric_without_full_reads(self):
        for index, iptm in enumerate([.3, .8]):
            self.af3_sample(index, {'iptm': iptm, 'ptm': .5})
        actual = engine.stream_confidence
        def summary_only(path, *args, **kwargs):
            self.assertTrue(Path(path).name.endswith('_summary_confidences.json'))
            return actual(path, *args, **kwargs)
        with patch.object(engine, 'stream_confidence', side_effect=summary_only):
            result = engine._collect_outputs(self.root, 'alphafold3', self.root, build_pae=False)
        self.assertEqual(result['rankingMetric'], '0.8 ipTM + 0.2 pTM')
        self.assertIn('sample-1', result['structurePath'])
        self.assertEqual(result['iptm'], .8)


if __name__ == '__main__':
    unittest.main()
