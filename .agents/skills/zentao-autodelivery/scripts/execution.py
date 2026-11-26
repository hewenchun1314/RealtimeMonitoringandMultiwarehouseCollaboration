"""Work-chat binding, attempts, stage progression and closeout."""

import json
import re
from datetime import timedelta
from uuid import uuid4

from backend_dependencies import backend_services, dependency_context
from branch_check import branch_context, planned_applications, valid_evidence
from delivery_scope import material_location, read_material
from deploy_state import (DEPLOY_STAGE, GIT_PUSH_STAGES, WORKER_STAGES, configured_targets,
                          deployed_receipt_matches, release_locks)
from desktop_runtime import runtime_check
from ledger import STAGES, STAGE_NAMES, encode, item_identity, message, require_text, stamp
from material_sync import requirement_checkout, verify_requirement_intent
from route_evidence import agentlimb_receipt_error, delivery_summary, validate_partial_merge, validate_success
from route_execution import (blocked_tasks, bug_route, get_split, is_routed, is_streamlined_bug, read_history,
                             repository_plan, snapshot, split_result, stage_evidence, stage_role, stage_task_ids,
                             stages_for, task_scope, unit_test_revision, v2_repair)
import task_evidence


REPAIR_PHASES = {
    'code_review': ('repair_coding', 'repair_unit_test', 'verify'),
    'e2e': ('repair_coding', 'repair_unit_test', 'repair_review', 'verify'),
}
PHASE_TIMERS = {'repair_coding': 'coding', 'repair_unit_test': 'unit_test', 'repair_review': 'code_review'}
DEFAULT_INTEGRATION_TIMEOUT_MINUTES = 45
DEFAULT_DEPLOY_TIMEOUT_MINUTES = 60


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
    base = {'split': 'plan', 'handoff': 'integration', 'acceptance': 'e2e', 'push': 'integration'}.get(name, name)
    minutes = timeouts.get(key, timeouts.get(name, timeouts.get(base)))
    if minutes is None:
        if base == 'integration':
            minutes = DEFAULT_INTEGRATION_TIMEOUT_MINUTES
        elif base == 'branch_check':
            minutes = 15
        elif base == 'deploy':
            minutes = DEFAULT_DEPLOY_TIMEOUT_MINUTES
        else:
            raise ValueError(f'Missing timeout for {key}')
    return stamp(store.now + timedelta(minutes=minutes))


def phase_context(store, item, phase, config_directory):
    stage = item['stage'] if phase == 'verify' else phase
    context = {'role': stage_role(stage)}
    if is_streamlined_bug(item):
        side, _, leaf = stage.partition('.')
        if stage in ('material_check', 'split'):
            context['role'] = '主工单 Agent'
            context['bug_triage_required'] = True
        elif leaf in ('code_review', 'acceptance', 'e2e'):
            context['role'] = '独立验证 Agent'
            context['validation_stages'] = [side + '.code_review',
                                            side + ('.e2e' if side == 'frontend' else '.acceptance')]
    scope = task_scope(store, item, stage)
    if scope is not None and stage_task_ids(get_split(store, item), stage):
        context['task_scope'] = scope
    if not stage.startswith('backend.'):
        return context
    config = snapshot(item)
    if not config.get('backend_dependencies'):
        return context
    services = None
    if stage not in ('backend.design', 'backend.plan'):
        plan = repository_plan(store, item, 'backend')
        services = set(planned_applications(plan, 'backend') or [])
    dependencies = dependency_context(config, config_directory, services)
    if dependencies:
        context['backend_dependencies'] = dependencies
    return context


def material_sync_required(store, item):
    return (item['kind'] == 'story' and item['stage'] == 'material_check'
            and store.config.get('runtime', {}).get('sync_requirement_before_review', False))


def story_review_enabled(item):
    return (item['kind'] == 'story' and is_routed(item)
            and snapshot(item).get('runtime', {}).get('story_review_sync', False))


