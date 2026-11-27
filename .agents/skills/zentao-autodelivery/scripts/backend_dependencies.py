"""Resolve contract references and protect upstream repositories in work-item workers."""

from os.path import expandvars
from pathlib import Path
import subprocess

from material_sync import requirement_checkout


def writable_dependencies(config):
    return {key: entry for key, entry in config.get('backend_dependencies', {}).items()
            if entry.get('access') == 'writable'}


def backend_services(config):
    services = config['development'].get('backend_bundle', {}).get('services', {})
    return {**services, **{key: {**entry['delivery'], 'repository': '.'}
                           for key, entry in writable_dependencies(config).items()}}


def dependency_locations(config, config_directory):
    dependencies = config.get('backend_dependencies', {})
    if not dependencies:
        return {}
    config_directory = config_directory.resolve()
    workspace = config.get('workspaces', {}).get('backend')
    services = config['development']['backend_bundle']['services']
    writable_repositories = [(config_directory / workspace / service['repository']).resolve()
                             for service in services.values()] if workspace else []
    references = {}
    for key, dependency in dependencies.items():
        root = (config_directory / dependency['source_path']).resolve()
        api_source = (root / dependency['api_source']).resolve()
        if config_directory.is_relative_to(root) or any(
                repo.is_relative_to(root) or root.is_relative_to(repo) for repo in writable_repositories):
            raise ValueError(f'backend_dependencies.{key}.source_path overlaps a writable repository')
        if not api_source.is_relative_to(root):
            raise ValueError(f'backend_dependencies.{key}.api_source escapes source_path')
        if any(char in root.as_posix() for char in '*?[]'):
            raise ValueError(f'backend_dependencies.{key}.source_path must resolve to a literal directory')
        access = dependency.get('access', 'read_only')
        for other in references.values():
            other_root = Path(other['source_path'])
            if ((access == 'writable' or other['access'] == 'writable')
                    and (root.is_relative_to(other_root) or other_root.is_relative_to(root))):
                raise ValueError(f'backend_dependencies.{key}.source_path overlaps another dependency')
        references[key] = {'source_path': str(root), 'api_source': str(api_source),
                           'used_by': dependency['used_by'], 'access': access}
    return references


def dependency_context(config, config_directory, services):
    references = dependency_locations(config, config_directory)
    if services is None:
        return references
    return {key: {**reference, 'used_by': [service for service in reference['used_by'] if service in services]}
            for key, reference in references.items()
            if key in services or services.intersection(reference['used_by'])}


def git_checkout(config, config_directory, key):
    side, _, name = key.partition(':')
    workspace = config.get('workspaces', {}).get(side)
    bundle = config.get('development', {}).get(f'{side}_bundle', {})
    targets = bundle.get('services' if side == 'backend' else 'applications', {})
    repository = targets.get(name, {}).get('repository')
    if not workspace or not repository or side not in ('backend', 'frontend'):
        return None
    workspace = (config_directory / workspace).resolve()
    checkout = (workspace / repository).resolve()
    if checkout.is_relative_to(workspace):
        return workspace, checkout
    return None


def git_metadata_roots(configs, config_directory):
    keys = configs[0].get('runtime', {}).get('worker_git_write_repositories', ())
    for key in keys:
        locations = {git_checkout(config, config_directory, key) for config in configs}
        if len(locations) != 1 or None in locations:
            continue
        workspace, checkout = locations.pop()
        if not checkout.is_dir():
            continue
        # Only recognize the registered checkout's own Git directory or its
        # canonical submodule directory. A gitdir pointer cannot grant new paths.
        expected = {checkout / '.git'}
        if checkout != workspace:
            expected.add(workspace / '.git' / 'modules' / checkout.relative_to(workspace))
        try:
            result = subprocess.run(
                ['git', '-C', str(checkout), 'rev-parse', '--path-format=absolute',
                 '--show-toplevel', '--git-common-dir'],
                check=True, capture_output=True, encoding='utf-8', timeout=5)
            root, common = map(Path, result.stdout.splitlines())
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        if root.resolve() == checkout and common in expected and common.resolve() == common:
            yield common


def worker_roots(configs, config_directory, project_root, codex_directory):
    roots = set()
    dependency_writes = []
    writable = {project_root.resolve(), codex_directory.resolve()}
    for config in configs:
        references = dependency_locations(config, config_directory)
        roots.update(Path(entry['source_path']) for entry in references.values())
        dependency_writes.append({(key, Path(entry['source_path'])) for key, entry in references.items()
                                  if entry['access'] == 'writable'})
        for side in ('frontend', 'backend'):
            path = config.get('workspaces', {}).get(side)
            if path:
                writable.add((config_directory / path).resolve())
        for path in config.get('runtime', {}).get('worker_cache_paths', []):
            writable.add((config_directory / Path(expandvars(path)).expanduser()).resolve())
    git_roots = set(git_metadata_roots(configs, config_directory))
    requirements = {requirement_checkout(config, config_directory) for config in configs
                    if config.get('runtime', {}).get('sync_requirement_before_review')} - {None}
    granted = {root for _, root in set.intersection(*dependency_writes)}
    protected = (roots - granted) | requirements
    writable.update(root for root in granted | git_roots if not any(
        root.is_relative_to(path) or path.is_relative_to(root) for path in protected))
    return sorted(protected), sorted(writable)
