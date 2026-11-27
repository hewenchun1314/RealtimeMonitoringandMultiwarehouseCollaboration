import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


RUNNER = Path(__file__).resolve().parents[1] / 'scripts' / 'stage_runner.py'
sys.path.insert(0, str(RUNNER.parent))


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

    def test_codex_exec_receives_project_model_settings_in_the_worktree(self):
        from stage_runner import run_stage

        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            worktree = folder / 'worktree'
            worktree.mkdir()
            config_path = folder / 'zentao-project.json'
            config_path.write_text(json.dumps({'runtime': {
                'codex_model': 'gpt-6.1-sol', 'codex_reasoning_effort': 'xhigh',
            }}), encoding='utf-8')
            deadline = datetime.now(timezone.utc) + timedelta(seconds=10)
            with patch('workflow.CONFIG_PATH', config_path), patch(
                'stage_runner.subprocess.Popen', side_effect=OSError('stop before launching Codex')
            ) as spawn:
                with self.assertRaisesRegex(OSError, 'stop before launching Codex'):
                    run_stage('model-probe', 'backend.design', deadline, worktree, folder, None,
                              ['codex.exe', 'exec', '--ephemeral', '-'])

            self.assertEqual(spawn.call_args.kwargs['cwd'], worktree)
            self.assertEqual(spawn.call_args.args[0][5:], [
                'codex.exe', '-c', 'model="gpt-6.1-sol"', '-c', 'model_reasoning_effort="xhigh"',
                'exec', '--ephemeral', '-',
            ])

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

    def test_failed_preflight_stops_before_agent_start(self):
        from stage_runner import run_stage, utc_now

        for check in ('sandbox', 'frontend_dependencies'):
            with self.subTest(check=check), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                command = [sys.executable, '-c', 'from pathlib import Path; Path("agent-started").touch()']
                preflight = [{'check': check, 'cwd': str(folder), 'command': [
                    sys.executable, '-c', 'import sys; print("blocked"); sys.exit(3)',
                ]}]
                receipt = run_stage('blocked', 'verify', utc_now() + timedelta(seconds=15),
                                    folder, folder, None, command, preflight=preflight)
                self.assertEqual(receipt['outcome'], 'failed')
                self.assertEqual(receipt['preflight_failure']['check'], check)
                self.assertTrue(receipt['stop_confirmed'])
                self.assertFalse(receipt['stop_evidence']['worker_started'])
                self.assertIn('blocked', Path(receipt['preflight_failure']['log']).read_text())
                self.assertFalse((folder / 'agent-started').exists())

    def test_successful_preflight_runs_in_order_before_agent(self):
        from stage_runner import run_stage, utc_now

        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            preflight = [{'check': 'sandbox', 'cwd': str(folder), 'command': [
                sys.executable, '-c', 'from pathlib import Path; Path("probe-ok").touch()',
            ]}]
            command = [sys.executable, '-c', 'from pathlib import Path; assert Path("probe-ok").is_file()']
            receipt = run_stage('ready', 'verify', utc_now() + timedelta(seconds=15),
                                folder, folder, None, command, preflight=preflight)
            self.assertEqual(receipt['outcome'], 'success')
            self.assertTrue(receipt['stop_evidence']['worker_started'])

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

    def test_recoverable_job_proves_stop_after_supervisor_termination(self):
        from stage_runner import recover_job_stop

        script = (
            'import sys\n'
            'from pathlib import Path\n'
            'from datetime import timedelta\n'
            'sys.path.insert(0, sys.argv[1])\n'
            'from stage_runner import run_stage, utc_now\n'
            'cwd = Path(sys.argv[2])\n'
            'run_stage("owned", "publish", utc_now() + timedelta(seconds=30), cwd, cwd, None,\n'
            '          [sys.executable, "-c", "import time; time.sleep(30)"], recoverable=True)\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            worker = subprocess.Popen([sys.executable, '-B', '-c', script, str(RUNNER.parent), directory],
                                      stderr=subprocess.PIPE)
            launch_path = Path(directory) / 'owned' / 'publish' / 'launch.json'
            try:
                until = time.monotonic() + 8
                while not launch_path.exists() and worker.poll() is None and time.monotonic() < until:
                    time.sleep(0.025)
                self.assertTrue(launch_path.exists())
                launch = json.loads(launch_path.read_text(encoding='utf-8'))
                self.assertIsNone(recover_job_stop(launch))
                worker.kill()
                worker.wait(5)
                until = time.monotonic() + 5
                stopped = None
                while stopped is None and time.monotonic() < until:
                    stopped = recover_job_stop(launch)
                    if stopped is None:
                        time.sleep(0.025)
                self.assertIsNotNone(stopped)
                self.assertEqual(stopped['owned_processes_active_after_stop'], 0)
                self.assertIsNone(recover_job_stop({**launch, 'job_host': 'other-host'}))
                self.assertFalse(launch_path.with_name('result.json').exists())
            finally:
                if worker.poll() is None:
                    worker.kill()
                worker.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
