"""Read the effective route, evidence and revision history from durable attempts."""

import json

from delivery_scope import resolve_split, route_stages
from deploy_state import DEPLOY_STAGE, FRONTEND_DEPLOY_STAGE, GIT_PUSH_STAGES, configured_targets, frontend_release_config
from ledger import workflow_stages


def snapshot(item):
    return json.loads(item['config_snapshot'])


def is_routed(item):
    return snapshot(item).get('workflow_version') == 2


def invalidate_repaired_outputs(stages, phase, route):
    if not route or phase not in route['stages']:
        return
    order = route['stages']
    side = phase.partition('.')[0]
    for stage in order[order.index(phase) + 1:]:
        if (stage.partition('.')[0] == side or
                (side in ('backend', 'handoff') and stage in (
                    'handoff', 'frontend.e2e', 'frontend.integration', 'frontend.push', FRONTEND_DEPLOY_STAGE))):
            stages.pop(stage, None)


def read_history(store, item):
    """Apply attempts and repair phases in insertion order, including an active repair."""
    records = store.conn.execute('''SELECT rowid,* FROM attempts WHERE item_seq=? ORDER BY rowid''',
                                 (item['seq'],))
    route = None
    stages = {}
    run_round = 0
    for record in records:
        stage = record['stage']
        if record['run_round'] != run_round:
            # Recovery restarts at the first unmet dependency. Downstream receipts from
            # the previous round are historical until that work is verified again.
            order = route['stages'] if route else ('material_check', 'split')
            if stage in order:
                for name in order[order.index(stage):]:
                    stages.pop(name, None)
                if stage in ('material_check', 'split'):
                    route = None
            run_round = record['run_round']
        for phase in json.loads(record['phase_history']):
            if phase['phase'] == 'split':
                route = phase['route']
                stages = {name: receipt for name, receipt in stages.items() if name == 'material_check'}
            else:
                invalidate_repaired_outputs(stages, phase['phase'], route)
            stages[phase['phase']] = {'stage': phase['phase'], 'outcome': 'success',
                                      'summary': phase['summary'], 'evidence': phase['evidence'],
                                      'started_at': phase['started_at'], 'ended_at': phase['ended_at'],
                                      'retries': record['retry_number']}
        if record['outcome'] is None:
            continue
        result = json.loads(record['result'])
        if result.get('route_revision'):
            continue
        if record['outcome'] == 'success' and stage == 'split':
            route = result['route']
        entry = {'stage': stage, 'outcome': 'success' if record['outcome'] == 'success' else 'failed',
                 'summary': result['summary'], 'evidence': result['evidence'], 'started_at': record['started_at'],
                 'ended_at': record['ended_at'], 'retries': record['retry_number']}
        if 'terminal_step' in result:
            entry['terminal_step'] = result['terminal_step']
        if result.get('task_blockers'):
            entry['task_blockers'] = result['task_blockers']
        if entry['outcome'] == 'failed':
            entry['completion'] = result.get('completion', '阻断')
            phase = result['phase']
            if phase != 'verify' and phase != 'split':
                invalidate_repaired_outputs(stages, phase, route)
                stages[phase] = {**entry, 'stage': phase}
        stages[stage] = entry
    return route, stages


def get_split(store, item):
    return read_history(store, item)[0]


def stage_evidence(store, item, stage):
    step = read_history(store, item)[1].get(stage)
    if step and step['outcome'] == 'success':
        return step['evidence']
    return None


def stages_for(store, item, route=None):
    if not is_routed(item):
        return workflow_stages(snapshot(item)['development'])
    route = route or get_split(store, item)
    return route['stages'] if route else ('material_check', 'split')


def handoff_tasks(route):
    """Tasks whose remaining work needs a callable backend handoff."""
    backend = route['tasks'].get('backend', [])
    upstream = {task['id'] for task in backend}
    joint = {task['id'] for task in backend if task.get('joint_acceptance')}
    frontend = route['tasks'].get('frontend', [])
    while True:
        dependent = {task['id'] for task in frontend
                     if set(task.get('depends_on', [])) & (upstream | joint)}
        if dependent <= joint:
            return joint
        joint.update(dependent)


