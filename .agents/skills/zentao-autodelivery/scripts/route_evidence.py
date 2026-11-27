"""Version-bound evidence for a scoped work item, including its backend handoff."""

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from branch_check import git, planned_applications
from deploy_state import (COMMIT, DEPLOY_STAGE, DIGEST, FRONTEND_DEPLOY_STAGE, FRONTEND_PROFILE, GIT_PUSH_STAGES, TAG,
                          configured_targets, deployed_receipt_matches, frontend_release_config,
                          rollout_matches, release_verification)
from route_execution import (blocked_tasks, contract_revision_pending, get_split, handoff_tasks,
                             read_history, repository_plan, snapshot, stage_evidence)
from task_evidence import verified_task_merge


def nonblank(value):
    return isinstance(value, str) and bool(value.strip())


def commit_map(value):
    return isinstance(value, dict) and bool(value) and all(
        nonblank(key) and isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha)
        for key, sha in value.items())


def frontend_evidence_root(store, config_directory, marker):
    if store is not None:
        filename = store.conn.execute('PRAGMA database_list').fetchone()[2]
        if filename:
            return Path(filename).resolve().parent / 'items' / marker
    return (config_directory.parent / '.local' / 'zentao-delivery-v2' / 'items' / marker).resolve()


def agentlimb_receipt_error(store, item, attempt_id, evidence, expected, config_directory):
    path = evidence.get('agentlimb_receipt') if isinstance(evidence, dict) else None
    if not nonblank(path) or not config_directory:
        return 'Provide the AgentLimb runner JSON receipt for this attempt'
    project = config_directory.parent
    root = frontend_evidence_root(store, config_directory, item['marker'])
    try:
        receipt_path = (project / path).resolve(strict=True)
        if not receipt_path.is_relative_to(root):
            return 'AgentLimb receipt must be inside the current work-item evidence directory'
        receipt = json.loads(receipt_path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return 'AgentLimb receipt could not be read'
    route = get_split(store, item)
    versions = frontend_acceptance_versions(store, item, route)
    if (route or {}).get('post_merge_e2e'):
        expected_path = root / 'frontend' / f'e2e-{attempt_id}' / 'agentlimb.json'
        host = receipt.get('host_run', {}) if isinstance(receipt, dict) else {}
        stop = host.get('stop_evidence', {}) if isinstance(host, dict) else {}
        if (receipt_path != expected_path or not isinstance(host, dict) or host.get('stop_confirmed') is not True
                or not isinstance(stop, dict) or type(stop.get('owned_processes_active_after_stop')) is not int
                or stop['owned_processes_active_after_stop'] != 0):
            return 'Provide the verified host receipt with tested process cleanup for this E2E attempt'
    if (not isinstance(receipt, dict) or receipt.get('runner') != 'agentlimb' or receipt.get('marker') != item['marker']
            or receipt.get('attempt_id') != attempt_id or receipt.get('status') != expected
            or receipt.get('stage') != 'acceptance' or not commit_map(versions) or receipt.get('versions') != versions):
        return 'AgentLimb receipt must match the active attempt, candidate versions and result'
    resources = receipt.get('candidate_resources')
    post_merge = (route or {}).get('post_merge_e2e')
    automatic = (post_merge and 'config_snapshot' in item.keys()
                 and snapshot(item).get('e2e_attestation') == 'launch')
    if (not (automatic or receipt.get('checks')) or not isinstance(resources, list) or not resources
            or any(not isinstance(row, dict) or not nonblank(row.get('app')) for row in resources)
            or {row['app'] for row in resources} != versions.keys()):
        return 'AgentLimb receipt must include checks and observed resources for every candidate application'
    if automatic:
        if (receipt.get('version_attestation') != 'launch' or len(resources) != len(versions)
                or not all(loopback_resource(row.get('url')) for row in resources)):
            return 'Host E2E must attest a loaded resource for each application from its own launch'
    elif post_merge and any(
        not nonblank(row.get('url')) or not isinstance(row.get('sha256'), str)
        or not re.fullmatch(r'[0-9a-f]{64}', row['sha256']) for row in resources
    ):
        return 'Host E2E must attest resource addresses and SHA-256 digests observed by the browser'
    if (route or {}).get('post_merge_e2e'):
        cases = receipt.get('scenario_cases')
        if (not isinstance(cases, list) or not cases or any(
                not isinstance(case, dict) or not nonblank(case.get('name'))
                or case.get('status') not in ('passed', 'failed', 'blocked') for case in cases
        ) or len({case['name'] for case in cases}) != len(cases)):
            return 'Host E2E must record all declared business scenario outcomes'
        states = {case['status'] for case in cases}
        if (expected == 'passed' and states != {'passed'}) or (expected == 'failed' and 'failed' not in states):
            return 'The formal E2E result must agree with recorded business case outcomes'
    if expected == 'failed' and receipt.get('repair_required') is not True:
        return 'Only a confirmed AgentLimb business assertion failure can trigger code repair'


def loopback_resource(value):
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme in ('http', 'https') and url.hostname in ('localhost', '127.0.0.1', '::1')
                and url.username is None and url.password is None and bool(url.path) and not url.fragment)
    except ValueError:
        return False


