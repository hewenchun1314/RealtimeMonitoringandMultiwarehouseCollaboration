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
import time
from uuid import UUID, uuid4

from backend_deploy_runner import deployment_action, run_deployment
from backend_dependencies import worker_roots
from desktop_runtime import runtime_check
from deploy_state import FRONTEND_DEPLOY_STAGE, GIT_PUSH_STAGES
from dingtalk_notify import dispatch as dispatch_dingtalk, enabled as dingtalk_enabled
from ledger import Ledger, encode, item_identity, transaction, validate_config
from frontend_release_runner import run_frontend_release
from frontend_e2e_runner import (frontend_delivery_in_use, frontend_e2e_lock, frontend_queue_owner,
                                 frontend_waiting, pending_frontend_e2e, run_frontend_e2e)
from git_delivery_runner import run_git_delivery
from development_git_runner import pending_development_git, run_development_git
from stage_runner import recover_job_stop, run_stage, utc_now
from status_board import publish_status
from story_review_remote import StoryReviewRemote
from story_review_sync import rearm_uncertain, release_handoff, resume_ready, sync_review
from task_remote import RemoteRejected
from task_sync import enabled as task_sync_enabled
from workflow import CONFIG_PATH, DATABASE_PATH, PROJECT_ROOT
from material_sync_runner import (pending_material_sync, run_material_sync, run_git_preflight,
                                  release_preflight_reservation)


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
    source_context = ''
    admitted = json.loads(item['config_snapshot'] or '{}')
    if admitted.get('zentao', {}).get('scope_mode') == 'project_or_product':
        with closing(sqlite3.connect(DATABASE_PATH.as_uri() + '?mode=ro', uri=True)) as conn:
            source = conn.execute('SELECT source FROM work_item_sources WHERE item_seq=?', (item['seq'],)).fetchone()
        source_context = ('本单按“所属项目或所属产品”接收，绑定中的 project_id 是自动化队列项目，'
                          '不代表原单必须关联此项目。原始归属：' + (source[0] if source else '待复核') + '。'
                          '按工单快照中 product_id 或实际项目复核原单；禁止改写原单项目来迁就账本。')
    return (
        f'从项目根目录读取 .agents/skills/zentao-autodelivery/SKILL.md，并按照其中的工单执行入口执行。'
        f'这是经授权的无人值守工单 {item["marker"]}。原账本绑定为 {json.dumps(binding)}。'
        f'{source_context}'
        '先用 record runtime_check 核对本轮策略，并按实际账本状态领取或续接。'
        '恢复轮次读取本工单 recoveries 的最新记录及其中的本地证据文件，落实已核验的受阻范围。'
        '禁止申请临时权限、弹审批、提问或等待人工确认，也不要开启新的桌面聊天。'
        '每次阶段尝试使用账本截止时间及阶段运行器。'
        '材料评审返回 material_sync_required 时，先按步骤指引运行 record material_sync，'
        '若返回 material_sync_worker_required，立即结束本轮，交 Windows 启动器在宿主同步；'
        '禁止在沙箱里自行 fetch、探测或转存 Git 凭据。恢复同一会话后读取原尝试结果，'
        '取得 material_synced 回执后才派发需求评审；同步失败按原尝试登记。'
        '前后端 branch_prepare、candidate_commit、integration_merge 只登记固定宿主 Git 请求。'
        '返回 host_git_worker_required 时立即结束当前轮次；Windows 启动器确认主进程及任务同步进程均已停止后执行，'
        '随后续接原聊天。恢复后用相同 event_id 和原提交数据读回，或用 host_git_status 按 operation_id 查询。'
        '新路线前端正式验收取得 host_e2e 后，在回执指定的工单相对路径写 host-request.json，'
        '包含本次 attempt_id、工单目录相对路径 profile 与 scenario（均为 JSON）及 base_url，并结束本轮。'
        '宿主执行后从同一目录的 agentlimb.json 读取实际结果，再登记账本原尝试。'
        '若返回 frontend_delivery_busy，结束本轮等待当前交付单完成。'
        '沙箱内只编辑、构建、测试及只读 Git 核验；禁止自行 fetch、worktree add、commit、checkout 或 merge，禁止跳过原有 hooks。'
        '若返回 story_review_required，按步骤指引取得 review-target，交给需求评审 Agent 随评审结果提交。'
        '账本返回 disposition=review_wait 时立即结束本轮，Windows 启动器负责禅道评审与续接。'
        '若阶段回执带 bug_intake_required，主会话按步骤指引直接核对并登记 Bug 的仓库与验收条件。'
        '按本单快照与账本当前阶段分派评审、拆分和本端开发或测试 Agent，主会话负责分支核验和合回。'
        '把后端阶段回执中的 backend_dependencies 交给对应步骤 Agent。先追踪业务服务自己的接口与实现，'
        '只在本单涉及上游调用时读取对应依赖的契约。read_only 依赖只供查阅。writable 依赖确需修改时，'
        '按同名后端服务登记目标仓库并使用核验过的工单工作树。前端使用已配置业务后端的契约。'
        '若账本进入 backend.push、frontend.push、backend.deploy 或 frontend.deploy（含返工分段），'
        '结束当前轮次，Windows 执行器会接手并续接原会话。'
        '各阶段按 task_scope.active 派发，按编号记录 task_blockers 与证据。'
        '依次处理账本返回的每个阶段。当前阶段缺少必要输入或没有可处理的任务时，'
        '用 attempt_finish 的 outcome=skipped 记录本阶段核对结果并继续；实际运行失败记 failed。'
        '跨端依赖须验证交接；交接受阻时按账本继续独立的前端任务。旧工单沿用原阶段。'
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
    argv = command(cli, operation, item['chat_id'] if item is not None else None,
                   protected_roots=protected, writable_roots=writable)
    prompt_file = output / f'{run_id}.txt'
    prompt_file.write_text(worker_prompt(operation, item), encoding='utf-8')
    launch = (run_id, operation, deadline, PROJECT_ROOT, output, prompt_file, argv)
    # Any development phase can hand off fixed Git work, regardless of optional
    # material/review integration flags. Persist both owned Job identities.
    options = {'recoverable': True} if operation == 'run' else {}
    if operation != 'run' or not task_sync_enabled(item):
        return run_stage(*launch, **options), None
    stop = Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_stage, run_id, 'task-sync', deadline, PROJECT_ROOT, output, None,
                                 sync_command('--marker', item['marker'], '--chat-id', item['chat_id']),
                                 stop_event=stop, **options)
        try:
            receipt = run_stage(*launch, **options)
        finally:
            stop.set()
        return receipt, future.result()