def blocked_tasks(route, stages):
    blocked = {key: {'stage': stage, 'reason': reason}
               for stage, receipt in stages.items()
               for key, reason in receipt.get('task_blockers', {}).items()}
    tasks = [task for tasks in route['tasks'].values() for task in tasks]
    while True:
        added = {}
        for task in tasks:
            key = task['id']
            dependencies = sorted(set(task.get('depends_on', [])) & blocked.keys())
            if dependencies and key not in blocked:
                added[key] = {'depends_on': dependencies, 'reason': '依赖受阻：' + '、'.join(dependencies)}
        if not added:
            return blocked
        blocked.update(added)


def stage_task_ids(route, stage):
    if stage == 'handoff':
        return handoff_tasks(route)
    return {task['id'] for task in route['tasks'].get(stage.partition('.')[0], [])}


def task_scope(store, item, stage):
    route, stages = read_history(store, item)
    if not route:
        return None
    blocked = blocked_tasks(route, stages)
    return {'active': sorted(stage_task_ids(route, stage) - blocked.keys()), 'blocked': blocked}


def split_result(store, item, evidence, *, revision=False):
    reviewed = stage_evidence(store, item, 'material_check')
    if not isinstance(reviewed, dict) or not reviewed.get('material'):
        raise ValueError('Scope splitting requires the reviewed material snapshot')
    if not isinstance(evidence, dict):
        raise ValueError('Split evidence must be an object')
    config = snapshot(item)
    route = resolve_split(config, {**evidence, 'material': reviewed['material']})
    route['e2e_enabled'] = config.get('e2e', {}).get('enabled', True)
    route['stages'] = list(route_stages(route['scope'], e2e_enabled=route['e2e_enabled'],
                                       deploy_enabled=bool(configured_targets(config)),
                                       frontend_deploy_enabled=frontend_release_config(config).get('enabled', False),
                                       git_push_enabled=config.get('runtime', {}).get('git_push_enabled', False)))
    if revision:
        previous = get_split(store, item)
        if not previous or previous['scope'] != route['scope']:
            raise ValueError('A repair must remain within the admitted route')
    return route


def v2_repair(stage, data, route):
    """Store a repair itinerary in the failed verification receipt for replay."""
    side, name = stage.split('.')
    owner = data.get('repair_side', side)
    for flag in ('contract_changed', 'frontend_changed'):
        if flag in data and type(data[flag]) is not bool:
            raise ValueError(f'{flag} must be a boolean')
    joint = stage == 'frontend.e2e' and route and route['scope'] == 'fullstack'
    if owner not in (side, 'backend') or (owner != side and not joint):
        raise ValueError('repair_side must belong to this verification')
    phases = ['split'] if data.get('contract_changed') else []
    if name in ('plan', 'design'):
        return ['split', 'verify']
    phases.extend((owner + '.coding', owner + '.unit_test'))
    if name != 'code_review':
        phases.append(owner + '.code_review')
    if name == 'integration':
        acceptance = owner + ('.e2e' if owner == 'frontend' else '.acceptance')
        if acceptance in route['stages']:
            phases.append(acceptance)
    if joint and owner == 'backend':
        phases.extend(('backend.acceptance', 'backend.integration'))
        if 'backend.push' in route['stages']:
            phases.append('backend.push')
        if DEPLOY_STAGE in route['stages']:
            phases.append(DEPLOY_STAGE)
        phases.append('handoff')
        if data.get('frontend_changed') or data.get('contract_changed'):
            phases.extend(('frontend.coding', 'frontend.unit_test', 'frontend.code_review'))
    return [*phases, 'verify']


def stage_role(stage):
    if stage in GIT_PUSH_STAGES:
        return 'Git 推送执行器'
    if stage == FRONTEND_DEPLOY_STAGE:
        return '前端部署 Agent'
    if stage == DEPLOY_STAGE:
        return '发布执行器'
    if stage == 'material_check':
        return '需求评审 Agent'
    if stage == 'split':
        return '拆分 Agent'
    if stage == 'handoff' or stage.endswith(('.branch_check', '.integration')):
        return '主工单 Agent'
    side, _, name = stage.partition('.')
    role = '测试 Agent' if name in ('code_review', 'acceptance', 'e2e') else '开发 Agent'
    return ('前端' if side == 'frontend' else '后端') + role
