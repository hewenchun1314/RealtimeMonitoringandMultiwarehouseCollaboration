"""A private POSIX session for a stage; nested stages retain the outer session."""

import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time


def members(leader, scope):
    # BSD and GNU ps support these fields. Getsid is portable across macOS/Linux.
    # Zombies have already closed resources and cannot execute another instruction.
    output = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,stat='], text=True, timeout=5)
    children = {}
    found = set()
    for row in output.splitlines():
        pid, parent, state = row.split()
        if state.startswith('Z'):
            continue
        pid = int(pid)
        try:
            owner = os.getsid(pid) if scope == 'session' else os.getpgid(pid)
        except ProcessLookupError:
            continue
        except PermissionError:
            owner = None
        if owner == leader:
            found.add(pid)
        children.setdefault(int(parent), []).append(pid)
    # Include live descendants that started their own group/session, even if
    # the group changed since launch. Visit parents before descendants.
    queue = list(found)
    while queue:
        for pid in children.get(queue.pop(), ()):
            if pid not in found:
                found.add(pid)
                queue.append(pid)
    return found


def record_exit(path, value):
    pending = path.with_suffix('.tmp')
    pending.write_text(str(value), encoding='ascii')
    pending.replace(path)


def guard(exit_file, prompt, command):
    # Do not start user code until the supervisor has durably recorded our PID.
    exit_file = Path(exit_file)
    cleanup = exit_file.with_name('guard-stopped')
    if os.read(0, 1) != b'G':
        record_exit(cleanup, os.getpid())
        return 125
    if sys.platform.startswith('linux'):
        # Adopt double-forked descendants, including detached services. When
        # every original parent goes away these children still belong to us.
        import ctypes
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'Could not supervise orphaned descendants')
    scope = 'session' if os.getsid(0) == os.getpid() else 'group'
    environment = dict(os.environ, ZENTAO_STAGE_SESSION=str(os.getsid(0)))
    try:
        with open(prompt or os.devnull, 'rb') as stream:
            child = subprocess.Popen(command, stdin=stream, env=environment)
    except OSError as error:
        print(error, file=sys.stderr)
        record_exit(exit_file, 127)
        record_exit(cleanup, os.getpid())
        return 0
    record_exit(exit_file.with_name('worker-pid'), child.pid)
    while True:
        # A Linux subreaper also adopts orphans. Harvest them during a long
        # stage, including the foreground child when it exits.
        while True:
            try:
                pid, wait_status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
            if pid == child.pid:
                child.returncode = os.waitstatus_to_exitcode(wait_status)
        status = child.returncode
        if status is not None and not exit_file.exists():
            record_exit(exit_file, status)
        # A dead supervisor closes this pipe. Keep the group leader alive until all
        # children are reaped/killed, so its identifier cannot be reused during cleanup.
        ready, _, _ = select.select([0], [], [], 0.1)
        if ready and not os.read(0, 1):
            break
    until = time.monotonic() + 6
    while time.monotonic() < until:
        child.poll()
        remaining = members(os.getpid(), scope) - {os.getpid()}
        if not remaining:
            record_exit(cleanup, os.getpid())
            return 0
        # Freeze before killing so a process cannot fork more children
        # between enumerating its descendants and stopping their parents.
        for sig in (signal.SIGSTOP, signal.SIGKILL):
            for pid in remaining & members(os.getpid(), scope):
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
        time.sleep(0.035)
    return 125


def run(command, prompt, cwd, directory, stdout, stderr, env, deadline, now, evidence, publish, stop_event):
    nested = os.environ.get('ZENTAO_STAGE_SESSION') == str(os.getsid(0))
    options = {'process_group': 0} if nested else {'start_new_session': True}
    exit_file = directory / 'worker-exit'
    argv = [sys.executable, '-B', __file__, str(exit_file), str(prompt) if prompt else '', *command]
    with subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, **options) as guard_process:
        scope = 'group' if nested else 'session'
        evidence.update(launcher_pid=guard_process.pid, supervisor_pid=os.getpid(), posix_scope=scope,
                        launch_timestamp=now().isoformat(), worker_started=False)
        worker_code = None
        stopped = False
        try:
            publish()
            if now() < deadline:
                guard_process.stdin.write(b'G')
                guard_process.stdin.flush()
            while now() < deadline:
                if exit_file.is_file():
                    worker_code = int(exit_file.read_text(encoding='ascii'))
                    break
                if guard_process.poll() is not None:
                    break
                if stop_event is not None and stop_event.is_set():
                    stopped = True
                    break
                time.sleep(min(0.10, max(0, (deadline - now()).total_seconds())))
            timed_out = now() >= deadline
            before = members(guard_process.pid, scope)
        finally:
            guard_process.stdin.close()
            guard_process.wait(timeout=9)
        after = members(guard_process.pid, scope)
        evidence.update(owned_processes_at_stop=len(before), owned_processes_active_after_stop=len(after),
                        worker_started=(directory / 'worker-pid').is_file() or len(before) > 1, verified_at=now().isoformat(),
                        launcher_exit_code=worker_code)
        outcome = 'timed_out' if timed_out else 'stopped' if stopped else 'success' if worker_code == 0 else 'failed'
        return outcome, not after and guard_process.returncode == 0


if __name__ == '__main__':
    sys.exit(guard(sys.argv[1], sys.argv[2], sys.argv[3:]))
