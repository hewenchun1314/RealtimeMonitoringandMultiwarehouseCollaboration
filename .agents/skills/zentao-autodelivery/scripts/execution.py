"""Work-chat binding, attempts, stage progression and closeout."""

import json
from datetime import timedelta
from uuid import uuid4

from branch_check import branch_context, planned_applications, valid_evidence
from desktop_runtime import runtime_check
from ledger import STAGES, STAGE_NAMES, encode, item_identity, message, require_text, stamp, workflow_stages


REPAIR_PHASES = {
    'code_review': ('repair_coding', 'repair_unit_test', 'verify'),
    'e2e': ('repair_coding', 'repair_unit_test', 'repair_review', 'verify'),
}
PHASE_TIMERS = {'repair_coding': 'coding', 'repair_unit_test': 'unit_test', 'repair_review': 'code_review'}
DEFAULT_INTEGRATION_TIMEOUT_MINUTES = 45


def dispatch(store, item, data):
    request_id = require_text(data.get('request_id'), 'request_id')
    return store.conn.execute('''SELECT * FROM dispatches WHERE item_seq=? AND request_id=?''',
                              (item['seq'], request_id)).fetchone()


def set_dispatch_state(store, lease, state):
    store.conn.execute('UPDATE dispatches SET state=? WHERE request_id=?', (state, lease['request_id']))


def bind(store, item, data):
    lease = dispatch(store, item, data)
    chat_id = require_text(data.get('chat_id'), 'chat_id')
    desktop_id = require_text(data.get('desktop_project_id'), 'desktop_project_id')
    if not lease or item['request_id'] != lease['request_id']:
        return message('stale_dispatch')
    if lease['desktop_project_id'] != desktop_id or (item['chat_id'] and item['chat_id'] != chat_id):
        return message('binding_mismatch')
    if item['state'] == 'dispatch_uncertain' and lease['state'] == 'uncertain':
        state = 'queued' if item['eligible'] else 'ineligible'
        set_dispatch_state(store, lease, 'reconciled')
    elif item['state'] in ('reserved', 'bound') and lease['state'] == 'reserved':
        if store.time >= lease['expires_at']:
            return message('dispatch_expired')
        state = 'bound'
    else:
        return message('stale_dispatch')
    store.update_item(item, chat_id=chat_id, desktop_project_id=desktop_id, state=state)
    return message('bound', chat_id=chat_id, item_state=state, expires_at=lease['expires_at'])


def defer_dispatch(store, item, data):
    lease = dispatch(store, item, data)
    if not lease or item['request_id'] != lease['request_id'] or lease['state'] != 'reserved':
        return message('stale_dispatch')
    if data.get('reason') == 'approval_policy' and item['state'] == 'bound':
        return defer_for_runtime(store, item, lease) or message('cannot_defer')
    if item['state'] != 'reserved' or item['chat_id']:
        return message('cannot_defer')
    set_dispatch_state(store, lease, 'uncertain')
    store.update_item(item, state='dispatch_uncertain')
    return message('dispatch_uncertain', marker=item['marker'])


def defer_for_runtime(store, item, lease):
    runtime = runtime_check(item['chat_id'])
    if runtime['status'] == 'runtime_unattended':
        return None
    state = 'queued' if item['eligible'] else 'ineligible'
    set_dispatch_state(store, lease, 'runtime_blocked')
    store.update_item(item, state=state)
    return message('dispatch_runtime_blocked', item_state=state, chat_id=item['chat_id'], runtime=runtime)


def expire_dispatch(store, item, data):
    lease = dispatch(store, item, data)
    if not lease or item['request_id'] != lease['request_id'] or lease['state'] != 'reserved':
        return message('stale_dispatch')
    if store.time < lease['expires_at']:
        return message('not_expired', expires_at=lease['expires_at'])
    if not item['chat_id']:
        return defer_dispatch(store, item, data)
    set_dispatch_state(store, lease, 'expired')
    state = 'queued' if item['eligible'] else 'ineligible'
    store.update_item(item, state=state)
    return message('dispatch_expired', item_state=state, chat_id=item['chat_id'])


