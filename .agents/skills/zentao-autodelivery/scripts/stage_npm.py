"""Prepare a private npm dependency tree, reusing verified local snapshots."""

from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote


ROOT_INSTALL_SCRIPTS = {'preinstall', 'install', 'postinstall', 'prepublish', 'preprepare',
                        'prepare', 'postprepare', 'dependencies'}
INSTALL_OPTIONS = ('omit', 'include', 'optional', 'production', 'ignore-scripts', 'legacy-peer-deps',
                   'strict-peer-deps', 'install-links', 'install-strategy', 'bin-links', 'registry',
                   'cpu', 'os', 'libc', 'workspaces', 'include-workspace-root')


def linked(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def local_inputs(worktree, lock):
    packages = lock.get('packages')
    if not isinstance(packages, dict):
        return False, ()
    portable = True
    inputs = set()
    for package in packages.values():
        location = package.get('resolved') or ''
        is_link = package.get('link') or location.startswith(('link:', 'workspace:'))
        if not (is_link or location.startswith('file:')):
            continue
        relative = Path(unquote(location.removeprefix('file:').removeprefix('link:')))
        candidate = worktree / relative
        if relative.anchor or '..' in relative.parts or candidate.resolve() != candidate:
            return False, None
        if not is_link and candidate.suffix.lower() == '.tgz':
            inputs.add(candidate)
        else:
            portable = False
            inputs.add(candidate / 'package.json')
    return portable, sorted(inputs)


def receipt_matches(modules, fingerprint):
    if linked(modules):
        return False
    try:
        previous = json.loads((modules / '.zentao-npm-ci').read_text(encoding='ascii'))
        actual = hashlib.sha256((modules / '.package-lock.json').read_bytes()).hexdigest()
    except (FileNotFoundError, ValueError):
        return False
    return previous == {'inputs': fingerprint, 'installed_lock': actual}


@contextmanager
def snapshot_lock(path):
    import msvcrt

    with path.open('a+b') as stream:
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                time.sleep(0.2)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def copy_private_tree(source, destination):
    def check_entries(directory, names):
        for name in names:
            if linked(Path(directory) / name):
                raise ValueError('The installed dependency tree contains a filesystem link')
        return []

    if linked(source):
        raise ValueError('The dependency snapshot is a filesystem link')
    # Publish only a complete copy and never merge into an existing tree.
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.npm-copy-') as folder:
        staged = Path(folder) / 'node_modules'
        shutil.copytree(source, staged, ignore=check_entries)
        os.rename(staged, destination)


def prepare(worktree, cache_root=None):
    worktree = worktree.resolve(strict=True)
    modules = worktree / 'node_modules'
    if linked(modules):
        raise ValueError('The worktree node_modules is a link. Use a private worktree dependency directory.')
    lock = worktree / 'npm-shrinkwrap.json'
    if not lock.is_file():
        lock = worktree / 'package-lock.json'
    manifest = worktree / 'package.json'
    if not manifest.is_file() or not lock.is_file():
        raise ValueError('Frontend dependency preparation needs package.json and an npm lockfile')
    npm = ['cmd.exe', '/d', '/c', 'npm']
    node = subprocess.check_output(
        ['node', '-p', 'JSON.stringify([process.version,process.platform,process.arch])'], cwd=worktree,
    ).strip()
    npm_version = subprocess.check_output([*npm, '--version'], cwd=worktree).strip()
    npm_config = json.loads(subprocess.check_output([*npm, 'config', 'list', '--json'], cwd=worktree))
    install_options = json.dumps({key: npm_config.get(key) for key in INSTALL_OPTIONS}, sort_keys=True).encode('utf-8')
    package_data = json.loads(manifest.read_text(encoding='utf-8-sig'))
    portable_inputs, inputs = local_inputs(worktree, json.loads(lock.read_text(encoding='utf-8-sig')))
    portable = (cache_root is not None and portable_inputs and not package_data.get('workspaces')
                and not ROOT_INSTALL_SCRIPTS.intersection(package_data.get('scripts', {})))

    def fingerprint():
        digest = hashlib.sha256(b'zt-npm-snapshot-v1\0' + node + b'\0' + npm_version + b'\0' + install_options)
        for path in (manifest, lock, worktree / '.npmrc', *(inputs or ())):
            if path.exists():
                digest.update(path.relative_to(worktree).as_posix().encode('utf-8') + b'\0'
                              + hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()

    expected = fingerprint()
    if inputs is not None and receipt_matches(modules, expected):
        print('Reused dependencies prepared for this worktree')
        return

    def install():
        receipt = modules / '.zentao-npm-ci'
        receipt.unlink(missing_ok=True)
        subprocess.run([*npm, 'ci', '--prefer-offline'], cwd=worktree, check=True)
        if fingerprint() != expected or linked(modules):
            raise ValueError('Dependency inputs or destination changed during npm ci')
        installed_lock = modules / '.package-lock.json'
        if installed_lock.is_file() and inputs is not None:
            receipt.write_text(json.dumps({
                'inputs': expected, 'installed_lock': hashlib.sha256(installed_lock.read_bytes()).hexdigest(),
            }) + '\n', encoding='ascii')
        print('Prepared worktree dependencies using npm ci')

    if not portable:
        install()
        return
    cache_root = cache_root.absolute()
    if cache_root.resolve() != cache_root:
        raise ValueError('The npm snapshot cache must use a physical directory')
    cache_root.mkdir(parents=True, exist_ok=True)
    snapshot = cache_root / expected
    with snapshot_lock(cache_root / f'{expected}.lock'):
        if fingerprint() != expected:
            raise ValueError('Dependency inputs changed while waiting for the npm snapshot')
        if not modules.exists() and receipt_matches(snapshot, expected):
            try:
                copy_private_tree(snapshot, modules)
            except (OSError, ValueError) as error:
                print(f'Could not restore npm snapshot: {error}', file=sys.stderr)
            else:
                print('Restored private worktree dependencies from the npm snapshot cache')
                return
        install()
        if not snapshot.exists() and not linked(snapshot) and receipt_matches(modules, expected):
            try:
                copy_private_tree(modules, snapshot)
            except (OSError, ValueError) as error:
                print(f'Could not save npm snapshot: {error}', file=sys.stderr)


if __name__ == '__main__':
    try:
        prepare(Path.cwd(), Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'Frontend dependency preparation failed: {error}', file=sys.stderr)
        sys.exit(1)
