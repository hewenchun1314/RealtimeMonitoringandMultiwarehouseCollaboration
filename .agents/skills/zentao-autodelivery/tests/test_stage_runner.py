import ctypes
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1] / 'scripts' / 'stage_runner.py'


@unittest.skipUnless(os.name == 'nt', 'Windows stage workers require Windows Job Objects')
class StageRunnerTests(unittest.TestCase):
    def invoke(self, folder, attempt, seconds, command, phase='verify'):
        deadline = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        worker = subprocess.run(
            [sys.executable, '-B', str(RUNNER), '--attempt-id', attempt, '--phase', phase,
             '--deadline-at', deadline.isoformat(), '--cwd', str(folder), '--evidence-dir', str(folder),
             '--', *command], capture_output=True, encoding='utf-8', timeout=15, check=True,
        )
        return json.loads(worker.stdout)

    def test_routed_repair_phase_has_its_own_worker_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            receipt = self.invoke(folder, 'joint', 10, [sys.executable, '-c', 'print("repaired")'],
                                  phase='backend.coding')
            self.assertEqual(receipt['outcome'], 'success')
            self.assertEqual(Path(receipt['receipt_path']), folder / 'joint' / 'backend.coding' / 'result.json')

    def test_deadline_stops_worker_and_grandchild_without_touching_external_process(self):
        payload = (
            'import json, os, subprocess, sys, time\n'
            'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
            'print(json.dumps({"worker": os.getpid(), "child": child.pid}), flush=True)\n'
            'time.sleep(30)\n'
        )
        external = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(external.wait, 10)
        self.addCleanup(external.kill)
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(Path(directory), 'deadline-probe', 3, [sys.executable, '-c', payload])
            self.assertEqual(result['outcome'], 'timed_out')
            self.assertEqual(result['stop_confirmed'], True)
            evidence = result['stop_evidence']
            self.assertEqual(evidence['owned_processes_active_after_stop'], 0)
            self.assertGreaterEqual(evidence['owned_processes_at_stop'], 3)
            self.assertEqual(result, json.loads(Path(result['receipt_path']).read_text(encoding='utf-8')))
            pids = json.loads(Path(result['stdout_path']).read_text(encoding='utf-8'))
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.WaitForSingleObject.restype = ctypes.c_uint32
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            for pid in pids.values():
                handle = kernel.OpenProcess(0x00100000, False, pid)
                if handle:
                    try:
                        self.assertEqual(kernel.WaitForSingleObject(handle, 0), 0)
                    finally:
                        kernel.CloseHandle(handle)
                else:
                    self.assertEqual(ctypes.get_last_error(), 87)
            self.assertIsNone(external.poll())

    def test_expired_attempt_never_launches_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            command = [sys.executable, '-c', 'from pathlib import Path; Path("marker").touch()']
            result = self.invoke(folder, 'expired', -1, command)
            self.assertEqual(result['outcome'], 'timed_out')
            self.assertTrue(result['stop_confirmed'])
            self.assertEqual(result['stop_evidence']['owned_processes_at_stop'], 0)
            self.assertFalse((folder / 'marker').exists())

    def test_successful_worker_cleans_up_its_remaining_background_process(self):
        payload = (
            'import subprocess, sys\n'
            'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
            'print("completed")\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(Path(directory), 'early-completion', 15, [sys.executable, '-c', payload])
            self.assertEqual(result['outcome'], 'success')
            evidence = result['stop_evidence']
            self.assertGreaterEqual(evidence['owned_processes_at_stop'], 1)
            self.assertEqual(evidence['owned_processes_active_after_stop'], 0)
            self.assertTrue(evidence['worker_started'])
            self.assertEqual(Path(result['stdout_path']).read_text(encoding='utf-8').strip(), 'completed')


if __name__ == '__main__':
    unittest.main()