def host_e2e_blocker_error(item, attempt_id, evidence, config_directory, outcome, *, store=None):
    """Keep an active host attempt separate from a terminal environment blocker."""
    if config_directory is None:
        return 'A formal E2E attempt needs its project evidence directory'
    folder = frontend_evidence_root(store, config_directory, item['marker']) / 'frontend'
    folder = folder / f'e2e-{attempt_id}'
    request = folder / 'host-request.json'
    receipt_path = folder / 'agentlimb.json'
    try:
        if not request.is_file() and not (folder / 'host-run').exists() and not receipt_path.is_file():
            if outcome in ('skipped', 'timed_out'):
                return None
            blocker = evidence.get('e2e_blocker', {}) if isinstance(evidence, dict) else {}
            if (isinstance(blocker, dict) and nonblank(blocker.get('stage')) and nonblank(blocker.get('reason'))
                    and blocker.get('cases') == 'not-run'):
                return None
            return 'Record the E2E blocker, reason and cases not run, or provide the completed host receipt'
        receipt = json.loads(receipt_path.read_text(encoding='utf-8-sig'))
        if (not isinstance(receipt, dict) or receipt.get('attempt_id') != attempt_id
                or receipt.get('marker') != item['marker'] or receipt.get('runner') != 'agentlimb'):
            return 'The active host E2E request requires its matching host receipt'
        if (folder / 'host-run' / attempt_id / 'e2e-host').exists():
            host = receipt.get('host_run')
            if (not isinstance(host, dict) or host.get('stop_confirmed') is not True
                    or (host.get('stop_evidence') or {}).get('owned_processes_active_after_stop') != 0):
                return 'Finish the host E2E and confirm that its processes stopped'
        if receipt.get('status') == 'blocked':
            return None
        if receipt.get('status') == 'failed':
            return 'Record the confirmed business assertion with repair_required and the formal E2E receipt'
        return 'The recorded E2E outcome must match the completed host receipt'
    except (OSError, ValueError, TypeError, AttributeError):
        return 'Wait for the host E2E result and process cleanup before finishing the attempt'


def check_candidate(proof, candidate):
    tree = proof['worktree']
    if git(tree, 'symbolic-ref', '--quiet', 'HEAD', allowed=(0, 1)) != 'refs/heads/' + proof['worktree_branch']:
        return 'The worktree is no longer on the verified item branch'
    if git(tree, 'rev-parse', 'HEAD') != candidate:
        return 'Candidate must match the verified worktree HEAD'
    if git(tree, '--no-optional-locks', 'status', '--porcelain=v1', '--untracked-files=no', '--ignore-submodules=untracked'):
        return 'Commit or reconcile tracked and staged item worktree changes before verifying this candidate'
    if git(tree, 'merge-base', '--is-ancestor', proof['base_commit'], candidate, allowed=(0, 1)) is None:
        return 'Candidate does not descend from the registered baseline'
    source = proof['source_root']
    if git(source, 'rev-parse', '--verify', 'refs/heads/' + proof['worktree_branch']) != candidate:
        return 'Candidate does not match the item branch in the registered repository'


