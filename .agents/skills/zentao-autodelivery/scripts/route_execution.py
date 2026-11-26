"""Read the effective route, evidence and revision history from durable attempts."""

import json

from backend_dependencies import backend_services
from branch_check import planned_applications, valid_target
from delivery_scope import resolve_split, route_stages
from deploy_state import DEPLOY_STAGE, FRONTEND_DEPLOY_STAGE, GIT_PUSH_STAGES, configured_targets, frontend_release_config
from ledger import workflow_stages


def snapshot(item):
    return json.loads(item['config_snapshot'])


def is_routed(item):
    return snapshot(item).get('workflow_version') == 2


def is_streamlined_bug(item):
    if 'kind' not in item.keys() or item['kind'] != 'bug':
        return False
    config = snapshot(item)
    return config.get('workflow_version') == 2 and config.get('runtime', {}).get('streamlined_bug_intake') is True


def invalidate_repaired_outputs(stages, phase, route):
    if not route or phase not in route['stages']:
        return
    order = route['stages']
    side = phase.partition('.')[0]
    for stage in order[order.index(phase) + 1:]:
        if stage == 'frontend.integration' and route.get('post_merge_e2e'):
            continue
        if (stage.partition('.')[0] == side or
                (side in ('backend', 'handoff') and stage in (
                    'handoff', 'frontend.e2e', 'frontend.integration', 'frontend.push', FRONTEND_DEPLOY_STAGE))):
            stages.pop(stage, None)


def replay_main_repair(stages, route, versions):
    """Keep the original integration attempt and advance its effective target versions."""
    if not (route or {}).get('post_merge_e2e'):
        return
    merged = stages['frontend.integration']
    for name in ('frontend.unit_test', 'frontend.code_review', 'frontend.e2e', 'frontend.push', FRONTEND_DEPLOY_STAGE):
        stages.pop(name, None)
    previous = merged['evidence']['merged_versions']
    bases = (merged['evidence'].get('main_repair_base_versions', previous)
             if previous == versions else previous)
    stages['frontend.integration'] = {**merged, 'evidence': {
        **merged['evidence'], 'versions': versions, 'merged_versions': versions,
        'task_changed_merges': [], 'main_workspace_repair': True,
        'main_repair_base_versions': bases,
    }}


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
            elif phase.get('frontend_main_repair_versions'):
                replay_main_repair(stages, route, phase['frontend_main_repair_versions'])
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
        route_stage = stage == 'split' or (stage == 'material_check' and is_streamlined_bug(item))
        if record['outcome'] == 'success' and route_stage and 'route' in result:
            route = result['route']
        outcome = 'failed' if record['outcome'] == 'timed_out' else record['outcome']
        entry = {'stage': stage, 'outcome': outcome,
                 'summary': result['summary'], 'evidence': result['evidence'], 'started_at': record['started_at'],
                 'ended_at': record['ended_at'], 'retries': record['retry_number']}
        if 'terminal_step' in result:
            entry['terminal_step'] = result['terminal_step']
        if 'release_versions' in result:
            entry['release_versions'] = result['release_versions']
        if result.get('task_blockers'):
            entry['task_blockers'] = result['task_blockers']
        if result.get('contract_revision_pending'):
            entry['contract_revision_pending'] = True
        if outcome != 'success':
            entry['completion'] = result.get('completion', '阻断')
            phase = result['phase']
            if phase != 'verify' and phase != 'split':
                invalidate_repaired_outputs(stages, phase, route)
                stages[phase] = {**entry, 'stage': phase}
        stages[stage] = entry
    return route, stages


def get_split(store, item):
    return read_history(store, item)[0]


def contract_revision_pending(stages):
    return any(receipt.get('contract_revision_pending') for receipt in stages.values())


def successful_evidence(stages, stage):
    if stage.endswith(('.plan', '.coding')) and contract_revision_pending(stages):
        return None
    step = stages.get(stage)
    if step and step['outcome'] == 'success':
        return step['evidence']
    return None


def stage_evidence(store, item, stage):
    _, stages = read_history(store, item)
    return successful_evidence(stages, stage)


def repository_plan(store, item, side, *, history=None):
    route, stages = read_history(store, item) if history is None else history
    if is_streamlined_bug(item):
        if route and not contract_revision_pending(stages):
            return route['intake_repository_plan'].get(side)
        return None
    return successful_evidence(stages, side + '.plan')


def stages_for(store, item, route=None):
    if not is_routed(item):
        return workflow_stages(snapshot(item)['development'])
    route = route or get_split(store, item)
    if route:
        return route['stages']
    return ('material_check',) if is_streamlined_bug(item) else ('material_check', 'split')


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
    tasks = stage_task_ids(route, stage)
    if stage.endswith('.integration'):
        # Pending validation does not remove an otherwise mergeable candidate.
        # Preserve observed assertion failures when selecting mergeable tasks.
        acceptance = 'frontend.e2e' if stage.startswith('frontend.') else 'backend.acceptance'
        if receipt := stages.get(acceptance):
            evidence = receipt.get('evidence')
            results = evidence.get('task_results') if isinstance(evidence, dict) else None
            if isinstance(results, dict):
                failed = {task: result.get('verification') or receipt['summary'] for task, result in results.items()
                          if task in tasks and isinstance(result, dict) and result.get('outcome') == 'failed'}
            else:
                failed = {}
            stages[acceptance] = {**receipt, 'task_blockers': failed}
    blocked = blocked_tasks(route, stages)
    if contract_revision_pending(stages):
        blocked.update({task: {'reason': '接口契约变更，等待重新拆分'} for task in tasks - blocked.keys()})
    return {'active': sorted(tasks - blocked.keys()), 'blocked': blocked}


