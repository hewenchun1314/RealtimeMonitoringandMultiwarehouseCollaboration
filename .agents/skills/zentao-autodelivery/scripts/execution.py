"""Work-chat binding, attempts, stage progression and closeout."""

import json
from datetime import timedelta
from uuid import uuid4

from branch_check import branch_context, planned_applications, valid_evidence
from delivery_scope import read_material
from desktop_runtime import runtime_check
from ledger import STAGES, STAGE_NAMES, encode, item_identity, message, require_text, stamp
from route_evidence import delivery_summary, validate_partial_merge, validate_success
from route_execution import (get_split, is_routed, read_history, snapshot, split_result, stage_role,
                             stages_for, v2_repair)


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
    timeouts = snapshot(item)['development']['step_timeout_minutes']
    key = item['stage'] if phase == 'verify' else PHASE_TIMERS.get(phase, phase)
    name = key.rsplit('.', 1)[-1]
    base = {'split': 'plan', 'handoff': 'integration', 'acceptance': 'e2e'}.get(name, name)
    minutes = timeouts.get(key, timeouts.get(name, timeouts.get(base)))
    if minutes is None:
        if base == 'integration':
            minutes = DEFAULT_INTEGRATION_TIMEOUT_MINUTES
        elif base == 'branch_check':
            minutes = 15
        else:
            raise ValueError(f'Missing timeout for {key}')
    return stamp(store.now + timedelta(minutes=minutes))


def attempt_start(store, item, data):
    if not execution_matches(store, item, data) or item['state'] != 'running':
        return message('stale_execution')
    if data.get('stage') != item['stage']:
        return message('stage_mismatch', stage=item['stage'])
    preparation = branch_context(store, item) if item['stage'].rsplit('.', 1)[-1] == 'branch_check' else {}
    routed = is_routed(item)
    if routed:
        preparation['route'] = get_split(store, item)
    active = active_attempt(store, item)
    if active:
        if routed:
            preparation['role'] = stage_role(item['stage'] if active['phase'] == 'verify' else active['phase'])
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
    last_result = json.loads(previous['result']) if previous else {}
    if routed and previous and previous['run_round'] != item['run_round'] and last_result.get('repair_required'):
        entered_round = store.conn.execute('SELECT 1 FROM attempts WHERE item_seq=? AND run_round=? LIMIT 1',
                                           (item['seq'], item['run_round'])).fetchone()
        stage_record = read_history(store, item)[1].get(item['stage'])
        # Direct recovery may repair the same failure. A re-split or any
        # earlier step executed in this round invalidates that old itinerary.
        if entered_round or not stage_record or stage_record['outcome'] != 'failed':
            last_result = {}
    phase = 'verify'
    history = []
    if last_result.get('repair_required'):
        phase = last_result['repair_sequence'][0] if routed else 'repair_coding'
        if routed and last_result['phase'] != 'verify':
            # An environment failure after a merge resumes its handoff, even across
            # recovery rounds, without merging the same candidate a second time.
            history = last_result['repair_phases']
            phase = last_result['phase']
            if previous['run_round'] != item['run_round']:
                for index, receipt in enumerate(history):
                    if validate_success(store, item, receipt['phase'], receipt['evidence']):
                        phase, history = receipt['phase'], history[:index]
                        break
    if routed:
        preparation['role'] = stage_role(item['stage'] if phase == 'verify' else phase)
    deadline = phase_deadline(store, item, phase)
    attempt_id = uuid4().hex
    store.conn.execute('''INSERT INTO attempts(attempt_id,item_seq,run_round,stage,retry_number,started_at,
                                              deadline_at,phase,phase_started_at,phase_history)
                          VALUES(?,?,?,?,?,?,?,?,?,?)''', (attempt_id, item['seq'], item['run_round'], item['stage'],
                                                          retries, store.time, deadline, phase, store.time, encode(history)))
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
    routed = is_routed(item)
    if phase != data.get('phase') or phase == 'verify' or (not routed and phase not in PHASE_TIMERS):
        return message('phase_mismatch', phase=phase)
    if store.time >= trial['deadline_at']:
        return message('deadline_elapsed', deadline_at=trial['deadline_at'])
    summary = require_text(data.get('summary'), 'summary')
    evidence = data.get('evidence')
    if not evidence:
        raise ValueError('Repair phase completion requires evidence')
    completed = json.loads(trial['phase_history'])
    receipt = {'phase': phase, 'summary': summary, 'evidence': evidence,
               'started_at': trial['phase_started_at'], 'ended_at': store.time}
    if routed:
        if phase == 'split':
            try:
                receipt['route'] = split_result(store, item, evidence, revision=True)
            except ValueError as exc:
                return message('evidence_required', reason=str(exc))
            return finish_revision(store, item, trial, receipt, completed)
        else:
            reason = validate_success(store, item, phase, evidence)
            if reason:
                return message('evidence_required', reason=reason)
        phases = prior_repair_sequence(store, item)
    else:
        phases = REPAIR_PHASES[item['stage']]
    completed.append(receipt)
    next_phase = phases[phases.index(phase) + 1]
    deadline = phase_deadline(store, item, next_phase)
    store.conn.execute('''UPDATE attempts SET phase=?,phase_started_at=?,phase_history=?,deadline_at=?
                          WHERE attempt_id=?''', (next_phase, store.time, encode(completed), deadline,
                                                  trial['attempt_id']))
    role = {'role': stage_role(item['stage'] if next_phase == 'verify' else next_phase)} if routed else {}
    return message('repair_advanced', attempt_id=trial['attempt_id'], phase=next_phase,
                   phase_started_at=store.time, deadline_at=deadline, retry_number=trial['retry_number'], **role)


