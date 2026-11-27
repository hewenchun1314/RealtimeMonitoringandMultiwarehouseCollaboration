"""Supervise one local stage worker and its owned processes on Windows, Linux and macOS."""

import argparse
from contextlib import nullcontext
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from socket import gethostname
import subprocess
import sys
import time
from uuid import uuid4

from runtime_paths import LOCAL_DIRECTORY


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

    def __init__(self, name=None):
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
        self.handle = self.api.CreateJobObjectW(None, name)
        self.check(self.handle)
        if name is not None and ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            self.close()
            raise OSError('Recovery Job Object name already exists')
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


def recover_job_stop(launch):
    """Recover only observable stop evidence for a run on the original host."""
    if launch.get('job_host') != gethostname():
        return None
    if os.name == 'posix':
        from posix_stage import members
        pid, scope = launch.get('launcher_pid'), launch.get('posix_scope')
        if launch.get('mechanism') != 'posix_session' or type(pid) is not int or pid <= 1 or scope not in ('session', 'group'):
            return None
        try:
            cleanup = Path(launch['receipt_path']).with_name('guard-stopped')
            if members(pid, scope) or cleanup.read_text(encoding='ascii') != str(pid):
                return None
        except (KeyError, OSError, ValueError, subprocess.SubprocessError):
            return None
        return {'mechanism': 'posix_session', 'job_host': gethostname(), 'launcher_pid': pid,
                'owned_processes_active_after_stop': 0, 'verified_at': utc_now().isoformat()}
    name = launch.get('job_name')
    if (os.name != 'nt' or launch.get('job_host') != gethostname() or not isinstance(name, str)
            or not re.fullmatch(r'Global\\ZentaoDeploy-[0-9a-f]{32}', name)):
        return None
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    api.OpenJobObjectW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
    ]
    handle = api.OpenJobObjectW(0x0004, False, name)  # JOB_OBJECT_QUERY
    if handle:
        try:
            accounting = Accounting()
            if (not api.QueryInformationJobObject(handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None)
                    or accounting.active):
                return None
            observed = 'zero_active_processes'
        finally:
            api.CloseHandle(handle)
    elif ctypes.get_last_error() == 2:  # ERROR_FILE_NOT_FOUND
        # Windows destroys the named job after its handles close and all
        # associated processes terminate.
        observed = 'job_destroyed'
    else:
        return None
    return {'mechanism': 'windows_job_object', 'job_name': name, 'job_host': gethostname(),
            'observation': observed, 'owned_processes_active_after_stop': 0, 'verified_at': utc_now().isoformat()}


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
            if event.get('type') == 'turn.started':
                session['turn_started'] = True
            if event.get('type') in ('turn.started', 'turn.completed', 'turn.failed'):
                session['last_turn_event'] = event['type']
    return session


def bootstrap(prompt, pid_path, command):
    # A closed pipe means the supervisor failed before the process was contained.
    if sys.stdin.buffer.read(1) != b'G':
        return 125
    with open(prompt, 'rb') if prompt else nullcontext(subprocess.DEVNULL) as input_stream:
        with subprocess.Popen(command, stdin=input_stream) as child:
            Path(pid_path).write_text(str(child.pid), encoding='ascii')
            return child.wait()