def attempt_seed(store, item, routed):
    previous = store.conn.execute('''SELECT run_round,retry_number,result FROM attempts
                                     WHERE item_seq=? AND stage=?
                                     ORDER BY run_round DESC,retry_number DESC LIMIT 1''',
                                  (item['seq'], item['stage'])).fetchone()
    new_round = previous and previous['run_round'] != item['run_round']
    retries = previous['retry_number'] + 1 if previous and not new_round else 0
    if retries > snapshot(item)['development']['max_retries_per_step']:
        return None
    last_result = json.loads(previous['result']) if previous else {}
    if routed and new_round and last_result.get('repair_required'):
        entered_round = store.conn.execute('SELECT 1 FROM attempts WHERE item_seq=? AND run_round=? LIMIT 1',
                                           (item['seq'], item['run_round'])).fetchone()
        stage_record = read_history(store, item)[1].get(item['stage'])
        # A re-split or an earlier step in the new round invalidates the old itinerary.
        if entered_round or not stage_record or stage_record['outcome'] not in ('failed', 'skipped'):
            last_result = {}
    if not last_result.get('repair_required'):
        return retries, 'verify', []
    if not routed:
        return retries, 'repair_coding', []
    if last_result['phase'] == 'verify':
        return retries, last_result['repair_sequence'][0], []
    history = last_result['repair_phases']
    if new_round:
        for index, receipt in enumerate(history):
            if validate_success(store, item, receipt['phase'], receipt['evidence']):
                return retries, receipt['phase'], history[:index]
    if last_result.get('coding_revision_required'):
        revision = unit_test_revision(last_result['phase'], last_result['repair_sequence'], history)
        if revision is None:
            raise ValueError('Unit-test coding revision has no preceding coding receipt')
        return retries, *revision
    return retries, last_result['phase'], history


def attempt_start(store, item, data, *, config_directory, deployment_worker=False):
    if item['state'] in ('review_handoff', 'review_wait', 'review_ready') and execution_matches(store, item, data):
        return message('story_review_pending')
    if not execution_matches(store, item, data) or item['state'] != 'running':
        return message('stale_execution')
    if data.get('stage') != item['stage']:
        return message('stage_mismatch', stage=item['stage'])
    if item['stage'] in WORKER_STAGES and not deployment_worker:
        return message('deployment_worker_required')
    preparation = branch_context(store, item) if item['stage'].rsplit('.', 1)[-1] == 'branch_check' else {}
    if material_sync_required(store, item):
        preparation['material_sync_required'] = True
    if item['stage'] == 'material_check' and story_review_enabled(item):
        preparation['story_review_required'] = True
    routed = is_routed(item)
    if routed:
        preparation['route'] = get_split(store, item)
    active = active_attempt(store, item)
    if active:
        if active['phase'] in WORKER_STAGES and not deployment_worker:
            return message('deployment_worker_required')
        if routed:
            preparation.update(phase_context(store, item, active['phase'], config_directory))
        return message('attempt_active', attempt_id=active['attempt_id'], deadline_at=active['deadline_at'],
                       phase=active['phase'], retry_number=active['retry_number'],
                       phase_started_at=active['phase_started_at'], **preparation)
    seed = attempt_seed(store, item, routed)
    if seed is None:
        return message('retry_exhausted')
    retries, phase, history = seed
    if phase in WORKER_STAGES and not deployment_worker:
        return message('deployment_worker_required')
    deadline = phase_deadline(store, item, phase)
    attempt_id = uuid4().hex
    store.conn.execute('''INSERT INTO attempts(attempt_id,item_seq,run_round,stage,retry_number,started_at,
                                              deadline_at,phase,phase_started_at,phase_history)
                          VALUES(?,?,?,?,?,?,?,?,?,?)''', (attempt_id, item['seq'], item['run_round'], item['stage'],
                                                          retries, store.time, deadline, phase, store.time, encode(history)))
    if routed:
        preparation.update(phase_context(store, item, phase, config_directory))
    return message('attempt_started', attempt_id=attempt_id, stage=item['stage'], retry_number=retries,
                   started_at=store.time, phase_started_at=store.time, phase=phase, deadline_at=deadline,
                   **preparation)


