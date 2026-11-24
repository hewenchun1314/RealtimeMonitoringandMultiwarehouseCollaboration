"""Fast-forward the requirement checkout before a Story review."""

import os
from pathlib import Path
import subprocess
from time import monotonic


def requirement_checkout(config, config_directory):
    path = config.get('workspaces', {}).get('requirement')
    if not path:
        return None
    root = (config_directory / path).resolve()
    if not config_directory.resolve().is_relative_to(root):
        return root
    return None


def readonly_git_directory(checkout):
    """Resolve a normal .git directory or Git's one-line linked-worktree pointer."""
    metadata = checkout / '.git'
    if metadata.is_file():
        lines = metadata.read_text(encoding='utf-8').splitlines()
        if len(lines) != 1 or not lines[0].startswith('gitdir: '):
            raise ValueError('The requirement Git metadata pointer is invalid')
        metadata = (checkout / lines[0][8:]).resolve()
    if not metadata.is_dir():
        raise ValueError('The requirement Git metadata is not readable')
    return metadata


def git_until(checkout, deadline, *, read_only=False):
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never', 'GCM_GUI_PROMPT': 'false', 'LC_ALL': 'C'}

    prefix = (['git', '--git-dir=' + str(readonly_git_directory(checkout)), '--work-tree=' + str(checkout)]
              if read_only else ['git', '-C', str(checkout)])

    def git(operation, *args):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ValueError('Requirement repository sync timed out')
        try:
            result = subprocess.run(
                [*prefix, '-c', 'credential.interactive=never', '--literal-pathspecs', *args],
                env=env, stdin=subprocess.DEVNULL, capture_output=True, encoding='utf-8',
                errors='replace', timeout=remaining, check=True)
        except subprocess.TimeoutExpired:
            raise ValueError('Requirement repository sync timed out') from None
        except subprocess.CalledProcessError as error:
            details = error.stderr.lower()
            if 'would be overwritten' in details:
                raise ValueError('Local requirement edits overlap the incoming changes') from None
            if 'not possible to fast-forward' in details:
                raise ValueError('The requirement branch has diverged from its upstream') from None
            # Git diagnostics can include the configured remote URL and credentials.
            raise ValueError(f'Cannot {operation} for the requirement repository') from None
        return result.stdout.strip()

    return git


def sync_requirement(checkout, budget):
    git = git_until(checkout, monotonic() + budget)
    root = git('resolve the checkout', 'rev-parse', '--show-toplevel')
    if Path(root).resolve() != checkout:
        raise ValueError('The configured requirement path is not a Git checkout root')
    branch = git('resolve the current branch', 'symbolic-ref', '--short', 'HEAD')
    remote = git('resolve the upstream remote', 'config', '--get', f'branch.{branch}.remote')
    if not remote or remote == '.':
        raise ValueError('The requirement branch needs a remote upstream')
    upstream = git('resolve the upstream branch', 'rev-parse', '--symbolic-full-name', '@{upstream}')
    remote_ref = git('resolve the upstream ref', 'config', '--get-all', f'branch.{branch}.merge')
    if not remote_ref.startswith('refs/heads/') or len(remote_ref.splitlines()) != 1:
        raise ValueError('The requirement branch must track one remote branch')
    git('fetch the upstream branch', 'fetch', '--no-tags', '--no-recurse-submodules', '--', remote, remote_ref)
    fetched, tracking = git('verify the fetched branch', 'rev-parse', 'FETCH_HEAD', upstream).splitlines()
    if fetched != tracking or git('verify the current branch', 'symbolic-ref', '--short', 'HEAD') != branch:
        raise ValueError('The requirement checkout or upstream changed during sync')
    git('fast-forward the requirement branch', '-c', 'submodule.recurse=false',
        'merge', '--ff-only', '--no-autostash', '--no-overwrite-ignore', fetched)
    if git('verify the synced HEAD', 'rev-parse', 'HEAD') != fetched:
        raise ValueError('The requirement branch HEAD differs from the fetched upstream commit')
    return {'branch': branch, 'upstream': upstream, 'commit': fetched}


def verify_requirement_intent(checkout, commit, path, *, missing=False):
    # MXC permits reading protected material but denies chdir into a read-only
    # checkout. Explicit metadata/worktree paths preserve read-only verification.
    git = git_until(checkout, monotonic() + 5, read_only=True)
    if git('verify the reviewed HEAD', 'rev-parse', 'HEAD') != commit:
        raise ValueError('The requirement repository changed since sync')
    if not path:
        return
    if missing:
        if git('check the committed intent', 'ls-tree', '--full-tree', commit, '--', path):
            raise ValueError('The intent exists in the synchronized commit')
        return
    expected = git('find the intent in the synchronized commit', 'rev-parse', '--verify', f'{commit}:{path}')
    actual = git('verify the reviewed intent', 'hash-object', f'--path={path}', '--', str(checkout / path))
    if actual != expected:
        raise ValueError('The local intent differs from the synchronized commit')
