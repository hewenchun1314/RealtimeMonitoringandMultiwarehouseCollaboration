"""Inspect configured Git repositories and verify newly prepared worktrees."""

import json
import os
import re
import subprocess
from pathlib import Path

from backend_dependencies import backend_services, writable_dependencies


BRANCH_STAGES = ('branch_check', 'frontend.branch_check', 'backend.branch_check')


def planned_applications(evidence, side='frontend'):
    field = 'services' if side == 'backend' else 'applications'
    applications = evidence.get(field) if isinstance(evidence, dict) else None
    if (not isinstance(applications, list) or not applications
            or any(not isinstance(app, str) or not app.strip() for app in applications)
            or len(set(applications)) != len(applications)):
        return None
    return applications


def branch_context(store, item):
    from route_execution import is_streamlined_bug, repository_plan

    snapshot = json.loads(item['config_snapshot'])
    side, separator, _ = item['stage'].partition('.')
    legacy = not separator
    side = 'frontend' if legacy else side
    bundle = snapshot['development'].get(f'{side}_bundle', {})
    targets = backend_services(snapshot) if side == 'backend' else bundle.get('applications', {})
    evidence = None
    if is_streamlined_bug(item):
        evidence = repository_plan(store, item, side)
    else:
        plan_stage = 'plan' if legacy else f'{side}.plan'
        plan = store.conn.execute('''SELECT result,outcome FROM attempts WHERE item_seq=? AND stage=?
                                     ORDER BY run_round DESC,retry_number DESC LIMIT 1''',
                                  (item['seq'], plan_stage)).fetchone()
        if plan and plan['result'] and (legacy or plan['outcome'] == 'success'):
            evidence = json.loads(plan['result']).get('evidence')
    applications = planned_applications(evidence, side) or []
    workspace = snapshot.get('workspaces', {}).get(side)
    suffix = '' if legacy else f'-{side}'
    context = {'bundle': bundle.get('name', side), 'workspace': workspace,
               'branch_targets': {app: targets.get(app) for app in applications},
               'worktree_branch': f'zentao/{item["marker"].lower()}-{item["run_round"]}{suffix}'}
    if legacy:
        context['frontend_workspace'] = workspace
    else:
        context['side'] = side
    if side == 'backend':
        dependency_workspaces = {key: entry['source_path'] for key, entry in writable_dependencies(snapshot).items()
                                 if key in applications}
        if dependency_workspaces:
            context['dependency_workspaces'] = dependency_workspaces
    return context


def source_refs(branch):
    return f'refs/heads/{branch}', f'refs/remotes/origin/{branch}'


def valid_target(target):
    if not isinstance(target, dict):
        return False
    branch, relative_path = target.get('branch'), target.get('repository')
    if not isinstance(branch, str) or not branch.strip() or not isinstance(relative_path, str) or not relative_path.strip():
        return False
    path = Path(relative_path)
    return not path.anchor and '..' not in path.parts


