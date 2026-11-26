"""Per-task acceptance and merge decisions derived from the effective workflow receipts."""

import json
import subprocess

from deploy_state import COMMIT, DEPLOY_STAGE, deployed_receipt_matches


# ledger imports this constant during initialization; avoid importing the workflow here.
EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_evidence (
    item_seq INTEGER NOT NULL, task_key TEXT NOT NULL, disposition TEXT NOT NULL,
    reason TEXT NOT NULL, evidence TEXT NOT NULL,
    PRIMARY KEY(item_seq,task_key),
    FOREIGN KEY(item_seq,task_key) REFERENCES split_tasks(item_seq,task_key)
);
"""


def _tasks(route, side):
    return {task['id']: task for task in route['tasks'].get(side, [])}


def _nonblank(value):
    return isinstance(value, str) and bool(value.strip())


def validate_task_fields(route, stage, evidence, success, blocked=()):
    side, _, leaf = stage.partition('.')
    if leaf not in ('plan', 'e2e', 'acceptance'):
        return None
    own = set(_tasks(route, side)) - set(blocked)
    backend = _tasks(route, 'backend')
    combined = side == 'frontend' and route['scope'] == 'fullstack'
    joint = ({key for key, task in backend.items() if task.get('joint_acceptance')} - set(blocked)
             if combined else set())
    if leaf == 'plan' and success:
        mapping = evidence.get('task_repositories')
        if not isinstance(mapping, dict) or set(mapping) != own | joint:
            return 'task_repositories must cover each owned task and backend tasks requiring joint acceptance'
        planned = evidence.get('applications' if side == 'frontend' else 'services')
        if not isinstance(planned, list) or not all(_nonblank(k) for k in planned):
            return 'Supply this side’s planned repository keys'
        for keys in mapping.values():
            if (not isinstance(keys, list) or not keys or not all(_nonblank(k) for k in keys)
                    or len(set(keys)) != len(keys) or set(keys) - set(planned)):
                return 'Each task needs nonempty, unique repository keys from the side plan'
        if set(planned) != {key for keys in mapping.values() for key in keys}:
            return 'Associate every planned repository with its affected tasks'
    if leaf in ('e2e', 'acceptance'):
        results = evidence.get('task_results')
        if results is None and not success:
            return None
        allowed = own | (set(backend) - set(blocked) if combined else set())
        required = own | joint
        if not isinstance(results, dict) or set(results) - allowed or (success and required - set(results)):
            return 'task_results must identify current tasks and cover all required tasks on success'
        for result in results.values():
            if (not isinstance(result, dict) or result.get('outcome') not in ('passed', 'failed', 'pending')
                    or not _nonblank(result.get('verification')) or (success and result['outcome'] != 'passed')):
                return 'Provide each task outcome and independent verification; successful stages require passes'
    return None


def validate_merge_tests(mapping, changed, reviews, success):
    if (not isinstance(reviews, dict) or set(reviews) - mapping.keys()
            or not all(_nonblank(value) for value in reviews.values())):
        return 'merged_task_verification must map planned task IDs to final-version verification evidence'
    affected = {key for key, repositories in mapping.items() if set(changed).intersection(repositories)}
    if success and affected - reviews.keys():
        return 'Reverify the final merged versions of these tasks: ' + ', '.join(sorted(affected - reviews.keys()))
    return None


def prepare_receipt(store, item, stage, evidence, success, route=None, *, task_blockers=None):
    """Validate and enrich a receipt before the existing transaction records the stage."""
    import task_sync
    from branch_check import git
    from route_evidence import frontend_acceptance_versions, repository_evidence
    from route_execution import blocked_tasks, handoff_tasks, read_history, stage_evidence

    if not task_sync.enabled(item):
        return None
    if stage == 'split' and success:
        for side, tasks in route['tasks'].items():
            for task in tasks:
                joint = task.get('joint_acceptance', False)
                if type(joint) is not bool or (joint and (side != 'backend' or route['scope'] != 'fullstack')):
                    return 'joint_acceptance is a boolean for backend tasks requiring fullstack verification'
        # Registration must remain atomic even when invalid revisions return an evidence response.
        store.conn.execute('SAVEPOINT register_tasks')
        try:
            task_sync.register_split(store, item, route)
        except ValueError as error:
            store.conn.execute('ROLLBACK TO register_tasks')
            return str(error)
        finally:
            store.conn.execute('RELEASE register_tasks')
        return None
    side, _, leaf = stage.partition('.')
    if leaf not in ('plan', 'e2e', 'acceptance', 'integration') or not isinstance(evidence, dict):
        return None
    route, stages = read_history(store, item)
    if task_blockers:
        stages[stage] = {'task_blockers': task_blockers}
    blocked = blocked_tasks(route, stages)
    reason = validate_task_fields(route, stage, evidence, success, blocked)
    if reason:
        return reason
    # The existing successful acceptance path checks these versions. Failed stages can
    # still contribute independent passed tasks, so they need the same version checks.
    if not success and leaf in ('e2e', 'acceptance') and evidence.get('task_results'):
        coding = stage_evidence(store, item, side + '.coding') or {}
        expected = frontend_acceptance_versions(store, item, route) if leaf == 'e2e' else coding.get('versions')
        if not expected or expected != evidence.get('versions'):
            return 'Partial acceptance must use the current candidate versions'
        if (leaf == 'e2e' and route['scope'] == 'fullstack'
                and (handoff_tasks(route) - blocked.keys() or stage_evidence(store, item, 'handoff'))):
            handoff = stage_evidence(store, item, 'handoff') or {}
            if not handoff or evidence.get('backend_versions') != handoff.get('backend_versions'):
                return 'Partial joint acceptance needs the latest callable backend versions'
        return repository_evidence(store, item, side, leaf, evidence)
    if leaf == 'integration':
        merged = evidence.get('merged_versions') or {}
        targets = (stage_evidence(store, item, side + '.branch_check') or {}).get('repositories', {})
        changed = []
        try:
            for key, target_sha in merged.items():
                candidate = evidence['versions'][key]
                source = targets[key]['source_root']
                if candidate != target_sha and git(source, 'rev-parse', candidate + '^{tree}') != git(
                        source, 'rev-parse', target_sha + '^{tree}'):
                    changed.append(key)
        except (ValueError, OSError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
            return f'Cannot verify per-task merged trees: {error}'
        # Never trust a writer-supplied claim about tree equivalence.
        evidence['task_changed_merges'] = changed
        reviews = evidence.get('merged_task_verification', {})
        plan = stage_evidence(store, item, side + '.plan') or {}
        defer = side == 'frontend' and route.get('post_merge_e2e') and 'frontend.e2e' in route['stages']
        return validate_merge_tests(plan.get('task_repositories', {}), changed, reviews, success and not defer)
    return None


def _receipt(stages, stage, *, partial=False):
    entry = stages.get(stage, {})
    evidence = entry.get('evidence')
    if isinstance(evidence, dict) and (partial or entry.get('outcome') == 'success'):
        return evidence
    return {}


def _side_result(stages, side, task, facts, previous, *, skip_e2e=False, post_merge=False):
    plan = _receipt(stages, side + '.plan').get('task_repositories', {})
    repositories = plan.get(task, [])
    if not repositories:
        return '等待任务与仓库对应关系'
    test_stage = side + ('.e2e' if side == 'frontend' else '.acceptance')
    test = _receipt(stages, test_stage, partial=True)
    result = test.get('task_results', {}).get(task, {})
    merged = _receipt(stages, side + '.integration', partial=True)
    current = (merged.get('merged_versions', {}) if post_merge and side == 'frontend' and not skip_e2e
               else _receipt(stages, side + '.coding').get('versions', {}))
    versions = {key: current.get(key) for key in repositories}
    prior = previous.get(side, {})
    if prior.get('versions') != versions or set(prior.get('repositories', [])) != set(repositories):
        prior = {}
    backend_versions = test.get('backend_versions')
    same_joint = not backend_versions or backend_versions == prior.get('backend_versions')
    component = {'repositories': repositories, 'versions': versions,
                 'tested': {}, 'merged': dict(prior.get('merged', {})),
                 'merge_tested': dict(prior.get('merge_tested', {}))}
    if not skip_e2e:
        if result:
            component.update(acceptance=result, tested={k: test.get('versions', {}).get(k) for k in repositories},
                             verified_at=stages.get(test_stage, {}).get('ended_at'), backend_versions=backend_versions)
        elif same_joint:
            component.update({k: prior[k] for k in ('acceptance', 'tested', 'verified_at', 'backend_versions')
                              if k in prior})
    facts[side] = component
    for key in repositories:
        merged_sha = merged.get('merged_versions', {}).get(key)
        same_tree = merged.get('versions', {}).get(key) == versions[key]
        verified_after_merge = post_merge and side == 'frontend' and not skip_e2e
        if merged_sha and versions[key] and (verified_after_merge or same_tree):
            component['merged'][key] = merged_sha
            component['merge_tested'][key] = (
                component.get('tested', {}).get(key) == merged_sha if verified_after_merge
                else key not in merged.get('task_changed_merges', []) or
                bool(merged.get('merged_task_verification', {}).get(task))
            )
    if skip_e2e:
        unit = _receipt(stages, side + '.unit_test').get('versions', {})
        review = _receipt(stages, side + '.code_review').get('versions', {})
        if any(not versions[key] or unit.get(key) != versions[key] or review.get(key) != versions[key]
               for key in repositories):
            return '当前候选版本的单测或代码审查待通过'
    elif component.get('acceptance', {}).get('outcome') != 'passed':
        return '独立验收未通过或待验证'
    for key in repositories:
        if not skip_e2e and (not versions[key] or component['tested'].get(key) != versions[key]):
            return f'{key} 当前候选版本待验证'
        if not component['merged'].get(key):
            return f'{key} 待合回验收版本'
        if not component['merge_tested'].get(key):
            return f'{key} 最终内容变化，待逐项复验'
    return None


def _push_result(route, stages, side, facts):
    stage = side + '.push'
    if stage not in route.get('stages', ()):
        return None
    receipts = _receipt(stages, stage, partial=True).get('repositories', {})
    pending = []
    for key, sha in facts[side]['merged'].items():
        entry = receipts.get(key, {})
        if entry.get('sha') != sha or not COMMIT.fullmatch(entry.get('remote_sha') or ''):
            pending.append(key)
    if pending:
        facts.setdefault('push_pending', {})[side] = pending
        return '已合回版本待远端核验 ' + '、'.join(pending)


def verified_task_merge(stages, side, task, *, skip_e2e=False):
    facts = {}
    if _side_result(stages, side, task, facts, {}, skip_e2e=skip_e2e):
        return {}
    return facts[side]['merged']


def evaluate(route, stages, history=None, *, deployment_targets=None):
    from route_evidence import pushed_backend_versions
    from route_execution import blocked_tasks, handoff_tasks

    decisions = {}
    targets = deployment_targets or {}
    deployment = _receipt(stages, DEPLOY_STAGE, partial=True).get('services')
    if not isinstance(deployment, dict):
        deployment = {}
    e2e_enabled = route.get('e2e_enabled', True)
    handoff = _receipt(stages, 'handoff').get('backend_versions')
    joint_test = _receipt(stages, 'frontend.e2e', partial=True)
    backend_merge = _receipt(stages, 'backend.integration', partial='backend.push' in route.get('stages', ())).get(
        'merged_versions', {})
    backend_merge = pushed_backend_versions(route, stages, backend_merge)
    current_joint = bool(handoff and isinstance(backend_merge, dict) and handoff == joint_test.get('backend_versions')
                         and all(backend_merge.get(key) == sha for key, sha in handoff.items())
                         and (handoff == backend_merge or stages.get(DEPLOY_STAGE, {}).get('task_blockers')))
    blocked = blocked_tasks(route, stages)
    joint_tasks = handoff_tasks(route)
    scoped = bool(blocked or (stages.get('handoff') or {}).get('outcome') == 'failed')
    for side, tasks in route['tasks'].items():
        for task in tasks:
            key = task['id']
            facts = {'criteria': task.get('acceptance', []), 'joint': task.get('joint_acceptance', False),
                     'dependencies': task.get('depends_on', [])}
            if key in blocked:
                facts['blocked'] = blocked[key]
                decisions[key] = ('open', blocked[key]['reason'], facts)
                continue
            previous = (history or {}).get(key, {})
            if any(previous.get(field) != value for field, value in facts.items()):
                previous = {}
            skip_e2e = side == 'frontend' and not e2e_enabled
            reason = _side_result(stages, side, key, facts, previous, skip_e2e=skip_e2e,
                                  post_merge=route.get('post_merge_e2e', False))
            if not reason:
                reason = _push_result(route, stages, side, facts)
            if side == 'backend' and not reason and DEPLOY_STAGE in route.get('stages', []):
                component = facts[side]
                for service in sorted(set(component['repositories']) & targets.keys()):
                    receipt = deployment.get(service)
                    if deployed_receipt_matches(receipt, service, component['merged'][service], targets[service]):
                        facts.setdefault('deployment', {})[service] = receipt
                    else:
                        facts.setdefault('deployment_pending', []).append(service)
                if facts.get('deployment_pending'):
                    reason = '已合回版本待部署核验 ' + '、'.join(facts['deployment_pending'])
            if route['scope'] == 'fullstack' and e2e_enabled:
                joint_result = joint_test.get('task_results', {}).get(key, {})
                if current_joint and joint_result.get('outcome') == 'failed':
                    facts['joint_failure'] = joint_result
                    facts['joint_failure_at'] = stages.get('frontend.e2e', {}).get('ended_at')
                    reason = '联合验收发现该项原验收条件未满足'
                elif not reason and ((side == 'frontend' and (not scoped or key in joint_tasks))
                                     or task.get('joint_acceptance')):
                    if not current_joint:
                        reason = '等待有效的前后端版本联合验收'
                    elif side == 'backend':
                        reason = _side_result(stages, 'frontend', key, facts, previous,
                                              post_merge=route.get('post_merge_e2e', False))
                        if not reason:
                            reason = _push_result(route, stages, 'frontend', facts)
            if skip_e2e:
                facts['e2e_skipped'] = True
                closed_reason = '单测和代码审查通过且相关仓库已合回，页面验收按项目配置跳过'
            elif not e2e_enabled and route['scope'] == 'fullstack' and facts['joint']:
                facts['e2e_skipped'] = True
                closed_reason = '后端服务验收通过且相关仓库已合回，页面联合验收按项目配置跳过'
            else:
                closed_reason = '独立验收通过且相关仓库已合回'
            if side + '.push' in route.get('stages', []):
                closed_reason += '，已核验 Git 远端'
            if facts.get('deployment'):
                closed_reason += '，已核验相关服务部署'
            decisions[key] = ('open' if reason else 'closed', reason or closed_reason, facts)
    return decisions


def progress_note(task, side, facts, stages, blocked):
    """Describe an evidenced blocker without turning a dependency wait into an actual start."""
    block = facts.get('blocked')
    if not block:
        for stage in ('frontend.e2e', 'backend.acceptance'):
            receipt = stages.get(stage, {})
            result = _receipt(stages, stage, partial=True).get('task_results', {}).get(task, {})
            if receipt.get('terminal_step') and result.get('outcome') in ('failed', 'pending'):
                return '联调或验收待通过：' + result['verification'][:300] + '。待问题处理后复验。'
        return None
    dependencies = block.get('depends_on')
    if dependencies:
        reasons = ['%s（%s）' % (key, blocked[key]['reason']) for key in dependencies]
        return '等待前置任务：' + '、'.join(reasons) + '。前置任务恢复后继续开发或联调。'
    if block.get('stage') in (DEPLOY_STAGE, 'frontend.deploy'):
        repositories = _receipt(stages, side + '.plan').get('task_repositories', {}).get(task, [])
        merged = _receipt(stages, side + '.integration', partial=True).get('merged_versions', {})
        versions = [f'{repository} {merged[repository][:12]}' for repository in repositories
                    if merged.get(repository)]
        if versions:
            prefix = '开发已完成，代码已合回（' + '、'.join(versions) + '）。'
        else:
            prefix = ''
        services = _receipt(stages, block['stage'], partial=True).get('services') or {}
        failures = [f'{repository}：{services[repository]["reason"][:200]}' for repository in repositories
                    if repository in services and services[repository].get('reason')]
        kind = '后端' if side == 'backend' else '前端'
        return prefix + '阻塞于' + kind + '部署：' + '；'.join(failures or [block['reason']]) + '。待部署核验通过后继续。'
    return '任务受阻：' + block['reason'] + '。解除阻塞后继续。'


def refresh(store, item):
    import task_sync
    from deploy_state import configured_targets
    from route_execution import read_history, snapshot

    if not task_sync.enabled(item):
        return
    route, stages = read_history(store, item)
    if not route:
        return
    prior = {row['task_key']: row for row in store.conn.execute(
        '''SELECT e.*,t.side,t.remote_status FROM task_evidence e JOIN split_tasks t USING(item_seq,task_key)
           WHERE e.item_seq=?''', (item['seq'],))}
    history = {key: json.loads(row['evidence']) for key, row in prior.items()}
    targets = configured_targets(snapshot(item)) if DEPLOY_STAGE in route['stages'] else {}
    decisions = evaluate(route, stages, history, deployment_targets=targets)
    progress = task_sync.progress_enabled(item)
    blocked = {key: facts['blocked'] for key, (_, _, facts) in decisions.items() if 'blocked' in facts}
    sides = {task['id']: side for side, tasks in route['tasks'].items() for task in tasks}
    for task, (desired, reason, facts) in decisions.items():
        previous = prior.get(task)
        if previous:
            old = history[task]
            side = previous['side']
            acceptance = facts.get(side, {}).get('acceptance', {})
            same_criteria = all(old.get(field) == facts[field] for field in ('criteria', 'joint', 'dependencies'))
            failed_at = facts.get('joint_failure_at') or old.get('joint_failure_at')
            if failed_at and desired == 'closed' and (facts.get(side, {}).get('verified_at') or '') <= failed_at:
                desired, reason = 'open', '等待联调缺陷返工后的独立复验'
            if failed_at and desired != 'closed':
                facts['joint_failure_at'] = failed_at
            # A revision adding another requirement starts a new route history. Preserve
            # a confirmed delivery until its own criterion changes or actually fails.
            versions = facts.get(side, {}).get('versions', {})
            changed_versions = any(value and value != old.get(side, {}).get('versions', {}).get(key)
                                   for key, value in versions.items())
            changed_scope = any(
                component in facts and set(facts[component]['repositories']) != set(old.get(component, {}).get('repositories', []))
                for component in ('backend', 'frontend'))
            if (previous['disposition'] == 'closed' and (previous['remote_status'] == 'closed' or not changed_versions)
                    and same_criteria and not changed_scope and desired == 'open' and not facts.get('joint_failure')
                    and not facts.get('deployment_pending') and not facts.get('push_pending') and not facts.get('blocked')
                    and acceptance.get('outcome') != 'failed'):
                continue
        store.conn.execute('''INSERT INTO task_evidence(item_seq,task_key,disposition,reason,evidence)
            VALUES(?,?,?,?,?) ON CONFLICT(item_seq,task_key) DO UPDATE SET disposition=excluded.disposition,
            reason=excluded.reason,evidence=excluded.evidence''',
                           (item['seq'], task, desired, reason, json.dumps(facts, ensure_ascii=False)))
        task_sync.set_desired(store, item, task, desired)
        if desired == 'open' and progress:
            note = progress_note(task, sides[task], facts, stages, blocked)
            if note:
                task_sync.record_note(store, item, task, note)


def first_unverified_stage(store, item):
    import task_sync
    from route_execution import get_split

    if not task_sync.enabled(item):
        return None
    task = store.conn.execute('''SELECT t.side,e.evidence FROM split_tasks t LEFT JOIN task_evidence e
        USING(item_seq,task_key) WHERE t.item_seq=? AND COALESCE(e.disposition,'open') <> 'closed'
        ORDER BY t.side,t.task_key LIMIT 1''', (item['seq'],)).fetchone()
    if not task:
        return None
    facts = json.loads(task['evidence']) if task['evidence'] else {}
    if facts.get('push_pending'):
        return next(iter(facts['push_pending'])) + '.push'
    if task['side'] == 'backend':
        return DEPLOY_STAGE if facts.get('deployment_pending') else 'backend.acceptance'
    route = get_split(store, item)
    return 'frontend.integration' if route and not route.get('e2e_enabled', True) else 'frontend.e2e'