def check_merged_commit(proof, candidate, merged):
    source = proof['source_root']
    actual = git(source, 'rev-parse', '--verify', 'refs/heads/' + proof['target_branch'])
    if merged != actual or git(source, 'merge-base', '--is-ancestor', candidate, actual, allowed=(0, 1)) is None:
        return 'Merged version must be the actual target tip and contain the candidate'


def check_merge(proof, candidate, merged, reverified):
    reason = check_merged_commit(proof, candidate, merged)
    if reason:
        return reason
    source = proof['source_root']
    if candidate != merged and not reverified:
        if git(source, 'rev-parse', candidate + '^{tree}') != git(source, 'rev-parse', merged + '^{tree}'):
            return 'Record merged_verification for a merge that changed the tested tree'


def host_merge_receipt_error(store, item, side, versions, merged):
    # Historical integrations can predate host operations. Where a host request
    # exists in this attempt its per-repository terminal outcome is authoritative.
    outstanding = set(merged)
    rows = store.conn.execute("""SELECT g.request, g.result FROM development_git_operations g
        JOIN attempts a ON a.attempt_id=g.attempt_id WHERE g.item_seq=? AND g.kind='integrate'
        AND a.run_round=? AND a.ended_at IS NULL ORDER BY g.created_at DESC,g.rowid DESC""",
                              (item['seq'], item['run_round']))
    for row in rows:
        context = json.loads(row['request'])['context']
        if context['side'] != side:
            continue
        actual = json.loads(row['result'])['repositories'] if row['result'] else {}
        for key in outstanding.intersection(context['repositories']):
            repo = actual.get(key, {})
            if (context['repositories'][key]['candidate'] != versions[key] or repo.get('status') != 'verified'
                    or repo.get('merged') != merged[key]):
                return f'{key}: a verified host integration receipt is required for this revision'
            outstanding.remove(key)
        if not outstanding:
            break


def validate_partial_merge(store, item, stage, evidence):
    """Verify successful repositories in a failed or interrupted normal/repair merge."""
    if not stage.endswith('.integration') or not isinstance(evidence, dict) or 'merged_versions' not in evidence:
        return None
    merged = evidence['merged_versions']
    if merged == {}:
        return None
    side = stage.split('.')[0]
    targets = (stage_evidence(store, item, side + '.branch_check') or {}).get('repositories', {})
    candidates = (stage_evidence(store, item, side + '.coding') or {}).get('versions', {})
    versions = evidence.get('versions')
    if (not commit_map(merged) or not commit_map(versions) or not merged.keys() <= versions.keys()
            or not merged.keys() <= targets.keys() or any(candidates.get(k) != sha for k, sha in versions.items())):
        return 'Partial merge receipts must identify registered repositories and their recorded candidates'
    if reason := host_merge_receipt_error(store, item, side, versions, merged):
        return reason
    try:
        for key, sha in merged.items():
            reason = check_merged_commit(targets[key], versions[key], sha)
            if reason:
                return f'{key}: {reason}'
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired) as error:
        return f'Git evidence could not be verified: {error}'


