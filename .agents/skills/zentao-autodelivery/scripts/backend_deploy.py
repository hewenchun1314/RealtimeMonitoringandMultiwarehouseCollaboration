"""Publish and verify one approved service checkout. Run under the deployment worker."""

import argparse
from base64 import b64encode
from http.client import HTTPConnection, HTTPException, HTTPSConnection
import json
import os
from pathlib import Path
import re
from shutil import copy2, which
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


from runtime_paths import LOCAL_DIRECTORY
from service_runtime import command_args


COMMIT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
REVISION = 'org.opencontainers.image.revision'
MAVEN_NAMESPACE = 'http://maven.apache.org/POM/4.0.0'


class DeploymentError(ValueError):
    """A publication failure whose message is safe to include in a receipt."""


def command(*args, cwd=None, allow_failure=False, timeout=1800):
    executable = which(args[0]) or args[0]
    action = args[5] if args[0] == 'kubectl' and len(args) > 5 and args[1] == '--context' else args[1]
    try:
        result = subprocess.run([executable, *args[1:]], cwd=cwd, stdin=subprocess.DEVNULL,
                                capture_output=True, encoding='utf-8', errors='replace', timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeploymentError(f'{args[0]} {action} unavailable or timed out') from error
    if result.returncode and not allow_failure:
        raise DeploymentError(f'{args[0]} {action} failed (exit {result.returncode})')
    return result


def required_digest(value):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise DeploymentError('Invalid or missing SHA-256 digest')
    return value


def image_tag(request):
    """Calculate the exact image tag used by the worker's publication lock."""
    sha = request['sha']
    if not isinstance(sha, str) or not COMMIT.fullmatch(sha):
        raise DeploymentError('An exact merged commit SHA is required')
    target = request['target']
    registry = target['registry']
    repository = target['image_repository']
    if (not re.fullmatch(r'[a-z0-9.-]+(?::[0-9]+)?', registry)
            or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]*(?:/[a-z0-9][a-z0-9_.-]*)*', repository)):
        raise DeploymentError('Invalid configured registry or image repository')
    service = repository.rsplit('/', 1)[-1]
    versions = Path(request['backend_root']) / 'publish-version.txt'
    entries = {}
    for line in versions.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            name, separator, value = line.partition('=')
            if not separator or name in entries or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', value):
                raise DeploymentError('Invalid publish-version.txt')
            entries[name] = value
    if service not in entries:
        raise DeploymentError('Image repository is missing from publish-version.txt')
    count = int(command('git', '-C', request['repository'], 'rev-list', '--count', sha).stdout.strip())
    if count < 1:
        raise DeploymentError('Merged commit history is empty')
    return f'{registry}/{repository}:{entries[service]}.{count // 100}.{count % 100}'


def checkout(repository, sha, destination):
    if not COMMIT.fullmatch(sha):
        raise DeploymentError('Build checkout requires a pinned commit')
    command('git', 'clone', '--local', '--no-hardlinks', '--no-checkout', '--', str(repository), str(destination))
    command('git', '-C', str(destination), 'checkout', '--detach', sha)
    observed = command('git', '-C', str(destination), 'rev-parse', 'HEAD').stdout.strip()
    if observed != sha:
        raise DeploymentError('Build checkout does not match the pinned commit')


def build_workspace(request, temporary):
    source = Path(request['repository'])
    dependencies = request.get('build_dependencies', {})
    worktree = temporary / source.name
    checkout(source, request['sha'], worktree)
    if not dependencies:
        if request['service'] == 'smart-geo':
            raise DeploymentError('Smart-Geo requires the pinned tet-common-java reactor dependency')
        return worktree, worktree
    if request['service'] == 'smart-geo' and 'tet-common-java' not in dependencies:
        raise DeploymentError('Smart-Geo requires the pinned tet-common-java reactor dependency')
    backend_root = Path(request['backend_root'])
    for module, sha in sorted(dependencies.items()):
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+', module) or module in ('.', '..', source.name):
            raise DeploymentError('Invalid pinned reactor module')
        checkout(backend_root / module, sha, temporary / module)
    ET.register_namespace('', MAVEN_NAMESPACE)
    pom = ET.parse(backend_root / 'pom.xml')
    modules = pom.getroot().find(f'{{{MAVEN_NAMESPACE}}}modules')
    names = {source.name, *dependencies}
    if modules is None or not names <= {entry.text for entry in modules}:
        raise DeploymentError('Pinned modules do not match the configured Maven reactor')
    for entry in tuple(modules):
        if entry.text not in names:
            modules.remove(entry)
    pom.write(temporary / 'pom.xml', encoding='utf-8', xml_declaration=True)
    return temporary, worktree


def local_image(tag, platform, sha, *, optional=False):
    output = command('docker', 'image', 'inspect', tag, allow_failure=optional)
    if output.returncode:
        if optional and re.search(r'no such (?:image|object)', output.stderr, re.IGNORECASE):
            return None
        raise DeploymentError('Could not inspect local image')
    image = json.loads(output.stdout)[0]
    if (f'{image["Os"]}/{image["Architecture"]}' != platform
            or (image.get('Config', {}).get('Labels') or {}).get(REVISION) != sha):
        raise DeploymentError('Local image architecture or revision does not match the request')
    return required_digest(image['Id'])


def build_image(request, tag):
    target = request['target']
    platform = target['platform']
    if platform not in ('linux/amd64', 'linux/arm64'):
        raise DeploymentError('The publisher requires linux/amd64 or linux/arm64')
    command('docker', 'buildx', 'version', timeout=30)
    with TemporaryDirectory(prefix='zentao-publish-') as temp:
        root = Path(temp)
        reactor, worktree = build_workspace(request, root)
        args = command_args(request['service_config']['build_command'])
        if not args or args[0] not in ('mvn', 'mvn.cmd') or not set(args) & {'package', 'verify', 'install'}:
            raise DeploymentError('The publisher requires a configured Maven packaging command')
        command(*args, cwd=reactor)
        pattern = target.get('artifact_glob', 'smart-geo-server/target/*.jar'
                             if request['service'] == 'smart-geo' else 'target/*.jar')
        if Path(pattern).is_absolute() or '..' in Path(pattern).parts:
            raise DeploymentError('Packaging artifact pattern must remain inside the checkout')
        artifacts = [file for file in worktree.glob(pattern) if file.is_file()
                     and not file.name.endswith(('-sources.jar', '-javadoc.jar', '-tests.jar'))]
        if len(artifacts) != 1 or not artifacts[0].resolve().is_relative_to(worktree):
            raise DeploymentError('The checkout must produce one runtime JAR')
        context = root / '.docker-context'
        artifact = context / artifacts[0].relative_to(worktree)
        artifact.parent.mkdir(parents=True)
        copy2(artifacts[0], artifact)
        dockerfile = worktree / 'Dockerfile'
        if dockerfile.is_symlink() or not dockerfile.is_file():
            raise DeploymentError('The checkout must contain a regular Dockerfile')
        copy2(dockerfile, context / 'Dockerfile')
        command('docker', 'buildx', 'build', '--platform', platform, '--load', '--tag', tag,
                '--build-arg', f'VCS_REF={request["sha"]}', str(context))
    return local_image(tag, platform, request['sha'])


def remote_image(tag, platform):
    """Resolve a tag or digest, then inspect platform and immutable config."""
    args = ('docker', 'buildx', 'imagetools', 'inspect')
    result = command(*args, tag, allow_failure=True, timeout=90)
    if result.returncode:
        if re.search(r'manifest unknown|name unknown|(?:not found|404 not found)\s*\Z',
                     result.stderr, flags=re.IGNORECASE):
            return None
        raise DeploymentError('Could not verify the remote image tag')
    match = re.search(r'^Digest:\s*(sha256:[0-9a-f]{64})\s*$', result.stdout, re.MULTILINE)
    if not match:
        raise DeploymentError('The registry did not report a manifest digest')
    digest = match.group(1)
    reference, separator, pinned = tag.partition('@')
    if separator:
        if required_digest(pinned) != digest:
            raise DeploymentError('Harbor returned a different manifest digest')
    else:
        reference = tag.rsplit(':', 1)[0]
    manifest = json.loads(command(*args, f'{reference}@{digest}', '--raw', timeout=90).stdout)
    platform_digest = digest
    if 'manifests' in manifest:
        candidates = [
            entry for entry in manifest['manifests']
            if f'{entry.get("platform", {}).get("os")}/{entry.get("platform", {}).get("architecture")}' == platform
        ]
        if len(candidates) != 1:
            raise DeploymentError('The registry image must contain one requested platform')
        platform_digest = required_digest(candidates[0].get('digest'))
        manifest = json.loads(command(*args, f'{reference}@{platform_digest}', '--raw', timeout=90).stdout)
    config = json.loads(command(*args, f'{reference}@{platform_digest}', '--format', '{{json .Image}}',
                                timeout=90).stdout)
    if f'{config.get("os")}/{config.get("architecture")}' != platform:
        raise DeploymentError('Published architecture does not match the request')
    return {'digest': digest, 'platform_digest': platform_digest,
            'config_digest': required_digest(manifest.get('config', {}).get('digest')),
            'sha': (config.get('config', {}).get('Labels') or {}).get(REVISION)}


def owned_by(resource, kind, uid):
    return any(ref.get('uid') == uid and ref.get('kind') == kind and ref.get('controller') is True
               for ref in resource.get('metadata', {}).get('ownerReferences', ()))


class Kubernetes:
    def __init__(self, target):
        self.context = target['kube_context']
        self.connection = []
        if server := target.get('kube_server'):
            config = LOCAL_DIRECTORY / 'rancher-kubeconfig.yaml'
            if not config.is_file():
                raise DeploymentError('Missing .local/rancher-kubeconfig.yaml')
            # Pin the API endpoint even if the local context or default config changes.
            self.connection = ['--kubeconfig', str(config), '--server', server]
        self.namespace = target['namespace']
        self.name = target['deployment']
        self.container = target['container']
        self.uid = None

    def run(self, *args, timeout=90):
        return command('kubectl', '--context', self.context, '--namespace', self.namespace, *args, *self.connection,
                       timeout=timeout).stdout

    def deployment(self):
        value = json.loads(self.run('get', 'deployment', self.name, '-o', 'json'))
        uid = value['metadata']['uid']
        if self.uid is not None and uid != self.uid:
            raise DeploymentError('Deployment was replaced')
        self.uid = uid
        return value

    def container_location(self, deployment):
        containers = deployment['spec']['template']['spec']['containers']
        for index, container in enumerate(containers):
            if container['name'] == self.container:
                return index, container['image']
        raise DeploymentError('The configured deployment container was not found')

    def current_image(self):
        return self.container_location(self.deployment())[1]

    def preflight(self):
        image = self.current_image()
        permissions = (('patch', f'deployments/{self.name}'), ('watch', f'deployments/{self.name}'),
                       ('list', 'replicasets'), ('list', 'pods'))
        for verb, resource in permissions:
            if self.run('auth', 'can-i', verb, resource).strip() != 'yes':
                raise DeploymentError('Kubernetes permissions do not allow publication and read-back')
        return image

    def update(self, previous_image, reference):
        deployment = self.deployment()
        index, image = self.container_location(deployment)
        if image != previous_image:
            raise DeploymentError('Deployment image changed before the planned update')
        container_path = f'/spec/template/spec/containers/{index}'
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': deployment['metadata']['uid']},
                 {'op': 'test', 'path': container_path + '/name', 'value': self.container},
                 {'op': 'test', 'path': container_path + '/image', 'value': previous_image},
                 {'op': 'replace', 'path': container_path + '/image', 'value': reference}]
        self.run('patch', 'deployment', self.name, '--type=json', '-p', json.dumps(patch))

    def verify_rollout(self, reference, digests):
        self.run('rollout', 'status', f'deployment/{self.name}', '--timeout=300s', timeout=320)
        deployment = self.deployment()
        spec, metadata, status = deployment['spec'], deployment['metadata'], deployment.get('status', {})
        desired = spec.get('replicas', 1)
        if (self.container_location(deployment)[1] != reference or desired < 1
                or status.get('observedGeneration', 0) < metadata['generation']
                or status.get('updatedReplicas', 0) != desired or status.get('readyReplicas', 0) != desired):
            raise DeploymentError('Deployment did not reach the expected ready version')
        revision = metadata.get('annotations', {}).get('deployment.kubernetes.io/revision')
        replicasets = json.loads(self.run('get', 'replicasets', '-o', 'json'))['items']
        candidates = [replicaset for replicaset in replicasets if
                      owned_by(replicaset, 'Deployment', metadata['uid']) and revision is not None
                      and replicaset['metadata'].get('annotations', {}).get('deployment.kubernetes.io/revision')
                      == revision and self.container_location(replicaset)[1] == reference]
        if len(candidates) != 1:
            raise DeploymentError('Could not identify the ready Deployment replica set')
        uid = candidates[0]['metadata']['uid']
        pods = json.loads(self.run('get', 'pods', '-o', 'json'))['items']
        current = [pod for pod in pods if owned_by(pod, 'ReplicaSet', uid)
                   and not pod.get('metadata', {}).get('deletionTimestamp')]
        if len(current) != desired:
            raise DeploymentError('The new replica set does not have the expected pods')
        for pod in current:
            status = next((entry for entry in pod.get('status', {}).get('containerStatuses', ())
                           if entry.get('name') == self.container), {})
            image_id = re.search(r'(sha256:[0-9a-f]{64})\Z', status.get('imageID', ''))
            if status.get('ready') is not True or not image_id or image_id.group(1) not in digests:
                raise DeploymentError('A ready pod does not match the published image digest')
        if self.current_image() != reference:
            raise DeploymentError('Deployment image changed during runtime verification')
        return {'deployment_uid': metadata['uid'], 'revision': revision, 'ready_replicas': desired,
                'image': reference, 'pods': sorted(pod['metadata']['name'] for pod in current)}


