"""Update and verify one configured Deployment over the Rancher MCP service."""

from http.client import HTTPConnection, HTTPException, HTTPSConnection
import json
import os
import re
import time
from urllib.parse import urlsplit


PROTOCOL_VERSION = '2025-03-26'
MAX_RESPONSE = 512 * 1024
IMAGE = re.compile(r'[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}')


class RancherMcpError(ValueError):
    pass


class McpClient:
    def __init__(self, url, *, timeout, bearer_env=None):
        try:
            self.url = urlsplit(url)
            self.port = self.url.port
        except (TypeError, ValueError):
            raise RancherMcpError('Invalid Rancher MCP URL') from None
        if (self.url.scheme not in ('http', 'https') or not self.url.hostname or not self.url.path
                or self.url.username is not None or self.url.fragment or self.url.query):
            raise RancherMcpError('Invalid Rancher MCP URL')
        if not 0 < timeout:
            raise RancherMcpError('Rancher MCP requires a positive timeout')
        self.deadline = time.monotonic() + timeout
        self.headers = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
        self.sequence = 0
        if bearer_env:
            token = os.environ.get(bearer_env)
            if self.url.scheme != 'https' or not token:
                raise RancherMcpError('Rancher MCP bearer authentication requires HTTPS and a configured token')
            self.headers['Authorization'] = 'Bearer ' + token
        self.initialized = False

    def remaining(self):
        return max(0.0, self.deadline - time.monotonic())

    def _send(self, method, params=None, *, notification=False):
        self.sequence += 1
        request = {'jsonrpc': '2.0', 'method': method}
        request_id = self.sequence
        if not notification:
            request['id'] = request_id
        if params is not None:
            request['params'] = params
        connection_type = HTTPSConnection if self.url.scheme == 'https' else HTTPConnection
        remaining = self.remaining()
        if not remaining:
            raise RancherMcpError('Rancher MCP deadline elapsed')
        connection = connection_type(self.url.hostname, self.port, timeout=min(10.0, remaining))
        try:
            connection.request('POST', self.url.path, json.dumps(request).encode(), self.headers)
            response = connection.getresponse()
            if response.status == 202 and notification:
                return None
            if response.status != 200:
                raise RancherMcpError(f'Rancher MCP {method} returned HTTP {response.status}')
            if method == 'initialize' and (session := response.getheader('Mcp-Session-Id')):
                self.headers['Mcp-Session-Id'] = session
            if notification:
                return None
            envelope = self._read(response, request_id)
        except (OSError, HTTPException, UnicodeError, json.JSONDecodeError):
            raise RancherMcpError(f'Rancher MCP {method} was not confirmed') from None
        finally:
            connection.close()
        if not isinstance(envelope, dict) or envelope.get('id') != request_id:
            raise RancherMcpError(f'Rancher MCP {method} response ID does not match')
        if not isinstance(envelope.get('result'), dict):
            error = envelope.get('error')
            code = error.get('code') if isinstance(error, dict) else None
            suffix = f' ({code})' if type(code) is int else ''
            raise RancherMcpError(f'Rancher MCP {method} did not return a result{suffix}')
        return envelope['result']

    def _read(self, response, request_id):
        if response.headers.get_content_type() != 'text/event-stream':
            body = response.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise RancherMcpError('Rancher MCP response exceeds size limit')
            return json.loads(body)
        total = 0
        event = []
        while self.remaining():
            line = response.readline(MAX_RESPONSE - total + 1)
            total += len(line)
            if total > MAX_RESPONSE:
                raise RancherMcpError('Rancher MCP response exceeds size limit')
            if not line:
                break
            if line.startswith(b'data:'):
                event.append(line[5:].lstrip().rstrip(b'\r\n'))
            elif line in (b'\n', b'\r\n') and event:
                body = json.loads(b'\n'.join(event))
                if isinstance(body, dict) and body.get('id') == request_id:
                    return body
                event.clear()
        raise RancherMcpError('Rancher MCP stream ended without a matching result')

    def call(self, tool, arguments):
        if not self.initialized:
            info = self._send('initialize', {
                'protocolVersion': PROTOCOL_VERSION,
                'clientInfo': {'name': 'zentao-delivery', 'version': '1'},
                'capabilities': {},
            })
            if (not isinstance(info.get('protocolVersion'), str)
                    or re.fullmatch(r'\d{4}-\d{2}-\d{2}', info['protocolVersion']) is None
                    or not isinstance(info.get('capabilities'), dict) or 'tools' not in info['capabilities']):
                raise RancherMcpError('Rancher MCP did not negotiate a tool session')
            self.headers['MCP-Protocol-Version'] = info['protocolVersion']
            self._send('notifications/initialized', notification=True)
            self.initialized = True
        result = self._send('tools/call', {'name': tool, 'arguments': arguments})
        if result.get('isError') is True:
            raise RancherMcpError(f'Rancher MCP rejected {tool}')
        body = result.get('structuredContent')
        if not isinstance(body, dict):
            try:
                block, = result['content']
                if block['type'] == 'text':
                    body = json.loads(block['text'])
            except (KeyError, TypeError, ValueError):
                body = None
        if (not isinstance(body, dict) or body.get('error')
                or (tool != 'check_auth' and body.get('ok') is False)):
            raise RancherMcpError(f'Rancher MCP {tool} did not return verified data')
        return body


