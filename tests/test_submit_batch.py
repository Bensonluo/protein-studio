"""Standard-library tests for the streaming batch client; no model or GPU calls."""
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts import submit_batch as batch


class BatchClientTests(unittest.TestCase):
    def invoke(self, arguments):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = batch.main(arguments)
        return code, json.loads(out.getvalue()) if code == 0 else None, err.getvalue()

    def test_multiline_records_are_independent_and_preserve_full_names(self):
        records = list(batch.iter_fasta(StringIO('>candidate A\nac de\nFG\n\n>candidate B\nACDEFG\n')))
        self.assertEqual([r.name for r in records], ['candidate A', 'candidate B'])
        self.assertEqual([r.sequence for r in records], ['ACDEFG', 'ACDEFG'])

    def test_invalid_fasta_is_rejected(self):
        for text in ('ACD\n', '>empty\n', '>one\nACD:X\n', '>\nACD\n', '>one\nAXZ\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                list(batch.iter_fasta(StringIO(text)))

    def test_parser_does_not_read_all_input(self):
        class LinesOnly:
            def __iter__(self):
                yield '>one\n'
                yield 'ACDE\n'
                yield '>two\n'
                raise AssertionError('Consumed more than the first record.')
        record = next(batch.iter_fasta(LinesOnly()))
        self.assertEqual(record.sequence, 'ACDE')

    def test_server_and_seed_validation(self):
        self.assertEqual(batch.normalize_server('localhost'), 'http://localhost:8765')
        self.assertEqual(batch.normalize_server('127.0.0.1:1234'), 'http://127.0.0.1:1234')
        self.assertEqual(batch.parse_seeds('0,12,31'), [0, 12, 31])
        for server in ('file:///tmp/test', 'http://user:password@localhost', 'http://localhost/api', 'http://localhost:bad'):
            with self.subTest(server=server), self.assertRaises(ValueError):
                batch.normalize_server(server)

    def test_dry_run_counts_without_network_or_manifest_write(self):
        with tempfile.TemporaryDirectory() as temp:
            fasta = Path(temp) / 'input.fasta'
            fasta.write_text('>first\nACDE\n>second\nACDE\n>third\nFGHI\n')
            with patch.object(batch, 'post_batch', side_effect=AssertionError('No HTTP in dry-run')):
                code, result, error = self.invoke(['--input', str(fasta), '--engine', 'alphafold2', '--dry-run', '--limit', '2', '--chunk-size', '1'])
            self.assertEqual((code, error), (0, ''))
            self.assertEqual(result['recordsRead'], 2)
            self.assertEqual(result['plannedJobs'], 2)
            self.assertEqual(result['chunks'], 2)
            self.assertFalse(Path(str(fasta) + '.submissions.jsonl').exists())
            self.assertNotEqual(result['preview'][0]['fingerprint'], result['preview'][1]['fingerprint'])

    def test_chunk_and_af2_monomer_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            fasta = Path(temp) / 'input.fasta'
            fasta.write_text('>one\nACDE\n')
            for options in (['--chunk-size', '101'], ['--samples', '2'], ['--recycles', '0'], ['--limit', '0']):
                with self.subTest(options=options):
                    code, _, _ = self.invoke(['--input', str(fasta), '--engine', 'alphafold2', '--dry-run', *options])
                    self.assertEqual(code, 1)

    def test_http_chunks_acknowledgements_and_resume(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                if self.path != '/api/jobs/batch':
                    self.send_error(404)
                    return
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append(payload['jobs'])
                start = sum(len(x) for x in received[:-1])
                data = json.dumps({'jobs': [{'id': f'job-{start + i}'} for i in range(len(payload['jobs']))],
                                   'count': len(payload['jobs'])}).encode()
                self.send_response(201)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                fasta = Path(temp) / 'input.fasta'
                # Same sequence with different names must remain distinct; even
                # identical duplicate records have separate occurrence identities.
                fasta.write_text('>duplicate\nACDE\n>duplicate\nACDE\n' + ''.join(f'>candidate-{i}\nACDE\n' for i in range(203)))
                args = ['--input', str(fasta), '--engine', 'alphafold2', '--server', f'http://127.0.0.1:{server.server_port}']
                code, result, _ = self.invoke(args)
                self.assertEqual(code, 0)
                self.assertEqual([len(x) for x in received], [100, 100, 5])
                self.assertEqual(result['acceptedJobs'], 205)
                entries = [json.loads(x) for x in Path(result['manifest']).read_text().splitlines()]
                self.assertEqual(len({x['fingerprint'] for x in entries}), 205)
                self.assertEqual(len({x['inputSha256'] for x in entries}), 1)
                self.assertEqual([x['occurrence'] for x in entries[:2]], [1, 2])
                self.assertTrue(all(len(job['chains']) == 1 for chunk in received for job in chunk))
                code, result, _ = self.invoke(args + ['--resume'])
                self.assertEqual(code, 0)
                self.assertEqual(result['skippedPreviouslyAccepted'], 205)
                self.assertEqual(result['acceptedJobs'], 0)
                self.assertEqual(len(received), 3)
                code, result, _ = self.invoke(args + ['--resume', '--seeds', '2', '--limit', '1'])
                self.assertEqual((code, result['acceptedJobs']), (0, 1))
                self.assertEqual(len(received), 4)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            fasta = Path(temp) / 'input.fasta'
            fasta.write_text('>one\nACDE\n')
            with patch.object(batch, 'post_batch', side_effect=RuntimeError('Unknown outcome')) as request:
                code, _, error = self.invoke(['--input', str(fasta), '--engine', 'alphafold2'])
            self.assertEqual(code, 1)
            self.assertIn('Unknown outcome', error)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(Path(str(fasta) + '.submissions.jsonl').read_text(), '')


if __name__ == '__main__':
    unittest.main()
