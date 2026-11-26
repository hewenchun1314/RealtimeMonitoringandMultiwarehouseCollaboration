"""Identify and verify local edits that a disjoint native fast-forward can keep in place."""
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import stat
import unicodedata

from development_git_overlay import _run


def _git(root, deadline, *args):
    return _run(root, deadline, '--no-optional-locks', *args).stdout


def _local_entries(root, deadline):
    entries = {}
    status = _git(root, deadline, 'status', '--porcelain=v2', '-z', '--no-renames',
                  '--untracked-files=no', '--ignore-submodules=untracked')
    for record in filter(None, status.split(b'\0')):
        if record.startswith(b'u '):
            raise ValueError('Resolve the existing unmerged index before integration')
        fields = record.split(b' ', 8)
        if len(fields) != 9 or fields[0] != b'1':
            raise ValueError('Unrecognized Git status in native integration')
        name = fields[-1]
        entries[name] = ([record], b'160000' in fields[3:6])
    # Git status can hide paths marked assume-unchanged or skip-worktree.
    listing = _git(root, deadline, 'ls-files', '-z', '-v', '--stage')
    for entry in filter(None, listing.split(b'\0')):
        if entry[:1] == b'S' or entry[:1].islower():
            metadata, name = entry.split(b'\t', 1)
            records, _ = entries.setdefault(name, ([], metadata.split()[1] == b'160000'))
            records.append(entry)
    return entries


def _field(digest, value):
    digest.update(len(value).to_bytes(8, 'big'))
    digest.update(value)


def _disk_value(root, name, submodule, deadline):
    # Inspect each path component with lstat. If a tracked directory was
    # replaced by a link, record the link itself without reading its target.
    path = root
    for part in os.fsdecode(name).split('/'):
        path = path / part
        try:
            info = path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return b'missing'
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_reparse_tag', None) == getattr(stat, 'IO_REPARSE_TAG_MOUNT_POINT', -1)):
            tag = str(getattr(info, 'st_reparse_tag', 0)).encode()
            return b'link\0' + tag + b'\0' + os.fsencode(path.relative_to(root)) + b'\0' + os.fsencode(os.readlink(path))
    mode = str(stat.S_IMODE(info.st_mode)).encode()
    if stat.S_ISREG(info.st_mode):
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0)
        with os.fdopen(os.open(path, flags), 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(info, opened):
                raise ValueError('A tracked file changed during fingerprinting')
            content = sha256()
            limit = datetime.fromisoformat(deadline)
            for data in iter(lambda: stream.read(1024 * 1024), b''):
                if datetime.now(timezone.utc) >= limit:
                    raise ValueError('The host Git deadline elapsed during fingerprinting')
                content.update(data)
            finished = os.fstat(stream.fileno())
            if (not os.path.samestat(finished, path.lstat())
                    or (opened.st_size, opened.st_mtime_ns) != (finished.st_size, finished.st_mtime_ns)):
                raise ValueError('A tracked file changed during fingerprinting')
        return b'file\0' + mode + b'\0' + content.digest()
    if stat.S_ISDIR(info.st_mode):
        marker = path / '.git'
        if submodule:
            try:
                marker_info = marker.lstat()
            except FileNotFoundError:
                return b'directory'
            if (stat.S_ISLNK(marker_info.st_mode)
                    or getattr(marker_info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
                return b'gitdir-link\0' + os.fsencode(os.readlink(marker))
            head = _git(path, deadline, 'rev-parse', '--verify', 'HEAD').strip()
            return b'gitlink\0' + head + b'\0' + fingerprint(path, deadline).encode('ascii')
        return b'directory'
    return b'special\0' + str(info.st_mode).encode() + b'\0' + str(info.st_rdev).encode()


def _fingerprint(root, entries, deadline):
    digest = sha256(b'native-integration-local-state-v1\0')
    for name, (records, submodule) in sorted(entries.items()):
        _field(digest, name)
        for record in records:
            _field(digest, record)
        _field(digest, _disk_value(root, name, submodule, deadline))
    return digest.hexdigest()


def fingerprint(source, deadline):
    source = Path(source)
    return _fingerprint(source, _local_entries(source, deadline), deadline)


def _keys(paths):
    names, ancestors = set(), set()
    for path in paths:
        parts = tuple(unicodedata.normalize('NFC', os.fsdecode(path)).casefold().split('/'))
        names.add(parts)
        ancestors.update(parts[:size] for size in range(1, len(parts) + 1))
    return names, ancestors


def native_fingerprint_if_disjoint(source, before, after, deadline):
    source = Path(source)
    entries = _local_entries(source, deadline)
    changed = _git(source, deadline, 'diff', '--name-only', '--no-renames', '--no-ext-diff',
                   '--ignore-submodules=none', '-z', before, after, '--')
    local, local_ancestors = _keys(entries)
    incoming, incoming_ancestors = _keys(filter(None, changed.split(b'\0')))
    if local & incoming_ancestors or incoming & local_ancestors:
        return None
    return _fingerprint(source, entries, deadline)