def resolve_dispatch(store, item, data):
    lease = dispatch(store, item, data)
    if (not lease or item['request_id'] != lease['request_id'] or lease['state'] != 'uncertain'
            or item['state'] != 'dispatch_uncertain'):
        return message('stale_dispatch')
    if data.get('not_created') is not True or not data.get('evidence'):
        return message('reconciliation_needed')
    set_dispatch_state(store, lease, 'reconciled')
    state = 'queued' if item['eligible'] else 'ineligible'
    store.update_item(item, state=state)
    return message('dispatch_resolved', item_state=state)


def start(store, item, data):
    lease = dispatch(store, item, data)
    chat_id = require_text(data.get('chat_id'), 'chat_id')
    if not lease or item['request_id'] != lease['request_id'] or item['chat_id'] != chat_id:
        return message('stale_dispatch')
    if item['state'] in ('running', 'closing') and lease['state'] == 'started':
        return message('already_started', stage=item['stage'], run_round=item['run_round'])
    if item['state'] != 'bound' or lease['state'] != 'reserved':
        return message('stale_dispatch')
    if store.time >= lease['expires_at']:
        return message('dispatch_expired')
    if not item['eligible']:
        set_dispatch_state(store, lease, 'ineligible')
        store.update_item(item, state='ineligible')
        return message('not_eligible', chat_id=chat_id)
    if store.config.get('runtime', {}).get('require_unattended_approval'):
        blocked = defer_for_runtime(store, item, lease)
        if blocked:
            return blocked
    set_dispatch_state(store, lease, 'started')
    stage = item['stage'] or STAGES[0]
    store.update_item(item, state='running', stage=stage)
    return message('started', stage=stage, run_round=item['run_round'])


def execution_matches(store, item, data):
    lease = dispatch(store, item, data)
    return (lease is not None and lease['state'] == 'started' and item['request_id'] == lease['request_id']
            and item['chat_id'] == data.get('chat_id'))


def active_attempt(store, item):
    return store.conn.execute('SELECT * FROM attempts WHERE item_seq=? AND outcome IS NULL',
                              (item['seq'],)).fetchone()


def phase_deadline(store, item, phase):
    timeouts = json.loads(item['config_snapshot'])['development']['step_timeout_minutes']
    timeout_key = PHASE_TIMERS.get(phase, item['stage'])
    minutes = (timeouts.get(timeout_key, DEFAULT_INTEGRATION_TIMEOUT_MINUTES)
               if timeout_key == 'integration' else timeouts[timeout_key])
    return stamp(store.now + timedelta(minutes=minutes))


def attempt_start(store, item, data):
    if not execution_matches(store, item, data) or item['state'] != 'running':
        return message('stale_execution')
    if data.get('stage') != item['stage']:
        return message('stage_mismatch', stage=item['stage'])
    preparation = branch_context(store, item) if item['stage'] == 'branch_check' else {}
    active = active_attempt(store, item)
    if active:
        return message('attempt_active', attempt_id=active['attempt_id'], deadline_at=active['deadline_at'],
                       phase=active['phase'], retry_number=active['retry_number'],
                       phase_started_at=active['phase_started_at'], **preparation)
    development = json.loads(item['config_snapshot'])['development']
    previous = store.conn.execute('''SELECT run_round,retry_number,result FROM attempts
                                     WHERE item_seq=? AND stage=?
                                     ORDER BY run_round DESC,retry_number DESC LIMIT 1''',
                                  (item['seq'], item['stage'])).fetchone()
    retries = previous['retry_number'] + 1 if previous and previous['run_round'] == item['run_round'] else 0
    if retries > development['max_retries_per_step']:
        return message('retry_exhausted')
    needs_repair = (item['stage'] in REPAIR_PHASES and previous
                    and json.loads(previous['result']).get('repair_required'))
    phase = 'repair_coding' if needs_repair else 'verify'
    deadline = phase_deadline(store, item, phase)
    attempt_id = uuid4().hex
    store.conn.execute('''INSERT INTO attempts(attempt_id,item_seq,run_round,stage,retry_number,started_at,
                                              deadline_at,phase,phase_started_at)
                          VALUES(?,?,?,?,?,?,?,?,?)''', (attempt_id, item['seq'], item['run_round'], item['stage'],
                                                         retries, store.time, deadline, phase, store.time))
    return message('attempt_started', attempt_id=attempt_id, stage=item['stage'], retry_number=retries,
                   started_at=store.time, phase_started_at=store.time, phase=phase, deadline_at=deadline,
                   **preparation)