def prior_repair_sequence(store, item):
    previous = store.conn.execute('''SELECT result FROM attempts WHERE item_seq=? AND stage=? AND outcome IS NOT NULL
                                     ORDER BY rowid DESC LIMIT 1''', (item['seq'], item['stage'])).fetchone()
    return json.loads(previous['result'])['repair_sequence']


def revision_count(store, item):
    return store.conn.execute('''SELECT COUNT(*) FROM attempts
                                 WHERE item_seq=? AND json_extract(result,'$.route_revision')=1''',
                              (item['seq'],)).fetchone()[0]


def finish_revision(store, item, trial, receipt, completed):
    if revision_count(store, item) >= snapshot(item)['development']['max_retries_per_step']:
        return message('revision_limit_reached')
    route = receipt['route']
    completed.append(receipt)
    detail = {'summary': receipt['summary'], 'evidence': receipt['evidence'],
              'phase': 'split', 'route_revision': True, 'repair_required': False,
              'repair_phases': completed, 'terminal_step': False}
    store.conn.execute('''UPDATE attempts SET ended_at=?, outcome='success', result=?, phase_history=?
                          WHERE attempt_id=?''',
                       (store.time, encode(detail), encode(completed), trial['attempt_id']))
    enqueue_note(store, item, 'split-revision-' + trial['attempt_id'],
                 f'拆分已修订，本轮旧版本归档，重新核验受影响仓库。{receipt["summary"]}')
    next_round = item['run_round'] + 1
    if route['blockers'] and not any(route['tasks'].values()):
        store.update_item(item, state='closing', conclusion='阻断', stage='split', run_round=next_round)
        next_stage = None
    else:
        next_stage = stages_for(store, item, route)[2]
        store.update_item(item, stage=next_stage, run_round=next_round)
    return message('route_revised', attempt_id=trial['attempt_id'], next_stage=next_stage,
                   run_round=next_round, route=route)


def enqueue(store, item, operation_id, operation_type, payload):
    store.conn.execute('''INSERT INTO writebacks(operation_id,item_seq,run_round,operation_type,payload)
                          VALUES(?,?,?,?,?)''', (operation_id, item['seq'], item['run_round'], operation_type,
                                                 encode(payload)))


def enqueue_note(store, item, label, text):
    operation_id = f'{item["marker"]}/{item["run_round"]}/{label}'
    enqueue(store, item, operation_id, 'comment', {'text': f'{text} [{operation_id}]'})
    return operation_id


