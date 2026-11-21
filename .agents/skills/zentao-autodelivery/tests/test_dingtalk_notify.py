import sqlite3
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

from test_workflow import WorkflowFixture


class DingTalkNotificationTests(WorkflowFixture):
    def notifications(self):
        with closing(sqlite3.connect(self.database)) as conn:
            return conn.execute('SELECT event_type, body, state FROM dingtalk_notifications ORDER BY rowid').fetchall()

    def setUp(self):
        super().setUp()
        self.config['notifications'] = {'dingtalk': {'enabled': True}}

    def test_first_start_and_stop_enqueue_two_distinct_messages(self):
        binding = self.start()
        self.assertEqual(self.record('start', **binding)['status'], 'already_started')
        self.assertEqual(self.record('stop', **binding, stop_evidence={
            'chat_status': 'idle', 'owned_processes_active_after_stop': 0,
        })['status'], 'completed')
        rows = self.notifications()
        self.assertEqual([row[0] for row in rows], ['started', 'closed'])
        self.assertIn('【已接单】', rows[0][1])
        self.assertIn('【主动停跑】', rows[1][1])
        self.assertTrue(all('#2 Fixture 2' in row[1] for row in rows))

    def test_disabled_configuration_enqueues_nothing(self):
        self.config['notifications']['dingtalk']['enabled'] = False
        self.start()
        self.assertEqual(self.notifications(), [])

    def test_send_failure_leaves_committed_work_item_and_retries_throttle(self):
        from dingtalk_notify import drain

        self.start()
        with patch('dingtalk_notify.read_credentials', return_value=('fixture', 'fixture')):
            with patch('dingtalk_notify.send_message', side_effect=['retry', 'sent']) as sender:
                drain(self.database, self.config, now=self.time)
                self.assertEqual(self.notifications()[0][2], 'pending')
                drain(self.database, self.config, now=self.time)
                self.assertEqual(sender.call_count, 1)
                drain(self.database, self.config, now=self.time + timedelta(minutes=2))
                self.assertEqual(self.notifications()[0][2], 'sent')
                drain(self.database, self.config, now=self.time + timedelta(hours=1))
                self.assertEqual(sender.call_count, 2)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT state FROM work_items WHERE item_id=2').fetchone(), ('running',))

    def test_unknown_receipt_is_preserved_without_replay(self):
        from dingtalk_notify import drain

        self.start()
        with patch('dingtalk_notify.read_credentials', return_value=('fixture', 'fixture')):
            with patch('dingtalk_notify.send_message', return_value='unknown') as sender:
                drain(self.database, self.config, now=self.time)
                drain(self.database, self.config, now=self.time + timedelta(hours=1))
                self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.notifications()[0][2], 'unknown')

    def test_signed_request_uses_dingtalk_endpoint_and_redacts_credentials(self):
        import base64
        import hashlib
        import hmac
        import json
        from io import BytesIO
        from urllib.parse import parse_qs, urlsplit

        from dingtalk_notify import OPENER, send_message

        token = 'fixture_token_not_a_real_secret'
        secret = 'SECfixture_secret_not_real'
        url = f'https://oapi.dingtalk.com/robot/send?access_token={token}'
        with patch.object(OPENER, 'open', return_value=BytesIO(b'{"errcode":0}')) as transport:
            self.assertEqual(send_message(url, secret, '模拟通知'), 'sent')
        request = transport.call_args.args[0]
        signed = parse_qs(urlsplit(request.full_url).query)
        material = f'{signed["timestamp"][0]}\n{secret}'.encode()
        expected_sign = base64.b64encode(hmac.digest(secret.encode(), material, hashlib.sha256)).decode()
        self.assertEqual(signed['sign'], [expected_sign])
        self.assertEqual(json.loads(request.data), {'msgtype': 'text', 'text': {'content': '模拟通知'}})
        with self.assertRaisesRegex(ValueError, 'Webhook 地址') as invalid:
            send_message('https://example.invalid/robot/send?access_token=' + token, secret, '模拟通知')
        self.assertNotIn(token, str(invalid.exception))

    def test_connection_failure_and_uncertain_response_use_different_retry_policies(self):
        from http.client import IncompleteRead
        from urllib.error import URLError

        from dingtalk_notify import OPENER, send_message

        url = 'https://oapi.dingtalk.com/robot/send?access_token=fixture_token_not_a_real_secret'
        secret = 'SECfixture_secret_not_real'
        with patch.object(OPENER, 'open', side_effect=URLError(ConnectionRefusedError())):
            self.assertEqual(send_message(url, secret, '模拟通知'), 'retry')
        with patch.object(OPENER, 'open', side_effect=IncompleteRead(b'{"errcode":')):
            self.assertEqual(send_message(url, secret, '模拟通知'), 'unknown')

    def test_invalid_deeply_nested_receipt_does_not_escape_the_sender(self):
        from io import BytesIO

        from dingtalk_notify import OPENER, send_message

        url = 'https://oapi.dingtalk.com/robot/send?access_token=fixture_token_not_a_real_secret'
        with patch.object(OPENER, 'open', return_value=BytesIO(b'[' * 4000 + b']' * 4000)):
            self.assertEqual(send_message(url, 'SECfixture_secret_not_real', '模拟通知'), 'unknown')

    def test_sender_process_timeout_returns_a_status(self):
        from subprocess import TimeoutExpired

        from dingtalk_notify import dispatch

        with patch('dingtalk_notify.subprocess.run', side_effect=TimeoutExpired('fixture', 18)) as runner:
            self.assertEqual(dispatch(), {'status': 'worker_timeout'})
            self.assertEqual(runner.call_args.kwargs['timeout'], 18)