def worker_result(receipt, sync_receipt):
    stopped = receipt['stop_confirmed'] and (sync_receipt is None or sync_receipt['stop_confirmed'])
    result = {'status': 'worker_stopped' if stopped else 'worker_stop_unverified',
              'receipt': receipt['receipt_path'], 'outcome': receipt['outcome']}
    if sync_receipt:
        result.update(task_sync_receipt=sync_receipt['receipt_path'], task_sync_outcome=sync_receipt['outcome'])
    return result


def review_handoff_stopped(output, item):
    turns = list(output.glob('*/run'))
    if not turns:
        return False
    turn = max(turns, key=lambda path: path.stat().st_mtime_ns)
    phases = [turn, turn.parent / 'task-sync'] if task_sync_enabled(item) else [turn]
    try:
        for phase in phases:
            receipt = phase / 'result.json'
            if receipt.is_file() and json.loads(receipt.read_text(encoding='utf-8')).get('stop_confirmed') is True:
                continue
            launch = json.loads((phase / 'launch.json').read_text(encoding='utf-8'))
            if not recover_job_stop(launch):
                return False
        return True
    except (OSError, ValueError):
        return False


def dispatch_work_item(marker, launch_chat, deadline=None, *, recover_deployment=False, recover_review=None,
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
        if not item or item['state'] not in ('bound', 'running', 'closing', 'review_handoff', 'review_wait', 'review_ready'):
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
        if item['state'] == 'review_handoff':
            if last_turn.get('status') != 'worker_stopped' and not (recover_review and recover_review(item)):
                return {**last_turn, 'status': 'worker_review_stop_unverified'}
            config = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
            release_handoff(DATABASE_PATH, config, item_identity(item))
            continue
        if item['state'] in ('review_wait', 'review_ready'):
            if runtime_check(item['chat_id'])['status'] != 'runtime_unattended':
                return {**last_turn, 'status': 'worker_review_wait', 'story_review': {'status': 'review_runtime_blocked'}}
            config = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
            review = sync_item_review(config, item)
            if review['status'] == 'review_ready':
                review = resume_ready(DATABASE_PATH, config, item_identity(item))
            if review['status'] != 'review_resumed':
                return {**last_turn, 'status': 'worker_review_wait', 'story_review': review}
            pending = peek()
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
        current = read_item(marker)
        if current and current['state'] in ('review_handoff', 'review_wait', 'review_ready'):
            continue
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
            if operation == 'run' and (item['state'] not in ('bound', 'running', 'closing', 'review_handoff', 'review_wait', 'review_ready')
                                       or not item['chat_id']):
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
        if item is not None and (operation == 'prepare' or (operation == 'run'
                and item['state'] == 'bound' and not item['stage'])):
            preflight = run_git_preflight(CONFIG_PATH, DATABASE_PATH, PROJECT_ROOT, item)
            if preflight['status'] not in ('git_preflight_passed', 'git_preflight_skipped'):
                config = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
                released = release_preflight_reservation(DATABASE_PATH, config, item, preflight)
                return {'status': 'worker_git_preflight_blocked', 'git_preflight': preflight,
                        'reservation': released}
        os.environ.pop('CODEX_THREAD_ID', None)
        deadline = utc_now() + (timedelta(hours=72) if operation == 'run' else timedelta(minutes=4))
        if operation == 'run':
            return dispatch_work_item(
                marker, lambda current: launch_turn(cli, operation, current, output, deadline), deadline,
                recover_deployment=True, recover_review=lambda current: review_handoff_stopped(output, current),
                recover_material=lambda current: review_handoff_stopped(output, current),
                recover_git=lambda current: review_handoff_stopped(output, current),
                recover_e2e=lambda current: review_handoff_stopped(output, current))
        receipt, _ = launch_turn(cli, operation, item, output, deadline)
        if operation == 'prepare':
            return prepared_result(receipt)
        return {'status': 'probe_passed' if probe_passed(receipt) else 'probe_failed',
                'receipt': receipt['receipt_path']}


def sync_command(*args):
    return [sys.executable, '-B', str(Path(__file__).with_name('task_sync_runner.py')), *args]


def review_remote(config, item):
    admitted = json.loads(item['config_snapshot'])
    for key in ('base_url', 'credential_ref', 'project_name'):
        if config['zentao'].get(key) != admitted['zentao'].get(key):
            raise RemoteRejected('当前禅道配置与工单快照不匹配')
    return StoryReviewRemote(admitted)


def read_review_target(marker):
    item = read_item(marker)
    if (not item or item['kind'] != 'story' or item['state'] != 'running' or item['stage'] != 'material_check'
            or not json.loads(item['config_snapshot']).get('runtime', {}).get('story_review_sync')
            or runtime_check(item['chat_id'])['status'] != 'runtime_unattended'):
        return {'status': 'review_target_inactive'}
    config = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
    with transaction(DATABASE_PATH) as conn:
        attempt = review_target_attempt(conn, config, item)
        if not attempt:
            return {'status': 'review_target_inactive'}
        if attempt['target']:
            return {'status': 'review_target_verified', 'target': json.loads(attempt['target'])}
    try:
        target = review_remote(config, item).observe(item)['target']
    except RemoteRejected as error:
        return {'status': 'review_target_failed', 'reason': str(error)}
    if target['status'] not in ('active', 'reviewing'):
        return {'status': 'review_target_failed', 'reason': '禅道需求当前未处于接单所需的评审中或激活状态'}
    with transaction(DATABASE_PATH) as conn:
        current = review_target_attempt(conn, config, item)
        if not current or current['attempt_id'] != attempt['attempt_id']:
            return {'status': 'review_target_inactive'}
        if current['target']:
            target = json.loads(current['target'])
        else:
            conn.execute('INSERT INTO story_review_targets(attempt_id,target) VALUES (?,?)',
                         (attempt['attempt_id'], encode(target)))
        return {'status': 'review_target_verified', 'target': target}


def review_target_attempt(conn, config, item):
    now = Ledger(conn, config, utc_now()).time
    return conn.execute('''SELECT a.attempt_id,t.target FROM attempts a
                           JOIN work_items w ON w.seq=a.item_seq AND w.run_round=a.run_round
                           LEFT JOIN story_review_targets t ON t.attempt_id=a.attempt_id
                           WHERE a.item_seq=? AND a.stage='material_check' AND a.outcome IS NULL
                           AND a.deadline_at>? AND w.request_id=? AND w.state='running' AND w.stage=a.stage''',
                        (item['seq'], now, item['request_id'])).fetchone()


def sync_item_review(config, item):
    try:
        remote = review_remote(config, item)
    except RemoteRejected as error:
        return {'status': 'review_retry', 'reason': str(error)}
    result = sync_review(DATABASE_PATH, config, remote, item_identity(item))
    try:
        publish_status(DATABASE_PATH, config)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f'Status page refresh failed: {error}', file=sys.stderr)
    return result


