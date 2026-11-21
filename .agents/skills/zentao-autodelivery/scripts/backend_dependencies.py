"""Resolve contract references and protect upstream repositories in work-item workers."""

from os.path import expandvars
from pathlib import Path


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
    if not roots:
        return [], []
    granted = {root for _, root in set.intersection(*dependency_writes)}
    protected = roots - granted
    writable.update(root for root in granted if not any(root.is_relative_to(path) for path in protected))
    return sorted(protected), sorted(writable)