def repository_evidence(store, item, side, stage, evidence):
    branches = stage_evidence(store, item, side + '.branch_check') or {}
    targets = branches.get('repositories', {})
    versions = evidence.get('versions')
    if not commit_map(versions) or versions.keys() != targets.keys():
        return 'Provide a candidate commit for every repository in the verified branch check'
    merged = evidence.get('merged_versions')
    if stage == 'integration':
        candidate = stage_evidence(store, item, side + '.coding') or {}
        if versions != candidate.get('versions') or not commit_map(merged) or merged.keys() != targets.keys():
            return 'Merge evidence must cover the recorded candidates and every target repository'
        if reason := host_merge_receipt_error(store, item, side, versions, merged):
            return reason
    route = get_split(store, item)
    post_merge = side == 'frontend' and (route or {}).get('post_merge_e2e')
    defer_page_checks = post_merge and 'frontend.e2e' in route['stages']
    effective = (stage_evidence(store, item, 'frontend.integration') or {}) if post_merge else {}
    try:
        for key, proof in targets.items():
            if stage == 'integration':
                reverified = defer_page_checks or nonblank(evidence.get('merged_verification'))
                reason = check_merge(proof, versions[key], merged[key], reverified)
            elif post_merge and (stage == 'e2e' or effective.get('main_workspace_repair')):
                reason = check_merged_commit(proof, versions[key], versions[key])
            else:
                reason = check_candidate(proof, versions[key])
            if reason:
                return f'{key}: {reason}'
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired) as error:
        return f'Git evidence could not be verified: {error}'


def frontend_acceptance_versions(store, item, route):
    if (route or {}).get('post_merge_e2e'):
        return (stage_evidence(store, item, 'frontend.integration') or {}).get('merged_versions')
    return (stage_evidence(store, item, 'frontend.coding') or {}).get('versions')


def frontend_acceptance_terminal(route, stages, merged):
    if 'frontend.e2e' not in route['stages']:
        return True
    e2e = stages.get('frontend.e2e', {})
    return e2e.get('terminal_step') is True and e2e.get('release_versions') == merged


def git_push_requirements(store, item, stage):
    route, history = read_history(store, item)
    side = stage.split('.')[0]
    if not route or stage not in route['stages']:
        return {}, {}, {}, '工单未启用该端 Git 推送'
    if contract_revision_pending(history):
        return {}, {}, {}, '接口契约变更，等待重新拆分后核验 Git 推送'
    required = ('branch_check', 'coding', 'unit_test', 'code_review')
    if side + '.plan' in route['stages']:
        required = ('plan', *required)
    for step in required:
        name = side + '.' + step
        if history.get(name, {}).get('outcome') != 'success':
            return {}, {}, {}, f'Git 推送缺少成功的 {name} 回执'
    plan = repository_plan(store, item, side, history=(route, history))
    planned = planned_applications(plan, side)
    integration = history.get(side + '.integration', {})
    merged = (integration.get('evidence') or {}).get('merged_versions')
    branches = history[side + '.branch_check']['evidence'].get('repositories', {})
    if not planned or not commit_map(merged) or not merged.keys() <= set(planned) or branches.keys() != set(planned):
        return {}, {}, {}, 'Git 推送需要一致的目标仓库、分支核验及合回 SHA'
    if side == 'frontend' and route.get('post_merge_e2e'):
        if integration.get('outcome') != 'success' or merged.keys() != set(planned):
            return {}, {}, {}, '前端推送需要全部计划应用的成功合回回执'
        if not frontend_acceptance_terminal(route, history, merged):
            return {}, {}, {}, '前端 E2E、返工和复测须结束，且回执须对应最终合回版本'
        unaffected = {name: entry for name, entry in history.items() if name not in ('frontend.e2e', stage)}
        blockers = blocked_tasks(route, unaffected)
        mapping = plan.get('task_repositories', {})
        held = {key: '同一仓库仍有合回或交付前置任务受阻' for task in blockers
                for key in mapping.get(task, [])}
        return {key: sha for key, sha in merged.items() if key not in held}, branches, held, None
    acceptance_stage = 'backend.acceptance' if side == 'backend' else 'frontend.e2e'
    skip_e2e = acceptance_stage not in route['stages']
    acceptance = history.get(acceptance_stage, {})
    full = (skip_e2e or acceptance.get('outcome') == 'success') and integration.get('outcome') == 'success'
    if not full and ((not skip_e2e and not (acceptance.get('terminal_step') or acceptance.get('outcome') == 'success'))
                     or not (integration.get('terminal_step') or integration.get('outcome') == 'success')):
        return {}, {}, {}, 'Git 推送缺少已结束的验收或合回回执'
    blocked = blocked_tasks(route, {name: receipt for name, receipt in history.items() if name != stage})
    mapping = plan.get('task_repositories', {})
    passed = {task: verified_task_merge(history, side, task, skip_e2e=skip_e2e)
              for task in mapping if task not in blocked} if not full else {}
    selected, withheld = {}, {}
    for key in planned:
        affected = {task for task, keys in mapping.items() if key in keys}
        if affected & blocked.keys():
            if affected - blocked.keys():
                withheld[key] = '同一仓库仍有受阻任务'
        elif key in merged and (full or (affected and all(passed[task].get(key) == merged[key] for task in affected))):
            selected[key] = merged[key]
        else:
            withheld[key] = '缺少该仓全部任务的验收及实际合回证据'
    if not selected and not withheld:
        return {}, {}, {}, '没有已验收且可推送的仓库'
    return selected, branches, withheld, None