def split_result(store, item, evidence, *, revision=False):
    reviewed = stage_evidence(store, item, 'material_check')
    if not isinstance(reviewed, dict) or not reviewed.get('material'):
        raise ValueError('Scope splitting requires the reviewed material snapshot')
    if not isinstance(evidence, dict):
        raise ValueError('Split evidence must be an object')
    config = snapshot(item)
    if is_streamlined_bug(item):
        route = bug_intake_route(config, evidence, reviewed['material'])
    else:
        route = configure_route(config, resolve_split(config, {**evidence, 'material': reviewed['material']}))
    if revision:
        previous = get_split(store, item)
        if not previous or previous['scope'] != route['scope']:
            raise ValueError('A repair must remain within the admitted route')
    return route


def configure_route(config, route):
    route['e2e_enabled'] = config.get('e2e', {}).get('enabled', True)
    if config.get('e2e', {}).get('after_integration') is True and route['scope'] != 'backend_only':
        route['post_merge_e2e'] = True
    route['stages'] = list(route_stages(route['scope'], e2e_enabled=route['e2e_enabled'],
                                       post_merge_e2e=route.get('post_merge_e2e', False),
                                       deploy_enabled=bool(configured_targets(config)),
                                       frontend_deploy_enabled=frontend_release_config(config).get('enabled', False),
                                       git_push_enabled=config.get('runtime', {}).get('git_push_enabled', False),
                                       streamlined_intake='intake_repository_plan' in route))
    return route


def bug_intake_route(config, evidence, material):
    if not isinstance(evidence, dict):
        raise ValueError('Bug intake evidence must be an object')
    repositories = evidence.get('repositories')
    if not isinstance(repositories, dict) or not repositories or repositories.keys() - {'frontend', 'backend'}:
        raise ValueError('Bug intake requires configured frontend or backend repositories')
    configured = {
        'frontend': config['development'].get('frontend_bundle', {}).get('applications', {}),
        'backend': backend_services(config),
    }
    acceptance = evidence.get('acceptance')
    if isinstance(acceptance, dict) and acceptance.keys() != repositories.keys():
        raise ValueError('Per-side acceptance must match the sides in Bug repositories')
    plans = {}
    tasks = {}
    for side, keys in repositories.items():
        field = 'services' if side == 'backend' else 'applications'
        plan = {field: keys}
        if not planned_applications(plan, side) or any(not valid_target(configured[side].get(key)) for key in keys):
            raise ValueError(f'Bug intake requires valid configured {side} repositories')
        plans[side] = {**plan, 'task_repositories': {side: keys}}
        criteria = acceptance[side] if isinstance(acceptance, dict) else acceptance
        tasks[side] = {'id': side, 'description': f'{side} Bug 修复', 'acceptance': criteria}
    fullstack = len(tasks) == 2
    depends = evidence.get('depends_on_backend', fullstack)
    if type(depends) is not bool or (depends and not fullstack):
        raise ValueError('Only a cross-side Bug can have a frontend dependency on backend changes')
    if depends:
        tasks['frontend']['depends_on'] = ['backend']
        tasks['backend']['joint_acceptance'] = True
        plans['frontend']['task_repositories']['backend'] = plans['frontend']['applications']
    scope = 'fullstack' if fullstack else next(iter(tasks)) + '_only'
    route = resolve_split(config, {**evidence, 'material': material, 'ai_scope': scope,
                                  'rationale': 'Bug 接单已核对目标仓库和验收条件',
                                  'tasks': {side: [task] for side, task in tasks.items()}})
    if route['scope'] != scope:
        raise ValueError('Bug repositories must match the configured or declared delivery scope')
    return configure_route(config, {**route, 'intake_repository_plan': plans})


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
    if name == 'unit_test':
        # The original unit-test stage is the final verifier. Publish a coding
        # revision first so its immutable candidate receipt names the new SHA.
        return [*phases, owner + '.coding', 'verify']
    phases.extend((owner + '.coding', owner + '.unit_test'))
    if name != 'code_review':
        phases.append(owner + '.code_review')
    if name == 'integration':
        acceptance = owner + ('.e2e' if owner == 'frontend' else '.acceptance')
        if (acceptance in route['stages'] and
                not (owner == 'frontend' and route.get('post_merge_e2e'))):
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


def unit_test_revision(phase, sequence, history):
    """Derive the preceding same-side coding phase without trusting caller routes."""
    if not phase.endswith('.unit_test') or phase not in sequence:
        return None
    position = sequence.index(phase)
    coding = phase.rsplit('.', 1)[0] + '.coding'
    if position == 0 or sequence[position - 1] != coding:
        return None
    for index in range(len(history) - 1, -1, -1):
        if history[index]['phase'] == coding:
            return coding, history[:index]
    return None


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
