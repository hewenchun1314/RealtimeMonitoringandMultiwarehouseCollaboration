from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Thread
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from rancher_mcp import McpClient, RancherMcpError, current_image, update_and_verify, verify_current


TARGET = {'mcp_url': 'http://fixture.invalid/mcp', 'namespace': 'internalrd',
          'deployment': 'web', 'container': 'web', 'cluster_id': 'test-cluster'}
OLD = 'harbor.example/internalrd/web:old'
IMAGE = 'harbor.example/internalrd/web@sha256:' + 'a' * 64


class Fixture:
    def __init__(self):
        self.image = OLD
        self.pods = ['web-old']
        self.ready = True
        self.container = 'web'
        self.cluster = 'test-cluster'
        self.change_pull_secret = False
        self.calls = []

    def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == 'check_auth':
            return {'ok': False, 'API Server': 'https://rancher.example/k8s/clusters/' + self.cluster}
        if tool == 'get_deployment':
            return {'namespace': 'internalrd', 'name': 'web',
                    'replicas': {'desired': 1, 'ready': int(self.ready), 'available': int(self.ready)},
                    'podIPs': [{'pod': pod, 'ready': self.ready, 'phase': 'Running'} for pod in self.pods]}
        if tool != 'imageUpdate':
            raise AssertionError(tool)
        change = args['image'] != self.image
        body = {'namespace': 'internalrd', 'name': 'web', 'changed': change,
                'changes': [{'container': self.container, 'from': self.image, 'to': args['image']}] if change else [],
                'imagePullSecrets': {'from': ['hzhub'], 'to': ['other'] if self.change_pull_secret else ['hzhub']}}
        if args['apply']:
            self.image = args['image']
            self.pods = ['web-new']
        elif change:
            body['dryRun'] = True
        return body


class RancherRolloutTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        mocked = patch('rancher_mcp.McpClient', return_value=self.fixture)
        mocked.start()
        self.addCleanup(mocked.stop)
        self.receipt = {'expected_previous_image': OLD}
        self.events = []

    def record(self, **data):
        self.events.append(('record', data))
        self.receipt.update(data)
        self.assertFalse(any(args.get('apply') for _, args in self.fixture.calls))

    def test_updates_only_after_recording_baseline_and_proves_new_pod(self):
        proof = update_and_verify(TARGET, IMAGE, self.receipt, self.record, poll_interval=0)
        self.assertEqual(proof, {'image': IMAGE, 'pods': ['web-new'], 'previous_pods': ['web-old'],
                                 'ready_replicas': 1})
        self.assertEqual(self.events[0][1]['mcp_baseline']['previous_image'], OLD)
        self.assertEqual(len([1 for _, args in self.fixture.calls if args.get('apply')]), 1)
        self.assertEqual(verify_current(TARGET, IMAGE)['pods'], ['web-new'])
        self.assertEqual(len([1 for _, args in self.fixture.calls if args.get('apply')]), 1)

    def test_resume_never_reissues_already_applied_update(self):
        update_and_verify(TARGET, IMAGE, self.receipt, self.record, poll_interval=0)
        self.fixture.calls.clear()
        proof = update_and_verify(TARGET, IMAGE, self.receipt, self.record, poll_interval=0)
        self.assertEqual(proof['pods'], ['web-new'])
        self.assertFalse(any(args.get('apply') for _, args in self.fixture.calls))

    def test_never_applies_after_base_drift_extra_changes_or_wrong_container(self):
        for state in ({'image': 'harbor.example/internalrd/web:drift'}, {'change_pull_secret': True},
                      {'container': 'other'}):
            with self.subTest(state=state):
                self.fixture.__init__()
                vars(self.fixture).update(state)
                with self.assertRaises(RancherMcpError):
                    update_and_verify(TARGET, IMAGE, self.receipt, self.record, poll_interval=0)
                self.assertFalse(any(args.get('apply') for _, args in self.fixture.calls))

    def test_read_current_template_is_read_only(self):
        self.assertEqual(current_image(TARGET, IMAGE), OLD)
        self.fixture.image = IMAGE
        self.assertEqual(current_image(TARGET, IMAGE), IMAGE)
        self.assertFalse(any(args.get('apply') for _, args in self.fixture.calls))

    def test_cluster_drift_blocks_before_preview_or_write(self):
        self.fixture.cluster = 'other'
        with self.assertRaisesRegex(RancherMcpError, 'configured cluster'):
            update_and_verify(TARGET, IMAGE, self.receipt, self.record, poll_interval=0)
        self.assertEqual(self.fixture.calls, [('check_auth', {})])

    def test_old_ready_pod_cannot_prove_a_new_rollout(self):
        update_and_verify(TARGET, IMAGE, self.receipt, self.record, poll_interval=0)
        self.fixture.pods = ['web-old']
        self.fixture.calls.clear()
        with self.assertRaisesRegex(RancherMcpError, 'rollout'):
            update_and_verify(TARGET, IMAGE, self.receipt, self.record, timeout=0.01, poll_interval=0.01)
        self.assertFalse(any(args.get('apply') for _, args in self.fixture.calls))

    def test_unconfirmed_write_reconciles_without_second_write(self):
        call = self.fixture.call

        def apply_then_disconnect(tool, args):
            response = call(tool, args)
            if args.get('apply'):
                raise RancherMcpError('connection closed')
            return response

        self.fixture.call = apply_then_disconnect
        with self.assertRaisesRegex(RancherMcpError, 'connection closed'):
            update_and_verify(TARGET, IMAGE, self.receipt, self.record, poll_interval=0)
        self.assertEqual(self.receipt['mcp_baseline']['previous_pods'], ['web-old'])
        self.fixture.calls.clear()
        self.assertEqual(update_and_verify(TARGET, IMAGE, self.receipt, self.record, poll_interval=0)['pods'],
                         ['web-new'])
        self.assertFalse(any(args.get('apply') for _, args in self.fixture.calls))


class StreamableHttpTests(unittest.TestCase):
    def test_negotiates_session_and_reads_sse_wrapped_tool_output(self):
        events = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                events.append((data['method'], self.headers.get('Mcp-Session-Id'),
                               self.headers.get('MCP-Protocol-Version')))
                if data['method'] == 'notifications/initialized':
                    self.send_response(202)
                    self.end_headers()
                    return
                if data['method'] == 'initialize':
                    result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}}
                else:
                    result = {'content': [{'type': 'text', 'text': json.dumps({'healthy': True})}]}
                body = ('event: message\ndata: ' + json.dumps({'jsonrpc': '2.0', 'id': data['id'],
                                                                   'result': result}) + '\n\n').encode()
                self.send_response(200)
                self.send_header('Mcp-Session-Id', 'fixture-session')
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host = f'http://127.0.0.1:{server.server_port}/mcp'
                self.assertEqual(McpClient(host, timeout=3).call('get_deployment', {}), {'healthy': True})
            finally:
                server.shutdown()
        self.assertEqual(events, [('initialize', None, None),
                                 ('notifications/initialized', 'fixture-session', '2025-03-26'),
                                 ('tools/call', 'fixture-session', '2025-03-26')])
