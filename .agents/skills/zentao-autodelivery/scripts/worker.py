"""Run the bound work-item chat through a non-interactive Codex CLI."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
from threading import Event
import time
from uuid import UUID, uuid4

from backend_deploy_runner import deployment_action, run_deployment
from backend_dependencies import worker_roots
from desktop_runtime import runtime_check
from deploy_state import FRONTEND_DEPLOY_STAGE, GIT_PUSH_STAGES
from ledger import validate_config
from portable_lock import file_lock
from runtime_paths import LOCAL_DIRECTORY
import git_broker
from frontend_release_runner import run_frontend_release
from frontend_e2e_runner import (frontend_delivery_in_use, frontend_e2e_lock, frontend_queue_owner,
                                 frontend_waiting, pending_frontend_e2e, run_frontend_e2e)
from git_delivery_runner import run_git_delivery
from development_git_runner import pending_development_git, run_development_git
from stage_runner import parse_session, recover_job_stop, run_stage, utc_now
from workflow import CONFIG_PATH, DATABASE_PATH, PROJECT_ROOT
from material_sync_runner import (pending_material_sync, run_material_sync, run_git_preflight,
                                  release_preflight_reservation)


def codex_directory():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')


def resolve_cli(config=None):
    if config is None:
        config = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
    configured = config.get('runtime', {}).get('codex_cli')
    if configured:
        candidate = shutil.which(os.path.expanduser(configured))
    else:
        candidate = shutil.which('codex.exe') if os.name == 'nt' else None
        candidate = candidate or shutil.which('codex')
    if not candidate:
        raise ValueError('Install Codex CLI on PATH or set runtime.codex_cli to an executable')
    path = Path(candidate).absolute()
    if os.name == 'nt' and path.suffix.lower() in ('.cmd', '.bat', '.ps1', ''):
        # Use the native binary from the npm distribution. Passing permission
        # expressions through cmd.exe would alter their quoting and arguments.
        package = path.parent / 'node_modules' / '@openai' / 'codex'
        binaries = list(package.glob('node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe'))
        if len(binaries) != 1:
            raise ValueError('Set runtime.codex_cli to the native Codex executable')
        path = binaries[0]
    return path


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
                     '-c', 'permissions.zentao_delivery.network.enabled=true'])
        if os.name == 'nt':
            argv.extend(['-c', 'windows.sandbox="mxc"'])
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
        interpreter = 'python' if os.name == 'nt' else 'python3'
        return (f'请使用 shell 实际执行只读检查：{interpreter} -c "from pathlib import Path; '
                "assert Path('AGENTS.md').is_file(); print('WORKER_RUNTIME_OK')\""
                '。记录实际结果；不要写文件或访问禅道。')
    if operation == 'prepare':
        return f'这是禅道工单 {item["marker"]} 的固定会话。只回复「就绪」，不要调用工具或开始执行。'
    identity = {key: item[key] for key in ('instance', 'project_id', 'kind')}
    identity['id'] = item['item_id']
    binding = {'item': identity, 'request_id': item['request_id'], 'chat_id': item['chat_id']}
    source_context = ''
    admitted = json.loads(item['config_snapshot'] or '{}')
    if admitted.get('zentao', {}).get('scope_mode') == 'project_or_product':
        with closing(sqlite3.connect(DATABASE_PATH.as_uri() + '?mode=ro', uri=True)) as conn:
            source = conn.execute('SELECT source FROM work_item_sources WHERE item_seq=?', (item['seq'],)).fetchone()
        source_context = ('本单按“所属项目或所属产品”接收，绑定中的 project_id 是自动化队列项目，'
                          '不代表原单必须关联此项目。原始归属：' + (source[0] if source else '待复核') + '。'
                          '按工单快照中 product_id 或实际项目复核原单；禁止改写原单项目来迁就账本。')
    if admitted.get('e2e_attestation') == 'launch' and admitted.get('e2e', {}).get('after_integration'):
        e2e_request = (
            '新的前端正式验收取得 host_e2e 后，仅编写 JSON 页面场景（entry_path、可选 work_area/checks、'
            'cases 或 steps），按回执路径提交 host-request.json，只填 attempt_id 和本工单相对 scenario 路径。'
            '由宿主核对浏览器加载版本，写完请求后结束本轮。')
    else:
        e2e_request = (
            '前端正式验收取得 host_e2e 后，在回执路径写 host-request.json，'
            '包含 attempt_id、工单目录相对的 profile 和 scenario（均为 JSON）以及 base_url，并结束本轮。')
    return (
        f'从项目根目录读取 .agents/skills/zentao-autodelivery/SKILL.md，并按照其中的工单执行入口执行。'
        f'这是经授权的无人值守工单 {item["marker"]}。原账本绑定为 {json.dumps(binding)}。'
        f'{source_context}'
        '先用 record runtime_check 核对本轮策略，并按实际账本状态领取或续接。'
        '恢复轮次读取本工单 recoveries 的最新记录及其中的本地证据文件，落实已核验的受阻范围。'
        '禁止申请临时权限、弹审批、提问或等待人工确认，也不要开启新的桌面聊天。'
        '每次阶段尝试使用账本截止时间及阶段运行器。'
        '材料检查需要同步时执行 record material_sync。使用统一的 workflow.py record 入口，'
        'branch_prepare、candidate_commit、integration_merge 和材料同步都会等待宿主执行并返回结果。'
        '保持同一会话，只有拿到已核实的回执才继续；结果不明时查询原尝试或原 operation_id。'
        '禁止在沙箱自行补取、提交、合回或读取 Git 凭据。'
        '主工作区可有已暂存和未暂存改动。单仓返回 integration_conflict 时，主聊天在当前 integration 尝试'
        '按 references/branches.md 组织逐文件协调，取得解决内容和复验依据，以新事件提交 resolutions。'
        '读到 integration_retry_required 表示原工作区已恢复，立即在同一 integration 阶段用新事件续办。'
        f'{e2e_request}'
        '宿主执行后从同一目录的 agentlimb.json 读取实际结果，再登记账本原尝试。'
        '若返回 frontend_delivery_busy，结束本轮等待当前交付单完成。'
        '沙箱内只编辑、构建、测试及只读 Git 核验；禁止自行 fetch、worktree add、commit、checkout 或 merge，禁止跳过原有 hooks。'
        '若阶段回执带 bug_intake_required，主会话按步骤指引直接核对并登记 Bug 的仓库与验收条件。'
        '按本单快照与账本当前阶段分派评审、拆分和本端开发或测试 Agent，主会话负责分支核验和合回。'
        '把后端阶段回执中的 backend_dependencies 交给对应步骤 Agent。先追踪业务服务自己的接口与实现，'
        '只在本单涉及上游调用时读取对应依赖的契约。read_only 依赖只供查阅。writable 依赖确需修改时，'
        '按同名后端服务登记目标仓库并使用核验过的工单工作树。前端使用已配置业务后端的契约。'
        '若账本进入 backend.push、frontend.push、backend.deploy 或 frontend.deploy（含返工分段），'
        '结束当前轮次，宿主执行器会接手并续接原会话。'
        '各阶段按 task_scope.active 派发，按编号记录 task_blockers 与证据。'
        '依次处理账本返回的每个阶段。当前阶段缺少必要输入或没有可处理的任务时，'
        '用 attempt_finish 的 outcome=skipped 记录本阶段核对结果并继续；实际运行失败记 failed。'
        '跨端依赖须验证交接；交接受阻时按账本继续独立的前端任务。'
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


def launch_turn(cli, operation, item, output, deadline):
    run_id = UUID(item['request_id']).hex if operation == 'prepare' else uuid4().hex
    config = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
    validate_config(config)
    configs = [config]
    if item is not None and item['config_snapshot']:
        configs.append(json.loads(item['config_snapshot']))
    # Recheck current permissions before each new or resumed turn.
    protected, writable = worker_roots(configs, CONFIG_PATH.parent, PROJECT_ROOT, codex_directory())
    for directory in (DATABASE_PATH.parent, LOCAL_DIRECTORY / 'npm-cache'):
        if not any(directory.is_relative_to(root) for root in writable):
            writable.append(directory)
    argv = command(cli, operation, item['chat_id'] if item is not None else None,
                   protected_roots=protected, writable_roots=writable)
    prompt_file = output / f'{run_id}.txt'
    prompt_file.write_text(worker_prompt(operation, item), encoding='utf-8')
    launch = (run_id, operation, deadline, PROJECT_ROOT, output, prompt_file, argv)
    environment = os.environ.copy()
    environment.pop('CODEX_THREAD_ID', None)
    environment.pop('ZENTAO_GIT_BROKER_TURN', None)
    # The CLI can filter inherited shell variables. Keep all workflow clients
    # on this launcher's configuration and ledger, including isolated runs.
    shell_environment = {
        'ZENTAO_DELIVERY_CONFIG': str(CONFIG_PATH.resolve()),
        'ZENTAO_DELIVERY_DATABASE': str(DATABASE_PATH.resolve()),
        'npm_config_cache': str(LOCAL_DIRECTORY / 'npm-cache'),
    }
    if operation == 'run':
        shell_environment['ZENTAO_GIT_BROKER_TURN'] = run_id
    environment.update(shell_environment)
    for key, value in shell_environment.items():
        argv[1:1] = ['-c', f'shell_environment_policy.set.{key}={json.dumps(value, ensure_ascii=False)}']
    if operation != 'run':
        return run_stage(*launch, recoverable=True, agent=True, env=environment), None
    git_broker.prepare(DATABASE_PATH)
    stop = Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        broker = executor.submit(git_broker.service, run_id, item, DATABASE_PATH, CONFIG_PATH, PROJECT_ROOT, stop)
        broker.add_done_callback(lambda _: stop.set())
        try:
            receipt = run_stage(*launch, recoverable=True, agent=True, env=environment, stop_event=stop)
        finally:
            stop.set()
        broker.result()
    return receipt, None


def worker_result(receipt, _unused=None):
    return {'status': 'worker_stopped' if receipt['stop_confirmed'] else 'worker_stop_unverified',
            'receipt': receipt['receipt_path'], 'outcome': receipt['outcome']}


def previous_turn_stopped(output):
    # Check every launch without an affirmative receipt. The OS lock alone is
    # insufficient after supervisor loss since descendants might still be alive.
    for turn in output.glob('*/run'):
        try:
            receipt = turn / 'result.json'
            if receipt.is_file() and json.loads(receipt.read_text(encoding='utf-8')).get('stop_confirmed') is True:
                continue
            if not recover_job_stop(json.loads((turn / 'launch.json').read_text(encoding='utf-8'))):
                return False
        except (OSError, ValueError):
            return False
    return True


def dispatch_work_item(marker, launch_chat, deadline=None, *, recover_deployment=False,
                       recover_material=None, recover_git=None, recover_e2e=None):
    deadline = deadline or utc_now() + timedelta(hours=72)

    def peek():
        return deployment_action('peek', marker, DATABASE_PATH, CONFIG_PATH)

    pending = peek()
    last_turn = {}
    last_deployment = None
    last_material = None
    last_git = None
    last_e2e = None
    release_key = 'backend_deployment'
    while utc_now() < deadline:
        state = pending['status']
        item = read_item(marker)
        if not item or item['state'] not in ('bound', 'running', 'closing'):
            return last_turn or {'status': 'worker_released'}
        if frontend_queue_owner(marker, DATABASE_PATH, CONFIG_PATH) is not None:
            time.sleep(max(0, min(2, (deadline - utc_now()).total_seconds())))
            pending = peek()
            continue
        e2e = pending_frontend_e2e(marker, DATABASE_PATH, CONFIG_PATH)
        if e2e['status'] == 'host_e2e_pending':
            if last_turn.get('status') != 'worker_stopped' and not (recover_e2e and recover_e2e(item)):
                return {**last_turn, 'status': 'worker_host_e2e_stop_unverified'}
            if runtime_check(item['chat_id'])['status'] != 'runtime_unattended':
                return {**last_turn, 'status': 'worker_host_e2e_blocked',
                        'frontend_e2e': {'status': 'host_e2e_runtime_blocked'}}
            last_e2e = run_frontend_e2e(marker, DATABASE_PATH, CONFIG_PATH, PROJECT_ROOT)
            if last_e2e['status'] == 'host_e2e_busy':
                time.sleep(max(0, min(2, (deadline - utc_now()).total_seconds())))
            elif last_e2e['status'] not in ('host_e2e_finished', 'host_e2e_inactive', 'host_e2e_not_pending'):
                return {**last_turn, 'status': 'worker_host_e2e_blocked', 'frontend_e2e': last_e2e}
            continue
        development_git = pending_development_git(marker, DATABASE_PATH, CONFIG_PATH)
        if development_git['status'] == 'host_git_pending':
            if last_turn.get('status') != 'worker_stopped' and not (recover_git and recover_git(item)):
                return {**last_turn, 'status': 'worker_host_git_stop_unverified'}
            if runtime_check(item['chat_id'])['status'] != 'runtime_unattended':
                return {**last_turn, 'status': 'worker_host_git_blocked',
                        'development_git': {'status': 'host_git_runtime_blocked'}}
            last_git = run_development_git(marker, DATABASE_PATH, CONFIG_PATH, PROJECT_ROOT, recover=True)
            if last_git['status'] not in ('host_git_completed', 'host_git_failed', 'host_git_inactive', 'host_git_not_pending'):
                return {**last_turn, 'status': 'worker_host_git_blocked', 'development_git': last_git}
            continue
        material = pending_material_sync(marker, DATABASE_PATH, CONFIG_PATH)
        if material['status'] == 'material_sync_pending':
            if last_turn.get('status') != 'worker_stopped' and not (recover_material and recover_material(item)):
                return {**last_turn, 'status': 'worker_material_stop_unverified'}
            if runtime_check(item['chat_id'])['status'] != 'runtime_unattended':
                return {**last_turn, 'status': 'worker_material_blocked',
                        'material_sync': {'status': 'material_runtime_blocked'}}
            last_material = run_material_sync(marker, DATABASE_PATH, CONFIG_PATH, PROJECT_ROOT, recover=True)
            if last_material['status'] not in ('material_synced', 'material_sync_failed', 'material_sync_inactive'):
                return {**last_turn, 'status': 'worker_material_blocked', 'material_sync': last_material}
            continue
        if state == 'deployment_pending':
            stage = pending.get('stage')
            if stage in GIT_PUSH_STAGES:
                runner = run_git_delivery
                release_key = 'git_delivery'
            elif stage == FRONTEND_DEPLOY_STAGE:
                runner = run_frontend_release
                release_key = 'frontend_deployment'
            else:
                runner = run_deployment
                release_key = 'backend_deployment'
            if runtime_check(item['chat_id'])['status'] != 'runtime_unattended':
                deployment = {'status': 'deployment_runtime_blocked'}
            else:
                deployment = runner(marker, DATABASE_PATH, CONFIG_PATH, PROJECT_ROOT,
                                    recover=recover_deployment)
            status = deployment['status']
            if status == 'deployment_busy' and 'attempt_id' not in deployment:
                # Another work item owns the target. Its release can be observed
                # by this launcher without consuming a ledger attempt.
                remaining = (deadline - utc_now()).total_seconds()
                if remaining > 0:
                    time.sleep(min(2, remaining))
            elif status not in ('attempt_finished', 'repair_advanced', 'deployment_not_pending'):
                return {**last_turn, 'status': 'worker_deployment_blocked', release_key: deployment}
            else:
                last_deployment = deployment
            pending = peek()
            continue
        if state != 'deployment_not_pending':
            return {**last_turn, 'status': 'worker_deployment_blocked',
                    release_key: {'status': state}}
        was_waiting = frontend_waiting(marker, DATABASE_PATH, CONFIG_PATH)
        last_turn = worker_result(*launch_chat(item))
        if last_deployment is not None:
            last_turn[release_key] = last_deployment
        if last_material is not None:
            last_turn['material_sync'] = last_material
        if last_git is not None:
            last_turn['development_git'] = last_git
        if last_e2e is not None:
            last_turn['frontend_e2e'] = last_e2e
        if last_turn['status'] != 'worker_stopped':
            return last_turn
        pending = peek()
        if (pending_frontend_e2e(marker, DATABASE_PATH, CONFIG_PATH)['status'] == 'host_e2e_pending'
                or frontend_queue_owner(marker, DATABASE_PATH, CONFIG_PATH) is not None
                or (not was_waiting and frontend_waiting(marker, DATABASE_PATH, CONFIG_PATH))):
            continue
        if pending_material_sync(marker, DATABASE_PATH, CONFIG_PATH)['status'] == 'material_sync_pending':
            continue
        if pending_development_git(marker, DATABASE_PATH, CONFIG_PATH)['status'] == 'host_git_pending':
            continue
        if pending['status'] == 'deployment_not_pending':
            return last_turn
    return {**last_turn, 'status': 'worker_deployment_blocked',
            release_key: {'status': 'deployment_worker_deadline'}}


def run(operation, marker, cli):
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
    lock = nullcontext(True) if operation == 'probe' else file_lock(output / 'worker.lock')
    with lock as acquired:
        if not acquired:
            return {'status': 'worker_busy'}
        if operation == 'run' and not previous_turn_stopped(output):
            return {'status': 'worker_stop_unverified', 'evidence': str(output)}
        if item is not None:
            item = read_item(marker)
            if operation == 'prepare' and (item['state'] != 'reserved' or item['chat_id']):
                raise ValueError('Preparing requires a reserved and unbound work item')
            if operation == 'run' and (item['state'] not in ('bound', 'running', 'closing')
                                       or not item['chat_id']):
                raise ValueError('Running requires an active ledger binding')
        if operation == 'prepare':
            run_id = UUID(item['request_id']).hex
            for directory in sorted(output.glob('*/prepare')):
                existing = read_prepared_turn(directory)
                if directory.parent.name == run_id:
                    return existing
                # A chat (or an indeterminate launch) for a different request
                # cannot bind this request, nor safely authorize a fresh chat.
                with closing(sqlite3.connect(DATABASE_PATH.as_uri() + '?mode=ro', uri=True)) as db:
                    prior = db.execute('SELECT state FROM dispatches WHERE request_id=? AND item_seq=?',
                                       (directory.parent.name, item['seq'])).fetchone()
                if existing.get('chat_id') or not prior or prior[0] != 'reconciled':
                    return {'status': 'worker_prepare_uncertain', 'evidence': str(directory)}
        if item is not None and (operation == 'prepare' or (operation == 'run'
                and item['state'] == 'bound' and not item['stage'])):
            preflight = run_git_preflight(CONFIG_PATH, DATABASE_PATH, PROJECT_ROOT, item)
            if preflight['status'] not in ('git_preflight_passed', 'git_preflight_skipped'):
                config = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
                released = release_preflight_reservation(DATABASE_PATH, config, item, preflight)
                return {'status': 'worker_git_preflight_blocked', 'git_preflight': preflight,
                        'reservation': released}
        deadline = utc_now() + (timedelta(hours=72) if operation == 'run' else timedelta(minutes=4))
        if operation == 'run':
            return dispatch_work_item(
                marker, lambda current: launch_turn(cli, operation, current, output, deadline), deadline,
                recover_deployment=True,
                recover_material=lambda current: previous_turn_stopped(output),
                recover_git=lambda current: previous_turn_stopped(output),
                recover_e2e=lambda current: previous_turn_stopped(output))
        receipt, _ = launch_turn(cli, operation, item, output, deadline)
        if operation == 'prepare':
            return prepared_result(receipt)
        return {'status': 'probe_passed' if probe_passed(receipt) else 'probe_failed',
                'receipt': receipt['receipt_path']}


def prepared_result(receipt):
    session = receipt.get('stop_evidence', {}).get('codex_session', {})
    thread_id = session.get('thread_id')
    ready = (receipt['outcome'] == 'success' and receipt['stop_confirmed'] and thread_id
             and session.get('last_turn_event') == 'turn.completed'
             and runtime_check(thread_id)['status'] == 'runtime_unattended')
    return {'status': 'worker_ready' if ready else 'worker_prepare_uncertain', 'chat_id': thread_id,
            'receipt': receipt['receipt_path'], 'stop_confirmed': receipt['stop_confirmed']}


def read_prepared_turn(directory):
    # A supervisor may have lost its final disk write after receiving all CLI
    # events. Reconstruct this request's receipt only after proving it stopped.
    try:
        previous = directory / 'result.json'
        if previous.is_file():
            return prepared_result(json.loads(previous.read_text(encoding='utf-8')))
        launch = directory / 'launch.json'
        stopped = recover_job_stop(json.loads(launch.read_text(encoding='utf-8')))
        if stopped:
            session = parse_session(directory / 'stdout.log')
            return prepared_result({'outcome': 'success' if session.get('last_turn_event') == 'turn.completed' else 'failed',
                                    'stop_confirmed': True, 'receipt_path': str(launch),
                                    'stop_evidence': {**stopped, 'codex_session': session}})
    except (AttributeError, KeyError, TypeError, ValueError, OSError):
        pass
    return {'status': 'worker_prepare_uncertain', 'evidence': str(directory)}


def run_e2e_preflight(profile):
    with frontend_e2e_lock(DATABASE_PATH) as acquired:
        if not acquired or frontend_delivery_in_use(DATABASE_PATH):
            return {'status': 'e2e_preflight_busy', 'check': {'stage': 'runner-lock'}}
        return run_locked_e2e_preflight(profile)


def run_locked_e2e_preflight(profile):
    attempt_id = uuid4().hex
    phase = 'e2e-preflight'
    output = DATABASE_PATH.parent / 'worker-probes'
    startup_log = output / attempt_id / phase / 'frontend.log'
    command = [sys.executable, '-B', str(Path(__file__).with_name('agentlimb_e2e.py')),
               '--startup-log', str(startup_log)]
    if profile:
        command.extend(['--profile', str(profile.resolve(strict=True))])
    output.mkdir(parents=True, exist_ok=True)
    with file_lock(output / 'e2e-preflight.lock') as acquired:
        if not acquired:
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
    parser.add_argument('operation', choices=('prepare', 'run', 'probe', 'e2e-preflight', 'git-preflight'))
    parser.add_argument('--marker')
    parser.add_argument('--profile', type=Path)
    args = parser.parse_args()
    if args.profile and args.operation != 'e2e-preflight':
        parser.error('--profile is only supported for e2e-preflight')
    try:
        if args.operation == 'git-preflight':
            result = run_git_preflight(CONFIG_PATH, DATABASE_PATH, PROJECT_ROOT)
        elif args.operation == 'e2e-preflight':
            result = run_e2e_preflight(args.profile)
        else:
            result = run(args.operation, args.marker, resolve_cli())
    except (OSError, TypeError, ValueError, sqlite3.Error) as error:
        result = {'status': 'worker_error', 'reason': str(error)}
    print(json.dumps(result, ensure_ascii=True))
    return int(result['status'] in ('worker_error', 'worker_deployment_blocked', 'probe_failed', 'worker_stop_unverified',
                                    'e2e_preflight_blocked', 'e2e_preflight_busy',
                                    'worker_git_preflight_blocked', 'git_preflight_failed', 'git_preflight_busy',
                                    'worker_material_stop_unverified', 'worker_material_blocked',
                                    'worker_host_git_stop_unverified', 'worker_host_git_blocked',
                                    'worker_host_e2e_stop_unverified', 'worker_host_e2e_blocked'))


if __name__ == '__main__':
    sys.exit(main())
