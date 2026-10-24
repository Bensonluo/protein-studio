#!/usr/bin/env python3
"""Read-only public reference demo; never expose a personal workspace.

Seed bundled public structures through internal APIs before binding HTTP. There
is no public write window, worker listener, external model, or GPU execution.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.server import StudioApplication, StudioHandler, StudioHTTPServer

LOG = logging.getLogger('protein-studio.public-demo')
MARKER = '.public-demo.json'
JOB_NAME = '公开结构演示 · Polyubiquitin-B / AFDB'
CAMPAIGN_NAME = '公开参考批次 · 三种已发布结构'
EXAMPLES = ('ubiquitin-afdb', 'ubiquitin', 'crambin')
MARKER_IDENTITY = {'schemaVersion': 1, 'kind': 'protein-studio-public-reference-demo',
                   'examples': list(EXAMPLES)}


def normalize_origin(value):
    """An explicit HTTPS origin, not a URL prefix or an inferred proxy header."""
    if not isinstance(value, str) or any(character.isspace() for character in value):
        raise ValueError('PUBLIC_ORIGIN must be an explicit HTTPS origin.')
    parsed = urlsplit(value)
    try:
        port = 443 if parsed.port is None else parsed.port
    except ValueError as exc:
        raise ValueError('PUBLIC_ORIGIN has an invalid port.') from exc
    if (parsed.scheme != 'https' or not parsed.hostname or not 1 <= port <= 65535 or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        raise ValueError('PUBLIC_ORIGIN must contain only https://host[:port], without a path.')
    return ('https', parsed.hostname.lower(), port)


def write_marker(directory, value):
    temporary = directory / (MARKER + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, directory / MARKER)


def check_demo_directory(directory):
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / MARKER
    if marker.exists():
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 16384:
            raise ValueError('Invalid public demo directory marker.')
        value = json.loads(marker.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or any(value.get(k) != v for k, v in MARKER_IDENTITY.items()):
            raise ValueError('This directory is not owned by the public reference demo.')
        return directory, value
    if any(directory.iterdir()):
        raise ValueError('Refusing a nonempty unmarked data directory. Use a fresh, dedicated PUBLIC_DATA_DIR.')
    write_marker(directory, MARKER_IDENTITY)
    return directory, dict(MARKER_IDENTITY)


class PublicDemoApplication(StudioApplication):
    def __init__(self, data_dir, public_origin, **kwargs):
        self.public_origin = public_origin
        self.public_origin_identity = normalize_origin(public_origin)
        super().__init__(data_dir=data_dir, workers=1, **kwargs)

    def diagnostics(self):
        result = super().diagnostics()
        result['publicDemo'] = {
            'enabled': True, 'readOnly': True, 'origin': self.public_origin,
            'execution': 'bundled-public-reference-replay', 'gpuInference': False,
            'message': '公开只读演示：结构和分析来自已发布数据；未执行新的 GPU 预测。',
        }
        return result


class PublicDemoHandler(StudioHandler):
    def _local_request(self):
        expected = self.app.public_origin_identity
        host = self.headers.get('Host', '')
        try:
            parsed = urlsplit('https://' + host)
            port = 443 if parsed.port is None else parsed.port
            identity = ('https', parsed.hostname, port)
            if not host or not 1 <= port <= 65535 or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                return False
        except ValueError:
            return False
        origin = self.headers.get('Origin')
        if identity != expected:
            # Container-local health checks may use localhost, but this is not
            # a general alternate public Host or X-Forwarded-* trust mechanism.
            return (not origin and self.client_address[0] in {'127.0.0.1', '::1'}
                    and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
                    and urlsplit(self.path).path == '/api/health')
        if origin:
            try:
                return normalize_origin(origin) == expected
            except ValueError:
                return False
        return True

    def _dispatch(self):
        # Decode exactly as the application router will, without stripping the
        # configured prefix twice. No method can bypass the read-only gate.
        from backend.proxy_paths import strip_request_target
        internal = strip_request_target(self.path, self.app.base_path)
        path = unquote(urlsplit(internal).path)
        if path == '/worker' or path.startswith('/worker/'):
            self.close_connection = True
            self._json(403, {'error': 'Worker APIs are disabled in the public read-only demo.'})
            return
        if self.command not in {'GET', 'HEAD'}:
            self.close_connection = True
            self._json(403, {'error': 'This public demonstration is read-only. No task, upload, or storage change was accepted.'})
            return
        # super() applies the base path and then calls this class's explicit
        # HTTPS-origin policy; it never trusts forwarded origin/host headers.
        return super()._dispatch()

    do_PUT = StudioHandler._handle
    do_PATCH = StudioHandler._handle
    do_TRACE = StudioHandler._handle
    do_CONNECT = StudioHandler._handle


class PublicDemoHTTPServer(StudioHTTPServer):
    def __init__(self, address, app):
        super().__init__(address, app)
        self.RequestHandlerClass = PublicDemoHandler


def _check_existing_seed(app):
    """Reject unexpected saved workloads before starting any execution thread."""
    jobs = app.store.list(2)
    if len(jobs) > 1 or any(j.get('name') != JOB_NAME or j.get('engine') != 'reference'
                           or j.get('example') != EXAMPLES[0] for j in jobs):
        raise ValueError('Public demo directory contains unexpected jobs; refusing to serve it.')
    campaigns = app.pipeline.store.list(2)
    if len(campaigns) > 1 or any(c.get('name') != CAMPAIGN_NAME or c.get('engine') != 'reference'
                                for c in campaigns):
        raise ValueError('Public demo directory contains unexpected campaigns; refusing to serve it.')
    if campaigns:
        rows = app.pipeline.store.items(campaigns[0]['id'], 200)['items']
        if len(rows) > len(EXAMPLES) or any(row['targetId'] not in EXAMPLES for row in rows):
            raise ValueError('Public demo contains unexpected targets.')
        for row in rows:
            item = app.pipeline.store.item(campaigns[0]['id'], row['id'])
            submission = item['input']
            expected = app.engine.load_example(row['targetId'])
            if (submission.get('engine') != 'reference' or submission.get('example') != row['targetId']
                    or submission.get('chains') != expected['chains']):
                raise ValueError('Public demo target differs from its bundled public reference.')
    return jobs, campaigns


def build_public_app(data_dir, origin='https://benluo.art', *, base_path=None, timeout=60):
    normalize_origin(origin)
    # Public execution is always the bundled reference path, never an inherited
    # external runner or authenticated GPU-worker controller configuration.
    unsafe = [key for key, value in os.environ.items() if value and (
        (key.startswith('PROTEIN_STUDIO_') and key.endswith('_COMMAND'))
        or key in {'PROTEIN_STUDIO_DATABASE_CONFIG', 'PROTEIN_STUDIO_PREPARATION_RESOURCES_JSON',
                   'PROTEIN_STUDIO_SEARCH_GPU_GROUPS_JSON', 'PROTEIN_STUDIO_GPU_GROUPS_JSON',
                   'PROTEIN_STUDIO_GPU_DEVICES', 'PROTEIN_STUDIO_WORKER_CONFIG'})]
    if unsafe or os.environ.get('PROTEIN_STUDIO_DISTRIBUTED', '0') != '0' or os.environ.get('PROTEIN_STUDIO_PREDICTION_MODE', 'legacy') != 'legacy':
        raise ValueError('Public demo requires an isolated reference environment, without external engine/database/GPU-worker settings.')
    directory, marker = check_demo_directory(data_dir)
    app = PublicDemoApplication(directory, origin, base_path=base_path)
    try:
        jobs, campaigns = _check_existing_seed(app)
        if not jobs:
            example = app.engine.load_example(EXAMPLES[0])
            job = app.submit({'name': JOB_NAME, 'engine': 'reference', 'example': EXAMPLES[0],
                              'chains': example['chains'], 'seeds': [1], 'numSamples': 1})
        else:
            job = jobs[0]
        if not campaigns:
            campaign = app.create_campaign({'name': CAMPAIGN_NAME, 'engine': 'reference',
                'databaseProfile': 'reference', 'status': 'paused', 'placement': 'local',
                'settings': {'seeds': [1], 'numSamples': 1},
                'executionProfile': {'id': 'public-reference-demo', 'version': 'bundled-public-fixtures'}})
        else:
            campaign = campaigns[0]
        items = []
        for name in EXAMPLES:
            example = app.engine.load_example(name)
            items.append({'targetId': name, 'name': example['name'], 'example': name, 'chains': example['chains']})
        app.append_campaign_items(campaign['id'], {'items': items})
        app.pipeline.store.control(campaign['id'], 'seal')
        app.pipeline.store.control(campaign['id'], 'resume')
        marker.update(jobId=job['id'], campaignId=campaign['id'])
        write_marker(directory, marker)
        app.start()
        deadline = time.monotonic() + timeout
        while True:
            current = app.store.get(job['id'])
            current_campaign = app.pipeline.store.get(campaign['id'])
            if current['status'] in {'failed', 'cancelled'} or current_campaign['counts']['failed'] or current_campaign['counts']['cancelled']:
                raise RuntimeError('Bundled public reference preparation failed; the HTTP listener was not opened.')
            if current['status'] == 'completed' and current_campaign['counts']['completed'] == len(EXAMPLES):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('Public reference preparation did not finish; the HTTP listener was not opened.')
            time.sleep(.05)
        # A real bounded scan supplies the read-only storage page with real
        # cache inventory. No quarantine/purge/apply operation is performed.
        if app.storage.manager.snapshot().get('latestScan') is None:
            app.storage.manager.scan(max_entries=1000, max_seconds=10)
        return app
    except BaseException:
        app.close()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('PUBLIC_DATA_DIR', os.environ.get('PROTEIN_STUDIO_PUBLIC_DATA_DIR', ROOT/'data-public-demo'))))
    parser.add_argument('--origin', default=os.environ.get('PUBLIC_ORIGIN', os.environ.get('PROTEIN_STUDIO_PUBLIC_ORIGIN', 'https://benluo.art')))
    parser.add_argument('--host', default=os.environ.get('PROTEIN_STUDIO_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PROTEIN_STUDIO_PORT', '8765')))
    parser.add_argument('--base-path', default=os.environ.get('PROTEIN_STUDIO_BASE_PATH', ''))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    app = build_public_app(args.data_dir, args.origin, base_path=args.base_path)
    try:
        server = PublicDemoHTTPServer((args.host, args.port), app)
    except BaseException:
        app.close()
        raise
    def shutdown(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOG.info('Read-only public reference demo ready: %s%s/', args.origin, args.base_path)
    try:
        server.serve_forever(poll_interval=.25)
    finally:
        server.server_close()
        app.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
