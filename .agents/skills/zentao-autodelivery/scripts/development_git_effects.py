"""Fixed host-side branch/worktree, candidate and fast-forward operations."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
from contextlib import closing
import subprocess

from branch_check import inspect_repository, is_repo_root, source_refs
from development_git import overlapping_paths, relative_files
from stage_runner import write_json

DIAGNOSTICS = ContextVar('host_git_diagnostics', default=None)


def diagnostic(process):
    folder = DIAGNOSTICS.get()
    if folder is None:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    def clean(value):
        value = re.sub(r'https?://[^\s]+', '[remote-url]', value or '')
        value = re.sub(r'(?im)(password|token|secret|authorization)[\"\']?\s*[:=].*', r'\1=[redacted]', value)
        return value[-12000:]
    path = folder / (str(len(list(folder.glob('*.json')))).zfill(4) + '.json')
    write_json(path, {'exit_code': process.returncode, 'stdout': clean(process.stdout), 'stderr': clean(process.stderr)})
    return str(path)



def git(root, *args, deadline, allowed=(0,), env=None, raw=False, keep_output=False):
    budget = (datetime.fromisoformat(deadline) - datetime.now(timezone.utc)).total_seconds()
    if budget <= 0:
        raise ValueError('The original host Git deadline elapsed')
    inherited = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never', 'GCM_GUI_PROMPT': 'false', **(env or {})}
    count = int(inherited.get('GIT_CONFIG_COUNT', '0'))
    inherited.update({f'GIT_CONFIG_KEY_{count}': 'core.longpaths', f'GIT_CONFIG_VALUE_{count}': 'true',
                      'GIT_CONFIG_COUNT': str(count + 1)})
    literal = ['--literal-pathspecs'] if args and args[0] == 'add' else []
    process = subprocess.run(['git', '-C', str(root), *literal, *args], capture_output=True,
                             text=True, encoding='utf-8', errors='replace', timeout=budget,
                             env=inherited)
    log = diagnostic(process)
    if process.returncode not in allowed:
        raise ValueError('Git operation failed (exit ' + str(process.returncode) + '); hooks/checks remain enabled'
                         + ('; diagnostics: ' + log if log else ''))
    return (process.stdout if raw else process.stdout.strip()) if process.returncode == 0 or keep_output else None


def common_directory(source, deadline):
    if not is_repo_root(source):
        raise ValueError('The configured source is no longer a repository root')
    return Path(git(source, 'rev-parse', '--path-format=absolute', '--git-common-dir', deadline=deadline)).resolve()


def inventory(source, deadline):
    raw = git(source, 'worktree', 'list', '--porcelain', '-z', deadline=deadline)
    result = []
    for block in raw.split('\0\0'):
        entry = {}
        for field in block.split('\0'):
            key, _, value = field.partition(' ')
            if key in ('worktree', 'branch'):
                entry[key] = value
        if entry:
            result.append(entry)
    return result


def tracked_dirty(root, deadline):
    return bool(git(root, '--no-optional-locks', 'status', '--porcelain=v1', '--untracked-files=no',
                    '--ignore-submodules=untracked', deadline=deadline))


def active_git_operation(root, deadline):
    directory = Path(git(root, 'rev-parse', '--absolute-git-dir', deadline=deadline))
    return any((directory / name).exists() for name in (
        'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer'))


def has_local_work(root, deadline):
    if tracked_dirty(root, deadline):
        return True
    # Git status deliberately skips assume-unchanged and sparse index entries.
    # Treat them as local state before choosing a checkout or merge strategy.
    records = git(root, 'ls-files', '-v', '-z', deadline=deadline, raw=True)
    return any(row and (row[0] == 'S' or row[0].islower()) for row in records.split('\0'))


def validate_worktree(proof, deadline):
    root = Path(proof['worktree']).resolve()
    source = Path(proof['source_root'])
    expected = 'refs/heads/' + proof['worktree_branch']
    if (root == source or not is_repo_root(root) or common_directory(root, deadline) != common_directory(source, deadline)
            or git(root, 'symbolic-ref', '--quiet', 'HEAD', deadline=deadline, allowed=(0, 1)) != expected):
        raise ValueError('The checkout is not the registered item-owned worktree')
    if not any(Path(x['worktree']).resolve() == root and x.get('branch') == expected for x in inventory(source, deadline)):
        raise ValueError('The item worktree is not registered in the canonical repository')
    head = git(root, 'rev-parse', 'HEAD', deadline=deadline)
    if git(root, 'merge-base', '--is-ancestor', proof['base_commit'], head, deadline=deadline, allowed=(0, 1)) is None:
        raise ValueError('The item worktree no longer descends from its registered baseline')
    return root, head


@contextmanager
def hooks(root, journal, deadline, *, selected_files=None):
    """Observe real hook exit codes, including post hooks Git itself ignores."""
    actual = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-path', 'hooks', deadline=deadline))
    wrappers = journal / 'hook-forwarders'
    reports = journal / 'hook-results'
    wrappers.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    # New executions have their own stable journal; no original hook/config is
    # edited. Restore the caller's config parameters before the original hook so
    # nested Git resolves its own normal hooks rather than inheriting this shim.
    for report in [*reports.glob('*.exit'), *reports.glob('*.args')]:
        report.unlink()
    if actual.is_dir():
        for hook in actual.iterdir():
            if not hook.is_file() or hook.name.endswith('.sample') or not os.access(hook, os.X_OK):
                continue
            guard = ''
            if selected_files is not None and hook.name in ('pre-commit', 'prepare-commit-msg', 'commit-msg'):
                exclusions = ' '.join(shlex.quote(':(exclude,top,literal)' + file) for file in selected_files)
                guard = ('if [ "$status" = 0 ]; then\n'
                         '  (unset GIT_LITERAL_PATHSPECS GIT_GLOB_PATHSPECS GIT_NOGLOB_PATHSPECS GIT_ICASE_PATHSPECS; '
                         'git diff --cached --quiet -- . ' + exclusions + ')\n'
                         '  status=$?\nfi\n')
            script = ("#!/bin/sh\n"
                      "if [ \"$ZENTAO_PARENT_GIT_CONFIG_PRESENT\" = 1 ]; then\n"
                      "  export GIT_CONFIG_PARAMETERS=\"$ZENTAO_PARENT_GIT_CONFIG_PARAMETERS\"\n"
                      "else unset GIT_CONFIG_PARAMETERS; fi\n"
                      + 'printf \'%s\\0\' "$@" > ' + shlex.quote((reports / (hook.name + '.args')).as_posix()) + '\n'
                      + shlex.quote(hook.resolve().as_posix()) + ' "$@"\n'
                      + 'status=$?\n' + guard + 'printf \"%s\\n\" "$status" > '
                      + shlex.quote((reports / (hook.name + '.exit')).as_posix()) + '\nexit "$status"\n')
            wrapper = wrappers / hook.name
            wrapper.write_text(script, encoding='utf-8', newline='\n')
            wrapper.chmod(0o755)
    env = {'ZENTAO_PARENT_GIT_CONFIG_PRESENT': '1' if 'GIT_CONFIG_PARAMETERS' in os.environ else '0',
           'ZENTAO_PARENT_GIT_CONFIG_PARAMETERS': os.environ.get('GIT_CONFIG_PARAMETERS', '')}
    yield ['-c', 'core.hooksPath=' + str(wrappers)], env
    if any(p.read_text().strip() != '0' for p in reports.glob('*.exit')):
        raise ValueError('An original Git hook failed; fix its checks before retrying')


def ensure_hooks(source, root, deadline, *, checkout=None):
    source_hooks = Path(git(source, 'rev-parse', '--path-format=absolute', '--git-path', 'hooks', deadline=deadline))
    item_hooks = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-path', 'hooks', deadline=deadline))
    if source_hooks != item_hooks and source_hooks.is_dir():
        if (source_hooks.relative_to(source).as_posix() == '.husky/_' if source_hooks.is_relative_to(source) else False):
            if not item_hooks.is_relative_to(root) or item_hooks.relative_to(root).as_posix() != '.husky/_':
                raise ValueError('The item hook path differs from the configured repository hook path')
            for entry in sorted(source_hooks.iterdir()):
                destination = item_hooks / entry.name
                if destination.exists():
                    continue
                if (not entry.is_file() or entry.is_symlink() or git(source, 'check-ignore', '--quiet', '--',
                        entry.relative_to(source).as_posix(), deadline=deadline, allowed=(0, 1)) is None):
                    raise ValueError('Install original generated hooks; host bootstrap only copies ignored regular runtime files')
                item_hooks.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry, destination)
    names = ('post-checkout', 'pre-commit', 'prepare-commit-msg', 'commit-msg', 'post-commit', 'pre-merge-commit', 'post-merge')
    for name in names:
        if (source_hooks / name).is_file() and not (item_hooks / name).is_file():
            raise ValueError('Install the registered repository hooks in this item worktree before host Git work')
    if source_hooks.name == '_' and source_hooks.parent.name == '.husky':
        # The source can be on another user branch. Conservatively check runtime
        # readiness for the locked target tree before creating its checkout.
        if (checkout and git(source, 'cat-file', '-e', checkout + ':.husky/post-checkout',
                             deadline=deadline, allowed=(0, 128)) is not None
                and (not (source_hooks / 'post-checkout').is_file() or not os.access(source_hooks / 'post-checkout', os.X_OK))):
            raise ValueError('Install the original generated Husky runtime before running the tracked checkout check')
        for name in names:
            if (((source_hooks.parent / name).is_file() or (item_hooks.parent / name).is_file())
                    and (not (item_hooks / name).is_file() or not os.access(item_hooks / name, os.X_OK))):
                raise ValueError('Install the original generated Husky runtime before running the tracked checks')
            if (source_hooks.parent / name).is_file() and not (item_hooks.parent / name).is_file():
                raise ValueError('The original Husky check disappeared from the item worktree')


def failed_hooks(journal):
    reports = journal / 'hook-results'
    return [p.stem for p in reports.glob('*.args') if not p.with_suffix('.exit').exists()
            or p.with_suffix('.exit').read_text().strip() != '0']


def failed_hook_args(journal):
    return {name: (journal / 'hook-results' / (name + '.args')).read_bytes().decode('utf-8').rstrip('\0').split('\0')
            for name in failed_hooks(journal) if (journal / 'hook-results' / (name + '.args')).exists()}


def rerun_post_hooks(root, names, journal, deadline, arguments=None, *, checkout_source=None):
    for name in names:
        args = (arguments or {}).get(name, ['0'] if name == 'post-merge' else [])
        if args == ['']:
            args = []
        if (name not in ('post-commit', 'post-merge', 'post-checkout')
                or name == 'post-commit' and args
                or name == 'post-merge' and args not in (['0'], ['1'])
                or name == 'post-checkout' and (len(args) != 3 or args[2] not in ('0', '1')
                    or any(not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value) for value in args[:2]))):
            raise ValueError('A failed original hook requires a new checked Git operation')
        # Worktree add resolves the SOURCE hook but runs it in the NEW checkout.
        # Reuse that dispatch on recovery; resolving a target Husky wrapper here
        # would run a different tracked script and could hide the source failure.
        if name == 'post-checkout':
            if checkout_source is None:
                raise ValueError('The original source checkout hook context is required')
            with hooks(checkout_source, journal, deadline) as (opts, env):
                git(root, *opts, 'hook', 'run', name, '--', *args, deadline=deadline, env=env)
        else:
            # Explicit hook run propagates failures Git commit/merge may ignore.
            git(root, 'hook', 'run', name, *(['--', *args] if args else []), deadline=deadline)


def prepare(proof, context, key, database, journal, payload=None):
    deadline = context['deadline_at']
    source = Path(proof['source_root'])
    branch = proof['target_branch']
    git(source, 'check-ref-format', 'refs/heads/' + branch, deadline=deadline)
    refs = source_refs(branch)
    available = set(git(source, 'for-each-ref', '--format=%(refname)', *refs, deadline=deadline).splitlines()).intersection(refs)
    if not available:
        # Exact configured origin branch only; the model supplies neither URL nor
        # refspec. Do not fetch every branch or replace a present local target.
        git(source, '-c', 'credential.interactive=never', 'fetch', '--no-tags', '--no-recurse-submodules',
            '--', 'origin', f'refs/heads/{branch}:refs/remotes/origin/{branch}', deadline=deadline)
    ref = next((r for r in refs if git(source, 'show-ref', '--verify', '--quiet', r,
                                      deadline=deadline, allowed=(0, 1)) is not None), None)
    if ref is None:
        raise ValueError('The configured origin target branch is unavailable')
    base = git(source, 'rev-parse', '--verify', ref + '^{commit}', deadline=deadline)
    worktrees_root = database.resolve().parent / 'worktrees'
    worktree = worktrees_root / proof['worktree_branch'].split('/')[-1] / key
    if (worktree.resolve() != worktree or not worktree.is_relative_to(worktrees_root)
            or worktree.is_relative_to(source) or source.is_relative_to(worktree)):
        raise ValueError('The deterministic item worktree location is invalid')
    existing = [x for x in inventory(source, deadline) if x.get('branch') == 'refs/heads/' + proof['worktree_branch']]
    if existing and any(Path(x['worktree']).resolve() != worktree for x in existing):
        raise ValueError('The item branch is checked out in another worktree')
    effect = journal / 'prepare.json'
    old = json.loads(effect.read_text()) if effect.exists() else None
    if old and old['base_commit'] != base:
        raise ValueError('The locked preparation baseline changed')
    write_json(effect, {'base_commit': base, 'worktree': str(worktree), 'source_ref': ref})
    if not worktree.exists():
        ensure_hooks(source, source, deadline, checkout=base)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        branch_exists = git(source, 'show-ref', '--verify', '--quiet', 'refs/heads/' + proof['worktree_branch'],
                            deadline=deadline, allowed=(0, 1)) is not None
        if branch_exists and git(source, 'rev-parse', proof['worktree_branch'], deadline=deadline) != base:
            raise ValueError('The existing item branch has a foreign baseline')
        with hooks(source, journal, deadline) as (opts, env):
            git(source, *opts, 'worktree', 'add', *([] if branch_exists else ['-b', proof['worktree_branch']]),
                '--', str(worktree), proof['worktree_branch'] if branch_exists else base, deadline=deadline, env=env)
    elif (payload and payload.get('failed_hooks')) or failed_hooks(journal):
        ensure_hooks(source, worktree, deadline)
        rerun_post_hooks(worktree, (payload or {}).get('failed_hooks') or failed_hooks(journal), journal, deadline,
                         (payload or {}).get('failed_hook_args') or failed_hook_args(journal), checkout_source=source)
    target = {'branch': branch, 'repository': '.'}
    inspected = inspect_repository(source, target, proof['worktree_branch'], str(worktree))
    if inspected['status'] != 'verified' or tracked_dirty(worktree, deadline):
        raise ValueError('The prepared worktree or hook-generated files require reconciliation')
    return inspected


def validate_main_checkout(proof, deadline):
    root = Path(proof['source_root'])
    ref = 'refs/heads/' + proof['target_branch']
    if (Path(proof['worktree']) != root or not is_repo_root(root)
            or git(root, 'symbolic-ref', '--quiet', 'HEAD', allowed=(0, 1), deadline=deadline) != ref):
        raise ValueError('The main checkout is not on the configured target branch')
    return root, git(root, 'rev-parse', '--verify', ref, deadline=deadline)


def tracked_changes(root, deadline, *, cached=False):
    args = ('--cached',) if cached else ()
    return set(git(root, 'diff', *args, '--name-only', '--no-renames', '--ignore-submodules=untracked', '-z',
                   deadline=deadline, raw=True).split('\0')) - {''}


def committed_paths(root, before, after, deadline):
    return set(git(root, 'diff', '--name-only', '--no-renames', '-z', before, after,
                   deadline=deadline, raw=True).split('\0')) - {''}


def main_commit(proof, entry, context, operation, key, journal):
    deadline = context['deadline_at']
    root, head = validate_main_checkout(proof, deadline)
    selected = entry['files']
    baseline = set(entry['baseline_dirty'])
    if overlapping_paths(selected, baseline):
        raise ValueError('Requested repair files overlap pre-existing main checkout changes')
    ensure_hooks(root, root, deadline)
    marker = 'Codex-Host-Git: ' + operation + '/' + key
    effect = journal / 'commit.json'
    if head != entry['expected_head']:
        body = git(root, 'log', '-1', '--format=%B', deadline=deadline)
        parent = git(root, 'rev-parse', 'HEAD^', deadline=deadline)
        if not effect.is_file() or marker not in body.splitlines() or parent != entry['expected_head']:
            raise ValueError('The main checkout HEAD changed outside this host operation')
        return {'status': 'candidate_retest_required', 'candidate': head, 'failed_hooks': failed_hooks(journal),
                'reason': 'Recovered a repair commit; verify hooks and rerun checks on this SHA'}
    if relative_files(root, selected) != entry['fingerprints']:
        raise ValueError('Requested repair files changed after the sandbox request')
    staged = tracked_changes(root, deadline, cached=True)
    unstaged = tracked_changes(root, deadline)
    if (staged | unstaged) - set(selected) - baseline:
        raise ValueError('Unexpected tracked or staged changes outside the repair files require reconciliation')
    if not selected:
        if entry.get('needs_retest') and not str(entry.get('verification', '')).strip():
            raise ValueError('Rerun checks and supply verification before confirming this repair')
        if head != proof['base_commit'] and (git(root, 'rev-parse', 'HEAD^', deadline=deadline) != proof['base_commit']
                or committed_paths(root, proof['base_commit'], head, deadline) - set(entry.get('confirmed_files', []))):
            raise ValueError('The recorded repair does not exclusively cover its authorized paths and base')
        rerun_post_hooks(root, entry.get('failed_hooks', []), journal, deadline, entry.get('failed_hook_args'))
        if (tracked_changes(root, deadline, cached=True) | tracked_changes(root, deadline)) - baseline:
            return {'status': 'candidate_retest_required', 'candidate': head,
                    'reason': 'The original hook added tracked changes; reconcile and rerun checks'}
        return {'status': 'verified', 'candidate': head}
    untracked = set(git(root, 'ls-files', '--others', '--exclude-standard', '-z', deadline=deadline, raw=True).split('\0'))
    if new_files := sorted(set(selected) & untracked):
        git(root, 'add', '--intent-to-add', '--', *new_files, deadline=deadline)
    write_json(effect, {'expected_head': head, 'files': selected, 'marker': marker})
    try:
        with hooks(root, journal, deadline, selected_files=selected) as (opts, env):
            git(root, '--literal-pathspecs', *opts, 'commit', '--only', '-m', entry['message'], '-m', marker,
                '--', *selected, deadline=deadline, env=env)
    except (ValueError, subprocess.TimeoutExpired):
        _, after = validate_main_checkout(proof, deadline)
        if after == head:
            raise
        return {'status': 'candidate_retest_required', 'candidate': after,
                'reason': 'The repair commit took effect but a hook or post-commit check failed; rerun checks',
                'failed_hooks': failed_hooks(journal)}
    _, after = validate_main_checkout(proof, deadline)
    if (git(root, 'rev-parse', 'HEAD^', deadline=deadline) != head
            or committed_paths(root, head, after, deadline) - set(selected) or relative_files(root, selected) != entry['fingerprints']
            or (tracked_changes(root, deadline, cached=True) | tracked_changes(root, deadline)) - baseline):
        return {'status': 'candidate_retest_required', 'candidate': after,
                'reason': 'A hook changed the planned repair; reconcile files and rerun checks'}
    return {'status': 'verified', 'candidate': after}


def commit(proof, entry, context, operation, key, journal):
    if proof.get('main_repair'):
        return main_commit(proof, entry, context, operation, key, journal)
    deadline = context['deadline_at']
    root, head = validate_worktree(proof, deadline)
    ensure_hooks(Path(proof['source_root']), root, deadline)
    marker = 'Codex-Host-Git: ' + operation + '/' + key
    effect_path = journal / 'commit.json'
    prior = json.loads(effect_path.read_text()) if effect_path.exists() else None
    if head != entry['expected_head']:
        body = git(root, 'log', '-1', '--format=%B', deadline=deadline)
        parent = git(root, 'rev-parse', 'HEAD^', deadline=deadline, allowed=(0, 128))
        if not prior or marker not in body.splitlines() or parent != entry['expected_head']:
            raise ValueError('The candidate HEAD is stale or changed outside this operation')
        # Effects occurred before receipt. Never run the commit again; require
        # checks on the existing candidate after uncertain/post-commit execution.
        return {'status': 'candidate_retest_required', 'candidate': head,
                'reason': 'Recovered an existing commit; verify hooks and rerun checks on this SHA',
                'failed_hooks': failed_hooks(journal)}
    if relative_files(root, entry['files']) != entry['fingerprints']:
        raise ValueError('Requested file contents changed after the sandbox request')
    selected = set(entry['files'])
    staged = set(git(root, 'diff', '--cached', '--name-only', '--no-renames', '-z', deadline=deadline).split('\0')) - {''}
    changed = set(git(root, 'diff', '--name-only', '--no-renames', '-z', deadline=deadline).split('\0')) - {''}
    if (staged | changed) - selected:
        raise ValueError('Tracked or staged changes outside the literal requested file list remain')
    if not selected:
        if entry.get('needs_retest') and not str(entry.get('verification', '')).strip():
            raise ValueError('Rerun checks on the hook-affected candidate and supply verification before confirming it')
        if tracked_dirty(root, deadline):
            raise ValueError('The existing candidate is not clean')
        rerun_post_hooks(root, entry.get('failed_hooks', []), journal, deadline, entry.get('failed_hook_args'))
        if tracked_dirty(root, deadline):
            return {'status': 'candidate_retest_required', 'candidate': head, 'failed_hooks': [],
                    'reason': 'The rechecked original hook changed tracked/index files; clean and retest'}
        return {'status': 'verified', 'candidate': head, 'verification': entry.get('verification', '')}
    git(root, 'add', '--', *entry['files'], deadline=deadline)
    tree = git(root, 'write-tree', deadline=deadline)
    if tree == git(root, 'rev-parse', 'HEAD^{tree}', deadline=deadline):
        if entry.get('needs_retest') and not str(entry.get('verification', '')).strip():
            raise ValueError('Rerun checks on the hook-affected candidate before confirming it')
        rerun_post_hooks(root, entry.get('failed_hooks', []), journal, deadline, entry.get('failed_hook_args'))
        if tracked_dirty(root, deadline):
            raise ValueError('The rechecked original hook changed tracked/index files')
        return {'status': 'verified', 'candidate': head}
    write_json(effect_path, {'expected_head': head, 'staged_tree': tree, 'marker': marker})
    try:
        with hooks(root, journal, deadline) as (opts, env):
            git(root, *opts, 'commit', '-m', entry['message'], '-m', marker, deadline=deadline, env=env)
    except (ValueError, subprocess.TimeoutExpired):
        after = git(root, 'rev-parse', 'HEAD', deadline=deadline)
        if after != head:
            return {'status': 'candidate_retest_required', 'candidate': after,
                    'reason': 'The commit took effect but a hook or post-commit result failed; rerun checks',
                    'failed_hooks': failed_hooks(journal)}
        raise
    after = git(root, 'rev-parse', 'HEAD', deadline=deadline)
    actual_tree = git(root, 'rev-parse', 'HEAD^{tree}', deadline=deadline)
    if tracked_dirty(root, deadline) or actual_tree != tree:
        return {'status': 'candidate_retest_required', 'candidate': after,
                'reason': 'Hooks changed tracked/index content; reconcile, clean and retest this candidate'}
    return {'status': 'verified', 'candidate': after}


def integrate_checkout(source, root, proof, tip, journal, key, deadline, payload, earlier_effect):
    from development_git_overlay import (matches_parked_overlay, prepare_overlay, park_overlay, restore_original_overlay,
                                         restore_overlay, verify_overlay)

    branch = 'refs/heads/' + proof['target_branch']
    candidate = proof['candidate']
    effect = journal / 'integration.json'
    planned = {'target': branch, 'candidate': candidate, 'before': tip, 'worktree': str(root)}
    if active_git_operation(root, deadline):
        raise ValueError('Another Git operation is using the target checkout')
    prior_failed = payload.get('failed_hooks', {}).get(key) or failed_hooks(journal)
    contained = git(source, 'merge-base', '--is-ancestor', candidate, tip, deadline=deadline, allowed=(0, 1)) is not None
    if not contained:
        # A fresh merge invokes its own merge/commit hooks. Only a failed checkout
        # ran outside that command and still needs its original dispatch.
        prior_failed = [name for name in prior_failed if name == 'post-checkout']
    result = {'status': 'verified', 'candidate': candidate, 'target_before': tip}
    if contained and not prior_failed:
        if (earlier_effect or key in payload.get('needs_retest', [])) and not str(payload.get('verification', '')).strip():
            return {**result, 'status': 'integration_retest_required', 'merged': tip, 'worktree': str(root),
                    'reason': 'Verify interrupted integration and retest this merged revision'}
        return {**result, 'merged': tip, 'already_contained': True}
    def on_target():
        return git(root, 'symbolic-ref', '--quiet', 'HEAD', deadline=deadline, allowed=(0, 1)) == branch

    if not on_target() or git(root, 'rev-parse', 'HEAD', deadline=deadline) != tip:
        raise ValueError('The checked-out target or its tip changed before integration')

    merge_target = candidate
    expected = tip if contained else candidate
    divergent = not contained and git(root, 'merge-base', '--is-ancestor', tip, candidate,
                                      deadline=deadline, allowed=(0, 1)) is None
    decisions = payload.get('resolutions', {}).get(key, {})
    if divergent:
        expected = git(root, 'merge-tree', '--write-tree', '--no-messages', tip, candidate,
                       deadline=deadline, allowed=(0, 1))
        if expected is None:
            from development_git_history import reviewed_history_merge
            merge_target = reviewed_history_merge(source, tip, candidate, journal, Path(proof['worktree']).parent,
                                                  decisions, deadline)
            expected = merge_target
            divergent = False
    tree = git(root, 'rev-parse', expected + '^{tree}', deadline=deadline)
    overlay = journal / 'overlay'
    preserve = root == source and has_local_work(root, deadline)
    native = None
    if preserve and not divergent and not prior_failed and not contained:
        from development_git_native import native_fingerprint_if_disjoint
        native = native_fingerprint_if_disjoint(root, tip, merge_target, deadline)
        if native:
            preserve = False
            planned['local_fingerprint'] = native
            planned['merge_target'] = merge_target
    if preserve:
        prepare_overlay(root, tip, tree, overlay, deadline, resolutions=decisions)
    ensure_hooks(source, root, deadline)
    write_json(effect, {**planned, 'phase': 'merge', 'prediction': tree})
    if preserve:
        park_overlay(root, overlay, deadline)

    def unexpected_changes():
        if preserve:
            return not matches_parked_overlay(root, overlay, deadline)
        return not native and tracked_dirty(root, deadline)

    try:
        if native:
            from development_git_native import fingerprint
            if fingerprint(root, deadline) != native:
                raise ValueError('Local modifications changed before fast-forward')
        if unexpected_changes() or git(root, 'rev-parse', 'HEAD', deadline=deadline) != tip:
            raise ValueError('Integration inputs changed before the original Git hooks ran')
        if prior_failed:
            rerun_post_hooks(root, prior_failed, journal, deadline,
                             payload.get('failed_hook_args', {}).get(key) or failed_hook_args(journal),
                             checkout_source=source)
            if unexpected_changes():
                raise ValueError('The rechecked original integration hook modified tracked files')
        if not contained:
            with hooks(root, journal, deadline) as (opts, env):
                if not on_target() or git(root, 'rev-parse', 'HEAD', deadline=deadline) != tip:
                    raise ValueError('The checked-out branch or baseline changed before merge')
                strategy = ['--no-ff', '--no-edit'] if divergent else ['--ff-only']
                git(root, *opts, 'merge', *strategy, '--no-autostash', '--no-overwrite-ignore', merge_target,
                    deadline=deadline, env=env)
    except (ValueError, subprocess.TimeoutExpired):
        merged = git(root, 'rev-parse', branch, deadline=deadline)
        if merged == tip:
            if (divergent and on_target()
                    and git(root, 'rev-parse', '--verify', 'MERGE_HEAD', deadline=deadline, allowed=(0, 128)) == candidate
                    and not tracked_changes(root, deadline) and git(root, 'write-tree', deadline=deadline) == tree):
                git(root, 'merge', '--abort', deadline=deadline)
            if preserve:
                restore_original_overlay(root, overlay, deadline)
        elif git(root, 'merge-base', '--is-ancestor', candidate, merged, deadline=deadline, allowed=(0, 1)) is not None:
            if preserve:
                restore_overlay(root, overlay, deadline)
            return {**result, 'status': 'integration_retest_required', 'merged': merged, 'worktree': str(root),
                    'reason': 'Merge took effect but an original check failed; retest this revision',
                    'failed_hooks': failed_hooks(journal)}
        raise
    merged = git(root, 'rev-parse', branch, deadline=deadline)
    if not on_target():
        raise ValueError('The target checkout switched branches during integration')
    correct = merged == (tip if contained else merge_target)
    if divergent:
        correct = (git(root, 'rev-list', '--parents', '-n', '1', merged, deadline=deadline).split()
                   == [merged, tip, candidate] and git(root, 'rev-parse', merged + '^{tree}', deadline=deadline) == tree)
    if preserve:
        restored = restore_overlay(root, overlay, deadline)
        if restored.get('additional_checkout_changes'):
            return {**result, 'status': 'integration_retest_required', 'merged': merged, 'worktree': str(root),
                    'reason': 'Local edits restored; additional checkout changes appeared during integration. Verify this revision.'}
        verify_overlay(root, overlay, deadline)
        result['overlay_verified'] = True
    if native:
        result['local_fingerprint'] = native
        correct = correct and fingerprint(root, deadline) == native
    if not correct or (not native and not preserve and tracked_dirty(root, deadline)):
        return {**result, 'status': 'integration_retest_required', 'merged': merged, 'worktree': str(root),
                'reason': 'The integration result differs from its predicted tree; reconcile and retest'}
    if contained and key in payload.get('needs_retest', []) and not str(payload.get('verification', '')).strip():
        return {**result, 'status': 'integration_retest_required', 'merged': merged,
                'reason': 'Retest the checked merged revision and supply verification'}
    return {**result, 'merged': merged}


def integrate(proof, context, operation, key, journal, database, payload):
    deadline = context['deadline_at']
    source = Path(proof['source_root'])
    root, candidate_head = validate_worktree(proof, deadline)
    candidate = proof['candidate']
    if candidate_head != candidate or tracked_dirty(root, deadline):
        raise ValueError('The recorded coding candidate is no longer clean and current')
    branch = 'refs/heads/' + proof['target_branch']
    tip = git(source, 'rev-parse', '--verify', branch, deadline=deadline, allowed=(0, 128))
    if tip is None:
        remote = 'refs/remotes/origin/' + proof['target_branch']
        tip = git(source, 'rev-parse', '--verify', remote, deadline=deadline)
        git(source, 'branch', '--', proof['target_branch'], tip, deadline=deadline)
    checked = [Path(x['worktree']).resolve() for x in inventory(source, deadline) if x.get('branch') == branch]
    worktrees_root = database.resolve().parent / 'worktrees'
    temporary = worktrees_root / (proof['worktree_branch'].split('/')[-1] + '-integration') / key
    if temporary.resolve() != temporary or not temporary.is_relative_to(worktrees_root):
        raise ValueError('The integration checkout must remain inside the ledger worktree root')
    ownership_path = temporary.parent / (key + '-ownership.json')
    identity = {'marker': context['marker'], 'source_root': str(source), 'target_branch': proof['target_branch'],
                'worktree': str(temporary)}
    ownership = json.loads(ownership_path.read_text()) if ownership_path.exists() else None
    owned = {temporary} if ownership == identity else set()
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        for op, raw in connection.execute("SELECT operation_id,request FROM development_git_operations WHERE item_seq=? AND kind='integrate'", (context['item_seq'],)):
            old = json.loads(raw)['context']
            registered = old['repositories'].get(key, {})
            if (old['marker'] == context['marker'] and registered.get('source_root') == str(source)
                    and registered.get('target_branch') == proof['target_branch']):
                previous = database.parent / 'items' / context['marker'] / 'development-git' / op / 'host-git' / 'effects' / key
                effect_path = previous / 'integration.json'
                if effect_path.exists():
                    recorded = json.loads(effect_path.read_text())
                    legacy = previous / 'integration-worktree'
                    if recorded.get('worktree') == str(legacy):
                        owned.add(legacy)
    if len(checked) > 1 or any(path != source and path not in owned for path in checked):
        raise ValueError('The target branch is checked out in another user worktree')
    if temporary.exists() and temporary not in owned:
        raise ValueError('The integration checkout path lacks matching item ownership evidence')
    effect = journal / 'integration.json'
    earlier_effect = effect.is_file()
    planned = {'target': branch, 'candidate': candidate, 'before': tip}
    original_ref = git(source, 'symbolic-ref', '--quiet', 'HEAD', deadline=deadline, allowed=(0, 1)) or 'HEAD'
    if (proof.get('main_delivery') and not checked and not has_local_work(source, deadline)
            and not active_git_operation(source, deadline)):
        ensure_hooks(source, source, deadline, checkout=tip)
        write_json(effect, {**planned, 'worktree': str(source), 'phase': 'checkout'})
        with hooks(source, journal, deadline) as (opts, env):
            git(source, *opts, 'switch', '--no-guess', '--no-overwrite-ignore', '--', proof['target_branch'],
                deadline=deadline, env=env)
        checked = [source]
    root = checked[0] if checked else temporary
    if root != source and root.exists() and tracked_dirty(root, deadline):
        return {'status': 'integration_retest_required', 'candidate': candidate, 'merged': tip, 'worktree': str(root),
                'reason': 'Reconcile the previous changes in the item-owned integration checkout'}
    if not root.exists():
        if payload.get('failed_hooks', {}).get(key) or failed_hooks(journal):
            raise ValueError('The original integration hook checkout is unavailable for recheck')
        if git(source, 'merge-base', '--is-ancestor', candidate, tip, deadline=deadline, allowed=(0, 1)) is not None:
            if (earlier_effect or key in payload.get('needs_retest', [])) and not str(payload.get('verification', '')).strip():
                return {'status': 'integration_retest_required', 'candidate': candidate, 'merged': tip,
                        'reason': 'Verify interrupted integration and retest this merged revision'}
            return {'status': 'verified', 'candidate': candidate, 'merged': tip, 'already_contained': True,
                    'preserved_source_ref': original_ref, 'target_before': tip}
        ensure_hooks(source, source, deadline, checkout=tip)
        root.parent.mkdir(parents=True, exist_ok=True)
        write_json(ownership_path, identity)
        write_json(effect, {**planned, 'worktree': str(root), 'phase': 'checkout'})
        with hooks(source, journal, deadline) as (opts, env):
            git(source, *opts, 'worktree', 'add', '--', str(root), proof['target_branch'], deadline=deadline, env=env)
    result = integrate_checkout(source, root, proof, tip, journal, key, deadline, payload, earlier_effect)
    if root != source and result['status'] == 'verified':
        git(source, 'worktree', 'remove', '--', str(root), deadline=deadline)
        if proof.get('main_delivery'):
            result['preserved_source_ref'] = original_ref
    return result
