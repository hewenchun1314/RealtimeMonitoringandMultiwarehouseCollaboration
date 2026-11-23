import json
import sqlite3
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from test_workflow import WorkflowFixture


class StoryReviewTests(WorkflowFixture):
    def setUp(self):
        super().setUp()
        self.config['runtime']['story_review_sync'] = True
        self.config['workspaces'] = {'requirement': '.'}
        self.intent = self.fixture / 'intent.md'
        self.intent.write_text('研发范围：纯前端', encoding='utf-8')
        self.target = {'version': '2', 'round': '100', 'account': 'Test-user', 'status': 'reviewing'}

    def material(self, *, missing=False, technical=False, handoff=False):
        from story_review_sync import release_handoff

        binding = self.start()
        attempt = self.record('attempt_start', stage='material_check', **binding)
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute('INSERT INTO story_review_targets(attempt_id,target) VALUES (?,?)',
                         (attempt['attempt_id'], json.dumps(self.target)))
        evidence = {'path': 'intent.md'}
        if missing:
            self.intent.unlink()
            evidence['material_file_status'] = 'missing'
        result = self.record('attempt_finish', **binding, attempt_id=attempt['attempt_id'], phase='verify',
                             outcome='failed' if missing or technical else 'success', evidence=evidence,
                             retryable=False, summary='缺少需求正文，请补充 intent.md' if missing else '材料检查结果')
        if not handoff and result.get('disposition') == 'review_wait':
            self.assertTrue(release_handoff(self.database, self.config, self.item(2)))
        return result, binding

    def review_operation(self):
        with closing(sqlite3.connect(self.database)) as conn:
            row = conn.execute("SELECT operation_id,payload FROM writebacks WHERE operation_type='story_review'").fetchone()
            return (row[0], json.loads(row[1])) if row else (None, None)

    def test_pass_waits_and_releases_slot(self):
        from story_review_sync import release_handoff, sync_review

        finish, binding = self.material(handoff=True)
        self.assertEqual(finish['disposition'], 'review_wait')
        self.assertEqual(self.review_operation()[1]['result'], 'pass')
        self.assertEqual(self.record('attempt_start', stage='split', **binding)['status'], 'story_review_pending')
        self.assertEqual(self.reserve(3)['status'], 'busy')
        remote = ReviewFixture(self.target)
        self.assertEqual(sync_review(self.database, self.config, remote, self.item(2))['status'], 'review_not_pending')
        self.assertEqual(remote.submissions, [])
        self.assertTrue(release_handoff(self.database, self.config, self.item(2)))
        self.assertEqual(self.reserve(3)['status'], 'reserved')

    def test_missing_material_queues_a_formal_clarification(self):
        finish, _ = self.material(missing=True)
        self.assertEqual(finish['disposition'], 'review_wait')
        operation, payload = self.review_operation()
        self.assertEqual(payload['result'], 'clarify')
        self.assertIn('请补充 intent.md', payload['comment'])
        self.assertIn(f'[{operation}]', payload['comment'])

    def test_technical_failure_does_not_cast_a_vote(self):
        finish, _ = self.material(technical=True)
        self.assertEqual(finish['disposition'], 'blocked')
        self.assertEqual(self.review_operation(), (None, None))

    def test_launcher_waits_for_stop_proof_before_releasing_a_recovered_handoff(self):
        import worker

        self.material(handoff=True)
        config_path = self.fixture / 'project.json'
        config_path.write_text(json.dumps(self.config), encoding='utf-8')
        with (patch.object(worker, 'DATABASE_PATH', self.database), patch.object(worker, 'CONFIG_PATH', config_path),
              patch.object(worker, 'deployment_action', return_value={'status': 'deployment_not_pending'}),
              patch.object(worker, 'runtime_check', return_value={'status': 'runtime_unattended'}),
              patch.object(worker, 'sync_item_review', return_value={'status': 'review_waiting_votes'}) as sync):
            with closing(sqlite3.connect(self.database)) as conn:
                marker = conn.execute("SELECT marker FROM work_items WHERE item_id='2'").fetchone()[0]

            def unexpected_chat(_):
                self.fail('The waiting Story must not launch the original chat before a review receipt')

            self.assertEqual(worker.dispatch_work_item(marker, unexpected_chat)['status'], 'worker_review_stop_unverified')
            self.assertEqual(self.reserve(3)['status'], 'busy')
            sync.assert_not_called()
            self.assertEqual(worker.dispatch_work_item(marker, unexpected_chat, recover_review=lambda item: True)['status'],
                             'worker_review_wait')
            sync.assert_called_once()
            self.assertEqual(self.reserve(3)['status'], 'reserved')

    def test_recorded_pass_waits_for_activation_then_reacquires_slot(self):
        from story_review_sync import resume_ready, sync_review

        self.material()
        remote = ReviewFixture(self.target)
        self.assertEqual(sync_review(self.database, self.config, remote, self.item(2))['status'], 'review_waiting_votes')
        self.assertEqual(sync_review(self.database, self.config, remote, self.item(2))['status'], 'review_waiting_votes')
        self.assertEqual(remote.submissions, ['pass'])
        remote.status = 'active'
        self.assertEqual(sync_review(self.database, self.config, remote, self.item(2))['status'], 'review_ready')
        self.assertEqual(resume_ready(self.database, self.config, self.item(2))['stage'], 'split')

    def test_clarify_readback_goes_to_local_closeout(self):
        from story_review_sync import resume_ready, sync_review

        self.material(missing=True)
        remote = ReviewFixture(self.target)
        self.assertEqual(sync_review(self.database, self.config, remote, self.item(2))['status'], 'review_ready')
        result = resume_ready(self.database, self.config, self.item(2))
        self.assertEqual(result['item_state'], 'closing')
        self.assertEqual(result['conclusion'], '待补材料')
        self.assertEqual(remote.submissions, ['clarify'])

    def test_ambiguous_post_is_reconciled_without_second_submission(self):
        from story_review_sync import sync_review

        self.material()
        remote = ReviewFixture(self.target, lose_response=True)
        self.assertEqual(sync_review(self.database, self.config, remote, self.item(2))['status'], 'review_write_uncertain')
        remote.status = 'active'
        self.assertEqual(sync_review(self.database, self.config, remote, self.item(2))['status'], 'review_ready')
        self.assertEqual(remote.submissions, ['pass'])

    def test_review_can_be_rearmed_after_an_uncertain_request_ended_without_a_vote(self):
        from story_review_sync import rearm_uncertain, sync_review

        self.material()
        remote = ReviewFixture(self.target, lose_response=True)
        self.assertEqual(sync_review(self.database, self.config, remote, self.item(2))['status'], 'review_write_uncertain')
        remote.actions.clear()
        self.assertEqual(sync_review(self.database, self.config, remote, self.item(2))['status'], 'review_write_uncertain')
        self.assertEqual(remote.submissions, ['pass'])
        self.assertEqual(rearm_uncertain(self.database, self.config, remote, self.item(2), prior_ended=False)['status'],
                         'review_prior_request_unverified')
        self.assertEqual(rearm_uncertain(self.database, self.config, remote, self.item(2), prior_ended=True)['status'],
                         'review_rearmed')
        remote.lose_response = False
        self.assertEqual(sync_review(self.database, self.config, remote, self.item(2))['status'], 'review_waiting_votes')
        self.assertEqual(remote.submissions, ['pass', 'pass'])

    def test_changed_review_round_blocks_without_casting_a_vote(self):
        from story_review_sync import resume_ready, sync_review

        self.material()
        remote = ReviewFixture({**self.target, 'round': '110'})
        self.assertEqual(sync_review(self.database, self.config, remote, self.item(2))['status'], 'review_ready')
        self.assertEqual(resume_ready(self.database, self.config, self.item(2))['conclusion'], '阻断')
        self.assertEqual(remote.submissions, [])

    def test_existing_activation_allows_material_pass_without_casting_a_vote(self):
        from story_review_sync import resume_ready, sync_review

        self.target['status'] = 'active'
        self.material()
        remote = ReviewFixture(self.target)
        self.assertEqual(sync_review(self.database, self.config, remote, self.item(2))['status'], 'review_ready')
        self.assertEqual(resume_ready(self.database, self.config, self.item(2))['stage'], 'split')
        self.assertEqual(remote.submissions, [])


