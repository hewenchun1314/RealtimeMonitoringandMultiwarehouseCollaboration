"""Run the bound work-item chat through a non-interactive Codex CLI."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from threading import Event
from uuid import UUID, uuid4

from backend_dependencies import worker_roots
from desktop_runtime import runtime_check
from dingtalk_notify import dispatch as dispatch_dingtalk, enabled as dingtalk_enabled
from ledger import validate_config
from stage_runner import run_stage, utc_now
from task_sync import enabled as task_sync_enabled
from workflow import CONFIG_PATH, DATABASE_PATH, PROJECT_ROOT


def codex_directory():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')


def command(cli, operation, chat_id=None, *, protected_roots=(), writable_roots=()):
    argv = [str(cli), '--ask-for-approval', 'never', 'exec',
            '--json', '--color', 'never', '--skip-git-repo-check']
    if protected_roots or writable_roots:
        if any(root.is_relative_to(protected) for root in writable_roots for protected in protected_roots):
            raise ValueError('A writable worker root is inside a read-only dependency')
        filesystem = {':root': 'read', ':tmpdir': 'write'}
        filesystem.update({path.as_posix(): 'write' for path in writable_roots})
        filesystem.update({path.as_posix(): 'read' for path in protected_roots})
        entries = ','.join(f'{json.dumps(path, ensure_ascii=False)}="{access}"'
                           for path, access in filesystem.items())
        argv.extend(['-c', 'default_permissions="zentao_delivery"',
                     '-c', 'permissions.zentao_delivery.filesystem={' + entries + '}',
                     '-c', 'permissions.zentao_delivery.network.enabled=true',
                     '-c', 'windows.sandbox="mxc"'])
    if operation == 'probe':
        argv.append('--ephemeral')
    if operation == 'run':
        argv.extend(['resume', str(UUID(chat_id))])
    return [*argv, '-']


def read_item(marker):
    with closing(sqlite3.connect(DATABASE_PATH.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute('SELECT * FROM work_items WHERE marker=?', (marker,)).fetchone()


def worker_prompt(operation, item):
    if operation == 'probe':
        return (
            '这是无人值守入口的只读探针。必须用 shell 实际运行下面的 PowerShell 命令。'
            '执行成功后回答结果。禁止写文件、访问禅道或请求权限。\n'
            "if ((Get-Item -LiteralPath 'AGENTS.md').Exists) { Write-Output WORKER_RUNTIME_OK }"
        )
    if operation == 'prepare':
        return f'这是禅道工单 {item["marker"]} 的固定会话。只回复「就绪」，不要调用工具或开始执行。'
    identity = {key: item[key] for key in ('instance', 'project_id', 'kind')}
    identity['id'] = item['item_id']
    binding = {'item': identity, 'request_id': item['request_id'], 'chat_id': item['chat_id']}
    return (
        f'从项目根目录读取 .agents/skills/zentao-autodelivery/SKILL.md，并按照其中的工单执行入口执行。'
        f'这是经授权的无人值守工单 {item["marker"]}。原账本绑定为 {json.dumps(binding)}。'
        '先用 record runtime_check 核对本轮策略，并按实际账本状态领取或续接。'
        '禁止申请临时权限、弹审批、提问或等待人工确认，也不要开启新的桌面聊天。'
        '每次阶段尝试使用账本截止时间及阶段运行器。'
        '按本单快照与账本当前阶段分派评审、拆分和本端开发或测试 Agent，主会话负责分支核验和合回。'
        '把后端阶段回执中的 backend_dependencies 交给对应步骤 Agent。先追踪业务服务自己的接口与实现，'
        '只在本单涉及上游调用时读取对应依赖的契约。read_only 依赖只供查阅。writable 依赖确需修改时，'
        '按同名后端服务列入计划并使用核验过的工单工作树。前端使用已配置业务后端的契约。'
        '跨端工单在后端合回且联调交接通过后才启动前端。旧工单沿用原阶段。'
        '保持已有成功证据，核对后继续到可核实的收尾，按授权回写并读回原禅道工单。'
    )


def probe_passed(receipt):
    events = [json.loads(line) for line in Path(receipt['stdout_path']).read_text(encoding='utf-8').splitlines()]
    shell_results = (event.get('item', {}) for event in events if event['type'] == 'item.completed')
    return (receipt['outcome'] == 'success' and receipt['stop_confirmed']
            and any(item.get('type') == 'command_execution' and item.get('exit_code') == 0
                    and 'WORKER_RUNTIME_OK' in item.get('aggregated_output', '').split()
                    for item in shell_results)
            and any(event['type'] == 'turn.completed' for event in events))


def run(operation, marker, cli):
    import msvcrt

    if operation == 'probe':
        item = None
        output = DATABASE_PATH.parent / 'worker-probes' / uuid4().hex
    else:
        if not marker or not re.fullmatch(r'HNF-[0-9a-f]{20}', marker):
            raise ValueError('A stable work-item marker is required')
        item = read_item(marker)
        if item is None:
            raise ValueError('Unknown work-item marker')
        output = DATABASE_PATH.parent / 'items' / marker / 'worker'
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'worker.lock').open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return {'status': 'worker_busy'}
        if item is not None:
            item = read_item(marker)
            if operation == 'prepare' and (item['state'] != 'reserved' or item['chat_id']):
                raise ValueError('Preparing requires a reserved and unbound work item')
            if operation == 'run' and (item['state'] not in ('bound', 'running', 'closing') or not item['chat_id']):
                raise ValueError('Running requires an active ledger binding')
        if operation == 'prepare':
            run_id = UUID(item['request_id']).hex
            for directory in output.glob('*/prepare'):
                previous = directory / 'result.json'
                if not previous.is_file():
                    return {'status': 'worker_prepare_uncertain', 'evidence': str(directory)}
                proof = json.loads(previous.read_text(encoding='utf-8'))
                existing = prepared_result(proof)
                if existing['chat_id'] or not proof['stop_confirmed'] or directory.parent.name == run_id:
                    return existing
        else:
            run_id = uuid4().hex
        config = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
        validate_config(config)
        configs = [config]
        if item is not None and item['config_snapshot']:
            configs.append(json.loads(item['config_snapshot']))
        # The current configuration can narrow permissions for an admitted item.
        protected, writable = worker_roots(configs, CONFIG_PATH.parent, PROJECT_ROOT, codex_directory())
        argv = command(cli, operation, item['chat_id'] if item is not None else None,
                       protected_roots=protected, writable_roots=writable)
        prompt_file = output / f'{run_id}.txt'
        prompt_file.write_text(worker_prompt(operation, item), encoding='utf-8')
        os.environ.pop('CODEX_THREAD_ID', None)
        deadline = utc_now() + (timedelta(hours=72) if operation == 'run' else timedelta(minutes=4))
        launch = (run_id, operation, deadline, PROJECT_ROOT, output, prompt_file, argv)
        sync_receipt = None
        if operation == 'run' and task_sync_enabled(item):
            stop = Event()
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(run_stage, run_id, 'task-sync', deadline, PROJECT_ROOT, output, None,
                                         sync_command('--marker', marker, '--chat-id', item['chat_id']),
                                         stop_event=stop)
                try:
                    receipt = run_stage(*launch)
                finally:
                    stop.set()
                sync_receipt = future.result()
        else:
            receipt = run_stage(*launch)
        if operation == 'prepare':
            return prepared_result(receipt)
        if operation == 'probe':
            return {'status': 'probe_passed' if probe_passed(receipt) else 'probe_failed',
                    'receipt': receipt['receipt_path']}
        stopped = receipt['stop_confirmed'] and (sync_receipt is None or sync_receipt['stop_confirmed'])
        result = {'status': 'worker_stopped' if stopped else 'worker_stop_unverified',
                  'receipt': receipt['receipt_path'], 'outcome': receipt['outcome']}
        if sync_receipt:
            result.update(task_sync_receipt=sync_receipt['receipt_path'], task_sync_outcome=sync_receipt['outcome'])
        return result


def sync_command(*args):
    return [sys.executable, '-B', str(Path(__file__).with_name('task_sync_runner.py')), *args]


def run_monitor_sync():
    if runtime_check(os.environ.get('CODEX_THREAD_ID', ''))['status'] != 'runtime_unattended':
        raise ValueError('Task synchronization requires an unattended monitor session')
    receipt = run_stage(uuid4().hex, 'monitor', utc_now() + timedelta(minutes=3), PROJECT_ROOT,
                        DATABASE_PATH.parent / 'task-sync', None, sync_command())
    result = {'status': 'tasks_checked' if receipt['outcome'] == 'success' else 'task_sync_incomplete',
              'outcome': receipt['outcome'], 'stop_confirmed': receipt['stop_confirmed'],
              'receipt': receipt['receipt_path']}
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
        if dingtalk_enabled(config):
            result['dingtalk'] = dispatch_dingtalk()
    except (OSError, ValueError, TypeError, AttributeError):
        result['dingtalk'] = {'status': 'configuration_error'}
    return result


def prepared_result(receipt):
    session = receipt.get('stop_evidence', {}).get('codex_session', {})
    thread_id = session.get('thread_id')
    ready = (receipt['outcome'] == 'success' and receipt['stop_confirmed'] and thread_id
             and session.get('last_turn_event') == 'turn.completed'
             and runtime_check(thread_id)['status'] == 'runtime_unattended')
    return {'status': 'worker_ready' if ready else 'worker_prepare_uncertain', 'chat_id': thread_id,
            'receipt': receipt['receipt_path']}


def run_e2e_preflight(profile):
    import msvcrt

    attempt_id = uuid4().hex
    phase = 'e2e-preflight'
    output = DATABASE_PATH.parent / 'worker-probes'
    startup_log = output / attempt_id / phase / 'frontend.log'
    command = [sys.executable, '-B', str(Path(__file__).with_name('playwright_e2e.py')),
               '--startup-log', str(startup_log)]
    if profile:
        command.extend(['--profile', str(profile.resolve(strict=True))])
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'e2e-preflight.lock').open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return {'status': 'e2e_preflight_busy', 'check': {'stage': 'runner-lock'}}
        receipt = run_stage(attempt_id, phase, utc_now() + timedelta(minutes=4), PROJECT_ROOT,
                            output, None, command)
    try:
        check = json.loads(Path(receipt['stdout_path']).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        check = {'stage': 'runner'}
    passed = receipt['outcome'] == 'success' and receipt['stop_confirmed'] and check.get('status') == 'passed'
    return {'status': 'e2e_preflight_passed' if passed else 'e2e_preflight_blocked',
            'check': check, 'outcome': receipt['outcome'], 'stop_confirmed': receipt['stop_confirmed'],
            'receipt': receipt['receipt_path']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('prepare', 'run', 'probe', 'sync-tasks', 'e2e-preflight'))
    parser.add_argument('--marker')
    parser.add_argument('--profile', type=Path)
    args = parser.parse_args()
    if args.profile and args.operation != 'e2e-preflight':
        parser.error('--profile is only supported for e2e-preflight')
    cli = codex_directory() / 'packages' / 'app-server-daemon' / 'current' / 'bin' / 'codex.exe'
    try:
        if args.operation == 'e2e-preflight':
            result = run_e2e_preflight(args.profile)
        elif args.operation == 'sync-tasks':
            result = run_monitor_sync()
        else:
            result = run(args.operation, args.marker, cli.resolve(strict=True))
    except (OSError, TypeError, ValueError, sqlite3.Error) as error:
        result = {'status': 'worker_error', 'reason': str(error)}
    print(json.dumps(result, ensure_ascii=True))
    return int(result['status'] in ('worker_error', 'probe_failed', 'task_sync_incomplete',
                                    'e2e_preflight_blocked', 'e2e_preflight_busy'))


if __name__ == '__main__':
    sys.exit(main())
