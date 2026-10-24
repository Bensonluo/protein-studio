"""Persistent node worker: authenticated pull, verified bundles, resident model reuse."""
from __future__ import annotations
import argparse
import hashlib
import json
import logging
import os
import shutil
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

from .distributed import digest_file, safe_extract
from .resident import ResidentProcess, ResidentProcessError
from .gpu_access import gpu_group_lease, normalize_group
from .proxy_paths import normalize_base_path

LOG = logging.getLogger('protein-studio.worker')

class UploadPendingError(RuntimeError):
    """Computed ZIP retained locally; network delivery was not acknowledged."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Worker requests must not redirect to another endpoint.')


def worker_request_url(controller_url, path):
    """Resolve only worker API paths under the configured origin/mount, never external URLs."""
    if not isinstance(path, str) or any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError('Worker path must be a clean origin-relative path.')
    controller = urllib.parse.urlsplit(controller_url)
    if controller.scheme not in {'http', 'https'} or not controller.hostname or controller.username or controller.password or controller.query or controller.fragment:
        raise ValueError('Controller URL must be an HTTP(S) origin with an optional application mount.')
    base = normalize_base_path(controller.path)
    target = urllib.parse.urlsplit(path)
    if target.scheme or target.netloc or target.fragment or not target.path.startswith('/'):
        raise ValueError('Worker path must be an origin-relative worker API path.')
    # These endpoints use server-generated identifiers, not percent-encoded path
    # segments. Reject normalization tricks before attaching bearer credentials.
    if '%' in target.path or '\\' in target.path or any(part in {'.', '..'} for part in target.path.split('/')):
        raise ValueError('Worker API paths must not contain escaped or relative segments.')
    if base and target.path.startswith(base + '/worker/v1/'):
        internal = target.path[len(base):]
    else:
        internal = target.path
    if not internal.startswith('/worker/v1/'):
        raise ValueError('Worker path must use the configured worker API.')
    return urllib.parse.urlunsplit((controller.scheme, controller.netloc, base + internal, target.query, ''))


class WorkerClient:
    def __init__(self, url, worker_id, token, engine, runtime_version, work_dir, resident_command,
                 gpu_devices=None, demo=False, unsafe_http=False, samples=None, recycles=None,
                 resident_startup_timeout=600, resident_request_timeout=7200, http_timeout=60):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Controller URL must be an HTTP(S) origin with an optional application mount.')
        if parsed.scheme != 'https' and parsed.hostname not in {'127.0.0.1','localhost','::1'} and not unsafe_http:
            raise ValueError('Non-loopback workers require TLS; --unsafe-http is an explicit development-only override.')
        if len(token) < 24:
            raise ValueError('Worker bearer token must contain at least 24 characters.')
        normalize_base_path(parsed.path)
        self.url=url.rstrip('/')
        self.worker_id,self.token,self.engine,self.runtime_version=worker_id,token,engine,runtime_version
        self.root=Path(work_dir).resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.demo=demo
        self.group=[] if demo else list(gpu_devices or [])
        if (demo and engine!='reference') or (not demo and (engine=='reference' or not self.group)):
            raise ValueError('Reference nodes need --demo; real model workers need actual --gpu-devices.')
        if not demo:
            self.group=normalize_group(self.group)
        self._device_lease=None
        self.samples,self.recycles=samples,recycles
        environment={'CUDA_VISIBLE_DEVICES':'' if demo else ','.join(self.group)}
        for name,value in [('resident startup',resident_startup_timeout),('resident request',resident_request_timeout),('HTTP',http_timeout)]:
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not 0 < value <= 604800:
                raise ValueError(name+' timeout must be positive and at most seven days.')
        self.http_timeout=http_timeout
        self.resident=ResidentProcess(resident_command,self.root/'resident',env=environment,
            startup_timeout=resident_startup_timeout,request_timeout=resident_request_timeout)
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        self.stop_event,self.drain_event=threading.Event(),threading.Event()
        self.lock=threading.RLock()
        self.leases={}
        self.revoked=set()
        self.session=None
        self.registration=None
        self.heartbeat_thread=None
        self.processed=0
        self.model_initializations=0
        self.model_loads=0
        self.restart_count=0

    def request(self,path,payload=None,headers=None,data=None):
        url = worker_request_url(self.url, path)
        extra={'Authorization':'Bearer '+self.token}
        extra.update(headers or {})
        method='GET'
        if payload is not None:
            data=json.dumps(payload,allow_nan=False).encode()
            extra.update({'Content-Type':'application/json','Content-Length':str(len(data))})
            method='POST'
        elif data is not None:
            method='POST'
        request=urllib.request.Request(url,data=data,headers=extra,method=method)
        return self.opener.open(request,timeout=self.http_timeout)

    def json_request(self,path,payload):
        with self.request(path,payload) as response:
            return json.load(response)

    def _ready(self,restart=False):
        acquired=False
        if not self.demo and self._device_lease is None:
            context=gpu_group_lease(self.group,lambda:self.stop_event.is_set() or self.drain_event.is_set())
            devices=context.__enter__()
            self.resident.inherited_fds=devices.lock_fds
            self._device_lease=context
            acquired=True
        try:
            return self._load_ready(restart)
        except BaseException:
            if acquired:
                self.resident.close()
                self._release_devices()
            raise

    def _release_devices(self):
        if self._device_lease is not None:
            context,self._device_lease=self._device_lease,None
            self.resident.inherited_fds=()
            context.__exit__(None,None,None)

    def _load_ready(self,restart=False):
        ready=self.resident.restart() if restart else self.resident.start()
        self.model_initializations+=int(ready.get('initializationCount',1))
        self.model_loads+=0 if self.demo else int(ready.get('modelLoads',1))
        if restart:
            self.restart_count+=1
        signature=ready.get('signature',{})
        if ready.get('engine')!=self.engine or bool(ready.get('isDemo'))!=self.demo:
            raise ValueError('Resident engine/demo declaration differs from registered worker mode.')
        for key, requested in [('numSamples',self.samples),('numRecycles',self.recycles)]:
            actual=signature.get(key)
            if requested is not None and actual is not None and requested!=actual:
                raise ValueError('Worker CLI settings differ from resident startup signature: '+key)
        return ready

    def start(self):
        ready=self._ready()
        signature=ready.get('signature',{})
        payload=dict(workerId=self.worker_id,engine=self.engine,runtimeVersion=self.runtime_version,
                     gpuGroups=[self.group],pid=ready.get('pid'))
        buckets=signature.get('buckets',[])
        if isinstance(buckets,list) and buckets and all(isinstance(n,int) and not isinstance(n,bool) and n>0 for n in buckets):
            payload['maxResidues']=max(buckets)
        for key,value in [('numSamples',self.samples if self.samples is not None else signature.get('numSamples')),
                          ('numRecycles',self.recycles if self.recycles is not None else signature.get('numRecycles'))]:
            if value is not None: payload[key]=value
        self.registration=self.json_request('/worker/v1/register',payload)
        self.session=self.registration['session']
        if (self.registration['mode']=='fixture')!=self.demo:
            raise ValueError('Administrator worker mode does not match this node.')
        self._heartbeat()
        self.heartbeat_thread=threading.Thread(target=self._heartbeat_loop,name='worker-heartbeat',daemon=True)
        self.heartbeat_thread.start()

    def _heartbeat(self):
        with self.lock:
            leases=[{'itemId':k,'leaseToken':v['leaseToken'],'deliveryState':v.get('deliveryState')} for k,v in self.leases.items()]
        live=self.resident.process is not None and self.resident.process.poll() is None and not self.resident.closed
        payload=dict(session=self.session,leases=leases,pid=self.resident.pid,loaded=live and not self.resident.broken,
            modelInitCount=self.model_initializations,modelLoads=self.model_loads,processedTargets=self.processed,
            restartCount=self.restart_count,draining=self.drain_event.is_set())
        response=self.json_request('/worker/v1/heartbeat',payload)
        with self.lock:
            self.revoked.update(response.get('rejected',[]))
        return response

    def _heartbeat_loop(self):
        interval=max(.2,min(10,self.registration['leaseSeconds']/3))
        while not self.stop_event.wait(interval):
            try: self._heartbeat()
            except urllib.error.HTTPError as exc:
                if exc.code==401:
                    self.stop_event.set()
                LOG.warning('Heartbeat rejected: HTTP %s',exc.code)
            except (OSError,ValueError) as exc:
                LOG.warning('Heartbeat connection failed: %s',type(exc).__name__)

    def download_prepared(self,target,batch_root):
        item_root=batch_root/target['itemId']
        item_root.mkdir(parents=True,exist_ok=True)
        bundle=item_root/'prepared.zip'
        with self.request(target['preparedUrl'],headers={'X-Lease-Token':target['leaseToken']}) as response:
            expected=response.headers.get('X-Content-SHA256','')
            total=0
            with bundle.open('xb') as output:
                while True:
                    chunk=response.read(1024*1024)
                    if not chunk: break
                    total+=len(chunk)
                    if total>self.registration['maxBundleBytes']:
                        raise ValueError('Preparation download exceeds configured size limit.')
                    output.write(chunk)
        if digest_file(bundle)!=expected:
            raise ValueError('Preparation bundle SHA-256 verification failed.')
        extracted=safe_extract(bundle,item_root/'prepared',self.registration['maxExpandedBytes'])
        manifest=json.loads((extracted/'manifest.json').read_text())
        if manifest.get('itemId')!=target['itemId'] or manifest.get('schemaVersion')!=1:
            raise ValueError('Preparation bundle target identity mismatch.')
        cache=extracted/'cache'
        from .staged import _manifest
        cached=_manifest(cache,manifest['cacheKey'])
        if cached is None:
            raise ValueError('Preparation package lacks a valid cache manifest.')
        descriptor=dict(manifest['descriptor'],cacheDirectory=str(cache),manifestPath=str(cache/'COMPLETE.json'),
                        inputPath=str(cache/cached['inputRelativePath']),
                        artifacts=[dict(a,path=str(cache/a['name'])) for a in cached['artifacts']])
        path=item_root/'prepared.json'
        path.write_text(json.dumps(descriptor,allow_nan=False))
        output=item_root/'output'
        output.mkdir()
        return dict(targetId=target['itemId'],job=target['job'],preparedPath=str(path),outputDir=str(output))

    def upload_result(self,target,output_dir,result_json=None):
        output_dir=Path(output_dir).resolve()
        derived_result=Path(result_json).resolve() if result_json else output_dir/'result.json'
        bundle=output_dir.parent/'result.zip'
        total=0
        ready=self.resident.ready or {}
        native_mode=('resident-reference' if self.demo else
                     'resident-native' if self.engine=='alphafold3' and ready.get('weightsLoaded') is True else 'unverified')
        with zipfile.ZipFile(bundle,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json',json.dumps({'schemaVersion':1,'itemId':target['itemId'],
                'execution':{'residentPid':self.resident.pid,'clientGeneration':self.resident.generation,
                'residentSessionId':ready.get('sessionId'),'modelLoads':ready.get('modelLoads'),
                'nodeModelLoads':self.model_loads,'modelSignature':ready.get('signature'),
                'nativeExecutionMode':native_mode,'isDemo':self.demo}}))
            count=0
            for file in output_dir.rglob('*'):
                if file.is_symlink():
                    raise ValueError('Resident output may not contain symlinks.')
                if not file.is_file(): continue
                # This reserved directory contains controller-rebuildable PAE
                # previews/matrices. Send native confidence JSON once; the
                # controller independently derives and validates its own index.
                if file.relative_to(output_dir).parts[0] == '.result-artifacts':
                    continue
                # The controller independently parses native structures/scores.
                # Do not transmit a derived document containing node-local paths
                # or duplicate its large PAE arrays in another JSON artifact.
                if file.resolve()==derived_result: continue
                count+=1
                total+=file.stat().st_size
                if count>4095 or total>self.registration['maxExpandedBytes']:
                    raise ValueError('Resident output exceeds the bundle expanded limit.')
                archive.write(file,'payload/'+file.relative_to(output_dir).as_posix())
        if bundle.stat().st_size>self.registration['maxBundleBytes']:
            raise ValueError('Resident result ZIP exceeds the transfer limit.')
        headers={'X-Lease-Token':target['leaseToken'],'X-Content-SHA256':digest_file(bundle),
                 'Content-Type':'application/zip','Content-Length':str(bundle.stat().st_size)}
        for attempt in range(5):
            try:
                with bundle.open('rb') as stream, self.request(target['resultUrl'],headers=headers,data=stream) as response:
                    receipt=json.load(response)
                # A successful HTTP status alone does not prove that these bytes
                # were committed for this target. Retain the files until the
                # controller confirms the same identity and digest, including
                # an idempotent response after a lost acknowledgement.
                if (not isinstance(receipt,dict) or receipt.get('accepted') is not True
                    or receipt.get('itemId')!=target['itemId']
                    or receipt.get('sha256')!=headers['X-Content-SHA256']):
                    raise ValueError('Controller did not confirm this target and result digest.')
                return receipt
            except urllib.error.HTTPError as exc:
                if exc.code not in {408,429,500,502,503,504}:
                    raise
                error=exc
            except (OSError,ValueError) as exc:
                error=exc
            with self.lock:
                revoked=target['itemId'] in self.revoked
            if revoked or self.stop_event.is_set():
                raise UploadPendingError('Lease lost; computed result ZIP retained at '+str(bundle)) from error
            if attempt<4:
                self.stop_event.wait(min(4,.25*(2**attempt)))
        raise UploadPendingError('Upload retries exhausted; computed ZIP retained at '+str(bundle)) from error

    def _discard_accepted_target(self,target,output_dir,receipt):
        """Remove only acknowledged target scratch; unresolved siblings survive.

        The controller owns the durable native artifacts after acceptance. This
        is called by the batch lifecycle, not upload_result, so manual transports
        can still inspect/re-send the same ZIP for idempotency diagnostics.
        """
        if receipt.get('accepted') is not True or receipt.get('itemId')!=target['itemId']:
            return False
        output=Path(output_dir)
        item_root=output.parent
        batch_root=item_root.parent
        batches_root=self.root/'batches'
        if (output.name!='output' or item_root.name!=target['itemId']
            or batch_root.parent!=batches_root or not item_root.is_dir()
            or output.is_symlink() or item_root.is_symlink() or batch_root.is_symlink()
            or batches_root.is_symlink() or item_root.resolve()!=item_root):
            LOG.warning('Accepted target scratch path was not safe to remove: %s',item_root)
            return False
        try:
            shutil.rmtree(item_root)
            # Another target in this batch may still be running or uploading.
            try: batch_root.rmdir()
            except OSError: pass
            return True
        except OSError as exc:
            # Acceptance is durable; a local cleanup error must not turn it into
            # a prediction failure or revoke another target's lease.
            LOG.warning('Accepted target scratch cleanup failed: %s (%s)',item_root,type(exc).__name__)
            return False

    def run_once(self):
        response=self.json_request('/worker/v1/claim',{'session':self.session,'gpuGroup':self.group})
        batch=response.get('batch')
        if not batch: return False
        targets={target['itemId']:target for target in batch['targets']}
        with self.lock:
            self.leases.update(targets)
        batch_root=self.root/'batches'/batch['id']
        batch_root.mkdir(parents=True,exist_ok=False)
        requests,terminal=[],set()
        def fail_target(item_id,error):
            target=targets[item_id]
            try:
                self.json_request('/worker/v1/failure',{'itemId':item_id,'leaseToken':target['leaseToken'],'error':str(error)[:2000]})
            except urllib.error.HTTPError as exc:
                if exc.code not in (404,409): raise
            finally:
                with self.lock: self.leases.pop(item_id,None)
                terminal.add(item_id)
        for item_id,target in targets.items():
            try:
                target['deliveryState']='download'
                requests.append(self.download_prepared(target,batch_root))
                target['deliveryState']='executing'
            except Exception as exc: fail_target(item_id,'Preparation transfer failed: '+str(exc))
        outputs={request['targetId']:request['outputDir'] for request in requests}
        def emit(event):
            if event.get('type')!='target_result': return
            item_id=event.get('targetId')
            if item_id in terminal: return
            if event.get('retryable') and event.get('status')!='completed':
                terminal.add(item_id)
                with self.lock: self.leases.pop(item_id,None)
                return
            if event.get('status')=='completed':
                try:
                    targets[item_id]['deliveryState']='upload'
                    receipt=self.upload_result(targets[item_id],outputs[item_id],event.get('resultJson'))
                    self.processed+=1
                    terminal.add(item_id)
                    with self.lock: self.leases.pop(item_id,None)
                    self._discard_accepted_target(targets[item_id],outputs[item_id],receipt)
                except UploadPendingError as exc:
                    LOG.error('%s',exc)
                    terminal.add(item_id)
                    with self.lock: self.leases.pop(item_id,None)
                except Exception as exc:
                    fail_target(item_id,'Result upload/verification failed: '+str(exc))
            else:
                fail_target(item_id,event.get('error','Resident target failed'))
        def cancelled():
            with self.lock:
                return self.stop_event.is_set() or any(item_id in self.revoked for item_id in targets if item_id not in terminal)
        try:
            if requests:
                results=self.resident.predict_batch(requests,cancelled=cancelled,emit=emit)
                for result in results:
                    emit(dict(result,type='target_result'))
        except Exception as exc:
            for result in getattr(exc,'completed',[]):
                emit(dict(result,type='target_result'))
            # Preserve accepted results. Unresolved leases are no longer renewed
            # and the controller requeues only those targets after lease expiry.
            LOG.error('Resident batch interrupted: %s; unfinished targets will recover after lease expiry.',type(exc).__name__)
            if not self.stop_event.is_set() and not self.drain_event.is_set():
                self._ready(restart=True)
        finally:
            with self.lock:
                for item_id in targets: self.leases.pop(item_id,None)
        if self.resident.broken and not self.stop_event.is_set() and not self.drain_event.is_set():
            self._ready(restart=True)
        self._heartbeat()
        return True

    def run(self,once=False):
        try:
            self.start()
            while not self.stop_event.is_set() and not self.drain_event.is_set():
                try:
                    worked=self.run_once()
                except urllib.error.HTTPError as exc:
                    if exc.code not in {408,429,500,502,503,504}:
                        raise
                    LOG.warning('Controller temporarily unavailable: HTTP %s; keeping resident process loaded.',exc.code)
                    self.stop_event.wait(2)
                    continue
                except (urllib.error.URLError,TimeoutError) as exc:
                    LOG.warning('Controller connection unavailable; keeping resident process loaded.')
                    self.stop_event.wait(2)
                    continue
                if once: break
                if not worked: self.stop_event.wait(1)
        finally:
            self.close()

    def drain(self):
        self.drain_event.set()

    def close(self):
        self.drain_event.set()
        self.stop_event.set()
        if self.heartbeat_thread:
            self.heartbeat_thread.join(2)
        self.resident.close()
        self._release_devices()
        if self.session:
            try: self._heartbeat()
            except (OSError,ValueError): pass


def main():
    parser=argparse.ArgumentParser(description='Run one long-lived protein prediction worker against a trusted controller.')
    parser.add_argument('--url',required=True)
    parser.add_argument('--worker-id',required=True)
    parser.add_argument('--token-env',required=True)
    parser.add_argument('--engine',choices=['reference','alphafold3'],required=True)
    parser.add_argument('--runtime-version',required=True)
    parser.add_argument('--work-dir',required=True)
    parser.add_argument('--resident-command',required=True,help='JSON argv array for scripts/resident_runner.py')
    parser.add_argument('--gpu-devices',default='')
    parser.add_argument('--demo',action='store_true')
    parser.add_argument('--unsafe-http',action='store_true')
    parser.add_argument('--samples',type=int)
    parser.add_argument('--recycles',type=int)
    parser.add_argument('--resident-startup-timeout',type=float,default=600)
    parser.add_argument('--resident-request-timeout',type=float,default=7200,
                        help='Total wall time for a resident batch, including per-target uploads.')
    parser.add_argument('--http-timeout',type=float,default=60)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    client=WorkerClient(args.url,args.worker_id,os.environ.get(args.token_env,''),args.engine,args.runtime_version,args.work_dir,
        json.loads(args.resident_command),gpu_devices=[d.strip() for d in args.gpu_devices.split(',') if d.strip()],demo=args.demo,
        unsafe_http=args.unsafe_http,samples=args.samples,recycles=args.recycles,
        resident_startup_timeout=args.resident_startup_timeout,resident_request_timeout=args.resident_request_timeout,
        http_timeout=args.http_timeout)
    signal.signal(signal.SIGTERM,lambda *_:client.drain())
    signal.signal(signal.SIGINT,lambda *_:client.drain())
    client.run(once=args.once)

if __name__=='__main__':
    main()