def repair_advance(store, item, data):
    if not execution_matches(store, item, data) or item['state'] != 'running':
        return message('stale_execution')
    trial = active_attempt(store, item)
    if not trial or trial['attempt_id'] != data.get('attempt_id'):
        return message('stale_attempt')
    phase = trial['phase']
    if phase != data.get('phase') or phase not in PHASE_TIMERS:
        return message('phase_mismatch', phase=phase)
    if store.time >= trial['deadline_at']:
        return message('deadline_elapsed', deadline_at=trial['deadline_at'])
    summary = require_text(data.get('summary'), 'summary')
    evidence = data.get('evidence')
    if not evidence:
        raise ValueError('Repair phase completion requires evidence')
    completed = json.loads(trial['phase_history'])
    completed.append({'phase': phase, 'summary': summary, 'evidence': evidence,
                      'started_at': trial['phase_started_at'], 'ended_at': store.time})
    phases = REPAIR_PHASES[item['stage']]
    next_phase = phases[phases.index(phase) + 1]
    deadline = phase_deadline(store, item, next_phase)
    store.conn.execute('''UPDATE attempts SET phase=?,phase_started_at=?,phase_history=?,deadline_at=?
                          WHERE attempt_id=?''', (next_phase, store.time, encode(completed), deadline,
                                                  trial['attempt_id']))
    return message('repair_advanced', attempt_id=trial['attempt_id'], phase=next_phase,
                   phase_started_at=store.time, deadline_at=deadline, retry_number=trial['retry_number'])


def enqueue(store, item, operation_id, operation_type, payload):
    store.conn.execute('''INSERT INTO writebacks(operation_id,item_seq,run_round,operation_type,payload)
                          VALUES(?,?,?,?,?)''', (operation_id, item['seq'], item['run_round'], operation_type,
                                                 encode(payload)))


def enqueue_note(store, item, label, text):
    operation_id = f'{item["marker"]}/{item["run_round"]}/{label}'
    enqueue(store, item, operation_id, 'comment', {'text': f'{text} [{operation_id}]'})
    return operation_id


