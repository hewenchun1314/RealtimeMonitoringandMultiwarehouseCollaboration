"""Pin the frontend sources used by a formal post-merge browser run."""

import os
from pathlib import Path
import shutil

from branch_check import git


class RuntimeSourcesChanged(ValueError):
    """Working files changed while the recorded Git revisions were still current."""


def check_merged_checkout(repository, branch, sha):
    root = Path(repository)
    ref = 'refs/heads/' + branch if branch else 'HEAD'
    if (Path(git(root, 'rev-parse', '--show-toplevel')).resolve() != root
            or git(root, 'rev-parse', '--verify', ref) != sha):
        raise ValueError('The recorded frontend repository or merged version has changed')


def merged_workspace(options, config, branches, versions):
    root = Path(options['workspace']).resolve(strict=True)
    if not versions or branches.keys() != versions.keys():
        raise ValueError('Acceptance requires the recorded merged versions')
    applications = config['development']['frontend_bundle']['applications']
    planned = {}
    for app, sha in versions.items():
        application, proof = applications[app], branches[app]
        checkout = (root / application['repository']).resolve(strict=True)
        if (not checkout.is_relative_to(root) or checkout != Path(proof['source_root']).resolve(strict=True)
                or application['branch'] != proof['target_branch']):
            raise ValueError('Merged checkout must match the configured application and target branch')
        planned[checkout] = (application['branch'], sha)

    # The shell's npm script starts sibling applications even for a one-app task.
    # Record every installed configured repository, including the host shell.
    roots = {root, *planned}
    for application in applications.values():
        checkout = (root / application['repository']).resolve()
        if not checkout.is_relative_to(root):
            raise ValueError('Frontend application is outside the configured workspace')
        if (checkout / '.git').exists():
            roots.add(checkout)
    result = {}
    for checkout in sorted(roots, key=lambda path: (len(path.parts), path)):
        if checkout in planned:
            branch, sha = planned[checkout]
        else:
            branch = git(checkout, 'symbolic-ref', '--quiet', '--short', 'HEAD', allowed=(0, 1))
            sha = git(checkout, 'rev-parse', 'HEAD')
        entry = {'repository': str(checkout), 'branch': branch, 'sha': sha}
        check_merged_checkout(**entry)
        result[str(checkout)] = entry
    return result


def checkout_matches_runtime(root, sha, *, include_untracked):
    if git(root, 'rev-parse', 'HEAD') != sha:
        return False
    # status deliberately skips checking these paths. Their actual bytes may
    # already differ or be changed by an editor while the browser is running.
    listing = git(root, 'ls-files', '-v', '-z', timeout=60)
    if any(entry.startswith('S ') or entry[:1].islower() for entry in listing.split('\0')):
        return False
    untracked = '--untracked-files=normal' if include_untracked else '--untracked-files=no'
    return not git(root, '--no-optional-locks', 'status', '--porcelain=v1', untracked,
                   '--ignore-submodules=untracked', timeout=60)


def prepare_committed_sources(options, destination):
    checkouts = options.get('mergedCheckouts', {})
    if not options.get('forceCommittedSources') and all(
            checkout_matches_runtime(row['repository'], row['sha'], include_untracked=True) for row in checkouts.values()):
        return None
    main = Path(options['workspace']).resolve(strict=True)
    if destination.exists() or destination.resolve() != destination:
        raise ValueError('Formal frontend sources require a new attempt-owned directory')
    destination.mkdir()
    ownership = destination.parent / 'frontend-source.owner'
    with ownership.open('x', encoding='ascii') as receipt:
        receipt.write(str(destination.stat().st_ino))
    # Keep Git's object/alternates paths short even for long application names.
    # This sibling belongs to the same host attempt and has independent proof.
    git_storage = destination.parent / 'frontend-git'
    git_ownership = destination.parent / 'frontend-git.owner'
    if (git_storage.resolve() != git_storage or os.path.lexists(git_storage)
            or os.path.lexists(git_ownership)):
        raise ValueError('Formal frontend Git storage requires a new attempt-owned directory')
    git_storage.mkdir()
    with git_ownership.open('x', encoding='ascii') as receipt:
        receipt.write(str(git_storage.stat().st_ino))
    for index, row in enumerate(checkouts.values()):
        source = Path(row['repository'])
        target = destination / source.relative_to(main)
        if target.resolve() != target:
            raise ValueError('A nested frontend repository cannot replace a linked path')
        target.parent.mkdir(parents=True, exist_ok=True)
        # Shared object storage avoids copying every repository's history. Each
        # checkout has its own index and files, including package definitions.
        # Apply long paths before clone copies hook templates during init; the
        # new repository's --config alone takes effect too late on Windows.
        git(target.parent, '-c', 'core.longpaths=true', 'clone', '--shared',
            '--no-checkout', '--no-tags', '--config', 'core.longpaths=true',
            '--separate-git-dir', str(git_storage / str(index)),
            '--', str(source), str(target), timeout=180)
        git(target, 'checkout', '--detach', row['sha'], timeout=180)
        modules = source / 'node_modules'
        link = target / 'node_modules'
        if modules.is_dir() and not os.path.lexists(link):
            if os.name == 'nt':
                import _winapi
                _winapi.CreateJunction(str(modules.resolve()), str(link))
            else:
                link.symlink_to(modules.resolve(), target_is_directory=True)
    verify_runtime_sources(options, destination)
    return str(destination)


def remove_committed_sources(stage_folder):
    """Called by the host after all processes in the stage job have stopped."""
    destination = stage_folder / 'frontend-source'
    ownership = stage_folder / 'frontend-source.owner'
    if not ownership.is_file():
        return None
    try:
        if (destination.resolve() != destination or ownership.resolve() != ownership
                or ownership.read_text(encoding='ascii') != str(destination.stat().st_ino)):
            return 'ownership-changed'
        git_storage = stage_folder / 'frontend-git'
        git_ownership = stage_folder / 'frontend-git.owner'
        if os.path.lexists(git_storage) or os.path.lexists(git_ownership):
            if git_ownership.resolve() != git_ownership or not git_ownership.is_file():
                return 'ownership-changed'
            expected_inode = git_ownership.read_text(encoding='ascii')
            if not expected_inode.isdecimal():
                return 'ownership-changed'
            if os.path.lexists(git_storage):
                if (git_storage.resolve() != git_storage or not git_storage.is_dir()
                        or expected_inode != str(git_storage.stat().st_ino)):
                    return 'ownership-changed'
                shutil.rmtree(git_storage)
            # A previous cleanup may have removed storage before unlinking its
            # marker. No unverified directory is removed in this recovery state.
            # Remove metadata first so source cleanup can be retried independently.
            git_ownership.unlink()
        # Python 3.8+ unlinks Windows junctions without traversing their target.
        shutil.rmtree(destination)
        ownership.unlink()
        return 'removed'
    except (OSError, ValueError):
        return 'retained'


def verify_runtime_sources(options, source_workspace):
    main = Path(options['workspace'])
    for row in options['mergedCheckouts'].values():
        check_merged_checkout(**row)
    for row in options['mergedCheckouts'].values():
        source = Path(row['repository'])
        runtime = Path(source_workspace) / source.relative_to(main) if source_workspace else source
        if not checkout_matches_runtime(runtime, row['sha'], include_untracked=not source_workspace):
            raise RuntimeSourcesChanged('Frontend runtime sources changed during acceptance')
