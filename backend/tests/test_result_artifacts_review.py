"""Independent scientific-parser and changed-file regressions; temp fixtures only."""
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from backend import result_artifacts as artifacts


class ScientificArtifactReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root/'confidence.json'

    def parse(self, value):
        self.source.write_text(json.dumps(value))
        return artifacts.stream_confidence(self.source, self.root, build_pae=True)

    def test_fractional_token_residue_ids_are_not_silently_truncated(self):
        with self.assertRaises(ValueError):
            self.parse({'pae': [[0]], 'token_chain_ids': ['A'], 'token_res_ids': [1.75]})

    def test_objects_and_literal_dotted_keys_cannot_masquerade_as_vectors(self):
        for value in [
            {'plddt': {'item': 95}},
            {'pae': [[0]], 'token_res_ids': {'item': 2}},
            {'pae': [[0]], 'token_chain_ids': {'item': 'A'}},
            {'plddt.item': 95},
            {'pae': [[0]], 'token_res_ids.item': 1},
        ]:
            with self.subTest(value=value):
                try:
                    parsed = self.parse(value)
                except ValueError:
                    continue
                self.assertNotIn('plddt', parsed)
                self.assertNotIn('axisMetadata', parsed.get('paeArtifact', {}))

    def test_duplicate_scientific_fields_cannot_override_prior_values(self):
        for raw in ['{"plddt":[10],"plddt":[99]}', '{"ptm":0.1,"ptm":0.9}',
                    '{"pae":[[0]],"token_res_ids":[1],"token_res_ids":[2]}',
                    '{"pae":[[0]],"predicted_aligned_error":[[8]]}']:
            with self.subTest(raw=raw):
                self.source.write_text(raw)
                with self.assertRaises(ValueError):
                    artifacts.stream_confidence(self.source, self.root, build_pae=True)

    def test_replacement_after_descriptor_check_cannot_be_returned_as_original_values(self):
        descriptor = self.parse({'pae': [[0, 1], [7, 0]]})['paeArtifact']
        original_validate = artifacts._validate_descriptor
        replacement = self.root/'replacement.f64'
        replacement.write_bytes(struct.pack('<4d', 10., 11., 17., 10.))
        swapped = False
        def validate_then_replace(*args, **kwargs):
            nonlocal swapped
            path = original_validate(*args, **kwargs)
            if not swapped:
                os.replace(replacement, path)
                swapped = True
            return path
        with patch.object(artifacts, '_validate_descriptor', side_effect=validate_then_replace):
            with self.assertRaises(ValueError):
                artifacts.read_pae(descriptor, self.root, row_start=0, col_start=0, rows=2, cols=2)

    def test_source_changes_after_descriptor_check_invalidate_the_read(self):
        descriptor = self.parse({'pae': [[0, 1], [7, 0]]})['paeArtifact']
        original_validate = artifacts._validate_descriptor
        changed = False
        def validate_then_change_source(*args, **kwargs):
            nonlocal changed
            path = original_validate(*args, **kwargs)
            if not changed:
                self.source.write_text('{"pae":[[0,3],[9,0]]}')
                changed = True
            return path
        with patch.object(artifacts, '_validate_descriptor', side_effect=validate_then_change_source):
            with self.assertRaises(ValueError):
                artifacts.read_pae(descriptor, self.root, row_start=0, col_start=0, rows=2, cols=2)


if __name__ == '__main__':unittest.main()