def git_push_evidence_error(store, item, stage, evidence, *, partial=False):
    versions, branches, withheld, reason = git_push_requirements(store, item, stage)
    receipts = evidence.get('repositories')
    if (not isinstance(receipts, dict) or not receipts.keys() <= versions.keys()
            or (not partial and (reason or withheld or receipts.keys() != versions.keys()))):
        return reason or 'Git 回执需要覆盖本单可推送的仓库'
    for key, entry in receipts.items():
        if (not isinstance(entry, dict) or entry.get('sha') != versions[key]
                or entry.get('branch') != branches[key].get('target_branch')
                or not isinstance(entry.get('remote_sha'), str) or not COMMIT.fullmatch(entry['remote_sha'])):
            return f'{key} 的 Git 远端回执不匹配'


def push_covers(store, item, stage, versions):
    route = get_split(store, item) or {}
    if stage not in route.get('stages', ()):
        return True
    receipts = (stage_evidence(store, item, stage) or {}).get('repositories', {})
    return bool(receipts) and all(receipts.get(key, {}).get('sha') == sha for key, sha in versions.items())


def pushed_backend_versions(route, stages, versions):
    if 'backend.push' not in route.get('stages', ()):
        return versions
    push = stages.get('backend.push', {})
    if push.get('outcome') != 'success' and not push.get('terminal_step'):
        return {}
    receipts = (push.get('evidence') or {}).get('repositories', {})
    return {key: sha for key, sha in versions.items() if receipts.get(key, {}).get('sha') == sha}


def deployment_requirements(store, item):
    route, history = read_history(store, item)
    plan = repository_plan(store, item, 'backend', history=(route, history))
    services = planned_applications(plan, 'backend')
    if not services:
        return {}, [], {}, '缺少经过确认的后端服务清单'
    targets = configured_targets(snapshot(item))
    selected = {key: targets[key] for key in services if key in targets}
    skipped = [key for key in services if key not in targets]
    if not selected:
        return selected, skipped, {}, None
    if 'backend.push' in route.get('stages', ()):
        integration = history.get('backend.integration', {})
        merged = (integration.get('evidence') or {}).get('merged_versions') or {}
        verified = pushed_backend_versions(route, history, merged)
        available = {key: target for key, target in selected.items() if key in verified}
        if not verified:
            return selected, skipped, {}, '缺少后端 Git 远端核验成功回执'
        skipped.extend(key for key in selected if key not in available)
        return available, skipped, verified, None
    for stage in ('backend.code_review', 'backend.acceptance', 'backend.integration'):
        if not stage_evidence(store, item, stage):
            return selected, skipped, {}, f'缺少成功的 {stage} 回执'
    merged = stage_evidence(store, item, 'backend.integration').get('merged_versions')
    if not commit_map(merged) or merged.keys() != set(services):
        return selected, skipped, {}, '合回回执必须覆盖本单所有后端服务'
    return selected, skipped, merged, None


