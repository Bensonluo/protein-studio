"""Credential-safe URL composition for root and prefixed worker controllers."""
import unittest
from unittest.mock import Mock
from backend.worker_client import WorkerClient, worker_request_url


class WorkerProxyPathTests(unittest.TestCase):
    def test_root_and_prefixed_paths_resolve_once(self):
        self.assertEqual(worker_request_url('https://example.org', '/worker/v1/claim'),
                         'https://example.org/worker/v1/claim')
        for base in ('https://example.org/foldspace', 'https://example.org/foldspace/'):
            for path in ('/worker/v1/prepared/abc?x=1', '/foldspace/worker/v1/prepared/abc?x=1'):
                with self.subTest(base=base,path=path):
                    self.assertEqual(worker_request_url(base, path), 'https://example.org/foldspace/worker/v1/prepared/abc?x=1')

    def test_external_and_escaped_paths_are_rejected_before_transport(self):
        client = WorkerClient.__new__(WorkerClient)
        client.url = 'https://example.org/foldspace'; client.token = 'fixture-token-not-a-credential'
        client.http_timeout = 1; client.opener = Mock()
        for path in ('https://evil.example/worker/v1/claim', '//evil.example/worker/v1/claim',
                     'https://example.org/foldspace/worker/v1/claim', '/other/worker/v1/claim',
                     '/foldspacex/worker/v1/claim', '/worker/v1/../../api/jobs',
                     '/worker/v1/%2e%2e/api/jobs', '/worker/v1/claim#fragment',
                     '/worker/v1/claim\n', '/worker/v1/\\evil'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                client.request(path)
        client.opener.open.assert_not_called()

    def test_request_accepts_server_returned_prefixed_url_without_double_mount(self):
        client = WorkerClient.__new__(WorkerClient)
        client.url = 'https://example.org/foldspace'; client.token = 'fixture-token-not-a-credential'
        client.http_timeout = 17; client.opener = Mock()
        client.request('/foldspace/worker/v1/result/abc', headers={'X-Lease-Token':'fixture-lease'}, data=b'fixture')
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://example.org/foldspace/worker/v1/result/abc')
        self.assertEqual(request.get_header('Authorization'), 'Bearer fixture-token-not-a-credential')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(client.opener.open.call_args.kwargs['timeout'], 17)


if __name__ == '__main__':
    unittest.main()
