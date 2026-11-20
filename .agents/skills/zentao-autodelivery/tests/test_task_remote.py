"""Tests for the ZenTao task adapter and its read-back contracts."""
import sys
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from task_remote import RemoteRejected, RemoteUnknown, TaskRemote, ZenTaoError, get_remote


def page(key, rows, page_number=1, total=None, size=100):
    total = len(rows) if total is None else total
    return {key: rows, 'pager': {'pageID': page_number, 'recTotal': total, 'recPerPage': size}}


class TaskRemoteTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.remote = TaskRemote(self.api)
        self.api.get_story.return_value = {'id': 34, 'assignedTo': {'account': 'Alice'}}

    def test_target_requires_unique_story_in_execution_of_actual_project(self):
        def read(method, path):
            endpoint = urlsplit(path).path
            routes = {
                '/projects/2/executions': page('executions', [{'id': 7}, {'id': 9}]),
                '/executions/7/stories': page('stories', [{'id': 34}]),
                '/executions/9/stories': page('stories', [{'id': 54}]),
            }
            return routes[endpoint]
        self.api._v2.side_effect = read
        self.assertEqual(self.remote.target('34', '2'), {'execution': '7', 'assignee': 'Alice'})
        self.api._v2.side_effect = lambda method, path: (
            page('executions', [{'id': 7}, {'id': 9}]) if '/projects/' in path
            else page('stories', [{'id': 34}]))
        with self.assertRaisesRegex(ValueError, '迭代'):
            self.remote.target(34, 2)

    def test_find_paginates_and_reads_description_for_each_id(self):
        def read(method, path):
            n = int(parse_qs(urlsplit(path).query)['pageID'][0])
            return page('tasks', [{'id': n}], n, 2, 1)
        self.api._v2.side_effect = read
        self.api.get_task.side_effect = lambda task_id: {
            'id': task_id, 'story': 34, 'execution': 7, 'status': 'done', 'assignedTo': 'A',
            'desc': 'something [item/F1]' if task_id == 2 else 'something [item/F10]'}
        self.assertEqual([task['id'] for task in self.remote.find('7', 'item/F1')], ['2'])

    def test_incomplete_pagination_is_unknown(self):
        self.api._v2.return_value = page('tasks', [], 1, 4, 1)
        with self.assertRaises(ValueError):
            self.remote.find(7, 'item/F1')

    def test_native_memberships_select_only_executions_in_the_original_project(self):
        story = self.api.get_story.return_value
        story['executions'] = {'3': {'id': 3, 'project': 2}, '4': {'id': 4, 'project': 7}}
        self.assertEqual(self.remote.target('34', '2'), {'execution': '3', 'assignee': 'Alice'})
        self.api._v2.assert_not_called()
        story['executions']['4']['project'] = 2
        with self.assertRaisesRegex(RemoteRejected, '不唯一'):
            self.remote.target(34, 2)

    def test_create_carries_marker_story_execution_and_owner(self):
        self.api.create_task.return_value = {'id': 31}
        self.assertEqual(self.remote.create({'execution': '7', 'assignee': 'Alice'},
                                            34, '页面', '<登录>', 'item/F1'), '31')
        payload = self.api.create_task.call_args.args[0]
        self.assertEqual((payload['executionID'], payload['story'], payload['assignedTo']), (7, 34, 'Alice'))
        self.assertIn('[item/F1]', payload['desc'])
        self.assertIn('&lt;登录&gt;', payload['desc'])

    def test_task_identity_mismatch_is_rejected(self):
        self.api.get_task.return_value = {'id': 33, 'story': 34, 'execution': 7, 'status': 'done'}
        with self.assertRaises(ValueError):
            self.remote.get(32)

    def test_closed_task_is_not_closed_again(self):
        self.api.get_task.return_value = {'id': 1, 'story': 34, 'execution': 7, 'status': 'closed',
                                          'assignedTo': 'closed', 'desc': '[item/F1]'}
        self.remote.transition(1, 'close')
        self.api._v2.assert_not_called()

    def test_zero_estimate_reopens_original_task_without_updating_logged_effort(self):
        self.api.get_task.return_value = {'id': 1, 'status': 'closed', 'estimate': '0.00', 'left': 0}
        self.api._v2.return_value = {'status': 'success'}
        self.remote.transition(1, 'reopen', assignee='Alice')
        method, path, payload = self.api._v2.call_args.args
        self.assertEqual((method, path, payload['assignedTo'], payload['left']),
                         ('PUT', '/tasks/1/activate', 'Alice', '0.01'))
        self.assertIn('0.01', payload['comment'])
        self.assertNotIn('consumed', payload)
        self.assertNotIn('estimate', payload)

    def test_reopen_prefers_recorded_remaining_hours(self):
        self.api.get_task.return_value = {'id': 1, 'status': 'closed', 'estimate': 3, 'left': 2}
        self.api._v2.return_value = {'status': 'success'}
        self.remote.transition(1, 'reopen', assignee='Alice')
        self.assertEqual(self.api._v2.call_args.args[2]['left'], '2')

    def test_writes_distinguish_permission_refusal_and_dropped_response(self):
        args = ({'execution': '7', 'assignee': 'Alice'}, 34, 'Title', 'Text', 'F1')
        self.api.create_task.side_effect = ZenTaoError('response-with-credentials', status=403)
        with self.assertRaises(RemoteRejected) as permission:
            self.remote.create(*args)
        self.assertTrue(permission.exception.safe_to_retry)
        self.api.create_task.side_effect = TimeoutError('response-with-credentials')
        with self.assertRaises(RemoteUnknown) as lost:
            self.remote.create(*args)
        self.assertFalse(lost.exception.safe_to_retry)
        self.assertNotIn('credentials', str(lost.exception))

    def test_api_redirect_cannot_send_login_or_token_to_another_origin(self):
        other_requests = []

        class OtherOrigin(BaseHTTPRequestHandler):
            def record(self):
                other_requests.append(self.command)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')

            do_GET = do_POST = record

            def log_message(self, *args):
                pass

        def start(handler):
            server = HTTPServer(('127.0.0.1', 0), handler)
            thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join, 2)
            self.addCleanup(server.shutdown)
            return f'http://127.0.0.1:{server.server_port}'

        other = start(OtherOrigin)

        class Original(BaseHTTPRequestHandler):
            redirect_login = False

            def do_POST(self):
                if self.redirect_login:
                    self.do_GET()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"status":"success","token":"unit-test-token"}')

            def do_GET(self):
                self.send_response(302)
                self.send_header('Location', other + '/login')
                self.end_headers()

            def log_message(self, *args):
                pass

        config = {'zentao': {'base_url': start(Original)}}
        with patch('task_remote._credentials', return_value=('test', 'test-only-password')):
            remote = get_remote(config)
            with self.assertRaises(RemoteRejected):
                remote.get(1)
            Original.redirect_login = True
            with self.assertRaises(RemoteRejected):
                get_remote(config)
        self.assertEqual(other_requests, [])