def deployment_evidence_error(store, item, evidence):
    targets, skipped, versions, reason = deployment_requirements(store, item)
    if reason:
        return reason
    receipts = evidence.get('services')
    if not isinstance(receipts, dict) or receipts.keys() != targets.keys() or evidence.get('skipped') != skipped:
        return 'Provide release receipts for every configured service and identify any skipped service'
    for service, target in targets.items():
        if not deployed_receipt_matches(receipts[service], service, versions[service], target):
            return f'Invalid deployment evidence for {service}'


def handoff_error(store, item, evidence):
    route = get_split(store, item)
    blockers = route.get('handoff_blockers', route['blockers'])
    if blockers:
        return 'Backend handoff is blocked: ' + ', '.join(blockers)
    prerequisites = ('branch_check', 'coding', 'unit_test', 'code_review')
    if 'intake_repository_plan' not in route:
        prerequisites = ('design', 'plan', *prerequisites)
    git_delivery = 'backend.push' in route.get('stages', ())
    if not git_delivery:
        prerequisites += ('acceptance', 'integration')
    for name in prerequisites:
        if not stage_evidence(store, item, 'backend.' + name):
            return f'Backend {name} must succeed before frontend handoff'
    _, stages = read_history(store, item)
    integration = stage_evidence(store, item, 'backend.integration') or stages.get('backend.integration', {}).get('evidence')
    merged = (integration or {}).get('merged_versions', {})
    if git_delivery:
        verified = pushed_backend_versions(route, stages, merged)
        if not verified or (verified != merged and not blocked_tasks(route, stages)):
            return '后端 Git 远端核验通过后才可联调交接'
        merged = verified
    versions = evidence.get('backend_versions')
    partial_deployment = None
    if DEPLOY_STAGE in route.get('stages', ()) and not stage_evidence(store, item, DEPLOY_STAGE):
        receipt = stages.get(DEPLOY_STAGE, {})
        if receipt.get('task_blockers'):
            targets, _, deployed_versions, error = deployment_requirements(store, item)
            if error:
                return error
            entries = (receipt.get('evidence') or {}).get('services', {})
            partial_deployment = {key: entries[key] for key, target in targets.items()
                                  if deployed_receipt_matches(entries.get(key), key, deployed_versions[key], target)}
            merged = {key: sha for key, sha in merged.items() if key not in targets or key in partial_deployment}
    if not commit_map(versions) or versions != merged:
        return 'The callable backend version must match every merged service'
    if DEPLOY_STAGE in route.get('stages', ()):
        deployment = stage_evidence(store, item, DEPLOY_STAGE)
        if not deployment and partial_deployment is None:
            return 'The deployment stage must succeed before frontend handoff'
        if deployment:
            if reason := deployment_evidence_error(store, item, deployment):
                return reason
            receipts = deployment['services']
        else:
            receipts = partial_deployment
        if receipts:
            gateways = {receipt['target']['gateway_url'].rstrip('/') for receipt in receipts.values()}
            gateway = evidence.get('gateway_url')
            if (evidence.get('deployment') != receipts or not isinstance(gateway, str)
                    or gateways != {gateway.rstrip('/')}):
                return 'Handoff must carry the verified deployment receipts and a common gateway'
    if not all(nonblank(evidence.get(key)) for key in ('environment', 'verification', 'document')):
        return 'Supply the handoff document, callable environment and observed call evidence'
    database = Path(store.conn.execute('PRAGMA database_list').fetchone()[2])
    directory = database.parent.resolve() / 'items' / item['marker']
    document = Path(evidence['document'])
    expected = directory / f'round-{item["run_round"]}' / 'backend' / 'handoff.md'
    try:
        if (not document.is_absolute() or document.resolve() != expected or not document.is_file()
                or not document.read_text(encoding='utf-8-sig').strip()):
            return f'Provide the current handoff document at {expected}'
    except (OSError, UnicodeError, RuntimeError, ValueError) as error:
        return f'Handoff document unavailable: {error}'


