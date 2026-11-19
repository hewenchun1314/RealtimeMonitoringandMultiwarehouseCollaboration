"""Inspect configured Git repositories and verify newly prepared worktrees."""

import json
import os
import re
import subprocess
from pathlib import Path


def planned_applications(evidence):
    applications = evidence.get('applications') if isinstance(evidence, dict) else None
    if (not isinstance(applications, list) or not applications
            or any(not isinstance(app, str) or not app.strip() for app in applications)
            or len(set(applications)) != len(applications)):
        return None
    return applications


def branch_context(store, item):
    snapshot = json.loads(item['config_snapshot'])
    bundle = snapshot['development']['frontend_bundle']
    plan = store.conn.execute('''SELECT result FROM attempts WHERE item_seq=? AND stage='plan'
                                 AND outcome='success' ORDER BY run_round DESC LIMIT 1''',
                              (item['seq'],)).fetchone()
    applications = planned_applications(json.loads(plan['result'])['evidence'])
    return {
        'bundle': bundle['name'],
        'frontend_workspace': snapshot['workspaces']['frontend'],
        'branch_targets': {app: bundle['applications'].get(app) for app in applications},
        'worktree_branch': f'zentao/{item["marker"].lower()}-{item["run_round"]}',
    }


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
    if not isinstance(repositories, dict) or repositories.keys() != targets.keys():
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


def inspect_repository(frontend, target, worktree_branch, worktree):
    if target is None:
        return {'status': 'mapping_missing'}
    if not valid_target(target):
        return {'status': 'mapping_invalid'}
    branch = target['branch']
    repository = (frontend / target['repository']).resolve()
    app = {'source_root': str(repository), 'target_branch': branch}
    if not repository.is_relative_to(frontend) or not repository.is_dir() or not is_repo_root(repository):
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


def inspect_branches(context, config_directory, worktrees):
    frontend = (config_directory / context['frontend_workspace']).resolve()
    repositories = {}
    for application, target in context['branch_targets'].items():
        try:
            repositories[application] = inspect_repository(
                frontend, target, context['worktree_branch'], worktrees.get(application),
            )
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            repositories[application] = {'status': 'inspection_failed', 'reason': str(error)}
    return repositories


def prepare_local_worktree(context, config_directory, database, app, deadline_budget):
    branch = context['worktree_branch']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', app) or app not in context['branch_targets']:
        raise ValueError('Select a configured application from the current plan')
    target = context['branch_targets'][app]
    frontend = (config_directory / context['frontend_workspace']).resolve()
    resolved = inspect_repository(frontend, target, branch, None)
    if resolved['status'] != 'resolved':
        return resolved
    workspace = Path(database).resolve().parent
    path = workspace / 'worktrees' / branch.split('/')[-1] / app
    if path.resolve() != path:
        raise ValueError('Worktree destination must be within the ledger workspace and cannot contain directory redirects')
    repository = Path(resolved['source_root'])
    if path.is_relative_to(repository) or repository.is_relative_to(path):
        raise ValueError('The worktree must be outside its source checkout')
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        git(repository, 'worktree', 'add', '-b', branch, '--', str(path), resolved['base_commit'],
            timeout=min(50, max(1, deadline_budget)))
    return inspect_repository(frontend, target, branch, str(path))