def reconcile_review(marker, prior_ended):
    if not prior_ended:
        return {'status': 'review_prior_request_unverified'}
    item = read_item(marker)
    if not item or item['state'] != 'review_wait':
        return {'status': 'review_not_rearmable'}
    config = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
    remote = review_remote(config, item)
    return rearm_uncertain(DATABASE_PATH, config, remote, item_identity(item), prior_ended=prior_ended)


def sync_monitor_reviews(config):
    if not DATABASE_PATH.is_file():
        return {'checked': [], 'ready': []}
    zentao = config['zentao']
    scope = (zentao['base_url'].rstrip('/'), zentao['project_name'])
    with closing(sqlite3.connect(DATABASE_PATH.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        projects = conn.execute('SELECT project_id FROM scans WHERE instance=? AND project_name=?', scope).fetchall()
        if len(projects) != 1:
            return {'checked': [], 'ready': []}
        items = conn.execute('''SELECT w.* FROM work_items w JOIN writebacks b ON b.item_seq=w.seq
                                AND b.run_round=w.run_round AND b.operation_type='story_review'
                                WHERE w.instance=? AND w.project_id=? AND w.state='review_wait'
                                ORDER BY COALESCE(json_extract(b.receipt,'$.checked_at'), ''), w.seq LIMIT 4''',
                             (scope[0], projects[0]['project_id'])).fetchall()
    checked = [{'marker': item['marker'], **sync_item_review(config, item)} for item in items]
    with closing(sqlite3.connect(DATABASE_PATH.as_uri() + '?mode=ro', uri=True)) as conn:
        ready = [row[0] for row in conn.execute('''SELECT marker FROM work_items WHERE instance=? AND project_id=?
                                                   AND state IN ('review_handoff','review_ready') ORDER BY seq''',
                                               (scope[0], projects[0]['project_id']))]
    return {'checked': checked, 'ready': ready}


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
        result['git_preflight'] = run_git_preflight(CONFIG_PATH, DATABASE_PATH, PROJECT_ROOT)
        result['story_reviews'] = sync_monitor_reviews(config)
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
    with frontend_e2e_lock(DATABASE_PATH) as acquired:
        if not acquired or frontend_delivery_in_use(DATABASE_PATH):
            return {'status': 'e2e_preflight_busy', 'check': {'stage': 'runner-lock'}}
        return run_locked_e2e_preflight(profile)


def run_locked_e2e_preflight(profile):
    import msvcrt

    attempt_id = uuid4().hex
    phase = 'e2e-preflight'
    output = DATABASE_PATH.parent / 'worker-probes'
    startup_log = output / attempt_id / phase / 'frontend.log'
    command = [sys.executable, '-B', str(Path(__file__).with_name('agentlimb_e2e.py')),
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
    parser.add_argument('operation', choices=('prepare', 'run', 'probe', 'sync-tasks', 'e2e-preflight',
                                             'review-target', 'review-reconcile', 'git-preflight'))
    parser.add_argument('--marker')
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--prior-ended', action='store_true')
    args = parser.parse_args()
    if args.profile and args.operation != 'e2e-preflight':
        parser.error('--profile is only supported for e2e-preflight')
    if args.prior_ended and args.operation != 'review-reconcile':
        parser.error('--prior-ended is only supported for review-reconcile')
    cli = codex_directory() / 'packages' / 'app-server-daemon' / 'current' / 'bin' / 'codex.exe'
    try:
        if args.operation == 'git-preflight':
            result = run_git_preflight(CONFIG_PATH, DATABASE_PATH, PROJECT_ROOT)
        elif args.operation == 'e2e-preflight':
            result = run_e2e_preflight(args.profile)
        elif args.operation == 'sync-tasks':
            result = run_monitor_sync()
        elif args.operation == 'review-target':
            result = read_review_target(args.marker)
        elif args.operation == 'review-reconcile':
            result = reconcile_review(args.marker, args.prior_ended)
        else:
            result = run(args.operation, args.marker, cli.resolve(strict=True))
    except (OSError, TypeError, ValueError, sqlite3.Error) as error:
        result = {'status': 'worker_error', 'reason': str(error)}
    print(json.dumps(result, ensure_ascii=True))
    return int(result['status'] in ('worker_error', 'worker_deployment_blocked', 'probe_failed', 'task_sync_incomplete',
                                    'e2e_preflight_blocked', 'e2e_preflight_busy', 'worker_review_stop_unverified',
                                    'worker_git_preflight_blocked', 'git_preflight_failed', 'git_preflight_busy',
                                    'worker_material_stop_unverified', 'worker_material_blocked',
                                    'worker_host_git_stop_unverified', 'worker_host_git_blocked',
                                    'worker_host_e2e_stop_unverified', 'worker_host_e2e_blocked'))


if __name__ == '__main__':
    sys.exit(main())
