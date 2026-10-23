"""Real thread/process maintenance-lock contracts, only in temporary directories."""
import fcntl
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from backend import cache_access as access
from backend.engine import PredictionCancelled


CHILD = '''
import sys
from pathlib import Path
from backend.cache_access import shared_cache, exclusive_cache
root, marker, mode = sys.argv[1:]
guard = shared_cache if mode == 'shared' else exclusive_cache
Path(marker + '.attempting').write_text('attempting')
with guard(root, timeout=8):
    Path(marker).write_text('entered')
    sys.stdin.readline()
'''


class CacheAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'cache'
        self.root.mkdir()
        self.threads, self.errors, self.children = [], [], []
        self.addCleanup(self._cleanup_workers)

    def _cleanup_workers(self):
        for process in self.children:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)
        for thread in self.threads:
            thread.join(3)

    def launch(self, fn):
        def wrapped():
            try:
                fn()
            except BaseException as exc:
                self.errors.append(exc)
        thread = threading.Thread(target=wrapped, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def join_ok(self, thread):
        thread.join(3)
        self.assertFalse(thread.is_alive(), 'Thread did not release/wake from maintenance access.')
        self.assertEqual(self.errors, [])

    def wait_until(self, condition, message='Condition did not become true'):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.005)
        self.fail(message)

    def waiting_writer(self):
        state = access._registry.get(self.root.resolve())
        if state is None:
            return False
        with state.condition:
            return state.waiting_writers > 0

    def child(self, mode='shared', name='child'):
        marker = Path(self.temporary.name) / (name + '.entered')
        process = subprocess.Popen([sys.executable, '-c', CHILD, str(self.root), str(marker), mode],
                                   cwd=Path(__file__).resolve().parents[2], stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.children.append(process)
        return process, marker

    def release_child(self, process):
        if process.poll() is None:
            process.stdin.write('\n')
            process.stdin.flush()
        _, error = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 0, error)

    def test_two_shared_threads_enter_together(self):
        entered, leave = threading.Event(), threading.Event()
        def reader():
            with access.shared_cache(self.root, timeout=2):
                entered.set()
                self.assertTrue(leave.wait(3))
        with access.shared_cache(self.root):
            thread = self.launch(reader)
            try:
                self.assertTrue(entered.wait(2), 'Shared access became exclusive.')
            finally:
                leave.set()
        self.join_ok(thread)

    def test_queued_writer_precedes_new_reader_and_nested_shared_does_not_deadlock(self):
        writer_entered, release_writer, reader_entered = threading.Event(), threading.Event(), threading.Event()
        order = []
        def writer():
            with access.exclusive_cache(self.root, timeout=3):
                order.append('writer')
                writer_entered.set()
                self.assertTrue(release_writer.wait(3))
        def reader():
            with access.shared_cache(self.root, timeout=3):
                order.append('reader')
                reader_entered.set()
        try:
            with access.shared_cache(self.root):
                write_thread = self.launch(writer)
                self.wait_until(self.waiting_writer)
                read_thread = self.launch(reader)
                # A waiting writer must not block this thread's inner acquisition.
                with access.shared_cache(self.root / '.', timeout=.2):
                    self.assertFalse(writer_entered.is_set())
                    self.assertFalse(reader_entered.is_set())
            self.assertTrue(writer_entered.wait(2))
            self.assertFalse(reader_entered.is_set())
        finally:
            release_writer.set()
        self.join_ok(write_thread)
        self.join_ok(read_thread)
        self.assertEqual(order, ['writer', 'reader'])

    def test_exclusive_and_shared_under_exclusive_are_reentrant(self):
        with access.exclusive_cache(self.root):
            with access.exclusive_cache(str(self.root), timeout=.1):
                with access.shared_cache(self.root, timeout=.1):
                    self.assertTrue(access._local.held[self.root.resolve()]['exclusive'])
        with access.exclusive_cache(self.root, timeout=.2):
            pass

    def test_shared_to_exclusive_upgrade_is_rejected_without_leaking(self):
        with access.shared_cache(self.root):
            with self.assertRaisesRegex(ValueError, 'upgrade'):
                with access.exclusive_cache(self.root):
                    self.fail('Upgrade unexpectedly entered.')
            with access.shared_cache(self.root):
                pass
        with access.exclusive_cache(self.root, timeout=.2):
            pass

    def test_waiting_writer_timeout_removes_local_writer_priority(self):
        timed_out = threading.Event()
        def writer():
            with self.assertRaises(TimeoutError):
                with access.exclusive_cache(self.root, timeout=.08):
                    self.fail('Writer entered despite another shared reader.')
            timed_out.set()
        with access.shared_cache(self.root):
            thread = self.launch(writer)
            self.assertTrue(timed_out.wait(2))
            self.join_ok(thread)
            self.assertFalse(self.waiting_writer())
            second_entered = threading.Event()
            def reader():
                with access.shared_cache(self.root, timeout=.3):
                    second_entered.set()
            second = self.launch(reader)
            self.assertTrue(second_entered.wait(1))
            self.join_ok(second)
        with access.exclusive_cache(self.root, timeout=.3):
            pass

    def test_waiting_writer_cancellation_cleans_queue(self):
        cancelled, acknowledged = threading.Event(), threading.Event()
        def writer():
            with self.assertRaises(PredictionCancelled):
                with access.exclusive_cache(self.root, cancelled=cancelled.is_set, timeout=2):
                    self.fail('Cancelled writer entered.')
            acknowledged.set()
        with access.shared_cache(self.root):
            thread = self.launch(writer)
            self.wait_until(self.waiting_writer)
            cancelled.set()
            self.assertTrue(acknowledged.wait(2))
        self.join_ok(thread)
        with access.exclusive_cache(self.root, timeout=.3):
            pass

    def test_real_process_reader_blocks_writer_and_releases_on_normal_exit(self):
        process, marker = self.child()
        self.wait_until(marker.exists, 'Child could not acquire shared flock.')
        with self.assertRaises(TimeoutError):
            with access.exclusive_cache(self.root, timeout=.1):
                self.fail('Separate process shared lock was ignored.')
        self.release_child(process)
        with access.exclusive_cache(self.root, timeout=.3):
            pass

    def test_cross_process_intent_blocks_late_reader_while_writer_drains(self):
        first, first_marker = self.child(name='first')
        self.wait_until(first_marker.exists)
        intent_held, writer_entered, leave = threading.Event(), threading.Event(), threading.Event()
        real_flock = access._flock
        def traced_flock(stream, mode, cancelled, deadline):
            real_flock(stream, mode, cancelled, deadline)
            if mode == fcntl.LOCK_EX and str(stream.name).endswith('.maintenance.intent.lock'):
                intent_held.set()
        def writer():
            with access.exclusive_cache(self.root, timeout=4):
                writer_entered.set()
                self.assertTrue(leave.wait(3))
        try:
            with patch.object(access, '_flock', traced_flock):
                thread = self.launch(writer)
                self.assertTrue(intent_held.wait(2))
                second, second_marker = self.child(name='second')
                self.wait_until(Path(str(second_marker) + '.attempting').exists)
                self.assertFalse(writer_entered.is_set())
                self.release_child(first)
                self.assertTrue(writer_entered.wait(2))
                self.assertFalse(second_marker.exists(), 'Late process bypassed exclusive intent gate.')
                leave.set()
                self.join_ok(thread)
                self.wait_until(second_marker.exists)
                self.release_child(second)
        finally:
            leave.set()

    def test_cancel_after_os_intent_acquired_releases_gate_and_local_state(self):
        first, first_marker = self.child(name='held')
        self.wait_until(first_marker.exists)
        intent_held, cancelled, acknowledged = threading.Event(), threading.Event(), threading.Event()
        real_flock = access._flock
        def traced_flock(stream, mode, callback, deadline):
            real_flock(stream, mode, callback, deadline)
            if mode == fcntl.LOCK_EX and str(stream.name).endswith('.maintenance.intent.lock'):
                intent_held.set()
        def writer():
            with self.assertRaises(PredictionCancelled):
                with access.exclusive_cache(self.root, cancelled=cancelled.is_set, timeout=3):
                    self.fail('Writer entered while child held main SH.')
            acknowledged.set()
        with patch.object(access, '_flock', traced_flock):
            thread = self.launch(writer)
            self.assertTrue(intent_held.wait(2))
            cancelled.set()
            self.assertTrue(acknowledged.wait(2))
            self.join_ok(thread)
        # Original child still holds SH: another process can enter only if intent
        # was released by the aborted writer, without waiting for that child.
        second, second_marker = self.child(name='after-cancel')
        self.wait_until(second_marker.exists)
        self.release_child(second)
        self.release_child(first)
        with access.exclusive_cache(self.root, timeout=.3):
            pass

    def test_process_kill_releases_actual_os_lock(self):
        process, marker = self.child(mode='exclusive', name='crash')
        self.wait_until(marker.exists)
        with self.assertRaises(TimeoutError):
            with access.shared_cache(self.root, timeout=.1):
                self.fail('Child exclusive lock was ignored.')
        process.kill()
        process.communicate(timeout=3)
        with access.exclusive_cache(self.root, timeout=.3):
            pass

    def test_exceptions_invalid_timeout_and_pre_cancel_do_not_leave_locks(self):
        for guard in (access.shared_cache, access.exclusive_cache):
            with self.subTest(guard=guard.__name__):
                with self.assertRaisesRegex(RuntimeError, 'body failed'):
                    with guard(self.root):
                        raise RuntimeError('body failed')
                with self.assertRaises(PredictionCancelled):
                    with guard(self.root, cancelled=lambda: True):
                        self.fail('Pre-cancelled access entered.')
                for value in (0, -1, True, math.inf, math.nan, '1'):
                    with self.assertRaises(ValueError):
                        with guard(self.root, timeout=value):
                            pass
                with access.exclusive_cache(self.root, timeout=.3):
                    pass

    def test_different_cache_roots_do_not_share_maintenance_gate(self):
        with access.shared_cache(self.root):
            with access.exclusive_cache(self.root.parent / 'other', timeout=.2):
                pass


if __name__ == '__main__':
    unittest.main()