def repair_advance(store, item, data, *, config_directory, deployment_worker=False):
    if not execution_matches(store, item, data) or item['state'] != 'running':
        return message('stale_execution')
    trial = active_attempt(store, item)
    if not trial or trial['attempt_id'] != data.get('attempt_id'):
        return message('stale_attempt')
    phase = trial['phase']
    if phase in WORKER_STAGES and not deployment_worker:
        return message('deployment_worker_required')
    routed = is_routed(item)
    if phase != data.get('phase') or phase == 'verify' or (not routed and phase not in PHASE_TIMERS):
        return message('phase_mismatch', phase=phase)
    if store.time >= trial['deadline_at']:
        return message('deadline_elapsed', deadline_at=trial['deadline_at'])
    summary = require_text(data.get('summary'), 'summary')
    evidence = data.get('evidence')
    if isinstance(evidence, dict):
        evidence = evidence.copy()
    if not evidence:
        raise ValueError('Repair phase completion requires evidence')
    completed = json.loads(trial['phase_history'])
    receipt = {'phase': phase, 'summary': summary, 'evidence': evidence,
               'started_at': trial['phase_started_at'], 'ended_at': store.time}
    if routed:
        if phase == 'split':
            try:
                if is_streamlined_bug(item):
                    material = stage_evidence(store, item, 'material_check')['material']
                    receipt['route'] = bug_route(snapshot(item), evidence, material)
                else:
                    receipt['route'] = split_result(store, item, evidence, revision=True)
            except ValueError as exc:
                return message('evidence_required', reason=str(exc))
            return finish_revision(store, item, trial, receipt, completed)
        else:
            if (phase == 'frontend.e2e' and is_streamlined_bug(item)
                    and snapshot(item).get('e2e', {}).get('agentlimb')):
                reason = agentlimb_receipt_error(store, item, trial['attempt_id'], evidence,
                                                 'passed', config_directory)
                if reason:
                    return message('evidence_required', reason=reason)
            reason = validate_success(store, item, phase, evidence)
            if reason:
                return message('evidence_required', reason=reason)
            reason = task_evidence.prepare_receipt(store, item, phase, evidence, True)
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
    if routed:
        task_evidence.refresh(store, item)
    context = phase_context(store, item, next_phase, config_directory) if routed else {}
    return message('repair_advanced', attempt_id=trial['attempt_id'], phase=next_phase,
                   phase_started_at=store.time, deadline_at=deadline, retry_number=trial['retry_number'], **context)


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
    reason = task_evidence.prepare_receipt(store, item, 'split', receipt['evidence'], True, route)
    if reason:
        return message('evidence_required', reason=reason)
    completed.append(receipt)
    detail = {'summary': receipt['summary'], 'evidence': receipt['evidence'],
              'phase': 'split', 'route_revision': True, 'repair_required': False,
              'repair_phases': completed, 'terminal_step': False}
    store.conn.execute('''UPDATE attempts SET ended_at=?, outcome='success', result=?, phase_history=?
                          WHERE attempt_id=?''',
                       (store.time, encode(detail), encode(completed), trial['attempt_id']))
    task_evidence.refresh(store, item)
    enqueue_note(store, item, 'split-revision-' + trial['attempt_id'],
                 f'拆分已修订，本轮旧版本归档，重新核验受影响仓库。{receipt["summary"]}')
    next_round = item['run_round'] + 1
    if route['blockers'] and not any(route['tasks'].values()):
        store.update_item(item, state='closing', conclusion='阻断', stage='split', run_round=next_round)
        next_stage = None
    else:
        next_stage = stages_for(store, item, route)[1 if is_streamlined_bug(item) else 2]
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