def verify_http(target, sha):
    """Read back the exact commit from the configured gateway without following redirects."""
    gateway = target['gateway_url']
    origin = urlsplit(gateway)
    path = target['verification_path']
    if (origin.scheme not in ('http', 'https') or not origin.hostname or origin.username is not None
            or origin.password is not None or origin.path not in ('', '/') or origin.query or origin.fragment
            or not path.startswith('/') or path.startswith('//') or '?' in path or '#' in path
            or any(ch == '\\' or ord(ch) < 32 for ch in gateway + path)):
        raise DeploymentError('Invalid gateway version endpoint')
    headers = {'Accept': 'application/json'}
    credential = target.get('http_credential_ref')
    if credential:
        if origin.scheme != 'https' or not target.get('http_username'):
            raise DeploymentError('HTTP verification credentials require an HTTPS gateway and username')
        credentials_file = LOCAL_DIRECTORY / 'backend-credentials.json'
        try:
            auth = json.loads(credentials_file.read_text(encoding='utf-8-sig'))[credential]
            if (auth['base_url'].rstrip('/') != gateway.rstrip('/')
                    or auth['username'] != target['http_username']
                    or not isinstance(auth['password'], str) or not auth['password']):
                raise ValueError('Invalid version probe credential')
        except (OSError, KeyError, TypeError, AttributeError, ValueError):
            raise DeploymentError('Version endpoint credential unavailable') from None
        value = f'{auth["username"]}:{auth["password"]}'.encode('utf-8')
        headers['Authorization'] = 'Basic ' + b64encode(value).decode('ascii')
    connect = HTTPSConnection if origin.scheme == 'https' else HTTPConnection
    connection = connect(origin.hostname, origin.port, timeout=10)
    try:
        connection.request('GET', path, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise DeploymentError(f'Gateway version check returned HTTP {response.status}')
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise DeploymentError('Gateway version response exceeded the size limit')
        observed = json.loads(body)
    except (OSError, HTTPException, json.JSONDecodeError, UnicodeError):
        raise DeploymentError('Gateway version endpoint did not return usable JSON') from None
    finally:
        connection.close()
    for key in target.get('version_field', 'git.commit.id').split('.'):
        observed = observed.get(key) if isinstance(observed, dict) else None
    if observed != sha:
        raise DeploymentError('Gateway version does not match the merged commit')
    return {'sha': sha, 'url': gateway.rstrip('/') + path}


def verify_mcp_health(url):
    """Check process health independently of the rollout's image evidence."""
    origin = urlsplit(url)
    if (origin.scheme not in ('http', 'https') or not origin.hostname or origin.username is not None
            or origin.password is not None or not origin.path.startswith('/') or origin.path.startswith('//')
            or origin.query or origin.fragment or any(ch == '\\' or ord(ch) < 32 for ch in url)):
        raise DeploymentError('Invalid service health endpoint')
    connect = HTTPSConnection if origin.scheme == 'https' else HTTPConnection
    connection = connect(origin.hostname, origin.port, timeout=10)
    try:
        connection.request('GET', origin.path, headers={'Accept': 'application/json'})
        response = connection.getresponse()
        if response.status != 200:
            raise DeploymentError(f'Service health returned HTTP {response.status}')
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise DeploymentError('Service health response exceeded the size limit')
        observed = json.loads(body)
    except (OSError, HTTPException, json.JSONDecodeError, UnicodeError):
        raise DeploymentError('Service health endpoint did not return usable JSON') from None
    finally:
        connection.close()
    if not isinstance(observed, dict) or observed.get('status') != 'UP':
        raise DeploymentError('Service health is not UP')


def deploy_mcp(target, reference, receipt, record):
    from rancher_mcp import RancherMcpError, update_and_verify

    try:
        return update_and_verify(target, reference, receipt, record)
    except RancherMcpError as error:
        raise DeploymentError(str(error)) from None


def verify_mcp_image(target, reference):
    from rancher_mcp import RancherMcpError, verify_current

    try:
        verify_current(target, reference)
    except RancherMcpError as error:
        raise DeploymentError(str(error)) from None


def verify_published_release(receipt):
    from deploy_state import release_verification

    target = receipt['target']
    if target.get('kube_server'):
        verification = release_verification(receipt)
        kube = Kubernetes(target)
        kube.uid = receipt['pod_verification']['deployment_uid']
        kube.verify_rollout(verification['image'], {receipt[key] for key in ('digest', 'platform_digest', 'config_digest')})
        verify_mcp_health(target['health_url'])
        return verification
    if not target.get('mcp_url'):
        return verify_http(target, receipt['sha'])
    verification = release_verification(receipt)
    verify_mcp_image(target, verification['image'])
    verify_mcp_health(target['health_url'])
    return verification


def save_receipt(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def publish(request):
    path = Path(request['receipt_path'])
    target, sha = request['target'], request['sha']
    tag = image_tag(request)
    if request.get('expected_image_tag', tag) != tag:
        raise DeploymentError('Image tag no longer matches the acquired release lock')
    expected = {'service': request['service'], 'sha': sha, 'target': target, 'image': tag}
    receipt = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    if receipt and any(receipt.get(key) != value for key, value in expected.items()):
        raise DeploymentError('Prior deployment receipt belongs to another release')
    receipt.update(expected)
    receipt.pop('reason', None)

    def record(stage, **facts):
        receipt.update(status='in_progress', stage=stage, **facts)
        save_receipt(path, receipt)

    try:
        record('preflight')
        use_mcp = bool(target.get('mcp_url'))
        if use_mcp:
            record('packaging')
        else:
            kube = Kubernetes(target)
            kube.uid = receipt.get('deployment_uid')
            observed_image = kube.preflight()
            record('packaging', deployment_uid=kube.uid, previous_image=receipt.get('previous_image', observed_image))
        platform = target['platform']
        remote = remote_image(tag, platform)
        config = receipt.get('config_digest')
        if config is None:
            config = build_image(request, tag)
            record('image_push', config_digest=config)
        else:
            required_digest(config)
            record('image_push')
        if remote is None:
            local = local_image(tag, platform, sha, optional=True)
            if local is None and build_image(request, tag) != config:
                raise DeploymentError('Rebuilt image breaks the prior publication identity')
            if local is not None and local != config:
                raise DeploymentError('Local image differs from the verified release')
            # Recheck the tag after the build and before any write.
            remote = remote_image(tag, platform)
            if remote is None:
                command('docker', 'push', tag)
                remote = remote_image(tag, platform)
        if not remote or remote['config_digest'] != config or remote['sha'] != sha:
            raise DeploymentError('Image tag conflict or unreadable pushed image')
        digest = required_digest(remote['digest'])
        platform_digest = required_digest(remote['platform_digest'])
        if receipt.get('digest', digest) != digest:
            raise DeploymentError('Published tag digest changed since the prior attempt')
        record('deployment_update', digest=digest, platform_digest=platform_digest)
        reference = tag.rsplit(':', 1)[0] + '@' + digest
        if use_mcp:
            from deploy_state import release_verification

            pod = deploy_mcp(target, reference, receipt, lambda **facts: record('deployment_update', **facts))
            record('health_verification', pod_verification=pod)
            verify_mcp_health(target['health_url'])
            verification = release_verification(receipt)
        else:
            current = kube.current_image()
            if current != reference:
                if current != receipt['previous_image']:
                    raise DeploymentError('Deployment image was changed by another release')
                kube.update(current, reference)
            record('deployment_verification')
            pod = kube.verify_rollout(reference, {digest, platform_digest, config})
            record('gateway_verification', pod_verification=pod)
            if target.get('kube_server'):
                from deploy_state import release_verification
                verify_mcp_health(target['health_url'])
                verification = release_verification(receipt)
            else:
                verification = verify_http(target, sha)
        receipt.update(status='deployed', verification=verification, stage='complete')
        save_receipt(path, receipt)
        return receipt
    except (DeploymentError, OSError, ValueError, KeyError, TypeError, IndexError, ET.ParseError) as error:
        receipt.update(status='failed', reason=str(error) if isinstance(error, DeploymentError)
                       else 'Publication could not read or verify required data')
        save_receipt(path, receipt)
        if isinstance(error, DeploymentError):
            raise
        raise DeploymentError(receipt['reason']) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    args = parser.parse_args()
    try:
        request = json.loads(args.request.read_text(encoding='utf-8-sig'))
        publish(request)
    except (DeploymentError, OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'status': 'failed', 'reason': 'Publication failed. Read its deployment receipt.'}))
        return 1
    print(json.dumps({'status': 'deployed'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