def _client(target, timeout):
    client = McpClient(target['mcp_url'], timeout=timeout, bearer_env=target.get('mcp_bearer_token_env'))
    details = client.call('check_auth', {})
    server = details.get('API Server')
    url = urlsplit(server) if isinstance(server, str) else None
    if (not url or not url.hostname or url.scheme not in ('http', 'https')
            or url.path.rstrip('/') != '/k8s/clusters/' + target['cluster_id']):
        raise RancherMcpError('Rancher MCP does not point to the configured cluster')
    return client


def _check_image(image):
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise RancherMcpError('Rancher MCP requires an immutable Harbor image reference')


def _call(client, target, tool, **args):
    identity = {'name': target['deployment'], 'namespace': target['namespace']}
    data = client.call(tool, {**identity, **args})
    if any(data.get(key) != value for key, value in identity.items()):
        raise RancherMcpError(f'Rancher MCP {tool} returned a different target')
    return data


def _template(client, target, image, *, apply=False, prior=None):
    data = _call(client, target, 'imageUpdate', image=image, apply=apply)
    changed = data.get('changed')
    changes = data.get('changes')
    secrets = data.get('imagePullSecrets')
    if (type(changed) is not bool or not isinstance(changes, list) or type(secrets) is not dict
            or not isinstance(secrets.get('from'), list) or secrets.get('from') != secrets.get('to')):
        raise RancherMcpError('Rancher MCP image update would also change image pull secrets or has incomplete data')
    if not changed and not changes:
        return image
    if changed and data.get('dryRun') is (not apply) and len(changes) == 1:
        change = changes[0]
        previous = change.get('from') if isinstance(change, dict) else None
        repository = image.split('@', 1)[0]
        if (isinstance(previous, str) and previous.startswith((repository + ':', repository + '@'))
                and change.get('container') == target['container'] and change.get('to') == image
                and (prior is None or prior == previous)):
            return previous
    # Older write responses do not include dryRun. A fresh read-back of the
    # complete rollout is still required before reporting success.
    if apply and 'dryRun' not in data and changed and len(changes) == 1:
        if changes[0] == {'container': target['container'], 'from': prior, 'to': image}:
            return prior
    raise RancherMcpError('Rancher MCP image update does not match the expected container and images')


