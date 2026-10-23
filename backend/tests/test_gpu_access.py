"""Device exclusion with real processes and fake device IDs; no GPU runtime."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from backend import engine
from backend.gpu_access import gpu_group_lease, try_gpu_group, GPUDevicesBusy
from backend.worker_client import WorkerClient


class GpuAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {
            'PROTEIN_STUDIO_GPU_LOCK_DIR': str(self.root / 'locks'),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_overlap_is_busy_disjoint_group_works_and_partial_acquisition_releases(self):
        with gpu_group_lease(['z']) as devices:
            self.assertEqual(devices, ['z'])
            self.assertEqual(len(devices.lock_fds), 1)
            with self.assertRaises(GPUDevicesBusy):
                with gpu_group_lease(['a', 'z'], blocking=False):
                    self.fail('Overlapping device groups cannot coexist.')
            with try_gpu_group([['z'], ['a']]) as available:
                self.assertEqual(available, ['a'])
            with self.assertRaises(GPUDevicesBusy):
                with try_gpu_group([['z']]):
                    self.fail('Busy search must yield to its pipeline.')
        with gpu_group_lease(['z'], blocking=False):
            pass

    def test_invalid_groups_and_cancelled_wait_never_hold_devices(self):
        for group in [[], ['a', 'a'], ['../invalid'], [True]]:
            with self.subTest(group=group), self.assertRaises(ValueError):
                with gpu_group_lease(group):
                    pass
        with self.assertRaises(ValueError):
            with try_gpu_group([['a'], ['a']]):
                pass
        with gpu_group_lease(['a']):
            start = time.monotonic()
            with self.assertRaises(engine.PredictionCancelled):
                with gpu_group_lease(['a'], lambda: time.monotonic() - start > .12):
                    pass
        with gpu_group_lease(['a'], blocking=False):
            pass

    def test_engine_gpu_execution_inherits_locks_and_cpu_execution_hides_devices(self):
        with patch.dict(os.environ, {'PROTEIN_STUDIO_GPU_GROUPS_JSON': '[["GPU-test"]]'}):
            with engine._gpu_lease(lambda *a: None, lambda: False) as devices:
                code = 'import os,sys;assert os.environ["CUDA_VISIBLE_DEVICES"]=="GPU-test";[os.fstat(int(n)) for n in sys.argv[1:]]'
                engine._execute([sys.executable, '-c', code, *map(str, devices.lock_fds)],
                                self.root, self.root / 'gpu.log', lambda: False, 5, devices)
                with self.assertRaises(GPUDevicesBusy):
                    with try_gpu_group([['GPU-test']]):
                        pass
        engine._execute([sys.executable, '-c', 'import os;assert os.environ["CUDA_VISIBLE_DEVICES"]==""'],
                        self.root, self.root / 'cpu.log', lambda: False, 5, [])

    def test_inherited_resident_lock_survives_abrupt_client_process_exit(self):
        release, pid_path = self.root / 'release', self.root / 'resident.pid'
        child = ('import time;from pathlib import Path;'
                 f'p=Path({str(release)!r});'
                 '\nwhile not p.exists():time.sleep(.02)')
        parent = '''
import os,subprocess,sys
from pathlib import Path
from backend.gpu_access import gpu_group_lease
with gpu_group_lease(['GPU-orphan']) as group:
    child=subprocess.Popen([sys.executable,'-c',sys.argv[1]],pass_fds=group.lock_fds,
                           stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                           start_new_session=True)
    Path(sys.argv[2]).write_text(str(child.pid))
    os._exit(17)
'''
        process = subprocess.run([sys.executable, '-c', parent, child, str(pid_path)],
                                 capture_output=True, text=True, timeout=5)
        self.assertEqual(process.returncode, 17, process.stderr)
        pid = int(pid_path.read_text())
        try:
            with self.assertRaises(GPUDevicesBusy):
                with try_gpu_group([['GPU-orphan']]):
                    pass
            release.touch()
            deadline = time.monotonic() + 5
            while True:
                try:
                    with try_gpu_group([['GPU-orphan']]):
                        break
                except GPUDevicesBusy:
                    if time.monotonic() > deadline:
                        self.fail('Resident did not release its inherited descriptor on exit.')
                    time.sleep(.02)
        finally:
            release.touch()
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def test_native_worker_holds_devices_across_restart_until_resident_is_closed(self):
        ready = dict(event='ready', protocolVersion=1, engine='alphafold3', isDemo=False,
                     modelLoads=0, initializationCount=1, signature={})
        code = ('import json,sys;print(' + repr(json.dumps(ready)) + ',flush=True);sys.stdin.read()')
        worker = WorkerClient('http://127.0.0.1:1', 'isolated', 'x' * 32, 'alphafold3',
                              'synthetic-runtime', self.root / 'worker', [sys.executable, '-c', code],
                              gpu_devices=['GPU-resident'])
        self.addCleanup(worker.close)
        worker._ready()
        first_pid = worker.resident.pid
        self.assertTrue(worker.resident.inherited_fds)
        with self.assertRaises(GPUDevicesBusy):
            with try_gpu_group([['GPU-resident']]):
                pass
        worker._ready(restart=True)
        self.assertNotEqual(worker.resident.pid, first_pid)
        with self.assertRaises(GPUDevicesBusy):
            with try_gpu_group([['GPU-resident']]):
                pass
        worker.close()
        with try_gpu_group([['GPU-resident']]):
            pass

    def test_abrupt_search_wrapper_exit_stops_managed_descendants_before_unlock(self):
        heartbeat = self.root / 'descendant-heartbeat'
        child = ('import signal,time;from pathlib import Path;'
                 'signal.signal(signal.SIGTERM,signal.SIG_IGN);'
                 f'p=Path({str(heartbeat)!r});'
                 '\nwhile True:\n with p.open("a") as stream:stream.write("x")\n time.sleep(.01)')
        wrapper = '''
import json,os,subprocess,sys,time
assert os.environ['PROTEIN_STUDIO_MANAGED_PROCESS_GROUP']=='1'
fds=json.loads(os.environ['PROTEIN_STUDIO_GPU_LOCK_FDS_JSON'])
subprocess.Popen([sys.executable,'-c',sys.argv[1]],pass_fds=fds)
time.sleep(.08)
os._exit(17)
'''
        with gpu_group_lease(['GPU-child']) as devices:
            with self.assertRaises(RuntimeError):
                engine._execute([sys.executable, '-c', wrapper, child], self.root,
                                self.root / 'orphan.log', lambda: False, 5, devices)
            self.assertTrue(heartbeat.exists())
            time.sleep(.05)
            final_bytes = heartbeat.stat().st_size
            time.sleep(.08)
            self.assertEqual(heartbeat.stat().st_size, final_bytes)
        with try_gpu_group([['GPU-child']]):
            pass

    def test_native_worker_startup_failure_releases_devices(self):
        worker = WorkerClient('http://127.0.0.1:1', 'isolated', 'x' * 32, 'alphafold3',
                              'synthetic-runtime', self.root / 'worker', [sys.executable, '-c', 'raise SystemExit(3)'],
                              gpu_devices=['GPU-failed'])
        self.addCleanup(worker.close)
        with self.assertRaises(Exception):
            worker._ready()
        with try_gpu_group([['GPU-failed']]):
            pass


if __name__ == '__main__':
    unittest.main()
