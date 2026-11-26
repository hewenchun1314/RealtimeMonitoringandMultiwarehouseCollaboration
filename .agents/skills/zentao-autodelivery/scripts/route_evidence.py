"""Version-bound evidence for a scoped work item, including its backend handoff."""

import json
import re
import subprocess
from pathlib import Path

from branch_check import git, planned_applications
from deploy_state import (COMMIT, DEPLOY_STAGE, DIGEST, FRONTEND_DEPLOY_STAGE, FRONTEND_PROFILE, GIT_PUSH_STAGES, TAG,
                          configured_targets, deployed_receipt_matches, frontend_release_config,
                          mcp_rollout_matches, release_verification)
from route_execution import (blocked_tasks, contract_revision_pending, get_split, handoff_tasks, is_streamlined_bug,
                             read_history, repository_plan, snapshot, stage_evidence)


def nonblank(value):
    return isinstance(value, str) and bool(value.strip())


def commit_map(value):
    return isinstance(value, dict) and bool(value) and all(
        nonblank(key) and isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha)
        for key, sha in value.items())


def agentlimb_receipt_error(store, item, attempt_id, evidence, expected, config_directory):
    path = evidence.get('agentlimb_receipt') if isinstance(evidence, dict) else None
    if not nonblank(path) or not config_directory:
        return 'Provide the AgentLimb runner JSON receipt for this attempt'
    project = config_directory.parent
    root = (project / '.local' / 'zentao-delivery' / 'items' / item['marker']).resolve()
    try:
        receipt_path = (project / path).resolve(strict=True)
        if not receipt_path.is_relative_to(root):
            return 'AgentLimb receipt must be inside the current work-item evidence directory'
        receipt = json.loads(receipt_path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return 'AgentLimb receipt could not be read'
    versions = (stage_evidence(store, item, 'frontend.coding') or {}).get('versions')
    if (not isinstance(receipt, dict) or receipt.get('runner') != 'agentlimb' or receipt.get('marker') != item['marker']
            or receipt.get('attempt_id') != attempt_id or receipt.get('status') != expected
            or receipt.get('stage') != 'acceptance' or not commit_map(versions) or receipt.get('versions') != versions):
        return 'AgentLimb receipt must match the active attempt, candidate versions and result'
    resources = receipt.get('candidate_resources')
    if (not receipt.get('checks') or not isinstance(resources, list) or not resources
            or any(not isinstance(row, dict) or not nonblank(row.get('app')) for row in resources)
            or {row['app'] for row in resources} != versions.keys()):
        return 'AgentLimb receipt must include checks and observed resources for every candidate application'
    if expected == 'failed' and receipt.get('repair_required') is not True:
        return 'Only a confirmed AgentLimb business assertion failure can trigger code repair'


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


def recorded_host_merge(store, item, key, proof, candidate, merged):
    rows = store.conn.execute(
        "SELECT request,result FROM development_git_operations WHERE item_seq=? AND kind='integrate' AND result IS NOT NULL",
        (item['seq'],))
    for row in rows:
        registered = json.loads(row['request'])['context']['repositories'].get(key, {})
        receipt = json.loads(row['result']).get('repositories', {}).get(key, {})
        if (registered.get('source_root') == proof['source_root']
                and registered.get('target_branch') == proof['target_branch']
                and registered.get('candidate') == candidate
                and (receipt.get('status'), receipt.get('candidate'), receipt.get('merged'))
                == ('verified', candidate, merged)):
            return True
    return False


def check_merged_commit(proof, candidate, merged, *, recorded=False):
    source = proof['source_root']
    actual = git(source, 'rev-parse', '--verify', 'refs/heads/' + proof['target_branch'])
    if ((merged != actual and (not recorded or
                              git(source, 'merge-base', '--is-ancestor', merged, actual, allowed=(0, 1)) is None))
            or git(source, 'merge-base', '--is-ancestor', candidate, merged, allowed=(0, 1)) is None):
        return 'Merged version must be the actual target tip and contain the candidate'


def check_merge(proof, candidate, merged, reverified, *, recorded=False):
    reason = check_merged_commit(proof, candidate, merged, recorded=recorded)
    if reason:
        return reason
    source = proof['source_root']
    if candidate != merged and not reverified:
        if git(source, 'rev-parse', candidate + '^{tree}') != git(source, 'rev-parse', merged + '^{tree}'):
            return 'Record merged_verification for a merge that changed the tested tree'


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
    try:
        for key, sha in merged.items():
            proof = targets[key]
            recorded = recorded_host_merge(store, item, key, proof, versions[key], sha)
            reason = check_merged_commit(proof, versions[key], sha, recorded=recorded)
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
    try:
        for key, proof in targets.items():
            if stage == 'integration':
                recorded = recorded_host_merge(store, item, key, proof, versions[key], merged[key])
                reason = check_merge(proof, versions[key], merged[key], nonblank(evidence.get('merged_verification')),
                                     recorded=recorded)
            else:
                reason = check_candidate(proof, versions[key])
            if reason:
                return f'{key}: {reason}'
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired) as error:
        return f'Git evidence could not be verified: {error}'