def validate_success(store, item, stage, evidence, *, task_blockers=None):
    """Return a reason when a successful V2 stage cannot be substantiated."""
    if not isinstance(evidence, dict) or not evidence:
        return 'Stage evidence must be a non-empty object'
    if stage == 'handoff':
        return handoff_error(store, item, evidence)
    if stage in GIT_PUSH_STAGES:
        return git_push_evidence_error(store, item, stage, evidence)
    if stage == DEPLOY_STAGE:
        return deployment_evidence_error(store, item, evidence)
    if stage == FRONTEND_DEPLOY_STAGE:
        settings = frontend_release_config(snapshot(item))
        merged = (stage_evidence(store, item, 'frontend.integration') or {}).get('merged_versions')
        if not push_covers(store, item, 'frontend.push', merged or {}):
            return '缺少前端 Git 远端核验成功回执'
        image = evidence.get('image')
        remote = evidence.get('remote_versions')
        digests = (evidence.get('digest'), evidence.get('config_digest'))
        repository = settings.get('image_repository')
        if (not commit_map(merged) or evidence.get('versions') != merged
                or not isinstance(remote, dict) or remote.keys() != merged.keys()
                or not all(isinstance(sha, str) and COMMIT.fullmatch(sha) for sha in remote.values())
                or not isinstance(image, str) or not TAG.fullmatch(image)
                or image.rsplit(':', 1)[0] != repository
                or not image.endswith('-' + FRONTEND_PROFILE)
                or evidence.get('status') != 'published'
                or not all(isinstance(digest, str) and DIGEST.fullmatch(digest) for digest in digests)):
            return '前端镜像发布需要逐仓版本、停服及 Harbor 摘要回执'
        base = evidence.get('base_image')
        target = settings.get('rancher')
        if target:
            rollout = evidence.get('rancher')
            if (not isinstance(base, str) or not base.startswith(repository + '@')
                    or not DIGEST.fullmatch(base.split('@', 1)[1])
                    or not isinstance(rollout, dict) or rollout.get('target') != target
                    or not rollout_matches(target, rollout.get('pod_verification'), repository + '@' + digests[0])):
                return '前端镜像发布需要当前基线及 Rancher 新 Pod 核验'
        elif base != settings.get('base_image'):
            return '前端镜像发布需要匹配的底包摘要'
        return None
    side, _, name = stage.partition('.')
    if name in ('coding', 'integration'):
        return repository_evidence(store, item, side, name, evidence)
    if name in ('unit_test', 'code_review', 'acceptance', 'e2e'):
        candidate = stage_evidence(store, item, side + '.coding') or {}
        expected_versions = (frontend_acceptance_versions(store, item, get_split(store, item))
                             if name == 'e2e' else candidate.get('versions'))
        if not commit_map(evidence.get('versions')) or evidence['versions'] != expected_versions:
            return 'Verify the exact set of current candidate versions'
        if not nonblank(evidence.get('verification')):
            return 'Provide the command, observed result or verification evidence reference'
        reason = repository_evidence(store, item, side, name, evidence)
        if reason:
            return reason
        if name not in ('acceptance', 'e2e'):
            return None
        route, stages = read_history(store, item)
        if task_blockers:
            stages[stage] = {'task_blockers': task_blockers}
        blocked = blocked_tasks(route, stages)
        expected = {task['id'] for task in route['tasks'].get(side, [])} - blocked.keys()
        tested = evidence.get('verified_tasks')
        if not isinstance(tested, list) or not all(nonblank(task) for task in tested) or set(tested) != expected:
            return 'Acceptance must identify all verified task IDs for this side'
        if name == 'e2e' and route['scope'] == 'fullstack':
            handoff = stage_evidence(store, item, 'handoff') or {}
            if (not handoff and (blocked or 'handoff' in stages)
                    and not (handoff_tasks(route) - blocked.keys())):
                return None
            if not handoff or evidence.get('backend_versions') != handoff.get('backend_versions'):
                return 'Joint acceptance must verify the backend versions from the latest handoff'
            deployed = handoff.get('deployment')
            if deployed:
                gateway = evidence.get('gateway_url')
                probes = evidence.get('backend_deployment')
                if (not isinstance(gateway, str) or gateway.rstrip('/') != handoff['gateway_url'].rstrip('/')
                        or not isinstance(probes, dict) or probes.keys() != deployed.keys()):
                    return 'Joint acceptance must identify the deployed gateway and every version probe'
                for key, receipt in deployed.items():
                    if probes[key] != release_verification(receipt):
                        return f'Joint acceptance verified a different release for {key}'


