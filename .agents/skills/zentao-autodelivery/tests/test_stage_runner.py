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
            worker_argv = spawn.call_args.args[0]
            self.assertEqual(worker_argv[worker_argv.index('codex.exe'):], [
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
            if os.name != 'nt':
                table = subprocess.check_output(['ps', '-axo', 'pid=,stat='], text=True)
                live = {int(pid) for pid, state in (r.split() for r in table.splitlines()) if not state.startswith('Z')}
                self.assertFalse(set(pids.values()) & live)
                self.assertIsNone(external.poll())
                return
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

    @unittest.skipUnless(os.name == 'posix', 'POSIX service may start a new session')
    def test_timeout_cleans_service_child_that_changed_its_session(self):
        payload = ('import subprocess, sys, time; '
                   'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True); '
                   'print(child.pid, flush=True); time.sleep(30)')
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(Path(directory), 'detached-child', 2, [sys.executable, '-c', payload])
            self.assertTrue(result['stop_confirmed'])
            pid = Path(result['stdout_path']).read_text().strip()
            status = subprocess.run(['ps', '-p', pid, '-o', 'stat='], capture_output=True, text=True)
            try:
                self.assertTrue(status.returncode or status.stdout.strip().startswith('Z'))
            finally:
                if status.returncode == 0 and not status.stdout.strip().startswith('Z'):
                    import signal
                    os.kill(int(pid), signal.SIGKILL)

    @unittest.skipUnless(os.name == 'posix', 'nested POSIX stage scope')
    def test_nested_stage_with_its_own_subprocess_reports_complete_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            nested = [sys.executable, '-B', str(RUNNER), '--attempt-id', 'inner', '--phase', 'verify',
                      '--deadline-at', (datetime.now(timezone.utc) + timedelta(seconds=8)).isoformat(),
                      '--cwd', directory, '--evidence-dir', directory, '--', sys.executable, '-c', 'print("ok")']
            result = self.invoke(folder, 'outer', 12, nested)
            self.assertTrue(result['stop_confirmed'])
            self.assertEqual(json.loads((folder / 'inner/verify/result.json').read_text())['stop_confirmed'], True)

    def test_expired_attempt_never_launches_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            command = [sys.executable, '-c', 'from pathlib import Path; Path("marker").touch()']
            result = self.invoke(folder, 'expired', -1, command)
            self.assertEqual(result['outcome'], 'timed_out')
            self.assertTrue(result['stop_confirmed'])
            self.assertEqual(result['stop_evidence']['owned_processes_at_stop'], 0)
            self.assertFalse((folder / 'marker').exists())

    def test_missing_executable_fails_with_verified_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            result = self.invoke(folder, 'missing-cli', 8, [str(folder / 'uninstalled-binary')])
            self.assertEqual(result['outcome'], 'failed')
            self.assertTrue(result['stop_confirmed'])
            self.assertFalse(result['stop_evidence']['worker_started'])

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
