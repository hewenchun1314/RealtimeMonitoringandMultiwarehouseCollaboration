"""Preflight and restore the staged and unstaged layers around a host integration."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import tempfile


class OverlayConflict(ValueError):
    def __init__(self, receipt, directory=None):
        location = f" in {directory / 'conflict.json'}" if directory else ""
        super().__init__(f"Resolve the {receipt['layer']} integration conflict{location}")
        self.receipt = receipt


def _run(root, deadline, *args, data=None, env=None, allowed=(0,)):
    budget = (datetime.fromisoformat(deadline) - datetime.now(timezone.utc)).total_seconds()
    if budget <= 0:
        raise ValueError('The host Git deadline elapsed during workspace preservation')
    environment = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_LITERAL_PATHSPECS='1')
    environment.update(env or {})
    result = subprocess.run(['git', '-C', str(root), '-c', 'core.longpaths=true', *args], input=data,
                            capture_output=True, timeout=budget, env=environment)
    if result.returncode not in allowed:
        raise ValueError('Workspace preservation Git check failed: ' + args[0] + ' / ' +
                         result.stderr.decode('utf-8', 'replace').strip()[-600:])
    return result


def _oid(root, deadline, *args, **kwargs):
    return _run(root, deadline, *args, **kwargs).stdout.decode('ascii').strip()


def _digest(data):
    return sha256(data).hexdigest()


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        name = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            name.unlink(missing_ok=True)
            raise
    try:
        os.replace(name, path)
    finally:
        name.unlink(missing_ok=True)


def _json(path, data):
    _atomic(path, json.dumps(data, sort_keys=True, ensure_ascii=False).encode('utf-8'))


def _unsafe(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _path(root, name, *, leaf_link=False):
    parts = name.split('/')
    if any(part in ('', '.', '..') or part.lower().rstrip(' .') == '.git'
           or '\\' in part or ':' in part or (os.name == 'nt' and
           (part.endswith((' ', '.')) or getattr(os.path, 'isreserved', lambda value: False)(part))) for part in parts):
        raise ValueError('Invalid literal overlay path: ' + name)
    result = root
    for i, part in enumerate(parts):
        result = result / part
        if (not leaf_link or i < len(parts) - 1) and _unsafe(result):
            raise ValueError('The overlay path contains a symlink or reparse point: ' + name)
    return result


def _state(source, directory):
    if directory.resolve() != directory.absolute():
        raise ValueError('The overlay journal path has changed')
    path = _path(directory, 'state.json')
    state = json.loads(path.read_bytes())
    if state['source'] != str(source.resolve()) or state['version'] != 1:
        raise ValueError('The overlay journal belongs to another checkout')
    return state


def _save(directory, state, phase):
    state['phase'] = phase
    _json(_path(directory, 'state.json'), state)
    return state


def _intent_paths(source, deadline, env=None):
    args = ('diff', '--cached', '--name-only', '--no-renames', '-z')
    visible = _run(source, deadline, *args, '--ita-visible-in-index', env=env).stdout.split(b'\0')
    invisible = _run(source, deadline, *args, '--ita-invisible-in-index', env=env).stdout.split(b'\0')
    return sorted(p.decode('utf-8') for p in set(visible) - set(invisible) if p)


def _set_intents(source, directory, deadline, expected, allowed, shadow):
    current = set(_intent_paths(source, deadline, shadow))
    if current - set(allowed):
        raise ValueError('A new intent-to-add entry appeared during workspace preservation')
    added, removed = set(expected) - current, current - set(expected)
    if removed:
        args = b''.join(p.encode() + b'\0' for p in sorted(removed))
        _run(source, deadline, 'update-index', '--force-remove', '-z', '--stdin', data=args, env=shadow)
    if added:
        with tempfile.TemporaryDirectory(dir=directory) as tmp:
            for name in added:
                file = _path(Path(tmp), name)
                file.parent.mkdir(parents=True, exist_ok=True)
                file.touch()
            args = b''.join(p.encode() + b'\0' for p in sorted(added))
            _run(source, deadline, 'add', '-N', '--pathspec-from-file=-', '--pathspec-file-nul',
                 data=args, env={**shadow, 'GIT_WORK_TREE': tmp})


def _flags(source, deadline, shadow=None):
    records = _run(source, deadline, 'ls-files', '-v', '-z', env=shadow).stdout.split(b'\0')
    return {rec[2:].decode(): rec[:1].decode() for rec in records if rec[:1] in (b'h', b'S', b's')}


def _change_flags(source, deadline, old, desired, scope, shadow):
    for option, types in [('assume-unchanged', {'h', 's'}), ('skip-worktree', {'S', 's'})]:
        for enable in (False, True):
            changed = [p for p in scope if (old.get(p) in types) != (desired.get(p) in types)
                       and (desired.get(p) in types) == enable]
            if changed:
                args = b''.join(p.encode() + b'\0' for p in sorted(changed))
                _run(source, deadline, 'update-index', '--' + ('' if enable else 'no-') + option,
                     '-z', '--stdin', data=args, env=shadow)


def _renames(source, deadline, old, new):
    fields = iter(_run(source, deadline, 'diff-tree', '-r', '-M', '--no-commit-id', '--name-status',
                       '-z', old, new).stdout.split(b'\0'))
    renamed = {}
    for status in fields:
        if status:
            path = next(fields).decode()
            if status.startswith(b'R'):
                renamed[path] = next(fields).decode()
    return renamed


def _snapshot(source, directory, deadline):
    if _run(source, deadline, 'ls-files', '-u', '-z').stdout:
        raise ValueError('Resolve the existing unmerged index before starting another integration')
    location = _run(source, deadline, 'rev-parse', '--path-format=absolute', '--git-path', 'index')
    index_path = Path(location.stdout.decode().strip())
    with tempfile.TemporaryDirectory(dir=directory) as temp:
        shadow_path = Path(temp) / 'index'
        shadow_path.write_bytes(index_path.read_bytes())
        shadow = {'GIT_INDEX_FILE': str(shadow_path)}
        index = _oid(source, deadline, 'write-tree', env=shadow)
        # Inspect actual bytes even if ordinary git status hides a materialized path.
        # An absent skip-worktree entry may be a deliberately sparse checkout.
        flags = _flags(source, deadline, shadow)
        inspect = [p for p, flag in flags.items() if flag == 'h' or os.path.lexists(_path(source, p, leaf_link=True))]
        _change_flags(source, deadline, flags, {}, inspect, shadow)
        changed = _run(source, deadline, 'diff-files', '--no-renames', '--name-only', '-z', env=shadow).stdout
        if changed:
            _run(source, deadline, 'add', '--sparse', '-u', '--pathspec-from-file=-',
                 '--pathspec-file-nul', data=changed, env=shadow)
        work = _oid(source, deadline, 'write-tree', env=shadow)
    return index, work


def _changed(source, deadline, a, b):
    if a == b:
        return set()
    raw = _run(source, deadline, 'diff-tree', '--no-commit-id', '--name-only', '-r', '-z',
               '--no-renames', a, b).stdout
    return {p.decode('utf-8') for p in raw.split(b'\0') if p}


def _entries(source, deadline, tree, names):
    result = {}
    if not names:
        return result
    # Read the tree once, avoiding Windows' command-line length limit on large selections.
    raw = _run(source, deadline, 'ls-tree', '-rz', tree).stdout
    for entry in raw.split(b'\0'):
        if not entry:
            continue
        props, raw_path = entry.split(b'\t', 1)
        path = raw_path.decode('utf-8')
        if path in names:
            mode, _, oid = props.decode('ascii').split()
            result[path] = (mode, oid)
    return result


def _commit(source, deadline, tree):
    identity = {key: value for role in ('AUTHOR', 'COMMITTER') for key, value in
                [(f'GIT_{role}_NAME', 'ZenTao overlay'), (f'GIT_{role}_EMAIL', 'host@localhost'),
                 (f'GIT_{role}_DATE', '2000-01-01T00:00:00Z')]}
    return _oid(source, deadline, 'commit-tree', tree, '-m', 'Temporary overlay tree', env=identity)


def _combine(source, directory, deadline, state, layer, base, upstream, local, resolutions):
    if base == local or upstream == local:
        return upstream
    if base == upstream:
        return local
    old, new, edits = [_commit(source, deadline, tree) for tree in (base, upstream, local)]
    result = _run(source, deadline, 'merge-tree', '-z', '--no-messages', '--merge-base=' + old,
                  new, edits, allowed=(0, 1))
    chunks = result.stdout.split(b'\0')
    merged = chunks[0].decode('ascii')
    if result.returncode == 0:
        return merged
    conflicts = {}
    for record in filter(None, chunks[1:]):
        info, name = record.split(b'\t', 1)
        name = name.decode('utf-8')
        _path(source, name, leaf_link=True)
        mode, oid, stage = info.decode('ascii').split()
        conflicts.setdefault(name, {})[{'1': 'base', '2': 'upstream', '3': 'local'}[stage]] = [mode, oid]
    if not conflicts:
        raise ValueError('Git could not represent this structural overlay conflict')
    receipt = {'layer': layer, 'before': state['before'], 'merged_tree': state['merged_tree'],
               'base_tree': base, 'upstream_tree': upstream, 'local_tree': local, 'source': state['source'],
               'index_tree': state['index_tree'], 'work_tree': state['work_tree'], 'conflicts': []}
    for name, sides in sorted(conflicts.items()):
        token = _digest(name.encode('utf-8'))
        folder = f'conflicts/{layer}/{token}/'
        details = {'path': name, 'token': token, 'sides': sides}
        receipt['conflicts'].append(details)
        _json(_path(directory, folder + 'details.json'), details)
        for side, (mode, oid) in sides.items():
            if mode != '160000':
                _atomic(_path(directory, folder + side), _run(source, deadline, 'cat-file', 'blob', oid).stdout)
        marked = _entries(source, deadline, merged, {name}).get(name)
        if marked and marked[0] != '160000':
            _atomic(_path(directory, folder + 'merged'), _run(source, deadline, 'cat-file', 'blob', marked[1]).stdout)
    conflict_id = _digest(json.dumps(receipt, sort_keys=True).encode())
    receipt['conflict_id'] = conflict_id
    _json(_path(directory, 'conflict.json'), receipt)
    provided = (resolutions or {}).get(conflict_id, {})
    if not provided:
        raise OverlayConflict(receipt, directory)
    files = provided.get('files', {})
    if not str(provided.get('verification', '')).strip() or set(files) != set(conflicts):
        raise ValueError('Reviewed overlay resolution must cover exactly the listed conflicts')
    with tempfile.TemporaryDirectory(dir=directory) as temp:
        shadow = {'GIT_INDEX_FILE': str(Path(temp) / 'index')}
        _run(source, deadline, 'read-tree', merged, env=shadow)
        for name, content in files.items():
            sides = conflicts[name]
            if isinstance(content, dict) and content.get('take') in ('base', 'local', 'upstream', 'delete'):
                entry = sides.get(content['take'])
            elif isinstance(content, str):
                if any(line.startswith(('<<<<<<<', '|||||||', '=======', '>>>>>>>')) for line in content.splitlines()):
                    raise ValueError('Conflict markers remain in the supplied overlay resolution')
                modes = {item[0] for item in sides.values()}
                if len(modes) != 1 or not modes.issubset({'100644', '100755'}):
                    raise ValueError('Choose an explicit side to resolve this file mode conflict')
                oid = _oid(source, deadline, '--attr-source=' + upstream, 'hash-object', '-w',
                           '--path=' + name, '--stdin', data=content.encode())
                entry = (modes.pop(), oid)
            else:
                raise ValueError('Invalid overlay resolution content')
            if entry:
                _run(source, deadline, 'update-index', '--add', '--cacheinfo', entry[0], entry[1], name, env=shadow)
            else:
                _run(source, deadline, 'update-index', '--force-remove', '--', name, env=shadow)
        return _oid(source, deadline, 'write-tree', env=shadow)


def _record_blob(directory, data):
    digest = _digest(data)
    path = _path(directory, 'bytes/' + digest)
    if not path.exists():
        _atomic(path, data)
    return digest


def _raw(path):
    if path.is_symlink():
        return os.fsencode(os.readlink(path))
    return path.read_bytes()


def _disk(path):
    if path.is_symlink():
        return [_digest(_raw(path)), 'symlink']
    if _unsafe(path):
        raise ValueError('A file has become a symlink or reparse point: ' + str(path))
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('A workspace directory or special file collides with a tracked path: ' + str(path))
    return [_digest(path.read_bytes()), bool(info.st_mode & stat.S_IXUSR) if os.name != 'nt' else False]


def _materialize(source, directory, deadline, name, tree, entry):
    if not entry:
        return None
    mode, oid = entry
    if mode == '120000':
        link = _run(source, deadline, 'config', '--bool', '--get', 'core.symlinks', allowed=(0, 1))
        as_link = link.stdout.strip() != b'false'
        data = _run(source, deadline, 'cat-file', 'blob', oid).stdout
        return [_record_blob(directory, data), 'symlink' if as_link else False]
    if mode not in ('100644', '100755'):
        raise ValueError('The modified path has a non-regular Git mode: ' + name)
    data = _run(source, deadline, '--attr-source=' + tree, 'cat-file', '--filters', '--path=' + name, oid).stdout
    return [_record_blob(directory, data), mode == '100755' and os.name != 'nt']


def _nested_state(source, names, deadline):
    details = {}
    for name in names:
        path = _path(source, name)
        if not path.is_dir() or not (path / '.git').exists():
            details[name] = None
            continue
        if Path(_run(path, deadline, 'rev-parse', '--show-toplevel').stdout.decode().strip()).resolve() != path:
            raise ValueError('Submodule directory belongs to a different repository: ' + name)
        args = ('diff', '--binary', '--no-ext-diff', '--no-textconv', '--full-index')
        details[name] = {'head': _oid(path, deadline, 'rev-parse', 'HEAD'),
                         'index': _digest(_run(path, deadline, *args, '--cached').stdout),
                         'worktree': _digest(_run(path, deadline, *args).stdout)}
    return details


def _with_nested_work(source, directory, deadline, tree, entries, links):
    present = sorted(set(links) & entries.keys())
    if not present:
        return tree
    with tempfile.TemporaryDirectory(dir=directory) as temp:
        shadow = {'GIT_INDEX_FILE': str(Path(temp) / 'index')}
        _run(source, deadline, 'read-tree', tree, env=shadow)
        data = b''.join(p.encode() + b'\0' for p in present)
        _run(source, deadline, 'add', '-u', '--pathspec-from-file=-', '--pathspec-file-nul',
             data=data, env=shadow)
        return _oid(source, deadline, 'write-tree', env=shadow)


def _disk_plan(source, directory, deadline, state, flags):
    old, idx, work = state['before_tree'], state['index_tree'], state['work_tree']
    merged, newidx, newwork = state['merged_tree'], state['new_index_tree'], state['new_work_tree']
    names = (_changed(source, deadline, old, idx) | _changed(source, deadline, idx, work) |
             _changed(source, deadline, merged, newidx) | _changed(source, deadline, newidx, newwork))
    status = _run(source, deadline, '--no-optional-locks', 'status', '--porcelain=v1', '-z', '--no-renames',
                  '--untracked-files=no', '--ignore-submodules=untracked').stdout
    local_paths = {rec[3:].decode() for rec in status.split(b'\0') if rec}
    trees = {tree: _entries(source, deadline, tree, names | local_paths) for tree in {old, idx, work, merged, newidx, newwork}}
    links = {name for entries in trees.values() for name, (mode, _) in entries.items() if mode == '160000'}
    for name in links:
        if (trees[old].get(name) != trees[merged].get(name)
                or any(entries[name][0] != '160000' for entries in trees.values() if name in entries)):
            raise ValueError('A changed submodule checkout overlaps the candidate gitlink: ' + name)
    state['nested'] = _nested_state(source, sorted(links), deadline)
    state['parked_work_tree'] = _with_nested_work(source, directory, deadline, old, trees[old], links)
    state['merged_work_tree'] = _with_nested_work(source, directory, deadline, merged, trees[merged], links)
    paths = {}
    for name in sorted(names - links):
        path = _path(source, name, leaf_link=True)
        original = _disk(path)
        work_entry = trees[work].get(name)
        before_entry = trees[old].get(name)
        if original is None and flags.get(name) in ('S', 's') and work_entry:
            # Sparse worktree entries have index content but deliberately have no disk file.
            paths[name] = dict.fromkeys(('original', 'parked', 'merged', 'restored'))
            continue
        cached_deletion = (original and not work_entry and before_entry and not trees[idx].get(name)
                           and not trees[newidx].get(name) and not trees[newwork].get(name))
        if not cached_deletion and bool(original) != bool(work_entry):
            raise ValueError('An untracked file or concurrent change collides with the overlay: ' + name)
        if original and _record_blob(directory, _raw(path)) != original[0]:
            raise ValueError('The file changed while snapshotting: ' + name)
        merged_entry = trees[merged].get(name)
        new_entry = trees[newwork].get(name)
        parked = original if before_entry == work_entry else _materialize(source, directory, deadline, name, old, before_entry)
        updated = parked if merged_entry == before_entry else _materialize(source, directory, deadline, name, merged, merged_entry)
        restored = original if new_entry == work_entry else _materialize(source, directory, deadline, name, newwork, new_entry)
        paths[name] = {'original': original, 'parked': parked, 'merged': updated, 'restored': restored}
    return paths


def prepare_overlay(source, before, merged, directory, deadline, resolutions=None):
    source, directory = Path(source).resolve(), Path(directory).absolute()
    if directory.resolve() != directory:
        raise ValueError('The overlay journal cannot use a symlink or reparse point')
    directory.mkdir(parents=True, exist_ok=True)
    merged_tree = _oid(source, deadline, 'rev-parse', '--verify', merged + '^{tree}')
    path = _path(directory, 'state.json')
    if path.exists():
        state = _state(source, directory)
        if (before, merged_tree) != (state['before'], state['merged_tree']):
            raise ValueError('The saved overlay belongs to a different target or candidate')
        if state['phase'] != 'planning':
            return state
    else:
        state = {'version': 1, 'source': str(source), 'before': before,
                 'before_tree': _oid(source, deadline, 'rev-parse', before + '^{tree}'),
                 'branch': _oid_ref(source, deadline), 'merged_tree': merged_tree, 'token': secrets.token_hex(12)}
    if _oid(source, deadline, 'rev-parse', 'HEAD') != before or _oid_ref(source, deadline) != state['branch']:
        raise ValueError('Target checkout moved before overlay preparation')
    git_dir = Path(_run(source, deadline, 'rev-parse', '--absolute-git-dir').stdout.decode('utf-8').strip())
    if any((git_dir / marker).exists() for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'sequencer',
                                                        'rebase-apply', 'rebase-merge')):
        raise ValueError('Another Git merge, rebase, or sequencer operation owns this checkout')
    index, work = _snapshot(source, directory, deadline)
    if 'work_tree' in state and (index, work) != (state['index_tree'], state['work_tree']):
        raise ValueError('Local edits changed after the overlay conflict was recorded; start a fresh preflight')
    intents = _intent_paths(source, deadline)
    flags = _flags(source, deadline)
    if 'initial_flags_digest' in state and state['initial_flags_digest'] != _digest(json.dumps(flags, sort_keys=True).encode()):
        raise ValueError('Index flags changed while preparing the overlay')
    state['initial_flags_digest'] = _digest(json.dumps(flags, sort_keys=True).encode())
    if 'intent_paths' in state and intents != state['intent_paths']:
        raise ValueError('Intent-to-add entries changed while preparing the overlay')
    state.update(index_tree=index, work_tree=work, intent_paths=intents)
    _save(directory, state, 'planning')
    newidx = _combine(source, directory, deadline, state, 'index', state['before_tree'], merged_tree, index, resolutions)
    newwork = _combine(source, directory, deadline, state, 'worktree', index, newidx, work, resolutions)
    state.update(new_index_tree=newidx, new_work_tree=newwork)
    state['restored_intents'] = sorted(set(intents) - set(_entries(source, deadline, newidx, intents)))
    state['paths'] = _disk_plan(source, directory, deadline, state, flags)
    affected = set(state['paths']) | _changed(source, deadline, before, merged_tree)
    state['flags'] = {p: flag for p, flag in flags.items() if p in affected}
    before_flags = _entries(source, deadline, state['before_tree'], state['flags'])
    sparse = {p for p, flag in state['flags'].items() if flag in ('S', 's')
              and not os.path.lexists(_path(source, p, leaf_link=True))}
    state['parked_flags'] = {p: state['flags'][p] for p in sparse if p in before_flags}
    state['restored_flags'] = {}
    state['sparse_materializations'] = {}
    if state['flags']:
        renamed = _renames(source, deadline, index, newidx)
        after = {renamed.get(p, p): flag for p, flag in state['flags'].items()}
        kept = _entries(source, deadline, newidx, set(after))
        state['restored_flags'] = {p: flag for p, flag in after.items() if p in kept}
        final_sparse = {renamed.get(p, p) for p in sparse} & kept.keys()
        upstream = _entries(source, deadline, merged_tree, final_sparse)
        for name in final_sparse:
            if name not in upstream or upstream[name] == before_flags.get(name):
                continue
            if os.path.lexists(_path(source, name, leaf_link=True)):
                raise ValueError('A file occupies the destination of a sparse overlay entry: ' + name)
            state['paths'].setdefault(name, dict.fromkeys(('original', 'parked', 'merged', 'restored')))
            state['sparse_materializations'][name] = _materialize(source, directory, deadline, name,
                                                                   merged_tree, upstream[name])
    # Save Git objects independently from the normal dangling-object GC horizon.
    revisions = '\n'.join([index, work, newidx, newwork, state['parked_work_tree'], state['merged_work_tree'],
                           '^' + before, '^' + merged_tree, '']).encode()
    packed = _run(source, deadline, 'pack-objects', '--stdout', '--revs', data=revisions).stdout
    _atomic(_path(directory, 'objects.pack'), packed)
    return _save(directory, state, 'prepared')


def _oid_ref(source, deadline):
    return _run(source, deadline, 'symbolic-ref', '--quiet', 'HEAD').stdout.decode('utf-8').strip()


def _link_move(from_path, to_path):
    # LINK has create-if-absent semantics. A new user file always wins this race.
    if os.name == 'nt':
        os.rename(from_path, to_path)
    else:
        os.link(from_path, to_path, follow_symlinks=False)
        from_path.unlink()


def _install(source, directory, name, old, new, token):
    path = _path(source, name, leaf_link=True)
    hold = path.with_name('.zentao-overlay-' + token + '-' + _digest(name.encode())[:12])
    if _unsafe(hold) and not hold.is_symlink():
        raise ValueError('The reserved recovery path is occupied: ' + str(hold))
    observed, saved = _disk(path), _disk(hold)
    if saved is not None and saved != old:
        if observed is None:
            _link_move(hold, path)
        raise ValueError('Concurrent edit preserved at ' + str(path))
    if observed == new:
        if saved is not None:
            hold.unlink()
        return
    if observed != old and not (observed is None and saved is not None):
        raise ValueError('File changed after overlay preflight: ' + name)
    if saved is not None and observed is not None:
        raise ValueError('Workspace file was recreated during overlay recovery: ' + name)
    if observed is not None:
        # The hold resides on the same filesystem as the live path.
        if os.path.lexists(hold):
            raise ValueError('Reserved workspace path is occupied: ' + str(hold))
        os.rename(path, hold)
        if _disk(hold) != old:
            if not os.path.lexists(path):
                _link_move(hold, path)
            raise ValueError('Concurrent change while parking file: ' + name)
    if new is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _path(source, name)
        data = _path(directory, 'bytes/' + new[0]).read_bytes()
        if _digest(data) != new[0]:
            raise ValueError('The saved overlay file does not match its fingerprint')
        if new[1] == 'symlink':
            os.symlink(os.fsdecode(data), path)
        else:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.zentao-overlay-write-', delete=False) as stream:
                staging = Path(stream.name)
                try:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                finally:
                    stream.close()
            try:
                if os.name != 'nt':
                    staging.chmod(0o755 if new[1] else 0o644)
                _link_move(staging, path)
            finally:
                staging.unlink(missing_ok=True)
    if _disk(path) != new:
        raise ValueError('Workspace changed while restoring file: ' + name)
    if saved is not None or old is not None:
        hold.unlink(missing_ok=True)


def _outside(state, changes):
    protected = set(state['paths']) | set(state['intent_paths']) | set(state['nested'])
    components = [Path(os.path.normcase(name)).parts for name in protected]
    for changed in changes:
        parts = Path(os.path.normcase(changed)).parts
        for other in components:
            common = min(len(parts), len(other))
            if parts[:common] == other[:common]:
                return False
    return True


def _compare_snapshot(source, directory, deadline, state, index, work, allow_extra):
    actual_index, actual_work = _snapshot(source, directory, deadline)
    changes = _changed(source, deadline, index, actual_index) | _changed(source, deadline, work, actual_work)
    if changes and not (allow_extra and _outside(state, changes)):
        raise ValueError('Workspace or index differs from the preserved overlay paths')
    return bool(changes)


def _install_index(source, directory, deadline, state, old_tree, new_tree, original_intents, wanted, allow_extra):
    # A compare-and-swap under Git's index.lock keeps editor/Git staging changes intact.
    index = Path(_run(source, deadline, 'rev-parse', '--path-format=absolute', '--git-path', 'index').stdout.decode().strip())
    lock = index.with_name(index.name + '.lock')
    journal = _path(directory, 'index-install.json')
    saved = json.loads(journal.read_bytes()) if journal.exists() else None
    if not saved:
        expected_flags = {'parking': 'parked_flags', 'rolling_back': 'flags', 'restoring': 'restored_flags'}
        wanted_flags = state[expected_flags[state['phase']]]
        flag_scope = set(state['flags']) | set(state['restored_flags'])
        actual = index.read_bytes()
        with tempfile.NamedTemporaryFile(dir=index.parent, prefix='zentao-' + state['token'] + '-',
                                         suffix='.index', delete=False) as stream:
            shadow = Path(stream.name)
            stream.write(actual)
        env = {'GIT_INDEX_FILE': str(shadow)}
        try:
            current = _oid(source, deadline, 'write-tree', env=env)
            intents = _intent_paths(source, deadline, env)
            extras = sorted(set(intents) - set(original_intents) - set(wanted))
            if extras and not (allow_extra and _outside(state, extras)):
                raise ValueError('The intent-to-add entries changed during overlay transition')
            wanted = sorted(set(wanted) | set(extras))
            delta = _changed(source, deadline, current, new_tree)
            tree_ready = not delta or (allow_extra and _outside(state, delta))
            present_flags = {p: v for p, v in _flags(source, deadline, env).items() if p in flag_scope}
            refresh = []
            if state['phase'] == 'parking':
                refresh = [p for p, views in state['paths'].items()
                           if views['parked'] is not None and p not in wanted_flags]
            if tree_ready and intents == wanted and present_flags == wanted_flags and not refresh:
                shadow.unlink()
                return
            if not tree_ready and current != old_tree:
                if not allow_extra or not _outside(state, _changed(source, deadline, old_tree, current)):
                    raise ValueError('Staging area changed during overlay transition')
            _set_intents(source, directory, deadline, extras, original_intents + wanted, env)
            if not tree_ready:
                _run(source, deadline, 'read-tree', '-i', '-m', old_tree, new_tree, env=env)
            _set_intents(source, directory, deadline, wanted, extras, env)
            _change_flags(source, deadline, _flags(source, deadline, env), wanted_flags, flag_scope, env)
            if refresh:
                # A clean filter can change the on-disk size without changing the blob.
                # Native merge consults index stat data before allowing a checkout.
                data = b''.join(p.encode() + b'\0' for p in refresh)
                _run(source, deadline, 'add', '--sparse', '-u', '--pathspec-from-file=-',
                     '--pathspec-file-nul', data=data, env=env)
            final = _oid(source, deadline, 'write-tree', env=env)
            unexpected = _changed(source, deadline, new_tree, final)
            if unexpected and not (allow_extra and _outside(state, unexpected)):
                raise ValueError('Prepared index differs from the preserved staging result')
            if _intent_paths(source, deadline, env) != wanted:
                raise ValueError('The prepared index contains different intent-to-add entries')
            with shadow.open('rb+') as prepared:
                os.fsync(prepared.fileno())
            saved = {'phase': state['phase'], 'name': shadow.name, 'before': _digest(actual),
                     'after': _digest(shadow.read_bytes())}
            _json(journal, saved)
        except BaseException:
            if not journal.exists():
                shadow.unlink(missing_ok=True)
            raise
    if saved['phase'] != state['phase'] or not saved['name'].startswith('zentao-' + state['token'] + '-'):
        raise ValueError('Index recovery does not belong to this workspace operation')
    shadow = _path(index.parent, saved['name'])
    if shadow.parent != index.parent or _unsafe(index) or _unsafe(lock):
        raise ValueError('The index, lock or saved index path is invalid')
    if _digest(index.read_bytes()) == saved['after'] and not os.path.lexists(lock):
        journal.unlink()
        shadow.unlink(missing_ok=True)
        return
    if _digest(shadow.read_bytes()) != saved['after']:
        raise ValueError('The prepared index was modified outside the host operation')
    if not lock.exists():
        os.link(shadow, lock)
    if not os.path.samefile(lock, shadow):
        raise ValueError('Another Git operation owns the index lock')
    if _digest(index.read_bytes()) != saved['before']:
        lock.unlink()
        journal.unlink()
        shadow.unlink()
        raise ValueError('The live index changed before the atomic index update')
    os.replace(lock, index)
    journal.unlink()
    shadow.unlink()


def _transition(source, directory, deadline, start, pending, finish, old_label, new_label,
                old_index, new_index, expected_head, expected_work, final_work, aborting=False):
    state = _state(source, directory)
    if (_oid(source, deadline, 'rev-parse', 'HEAD^{tree}') != expected_head or
            _oid_ref(source, deadline) != state['branch']):
        raise ValueError('Target branch or tree changed during overlay processing')
    intents_before = state['intent_paths'] if old_label == 'original' else []
    intents_after = (state['intent_paths'] if new_label == 'original' else
                     state['restored_intents'] if new_label == 'restored' else [])
    allow_extra = (finish in ('original', 'restored') or aborting)
    if state['phase'] in (start, finish):
        expected_intents = intents_after if state['phase'] == finish else intents_before
        changed_intents = set(_intent_paths(source, deadline)).symmetric_difference(expected_intents)
        if changed_intents and not (allow_extra and _outside(state, changed_intents)):
            raise ValueError('Intent-to-add entries changed during overlay restoration')
    if state['phase'] == finish:
        _compare_snapshot(source, directory, deadline, state, new_index, final_work, allow_extra)
        return state
    if state['phase'] == start:
        if finish == 'parked' and _digest(json.dumps(_flags(source, deadline), sort_keys=True).encode()) != state['initial_flags_digest']:
            raise ValueError('Index flags changed before overlay parking')
        if not allow_extra and _nested_state(source, state['nested'], deadline) != state['nested']:
            raise ValueError('Local changes in a preserved submodule have changed')
        _compare_snapshot(source, directory, deadline, state, old_index, expected_work, allow_extra)
        for name, entry in state['paths'].items():
            if _disk(_path(source, name, leaf_link=True)) != entry[old_label]:
                raise ValueError('File changed after overlay preflight: ' + name)
        state['next_path'] = 0
        _save(directory, state, pending)
    elif state['phase'] != pending:
        raise ValueError('Cannot resume overlay from phase ' + state['phase'])
    if _run(source, deadline, 'cat-file', '-e', new_index + '^{tree}', allowed=(0, 1, 128)).returncode:
        _run(source, deadline, 'index-pack', '--stdin', data=_path(directory, 'objects.pack').read_bytes())
    for number, (name, entry) in enumerate(sorted(state['paths'].items())):
        if number < state['next_path']:
            if _disk(_path(source, name, leaf_link=True)) != entry[new_label]:
                raise ValueError('Completed overlay file was edited during recovery: ' + name)
            continue
        if entry[old_label] != entry[new_label]:
            _install(source, directory, name, entry[old_label], entry[new_label], state['token'])
        elif _disk(_path(source, name, leaf_link=True)) != entry[new_label]:
            raise ValueError('A retained local file changed during recovery: ' + name)
        state['next_path'] = number + 1
        _save(directory, state, pending)
    _install_index(source, directory, deadline, state, old_index, new_index, intents_before, intents_after, allow_extra)
    added = _compare_snapshot(source, directory, deadline, state, new_index, final_work, allow_extra)
    if allow_extra and (added or _intent_paths(source, deadline) != intents_after or
                        _nested_state(source, state['nested'], deadline) != state['nested']):
        state['additional_checkout_changes'] = True
    return _save(directory, state, finish)


def park_overlay(source, directory, deadline, *, aborting=False):
    s = _state(source, directory)
    return _transition(source, directory, deadline, 'prepared', 'parking', 'parked', 'original', 'parked',
                       s['index_tree'], s['before_tree'], s['before_tree'], s['work_tree'], s['parked_work_tree'],
                       aborting=aborting)


def restore_overlay(source, directory, deadline):
    s = _state(source, directory)
    if s['phase'] == 'parked' and s['sparse_materializations']:
        # Native merge may materialize an updated sparse path. Only remove the
        # exact candidate bytes it was going to write; any third version is retained.
        for name, expected in s['sparse_materializations'].items():
            actual = _disk(_path(source, name, leaf_link=True))
            if actual is not None and actual != expected:
                raise ValueError('A newly materialized sparse file received other edits: ' + name)
            s['paths'][name]['merged'] = actual
        _save(directory, s, 'parked')
    return _transition(source, directory, deadline, 'parked', 'restoring', 'restored', 'merged', 'restored',
                       s['merged_tree'], s['new_index_tree'], s['merged_tree'], s['merged_work_tree'], s['new_work_tree'])


def restore_original_overlay(source, directory, deadline):
    s = _state(source, directory)
    if s['phase'] == 'parking':
        s = park_overlay(source, directory, deadline, aborting=True)
    return _transition(source, directory, deadline, 'parked', 'rolling_back', 'original', 'parked', 'original',
                       s['before_tree'], s['index_tree'], s['before_tree'], s['parked_work_tree'], s['work_tree'])


def verify_overlay(source, directory, deadline):
    state = _state(source, directory)
    if state['phase'] != 'restored':
        raise ValueError('Overlay restoration is incomplete')
    flag_scope = set(state['flags']) | set(state['restored_flags'])
    flags = {p: v for p, v in _flags(source, deadline).items() if p in flag_scope}
    if (flags != state['restored_flags'] or _nested_state(source, state['nested'], deadline) != state['nested'] or
            _intent_paths(source, deadline) != state['restored_intents'] or
            _oid_ref(source, deadline) != state['branch'] or
            _oid(source, deadline, 'rev-parse', 'HEAD^{tree}') != state['merged_tree'] or
            _snapshot(source, directory, deadline) != (state['new_index_tree'], state['new_work_tree'])):
        raise ValueError('Restored overlay no longer matches the integrated tree and the preserved local edits')
    for name, entry in state['paths'].items():
        if _disk(_path(source, name, leaf_link=True)) != entry['restored']:
            raise ValueError('The restored file differs from its expected bytes: ' + name)
    return state
def matches_parked_overlay(source, directory, deadline):
    state = _state(source, directory)
    return (state['phase'] == 'parked'
            and _oid_ref(source, deadline) == state['branch']
            and _oid(source, deadline, 'rev-parse', 'HEAD') == state['before']
            and not _intent_paths(source, deadline)
            and {p: v for p, v in _flags(source, deadline).items() if p in state['flags']} == state['parked_flags']
            and _nested_state(source, state['nested'], deadline) == state['nested']
            and _snapshot(source, directory, deadline) == (state['before_tree'], state['parked_work_tree']))