def attempt_finish(store, item, data, branches_verified=False):
    if not execution_matches(store, item, data):
        return message('stale_execution')
    attempt_id = require_text(data.get('attempt_id'), 'attempt_id')
    trial = active_attempt(store, item)
    if (not trial or trial['attempt_id'] != attempt_id or item['state'] != 'running'
            or trial['stage'] != item['stage'] or trial['run_round'] != item['run_round']):
        return message('stale_attempt')
    outcome = data.get('outcome')
    if outcome not in ('success', 'failed', 'timed_out'):
        raise ValueError('outcome must be success, failed or timed_out')
    phase = trial['phase']
    completed_phases = json.loads(trial['phase_history'])
    if data.get('phase', None if completed_phases else 'verify') != phase:
        return message('phase_mismatch', phase=phase)
    if phase != 'verify' and outcome == 'success':
        return message('phase_incomplete', phase=phase)
    summary = require_text(data.get('summary'), 'summary')
    if 'retryable' in data and type(data['retryable']) is not bool:
        raise ValueError('retryable must be a boolean')
    repair_required = data.get('repair_required', False)
    if type(repair_required) is not bool or (repair_required and (
            outcome != 'failed' or item['stage'] not in REPAIR_PHASES or phase != 'verify')):
        raise ValueError('repair_required applies to a failed Code Review or E2E verification')
    deadline = trial['deadline_at']
    if outcome == 'timed_out' and deadline and store.time < deadline:
        return message('deadline_not_reached', deadline_at=deadline)
    if deadline and store.time >= deadline and outcome != 'timed_out':
        return message('deadline_elapsed', deadline_at=deadline)
    if outcome == 'timed_out' and (data.get('stop_confirmed') is not True or not data.get('stop_evidence')):
        return message('stop_unverified')
    development = json.loads(item['config_snapshot'])['development']
    stages = workflow_stages(development)
    finished = outcome == 'success'
    stage = item['stage']
    evidence = data.get('evidence')
    missing_file = (item['kind'] == 'story' and stage == 'material_check' and outcome == 'failed'
                    and isinstance(evidence, dict) and evidence.get('material_file_status') == 'missing')
    retry = (not finished and not missing_file and data.get('retryable', True)
             and trial['retry_number'] < development['max_retries_per_step'])
    if finished and stage == 'plan' and 'branch_check' in stages and not planned_applications(evidence):
        return message('applications_required')
    if finished and stage == 'branch_check':
        if not valid_evidence(branch_context(store, item), evidence):
            return message('branch_evidence_required')
        if not branches_verified:
            return message('branch_worktree_unverified')
    number = stages.index(stage)
    next_stage = stages[number + 1] if not missing_file and number + 1 < len(stages) else None
    result = {'summary': summary, 'evidence': evidence, 'stop_evidence': data.get('stop_evidence'),
              'terminal_step': not retry, 'phase': phase, 'repair_phases': completed_phases,
              'repair_required': repair_required or phase != 'verify'}
    if not finished and not retry:
        result['completion'] = '待补材料' if missing_file else data.get('completion', '阻断')
        if result['completion'] not in ('待补材料', '待验证', '阻断'):
            raise ValueError('Invalid completion for a blocked step')
    store.conn.execute('UPDATE attempts SET ended_at=?,outcome=?,result=? WHERE attempt_id=?',
                       (store.time, outcome, encode(result), attempt_id))

    operation_id = None
    if retry:
        disposition = 'retry'
        next_stage = stage
    else:
        result_label = {'success': '成功', 'failed': '失败', 'timed_out': '超时'}[outcome]
        stage_started = store.conn.execute('''SELECT MIN(started_at) FROM attempts
                                              WHERE item_seq=? AND run_round=? AND stage=?''',
                                           (item['seq'], item['run_round'], stage)).fetchone()[0]
        note = (f'{STAGE_NAMES[stage]} {stage_started} 开始，{store.time} 结束，'
                f'{result_label}（重试 {trial["retry_number"]} 次）。{summary}')
        operation_id = enqueue_note(store, item, stage, note)
        if next_stage:
            disposition = 'next'
            store.update_item(item, stage=next_stage)
        else:
            conclusion = next((step['completion'] for step in terminal_steps(store, item)
                               if step['outcome'] == 'failed'), '已交付')
            disposition = 'close' if conclusion == '已交付' else 'blocked'
            store.update_item(item, state='closing', conclusion=conclusion)
    return message('attempt_finished', attempt_id=attempt_id, step=stage, next_stage=next_stage, outcome=outcome,
                   attempts_used=trial['retry_number'] + 1, disposition=disposition, operation_id=operation_id)


def terminal_steps(store, item):
    rows = store.conn.execute('''SELECT * FROM attempts WHERE item_seq=? AND outcome IS NOT NULL
                                  ORDER BY run_round DESC, retry_number DESC''', (item['seq'],))
    results = {}
    missing_material_closeout = (item['kind'] == 'story' and item['stage'] == 'material_check'
                                 and item['conclusion'] == '待补材料')
    for row in rows:
        if missing_material_closeout and row['run_round'] != item['run_round']:
            continue
        if row['stage'] in results:
            continue
        detail = json.loads(row['result'])
        if detail['terminal_step']:
            results[row['stage']] = {'stage': row['stage'], 'outcome': 'success' if row['outcome'] == 'success' else 'failed',
                                     'summary': detail['summary'], 'evidence': detail['evidence'],
                                     'started_at': row['started_at'], 'ended_at': row['ended_at'],
                                     'retries': row['retry_number']}
            if row['outcome'] != 'success':
                results[row['stage']]['completion'] = detail.get('completion', '阻断')
    steps = []
    skip_reason = ('因用户要求主动停跑而跳过' if item['conclusion'] == '主动停跑'
                   else f'因 {item["stage"]} 阻断跳过')
    if missing_material_closeout:
        skip_reason = '因需求评审确认缺少需求文件而跳过'
    development = json.loads(item['config_snapshot'])['development']
    for stage in workflow_stages(development):
        step = results.get(stage)
        if step is None:
            step = {'stage': stage, 'outcome': 'skipped', 'summary': skip_reason,
                    'ended_at': store.time}
        steps.append(step)
    return steps