def valid_evidence(context, evidence):
    repositories = evidence.get('repositories') if isinstance(evidence, dict) else None
    targets = context['branch_targets']
    if (context.get('revoked_dependencies') or not targets
            or not isinstance(repositories, dict) or repositories.keys() != targets.keys()):
        return False
    paths = set()
    for application, target in targets.items():
        entry = repositories[application]
        if not valid_target(target) or not isinstance(entry, dict):
            return False
        if (entry.get('target_branch') != target['branch']
                or entry.get('source_ref') not in source_refs(target['branch'])
                or entry.get('worktree_branch') != context['worktree_branch']
                or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', str(entry.get('base_commit', '')))):
            return False
        source_root = entry.get('source_root')
        worktree = entry.get('worktree')
        if (not isinstance(source_root, str) or not Path(source_root).is_absolute()
                or not isinstance(worktree, str) or not Path(worktree).is_absolute()):
            return False
        path_key = os.path.normcase(os.path.normpath(worktree))
        if path_key == os.path.normcase(os.path.normpath(source_root)) or path_key in paths:
            return False
        paths.add(path_key)
    return True


def git(path, *args, allowed=(0,), timeout=5):
    result = subprocess.run(
        ['git', '-C', str(path), *args], encoding='utf-8', errors='replace',
        capture_output=True, timeout=timeout, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
    )
    if result.returncode not in allowed:
        raise ValueError(f'Git {args[0]} returned {result.returncode} in {path}')
    return result.stdout.strip() if result.returncode == 0 else None


def is_repo_root(path):
    allowed = (0,) if (path / '.git').exists() else (0, 128)
    root = git(path, 'rev-parse', '--show-toplevel', allowed=allowed)
    return root is not None and Path(root).resolve() == path


def inspect_repository(workspace, target, worktree_branch, worktree):
    if target is None:
        return {'status': 'mapping_missing'}
    if not valid_target(target):
        return {'status': 'mapping_invalid'}
    branch = target['branch']
    repository = (workspace / target['repository']).resolve()
    app = {'source_root': str(repository), 'target_branch': branch}
    if not repository.is_relative_to(workspace) or not repository.is_dir() or not is_repo_root(repository):
        return {**app, 'status': 'repository_missing'}
    if git(repository, 'check-ref-format', f'refs/heads/{branch}', allowed=(0, 1)) is None:
        return {**app, 'status': 'invalid_target_branch'}
    refs = source_refs(branch)
    available = set(git(repository, 'for-each-ref', '--format=%(refname)', *refs).splitlines())
    source_ref = next((ref for ref in refs if ref in available), None)
    if source_ref is None:
        return {**app, 'status': 'target_ref_missing'}
    commit = git(repository, 'rev-parse', '--verify', f'{source_ref}^{{commit}}')
    app.update(source_ref=source_ref, base_commit=commit,
               current_branch=git(repository, 'symbolic-ref', '--quiet', '--short', 'HEAD', allowed=(0, 1)))
    if worktree is None:
        return {**app, 'status': 'resolved'}
    if not isinstance(worktree, str) or not Path(worktree).is_absolute():
        return {**app, 'status': 'worktree_mismatch'}
    worktree = Path(worktree).resolve()
    if worktree == repository or not worktree.is_dir() or not is_repo_root(worktree):
        return {**app, 'status': 'worktree_mismatch'}
    common_args = ('rev-parse', '--path-format=absolute', '--git-common-dir')
    if Path(git(worktree, *common_args)).resolve() != Path(git(repository, *common_args)).resolve():
        return {**app, 'status': 'worktree_mismatch'}
    actual_branch = git(worktree, 'symbolic-ref', '--quiet', 'HEAD', allowed=(0, 1))
    actual_commit = git(worktree, 'rev-parse', '--verify', 'HEAD')
    if actual_branch != f'refs/heads/{worktree_branch}' or actual_commit != commit:
        return {**app, 'status': 'worktree_mismatch', 'actual_ref': actual_branch, 'actual_commit': actual_commit}
    return {**app, 'status': 'verified', 'worktree': str(worktree), 'worktree_branch': worktree_branch}


def source_workspace(context, config_directory, key):
    workspace = context.get('dependency_workspaces', {}).get(key, context['workspace'])
    return (config_directory / workspace).resolve() if workspace else None


def inspect_branches(context, config_directory, worktrees):
    repositories = {}
    for application, target in context['branch_targets'].items():
        if application in context.get('revoked_dependencies', ()):
            repositories[application] = {'status': 'dependency_write_revoked'}
            continue
        try:
            workspace = source_workspace(context, config_directory, application)
            repositories[application] = (inspect_repository(
                workspace, target, context['worktree_branch'], worktrees.get(application)
            ) if workspace else {'status': 'workspace_missing'})
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            repositories[application] = {'status': 'inspection_failed', 'reason': str(error)}
    return repositories
