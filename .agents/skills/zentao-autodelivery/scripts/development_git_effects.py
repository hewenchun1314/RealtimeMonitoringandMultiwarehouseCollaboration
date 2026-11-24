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
from development_git import relative_files
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



def git(root, *args, deadline, allowed=(0,), env=None):
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
    return process.stdout.strip() if process.returncode == 0 else None


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
def hooks(root, journal, deadline):
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
            script = ("#!/bin/sh\n"
                      "if [ \"$ZENTAO_PARENT_GIT_CONFIG_PRESENT\" = 1 ]; then\n"
                      "  export GIT_CONFIG_PARAMETERS=\"$ZENTAO_PARENT_GIT_CONFIG_PARAMETERS\"\n"
                      "else unset GIT_CONFIG_PARAMETERS; fi\n"
                      + 'printf \'%s\\0\' "$@" > ' + shlex.quote((reports / (hook.name + '.args')).as_posix()) + '\n'
                      + shlex.quote(hook.resolve().as_posix()) + ' "$@"\n'
                      + 'status=$?\nprintf \"%s\\n\" "$status" > '
                      + shlex.quote((reports / (hook.name + '.exit')).as_posix()) + '\nexit "$status"\n')
            wrapper = wrappers / hook.name
            wrapper.write_text(script, encoding='utf-8', newline='\n')
            wrapper.chmod(0o755)
    env = {'ZENTAO_PARENT_GIT_CONFIG_PRESENT': '1' if 'GIT_CONFIG_PARAMETERS' in os.environ else '0',
           'ZENTAO_PARENT_GIT_CONFIG_PARAMETERS': os.environ.get('GIT_CONFIG_PARAMETERS', '')}
    yield ['-c', 'core.hooksPath=' + str(wrappers)], env
    if any(p.read_text().strip() != '0' for p in reports.glob('*.exit')):
        raise ValueError('An original Git hook failed; fix its checks before retrying')


def ensure_hooks(source, root, deadline):
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
    names = ('pre-commit', 'prepare-commit-msg', 'commit-msg', 'post-commit', 'pre-merge-commit', 'post-merge')
    for name in names:
        if (source_hooks / name).is_file() and not (item_hooks / name).is_file():
            raise ValueError('Install the registered repository hooks in this item worktree before host Git work')
    if source_hooks.name == '_' and source_hooks.parent.name == '.husky':
        for name in names:
            if ((source_hooks.parent / name).is_file()
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


def rerun_post_hooks(root, names, journal, deadline, arguments=None):
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
        # Unlike ordinary commit/merge, explicit hook run propagates a post-hook
        # failure. It runs the effective original hook, never a model command.
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
                         (payload or {}).get('failed_hook_args') or failed_hook_args(journal))
    target = {'branch': branch, 'repository': '.'}
    inspected = inspect_repository(source, target, proof['worktree_branch'], str(worktree))
    if inspected['status'] != 'verified' or tracked_dirty(worktree, deadline):
        raise ValueError('The prepared worktree or hook-generated files require reconciliation')
    return inspected


def commit(proof, entry, context, operation, key, journal):
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
    if tracked_dirty(source, deadline):
        raise ValueError('The source checkout has tracked/index edits; preserve and reconcile them')
    if temporary.exists() and temporary not in owned:
        raise ValueError('The integration checkout path lacks matching item ownership evidence')
    merge_root = checked[0] if checked else temporary
    if merge_root.exists() and tracked_dirty(merge_root, deadline):
        return {'status': 'integration_retest_required', 'candidate': candidate, 'merged': tip,
                'worktree': str(merge_root), 'reason': 'Clean and retest the original item-owned integration checkout'}
    effect = journal / 'integration.json'
    contained = git(source, 'merge-base', '--is-ancestor', candidate, tip, deadline=deadline, allowed=(0, 1)) is not None
    prior_failed = payload.get('failed_hooks', {}).get(key) or failed_hooks(journal)
    if prior_failed:
        if not merge_root.exists():
            raise ValueError('The original integration hook checkout is unavailable for recheck')
        ensure_hooks(source, merge_root, deadline)
        rerun_post_hooks(merge_root, prior_failed, journal, deadline,
                         payload.get('failed_hook_args', {}).get(key) or failed_hook_args(journal))
        if tracked_dirty(merge_root, deadline):
            return {'status': 'integration_retest_required', 'candidate': candidate, 'merged': tip,
                    'worktree': str(merge_root), 'reason': 'The rechecked original integration hook modified tracked files'}
    if contained:
        if (key in payload.get('needs_retest', []) or effect.exists()) and not str(payload.get('verification', '')).strip():
            return {'status': 'integration_retest_required', 'candidate': candidate, 'merged': tip,
                    'worktree': str(merge_root), 'reason': 'Verify failed or interrupted integration hooks and retest this merged SHA'}
        if merge_root != source and merge_root.exists():
            git(source, 'worktree', 'remove', '--', str(merge_root), deadline=deadline)
        return {'status': 'verified', 'candidate': candidate, 'merged': tip}
    if git(source, 'merge-base', '--is-ancestor', tip, candidate, deadline=deadline, allowed=(0, 1)) is None:
        raise ValueError('This fixed operation supports only fast-forward/already-contained results; resolve divergence through a separate controlled path')
    if merge_root == temporary and not temporary.exists():
        temporary.parent.mkdir(parents=True, exist_ok=True)
        write_json(ownership_path, identity)
        write_json(effect, {'target': branch, 'candidate': candidate, 'before': tip, 'worktree': str(merge_root)})
        with hooks(source, journal, deadline) as (opts, env):
            git(source, *opts, 'worktree', 'add', '--', str(temporary), proof['target_branch'], deadline=deadline, env=env)
    else:
        write_json(effect, {'target': branch, 'candidate': candidate, 'before': tip, 'worktree': str(merge_root)})
    ensure_hooks(source, merge_root, deadline)
    try:
        with hooks(merge_root, journal, deadline) as (opts, env):
            git(merge_root, *opts, 'merge', '--ff-only', '--no-autostash', '--no-overwrite-ignore', candidate, deadline=deadline, env=env)
    except ValueError:
        merged = git(source, 'rev-parse', branch, deadline=deadline)
        if git(source, 'merge-base', '--is-ancestor', candidate, merged, deadline=deadline, allowed=(0, 1)) is not None:
            return {'status': 'integration_retest_required', 'candidate': candidate, 'merged': merged,
                    'worktree': str(merge_root), 'reason': 'Merge took effect but an original hook failed; fix and retest',
                    'failed_hooks': failed_hooks(journal)}
        raise
    merged = git(source, 'rev-parse', branch, deadline=deadline)
    if tracked_dirty(merge_root, deadline):
        return {'status': 'integration_retest_required', 'candidate': candidate, 'merged': merged,
                'worktree': str(merge_root), 'reason': 'Integration hooks changed tracked/index files; clean and retest before success'}
    if merge_root != source:
        git(source, 'worktree', 'remove', '--', str(merge_root), deadline=deadline)
    return {'status': 'verified', 'candidate': candidate, 'merged': merged}
