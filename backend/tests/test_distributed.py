"""Real localhost worker protocol tests; fixture workers never execute GPUs."""
import io
import copy
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

from backend import engine
from backend.distributed import safe_extract,digest_file
from backend.server import StudioApplication,StudioHTTPServer
from backend.worker_client import WorkerClient
from backend.worker_pipeline import write_json
from backend.store import utcnow
from backend.tests.test_pipeline import wait_for

ROOT=Path(__file__).resolve().parents[2]
TOKENS={'LOCAL_TEST_TOKEN':'local-worker-'+'a'*32,'CLOUD_TEST_TOKEN':'cloud-worker-'+'b'*32}


class BundleTests(unittest.TestCase):
    def test_zip_path_symlink_and_expansion_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for index,kind in enumerate(('traversal','symlink','limit')):
                bundle=root/f'{kind}.zip'
                with zipfile.ZipFile(bundle,'w') as archive:
                    if kind=='traversal': archive.writestr('../escape','bad')
                    elif kind=='symlink':
                        entry=zipfile.ZipInfo('link')
                        entry.external_attr=(stat.S_IFLNK|0o777)<<16
                        archive.writestr(entry,'/etc/passwd')
                    else: archive.writestr('large',b'a'*101)
                with self.assertRaises(ValueError):
                    safe_extract(bundle,root/f'extracted-{index}',max_expanded=100)
                self.assertFalse((root/f'extracted-{index}').exists())
            self.assertFalse((root.parent/'escape').exists())

    def test_non_tls_remote_controller_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'require TLS'):
                WorkerClient('http://gpu.example','local','a'*32,'reference','fixtures',directory,['python'],demo=True)


class DistributedHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        config={'schemaVersion':1,'leaseSeconds':3,'batchTargets':2,'batchResidues':1000,
                'maxBundleBytes':20*1024*1024,'maxExpandedBytes':40*1024*1024,
                'planned':{'localGpuCards':16},'workers':[
                {'id':'local-node','name':'Local fixture','tokenEnv':'LOCAL_TEST_TOKEN','location':'local','mode':'fixture','engines':['reference']},
                {'id':'cloud-node','name':'Aliyun fixture','tokenEnv':'CLOUD_TEST_TOKEN','location':'aliyun','mode':'fixture','engines':['reference']}]}
        config_path=self.root/'workers.json'
        config_path.write_text(json.dumps(config))
        self.environment=patch.dict(os.environ,dict(TOKENS,PROTEIN_STUDIO_DISTRIBUTED='1',
            PROTEIN_STUDIO_PREDICTION_MODE='workers',PROTEIN_STUDIO_WORKER_CONFIG=str(config_path)),clear=False)
        self.environment.start()
        self.app=StudioApplication(self.root/'data')
        self.httpd=StudioHTTPServer(('127.0.0.1',0),self.app)
        self.thread=threading.Thread(target=self.httpd.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        self.thread.start()
        self.app.start()
        self.url=f'http://127.0.0.1:{self.httpd.server_port}'
        self.clients=[]

    def tearDown(self):
        for client in self.clients: client.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.close()
        self.thread.join(2)
        self.environment.stop()
        self.temp.cleanup()

    def request(self,path,payload=None,token=None,headers=None,data=None):
        extra={}
        if token: extra['Authorization']='Bearer '+token
        extra.update(headers or {})
        if payload is not None:
            data=json.dumps(payload).encode()
            extra['Content-Type']='application/json'
        request=urllib.request.Request(self.url+path,data=data,headers=extra)
        try: response=urllib.request.urlopen(request,timeout=10)
        except urllib.error.HTTPError as exc: response=exc
        raw=response.read()
        body=json.loads(raw) if 'application/json' in response.headers.get('Content-Type','') else raw
        return response.status,body,response.headers

    def campaign(self,count=3,placement='hybrid'):
        # Worker-mode preparation is admitted against registered destinations.
        # These protocol fixtures register before their resident clients start.
        self.register('local-node')
        self.register('cloud-node')
        example=engine.load_example('crambin')
        campaign=self.app.create_campaign({'name':'Remote campaign','engine':'reference','databaseProfile':'reference',
            'settings':{'example':'crambin','seeds':[1],'numSamples':1},'status':'active','placement':placement})
        self.app.append_campaign_items(campaign['id'],{'items':[{'targetId':f'target-{i}','chains':example['chains']} for i in range(count)]})
        self.app.pipeline.store.control(campaign['id'],'seal')
        wait_for(lambda:self.app.pipeline.store.get(campaign['id'])['counts']['ready']==count)
        return campaign['id']

    def register(self,node='local-node',token=None):
        token=token or TOKENS['LOCAL_TEST_TOKEN' if node=='local-node' else 'CLOUD_TEST_TOKEN']
        status,body,_=self.request('/worker/v1/register',{'workerId':node,'engine':'reference','runtimeVersion':'fixtures-v1','gpuGroups':[[]]},token)
        self.assertEqual(status,200,body)
        return body,token

    def client(self,node='local-node'):
        client=WorkerClient(self.url,node,TOKENS['LOCAL_TEST_TOKEN' if node=='local-node' else 'CLOUD_TEST_TOKEN'],
            'reference','fixtures-v1',self.root/node,[sys.executable,str(ROOT/'scripts/resident_runner.py'),'--engine','reference'],demo=True)
        self.clients.append(client)
        client.start()
        return client

    def test_authentication_mode_and_existing_gpu_jobs_are_isolated(self):
        self.assertEqual(self.request('/worker/v1/register',{})[0],401)
        self.assertEqual(self.request('/worker/v1/register',{},'wrong')[0],401)
        status,body,_=self.request('/api/jobs',{'name':'Real GPU task','engine':'alphafold3','chains':[{'id':'A','sequence':'ACD'}]})
        self.assertEqual(status,400,body)
        self.assertIn('campaigns',body['error'])
        self.assertEqual(self.app.store.list(),[])
        status,_,_=self.request('/api/jobs/batch',{'jobs':[{'engine':'alphafold3','chains':[{'id':'A','sequence':'ACD'}]}]})
        self.assertEqual(status,400)
        queued=self.app.store.create({'name':'Previously queued','engine':'alphafold3','chains':[{'id':'A','sequence':'ACD'}]})
        time.sleep(.6)
        self.assertEqual(self.app.store.get(queued['id'])['status'],'queued')
        registration,token=self.register()
        status,_,_=self.request('/worker/v1/claim',{'session':registration['session']},token,headers={'Origin':'https://evil.example'})
        self.assertEqual(status,401)

    def test_two_fixture_nodes_real_transfers_resident_reuse_and_final_artifacts(self):
        cid=self.campaign(5)
        local,cloud=self.client(),self.client('cloud-node')
        pid=local.resident.pid
        self.assertTrue(local.run_once())
        self.assertTrue(cloud.run_once())
        self.assertTrue(local.run_once())
        wait_for(lambda:self.app.pipeline.store.get(cid)['counts']['completed']==5,15)
        self.assertEqual(local.resident.pid,pid)
        cluster=self.app.distributed.cluster()
        self.assertEqual(len(cluster['nodes']),2)
        self.assertEqual({n['location'] for n in cluster['nodes']},{'local','aliyun'})
        self.assertTrue(all(n['demo'] and n['devices']==[] and n['modelLoads']==0 for n in cluster['nodes']))
        self.assertTrue(all(n['modelInitCount']==1 for n in cluster['nodes']))
        self.assertEqual(len(cluster['batches']),3)
        self.assertEqual(sum(b['completed'] for b in cluster['batches']),5)
        self.assertEqual(len(cluster['transfers']),10)
        self.assertTrue(all(t['bytes']>0 and len(t['sha256'])==64 for t in cluster['transfers']))
        self.assertTrue(cluster['events'])
        item=self.app.pipeline.store.items(cid)['items'][0]
        detail=self.app.pipeline.detail(cid,item['id'])
        self.assertFalse(detail['result']['provenance']['newPrediction'])
        self.assertEqual(detail['result']['provenance']['attempt'],2)
        self.assertIn(detail['result']['provenance']['workerId'],{'local-node','cloud-node'})
        self.assertEqual(self.request(detail['result']['structureUrl'])[0],200)
        self.assertNotIn(TOKENS['LOCAL_TEST_TOKEN'],json.dumps(cluster))

    def test_re_registration_and_lease_expiry_fence_old_uploads(self):
        cid=self.campaign(2)
        registration,token=self.register()
        _,response,_=self.request('/worker/v1/claim',{'session':registration['session']},token)
        target=response['batch']['targets'][0]
        self.assertFalse(self.app.pipeline.store.heartbeat(target['itemId'],target['leaseToken']))
        new,_=self.register()
        self.assertEqual(self.request('/worker/v1/heartbeat',{'session':registration['session'],'leases':[]},token)[0],401)
        self.assertEqual(self.request(target['preparedUrl'],token=token,headers={'X-Lease-Token':target['leaseToken']})[0],409)
        self.assertEqual(self.app.pipeline.store.get(cid)['counts']['ready'],2)
        _,response,_=self.request('/worker/v1/claim',{'session':new['session']},token)
        replacement=response['batch']['targets'][0]
        self.assertNotEqual(replacement['leaseToken'],target['leaseToken'])
        with self.app.pipeline.store.connection(True) as connection:
            connection.execute('UPDATE campaign_items SET lease_until=? WHERE remote_owner=?',(time.time()-1,'local-node'))
        self.app.pipeline.store.recover()
        self.assertEqual(self.request(replacement['resultUrl'],token=token,data=b'bad',headers={
            'X-Lease-Token':replacement['leaseToken'],'Content-Type':'application/zip','X-Content-SHA256':'0'*64})[0],409)
        self.assertEqual(self.app.pipeline.store.get(cid)['counts']['ready'],2)

    def test_bad_sha_zip_and_partial_batch_completion_preserve_accepted_target(self):
        cid=self.campaign(2)
        worker=self.client()
        batch=worker.json_request('/worker/v1/claim',{'session':worker.session,'gpuGroup':[]})['batch']
        targets=batch['targets']
        root=self.root/'manual-batch'
        first=worker.download_prepared(targets[0],root)
        result=worker.resident.predict_batch([first])[0]
        self.assertEqual(result['status'],'completed')
        worker.upload_result(targets[0],first['outputDir'])
        sent=Path(first['outputDir']).parent/'result.zip'
        duplicate_headers={'X-Lease-Token':targets[0]['leaseToken'],'Content-Type':'application/zip','X-Content-SHA256':digest_file(sent)}
        status,receipt,_=self.request(targets[0]['resultUrl'],token=worker.token,data=sent.read_bytes(),headers=duplicate_headers)
        self.assertEqual(status,200,receipt)
        self.assertTrue(receipt['alreadyAccepted'])
        changed=self.root/'different.zip'
        with zipfile.ZipFile(changed,'w') as archive: archive.writestr('different','cannot replace accepted result')
        duplicate_headers['X-Content-SHA256']=digest_file(changed)
        self.assertEqual(self.request(targets[0]['resultUrl'],token=worker.token,data=changed.read_bytes(),headers=duplicate_headers)[0],409)
        wait_for(lambda:self.app.pipeline.store.get(cid)['counts']['completed']==1)
        bad=self.root/'bad.zip'
        with zipfile.ZipFile(bad,'w') as archive: archive.writestr('../escape','bad')
        target=targets[1]
        headers={'X-Lease-Token':target['leaseToken'],'Content-Type':'application/zip','X-Content-SHA256':'0'*64}
        self.assertEqual(self.request(target['resultUrl'],token=worker.token,data=bad.read_bytes(),headers=headers)[0],400)
        headers['X-Content-SHA256']=digest_file(bad)
        self.assertEqual(self.request(target['resultUrl'],token=worker.token,data=bad.read_bytes(),headers=headers)[0],400)
        with self.app.pipeline.store.connection(True) as connection:
            connection.execute('UPDATE campaign_items SET lease_until=? WHERE id=?',(time.time()-1,target['itemId']))
        self.app.pipeline.store.recover()
        counts=self.app.pipeline.store.get(cid)['counts']
        self.assertEqual(counts['completed'],1)
        self.assertEqual(counts['ready'],1)
        self.assertEqual(self.app.pipeline.store.item(cid,targets[0]['itemId'])['stage'],'completed')

    def test_lost_upload_response_retries_same_zip_without_rerunning_model(self):
        cid=self.campaign(1)
        worker=self.client()
        original=worker.request
        calls=[]
        def lost_once(path,*args,**kwargs):
            response=original(path,*args,**kwargs)
            if path.startswith('/worker/v1/result/'):
                calls.append(kwargs['headers']['X-Content-SHA256'])
                if len(calls)==1:
                    response.read()
                    response.close()
                    raise OSError('Simulated lost acknowledgement after controller commit')
            return response
        pid=worker.resident.pid
        with patch.object(worker,'request',side_effect=lost_once):
            self.assertTrue(worker.run_once())
        wait_for(lambda:self.app.pipeline.store.get(cid)['counts']['completed']==1)
        self.assertEqual(len(calls),2)
        self.assertEqual(len(set(calls)),1)
        self.assertEqual(worker.resident.pid,pid)
        self.assertEqual(worker.processed,1)
        cluster=self.app.distributed.cluster()
        self.assertEqual(len([t for t in cluster['transfers'] if t['direction']=='upload']),1)

    def test_draining_and_compatible_settings_limit_claims(self):
        cid=self.campaign(3)
        registration,token=self.register()
        response=self.request('/worker/v1/heartbeat',{'session':registration['session'],'draining':True,'leases':[]},token)
        self.assertEqual(response[0],200)
        self.assertIsNone(self.request('/worker/v1/claim',{'session':registration['session']},token)[1]['batch'])
        self.request('/worker/v1/heartbeat',{'session':registration['session'],'draining':False,'leases':[]},token)
        # A later compatible row must be reachable even when earlier inputs have
        # a different resident signature. This changes only test input metadata.
        with self.app.pipeline.store.connection(True) as connection:
            rows=list(connection.execute('SELECT id,input_json FROM campaign_items WHERE campaign_id=? ORDER BY seq',(cid,)))
            changed=json.loads(rows[0]['input_json']);changed['numRecycles']=4
            connection.execute('UPDATE campaign_items SET input_json=? WHERE id=?',(json.dumps(changed),rows[0]['id']))
        _,registration,_=self.request('/worker/v1/register',{'workerId':'local-node','engine':'reference',
            'runtimeVersion':'fixtures-v1','gpuGroups':[[]],'numSamples':1,'numRecycles':3},token)
        batch=self.request('/worker/v1/claim',{'session':registration['session']},token)[1]['batch']
        self.assertEqual(len(batch['targets']),2)
        self.assertEqual(batch['signature']['numRecycles'],3)
        self.assertNotIn(rows[0]['id'],[target['itemId'] for target in batch['targets']])

    def test_administrator_residue_limit_excludes_long_target(self):
        cid=self.campaign(1)
        self.app.distributed.config['workers'][0]['maxResidues']=45
        registration,token=self.register()
        self.assertEqual(registration['maxResidues'],45)
        self.assertIsNone(self.request('/worker/v1/claim',{'session':registration['session']},token)[1]['batch'])
        cloud,cloud_token=self.register('cloud-node')
        batch=self.request('/worker/v1/claim',{'session':cloud['session']},cloud_token)[1]['batch']
        self.assertEqual(len(batch['targets']),1)
        self.assertEqual(batch['targets'][0]['campaignId'],cid)
        nodes=self.app.distributed.cluster()['nodes']
        self.assertEqual(next(n for n in nodes if n['id']=='local-node')['maxResidues'],45)

    def test_placement_restricts_authenticated_worker_location(self):
        cid=self.campaign(1,placement='local')
        cloud,cloud_token=self.register('cloud-node')
        self.assertIsNone(self.request('/worker/v1/claim',{'session':cloud['session']},cloud_token)[1]['batch'])
        local,local_token=self.register()
        batch=self.request('/worker/v1/claim',{'session':local['session']},local_token)[1]['batch']
        self.assertEqual(batch['targets'][0]['campaignId'],cid)
        self.assertEqual(self.app.pipeline.store.get(cid)['placement'],'local')
        status,_,_=self.request('/api/campaigns',{'name':'bad placement','engine':'reference','placement':'elsewhere'})
        self.assertEqual(status,400)

    def test_reclaimed_target_events_have_one_correct_attempt_assignment(self):
        cid=self.campaign(1)
        local,token=self.register()
        first=self.request('/worker/v1/claim',{'session':local['session']},token)[1]['batch']
        target=first['targets'][0]
        with self.app.pipeline.store.connection(True) as connection:
            connection.execute('UPDATE campaign_items SET lease_until=? WHERE id=?',(time.time()-1,target['itemId']))
        self.app.pipeline.store.recover()
        cloud=self.client('cloud-node')
        second=cloud.json_request('/worker/v1/claim',{'session':cloud.session,'gpuGroup':[]})['batch']
        replacement=second['targets'][0]
        request=cloud.download_prepared(replacement,self.root/'reclaimed-batch')
        cloud.resident.predict_batch([request])
        cloud.upload_result(replacement,request['outputDir'])
        wait_for(lambda:self.app.pipeline.store.get(cid)['counts']['completed']==1)
        events=self.app.distributed.cluster()['events']
        claims=[e for e in events if e['message'].startswith('Remote worker claimed')]
        self.assertEqual(len(claims),2)
        self.assertEqual({(e['nodeId'],e['batchId']) for e in claims},
                         {('local-node',first['id']),('cloud-node',second['id'])})
        self.assertEqual(len({e['id'] for e in events}),len(events))
        uploads=[e for e in events if e['message'].startswith('Verified remote')]
        self.assertEqual(len(uploads),1)
        self.assertEqual((uploads[0]['nodeId'],uploads[0]['batchId']),('cloud-node',second['id']))
        recoveries=[e for e in events if e['message'].startswith('Lease revoked')]
        self.assertTrue(recoveries)
        self.assertTrue(all(e['nodeId'] is None and e['batchId'] is None and e['origin']=='controller' for e in recoveries))

class PipelineOverviewTests(unittest.TestCase):
    """Metadata scale/eligibility tests run without background scheduling races."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        config={'schemaVersion':1,'leaseSeconds':120,'batchTargets':2,'batchResidues':1000,
                'workers':[
                    {'id':'local-node','tokenEnv':'LOCAL_TEST_TOKEN','location':'local','mode':'fixture','engines':['reference']},
                    {'id':'cloud-node','tokenEnv':'CLOUD_TEST_TOKEN','location':'aliyun','mode':'fixture','engines':['reference']}]}
        path=self.root/'workers.json'
        path.write_text(json.dumps(config))
        self.environment=patch.dict(os.environ,dict(TOKENS,PROTEIN_STUDIO_DISTRIBUTED='1',
            PROTEIN_STUDIO_PREDICTION_MODE='workers',PROTEIN_STUDIO_WORKER_CONFIG=str(path)))
        self.environment.start()
        self.app=StudioApplication(self.root/'data')
        self.store=self.app.pipeline.store
        self.controller=self.app.distributed

    def tearDown(self):
        self.app.close()
        self.environment.stop()
        self.temp.cleanup()

    def register(self, location='local'):
        policy=next(n for n in self.controller.config['workers'] if n['location']==location)
        registration=self.controller.register(policy,dict(engine='reference',runtimeVersion='fixtures-v1',gpuGroups=[[]]))
        return policy,registration

    def campaign(self,count,placement='hybrid',status='active',stage='waiting_input',name='Queue test'):
        example=engine.load_example('crambin')
        campaign=self.app.create_campaign({'name':name,'engine':'reference','databaseProfile':'reference',
            'settings':{'example':'crambin','seeds':[1],'numSamples':1},'status':status,'placement':placement})
        cid=campaign['id']
        for begin in range(0,count,200):
            self.app.append_campaign_items(cid,{'items':[{'targetId':f'target-{i}','name':f'Candidate {i}',
                'chains':example['chains']} for i in range(begin,min(begin+200,count))]})
        self.store.control(cid,'seal')
        if stage!='waiting_input':
            with self.store.connection(True) as conn:
                conn.execute('UPDATE campaign_items SET stage=? WHERE campaign_id=?',(stage,cid))
        return cid

    def test_global_counts_include_targets_outside_the_fifty_recent_batches(self):
        self.register()
        cid=self.campaign(75,stage='completed',name='Completed batch history')
        with self.store.connection(True) as conn:
            items=conn.execute('SELECT id,target_id FROM campaign_items WHERE campaign_id=? ORDER BY seq',(cid,)).fetchall()
            for index in range(60):
                bid=f'{index:032x}'
                conn.execute('INSERT INTO worker_batches VALUES(?,?,?,?,?,?)',
                    (bid,'local-node','{}','[]',f'2026-01-01T00:{index:02d}:00.000Z',utcnow()))
            for index,item in enumerate(items):
                bid=f'{min(index,59):032x}'
                conn.execute('INSERT INTO worker_batch_items VALUES(?,?,?,?,?,?,?,?)',
                    (bid,item['id'],cid,item['target_id'],'past-token',2,'accepted','uploaded'))
                self.store.event(conn,item['id'],'completed','Stage published: completed.')
            # Old events and non-terminal upload events must not count as new delivery.
            conn.execute("INSERT INTO campaign_events(item_id,time,stage,message) VALUES(?,?,'completed','Stage published: completed.')",
                         (items[0]['id'],'2000-01-01T00:00:00.000Z'))
            self.store.event(conn,items[0]['id'],'validating','Verified remote output accepted.')
            self.store.event(conn,items[1]['id'],'waiting_input','Routing deferred: no online compatible worker; prepared cache retained.')
            self.store.event(conn,items[2]['id'],'ready','Lease revoked; safe stage recovery.')
        overview=self.controller.cluster()
        self.assertEqual(len(overview['batches']),50)
        self.assertLess(sum(len(batch['members']) for batch in overview['batches']),75)
        pipeline=overview['pipeline']
        self.assertEqual(pipeline['totalItems'],75)
        self.assertEqual(pipeline['counts']['completed'],75)
        self.assertEqual(pipeline['campaigns']['complete'],1)
        self.assertEqual(pipeline['campaigns']['activeWithUnfinishedItems'],0)
        self.assertEqual(pipeline['recent']['completed'],{'events':75,'targets':75})
        self.assertEqual(pipeline['recent']['recovered'],{'events':1,'targets':1})
        member=overview['batches'][0]['members'][0]
        self.assertEqual(member['campaignName'],'Completed batch history')
        self.assertTrue(member['name'].startswith('Candidate '))

    def test_paused_targets_are_visible_but_excluded_from_active_budget_and_routing(self):
        policy,registration=self.register()
        self.campaign(2,stage='ready')
        self.campaign(4,status='paused',stage='ready')
        self.app.pipeline.ready_cap=4
        pipeline=self.controller.cluster()['pipeline']
        self.assertEqual(pipeline['counts']['ready'],6)
        self.assertEqual(pipeline['campaigns']['paused'],1)
        self.assertEqual(pipeline['queue']['pausedReady'],4)
        self.assertEqual(pipeline['queue']['readyActive'],2)
        self.assertEqual(pipeline['queue']['used'],2)
        self.assertEqual(pipeline['queue']['availableSlots'],2)
        self.assertEqual(pipeline['locations']['local']['eligibleReady'],2)
        self.assertEqual(pipeline['locations']['aliyun']['readyCandidates'],2)
        self.assertEqual(pipeline['locations']['aliyun']['reasons'][0]['code'],'no_registered_worker')
        self.controller.claim(policy,{'session':registration['session']})
        self.campaign(1,stage='ready')
        pipeline=self.controller.cluster()['pipeline']
        self.assertEqual(pipeline['locations']['local']['eligibleReady'],0)
        self.assertEqual(pipeline['locations']['local']['reasons'][0]['code'],'gpu_groups_busy')
        self.assertEqual(pipeline['queue']['routableReady'],1)  # CPU may prepare ahead of a busy group.

    def test_no_worker_means_waiting_not_failure_and_reports_ready_blockage(self):
        self.campaign(3,placement='local',stage='ready')
        waiting=self.campaign(1,placement='local')
        pipeline=self.controller.cluster()['pipeline']
        self.assertEqual(pipeline['locations']['local']['blockedReady'],3)
        self.assertEqual(pipeline['locations']['local']['reasons'],[{
            'code':'no_registered_worker','label':'No administrator-configured worker has registered at this location.','count':3}])
        self.assertEqual(pipeline['locations']['aliyun']['readyCandidates'],0)
        self.assertIsNone(self.store.claim('prepare',preparation_route=self.controller.preparation_route))
        self.assertEqual(self.store.get(waiting)['counts']['waiting_input'],1)
        self.assertEqual(self.controller.cluster()['pipeline']['counts']['failed'],0)

    def test_unroutable_ready_cap_and_six_hundred_earlier_targets_do_not_starve_local_work(self):
        cloud=self.campaign(600,placement='aliyun')
        local=self.campaign(1,placement='local')
        with self.store.connection(True) as conn:
            conn.execute("UPDATE campaign_items SET stage='ready',prepared_path='retained-prepared.json' WHERE id IN (SELECT id FROM campaign_items WHERE campaign_id=? ORDER BY seq LIMIT 64)",(cloud,))
        self.register()
        claim=self.store.claim('prepare',ready_cap=64,preparation_route=self.controller.preparation_route)
        self.assertIsNotNone(claim)
        self.assertEqual(claim['campaign_id'],local)
        self.assertEqual(self.store.get(cloud)['counts']['waiting_input'],600)
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM campaign_items WHERE campaign_id=? AND prepared_path='retained-prepared.json'",(cloud,)).fetchone()[0],64)
        # Existing in-flight work still counts; routing changes cannot create
        # unbounded CPU preparations when the remaining budget is exhausted.
        self.campaign(1,placement='local')
        self.assertIsNone(self.store.claim('prepare',ready_cap=1,preparation_route=self.controller.preparation_route))

    def test_offline_deferral_retains_and_reuses_real_preparation_cache(self):
        self.register()
        cid=self.campaign(1,placement='local')
        first=self.store.claim('prepare',preparation_route=self.controller.preparation_route)
        scheduler=self.app.pipeline
        directory=scheduler._attempt_directory(first,'prepare')
        first['job']['cacheRoot']=str(scheduler.cache_root)
        prepared=scheduler._module().prepare_stage(first['job'],directory,lambda *args:None,lambda:False)
        self.assertFalse(prepared['cacheHit'])
        descriptor=directory/'prepared.json'
        write_json(descriptor,prepared)
        self.assertTrue(self.store.advance(first['id'],first['token'],'ready',prepared_path=str(descriptor)))
        with self.store.connection(True) as conn:
            conn.execute('UPDATE worker_nodes SET last_seen=?',(time.time()-1000,))
        self.assertIsNone(self.store.claim('prepare',preparation_route=self.controller.preparation_route))
        self.assertTrue(descriptor.is_file())
        self.assertEqual(self.store.get(cid)['counts']['waiting_input'],1)
        self.register()
        second=self.store.claim('prepare',preparation_route=self.controller.preparation_route)
        second['job']['cacheRoot']=str(scheduler.cache_root)
        repeat=scheduler._module().prepare_stage(second['job'],scheduler._attempt_directory(second,'prepare'),lambda *args:None,lambda:False)
        self.assertTrue(repeat['cacheHit'])
        self.assertEqual(repeat['cacheDirectory'],prepared['cacheDirectory'])
        self.assertEqual(self.controller.cluster()['pipeline']['recent']['recovered']['targets'],0)

    def native_claim_fixture(self):
        """Protocol-only stand-in: no CUDA, weights, or actual AF3 execution."""
        policy=self.controller.config['workers'][0]
        policy.update(mode='real',engines=['alphafold3'])
        registration=self.controller.register(policy,dict(engine='alphafold3',runtimeVersion='controlled-af3-test',
            gpuGroups=[['TEST-ONLY-NO-GPU']],numSamples=1,numRecycles=3))
        campaign=self.store.create(dict(name='Synthetic AF3 protocol validation',engine='alphafold3',
            status='active',placement='local',executionProfile={'version':'controlled-af3-test'}))
        job=engine.validate_submission(dict(engine='alphafold3',sequence='AG',seeds=[3],numSamples=1,numRecycles=3))
        self.store.append(campaign['id'],[dict(targetId='controlled-native-protocol',submission=job)])
        with self.store.connection(True) as conn:
            conn.execute("UPDATE campaign_items SET stage='ready' WHERE campaign_id=?",(campaign['id'],))
        target=self.controller.claim(policy,{'session':registration['session']})['batch']['targets'][0]
        execution=dict(nativeExecutionMode='resident-native',isDemo=False,modelLoads=1,nodeModelLoads=4,
            residentPid=12345,clientGeneration=4,residentSessionId='test-only-session',
            modelSignature=dict(engine='alphafold3',modelVersion='controlled-af3-test',numSamples=1,numRecycles=3))
        return policy,target,execution

    def native_bundle_fixture(self,target,execution,name):
        # Small original-format result files for controller parsing, deliberately
        # without the derived result.json that contains remote filesystem paths.
        structure='''data_test
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.type_symbol
_atom_site.label_atom_id
_atom_site.label_alt_id
_atom_site.label_comp_id
_atom_site.label_asym_id
_atom_site.label_seq_id
_atom_site.Cartn_x
_atom_site.Cartn_y
_atom_site.Cartn_z
_atom_site.occupancy
_atom_site.B_iso_or_equiv
_atom_site.auth_seq_id
_atom_site.auth_asym_id
_atom_site.pdbx_PDB_model_num
ATOM 1 C CA . ALA A 1 1 2 3 1 91 1 A 1
ATOM 2 C CA . GLY A 2 4 5 6 1 81 2 A 1
#
'''
        bundle=self.root/(name+'.zip')
        with zipfile.ZipFile(bundle,'w') as archive:
            archive.writestr('manifest.json',json.dumps(dict(schemaVersion=1,itemId=target['itemId'],execution=execution)))
            archive.writestr('payload/protein/seed-3_sample-0/protein_model.cif',structure)
            archive.writestr('payload/protein/seed-3_sample-0/protein_summary_confidences.json',json.dumps({'ptm':.8,'ranking_score':.7}))
        return bundle

    def test_af3_native_upload_rejects_reference_or_mismatched_resident_claims(self):
        policy,target,execution=self.native_claim_fixture()
        changes=[lambda e:e.update(nativeExecutionMode='resident-reference'),lambda e:e.update(isDemo=True),
                 lambda e:e.update(modelLoads=2),lambda e:e.update(modelLoads=True),
                 lambda e:e['modelSignature'].update(engine='reference'),
                 lambda e:e['modelSignature'].update(modelVersion='different-runtime'),
                 lambda e:e['modelSignature'].update(numSamples=2),lambda e:e['modelSignature'].update(numRecycles=4)]
        for index,change in enumerate(changes):
            altered=copy.deepcopy(execution)
            change(altered)
            bundle=self.native_bundle_fixture(target,altered,f'rejected-{index}')
            with self.subTest(index=index),self.assertRaisesRegex(ValueError,'AF3'):
                self.controller.accept_result(policy,target['itemId'],target['leaseToken'],bundle,digest_file(bundle))
            with self.store.connection() as conn:
                item=conn.execute('SELECT stage,candidate_path FROM campaign_items WHERE id=?',(target['itemId'],)).fetchone()
                self.assertEqual(item['stage'],'predicting')
                self.assertIsNone(item['candidate_path'])

    def test_af3_raw_upload_retains_validated_native_identity_and_requested_settings(self):
        policy,target,execution=self.native_claim_fixture()
        bundle=self.native_bundle_fixture(target,execution,'accepted-native-format')
        receipt=self.controller.accept_result(policy,target['itemId'],target['leaseToken'],bundle,digest_file(bundle))
        self.assertTrue(receipt['accepted'])
        with self.store.connection() as conn:
            item=conn.execute('SELECT stage,candidate_path FROM campaign_items WHERE id=?',(target['itemId'],)).fetchone()
        self.assertEqual(item['stage'],'validating')
        result=json.loads(Path(item['candidate_path']).read_text())
        provenance=result['provenance']
        self.assertEqual(provenance['nativeExecutionMode'],'resident-native')
        self.assertEqual(provenance['transportMode'],'resident-worker')
        self.assertFalse(provenance['isDemo'])
        self.assertEqual(provenance['engineVersion'],'controlled-af3-test')
        self.assertEqual(provenance['seeds'],[3])
        self.assertEqual((provenance['numSamples'],provenance['numRecycles']),(1,3))
        self.assertEqual((provenance['modelLoads'],provenance['nodeModelLoads']),(1,4))
        self.assertTrue(result['samplingComplete'])
        self.assertEqual(result['actualSampleCount'],1)
        self.assertIn('not remote GPU attestation',provenance['runtimeEvidence'])


if __name__=='__main__': unittest.main()
