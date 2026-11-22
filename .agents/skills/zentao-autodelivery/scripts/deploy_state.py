"""Project deployment settings and the ledger contract for release receipts."""

import re
from urllib.parse import urlsplit


DEPLOY_STAGE = 'backend.deploy'
FRONTEND_DEPLOY_STAGE = 'frontend.deploy'
DEPLOY_STAGES = (DEPLOY_STAGE, FRONTEND_DEPLOY_STAGE)
FRONTEND_PROFILE = 'zentao-ai-development'
TARGET_FIELDS = {
    'registry': r'[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?',
    'image_repository': r'[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)+',
    'platform': r'linux/(?:amd64|arm64)',
    'cluster_id': r'[a-zA-Z0-9][a-zA-Z0-9_.:-]*',
    'kube_context': r'[A-Za-z0-9][A-Za-z0-9_.:@/-]*',
    'namespace': r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?',
    'deployment': r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',
    'container': r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?',
}
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
COMMIT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')
TAG = re.compile(r'[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?/[a-z0-9][a-z0-9._/-]*:[a-zA-Z0-9][a-zA-Z0-9_.-]*')


def frontend_release_config(config):
    return config['development'].get('frontend_bundle', {}).get('deployment', {})


def validate_mcp_target(target, field):
    if not isinstance(target, dict):
        raise ValueError(f'{field} must be an object')
    for key in ('cluster_id', 'namespace', 'deployment', 'container'):
        value = target.get(key)
        if not isinstance(value, str) or not re.fullmatch(TARGET_FIELDS[key], value):
            raise ValueError(f'{field}.{key} is invalid')
    address = target.get('mcp_url')
    url = urlsplit(address) if isinstance(address, str) else None
    if (not url or url.scheme not in ('http', 'https') or not url.hostname
            or url.username is not None or url.path != '/mcp' or url.query or url.fragment):
        raise ValueError(f'{field}.mcp_url must be an HTTP(S) /mcp endpoint')
    env = target.get('mcp_bearer_token_env')
    if env is not None and (url.scheme != 'https' or not isinstance(env, str)
                            or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', env)):
        raise ValueError(f'{field}.mcp_bearer_token_env requires HTTPS and a valid variable name')


def mcp_rollout_matches(proof, image):
    if not isinstance(proof, dict) or proof.get('image') != image:
        return False
    ready, pods, previous = (proof.get(key) for key in ('ready_replicas', 'pods', 'previous_pods'))
    return (type(ready) is int and ready > 0 and isinstance(pods, list) and len(pods) == ready
            and isinstance(previous, list) and all(isinstance(pod, str) and pod for pod in (*pods, *previous))
            and len(set(pods)) == ready and set(pods).isdisjoint(previous))


def release_verification(receipt):
    target = receipt['target']
    if target.get('mcp_url'):
        return {'sha': receipt['sha'], 'url': target['health_url'], 'provider': 'rancher_mcp',
                'image': receipt['image'].rsplit(':', 1)[0] + '@' + receipt['digest']}
    return {'sha': receipt['sha'], 'url': target['gateway_url'].rstrip('/') + target['verification_path']}


def validate_frontend_release(config):
    release = frontend_release_config(config)
    if not isinstance(release, dict) or type(release.get('enabled', False)) is not bool:
        raise ValueError('frontend_bundle.deployment.enabled must be a boolean')
    if not release.get('enabled'):
        return
    if release.get('image_mode', 'build') not in ('build', 'reuse'):
        raise ValueError('frontend_bundle.deployment.image_mode must be build or reuse')
    repository = release.get('image_repository')
    if not isinstance(repository, str) or not TAG.fullmatch(repository + ':check'):
        raise ValueError('frontend_bundle.deployment.image_repository is invalid')
    base = release.get('base_image')
    if (not isinstance(base, str) or not base.startswith(repository + '@')
            or not DIGEST.fullmatch(base.partition('@')[2])):
        raise ValueError('frontend_bundle.deployment.base_image must pin the repository digest')
    if release.get('platform') not in ('linux/amd64', 'linux/arm64'):
        raise ValueError('frontend_bundle.deployment.platform is invalid')
    ports = release.get('local_ports')
    if (not isinstance(ports, list) or not ports
            or any(type(port) is not int or not 1 <= port <= 65535 for port in ports)
            or len(set(ports)) != len(ports)):
        raise ValueError('frontend_bundle.deployment.local_ports must contain unique TCP ports')
    if 'rancher' in release:
        validate_mcp_target(release['rancher'], 'frontend_bundle.deployment.rancher')


def project_services(config):
    return config['development'].get('backend_bundle', {}).get('services', {})


def configured_targets(config):
    return {name: release['targets'][release['target']]
            for name, service in project_services(config).items()
            if (release := service.get('deployment')) and release['enabled']}


def validate_configured_targets(config):
    for service, settings in project_services(config).items():
        if 'deployment' not in settings:
            continue
        release = settings['deployment']
        field = f'{service}.deployment'
        if not isinstance(release, dict) or type(release.get('enabled')) is not bool:
            raise ValueError(f'{field}.enabled must be a boolean')
        targets = release.get('targets', {})
        if not isinstance(targets, dict):
            raise ValueError(f'{field}.targets must be an object')
        if not release['enabled']:
            continue
        dependencies = release.get('build_dependencies', {})
        if not isinstance(dependencies, dict) or any(
                not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', key) or not isinstance(sha, str)
                or not COMMIT.fullmatch(sha) for key, sha in dependencies.items()):
            raise ValueError(f'{field}.build_dependencies must pin module names to Git SHAs')
        if service == 'smart-geo' and 'tet-common-java' not in dependencies:
            raise ValueError(f'{field}.build_dependencies must pin tet-common-java')
        selected = release.get('target')
        if not isinstance(selected, str) or selected not in targets:
            raise ValueError(f'{field}.target must name a configured deployment target')
        for name, target in targets.items():
            prefix = f'{field}.targets.{name}'
            if not isinstance(target, dict):
                raise ValueError(f'{prefix} must be an object')
            use_mcp = 'mcp_url' in target
            for key, pattern in TARGET_FIELDS.items():
                if use_mcp and key == 'kube_context':
                    continue
                value = target.get(key)
                if not isinstance(value, str) or not re.fullmatch(pattern, value):
                    raise ValueError(f'{prefix}.{key} is invalid')
            gateway = target.get('gateway_url')
            if not isinstance(gateway, str):
                raise ValueError(f'{prefix}.gateway_url must be an HTTP origin')
            url = urlsplit(gateway)
            if (url.scheme not in ('http', 'https') or not url.hostname or url.username is not None
                    or url.path not in ('', '/') or url.query or url.fragment):
                raise ValueError(f'{prefix}.gateway_url must be an HTTP origin')
            if use_mcp:
                validate_mcp_target(target, prefix)
                health = target.get('health_url')
                probe = urlsplit(health) if isinstance(health, str) else None
                if (not probe or probe.scheme not in ('http', 'https') or not probe.hostname
                        or probe.username is not None or not probe.path or probe.path == '/'
                        or probe.query or probe.fragment):
                    raise ValueError(f'{prefix}.health_url must be an HTTP(S) health endpoint')
                continue
            path = target.get('verification_path')
            if not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9._~/-]+', path) or '//' in path:
                raise ValueError(f'{prefix}.verification_path must be a local absolute URL path')
            for key in ('version_field', 'http_credential_ref', 'http_username'):
                if key in target and (not isinstance(target[key], str) or not target[key].strip()):
                    raise ValueError(f'{prefix}.{key} must be a non-empty string')
            if target.get('http_credential_ref') and (url.scheme != 'https' or not target.get('http_username')):
                raise ValueError(f'{prefix} requires HTTPS and http_username to use HTTP credentials')


def target_key(target):
    return '/'.join(target[key] for key in ('cluster_id', 'namespace', 'deployment'))


def deployed_receipt_matches(receipt, service, sha, target):
    if not isinstance(receipt, dict) or not isinstance(sha, str) or not COMMIT.fullmatch(sha):
        return False
    digest = receipt.get('digest')
    image = receipt.get('image')
    probe = receipt.get('verification')
    common = (receipt.get('status') == 'deployed' and receipt.get('service') == service
            and receipt.get('sha') == sha and receipt.get('target') == target
            and isinstance(digest, str) and DIGEST.fullmatch(digest) is not None
            and isinstance(image, str) and TAG.fullmatch(image) is not None
            and image.rsplit(':', 1)[0] == f'{target["registry"]}/{target["image_repository"]}'
            and isinstance(probe, dict) and probe.get('sha') == sha)
    if not common or not target.get('mcp_url'):
        return common
    expected = release_verification(receipt)
    return probe == expected and mcp_rollout_matches(receipt.get('pod_verification'), expected['image'])


def release_locks(store, item, *, tags_only=False):
    where = " AND scope='tag'" if tags_only else ''
    store.conn.execute('DELETE FROM deployment_locks WHERE item_seq=?' + where, (item['seq'],))