def story_review_payload(item, target, result, summary):
    if (not isinstance(target, dict) or any(not isinstance(target.get(key), str)
            or not re.fullmatch(pattern, target[key]) for key, pattern in (
                ('version', r'[1-9][0-9]*'), ('round', r'[1-9][0-9]*'), ('account', r'[A-Za-z0-9_.@-]+'),
                ('status', r'reviewing|active')))):
        raise ValueError('禅道评审需要实际读回的需求版本、评审轮次、状态和评审账号')
    target = {key: target[key] for key in ('version', 'round', 'account', 'status')}
    operation_id = f'{item["marker"]}/{item["run_round"]}/story-review'
    vote_key = f'{item["marker"]}/review/{target["version"]}/{target["round"]}/{target["account"]}'
    comment = f'{summary} [{operation_id}] [{vote_key}]'
    return operation_id, {**target, 'result': result, 'comment': comment, 'vote_key': vote_key}


def attempt_finish(store, item, data, branches_verified=False, config_directory=None, *, deployment_worker=False):
    if not execution_matches(store, item, data):
        return message('stale_execution')
    attempt_id = require_text(data.get('attempt_id'), 'attempt_id')
    trial = active_attempt(store, item)
    if (not trial or trial['attempt_id'] != attempt_id or item['state'] != 'running'
            or trial['stage'] != item['stage'] or trial['run_round'] != item['run_round']):
        return message('stale_attempt')
    outcome = data.get('outcome')
    if outcome not in ('success', 'failed', 'timed_out', 'skipped'):
        raise ValueError('outcome must be success, failed, timed_out or skipped')
    phase = trial['phase']
    if (item['stage'] in WORKER_STAGES or phase in WORKER_STAGES) and not deployment_worker:
        return message('deployment_worker_required')
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
    streamlined = is_streamlined_bug(item)
    stage = item['stage']
    entry_stage = stage in ('material_check', 'split')
    skipped = outcome == 'skipped'
    if skipped and (not routed or entry_stage):
        raise ValueError('Only routed development stages can be skipped')
    leaf = stage.rsplit('.', 1)[-1]
    nested_unit_revision = None
    if routed and repair_required is True and phase.endswith('.unit_test'):
        nested_unit_revision = unit_test_revision(phase, prior_repair_sequence(store, item), completed_phases)
        # An inner test can only return to its own coding phase. A new contract
        # or cross-side itinerary must be raised at its outer verification.
        if (nested_unit_revision is None or data.get('repair_side', phase.split('.')[0]) != phase.split('.')[0]
                or data.get('contract_changed', False) is not False
                or data.get('frontend_changed', False) is not False):
            raise ValueError('Nested unit-test revision must return to its preceding same-side coding phase')
    if routed:
        can_repair = leaf in ('unit_test', 'code_review', 'e2e', 'acceptance', 'integration') or (
            leaf in ('plan', 'design') and data.get('contract_changed') is True)
    else:
        can_repair = stage in REPAIR_PHASES
    if type(repair_required) is not bool or (repair_required and (
            outcome != 'failed' or not can_repair or (phase != 'verify' and nested_unit_revision is None))):
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
    if isinstance(evidence, dict):
        if skipped and any(evidence.get(key) for key in (
                'merged_versions', 'repositories', 'services', 'task_results', 'verified_tasks')):
            return message('evidence_required', reason='A skipped stage cannot claim execution results')
        evidence = evidence.copy()
    if stage == 'frontend.e2e' and phase == 'verify' and (finished or repair_required) and config.get('e2e', {}).get('agentlimb'):
        expected = 'passed' if finished else 'failed'
        reason = agentlimb_receipt_error(store, item, attempt_id, evidence, expected, config_directory)
        if reason:
            return message('evidence_required', reason=reason)
    missing_file = (item['kind'] == 'story' and stage == 'material_check' and outcome == 'failed'
                    and isinstance(evidence, dict) and evidence.get('material_file_status') == 'missing')
    story_review = stage == 'material_check' and story_review_enabled(item)
    clarification = (story_review and outcome == 'failed' and isinstance(evidence, dict)
                     and evidence.get('material_review_status') == 'needs_clarification')
    sync_required = material_sync_required(store, item)
    sync_receipt = None
    if sync_required:
        row = store.conn.execute('SELECT receipt FROM requirement_sync_receipts WHERE attempt_id=?',
                                 (attempt_id,)).fetchone()
        if row:
            sync_receipt = json.loads(row['receipt'])
        elif finished or missing_file or clarification:
            return message('material_sync_required')
    if sync_receipt and (finished or missing_file or clarification):
        try:
            path = None
            if finished or clarification or evidence.get('path'):
                root, intent = material_location(config, config_directory,
                                                 evidence.get('path') if isinstance(evidence, dict) else None)
                path = intent.relative_to(root).as_posix()
            else:
                root = requirement_checkout(config, config_directory)
            verify_requirement_intent(root, sync_receipt['commit'], path, missing=missing_file)
        except (ValueError, OSError) as exc:
            return message('evidence_required', reason=str(exc))
    review = None
    if story_review and (finished or missing_file or clarification):
        target_row = store.conn.execute('SELECT target FROM story_review_targets WHERE attempt_id=?', (attempt_id,)).fetchone()
        if not target_row:
            return message('evidence_required', reason='评审前须通过 Windows 启动器取得本次尝试的禅道评审目标')
        comment = summary
        if finished:
            path = evidence.get('path') if isinstance(evidence, dict) else None
            comment += f'。需求材料：{path or "未登记"}'
            if sync_receipt:
                comment += f'，Git 提交：{sync_receipt["commit"]}'
        try:
            if clarification and not missing_file:
                read_material(config, config_directory, item['kind'], evidence)
            target = json.loads(target_row['target'])
            review = story_review_payload(item, target, 'pass' if finished else 'clarify', comment)
            evidence = {**(evidence or {}), 'review_target': target}
        except ValueError as exc:
            return message('evidence_required', reason=str(exc))
    route = None
    current_route, history = read_history(store, item) if routed else (None, {})
    reported = evidence.get('task_blockers') if isinstance(evidence, dict) else None
    stage_tasks = stage_task_ids(current_route, stage) if current_route else set()
    if reported is not None:
        if (phase != 'verify' or repair_required or not isinstance(reported, dict) or not reported
                or not reported.keys() <= stage_tasks
                or any(not isinstance(reason, str) or not reason.strip() for reason in reported.values())):
            return message('evidence_required', reason='task_blockers must map current stage task IDs to reasons')
        prospective = blocked_tasks(current_route, {**history, stage: {'task_blockers': reported}})
        if finished and not (stage_tasks - prospective.keys()):
            return message('evidence_required', reason='A stage with no remaining tasks cannot report success')
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
                    if streamlined:
                        route = bug_route(config, evidence, material)
                else:
                    route = split_result(store, item, evidence)
            except (ValueError, OSError) as exc:
                return message('evidence_required', reason=str(exc))
        else:
            reason = validate_success(store, item, stage, evidence, task_blockers=reported)
            if reason:
                return message('evidence_required', reason=reason)
    stages = stages_for(store, item, route)
    if sync_receipt:
        evidence = {**(evidence if isinstance(evidence, dict) else {}), 'requirement_sync': sync_receipt}
    retry = (not finished and not skipped and not missing_file and not clarification and data.get('retryable', True)
             and trial['retry_number'] < development['max_retries_per_step'])
    if routed and repair_required and data.get('contract_changed') and revision_count(store, item) >= development['max_retries_per_step']:
        retry = False
    if finished and leaf == 'plan':
        side = stage.split('.')[0] if routed else 'frontend'
        targets = planned_applications(evidence, side)
        if stage.replace('plan', 'branch_check') in stages and not targets:
            return message('services_required' if side == 'backend' else 'applications_required')
        if side == 'backend' and set(targets or []) - backend_services(config).keys():
            return message('evidence_required', reason='Backend plan services must be writable configured services')
    if finished and leaf == 'branch_check':
        if not valid_evidence(branch_context(store, item), evidence):
            return message('branch_evidence_required')
        if not branches_verified:
            return message('branch_worktree_unverified')
    if routed:
        reason = task_evidence.prepare_receipt(store, item, stage if phase == 'verify' else phase,
                                               evidence, finished, route, task_blockers=reported)
        if reason:
            return message('evidence_required', reason=reason)
    number = stages.index(stage)
    no_work = route and route['blockers'] and not any(route['tasks'].values())
    merge_blocked = (outcome in ('failed', 'timed_out') and not retry
                     and (leaf == 'integration' or phase.endswith('.integration')))
    bug_blocked = streamlined and not finished and not retry
    stop_route = (missing_file or no_work or merge_blocked or bug_blocked or
                  (not finished and ((routed and entry_stage) or sync_required or story_review)))
    next_stage = stages[number + 1] if not stop_route and number + 1 < len(stages) else None
    recorded_blockers = dict(reported or {}) if not retry else {}
    if current_route and not retry and phase == 'verify':
        existing = blocked_tasks(current_route, history)
        if not finished and stage in GIT_PUSH_STAGES:
            side = stage.split('.')[0]
            plan = repository_plan(store, item, side) or {}
            pushed = (evidence or {}).get('repositories', {})
            recorded_blockers.update({
                key: '待核验远端代码：' + '、'.join(sorted(set(repositories) - pushed.keys()))
                for key, repositories in plan.get('task_repositories', {}).items()
                if key in stage_tasks - existing.keys() and set(repositories) - pushed.keys()
            })
        if not finished and stage == 'handoff':
            recorded_blockers.update({key: summary for key in stage_tasks - existing.keys() - recorded_blockers.keys()})
        if not finished and stage == DEPLOY_STAGE and not recorded_blockers:
            plan = repository_plan(store, item, 'backend') or {}
            merged = (stage_evidence(store, item, 'backend.integration') or {}).get('merged_versions', {})
            receipts = (evidence or {}).get('services', {})
            targets = configured_targets(config)
            failed_services = {key for key in planned_applications(plan, 'backend') or ()
                               if key in targets and not deployed_receipt_matches(
                                   receipts.get(key), key, merged.get(key), targets[key])}
            recorded_blockers.update({
                key: '待核验部署：' + '、'.join(sorted(set(services) & failed_services))
                for key, services in plan.get('task_repositories', {}).items()
                if key in stage_tasks - existing.keys() and set(services) & failed_services
            })
    result = {'summary': summary, 'evidence': evidence, 'stop_evidence': data.get('stop_evidence'),
              'terminal_step': not retry, 'phase': phase, 'repair_phases': completed_phases,
              'repair_required': repair_required or phase != 'verify'}
    if recorded_blockers:
        result['task_blockers'] = recorded_blockers
    if routed and not retry and not finished and (phase == 'split' or (repair_required and data.get('contract_changed'))):
        result['contract_revision_pending'] = True
    if route:
        result['route'] = route
    if routed and result['repair_required']:
        result['repair_sequence'] = (v2_repair(stage, data, get_split(store, item)) if repair_required and not nested_unit_revision
                                     else prior_repair_sequence(store, item))
        if nested_unit_revision:
            result['coding_revision_required'] = True
    if not finished and not retry:
        default_completion = '待验证' if routed and not entry_stage else '阻断'
        result['completion'] = '待补材料' if missing_file or clarification else data.get('completion', default_completion)
        if result['completion'] not in ('待补材料', '待验证', '阻断'):
            raise ValueError('Invalid completion for a blocked step')
        if merge_blocked:
            result['completion'] = '阻断'
    store.conn.execute('UPDATE attempts SET ended_at=?,outcome=?,result=? WHERE attempt_id=?',
                       (store.time, outcome, encode(result), attempt_id))
    if routed:
        task_evidence.refresh(store, item)
    if not retry and (not next_stage or stage == 'frontend.e2e'):
        release_locks(store, item)

    operation_id = None
    if retry:
        disposition = 'retry'
        next_stage = stage
    else:
        result_label = {'success': '成功', 'failed': '失败', 'timed_out': '超时', 'skipped': '跳过'}[outcome]
        stage_started = store.conn.execute('''SELECT MIN(started_at) FROM attempts
                                              WHERE item_seq=? AND run_round=? AND stage=?''',
                                           (item['seq'], item['run_round'], stage)).fetchone()[0]
        note = (f'{STAGE_NAMES[stage]} {stage_started} 开始，{store.time} 结束，'
                f'{result_label}（重试 {trial["retry_number"]} 次）。{summary}')
        if route:
            note += f' 范围 {route["scope"]}，判定来源 {route["source"]}。'
        if recorded_blockers:
            note += ' 受阻任务：' + '；'.join(f'{key} {reason}' for key, reason in recorded_blockers.items()) + '。'
        operation_id = enqueue_note(store, item, stage, note)
        if review:
            enqueue(store, item, review[0], 'story_review', review[1])
            disposition = 'review_wait'
            next_stage = None
            store.update_item(item, state='review_handoff')
        elif next_stage:
            disposition = 'next'
            store.update_item(item, stage=next_stage)
        else:
            steps = terminal_steps(store, item)
            route = get_split(store, item) if routed else None
            conclusion = next((step['completion'] for step in steps if 'completion' in step),
                              '阻断' if route and (route.get('blockers') or any(
                                  step.get('task_blockers') for step in steps)) else '已交付')
            if merge_blocked:
                conclusion = '阻断'
            elif route and not stop_route and conclusion != '已交付':
                conclusion = '待验证'
            if conclusion == '已交付' and task_evidence.first_unverified_stage(store, item):
                conclusion = '待验证'
            disposition = 'close' if conclusion == '已交付' or (route and not stop_route) else 'blocked'
            store.update_item(item, state='closing', conclusion=conclusion)
    return message('attempt_finished', attempt_id=attempt_id, step=stage, next_stage=next_stage, outcome=outcome,
                   attempts_used=trial['retry_number'] + 1, disposition=disposition, operation_id=operation_id,
                   **({'review_operation_id': review[0]} if review else {}))