def complete(store, item, data):
    if not execution_matches(store, item, data) or item['state'] != 'closing':
        return message('stale_execution')
    if active_attempt(store, item):
        return message('attempt_active')
    steps = terminal_steps(store, item)
    conclusion = item['conclusion']
    if conclusion == '已交付' and any(step['outcome'] != 'success' for step in steps):
        raise ValueError('Successful closeout requires every step to pass')
    blockers_path = data.get('blockers_path')
    blockers_location = data.get('blockers_location')
    if conclusion not in ('已交付', '主动停跑') or blockers_path is not None:
        blockers_path = require_text(blockers_path, 'blockers_path')
    if blockers_location is not None:
        blockers_location = require_text(blockers_location, 'blockers_location')
    for step in steps:
        if step['outcome'] == 'skipped':
            name = STAGE_NAMES[step['stage']]
            enqueue_note(store, item, step['stage'], f'{name} {store.time} {step["summary"]}。')
    statuses = {'success': '成功', 'failed': '失败', 'skipped': '跳过'}
    points = [f'{STAGE_NAMES[step["stage"]]}{statuses[step["outcome"]]}（{step["summary"]}）' for step in steps]
    note = '，'.join(points) + f'。本单收尾为「{conclusion}」。'
    attachment_id = None
    if blockers_path:
        attachment_id = f'{item["marker"]}/{item["run_round"]}/blockers'
        enqueue(store, item, attachment_id, 'attachment', {'path': blockers_path, 'location': blockers_location,
                                                          'name': 'blockers.md'})
        if blockers_location:
            note += f'阻断记录位置：{blockers_location}。'
        else:
            note += '完整阻断记录 blockers.md 已保存在本机工单目录。'
    operation_id = enqueue_note(store, item, 'final', note)
    store.update_item(item, state='completed')
    set_dispatch_state(store, dispatch(store, item, data), 'completed')
    return message('completed', conclusion=conclusion, steps=steps, operation_id=operation_id,
                   attachment_id=attachment_id, item=item_identity(item))


def stop(store, item, data):
    if item['state'] != 'running' or not execution_matches(store, item, data):
        return message('stale_execution')
    if active_attempt(store, item):
        return message('attempt_active')
    evidence = data.get('stop_evidence')
    if (not isinstance(evidence, dict) or evidence.get('chat_status') != 'idle'
            or type(evidence.get('owned_processes_active_after_stop')) is not int
            or evidence['owned_processes_active_after_stop'] != 0):
        return message('stop_unverified')
    store.update_item(item, state='closing', conclusion='主动停跑')
    return complete(store, store.find_item(item_identity(item)), data)


def recover(store, item, data):
    recovery_id = require_text(data.get('recovery_id'), 'recovery_id')
    evidence = data.get('evidence')
    if not evidence:
        raise ValueError('Recovery requires new evidence')
    evidence = encode(evidence)
    if store.conn.execute('''SELECT 1 FROM recoveries WHERE item_seq=? AND (recovery_id=? OR evidence=?)''',
                          (item['seq'], recovery_id, evidence)).fetchone():
        return message('recovery_seen')
    if item['state'] != 'completed' or item['conclusion'] in ('已交付', '主动停跑'):
        return message('not_recoverable')
    store.conn.execute('INSERT INTO recoveries(item_seq,recovery_id,evidence) VALUES(?,?,?)',
                       (item['seq'], recovery_id, evidence))
    state = 'queued' if item['eligible'] else 'ineligible'
    stage = next((step['stage'] for step in terminal_steps(store, item)
                  if step['outcome'] == 'failed'), item['stage'])
    store.update_item(item, state=state, stage=stage, conclusion=None, run_round=item['run_round'] + 1)
    return message('recovered', item_state=state, stage=stage, chat_id=item['chat_id'],
                   run_round=item['run_round'] + 1)