def attempt_finish(store, item, data, branches_verified=False, config_directory=None):
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
    routed = is_routed(item)
    stage = item['stage']
    leaf = stage.rsplit('.', 1)[-1]
    if routed:
        can_repair = leaf in ('code_review', 'e2e', 'acceptance', 'integration') or (
            leaf in ('plan', 'design') and data.get('contract_changed') is True)
    else:
        can_repair = stage in REPAIR_PHASES
    if type(repair_required) is not bool or (repair_required and (
            outcome != 'failed' or not can_repair or phase != 'verify')):
        raise ValueError('repair_required needs a failed verification, merge conflict, or design/plan contract change')
    deadline = trial['deadline_at']
    if outcome == 'timed_out' and deadline and store.time < deadline:
        return message('deadline_not_reached', deadline_at=deadline)
    if deadline and store.time >= deadline and outcome != 'timed_out':
        return message('deadline_elapsed', deadline_at=deadline)
    if outcome == 'timed_out' and (data.get('stop_confirmed') is not True or not data.get('stop_evidence')):
        return message('stop_unverified')
    config = snapshot(item)
    development = config['development']
    finished = outcome == 'success'
    evidence = data.get('evidence')
    route = None
    if routed and not finished:
        reason = validate_partial_merge(store, item, stage if phase == 'verify' else phase, evidence)
        if reason:
            return message('evidence_required', reason=reason)
    if finished and routed:
        if stage in ('material_check', 'split'):
            try:
                if stage == 'material_check':
                    material = read_material(config, config_directory, item['kind'], evidence)
                    evidence = {**evidence, 'material': material}
                else:
                    route = split_result(store, item, evidence)
            except (ValueError, OSError) as exc:
                return message('evidence_required', reason=str(exc))
        else:
            reason = validate_success(store, item, stage, evidence)
            if reason:
                return message('evidence_required', reason=reason)
    stages = stages_for(store, item, route)
    missing_file = (item['kind'] == 'story' and stage == 'material_check' and outcome == 'failed'
                    and isinstance(evidence, dict) and evidence.get('material_file_status') == 'missing')
    retry = (not finished and not missing_file and data.get('retryable', True)
             and trial['retry_number'] < development['max_retries_per_step'])
    if routed and repair_required and data.get('contract_changed') and revision_count(store, item) >= development['max_retries_per_step']:
        retry = False
    if finished and leaf == 'plan':
        side = stage.split('.')[0] if routed else 'frontend'
        if stage.replace('plan', 'branch_check') in stages and not planned_applications(evidence, side):
            return message('services_required' if side == 'backend' else 'applications_required')
    if finished and leaf == 'branch_check':
        if not valid_evidence(branch_context(store, item), evidence):
            return message('branch_evidence_required')
        if not branches_verified:
            return message('branch_worktree_unverified')
    number = stages.index(stage)
    no_work = route and route['blockers'] and not any(route['tasks'].values())
    unresolved_contract = routed and not finished and (phase == 'split' or (repair_required and data.get('contract_changed')))
    stop_route = missing_file or no_work or unresolved_contract or (routed and stage in ('split', 'handoff') and not finished)
    next_stage = stages[number + 1] if not stop_route and number + 1 < len(stages) else None
    result = {'summary': summary, 'evidence': evidence, 'stop_evidence': data.get('stop_evidence'),
              'terminal_step': not retry, 'phase': phase, 'repair_phases': completed_phases,
              'repair_required': repair_required or phase != 'verify'}
    if route:
        result['route'] = route
    if routed and result['repair_required']:
        result['repair_sequence'] = (v2_repair(stage, data, get_split(store, item)) if repair_required
                                     else prior_repair_sequence(store, item))
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
        if route:
            note += f' 范围 {route["scope"]}，判定来源 {route["source"]}。'
        operation_id = enqueue_note(store, item, stage, note)
        if next_stage:
            disposition = 'next'
            store.update_item(item, stage=next_stage)
        else:
            steps = terminal_steps(store, item)
            route = get_split(store, item) if routed else None
            conclusion = next((step['completion'] for step in steps if step['outcome'] == 'failed'),
                              '阻断' if route and route.get('blockers') else '已交付')
            disposition = 'close' if conclusion == '已交付' else 'blocked'
            store.update_item(item, state='closing', conclusion=conclusion)
    return message('attempt_finished', attempt_id=attempt_id, step=stage, next_stage=next_stage, outcome=outcome,
                   attempts_used=trial['retry_number'] + 1, disposition=disposition, operation_id=operation_id)


def terminal_steps(store, item):
    if is_routed(item):
        route, results = read_history(store, item)
        return ordered_steps(store, item, stages_for(store, item, route), results)
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
    return ordered_steps(store, item, stages_for(store, item), results)


def ordered_steps(store, item, order, results):
    steps = []
    skip_reason = ('因用户要求主动停跑而跳过' if item['conclusion'] == '主动停跑'
                   else f'因 {item["stage"]} 阻断跳过')
    if item['stage'] == 'material_check' and item['conclusion'] == '待补材料':
        skip_reason = '因需求评审确认缺少需求文件而跳过'
    for stage in order:
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
            # A repair can invalidate a stage already reported in this round.
            # Keep the original receipt and identify the closeout fact separately.
            label = 'skipped/' + step['stage'] if is_routed(item) else step['stage']
            enqueue_note(store, item, label, f'{name} {store.time} {step["summary"]}。')
    statuses = {'success': '成功', 'failed': '失败', 'skipped': '跳过'}
    points = [f'{STAGE_NAMES[step["stage"]]}{statuses[step["outcome"]]}（{step["summary"]}）' for step in steps]
    note = '，'.join(points) + f'。本单收尾为「{conclusion}」。'
    extra = {}
    if is_routed(item):
        route, stages = read_history(store, item)
        if route:
            extra['delivery_summary'] = delivery_summary(route, stages)
            note += ' ' + extra['delivery_summary']
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
                   attachment_id=attachment_id, item=item_identity(item), **extra)


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
    if is_routed(item):
        route = get_split(store, item)
        # A corrected requirement may change the split. Re-read its source first.
        if stage == 'split' or (route and route.get('blockers')):
            stage = 'material_check'
    store.update_item(item, state=state, stage=stage, conclusion=None, run_round=item['run_round'] + 1)
    return message('recovered', item_state=state, stage=stage, chat_id=item['chat_id'],
                   run_round=item['run_round'] + 1)