def delivery_summary(route, stages):
    if not route:
        return '等待范围判定'
    scopes = {'fullstack': '前后端', 'frontend_only': '纯前端', 'backend_only': '纯后端'}
    sources = {'config': '项目配置', 'document': '需求标记', 'ai': 'AI 研判'}
    facts = [f'{scopes[route["scope"]]}（{sources[route["source"]]}）']
    for side, acceptance, label in (('backend', 'acceptance', '服务验收'), ('frontend', 'e2e', '页面验收')):
        test = stages.get(f'{side}.{acceptance}')
        if test:
            facts.append(label + {'success': '通过', 'failed': '失败', 'skipped': '已跳过'}[test['outcome']])
        elif side == 'frontend' and route['scope'] != 'backend_only' and not route.get('e2e_enabled', True):
            facts.append('页面验收已跳过（项目配置关闭）')
        merge = stages.get(side + '.integration')
        if merge and isinstance(merge.get('evidence'), dict):
            merged = merge['evidence'].get('merged_versions')
            if commit_map(merged):
                state = '合回' if merge['outcome'] == 'success' else '部分合回'
                targets = '、'.join(f'{key}@{sha}' for key, sha in sorted(merged.items()))
                facts.append(('后端' if side == 'backend' else '前端') + state + ' ' + targets)
        push = stages.get(side + '.push')
        if push:
            repositories = (push.get('evidence') or {}).get('repositories', {})
            label = '后端' if side == 'backend' else '前端'
            if repositories:
                reported = '、'.join(f'{key}@{entry["sha"]}' for key, entry in sorted(repositories.items()))
                facts.append(f'{label} Git 远端已核验 {reported}')
            if push['outcome'] != 'success':
                facts.append(f'{label} Git 推送待完成')
    deployed = stages.get(DEPLOY_STAGE)
    if deployed:
        evidence = deployed.get('evidence') or {}
        if deployed['outcome'] == 'skipped':
            facts.append('后端部署已跳过，待验证')
        elif deployed['outcome'] == 'failed':
            facts.append('后端部署待验证')
        elif not evidence.get('services'):
            facts.append('后端部署已跳过（本轮无适用目标）')
        for key, receipt in sorted(evidence.get('services', {}).items()):
            if receipt.get('status') == 'deployed':
                target = receipt['target']
                facts.append(f'{key} 已部署到 {target["namespace"]}/{target["deployment"]} '
                             f'{receipt["image"].rsplit(":", 1)[0]}@{receipt["digest"]}')
    frontend = stages.get(FRONTEND_DEPLOY_STAGE)
    if frontend:
        evidence = frontend.get('evidence') or {}
        if frontend['outcome'] == 'success':
            facts.append(f'前端镜像已推送 Harbor {evidence["image"]}（{evidence["digest"]}）')
            if rollout := evidence.get('rancher'):
                target = rollout['target']
                ready = rollout['pod_verification']['ready_replicas']
                facts.append(f'前端已在 Rancher 更新 {target["namespace"]}/{target["deployment"]}，{ready} 个 Pod 已就绪')
        elif frontend['outcome'] == 'skipped':
            facts.append('前端镜像发布已跳过，待验证')
        else:
            facts.append('前端镜像发布待验证')
    facts.extend(route.get('blockers', []))
    return '；'.join(facts)
