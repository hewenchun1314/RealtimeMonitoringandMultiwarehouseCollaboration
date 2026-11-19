"""Supervise one local stage worker and its process tree on Windows."""

import argparse
from contextlib import nullcontext
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


class BasicLimit(ctypes.Structure):
    _fields_ = [
        ('per_process_user_time', ctypes.c_longlong), ('per_job_user_time', ctypes.c_longlong),
        ('flags', wintypes.DWORD), ('min_working_set', ctypes.c_size_t), ('max_working_set', ctypes.c_size_t),
        ('active_process_limit', wintypes.DWORD), ('affinity', ctypes.c_size_t),
        ('priority_class', wintypes.DWORD), ('scheduling_class', wintypes.DWORD),
    ]


class ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ('basic', BasicLimit), ('io_counters', ctypes.c_ulonglong * 6),
        ('process_memory_limit', ctypes.c_size_t), ('job_memory_limit', ctypes.c_size_t),
        ('peak_process_memory_used', ctypes.c_size_t), ('peak_job_memory_used', ctypes.c_size_t),
    ]


class Accounting(ctypes.Structure):
    _fields_ = [
        ('times', ctypes.c_longlong * 4), ('page_faults', wintypes.DWORD),
        ('total', wintypes.DWORD), ('active', wintypes.DWORD), ('terminated', wintypes.DWORD),
    ]


class Job:
    """Keep descendants in one non-breakaway job that closes with the supervisor."""

    def __init__(self):
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        self.api.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
        ]
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.handle = self.api.CreateJobObjectW(None, None)
        self.check(self.handle)
        limits = ExtendedLimit()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            self.check(self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except OSError:
            self.close()
            raise

    @staticmethod
    def check(result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        self.api.CloseHandle(self.handle)

    def assign(self, pid):
        process = self.api.OpenProcess(0x0101, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        self.check(process)
        try:
            self.check(self.api.AssignProcessToJobObject(self.handle, process))
        finally:
            self.api.CloseHandle(process)

    def accounting(self):
        info = Accounting()
        self.check(self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None))
        return info

    def stop(self):
        self.check(self.api.TerminateJobObject(self.handle, 124))
        until = time.monotonic() + 5
        info = self.accounting()
        while info.active and time.monotonic() < until:
            time.sleep(0.025)
            info = self.accounting()
        return info


def utc_now():
    return datetime.now(timezone.utc)


def write_json(path, value):
    pending = path.with_suffix('.pending')
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    pending.replace(path)


def parse_session(path):
    if not path.is_file():
        return {}
    session = {}
    with path.open(encoding='utf-8', errors='replace') as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') == 'thread.started' and isinstance(event.get('thread_id'), str):
                session['thread_id'] = event['thread_id']
            if event.get('type') in ('turn.started', 'turn.completed', 'turn.failed'):
                session['last_turn_event'] = event['type']
    return session


def bootstrap(prompt, command):
    # A closed pipe means the supervisor failed before the process was contained.
    if sys.stdin.buffer.read(1) != b'G':
        return 125
    with open(prompt, 'rb') if prompt else nullcontext(subprocess.DEVNULL) as input_stream:
        return subprocess.call(command, stdin=input_stream)


def run_stage(attempt_id, phase, deadline, cwd, evidence_dir, prompt, command):
    directory = evidence_dir / attempt_id / phase
    directory.mkdir(parents=True, exist_ok=False)
    receipt_path = directory / 'result.json'
    stdout_path = directory / 'stdout.log'
    stderr_path = directory / 'stderr.log'
    hosted_agent = Path(command[0]).name.lower() in ('codex', 'codex.exe') and 'exec' in command[1:]
    result = {
        'attempt_id': attempt_id, 'phase': phase, 'deadline_at': deadline.isoformat(),
        'receipt_path': str(receipt_path), 'stdout_path': str(stdout_path), 'stderr_path': str(stderr_path),
    }
    evidence = {'mechanism': 'windows_job_object', 'working_directory': str(cwd)}
    if utc_now() >= deadline:
        result.update(outcome='timed_out', stop_confirmed=True,
                      stop_evidence={**evidence, 'worker_started': False, 'owned_processes_at_stop': 0,
                                     'owned_processes_active_after_stop': 0})
        write_json(receipt_path, result)
        return result

    job = Job()
    process = None
    try:
        with stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
            argv = [sys.executable, '-B', __file__, '--bootstrap', str(prompt) if prompt else '', *command]
            process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            job.assign(process.pid)
            evidence.update(supervisor_pid=os.getpid(), launcher_pid=process.pid,
                            launch_timestamp=utc_now().isoformat(), worker_started=False)
            write_json(directory / 'launch.json', {**result, **evidence})
            remaining = (deadline - utc_now()).total_seconds()
            if remaining > 0:
                process.stdin.write(b'G')
                process.stdin.flush()
            process.stdin.close()
            timed_out = False
            try:
                process.wait(timeout=max(0, (deadline - utc_now()).total_seconds()))
                timed_out = remaining <= 0 or utc_now() >= deadline
            except subprocess.TimeoutExpired:
                timed_out = True

            before = job.accounting()
            after = job.stop() if before.active else before
            process.wait(timeout=5)
            evidence.update(
                owned_processes_at_stop=before.active,
                owned_processes_active_after_stop=after.active,
                owned_processes_total=after.total,
                worker_started=after.total > 1,
                verified_at=utc_now().isoformat(),
                launcher_exit_code=process.returncode,
            )
            if hosted_agent:
                evidence['codex_session'] = parse_session(stdout_path)
            outcome = 'timed_out' if timed_out else ('success' if process.returncode == 0 else 'failed')
            result.update(outcome=outcome, stop_confirmed=after.active == 0, stop_evidence=evidence)
    finally:
        job.close()
        if process is not None:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    write_json(receipt_path, result)
    return result


def main(argv):
    if argv and argv[0] == '--bootstrap':
        return bootstrap(argv[1], argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--attempt-id', required=True)
    parser.add_argument('--phase', required=True)
    parser.add_argument('--deadline-at', required=True)
    parser.add_argument('--cwd', type=Path, required=True)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--stdin-file', type=Path)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command and args.command[0] == '--' else args.command
    try:
        if os.name != 'nt' or not command:
            raise ValueError('A Windows host and an explicit worker command are required')
        if any(not re.fullmatch(r'[a-zA-Z0-9_-]{1,96}', part) for part in (args.attempt_id, args.phase)):
            raise ValueError('Invalid attempt identifier or phase')
        deadline = datetime.fromisoformat(args.deadline_at)
        if deadline.utcoffset() is None:
            raise ValueError('The deadline must include a timezone offset')
        cwd = args.cwd.resolve(strict=True)
        prompt = args.stdin_file.resolve(strict=True) if args.stdin_file else None
        value = run_stage(args.attempt_id, args.phase, deadline, cwd, args.evidence_dir.resolve(), prompt, command)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({'outcome': 'runner_error', 'reason': str(error), 'stop_confirmed': False}))
        return 1
    print(json.dumps(value, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
