"""Per-task acceptance and merge decisions derived from the effective workflow receipts."""

import json
import subprocess


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


def validate_task_fields(route, stage, evidence, success):
    side, _, leaf = stage.partition('.')
    if leaf not in ('plan', 'e2e', 'acceptance'):
        return None
    own = set(_tasks(route, side))
    backend = _tasks(route, 'backend')
    combined = side == 'frontend' and route['scope'] == 'fullstack'
    joint = {key for key, task in backend.items() if task.get('joint_acceptance')} if combined else set()
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
        allowed = own | (set(backend) if combined else set())
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


def prepare_receipt(store, item, stage, evidence, success, route=None):
    """Validate and enrich a receipt before the existing transaction records the stage."""
    import task_sync
    from branch_check import git
    from route_evidence import repository_evidence
    from route_execution import get_split, stage_evidence

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
    route = get_split(store, item)
    reason = validate_task_fields(route, stage, evidence, success)
    if reason:
        return reason
    # The existing successful acceptance path checks these versions. Failed stages can
    # still contribute independent passed tasks, so they need the same version checks.
    if not success and leaf in ('e2e', 'acceptance') and evidence.get('task_results'):
        coding = stage_evidence(store, item, side + '.coding') or {}
        if not coding or coding.get('versions') != evidence.get('versions'):
            return 'Partial acceptance must use the current candidate versions'
        if leaf == 'e2e' and route['scope'] == 'fullstack':
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
        return validate_merge_tests(plan.get('task_repositories', {}), changed, reviews, success)
    return None


def _receipt(stages, stage, *, partial=False):
    entry = stages.get(stage, {})
    evidence = entry.get('evidence')
    if isinstance(evidence, dict) and (partial or entry.get('outcome') == 'success'):
        return evidence
    return {}


def _side_result(stages, side, task, facts, previous):
    plan = _receipt(stages, side + '.plan').get('task_repositories', {})
    repositories = plan.get(task, [])
    if not repositories:
        return '等待任务与仓库对应关系'
    test_stage = side + ('.e2e' if side == 'frontend' else '.acceptance')
    test = _receipt(stages, test_stage, partial=True)
    result = test.get('task_results', {}).get(task, {})
    current = _receipt(stages, side + '.coding').get('versions', {})
    versions = {key: current.get(key) for key in repositories}
    prior = previous.get(side, {})
    if prior.get('versions') != versions or set(prior.get('repositories', [])) != set(repositories):
        prior = {}
    backend_versions = test.get('backend_versions')
    same_joint = not backend_versions or backend_versions == prior.get('backend_versions')
    component = {'repositories': repositories, 'versions': versions,
                 'tested': {}, 'merged': dict(prior.get('merged', {})),
                 'merge_tested': dict(prior.get('merge_tested', {}))}
    if result:
        component.update(acceptance=result, tested={k: test.get('versions', {}).get(k) for k in repositories},
                         verified_at=stages.get(test_stage, {}).get('ended_at'), backend_versions=backend_versions)
    elif same_joint:
        component.update({k: prior[k] for k in ('acceptance', 'tested', 'verified_at', 'backend_versions') if k in prior})
    facts[side] = component
    merged = _receipt(stages, side + '.integration', partial=True)
    for key in repositories:
        merged_sha = merged.get('merged_versions', {}).get(key)
        if merged_sha and versions[key] and merged.get('versions', {}).get(key) == versions[key]:
            component['merged'][key] = merged_sha
            component['merge_tested'][key] = (key not in merged.get('task_changed_merges', [])
                                               or bool(merged.get('merged_task_verification', {}).get(task)))
    if component.get('acceptance', {}).get('outcome') != 'passed':
        return '独立验收未通过或待验证'
    for key in repositories:
        if not versions[key] or component['tested'].get(key) != versions[key]:
            return f'{key} 当前候选版本待验证'
        if not component['merged'].get(key):
            return f'{key} 待合回验收版本'
        if not component['merge_tested'].get(key):
            return f'{key} 最终内容变化，待逐项复验'
    return None


def evaluate(route, stages, history=None):
    decisions = {}
    handoff = _receipt(stages, 'handoff').get('backend_versions')
    joint_test = _receipt(stages, 'frontend.e2e', partial=True)
    backend_merge = _receipt(stages, 'backend.integration').get('merged_versions')
    current_joint = bool(handoff and handoff == backend_merge == joint_test.get('backend_versions'))
    for side, tasks in route['tasks'].items():
        for task in tasks:
            key = task['id']
            facts = {'criteria': task.get('acceptance', []), 'joint': task.get('joint_acceptance', False),
                     'dependencies': task.get('depends_on', [])}
            previous = (history or {}).get(key, {})
            if any(previous.get(field) != value for field, value in facts.items()):
                previous = {}
            reason = _side_result(stages, side, key, facts, previous)
            if route['scope'] == 'fullstack':
                joint_result = joint_test.get('task_results', {}).get(key, {})
                if current_joint and joint_result.get('outcome') == 'failed':
                    facts['joint_failure'] = joint_result
                    facts['joint_failure_at'] = stages.get('frontend.e2e', {}).get('ended_at')
                    reason = '联合验收发现该项原验收条件未满足'
                elif not reason and (side == 'frontend' or task.get('joint_acceptance')):
                    if not current_joint:
                        reason = '等待有效的前后端版本联合验收'
                    elif side == 'backend':
                        reason = _side_result(stages, 'frontend', key, facts, previous)
            decisions[key] = ('open' if reason else 'closed', reason or '独立验收通过且相关仓库已合回', facts)
    return decisions


def refresh(store, item):
    import task_sync
    from route_execution import read_history

    if not task_sync.enabled(item):
        return
    route, stages = read_history(store, item)
    if not route:
        return
    prior = {row['task_key']: row for row in store.conn.execute(
        '''SELECT e.*,t.side,t.remote_status FROM task_evidence e JOIN split_tasks t USING(item_seq,task_key)
           WHERE e.item_seq=?''', (item['seq'],))}
    history = {key: json.loads(row['evidence']) for key, row in prior.items()}
    for task, (desired, reason, facts) in evaluate(route, stages, history).items():
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
                    and acceptance.get('outcome') != 'failed'):
                continue
        store.conn.execute('''INSERT INTO task_evidence(item_seq,task_key,disposition,reason,evidence)
            VALUES(?,?,?,?,?) ON CONFLICT(item_seq,task_key) DO UPDATE SET disposition=excluded.disposition,
            reason=excluded.reason,evidence=excluded.evidence''',
                           (item['seq'], task, desired, reason, json.dumps(facts, ensure_ascii=False)))
        task_sync.set_desired(store, item, task, desired)


def first_unverified_stage(store, item):
    import task_sync

    if not task_sync.enabled(item):
        return None
    task = store.conn.execute('''SELECT t.side FROM split_tasks t LEFT JOIN task_evidence e
        USING(item_seq,task_key) WHERE t.item_seq=? AND COALESCE(e.disposition,'open') <> 'closed'
        ORDER BY t.side,t.task_key LIMIT 1''', (item['seq'],)).fetchone()
    return ('backend.acceptance' if task['side'] == 'backend' else 'frontend.e2e') if task else None
