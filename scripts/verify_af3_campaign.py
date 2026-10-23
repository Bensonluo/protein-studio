#!/usr/bin/env python3
"""Deferred, unvalidated draft: verify AF3 campaign from fresh sequence input.

No reference fallback. --dry-run does not connect, write a report, or run inference.
The report distinguishes observed worker results from online worker counts.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit, urljoin, quote
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.submit_batch import iter_fasta
from backend.parsers import parse_structure, parse_confidence

MAX_JSON = 64 * 1024 * 1024
MAX_ARTIFACT = 256 * 1024 * 1024
IDENTIFIER = re.compile(r'^[a-f0-9]{32}$')


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def fail(condition, message):
    if not condition:
        raise ValueError(message)


def finite(value, minimum, maximum):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and minimum <= value <= maximum


def load_config(path):
    path = Path(path)
    fail(path.stat().st_size <= 2 * 1024 * 1024, 'Acceptance configuration exceeds 2 MiB.')
    config = json.loads(path.read_text())
    fail(isinstance(config, dict) and config.get('schemaVersion') == 1, 'Acceptance schemaVersion must be 1.')
    fail(config.get('engine') == 'alphafold3', 'Only real alphafold3 acceptance is supported.')
    server = config.get('server', '')
    parsed = urlsplit(server)
    fail(parsed.scheme in ('http', 'https') and parsed.hostname in ('127.0.0.1', 'localhost', '::1')
         and not parsed.username and not parsed.password and parsed.path in ('', '/') and not parsed.query and not parsed.fragment,
         'Run acceptance against a loopback admin origin, directly or through an SSH tunnel.')
    fail(parsed.port is None or 1 <= parsed.port <= 65535, 'Invalid admin server port.')
    config['server'] = server.rstrip('/')
    fail(isinstance(config.get('databaseProfile'), str) and config['databaseProfile'] != 'reference', 'An actual AF3 database profile is required.')
    version = config.get('executionProfile', {}).get('version')
    manifest = config.get('expectedWeightsManifestSha256', '')
    fail(isinstance(manifest, str) and re.fullmatch('[a-f0-9]{64}', manifest), 'Expected full weights manifest SHA-256 is required.')
    fail(isinstance(version, str) and version.startswith('af3-v3.0.4-') and version.endswith('-w' + manifest[:12]),
         'Runtime version must match the generated AF3 source/weights-manifest identity.')
    for key, maximum in (('samples', 20), ('recycles', 50)):
        fail(type(config.get(key)) is int and 1 <= config[key] <= maximum, 'Invalid ' + key + '.')
    seeds = config.get('seeds')
    fail(isinstance(seeds, list) and seeds and all(type(s) is int and s >= 0 for s in seeds) and len(set(seeds)) == len(seeds), 'seeds must be distinct nonnegative integers.')
    workers = config.get('expectedWorkers')
    fail(isinstance(workers, list) and workers and all(isinstance(w, str) and w for w in workers) and len(set(workers)) == len(workers), 'expectedWorkers must enumerate distinct configured worker IDs.')
    return config


def plan_targets(path, count):
    fail(type(count) is int and 1 <= count <= 1024, '--targets must be in 1..1024 for this small acceptance run.')
    path = Path(path)
    fail(path.stat().st_size <= 2 * 1024 * 1024, 'Acceptance FASTA exceeds 2 MiB; use a small validation set.')
    raw = path.read_bytes()
    records = list(itertools.islice(iter_fasta(raw.decode('utf-8-sig').splitlines()), count))
    fail(bool(records), 'FASTA contains no targets.')
    fail(all(1 <= len(r.sequence) <= 2000 for r in records), 'Acceptance supports 1..2000 residues per single-chain target so PAE can be validated.')
    targets = []
    for index in range(count):
        record = records[index % len(records)]
        digest = sha((record.name + '\0' + record.sequence).encode())
        targets.append(dict(targetId=f'af3-accept-{index + 1:04}-{digest[:16]}',
                            name=f'AF3 acceptance {index + 1}: {record.name}'[:120],
                            chains=[dict(id='A', sequence=record.sequence)]))
    return targets, dict(path=str(path.resolve()), sha256=sha(raw), sourceRecordsUsed=len(records),
                        targets=count, repeatedInput=count > len(records),
                        submission='fresh amino-acid sequences only; no supplied MSA, templates, coordinates or reference example')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Acceptance endpoints must not redirect.')


class Client:
    def __init__(self, server):
        self.server = server
        self.timeout = 60
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def endpoint(self, value):
        url = urljoin(self.server + '/', value)
        expected, actual = urlsplit(self.server), urlsplit(url)
        fail((actual.scheme, actual.netloc) == (expected.scheme, expected.netloc), 'Artifact URL leaves the configured admin origin.')
        return url

    def json(self, path, payload=None):
        data = None if payload is None else canonical(payload).encode()
        req = Request(self.endpoint(path), data=data, headers={'Content-Type': 'application/json'}, method='GET' if data is None else 'POST')
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                body = response.read(MAX_JSON + 1)
                fail(len(body) <= MAX_JSON, 'API JSON exceeds the bounded acceptance size.')
                return json.loads(body)
        except HTTPError as error:
            raise RuntimeError(f'HTTP {error.code} for {path}: {error.read(2048).decode(errors="replace")}') from error

    def download(self, url, destination):
        digest, size = hashlib.sha256(), 0
        with self.opener.open(Request(self.endpoint(url)), timeout=self.timeout) as response:
            fail(response.status == 200, 'Artifact download did not return HTTP 200.')
            with Path(destination).open('wb') as handle:
                while block := response.read(256 * 1024):
                    size += len(block)
                    fail(size <= MAX_ARTIFACT, 'Artifact exceeds the 256 MiB acceptance bound.')
                    digest.update(block)
                    handle.write(block)
        fail(size > 0, 'Downloaded artifact is empty.')
        return dict(url=url, httpStatus=200, bytes=size, sha256=digest.hexdigest())


def write_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def matching_workers(cluster, config, placement):
    fail(cluster.get('enabled') is True, 'Distributed workers mode must be enabled.')
    wanted = set(config['expectedWorkers'])
    version = config['executionProfile']['version']
    return [dict(id=n['id'], location=n['location'], runtimeVersion=n['runtimeVersion'],
                 maxResidues=n.get('maxResidues'), currentPid=n.get('pid'))
            for n in cluster.get('nodes', []) if n.get('id') in wanted and n.get('status') == 'online'
            and n.get('mode') == 'real' and not n.get('demo') and n.get('engine') == 'alphafold3'
            and n.get('loaded') is True and n.get('runtimeVersion') == version
            and n.get('location') in ('local', 'aliyun') and (placement == 'hybrid' or n['location'] == placement)
            and n.get('settings', {}).get('numSamples') == config['samples']
            and n.get('settings', {}).get('numRecycles') == config['recycles']]


def validate_campaign(campaign, config, placement):
    fail(campaign.get('engine') == 'alphafold3', 'Existing campaign is not real AF3.')
    fail(campaign.get('status') != 'cancelled', 'A cancelled acceptance campaign cannot be resumed.')
    fail(campaign.get('placement', 'hybrid') == placement, 'Campaign placement differs from this acceptance.')
    fail(campaign.get('executionProfile', {}).get('version') == config['executionProfile']['version'], 'Campaign runtime version differs.')
    profile = campaign.get('databaseProfile')
    fail((profile.get('id') if isinstance(profile, dict) else profile) == config['databaseProfile'], 'Campaign database profile differs.')
    settings = campaign.get('settings', {})
    fail(not settings.get('example'), 'Reference example settings are forbidden in real AF3 acceptance.')
    for name, value in [('seeds', config['seeds']), ('numSamples', config['samples']), ('numRecycles', config['recycles'])]:
        fail(settings.get(name) == value, 'Campaign ' + name + ' differs.')


def metadata_pages(client, campaign_id):
    offset, seen = 0, set()
    while True:
        page = client.json(f'/api/campaigns/{campaign_id}/items?limit=200&offset={offset}')
        for item in page.get('items', []):
            fail(item.get('id') not in seen, 'Duplicate target in paginated API response.')
            seen.add(item.get('id'))
            yield item
        next_offset = page.get('nextOffset')
        if next_offset is None:
            return
        fail(type(next_offset) is int and next_offset > offset, 'Invalid pagination cursor.')
        offset = next_offset


def validate_result_contract(item, target, config):
    fail(item.get('stage') == 'completed' and item.get('engine') == 'alphafold3', 'Target is not a published AF3 result.')
    fail(item.get('targetId') == target['targetId'], 'Returned target identity differs.')
    submitted = item.get('input', {})
    fail(submitted.get('chains') == target['chains'], 'Submitted target sequence differs from acceptance input.')
    for key, expected in [('seeds', config['seeds']), ('numSamples', config['samples']), ('numRecycles', config['recycles'])]:
        fail(submitted.get(key) == expected, 'Target input ' + key + ' differs.')
    result = item.get('result')
    fail(isinstance(result, dict), 'Published target has no result document.')
    p = result.get('provenance', {})
    fail(p.get('mode') == 'prediction' and p.get('newPrediction') is True, 'Reference/replayed results are not real inference acceptance.')
    fail(p.get('isDemo') is False and p.get('demo') is False and p.get('workerMode') == 'real', 'Result must explicitly identify a real non-demo worker.')
    native = p.get('nativeExecutionMode') or p.get('executionMode')
    fail(native == 'resident-native', 'Result lacks native resident execution evidence.')
    signature = p.get('modelSignature', {})
    fail(signature.get('engine') == 'alphafold3' and signature.get('apiVersion') == 'v3.0.4', 'Wrong native model engine/API version.')
    fail(signature.get('modelVersion') == config['executionProfile']['version'], 'Result model version differs from acceptance configuration.')
    fail(signature.get('numSamples') == config['samples'] and signature.get('numRecycles') == config['recycles'], 'Resident sampling/recycle signature differs.')
    fail(re.fullmatch('[a-f0-9]{64}', signature.get('af3ScriptSha256', '') or ''), 'Native AF3 script SHA-256 is missing.')
    fail(type(p.get('residentPid')) is int and p['residentPid'] > 0 and p.get('modelLoads') == 1, 'Each result must identify a resident PID with one model load.')
    fail(p.get('workerId') in config['expectedWorkers'] and p.get('workerLocation') in ('local', 'aliyun'), 'Result worker identity/location is outside the configured deployment.')
    fail(re.fullmatch('[a-f0-9]{64}', p.get('transportSha256', '') or ''), 'Verified result transport digest is missing.')
    expected = {(seed, sample) for seed in config['seeds'] for sample in range(config['samples'])}
    models = result.get('rankedModels', [])
    actual = []
    for model in models:
        match = re.search(r'(?:^|/)seed-(\d+)_sample-(\d+)(?:/|$)', model.get('structureFile', ''))
        fail(match is not None, 'A ranked model lacks its native seed/sample identity.')
        actual.append((int(match[1]), int(match[2])))
    fail(len(actual) == len(expected) and set(actual) == expected, 'Returned native seed/sample coverage is incomplete or duplicated.')
    fail(result.get('samplingComplete') is True and result.get('actualSampleCount') == len(expected)
         and result.get('requestedSampleCount') == len(expected), 'Native sample completion counters differ.')
    events = item.get('events', [])
    visited = {event.get('stage') for event in events}
    fail({'preparing', 'ready', 'predicting', 'validating', 'completed'} <= visited, 'Preparation, prediction, return validation and delivery events are incomplete.')
    timings = item.get('timings', {})
    fail(all(finite(timings.get(key), 0, 1e12) for key in ('preparationSeconds', 'predictionSeconds', 'validationSeconds')), 'Recorded stage timing fields are invalid.')
    fail(result.get('structureUrl') and result.get('artifacts'), 'Native structure/artifact download registry is missing.')
    return dict(itemId=item['id'], targetId=item['targetId'], workerId=p['workerId'], location=p['workerLocation'],
                residentPid=p['residentPid'], residentSessionId=p.get('residentSessionId'), modelLoads=p['modelLoads'],
                signature=signature, nativeExecutionMode=native, transportMode=p.get('executionMode'),
                transportSha256=p['transportSha256'], actualSampleCount=len(expected), cacheHit=item.get('cacheHit'),
                attempt=item.get('attempt'), timings=timings,
                timingScope='recorded pipeline stage wall times; prediction includes scheduling/transfer and is not pure GPU seconds',
                stages=sorted(visited), createdAt=item.get('createdAt'), updatedAt=item.get('updatedAt'))


def verify_artifacts(client, campaign_id, item, target):
    result = item['result']
    prefix = f'/api/campaigns/{campaign_id}/items/{item["id"]}/'
    artifacts = result['artifacts']
    fail(isinstance(artifacts, list) and len(artifacts) <= 4096, 'Artifact registry exceeds acceptance bounds.')
    by_name, paths, evidence = {}, {}, []
    with tempfile.TemporaryDirectory(prefix='af3-accept-artifacts-') as temporary:
        for index, artifact in enumerate(artifacts):
            name, url = artifact.get('name'), artifact.get('url')
            fail(isinstance(name, str) and name not in by_name and isinstance(url, str), 'Artifact registry contains missing/duplicate names or URLs.')
            parsed = urlsplit(client.endpoint(url))
            fail(parsed.path == prefix + 'artifact', 'Artifact URL is not owned by this target.')
            local = Path(temporary) / (str(index) + Path(name).suffix)
            info = client.download(url, local)
            info['name'] = name
            evidence.append(info)
            by_name[name], paths[name] = artifact, local
        # Independently GET the canonical published structure route, too.
        fail(urlsplit(client.endpoint(result['structureUrl'])).path == prefix + 'structure', 'Published structure URL belongs to another target.')
        primary = Path(temporary) / 'published.cif'
        published = client.download(result['structureUrl'], primary)
        samples = []
        for model in result['rankedModels']:
            name = model['structureFile']
            fail(name in paths and name.endswith('_model.cif'), 'Native sample mmCIF is missing from artifacts.')
            structure = paths[name]
            parsed = parse_structure(structure, 'alphafold3')
            fail(sorted(c['sequence'] for c in parsed['chains']) == sorted(c['sequence'] for c in target['chains']), 'Downloaded structure sequence does not match the submitted protein.')
            pl = parsed.get('plddt')
            fail(isinstance(pl, list) and len(pl) == sum(len(c['sequence']) for c in target['chains']) and all(finite(x, 0, 100) for x in pl), 'Native mmCIF lacks valid residue pLDDT.')
            native_text = structure.read_text()
            fail('AlphaFold' in native_text and '_software.' in native_text, 'Native mmCIF software metadata is missing.')
            base = name[:-len('model.cif')]
            full, summary = base + 'confidences.json', base + 'summary_confidences.json'
            fail(full in paths and summary in paths, 'Each native sample must retain full confidence and summary artifacts.')
            arrays, metrics = parse_confidence(paths[full]), parse_confidence(paths[summary])
            pae = arrays.get('pae')
            fail(isinstance(pae, list) and len(pae) == len(pl) and all(len(row) == len(pl) for row in pae), 'Downloaded sample PAE is missing or has the wrong protein-token dimensions.')
            fail(finite(metrics.get('ptm'), 0, 1), 'Downloaded sample summary lacks a valid pTM.')
            samples.append(dict(name=name, meanPlddt=parsed['meanPlddt'], ptm=metrics['ptm'], iptm=metrics.get('iptm'), paeRows=len(pae)))
        fail(result.get('structureFile') in paths, 'Primary structure is not a registered sample.')
        fail(published['sha256'] == sha(paths[result['structureFile']].read_bytes()), 'Primary structure download differs from the selected native artifact.')
    return dict(downloads=evidence, publishedStructure=published, samples=samples)


def coverage(results, snapshot, args):
    actual = {entry['workerId'] for entry in results}
    locations = {entry['location'] for entry in results}
    local = {entry['workerId'] for entry in results if entry['location'] == 'local'}
    cloud = {entry['workerId'] for entry in results if entry['location'] == 'aliyun'}
    required = {entry['id'] for entry in snapshot} if args.require_worker_coverage else set()
    required_locations = ({'local', 'aliyun'} if args.placement == 'hybrid' else {args.placement}) if args.require_location_coverage else set()
    missing = sorted(required - actual)
    fail(len(local) >= args.min_local_workers and len(cloud) >= args.min_cloud_workers,
         f'Actual distinct worker deliveries insufficient: local={len(local)}, aliyun={len(cloud)}.')
    fail(not missing, 'Online-at-start workers without an actual delivered target: ' + ', '.join(missing))
    fail(required_locations <= locations, 'Actual results do not cover required locations: ' + ', '.join(sorted(required_locations - locations)))
    return dict(actualWorkers=sorted(actual), localWorkers=sorted(local), aliyunWorkers=sorted(cloud),
                requiredWorkers=sorted(required), requiredLocations=sorted(required_locations),
                onlineAtStart=[entry['id'] for entry in snapshot],
                basis='distinct workers in validated delivered results; online registration alone is not execution coverage')


def run_acceptance(args, client=None, sleep=time.sleep, clock=time.monotonic):
    config, targets, report = None, None, dict(schemaVersion=1, kind='real-af3-campaign-acceptance', status='failed', startedAt=now(),
        inferenceSubmitted=False, results=[], scientificAccuracy='not_evaluated_against_experimental_ground_truth')
    campaign_id = args.campaign_id
    old = None
    try:
        config = load_config(args.config)
        targets, input_evidence = plan_targets(args.input, args.targets)
        fail(0 < args.timeout <= 86400, '--timeout must be positive and at most 86400 seconds.')
        fail(args.min_local_workers >= 0 and args.min_cloud_workers >= 0, 'Minimum worker coverage cannot be negative.')
        if args.placement == 'local':fail(args.min_cloud_workers == 0, 'Local-only placement cannot require cloud workers.')
        if args.placement == 'aliyun':fail(args.min_local_workers == 0, 'Cloud-only placement cannot require local workers.')
        identity = dict(server=config['server'], configuration=config, input=input_evidence['sha256'], targets=args.targets, placement=args.placement)
        identity_hash = sha(canonical(identity).encode())
        report.update(server=config['server'], input=input_evidence, placement=args.placement, configIdentity=identity_hash,
            expectedRuntimeVersion=config['executionProfile']['version'], expectedWeightsManifestSha256=config['expectedWeightsManifestSha256'],
            weightsEvidence='expected manifest-derived runtime identity; on-node file hashing is verified by the separate GPU probe',
            requestedCoverage=dict(minLocalWorkers=args.min_local_workers,minCloudWorkers=args.min_cloud_workers,
                requireLocations=args.require_location_coverage,requireWorkers=args.require_worker_coverage))
        if args.dry_run:
            report.update(status='dry_run', campaignId=campaign_id, plannedTargets=[dict(targetId=t['targetId'],residues=len(t['chains'][0]['sequence'])) for t in targets],
                          notExecuted=['network requests','report write','campaign creation','database search','GPU inference','artifact verification'])
            return report
        if campaign_id:fail(bool(IDENTIFIER.fullmatch(campaign_id)), 'Invalid campaign ID.')
        old = None
        if args.report.exists():
            old = json.loads(args.report.read_text())
            fail(campaign_id and old.get('campaignId') == campaign_id and old.get('configIdentity') == identity_hash,
                 'Report already exists for a different/new run; preserve it and use --campaign-id or a new --report path.')
        client = client or Client(config['server'])
        pipeline = client.json('/api/pipeline/config')
        fail(pipeline.get('predictionMode') == 'workers', 'Real acceptance requires workers prediction mode.')
        snapshot = old.get('workersAtStart') if old and old.get('workersAtStart') else matching_workers(client.json('/api/cluster'), config, args.placement)
        fail(bool(snapshot), 'No matching online real AF3 workers for this runtime, sampling and placement.')
        fail(sum(w['location']=='local' for w in snapshot) >= args.min_local_workers and sum(w['location']=='aliyun' for w in snapshot) >= args.min_cloud_workers,
             'Too few matching online workers to satisfy requested coverage.')
        minimum_targets = max(args.min_local_workers + args.min_cloud_workers, len(snapshot) if args.require_worker_coverage else 0,
                              2 if args.require_location_coverage and args.placement == 'hybrid' else 1)
        fail(args.targets >= minimum_targets, 'Increase --targets to at least ' + str(minimum_targets) + ' for this coverage request; scheduling still must actually reach each worker.')
        report.update(workersAtStart=snapshot, status='in_progress')
        started = clock()
        if campaign_id:
            campaign = client.json('/api/campaigns/' + campaign_id)
            validate_campaign(campaign, config, args.placement)
        else:
            campaign = client.json('/api/campaigns', dict(name='AF3 real acceptance ' + report['startedAt'], engine='alphafold3',
                databaseProfile=config['databaseProfile'], executionProfile=config['executionProfile'], placement=args.placement,
                settings=dict(seeds=config['seeds'],numSamples=config['samples'],numRecycles=config['recycles']),status='paused'))
            campaign_id = campaign['id']
        report.update(campaignId=campaign_id)
        write_report(args.report, report)  # Durable ID before any target import or resume.
        if not campaign.get('sealed'):
            for offset in range(0, len(targets), 100):
                client.json(f'/api/campaigns/{campaign_id}/items', {'items': targets[offset:offset + 100]})
            client.json(f'/api/campaigns/{campaign_id}/seal', {})
        metadata = list(metadata_pages(client, campaign_id))
        fail(len(metadata) == len(targets) and {x['targetId'] for x in metadata} == {x['targetId'] for x in targets}, 'Campaign target manifest does not match this acceptance input.')
        # Before resuming, compare stored sequence/settings even for queued items.
        by_target = {t['targetId']: t for t in targets}
        for meta in metadata:
            item = client.json(f'/api/campaigns/{campaign_id}/items/{meta["id"]}')
            fail(item.get('input', {}).get('chains') == by_target[meta['targetId']]['chains'], 'Campaign contains a different input sequence.')
        client.json(f'/api/campaigns/{campaign_id}/resume', {})
        report['inferenceSubmitted'] = True
        write_report(args.report, report)
        verified, last_counts = set(), None
        while True:
            remaining = args.timeout - (clock() - started)
            if remaining <= 0:raise TimeoutError('Acceptance deadline reached; campaign ID retained for --campaign-id.')
            client.timeout = max(1, min(60, remaining))
            campaign = client.json('/api/campaigns/' + campaign_id)
            validate_campaign(campaign, config, args.placement)
            counts = campaign.get('counts', {})
            report.update(counts=counts, elapsedWallSeconds=clock()-started)
            if counts != last_counts:
                print('AF3 acceptance ' + campaign_id + ': ' + canonical(counts), file=sys.stderr)
                last_counts = dict(counts)
            for meta in metadata_pages(client, campaign_id):
                if meta.get('stage') in ('failed', 'cancelled'):
                    raise RuntimeError(f'Target {meta.get("targetId")} {meta["stage"]} at {meta.get("failedStage")}: {meta.get("error")}')
                if meta.get('stage') != 'completed' or meta['id'] in verified:
                    continue
                fail(clock() - started < args.timeout, 'Acceptance time budget expired before artifact validation.')
                item = client.json(f'/api/campaigns/{campaign_id}/items/{meta["id"]}')
                evidence = validate_result_contract(item, by_target[meta['targetId']], config)
                fail(args.placement == 'hybrid' or evidence['location'] == args.placement, 'Result executed outside requested placement.')
                evidence.update(verify_artifacts(client, campaign_id, item, by_target[meta['targetId']]))
                report['results'].append(evidence)
                verified.add(meta['id'])
                write_report(args.report, report)
            if len(verified) == len(targets):
                report['coverage'] = coverage(report['results'], snapshot, args)
                report.update(status='passed', finishedAt=now(), elapsedWallSeconds=clock()-started,
                    deliveredTargets=len(verified), uncachedPreparedTargets=sum(r['cacheHit'] is False for r in report['results']),
                    evidenceScope='real deployment APIs, worker-reported native runtime and independently downloaded native artifacts; not remote hardware attestation or experimental scientific accuracy')
                write_report(args.report, report)
                return report
            write_report(args.report, report)
            sleep(min(5, max(0, args.timeout - (clock()-started))))
    except Exception as error:
        report.update(status='timed_out' if isinstance(error, TimeoutError) else 'failed', error=str(error)[:4000], errorType=type(error).__name__, finishedAt=now())
        if campaign_id:
            report['campaignId'] = campaign_id
            if client is not None and not args.dry_run:
                try:
                    client.json(f'/api/campaigns/{campaign_id}/pause', {})
                    report['pauseRequested'] = True
                except Exception as pause_error:
                    report.update(pauseRequested=False, pauseError=str(pause_error)[:1000])
        if not args.dry_run:
            # Never overwrite an existing unrelated report after a config error.
            if not args.report.exists() or (campaign_id and report.get('configIdentity') and
                (not old or (old.get('campaignId') == campaign_id and old.get('configIdentity') == report['configIdentity']))):
                write_report(args.report, report)
        return report


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--input', type=Path, default=ROOT/'fixtures/af3_smoke.fasta')
    p.add_argument('--placement', choices=['local','aliyun','hybrid'], default='hybrid')
    p.add_argument('--targets', type=int, default=2)
    p.add_argument('--report', type=Path, required=True)
    p.add_argument('--timeout', type=float, default=86400)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--campaign-id')
    p.add_argument('--min-local-workers', type=int, default=0)
    p.add_argument('--min-cloud-workers', type=int, default=0)
    p.add_argument('--require-location-coverage', action='store_true')
    p.add_argument('--require-worker-coverage', action='store_true')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    report = run_acceptance(args)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report['status'] in ('passed','dry_run') else 1


if __name__ == '__main__':
    raise SystemExit(main())