def run_stage(attempt_id, phase, deadline, cwd, evidence_dir, prompt, command, *, stop_event=None, recoverable=False, agent=None, env=None):
    directory = evidence_dir / attempt_id / phase
    directory.mkdir(parents=True, exist_ok=False)
    receipt_path = directory / 'result.json'
    stdout_path = directory / 'stdout.log'
    stderr_path = directory / 'stderr.log'
    hosted_agent = Path(command[0]).name.lower() in ('codex', 'codex.exe') and 'exec' in command[1:]
    if agent is not None:
        hosted_agent = agent
    result = {
        'attempt_id': attempt_id, 'phase': phase, 'deadline_at': deadline.isoformat(),
        'receipt_path': str(receipt_path), 'stdout_path': str(stdout_path), 'stderr_path': str(stderr_path),
    }
    mechanism = 'windows_job_object' if os.name == 'nt' else 'posix_session'
    evidence = {'mechanism': mechanism, 'working_directory': str(cwd), 'job_host': gethostname()}
    environment = dict(os.environ if env is None else env)
    environment['npm_config_cache'] = str(LOCAL_DIRECTORY / 'npm-cache')
    if utc_now() >= deadline:
        result.update(outcome='timed_out', stop_confirmed=True,
                      stop_evidence={**evidence, 'worker_started': False, 'owned_processes_at_stop': 0,
                                     'owned_processes_active_after_stop': 0})
        write_json(receipt_path, result)
        return result

    if hosted_agent:
        from workflow import CONFIG_PATH

        runtime = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig')).get('runtime', {})
        overrides = []
        for key, setting in (('codex_model', 'model'), ('codex_reasoning_effort', 'model_reasoning_effort')):
            value = runtime.get(key)
            if value is not None:
                overrides.extend(['-c', f'{setting}={json.dumps(value)}'])
        command = [command[0], *overrides, *command[1:]]

    if os.name == 'posix':
        from posix_stage import run as run_posix
        with stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
            outcome, confirmed = run_posix(command, prompt, cwd, directory, stdout, stderr, environment,
                deadline, utc_now, evidence, lambda: write_json(directory / 'launch.json', {**result, **evidence}), stop_event)
        if hosted_agent:
            evidence['codex_session'] = parse_session(stdout_path)
        result.update(outcome=outcome, stop_confirmed=confirmed, stop_evidence=evidence)
        write_json(receipt_path, result)
        return result

    job_name = 'Global\\ZentaoDeploy-' + uuid4().hex if recoverable else None
    if job_name is not None:
        evidence.update(job_name=job_name, job_host=gethostname())
    job = Job(job_name)
    process = None
    try:
        with stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
            pid_path = directory / 'worker-pid'
            argv = [sys.executable, '-B', __file__, '--bootstrap', str(prompt) if prompt else '', str(pid_path), *command]
            process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                       env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
            job.assign(process.pid)
            evidence.update(supervisor_pid=os.getpid(), launcher_pid=process.pid,
                            launch_timestamp=utc_now().isoformat(), worker_started=False)
            write_json(directory / 'launch.json', {**result, **evidence})
            remaining = (deadline - utc_now()).total_seconds()
            if remaining > 0:
                process.stdin.write(b'G')
                process.stdin.flush()
            process.stdin.close()
            while utc_now() < deadline:
                if process.poll() is not None:
                    break
                if stop_event is not None and stop_event.is_set():
                    break
                remaining = max(0, (deadline - utc_now()).total_seconds())
                try:
                    wait = min(0.25, remaining) if stop_event is not None else remaining
                    process.wait(timeout=wait)
                except subprocess.TimeoutExpired:
                    continue
            timed_out = utc_now() >= deadline
            stopped = stop_event is not None and stop_event.is_set() and process.poll() is None

            before = job.accounting()
            after = job.stop() if before.active else before
            process.wait(timeout=5)
            evidence.update(
                owned_processes_at_stop=before.active,
                owned_processes_active_after_stop=after.active,
                owned_processes_total=after.total,
                worker_started=pid_path.is_file(),
                verified_at=utc_now().isoformat(),
                launcher_exit_code=process.returncode,
            )
            if hosted_agent:
                evidence['codex_session'] = parse_session(stdout_path)
            outcome = 'timed_out' if timed_out else ('success' if process.returncode == 0 else 'failed')
            if stopped:
                outcome = 'stopped'
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
        return bootstrap(argv[1], argv[2], argv[3:])
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
        if not command:
            raise ValueError('An explicit worker command is required')
        if (not re.fullmatch(r'[a-zA-Z0-9_-]{1,96}', args.attempt_id) or len(args.phase) > 96
                or not re.fullmatch(r'(?:(?:backend|frontend)\.)?[a-zA-Z0-9_-]+', args.phase)):
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
