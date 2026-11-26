"""Materialize a reviewed history conflict in a detached, operation-owned checkout."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from development_git_effects import common_directory, ensure_hooks, git, hooks, inventory, tracked_dirty
from development_git_overlay import OverlayConflict
from stage_runner import write_json


def tree_entries(root, revision, deadline):
    rows = git(root, 'ls-tree', '-rz', revision, raw=True, deadline=deadline)
    result = {}
    for row in filter(None, rows.split('\0')):
        metadata, path = row.split('\t', 1)
        mode, _, oid = metadata.split()
        result[path] = [mode, oid]
    return result


def disk_identity(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return ('link', os.readlink(path))
    if getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError('Unexpected redirected path in history checkout: ' + str(path))
    if stat.S_ISDIR(info.st_mode):
        return ('directory',)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Unexpected special file in history checkout: ' + str(path))
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').digest()
    return ('file', info.st_mode & 0o111, digest)


def check_cleanup_files(source, root, tree, deadline):
    # Use Git's own checkout filters and EOL rules. Comparing normalized diffs alone
    # could discard a raw edit whose filtered blob happens to equal AUTO_MERGE.
    with tempfile.TemporaryDirectory(prefix='history-check-', dir=root.parent) as directory:
        staging = Path(directory)
        expected = staging / 'files'
        expected.mkdir()
        env = {'GIT_INDEX_FILE': str(staging / 'index')}
        git(root, 'read-tree', tree, env=env, deadline=deadline)
        git(root, 'checkout-index', '--all', '--prefix=' + expected.as_posix() + '/', env=env, deadline=deadline)

        def walk(relative):
            actual = root / relative
            template = expected / relative
            names = {entry.name for entry in actual.iterdir()}
            if not relative.parts:
                names.discard('.git')
            for name in names | {entry.name for entry in template.iterdir()}:
                item = relative / name
                path, ref = root / item, expected / item
                if (item == Path('.husky/_') and not os.path.lexists(ref) and path.is_dir()
                        and path.resolve() == path and (source / item).resolve() == source / item):
                    # ensure_hooks copies only ignored source runtime files. Any
                    # user-created or edited file here still has to be retained.
                    for runtime in path.iterdir():
                        original = source / item / runtime.name
                        if (not runtime.is_file() or runtime.is_symlink() or not original.is_file()
                                or disk_identity(runtime) != disk_identity(original)
                                or git(source, 'check-ignore', '--quiet', '--', (item / runtime.name).as_posix(),
                                       deadline=deadline, allowed=(0, 1)) is None):
                            raise ValueError('Unrecognized hook runtime in history checkout: ' + str(runtime))
                    continue
                if not os.path.lexists(path) or not os.path.lexists(ref) or disk_identity(path) != disk_identity(ref):
                    raise ValueError('Retained changed history checkout path: ' + str(path))
                if ref.is_dir() and not ref.is_symlink():
                    walk(item)
        walk(Path())


def remove_history_checkout(source, root, owner, deadline):
    stat_id = root.stat()
    if root.resolve() != root or owner.get('root_identity') != [stat_id.st_dev, stat_id.st_ino]:
        raise ValueError('History-resolution checkout ownership changed')
    entries = [entry for entry in inventory(source, deadline) if Path(entry['worktree']).resolve() == root]
    if not entries and not any(root.iterdir()):
        root.rmdir()
        return
    if common_directory(root, deadline) != common_directory(source, deadline):
        raise ValueError('History-resolution checkout ownership changed')
    if len(entries) != 1 or entries[0].get('branch') or git(root, 'symbolic-ref', '--quiet', 'HEAD', allowed=(0, 1), deadline=deadline):
        raise ValueError('Retained history checkout with a changed registration')
    head = git(root, 'rev-parse', 'HEAD', deadline=deadline)
    tip, candidate, resolved = owner['tip'], owner['candidate'], owner['resolved_tree']
    if head != tip and (git(root, 'rev-list', '--parents', '-1', head, deadline=deadline).split() != [head, tip, candidate]
                        or git(root, 'rev-parse', head + '^{tree}', deadline=deadline) != resolved):
        raise ValueError('Retained history checkout with an unrecognized HEAD')
    gitdir = Path(git(root, 'rev-parse', '--absolute-git-dir', deadline=deadline))
    lock, anchor = gitdir / 'index.lock', gitdir / 'history-cleanup.index'
    owned_lock = lock.exists() and anchor.exists() and lock.samefile(anchor)
    if ((lock.exists() and not owned_lock) or any((gitdir / name).exists() for name in
            ('rebase-apply', 'rebase-merge', 'sequencer', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'locked'))):
        raise ValueError('Retained history checkout with another Git operation in progress')
    merge_head = git(root, 'rev-parse', '--verify', 'MERGE_HEAD', deadline=deadline, allowed=(0, 128))
    if merge_head and (merge_head != candidate or head != tip):
        raise ValueError('Retained history checkout with an unrecognized merge')
    head_tree = git(root, 'rev-parse', head + '^{tree}', deadline=deadline)
    if any(record and (record[0].islower() or record.startswith('S ')) for record in
           git(root, 'ls-files', '-v', '-z', raw=True, deadline=deadline).split('\0')):
        raise ValueError('Retained history checkout with changed index flags')
    raw_index = (gitdir / 'index').read_bytes()
    index = git(root, 'ls-files', '--stage', '-z', raw=True, deadline=deadline)
    normal, conflicts = {}, {}
    for row in filter(None, index.split('\0')):
        metadata, path = row.split('\t', 1)
        mode, oid, stage = metadata.split()
        if stage == '0':
            normal[path] = [mode, oid]
        else:
            conflicts.setdefault(path, {})[{'1':'base', '2':'local', '3':'upstream'}[stage]] = [mode, oid]
    if conflicts:
        expected = git(root, 'rev-parse', '--verify', 'AUTO_MERGE^{tree}', deadline=deadline)
        clean_entries = {path: val for path, val in tree_entries(root, expected, deadline).items() if path not in conflicts}
        if merge_head != candidate or conflicts != owner['conflicts'] or normal != clean_entries:
            raise ValueError('Retained history checkout with changed conflict stages or other staged edits')
    else:
        expected = git(root, 'write-tree', deadline=deadline)
        resumed = (anchor.exists() and merge_head == candidate
                   and expected == git(root, 'rev-parse', '--verify', 'AUTO_MERGE^{tree}', deadline=deadline)
                   and expected == git(root, 'write-tree', env={'GIT_INDEX_FILE': str(anchor)}, deadline=deadline))
        if (normal != tree_entries(root, expected, deadline)
                or (expected != head_tree and not (merge_head == candidate and expected == resolved) and not resumed)):
            raise ValueError('Retained unrecognized staged edits in history checkout')
    check_cleanup_files(source, root, expected, deadline)
    # Normalize only the operation-owned merge state. Two-tree read-tree and
    # ordinary worktree removal retain Git's own concurrent-edit protection.
    if conflicts:
        # A stopped cleanup can recognize its own lock by inode. No foreign
        # staged edits can slip between the index comparison and replacement.
        if not owned_lock:
            git(root, 'read-tree', expected, deadline=deadline, env={'GIT_INDEX_FILE': str(anchor)})
            os.link(anchor, lock)
        if (gitdir / 'index').read_bytes() != raw_index:
            lock.unlink()
            raise ValueError('Retained a concurrently edited history-resolution index')
        os.replace(lock, gitdir / 'index')
    if expected != head_tree:
        git(root, 'update-index', '--refresh', deadline=deadline)
        git(root, 'read-tree', '-u', '-m', expected, head_tree, deadline=deadline)
    git(source, 'worktree', 'remove', '--', str(root), deadline=deadline)


def reviewed_history_merge(source, tip, candidate, journal, checkout_parent, decisions, deadline):
    output = git(source, 'merge-tree', '--write-tree', '--no-messages', '-z', tip, candidate,
                 raw=True, deadline=deadline, keep_output=True, allowed=(1,))
    predicted, *entries = output.rstrip('\0').split('\0')
    stages = {}
    names = {'1': 'base', '2': 'local', '3': 'upstream'}
    for entry in entries:
        metadata, path = entry.split('\t', 1)
        mode, oid, stage = metadata.split()
        stages.setdefault(path, {})[names[stage]] = [mode, oid]
    if not stages:
        raise ValueError('Git reported a history conflict without resolvable paths')
    identity = json.dumps([tip, candidate, stages], ensure_ascii=True, sort_keys=True).encode()
    conflict_id = hashlib.sha256(identity).hexdigest()
    receipt = {'status': 'integration_conflict', 'layer': 'history', 'conflict_id': conflict_id,
               'reason': 'The candidate and target history conflict; review the registered Git blobs',
               'conflicts': [{'path': path, **values} for path, values in stages.items()]}
    decision = decisions.get(conflict_id)
    if not decision:
        write_json(journal / 'history-conflict.json', receipt)
        raise OverlayConflict(receipt)
    files = decision['files']
    if not decision.get('verification') or files.keys() != stages.keys():
        raise ValueError('History resolution must cover the exact reported paths and review')
    env = {'GIT_INDEX_FILE': str(journal / 'resolved-history.index')}
    git(source, 'read-tree', predicted, deadline=deadline, env=env)
    for path, choice in files.items():
        versions = stages[path]
        if isinstance(choice, str):
            if any(line.startswith(('<<<<<<<', '|||||||', '=======', '>>>>>>>')) for line in choice.splitlines()):
                raise ValueError('Conflict markers remain in the supplied history resolution')
            modes = {mode for mode, _ in versions.values()}
            if len(modes) != 1 or not modes <= {'100644', '100755'}:
                raise ValueError('Select a Git side explicitly for conflicting file modes or links')
            content = journal / 'resolved-blob'
            content.write_bytes(choice.encode('utf-8'))
            oid = git(source, '--attr-source=' + tip, 'hash-object', '-w', '--path=' + path, '--', str(content), deadline=deadline)
            version = [modes.pop(), oid]
        else:
            take = choice['take']
            version = None if take == 'delete' else versions.get(take)
        if version is None:
            git(source, '--literal-pathspecs', 'update-index', '--force-remove', '--', path, env=env, deadline=deadline)
        else:
            mode, oid = version
            git(source, 'update-index', '--add', '--cacheinfo', mode, oid, path, deadline=deadline, env=env)
    tree = git(source, 'write-tree', env=env, deadline=deadline)

    # Hooks run in this checkout, before the caller brings the resolved commit into the target.
    # Git for Windows has a shorter internal GIT_DIR limit than core.longpaths.
    # Keep the checkout beside the registered item worktree, outside its source.
    token = hashlib.sha256(str(source).encode()).hexdigest()[:24]
    root = checkout_parent / ('resolve-' + token)
    mark = checkout_parent / ('resolve-' + token + '.owner.json')
    identity = {'tip': tip, 'candidate': candidate, 'resolved_tree': tree, 'conflicts': stages,
                'source': str(source), 'worktree': str(root)}
    if root.resolve() != root or mark.resolve() != mark:
        raise ValueError('Unexpected redirect of the history-resolution checkout')
    if root.exists():
        previous = json.loads(mark.read_text(encoding='utf-8')) if mark.is_file() else {}
        if previous.get('source') != str(source) or previous.get('worktree') != str(root):
            raise ValueError('Existing history-resolution checkout has a different owner')
        remove_history_checkout(source, root, previous, deadline)
    root.mkdir()
    stat_id = root.stat()
    identity['root_identity'] = [stat_id.st_dev, stat_id.st_ino]
    write_json(mark, identity)
    try:
        return create_history_commit(source, root, tip, candidate, tree, journal, deadline)
    finally:
        if root.exists():
            remove_history_checkout(source, root, identity, deadline)
        mark.unlink()


def create_history_commit(source, root, tip, candidate, tree, journal, deadline):
    ensure_hooks(source, source, deadline, checkout=tip)
    with hooks(source, journal / 'history-checkout-hooks', deadline) as (opts, inherited):
        git(source, *opts, 'worktree', 'add', '--detach', '--', str(root), tip, deadline=deadline, env=inherited)
    ensure_hooks(source, root, deadline)
    with hooks(root, journal / 'history-merge-hooks', deadline) as (opts, inherited):
        git(root, *opts, 'merge', '--no-commit', '--no-ff', '--no-autostash', candidate,
            deadline=deadline, allowed=(1,), env=inherited)
    if (git(root, 'rev-parse', '--verify', 'MERGE_HEAD', deadline=deadline) != candidate
            or git(root, 'rev-parse', 'HEAD', deadline=deadline) != tip):
        raise ValueError('History resolution does not match its reviewed merge parents')
    git(root, '--literal-pathspecs', 'restore', '--source=' + tree, '--staged', '--worktree', '--', '.',
        deadline=deadline)
    if git(root, 'write-tree', deadline=deadline) != tree:
        raise ValueError('Resolved history index differs from its reviewed inputs')
    with hooks(root, journal / 'history-commit-hooks', deadline) as (opts, inherited):
        git(root, *opts, 'commit', '-m', 'fix: reconcile reviewed integration conflict', deadline=deadline, env=inherited)
    merged = git(root, 'rev-parse', 'HEAD', deadline=deadline)
    if (git(root, 'rev-list', '--parents', '-n', '1', merged, deadline=deadline).split()
            != [merged, tip, candidate] or git(root, 'rev-parse', 'HEAD^{tree}', deadline=deadline) != tree
            or tracked_dirty(root, deadline)):
        raise ValueError('A hook changed the reviewed history resolution; reconcile and verify again')
    return merged