def git_push_requirements(store, item, stage):
    route, history = read_history(store, item)
    side = stage.split('.')[0]
    if not route or stage not in route['stages']:
        return {}, {}, {}, '工单未启用该端 Git 推送'
    if contract_revision_pending(history):
        return {}, {}, {}, '接口契约变更，等待重新拆分后核验 Git 推送'
    streamlined = is_streamlined_bug(item)
    steps = ('branch_check', 'coding') if streamlined else ('plan', 'branch_check', 'coding')
    for step in steps:
        name = side + '.' + step
        if history.get(name, {}).get('outcome') != 'success':
            return {}, {}, {}, f'Git 推送缺少成功的 {name} 回执'
    plan = route['repository_plan'].get(side) if streamlined else history[side + '.plan']['evidence']
    planned = planned_applications(plan, side)
    integration = history.get(side + '.integration', {})
    if integration.get('outcome') != 'success':
        return {}, {}, {}, 'Git 推送需要成功的合回回执'
    merged = (integration.get('evidence') or {}).get('merged_versions')
    branches = history[side + '.branch_check']['evidence'].get('repositories', {})
    if not planned or not commit_map(merged) or merged.keys() != set(planned) or branches.keys() != set(planned):
        return {}, {}, {}, 'Git 推送需要一致的计划、分支核验及合回 SHA'
    return merged, branches, {}, None


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
    plan = (repository_plan(store, item, 'backend') if is_streamlined_bug(item)
            else stage_evidence(store, item, 'backend.plan'))
    services = planned_applications(plan, 'backend')
    if not services:
        return {}, [], {}, '缺少经过确认的后端服务计划'
    targets = configured_targets(snapshot(item))
    selected = {key: targets[key] for key in services if key in targets}
    skipped = [key for key in services if key not in targets]
    if not selected:
        return selected, skipped, {}, None
    route, history = read_history(store, item)
    integration = history.get('backend.integration', {})
    if integration.get('outcome') != 'success':
        return selected, skipped, {}, '缺少成功的 backend.integration 回执'
    merged = (integration.get('evidence') or {}).get('merged_versions')
    if not commit_map(merged) or merged.keys() != set(services):
        return selected, skipped, {}, '合回回执必须覆盖本单所有后端服务'
    if 'backend.push' in route.get('stages', ()):
        verified = pushed_backend_versions(route, history, merged)
        available = {key: target for key, target in selected.items() if key in verified}
        if not verified:
            return selected, skipped, {}, '缺少后端 Git 远端核验成功回执'
        skipped.extend(key for key in selected if key not in available)
        return available, skipped, verified, None
    if not stage_evidence(store, item, 'backend.coding'):
        return selected, skipped, {}, '缺少成功的 backend.coding 回执'
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
    streamlined = is_streamlined_bug(item)
    prerequisites = (('branch_check', 'coding', 'code_review', 'acceptance') if streamlined else
                     ('design', 'plan', 'branch_check', 'coding', 'unit_test', 'code_review'))
    git_delivery = 'backend.push' in route.get('stages', ())
    if streamlined and route['handoff_mode'] == 'candidate':
        if reason := bug_verification_error(store, item, 'backend'):
            return reason
        versions = stage_evidence(store, item, 'backend.coding')['versions']
        if versions != evidence.get('backend_versions') or evidence.get('deployment'):
            return 'Bug handoff must identify the verified backend candidates served in the test environment'
        return handoff_document_error(store, item, evidence)
    if streamlined:
        prerequisites += ('integration',)
    elif not git_delivery:
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
    return handoff_document_error(store, item, evidence)


def handoff_document_error(store, item, evidence):
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


def bug_verification_error(store, item, side):
    candidate = stage_evidence(store, item, side + '.coding') or {}
    acceptance = side + ('.e2e' if side == 'frontend' else '.acceptance')
    required = (side + '.code_review', acceptance)
    versions = candidate.get('versions')
    if not commit_map(versions) or not nonblank(candidate.get('unit_test')):
        return f'Bug {side} candidate requires passing unit-test evidence'
    for stage in required:
        verified = stage_evidence(store, item, stage) or {}
        if verified.get('versions') != versions:
            return f'Bug merge requires successful {stage} for the same candidate'


def bug_merge_error(store, item, side):
    if not is_streamlined_bug(item):
        return None
    if reason := bug_verification_error(store, item, side):
        return reason
    route = get_split(store, item)
    if route['scope'] != 'fullstack' or (side == 'backend' and route['handoff_mode'] == 'deployed'):
        return None
    if reason := bug_verification_error(store, item, 'frontend'):
        return reason
    e2e = stage_evidence(store, item, 'frontend.e2e') or {}
    handoff = stage_evidence(store, item, 'handoff') or {}
    versions = handoff.get('backend_versions')
    if not commit_map(versions) or e2e.get('backend_versions') != versions:
        return 'Bug merge requires joint acceptance on the handed-off backend versions'
    if route['handoff_mode'] == 'candidate':
        backend = stage_evidence(store, item, 'backend.coding') or {}
        if versions != backend.get('versions'):
            return 'Bug merge requires joint acceptance on the current backend candidates'


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
                or evidence.get('stopped_ports') != settings.get('local_ports')
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
                    or not mcp_rollout_matches(rollout.get('pod_verification'), repository + '@' + digests[0])):
                return '前端镜像发布需要当前基线及 Rancher 新 Pod 核验'
        elif base != settings.get('base_image'):
            return '前端镜像发布需要匹配的底包摘要'
        return None
    side, _, name = stage.partition('.')
    if name in ('coding', 'integration'):
        if name == 'coding' and is_streamlined_bug(item) and not nonblank(evidence.get('unit_test')):
            return 'Provide passing unit-test commands and results for the bug candidate'
        if name == 'integration' and (reason := bug_merge_error(store, item, side)):
            return reason
        return repository_evidence(store, item, side, name, evidence)
    if name in ('unit_test', 'code_review', 'acceptance', 'e2e'):
        candidate = stage_evidence(store, item, side + '.coding') or {}
        if not commit_map(evidence.get('versions')) or evidence['versions'] != candidate.get('versions'):
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
            if (not is_streamlined_bug(item) and not handoff and (blocked or 'handoff' in stages)
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