def _snapshot(client, target):
    data = _call(client, target, 'get_deployment')
    replicas, pods = data.get('replicas'), data.get('podIPs')
    if not isinstance(replicas, dict) or not isinstance(pods, list):
        raise RancherMcpError('Rancher MCP lacks Deployment replica or Pod status')
    counts = tuple(replicas.get(key) for key in ('desired', 'ready', 'available'))
    if not all(type(value) is int and value >= 0 for value in counts) or counts[0] < 1:
        raise RancherMcpError('Rancher MCP returned invalid Deployment replica status')
    named = {}
    for pod in pods:
        name = pod.get('pod') if isinstance(pod, dict) else None
        if not isinstance(name, str) or not name or name in named or type(pod.get('ready')) is not bool:
            raise RancherMcpError('Rancher MCP returned invalid Pod status')
        named[name] = pod.get('phase') == 'Running' and pod['ready']
    ready = sorted(name for name, ready in named.items() if ready)
    return counts, sorted(named), ready


def current_image(target, probe_image, *, timeout=30.0):
    _check_image(probe_image)
    return _template(_client(target, timeout), target, probe_image)


def verify_current(target, image, *, timeout=30.0):
    _check_image(image)
    client = _client(target, timeout)
    counts, _, ready = _snapshot(client, target)
    desired = counts[0]
    if counts != (desired,) * 3 or len(ready) != desired or _template(client, target, image) != image:
        raise RancherMcpError('Rancher MCP could not verify the expected image and Ready Pods')
    return {'image': image, 'pods': ready, 'ready_replicas': desired}


def update_and_verify(target, image, receipt, record, *, timeout=300.0, poll_interval=2.0):
    _check_image(image)
    client = _client(target, timeout)
    deadline = time.monotonic() + timeout
    current = _template(client, target, image)
    expected = receipt.get('expected_previous_image')
    baseline = receipt.get('mcp_baseline')
    if baseline is None:
        if current == image:
            raise RancherMcpError('Rancher MCP already has the target image without a recorded rollout baseline')
        if expected is not None and current != expected:
            raise RancherMcpError('Rancher MCP previous image changed before publication')
        counts, original, ready = _snapshot(client, target)
        if len(ready) < counts[1]:
            raise RancherMcpError('Rancher MCP did not list every previously Ready Pod')
        baseline = {'previous_image': current, 'previous_pods': original, 'desired_replicas': counts[0]}
        record(mcp_baseline=baseline)
    if (not isinstance(baseline, dict) or not isinstance(baseline.get('previous_pods'), list)
            or any(not isinstance(name, str) or not name for name in baseline['previous_pods'])
            or type(baseline.get('desired_replicas')) is not int or baseline['desired_replicas'] < 1
            or not isinstance(baseline.get('previous_image'), str) or baseline['previous_image'] == image
            or (expected is not None and baseline['previous_image'] != expected)):
        raise RancherMcpError('Rancher MCP rollout baseline is invalid')
    old = baseline['previous_image']
    if current != image:
        current = _template(client, target, image)
        if current not in (old, image):
            raise RancherMcpError('Rancher MCP previous image changed before the update')
        if current == old:
            _template(client, target, image, apply=True, prior=old)
    desired = baseline['desired_replicas']
    previous = set(baseline['previous_pods'])
    while time.monotonic() < deadline:
        counts, _, ready = _snapshot(client, target)
        if counts[0] != desired:
            raise RancherMcpError('Rancher MCP Deployment replica count changed during rollout')
        if counts == (desired,) * 3 and len(ready) == desired and not previous.intersection(ready):
            if _template(client, target, image) != image:
                raise RancherMcpError('Rancher MCP template image changed during rollout')
            return {'image': image, 'pods': ready, 'ready_replicas': desired, 'previous_pods': sorted(previous)}
        time.sleep(max(0.0, min(poll_interval, deadline - time.monotonic())))
    raise RancherMcpError('Rancher MCP rollout did not reach the expected number of new Ready Pods')
