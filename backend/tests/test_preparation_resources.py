"""Resource admission and real SQLite scheduling; no GPU/runtime dependency."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.preparation_resources import PreparationResourceGuard, parse_config
from backend.worker_pipeline import PipelineScheduler


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError('Resource scheduling condition timed out.')


class ResourceGuardTests(unittest.TestCase):
    def test_pipeline_exposes_positive_configured_input_limit(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'PROTEIN_STUDIO_MAX_RESIDUES':'777'}):
            scheduler = PipelineScheduler(directory, preparation_resources={})
            try:
                self.assertEqual(scheduler.config()['maxResiduesPerTarget'], 777)
            finally:
                scheduler.close()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'PROTEIN_STUDIO_MAX_RESIDUES':'0'}):
            with self.assertRaisesRegex(ValueError, 'positive integer'):
                PipelineScheduler(directory, preparation_resources={})

    def test_invalid_or_impossible_configuration_fails_at_construction(self):
        invalid = [[], {'cpuBudget':64}, {'cpuBudget':32,'cpuPerPrepare':64},
                   {'memoryBudgetBytes':100,'memoryPerPrepareBytes':101},
                   {'cpuBudget':float('inf'),'cpuPerPrepare':1},
                   {'cpuBudget':True,'cpuPerPrepare':1},
                   {'memoryBudgetBytes':10**1000,'memoryPerPrepareBytes':1},
                   {'memoryBudgetBytes':100.5,'memoryPerPrepareBytes':10},
                   {'diskMinFreeBytes':-1}, {'diskPerPrepareBytes':'1 GiB'},
                   {'cpuBuget':64}, {'cpuBudget':0,'cpuPerPrepare':1}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                PreparationResourceGuard(['/tmp'],config=value)
        with patch.dict(os.environ, {'PROTEIN_STUDIO_PREPARATION_RESOURCES_JSON':'{bad'}):
            with self.assertRaises(ValueError):
                parse_config()

    def test_unconfigured_resources_remain_unknown_and_do_not_probe_disk(self):
        with patch.dict(os.environ, {'PROTEIN_STUDIO_PREPARATION_RESOURCES_JSON':''}):
            guard = PreparationResourceGuard(['/unavailable'],disk_usage=lambda p: self.fail('Unexpected filesystem probe'))
        tokens = [guard.acquire() for _ in range(8)]
        snapshot = guard.snapshot()
        self.assertFalse(snapshot['configured'])
        self.assertEqual(snapshot['inflight']['count'],8)
        self.assertIsNone(snapshot['inflight']['cpuReserved'])
        self.assertIsNone(snapshot['inflight']['memoryReservedBytes'])
        self.assertFalse(snapshot['wouldBlock'])
        for token in tokens:guard.release(token)
        self.assertEqual(guard.snapshot()['inflight']['count'],0)

    def test_fifo_waiters_enter_in_order_after_release(self):
        guard = PreparationResourceGuard(['/tmp'],config={'cpuBudget':32,'cpuPerPrepare':32},poll_seconds=.05)
        first = guard.acquire()
        order = []
        def work(index):
            token=guard.acquire()
            try:
                order.append(index)
                time.sleep(.01)
            finally:guard.release(token)
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures=[]
            for index in range(3):
                futures.append(pool.submit(work,index))
                wait_for(lambda:guard.snapshot()['waiting']==index+1)
            self.assertEqual(order,[])
            self.assertEqual(guard.snapshot()['blockedReasons'][0]['code'],'cpu_budget')
            guard.release(first)
            for future in futures:future.result(timeout=3)
        self.assertEqual(order,[0,1,2])
        self.assertEqual(guard.snapshot()['inflight']['count'],0)

    def test_cancelled_head_waiter_does_not_block_following_thread(self):
        guard=PreparationResourceGuard(['/tmp'],config={'cpuBudget':1,'cpuPerPrepare':1},poll_seconds=.03)
        held=guard.acquire();cancel=threading.Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            cancelled=pool.submit(guard.acquire,cancel.is_set)
            wait_for(lambda:guard.snapshot()['waiting']==1)
            following=pool.submit(guard.acquire)
            wait_for(lambda:guard.snapshot()['waiting']==2)
            cancel.set()
            self.assertIsNone(cancelled.result(timeout=1))
            guard.release(held)
            token=following.result(timeout=1)
            guard.release(token)
        self.assertEqual(guard.snapshot()['waiting'],0)

    def test_close_notifies_long_wait_and_preserves_inflight_until_release(self):
        guard=PreparationResourceGuard(['/tmp'],config={'cpuBudget':1,'cpuPerPrepare':1},poll_seconds=30)
        held=guard.acquire()
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting=pool.submit(guard.acquire)
            wait_for(lambda:guard.snapshot()['waiting']==1)
            started=time.monotonic();guard.close()
            self.assertIsNone(waiting.result(timeout=1))
            self.assertLess(time.monotonic()-started,1)
        self.assertEqual(guard.snapshot()['inflight']['count'],1)
        guard.release(held)
        self.assertIsNone(guard.acquire())

    def test_disk_floor_accounts_for_reservations_on_least_free_path(self):
        free={'/cache':700,'/attempts':500}
        guard=PreparationResourceGuard(free,config={'diskMinFreeBytes':100,'diskPerPrepareBytes':250},
                                       disk_usage=lambda p:SimpleNamespace(free=free[p]),poll_seconds=.02)
        held=guard.acquire()
        state=guard.snapshot()
        self.assertEqual(state['disk']['minFreeBytes'],500)
        self.assertEqual(state['inflight']['diskReservedBytes'],250)
        self.assertEqual(state['blockedReasons'][0]['code'],'disk_free')
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting=pool.submit(guard.acquire)
            wait_for(lambda:guard.snapshot()['waiting']==1)
            free['/attempts']=900
            token=waiting.result(timeout=1)
            self.assertEqual(guard.snapshot()['inflight']['count'],2)
            guard.release(token)
        guard.release(held)

    def test_disk_probe_error_blocks_without_inventing_free_space(self):
        def unavailable(path):raise OSError('mount unavailable')
        guard=PreparationResourceGuard(['/cache'],config={'diskMinFreeBytes':1},disk_usage=unavailable)
        state=guard.snapshot()
        self.assertEqual(state['blockedReasons'][0]['code'],'disk_probe')
        self.assertIsNone(state['disk']['minFreeBytes'])
        self.assertEqual(state['disk']['paths'][0]['error'],'mount unavailable')


class PreparationModule:
    def __init__(self):
        self.allow_finish=threading.Event()
        self.lock=threading.Lock()
        self.active=0
        self.peak=0
        self.fail=False

    def staged_capabilities(self):return []

    def prepare_stage(self,job,work_dir,emit,cancelled):
        with self.lock:
            self.active+=1;self.peak=max(self.peak,self.active)
        try:
            while not self.allow_finish.wait(.01):
                if cancelled():return {'cacheHit':False}
            if self.fail:raise RuntimeError('controlled preparation error')
            return {'cacheHit':False}
        finally:
            with self.lock:self.active-=1

    def predict_stage(self,*args):raise AssertionError('CPU admission must not run prediction')


class SchedulerResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.environment=patch.dict(os.environ, {'PROTEIN_STUDIO_PREDICTION_MODE':'workers',
                                                'PROTEIN_STUDIO_PREPARATION_RESOURCES_JSON':''})
        self.environment.start()
        self.schedulers=[]

    def tearDown(self):
        for scheduler in self.schedulers:scheduler.close()
        self.environment.stop()
        self.temp.cleanup()

    def scheduler(self,config,workers=4):
        module=PreparationModule()
        root=self.root/str(len(self.schedulers));root.mkdir()
        scheduler=PipelineScheduler(root,staged_module=module,prep_workers=workers,ready_cap=20,
                                    preparation_resources=config,lease_seconds=3)
        scheduler.preparation_router=lambda conn,now:('1',[])
        self.schedulers.append(scheduler)
        campaign=scheduler.store.create({'name':'Resources','engine':'reference','status':'active',
                                         'settings':{},'databaseProfile':{},'executionProfile':{}})
        return scheduler,module,campaign['id']

    def append(self,scheduler,cid,count):
        return scheduler.store.append(cid,[{'targetId':str(i),'submission':{'name':str(i),'engine':'reference',
                    'chains':[{'id':'A','sequence':'AG'}],'seeds':[1],'numSamples':1}} for i in range(count)])

    def test_concurrent_preparation_never_exceeds_cpu_or_memory_admission(self):
        cases=[({'cpuBudget':64,'cpuPerPrepare':32,'memoryBudgetBytes':400,'memoryPerPrepareBytes':100},2),
               ({'cpuBudget':96,'cpuPerPrepare':32,'memoryBudgetBytes':300,'memoryPerPrepareBytes':200},1)]
        for config,expected in cases:
            with self.subTest(config=config):
                scheduler,module,cid=self.scheduler(config)
                self.append(scheduler,cid,6)
                scheduler.start()
                wait_for(lambda:module.active==expected)
                wait_for(lambda:scheduler.preparation_resources.snapshot()['waiting']==4-expected)
                state=scheduler.config()['preparationResources']
                self.assertLessEqual(state['inflight']['cpuReserved'],config['cpuBudget'])
                self.assertLessEqual(state['inflight']['memoryReservedBytes'],config['memoryBudgetBytes'])
                items=scheduler.store.items(cid)['items']
                self.assertEqual(sum(item['stage']=='preparing' for item in items),expected)
                self.assertTrue(all(item['attempt']==0 for item in items if item['stage']=='waiting_input'))
                self.assertEqual(module.peak,expected)
                module.allow_finish.set()
                wait_for(lambda:scheduler.store.get(cid)['counts']['ready']==6)
                self.assertEqual(module.peak,expected)
                scheduler.close()
                self.assertEqual(scheduler.preparation_resources.snapshot()['inflight']['count'],0)

    def test_disk_shortage_does_not_claim_or_fail_and_recovery_runs_waiting_target(self):
        scheduler,module,cid=self.scheduler({'diskMinFreeBytes':100,'diskPerPrepareBytes':100},workers=2)
        self.append(scheduler,cid,1)
        space={'free':150}
        guard=scheduler.preparation_resources
        guard._disk_usage=lambda path:SimpleNamespace(free=space['free'])
        guard._poll_seconds=.05
        scheduler.start()
        wait_for(lambda:guard.snapshot()['waiting']==2)
        item=scheduler.store.items(cid)['items'][0]
        self.assertEqual((item['stage'],item['attempt']),('waiting_input',0))
        self.assertEqual(guard.snapshot()['blockedReasons'][0]['code'],'disk_free')
        space['free']=500
        wait_for(lambda:module.active==1)
        module.allow_finish.set()
        wait_for(lambda:scheduler.store.get(cid)['counts']['ready']==1)
        scheduler.close()
        self.assertEqual(guard.snapshot()['inflight']['count'],0)

    def test_stage_failure_releases_reservation_for_next_target(self):
        scheduler,module,cid=self.scheduler({'cpuBudget':32,'cpuPerPrepare':32},workers=2)
        self.append(scheduler,cid,2);module.fail=True;module.allow_finish.set()
        with self.assertLogs('protein-studio.pipeline',level='ERROR'):
            scheduler.start()
            wait_for(lambda:scheduler.store.get(cid)['counts']['failed']==2)
        scheduler.close()
        self.assertEqual(scheduler.preparation_resources.snapshot()['inflight']['count'],0)
        self.assertEqual(module.peak,1)

    def test_empty_queue_and_claim_exception_release_before_wait(self):
        for fail in (False,True):
            with self.subTest(fail=fail):
                scheduler,module,cid=self.scheduler({'cpuBudget':1,'cpuPerPrepare':1},workers=1)
                calls=[]
                def claim(*args,**kwargs):
                    calls.append(scheduler.preparation_resources.snapshot()['inflight']['count'])
                    if fail:raise RuntimeError('controlled SQLite failure')
                    return None
                with patch.object(scheduler.store,'claim',side_effect=claim):
                    thread=threading.Thread(target=scheduler._worker,args=('prepare',),daemon=True)
                    scheduler.threads=[thread]
                    if fail:
                        with self.assertLogs('protein-studio.pipeline',level='ERROR'):
                            thread.start();wait_for(lambda:len(calls)>0)
                            wait_for(lambda:scheduler.preparation_resources.snapshot()['inflight']['count']==0)
                            scheduler.close()
                    else:
                        thread.start();wait_for(lambda:len(calls)>0)
                        wait_for(lambda:scheduler.preparation_resources.snapshot()['inflight']['count']==0)
                        scheduler.close()
                self.assertTrue(all(value==1 for value in calls))
                self.assertEqual(scheduler.preparation_resources.snapshot()['inflight']['count'],0)

    def test_campaign_cancel_and_scheduler_stop_release_active_and_waiting_slots(self):
        scheduler,module,cid=self.scheduler({'cpuBudget':32,'cpuPerPrepare':32},workers=3)
        self.append(scheduler,cid,3);scheduler.start()
        wait_for(lambda:module.active==1)
        wait_for(lambda:scheduler.preparation_resources.snapshot()['waiting']==2)
        scheduler.store.control(cid,'cancel')
        wait_for(lambda:module.active==0)
        self.assertTrue(scheduler.close())
        self.assertEqual(scheduler.preparation_resources.snapshot()['inflight']['count'],0)
        self.assertEqual(scheduler.preparation_resources.snapshot()['waiting'],0)
        self.assertEqual(scheduler.store.get(cid)['counts']['cancelled'],3)

    def test_disabled_guard_preserves_existing_worker_concurrency(self):
        scheduler,module,cid=self.scheduler({},workers=3)
        self.append(scheduler,cid,3);scheduler.start()
        wait_for(lambda:module.active==3)
        self.assertFalse(scheduler.config()['preparationResources']['configured'])
        module.allow_finish.set()
        wait_for(lambda:scheduler.store.get(cid)['counts']['ready']==3)
        scheduler.close()
        self.assertEqual(module.peak,3)


if __name__=='__main__':unittest.main()
