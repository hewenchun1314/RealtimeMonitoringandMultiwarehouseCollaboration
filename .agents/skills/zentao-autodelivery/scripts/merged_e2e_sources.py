"""Pin the frontend sources used by a formal post-merge browser run."""

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import tempfile

from branch_check import git


class RuntimeSourcesChanged(ValueError):
    """Working files changed while the recorded Git revisions were still current."""


def check_merged_checkout(repository, branch, sha):
    root = Path(repository)
    ref = 'refs/heads/' + branch if branch else 'HEAD'
    if (Path(git(root, 'rev-parse', '--show-toplevel')).resolve() != root
            or git(root, 'rev-parse', '--verify', ref) != sha):
        raise ValueError('The recorded frontend repository or merged version has changed')


def scenario_services(options, routes, versions):
    if not options.get('automaticAttestation') or options.get('entryPath') not in routes:
        return {}
    scenario = json.loads(Path(options['scenario']).read_text(encoding='utf-8-sig'))
    pages = {options['entryPath']}
    for case in scenario.get('cases', [scenario]):
        pages.update(step['path'] for step in case['steps'] if step['action'] == 'navigate')
    if not pages.issubset(routes):
        return {}
    services = {}
    for page in sorted(pages):
        services.update(routes[page])
    return services if versions.keys() <= services.keys() else {}


def merged_workspace(options, config, branches, versions):
    root = Path(options['workspace']).resolve(strict=True)
    if not versions or branches.keys() != versions.keys():
        raise ValueError('Acceptance requires the recorded merged versions')
    applications = config['development']['frontend_bundle']['applications']
    routes = config.get('e2e', {}).get('startup_routes', {})
    services = scenario_services(options, routes, versions)
    options.pop('startupServices', None)
    if services:
        options['startupServices'] = [
            (applications[app]['repository'], script, port) for app, (script, port) in services.items()]
    planned = {}
    for app, sha in versions.items():
        application, proof = applications[app], branches[app]
        checkout = (root / application['repository']).resolve(strict=True)
        if (not checkout.is_relative_to(root) or checkout != Path(proof['source_root']).resolve(strict=True)
                or application['branch'] != proof['target_branch']):
            raise ValueError('Merged checkout must match the configured application and target branch')
        planned[checkout] = (application['branch'], sha)

    # The default startup command also serves the installed sibling applications.
    roots = {root, *planned}
    for app in services or applications:
        application = applications[app]
        checkout = (root / application['repository']).resolve()
        if not checkout.is_relative_to(root):
            raise ValueError('Frontend application is outside the configured workspace')
        if services or (checkout / '.git').exists():
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


def short_checkout_paths(stage_folder):
    # A recovery process can derive the same paths without trusting an absolute
    # deletion target from a receipt. Different host attempts never share them.
    identity = os.fsencode(os.path.normcase(str(stage_folder.resolve(strict=True))))
    suffix = sha256(identity).hexdigest()[:24]
    root = Path(tempfile.gettempdir()).resolve(strict=True)
    return root / ('zt-src-' + suffix), root / ('zt-git-' + suffix)


def prepare_committed_sources(options, stage_folder):
    checkouts = options.get('mergedCheckouts', {})
    main = Path(options['workspace']).resolve(strict=True)
    # Legacy profiles include absolute checkout paths in Vite response hashes.
    short = options.get('automaticAttestation')
    destination, git_storage = (short_checkout_paths(stage_folder) if short else
                                (stage_folder / 'frontend-source', stage_folder / 'frontend-git'))
    ownership = stage_folder / ('frontend-short.owner' if short else 'frontend-source.owner')
    if os.path.lexists(ownership) or os.path.lexists(destination) or destination.resolve() != destination:
        raise ValueError('Formal frontend sources require a new attempt-owned directory')
    destination.mkdir(mode=0o700)
    with ownership.open('x', encoding='ascii') as receipt:
        receipt.write(str(destination.stat().st_ino))
    # Keep Git's object/alternates paths short even for long application names.
    # This sibling belongs to the same host attempt and has independent proof.
    git_ownership = stage_folder / 'frontend-git.owner'
    if (git_storage.resolve() != git_storage or os.path.lexists(git_storage)
            or os.path.lexists(git_ownership)):
        raise ValueError('Formal frontend Git storage requires a new attempt-owned directory')
    git_storage.mkdir(mode=0o700)
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
    ownership = stage_folder / 'frontend-short.owner'
    if os.path.lexists(ownership):
        destination, git_storage = short_checkout_paths(stage_folder)
    else:
        destination = stage_folder / 'frontend-source'
        git_storage = stage_folder / 'frontend-git'
        ownership = stage_folder / 'frontend-source.owner'
    if not ownership.is_file():
        return None
    try:
        if (destination.resolve() != destination or ownership.resolve() != ownership
                or ownership.read_text(encoding='ascii') != str(destination.stat().st_ino)):
            return 'ownership-changed'
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
