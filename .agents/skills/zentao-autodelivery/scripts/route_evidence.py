"""Version-bound evidence for a scoped work item, including its backend handoff."""

import re
import subprocess
from pathlib import Path

from branch_check import git, planned_applications
from deploy_state import (COMMIT, DEPLOY_STAGE, DIGEST, FRONTEND_DEPLOY_STAGE, FRONTEND_PROFILE, TAG,
                          configured_targets, deployed_receipt_matches, frontend_release_config,
                          mcp_rollout_matches, release_verification)
from route_execution import get_split, snapshot, stage_evidence


def nonblank(value):
    return isinstance(value, str) and bool(value.strip())


def commit_map(value):
    return isinstance(value, dict) and bool(value) and all(
        nonblank(key) and isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha)
        for key, sha in value.items())


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
    try:
        for key, proof in targets.items():
            if stage == 'integration':
                reason = check_merge(proof, versions[key], merged[key], nonblank(evidence.get('merged_verification')))
            else:
                reason = check_candidate(proof, versions[key])
            if reason:
                return f'{key}: {reason}'
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired) as error:
        return f'Git evidence could not be verified: {error}'


def deployment_requirements(store, item):
    plan = stage_evidence(store, item, 'backend.plan')
    services = planned_applications(plan, 'backend')
    if not services:
        return {}, [], {}, '缺少经过确认的后端服务计划'
    targets = configured_targets(snapshot(item))
    selected = {key: targets[key] for key in services if key in targets}
    skipped = [key for key in services if key not in targets]
    if not selected:
        return selected, skipped, {}, None
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
    for name in ('design', 'plan', 'branch_check', 'coding', 'unit_test', 'code_review', 'acceptance', 'integration'):
        if not stage_evidence(store, item, 'backend.' + name):
            return f'Backend {name} must succeed before frontend handoff'
    integration = stage_evidence(store, item, 'backend.integration')
    versions = evidence.get('backend_versions')
    if not commit_map(versions) or versions != integration.get('merged_versions'):
        return 'The callable backend version must match every merged service'
    if DEPLOY_STAGE in route.get('stages', ()):
        deployment = stage_evidence(store, item, DEPLOY_STAGE)
        if not deployment:
            return 'The deployment stage must succeed before frontend handoff'
        if reason := deployment_evidence_error(store, item, deployment):
            return reason
        receipts = deployment['services']
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


def validate_success(store, item, stage, evidence):
    """Return a reason when a successful V2 stage cannot be substantiated."""
    if not isinstance(evidence, dict) or not evidence:
        return 'Stage evidence must be a non-empty object'
    if stage == 'handoff':
        return handoff_error(store, item, evidence)
    if stage == DEPLOY_STAGE:
        return deployment_evidence_error(store, item, evidence)
    if stage == FRONTEND_DEPLOY_STAGE:
        settings = frontend_release_config(snapshot(item))
        merged = (stage_evidence(store, item, 'frontend.integration') or {}).get('merged_versions')
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
        route = get_split(store, item)
        expected = {task['id'] for task in route['tasks'].get(side, [])}
        tested = evidence.get('verified_tasks')
        if not isinstance(tested, list) or not all(nonblank(task) for task in tested) or set(tested) != expected:
            return 'Acceptance must identify all verified task IDs for this side'
        if name == 'e2e' and route['scope'] == 'fullstack':
            handoff = stage_evidence(store, item, 'handoff') or {}
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
            facts.append(label + ('通过' if test['outcome'] == 'success' else '失败'))
        elif side == 'frontend' and route['scope'] != 'backend_only' and not route.get('e2e_enabled', True):
            facts.append('页面验收已跳过（项目配置关闭）')
        merge = stages.get(side + '.integration')
        if merge and isinstance(merge.get('evidence'), dict):
            merged = merge['evidence'].get('merged_versions')
            if commit_map(merged):
                state = '合回' if merge['outcome'] == 'success' else '部分合回'
                targets = '、'.join(f'{key}@{sha}' for key, sha in sorted(merged.items()))
                facts.append(('后端' if side == 'backend' else '前端') + state + ' ' + targets)
    deployed = stages.get(DEPLOY_STAGE)
    if deployed:
        evidence = deployed.get('evidence') or {}
        if deployed['outcome'] == 'failed':
            facts.append('后端部署待验证')
        elif not evidence.get('services'):
            facts.append('后端部署已跳过（所选服务未配置部署目标）')
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
        else:
            facts.append('前端镜像发布待验证')
    facts.extend(route.get('blockers', []))
    return '；'.join(facts)