def terminal_steps(store, item):
    if is_routed(item):
        route, results = read_history(store, item)
        return ordered_steps(store, item, stages_for(store, item, route), results, route)
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


def ordered_steps(store, item, order, results, route=None):
    steps = []
    blocked = blocked_tasks(route, results) if route else {}
    skip_reason = ('因用户要求主动停跑而跳过' if item['conclusion'] == '主动停跑'
                   else f'因 {item["stage"]} 阻断跳过')
    if item['stage'] == 'material_check' and item['conclusion'] == '待补材料':
        skip_reason = '因需求评审确认材料待补充或待明确而跳过'
    for stage in order:
        step = results.get(stage)
        if step is None:
            reason = skip_reason
            if route and blocked and item['conclusion'] != '主动停跑':
                tasks = stage_task_ids(route, stage)
                if tasks and tasks <= blocked.keys():
                    reason = '本阶段任务均受阻：' + '、'.join(sorted(tasks))
                elif stage == 'handoff' and not tasks:
                    reason = '剩余任务无后端交接依赖'
            step = {'stage': stage, 'outcome': 'skipped', 'summary': reason,
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
        if step['outcome'] == 'skipped' and 'started_at' not in step:
            name = STAGE_NAMES[step['stage']]
            # A repair can invalidate a stage already reported in this round.
            # Keep the original receipt and identify the closeout fact separately.
            label = 'skipped/' + step['stage'] if is_routed(item) else step['stage']
            enqueue_note(store, item, label, f'{name} {store.time} {step["summary"]}。')
    statuses = {'success': '成功', 'failed': '失败', 'skipped': '跳过'}
    points = [f'{STAGE_NAMES[step["stage"]]}{statuses[step["outcome"]]}（{step["summary"]}）' for step in steps]
    note = '，'.join(points) + f'。本单收尾为「{conclusion}」。'
    extra = {}
    formal = store.conn.execute('''SELECT receipt FROM writebacks WHERE item_seq=? AND run_round=?
                                   AND operation_type='story_review' AND state='confirmed' ''',
                                (item['seq'], item['run_round'])).fetchone()
    if formal:
        review = json.loads(formal['receipt'])
        extra['story_review'] = review
        if review.get('progress') == 'clarified':
            note += f' 禅道已记录「有待明确」（操作 #{review["action_id"]}）。'
        elif review.get('progress') == 'blocked':
            note += f' 禅道评审阻断：{review["reason"]}。'
    if is_routed(item):
        route, stages = read_history(store, item)
        if route:
            extra['delivery_summary'] = delivery_summary(route, stages)
            note += ' ' + extra['delivery_summary']
            blocked = blocked_tasks(route, stages)
            if blocked:
                extra['blocked_tasks'] = blocked
                note += ' 待续接任务：' + '；'.join(f'{key} {entry["reason"]}'
                                                  for key, entry in sorted(blocked.items())) + '。'
        total, delivered, closed, unbound, closing, reopening = store.conn.execute('''
            SELECT COUNT(*), SUM(desired='closed'), SUM(remote_status='closed'), SUM(remote_id IS NULL),
                   SUM(remote_id IS NOT NULL AND desired='closed' AND remote_status IS NOT 'closed'),
                   SUM(desired='open' AND remote_status='closed')
            FROM split_tasks WHERE item_seq=?''', (item['seq'],)).fetchone()
        if total:
            delivery_label = ('满足本单交付条件' if route and route['scope'] != 'backend_only'
                              and not route.get('e2e_enabled', True) else '研发已验收并合回')
            extra['task_summary'] = (f'收尾时{delivery_label} {delivered}/{total} 项，'
                                     f'禅道已关闭 {closed or 0}/{total} 张，待建单 {unbound} 张，'
                                     f'待关闭 {closing or 0} 张，待重开 {reopening or 0} 张。')
            note += ' ' + extra['task_summary']
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
    release_locks(store, item)
    set_dispatch_state(store, dispatch(store, item, data), 'completed')
    return message('completed', conclusion=conclusion, steps=steps, operation_id=operation_id,
                   attachment_id=attachment_id, item=item_identity(item), **extra)


def stop(store, item, data):
    if item['state'] not in ('running', 'review_handoff', 'review_wait', 'review_ready') or not execution_matches(store, item, data):
        return message('stale_execution')
    if active_attempt(store, item):
        return message('attempt_active')
    evidence = data.get('stop_evidence')
    if (not isinstance(evidence, dict) or evidence.get('chat_status') != 'idle'
            or type(evidence.get('owned_processes_active_after_stop')) is not int
            or evidence['owned_processes_active_after_stop'] != 0):
        return message('stop_unverified')
    if item['state'] in ('review_handoff', 'review_wait', 'review_ready'):
        if store.conn.execute('''SELECT 1 FROM writebacks WHERE item_seq=? AND run_round=?
                                  AND operation_type='story_review' AND state='sending' AND expires_at>?''',
                              (item['seq'], item['run_round'], store.time)).fetchone():
            return message('story_review_busy')
        store.conn.execute('''UPDATE writebacks SET state='superseded' WHERE item_seq=? AND run_round=?
                              AND operation_type='story_review' AND state!='confirmed' ''',
                           (item['seq'], item['run_round']))
    store.update_item(item, state='closing', conclusion='主动停跑')
    return complete(store, store.find_item(item_identity(item)), data)


def recover(store, item, data, *, config_directory):
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
                  if 'completion' in step or step.get('task_blockers')), None)
    if stage is None:
        stage = task_evidence.first_unverified_stage(store, item) or item['stage']
    if is_routed(item):
        route = get_split(store, item)
        reviewed = (stage_evidence(store, item, 'material_check') or {}).get('material')
        changed = False
        if item['kind'] == 'story' and reviewed:
            try:
                current = read_material(snapshot(item), config_directory, 'story', {'path': reviewed['path']})
                changed = current['sha256'] != reviewed['sha256']
            except (ValueError, KeyError):
                # Route an unavailable reviewed source through normal material review.
                changed = True
        if changed or stage == 'split' or (route and route.get('blockers')):
            stage = 'material_check'
    store.update_item(item, state=state, stage=stage, conclusion=None, run_round=item['run_round'] + 1)
    return message('recovered', item_state=state, stage=stage, chat_id=item['chat_id'],
                   run_round=item['run_round'] + 1)