class ReviewFixture:
    def __init__(self, target, *, lose_response=False):
        self.target = target
        self.status = target['status']
        self.actions = []
        self.submissions = []
        self.lose_response = lose_response

    def observe(self, item):
        return {'target': {**self.target, 'status': self.status}, 'actions': self.actions}

    def verify_pending(self, item, payload):
        if self.actions or self.status != 'reviewing':
            raise AssertionError('Fixture expected a fresh pending vote')

    def submit(self, item, payload):
        from task_remote import RemoteUnknown

        self.submissions.append(payload['result'])
        self.actions.append({'id': '101', 'action': 'reviewed', 'actor': payload['account'],
                             'extra': payload['result'], 'comment': payload['comment']})
        if payload['result'] == 'clarify':
            self.status = 'draft'
        if self.lose_response:
            raise RemoteUnknown('Response lost')


class NativeReviewFormTests(TestCase):
    def test_real_http_client_posts_pass_and_clarify_to_native_form(self):
        from story_review_remote import StoryReviewRemote
        from story_review_sync import review_result

        self.forms = []
        story = {'id': 2, 'version': 2, 'type': 'story', 'status': 'reviewing', 'deleted': '0',
                 'executions': {'1': {'project': '1'}}, 'twins': '', 'reviewedBy': ''}
        round_action = {'id': '100', 'objectType': 'story', 'objectID': '2', 'action': 'submitreview'}
        fields = {key: {'default': value} for key, value in (
            ('reviewedDate', '2026-11-23 09:00:00'), ('assignedTo', 'Test-user'),
            ('pri', '3'), ('estimate', '0'), ('status', 'reviewing'))}
        fields['result'] = {'options': {'pass': '通过', 'clarify': '有待明确'}}
        forms = self.forms

        class Handler(BaseHTTPRequestHandler):
            def respond(self, value, *, cookie=False):
                self.send_response(200)
                if cookie:
                    self.send_header('Set-Cookie', 'zentaosid=fixture; Path=/')
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())

            def log_message(self, *args):
                pass

            def do_GET(self):
                args = parse_qs(urlsplit(self.path).query)
                if self.headers.get('Cookie') != 'zentaosid=fixture' or args.get('storyID') != ['2']:
                    return self.respond({'status': 'fail'})
                last = forms[-1] if forms else None
                actions = {'100': round_action}
                if last:
                    actions['101'] = {**round_action, 'id': '101', 'action': 'reviewed', 'actor': 'Test-user',
                                      'extra': last['result'][0], 'comment': last['comment'][0]}
                data = {'story': story, 'actions': actions, 'fields': fields, 'reviewers': {'Test-user': ''}}
                self.respond({'status': 'success', 'data': json.dumps(data)})

            def do_POST(self):
                args = parse_qs(urlsplit(self.path).query)
                form = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
                if args.get('f') == ['login']:
                    return self.respond({'status': 'success', 'user': {'account': 'Test-user'}}, cookie=True)
                if (args.get('f') != ['review'] or args.get('from') != ['project']
                        or args.get('storyType') != ['story'] or self.headers.get('Cookie') != 'zentaosid=fixture'):
                    return self.respond({'status': 'fail'})
                forms.append(form)
                self.respond({'result': 'success'})

        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                config = {'zentao': {'base_url': f'http://127.0.0.1:{server.server_port}'}}
                item = {'item_id': '2', 'project_id': '1'}
                with patch('story_review_remote._credentials', return_value=('Test-user', 'fixture')):
                    remote = StoryReviewRemote(config)
                for result, progress in (('pass', 'waiting_votes'), ('clarify', 'clarified')):
                    with self.subTest(result=result):
                        payload = {**remote.observe(item)['target'], 'result': result,
                                   'vote_key': 'fixture/review', 'comment': '核对说明 [fixture/review]'}
                        remote.submit(item, payload)
                        self.assertEqual(forms[-1]['result'], [result])
                        self.assertEqual(forms[-1]['assignedTo'], ['Test-user'])
                        self.assertEqual(review_result(remote.observe(item), payload)['progress'], progress)
            finally:
                server.shutdown()
                thread.join()
