import concurrent.futures
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
try:
    from status_board import publish_status
    from workflow import run
except ModuleNotFoundError:
    run = None


class WorkflowFixture(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(run, 'The workflow command handler is not implemented')
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fixture = Path(temp.name)
        self.database = self.fixture / 'test.sqlite3'
        self.config = {
            'zentao': {'base_url': 'http://localhost:8088', 'project_name': 'Sample',
                       'assigned_to_accounts': ['Test-user'],
                       'accepted_statuses': {'story': ['active'], 'bug': ['active']}},
            'monitor': {'enabled': True},
            'development': {'max_concurrent_items': 1, 'max_retries_per_step': 3,
                            'step_timeout_minutes': dict.fromkeys(
                                ('material_check', 'design', 'plan', 'coding', 'unit_test',
                                 'code_review', 'e2e'), 1)},
            'runtime': {'desktop_project_id': 'desktop-1', 'dispatch_lease_seconds': 30,
                        'writeback_lease_seconds': 30},
        }
        self.time = datetime(2026, 11, 18, 2, tzinfo=timezone.utc)
        self.next_event = 0

    def item(self, item_id, kind='story'):
        return {'instance': 'http://localhost:8088', 'project_id': '1', 'kind': kind, 'id': str(item_id)}

    def row(self, item_id, priority=2, user='Test-user', kind='story'):
        return {'kind': kind, 'id': str(item_id), 'title': f'Fixture {item_id}',
                'assigned_to': user, 'status': 'active', 'priority': priority}

    def scan(self, scan_id, items, complete=True, since=0):
        at = self.time + timedelta(seconds=since)
        result = run('claim', {'action': 'scan', 'scan_id': scan_id, 'observed_at': at.isoformat(),
                              'complete': complete, 'instance': 'http://localhost:8088',
                              'project_id': '1', 'project_name': 'Sample', 'items': items},
                     self.config, self.database, now=self.time)
        self.time += timedelta(seconds=1)
        return result

    def reserve(self, item_id, **overrides):
        payload = {'action': 'reserve', 'item': self.item(item_id), 'observed_at': self.time.isoformat(),
                   'assigned_to': 'Test-user', 'status': 'active', 'desktop_project_id': 'desktop-1'}
        return run('claim', {**payload, **overrides}, self.config, self.database, now=self.time)

    def record(self, action, item_id=2, **fields):
        self.next_event += 1
        payload = {'action': action, 'event_id': f'event-{self.next_event}', 'item': self.item(item_id)}
        return run('record', {**payload, **fields}, self.config, self.database, now=self.time,
                   config_directory=self.fixture)

    def start(self):
        self.scan('initial', [self.row(1)])
        self.scan('new', [self.row(1), self.row(2), self.row(3, 3)])
        request_id = self.reserve(2)['request_id']
        self.record('bind', request_id=request_id, chat_id='chat-2', desktop_project_id='desktop-1')
        self.record('start', request_id=request_id, chat_id='chat-2')
        return {'request_id': request_id, 'chat_id': 'chat-2'}

    def host_git(self, item_id=2):
        from development_git_runner import run_development_git
        config_file = self.fixture / 'host-project.json'
        config_file.write_text(json.dumps(self.config), encoding='utf-8')
        with closing(sqlite3.connect(self.database)) as conn:
            marker = conn.execute('SELECT marker FROM work_items WHERE item_id=?', (str(item_id),)).fetchone()[0]
        return run_development_git(marker, self.database, config_file, self.fixture, recover=True)


class WorkflowTests(WorkflowFixture):
    """Admitted legacy items continue to use their original configuration snapshot."""

    def reserve(self, item_id, **overrides):
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute('UPDATE work_items SET config_snapshot=? WHERE item_id=? AND config_snapshot IS NULL',
                         (json.dumps(self.config), str(item_id)))
            conn.commit()
        return super().reserve(item_id, **overrides)

    def test_baseline_stale_partial_and_case_sensitive_queue(self):
        self.assertEqual(self.scan('initial', [self.row(1)])['status'], 'baseline')
        self.assertIsNone(self.scan('initial', [self.row(9)])['candidate'])
        self.assertEqual(self.scan('partial', [self.row(8)], complete=False)['status'], 'partial')
        result = self.scan('new', [self.row(1, 1), self.row(2, 3), self.row(3, 1),
                                   self.row(4, 1, 'test-user')])
        self.assertEqual(result['candidate'], self.item(3))
        self.assertEqual(self.scan('stale', [], since=-5)['status'], 'stale')
        self.assertEqual(self.reserve(2)['status'], 'queue_changed')
        self.assertEqual(self.reserve(3, assigned_to='Other')['status'], 'not_eligible')
        self.assertEqual(self.reserve(2)['status'], 'reserved')

    def test_concurrent_reserve_has_one_winner(self):
        self.scan('initial', [])
        self.scan('new', [self.row(2), self.row(3)])
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.reserve, (2, 2)))
        self.assertCountEqual([result['status'] for result in results], ['busy', 'reserved'])

    def test_reserve_validates_current_project_scope(self):
        self.scan('initial', [])
        self.scan('new', [self.row(2)])
        self.config['zentao']['project_name'] = 'Another project'
        self.assertEqual(self.reserve(2)['status'], 'scope_mismatch')

    def test_uncertain_dispatch_requires_explicit_absence_evidence(self):
        self.scan('initial', [])
        self.scan('new', [self.row(2)])
        request_id = self.reserve(2)['request_id']
        self.record('defer_dispatch', request_id=request_id)
        self.assertEqual(self.record('resolve_dispatch', request_id=request_id,
                                     not_created=True)['status'], 'reconciliation_needed')
        self.assertEqual(self.record('resolve_dispatch', request_id=request_id, not_created=True,
                                     evidence={'desktop': 'creation rejected'})['status'], 'dispatch_resolved')

    def test_uncertain_dispatch_releases_slot_finds_original_chat_and_rejects_expired_start(self):
        self.scan('initial', [])
        self.scan('new', [self.row(2), self.row(3)])
        first = self.reserve(2)
        self.record('defer_dispatch', request_id=first['request_id'])
        second = self.reserve(3)
        self.assertEqual(second['status'], 'reserved')
        self.record('bind', request_id=first['request_id'], chat_id='chat-2', desktop_project_id='desktop-1')
        self.time += timedelta(seconds=31)
        self.record('expire_dispatch', item_id=3, request_id=second['request_id'])
        renewed = self.reserve(2)
        self.assertEqual(renewed['chat_id'], 'chat-2')
        self.assertEqual(renewed['marker'], first['marker'])
        self.assertNotEqual(renewed['request_id'], first['request_id'])
        self.assertEqual(self.record('start', request_id=first['request_id'], chat_id='chat-2')['status'],
                         'stale_dispatch')
        self.assertEqual(self.record('start', request_id=renewed['request_id'], chat_id='chat-2')['status'],
                         'started')

    def test_reassigned_bound_item_cannot_start_and_releases_the_slot(self):
        self.scan('initial', [])
        self.scan('new', [self.row(2), self.row(3)])
        request = self.reserve(2)
        self.record('bind', request_id=request['request_id'], chat_id='chat-2', desktop_project_id='desktop-1')
        self.scan('reassigned', [self.row(2, user='Other'), self.row(3)])
        self.assertEqual(self.record('start', request_id=request['request_id'], chat_id='chat-2')['status'],
                         'not_eligible')
        self.assertEqual(self.reserve(3)['status'], 'reserved')

    def test_unattended_dispatch_requires_effective_policy_and_can_resume_original_chat(self):
        self.config['runtime']['require_unattended_approval'] = True
        chat_id = '01a1f1df-ddb1-77f1-aa07-a96f2a74d43a'
        sessions = self.fixture / 'sessions' / '2026' / '11' / '18'
        sessions.mkdir(parents=True)
        transcript = sessions / f'rollout-2026-11-18T10-00-00-{chat_id}.jsonl'
        self.scan('initial', [])
        self.scan('new', [self.row(2)])
        first = self.reserve(2)
        self.record('bind', request_id=first['request_id'], chat_id=chat_id, desktop_project_id='desktop-1')

        with patch('desktop_runtime.os', SimpleNamespace(environ={'CODEX_HOME': str(self.fixture)})):
            self.assertEqual(self.record('runtime_check', chat_id=chat_id)['status'], 'runtime_unverified')
            with transcript.open('w', encoding='utf-8') as log:
                for policy in ('never', 'on-request'):
                    log.write(json.dumps({'type': 'turn_context',
                                          'payload': {'approval_policy': policy}}) + '\n')
            self.assertEqual(self.record('runtime_check', chat_id=chat_id)['status'], 'runtime_requires_approval')
            denied = self.record('defer_dispatch', request_id=first['request_id'], reason='approval_policy')
            self.assertEqual(denied['status'], 'dispatch_runtime_blocked')
            publish_status(self.database, self.config)
            self.assertIn('待核验无人值守权限', (self.fixture / 'status.html').read_text(encoding='utf-8'))
            second = self.reserve(2)
            self.assertEqual(second['chat_id'], chat_id)
            self.assertEqual(self.record('start', request_id=second['request_id'], chat_id=chat_id)['status'],
                             'dispatch_runtime_blocked')
            self.assertEqual(self.record('start', request_id=second['request_id'], chat_id=chat_id)['status'],
                             'stale_dispatch')

            with transcript.open('a', encoding='utf-8') as log:
                log.write(json.dumps({'type': 'turn_context',
                                      'payload': {'approval_policy': 'never'}}) + '\n')
            third = self.reserve(2)
            self.assertEqual(third['chat_id'], chat_id)
            self.assertEqual(self.record('runtime_check', chat_id=chat_id)['status'], 'runtime_unattended')
            self.assertEqual(self.record('defer_dispatch', request_id=third['request_id'],
                                         reason='approval_policy')['status'], 'cannot_defer')
            self.assertEqual(self.record('start', request_id=third['request_id'], chat_id=chat_id)['status'],
                             'started')

    def test_finish_and_outbox_are_atomic_and_stale_attempt_cannot_advance(self):
        context = self.start()
        attempt = self.record('attempt_start', stage='material_check', **context)
        event = {'event_id': 'stable-attempt-result', 'attempt_id': attempt['attempt_id'], 'outcome': 'success',
                 'summary': 'Read fixed intent', 'evidence': {'path': 'intent.md'}}
        first = self.record('attempt_finish', **event, **context)
        repeat = self.record('attempt_finish', **event, **context)
        self.assertEqual(first, repeat)
        self.assertEqual(first['next_stage'], 'design')
        stale = self.record('attempt_finish', **{**event, 'event_id': 'late'}, **context)
        self.assertEqual(stale['status'], 'stale_attempt')
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM writebacks').fetchone()[0], 1)

    def test_voluntary_stop_preserves_finished_steps_and_releases_slot(self):
        context = self.start()
        active = self.record('attempt_start', stage='material_check', **context)
        evidence = {'chat_status': 'idle', 'owned_processes_active_after_stop': 0}
        self.assertEqual(self.record('stop', stop_evidence=evidence, **context)['status'], 'attempt_active')
        self.record('attempt_finish', attempt_id=active['attempt_id'], outcome='success',
                    summary='Inspected materials', evidence={'path': 'intent.md'}, **context)
        self.assertEqual(self.record('stop', **context)['status'], 'stop_unverified')
        result = self.record('stop', stop_evidence=evidence, **context)
        self.assertEqual(result['conclusion'], '主动停跑')
        self.assertEqual(result['steps'][0]['outcome'], 'success')
        self.assertTrue(all(step['outcome'] == 'skipped' for step in result['steps'][1:]))
        self.assertEqual(self.record('recover', recovery_id='probe', evidence=evidence)['status'], 'not_recoverable')
        self.assertEqual(self.reserve(3)['status'], 'reserved')

    def test_replaying_a_step_start_never_grants_a_second_launch(self):
        context = self.start()
        data = {**context, 'stage': 'material_check', 'event_id': 'launch-step'}
        first = self.record('attempt_start', **data)
        replay = self.record('attempt_start', **data)
        self.assertEqual(first['status'], 'attempt_started')
        self.assertEqual(replay['status'], 'attempt_active')
        self.assertEqual(replay['attempt_id'], first['attempt_id'])
        self.assertTrue(replay['replayed'])

    def pass_steps(self, context, stages):
        for stage in stages:
            attempt = self.record('attempt_start', stage=stage, **context)
            self.record('attempt_finish', attempt_id=attempt['attempt_id'], outcome='success',
                        summary='Verified fixture', evidence={'fixture': stage}, **context)

    def start_branch_check(self, applications):
        self.config['workspaces'] = {'frontend': 'fixture'}
        self.config['development']['step_timeout_minutes']['branch_check'] = 15
        self.config['development']['frontend_bundle'] = {
            'name': 'test-bundle', 'applications': {
                'Example': {'branch': 'release', 'repository': 'children/example'},
                'Unused': {'branch': 'unused', 'repository': 'children/unused'},
            },
        }
        context = self.start()
        self.pass_steps(context, ('material_check', 'design'))
        plan = self.record('attempt_start', stage='plan', **context)
        finish = {'attempt_id': plan['attempt_id'], 'outcome': 'success', 'summary': 'Plan ready', **context}
        self.assertEqual(self.record('attempt_finish', **finish)['status'], 'applications_required')
        result = self.record('attempt_finish', evidence={'applications': applications}, **finish)
        self.assertEqual(result['next_stage'], 'branch_check')
        self.assertEqual(self.record('attempt_start', stage='coding', **context)['status'], 'stage_mismatch')
        return context, self.record('attempt_start', stage='branch_check', **context)

    def test_branch_check_requires_evidence_for_each_planned_app_from_snapshot(self):
        context, attempt = self.start_branch_check(['Example'])
        self.assertEqual(attempt['branch_targets'], {'Example': {'branch': 'release', 'repository': 'children/example'}})
        self.config['development']['frontend_bundle']['applications']['Example']['branch'] = 'another'
        finish = {'attempt_id': attempt['attempt_id'], 'outcome': 'success', 'summary': 'Workbench ready', **context}
        self.assertEqual(self.record('attempt_finish', **finish)['status'], 'branch_evidence_required')
        source = self.fixture / 'fixture' / 'children' / 'example'
        source.mkdir(parents=True)
        worktree = self.fixture / 'worktree'
        evidence = {'repositories': {'Example': {'target_branch': 'wrong', 'source_ref': 'refs/heads/wrong',
                    'source_root': str(source), 'base_commit': 'a' * 40, 'worktree': str(worktree),
                    'worktree_branch': attempt['worktree_branch']}}}
        self.assertEqual(self.record('attempt_finish', evidence=evidence, **finish)['status'], 'branch_evidence_required')
        evidence['repositories']['Example'].update(target_branch='release', source_ref='refs/heads/release')
        self.assertEqual(self.record('attempt_finish', evidence=evidence, **finish)['status'],
                         'branch_worktree_unverified')

        def git(path, *args):
            return subprocess.run(['git', '-C', str(path), '-c', 'user.name=Fixture',
                                   '-c', 'user.email=fixture@example.invalid', '-c', 'commit.gpgSign=false',
                                   *args], check=True, text=True, capture_output=True).stdout.strip()

        inspect = {'attempt_id': attempt['attempt_id'], **context}
        git(source, 'init', '-b', 'wrong')
        tracked = source / 'tracked.txt'
        tracked.write_text('release baseline', encoding='utf-8')
        git(source, 'add', 'tracked.txt')
        git(source, 'commit', '-m', 'Correct base')
        base = git(source, 'rev-parse', 'HEAD')
        self.assertEqual(self.record('branch_inspect', **inspect)['repositories']['Example']['status'],
                         'target_ref_missing')
        git(source, 'update-ref', 'refs/remotes/origin/release', base)
        git(source, 'commit', '--allow-empty', '-m', 'Unrelated base')
        tracked.write_text('dirty state from user', encoding='utf-8')
        (source / 'existing-edit.txt').write_text('leave this intact', encoding='utf-8')
        resolved = self.record('branch_inspect', **inspect)['repositories']['Example']
        self.assertEqual((resolved['target_branch'], resolved['base_commit'], resolved['current_branch']),
                         ('release', base, 'wrong'))
        self.assertEqual(resolved['source_ref'], 'refs/remotes/origin/release')
        git(source, 'worktree', 'add', '--detach', str(worktree), resolved['base_commit'])
        detached = self.record('branch_inspect', worktrees={'Example': str(worktree)}, **inspect)
        self.assertEqual(detached['repositories']['Example']['status'], 'worktree_mismatch')
        git(worktree, 'switch', '-c', attempt['worktree_branch'])
        verified = self.record('branch_inspect', worktrees={'Example': str(worktree)}, **inspect)
        self.assertEqual(verified['repositories']['Example']['status'], 'verified')
        result = self.record('attempt_finish', evidence={'repositories': verified['repositories']}, **finish)
        self.assertEqual(result['next_stage'], 'coding')
        self.assertEqual(git(source, 'branch', '--show-current'), 'wrong')
        self.assertEqual(git(source, 'status', '--porcelain').splitlines(),
                         ['M tracked.txt', '?? existing-edit.txt'])
        self.assertEqual((worktree / 'tracked.txt').read_text(encoding='utf-8'), 'release baseline')

    def test_host_worktree_preparation_recovers_same_checkout_without_switching_source(self):
        self.time = datetime.now(timezone.utc)
        context, attempt = self.start_branch_check(['Example'])
        repo = self.fixture / 'fixture' / 'children' / 'example'
        repo.mkdir(parents=True)
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), '-c', 'user.name=Fixture',
                                            '-c', 'user.email=fixture@example.invalid', '-c', 'commit.gpgSign=false',
                                            *args], text=True, stderr=subprocess.DEVNULL).strip()
        git('init', '-b', 'wrong')
        git('commit', '--allow-empty', '-m', 'Target base')
        base = git('rev-parse', 'HEAD')
        git('branch', 'release')
        git('commit', '--allow-empty', '-m', 'Another branch')
        args = dict(context, attempt_id=attempt['attempt_id'], application='Example', event_id='host-prepare-fixture')
        with patch('workflow.prepare_local_worktree') as sandbox_mutation:
            requested = self.record('branch_prepare', **args)
        self.assertEqual(requested['status'], 'host_git_worker_required')
        sandbox_mutation.assert_not_called()
        self.assertFalse((self.database.parent / 'worktrees').exists())
        first = self.host_git()
        self.assertEqual(first['status'], 'host_git_completed', first)
        self.assertEqual(first['repositories']['Example']['base_commit'], base)
        again = self.record('branch_prepare', **args)
        self.assertEqual(again, first)
        self.assertEqual(git('branch', '--show-current'), 'wrong')

    def test_branch_blocker_advances_and_releases_queue_only_after_integration(self):
        context, attempt = self.start_branch_check(['Missing'])
        self.assertEqual(attempt['branch_targets'], {'Missing': None})
        inspect = self.record('branch_inspect', attempt_id=attempt['attempt_id'], **context)
        self.assertEqual(inspect['repositories']['Missing']['status'], 'mapping_missing')
        result = self.record('attempt_finish', attempt_id=attempt['attempt_id'], outcome='failed', retryable=False,
                             summary='Missing target mapping for Missing', evidence={'application': 'Missing'}, **context)
        self.assertEqual((result['disposition'], result['next_stage']), ('next', 'coding'))
        self.assertEqual(self.record('complete', **context)['status'], 'stale_execution')
        self.pass_steps(context, ('coding', 'unit_test', 'code_review', 'e2e', 'integration'))
        blockers = self.fixture / 'blockers.md'
        blockers.write_text('Missing target mapping for Missing', encoding='utf-8')
        final = self.record('complete', blockers_path=str(blockers), **context)
        self.assertEqual([step['outcome'] for step in final['steps']], ['success'] * 3 + ['failed'] + ['success'] * 5)
        next_item = self.reserve(3)
        self.assertEqual(next_item['status'], 'reserved')
        self.record('bind', item_id=3, request_id=next_item['request_id'],
                    chat_id='chat-3', desktop_project_id='desktop-1')
        self.assertEqual(self.record('start', item_id=3, request_id=next_item['request_id'],
                                     chat_id='chat-3')['stage'], 'material_check')
        with closing(sqlite3.connect(self.database)) as conn:
            operations = conn.execute('SELECT operation_id FROM writebacks').fetchall()
        self.assertEqual(len(operations), 11)
        for (operation_id,) in operations:
            write = self.record('writeback_claim', operation_id=operation_id, owner='simulated-zen')
            permission = self.record('writeback_send', operation_id=operation_id, lease_id=write['lease_id'])
            self.assertEqual(permission['status'], 'send_authorized')
            confirmed = self.record('writeback_confirm', operation_id=operation_id, lease_id=write['lease_id'],
                                    receipt={'fixture_id': operation_id})
            self.assertEqual(confirmed['status'], 'writeback_confirmed')

    def test_branch_retry_stops_after_three_retries_and_keeps_plan(self):
        context, attempt = self.start_branch_check(['Example'])
        for retry in range(4):
            self.assertEqual(attempt['retry_number'], retry)
            finish = self.record('attempt_finish', attempt_id=attempt['attempt_id'], outcome='failed',
                                 summary='Temporary remote failure', **context)
            self.assertEqual(finish['disposition'], 'retry' if retry < 3 else 'next')
            if retry < 3:
                attempt = self.record('attempt_start', stage='branch_check', **context)
                self.assertEqual(attempt['branch_targets']['Example']['branch'], 'release')
        self.pass_steps(context, ('coding', 'unit_test', 'code_review', 'e2e', 'integration'))
        final = self.record('complete', blockers_path='blockers.md', **context)
        self.assertEqual(final['steps'][3]['retries'], 3)
        self.assertEqual(self.reserve(3)['status'], 'reserved')

    def test_review_repair_keeps_one_retry_and_uses_separate_deadlines(self):
        self.config['development']['step_timeout_minutes'].update(coding=120, unit_test=45, code_review=30)
        context = self.start()
        self.pass_steps(context, ('material_check', 'design', 'plan', 'coding', 'unit_test'))
        first = self.record('attempt_start', stage='code_review', **context)
        self.record('attempt_finish', attempt_id=first['attempt_id'], outcome='failed',
                    summary='Core regression', repair_required=True, **context)
        retry = self.record('attempt_start', stage='code_review', **context)
        self.assertEqual((retry['phase'], retry['retry_number']), ('repair_coding', 1))
        self.assertEqual(datetime.fromisoformat(retry['deadline_at']), self.time + timedelta(minutes=120))
        events = {**context, 'attempt_id': retry['attempt_id'], 'summary': 'Verified repair phase',
                  'evidence': {'revision': 'fixed'}}
        self.time += timedelta(minutes=46)
        units = self.record('repair_advance', phase='repair_coding', **events)
        self.assertEqual(units['phase'], 'repair_unit_test')
        self.assertEqual(datetime.fromisoformat(units['deadline_at']), self.time + timedelta(minutes=45))
        self.assertEqual(self.record('attempt_finish', phase='repair_coding', outcome='success',
                                     **events)['status'], 'phase_mismatch')
        review = self.record('repair_advance', phase='repair_unit_test', **events)
        self.assertEqual(review['phase'], 'verify')
        self.assertEqual(datetime.fromisoformat(review['deadline_at']), self.time + timedelta(minutes=30))
        result = self.record('attempt_finish', phase='verify', outcome='success', **events)
        self.assertEqual((result['attempts_used'], result['next_stage']), (2, 'e2e'))

    def test_e2e_repair_failure_consumes_retry_and_requires_review_before_retest(self):
        context = self.start()
        self.pass_steps(context, ('material_check', 'design', 'plan', 'coding', 'unit_test', 'code_review'))
        first = self.record('attempt_start', stage='e2e', **context)
        self.record('attempt_finish', attempt_id=first['attempt_id'], outcome='failed',
                    summary='Core acceptance failed', repair_required=True, **context)
        retry = self.record('attempt_start', stage='e2e', **context)
        self.record('attempt_finish', attempt_id=retry['attempt_id'], phase='repair_coding',
                    outcome='failed', summary='Repair did not compile', **context)
        retry = self.record('attempt_start', stage='e2e', **context)
        self.assertEqual((retry['phase'], retry['retry_number']), ('repair_coding', 2))
        events = {**context, 'attempt_id': retry['attempt_id'], 'summary': 'Verified repair phase',
                  'evidence': {'revision': 'fixed'}}
        for previous, expected in (('repair_coding', 'repair_unit_test'), ('repair_unit_test', 'repair_review'),
                                   ('repair_review', 'verify')):
            self.assertEqual(self.record('repair_advance', phase=previous, **events)['phase'], expected)
        self.assertEqual(self.record('attempt_finish', phase='verify', outcome='success',
                                     **events)['next_stage'], 'integration')

    def test_each_blocked_stage_reaches_the_next_and_keeps_its_own_outcome(self):
        self.config['workspaces'] = {'frontend': 'fixture'}
        self.config['development']['frontend_bundle'] = {'name': 'example', 'applications': {}}
        self.config['development']['step_timeout_minutes']['branch_check'] = 15
        context = self.start()
        stages = ('material_check', 'design', 'plan', 'branch_check', 'coding', 'unit_test',
                  'code_review', 'e2e', 'integration')
        for index, stage in enumerate(stages):
            with self.subTest(stage=stage):
                trial = self.record('attempt_start', stage=stage, **context)
                if stage == 'branch_check':
                    self.assertEqual(trial['branch_targets'], {})
                    empty = self.record('attempt_finish', attempt_id=trial['attempt_id'], outcome='success',
                                        summary='Empty targets', evidence={'repositories': {}}, **context)
                    self.assertEqual(empty['status'], 'branch_evidence_required')
                result = self.record('attempt_finish', attempt_id=trial['attempt_id'], outcome='failed',
                                     retryable=False, summary=f'Blocked at {stage}', **context)
                self.assertEqual(result['next_stage'], stages[index + 1] if index + 1 < len(stages) else None)
        final = self.record('complete', blockers_path='blockers.md', **context)
        self.assertEqual(final['conclusion'], '阻断')
        self.assertEqual([step['summary'] for step in final['steps']], [f'Blocked at {s}' for s in stages])

    def test_e2e_environment_failure_retries_without_code_repair(self):
        context = self.start()
        self.pass_steps(context, ('material_check', 'design', 'plan', 'coding', 'unit_test', 'code_review'))
        first = self.record('attempt_start', stage='e2e', **context)
        self.record('attempt_finish', attempt_id=first['attempt_id'], outcome='failed',
                    summary='Login service offline', **context)
        self.assertEqual(self.record('attempt_start', stage='e2e', **context)['phase'], 'verify')

    def test_exhausted_review_and_unavailable_e2e_still_reach_integration(self):
        context = self.start()
        self.pass_steps(context, ('material_check', 'design', 'plan', 'coding', 'unit_test'))
        for index in range(4):
            trial = self.record('attempt_start', stage='code_review', **context)
            finished = self.record('attempt_finish', attempt_id=trial['attempt_id'], outcome='failed',
                                   summary='Unresolved review findings', **context)
            self.assertEqual(finished['next_stage'], 'code_review' if index < 3 else 'e2e')
        e2e = self.record('attempt_start', stage='e2e', **context)
        finished = self.record('attempt_finish', attempt_id=e2e['attempt_id'], outcome='failed',
                               retryable=False, completion='待验证', summary='Login unavailable', **context)
        self.assertEqual(finished['next_stage'], 'integration')
        self.pass_steps(context, ('integration',))
        final = self.record('complete', blockers_path='blockers.md', **context)
        self.assertEqual(final['conclusion'], '阻断')
        self.assertEqual([step['outcome'] for step in final['steps'][-3:]], ['failed', 'failed', 'success'])

    def test_recovery_keeps_unresolved_repair_requirement_in_the_original_chat(self):
        context = self.start()
        self.pass_steps(context, ('material_check', 'design', 'plan', 'coding', 'unit_test'))
        attempt = self.record('attempt_start', stage='code_review', **context)
        self.record('attempt_finish', attempt_id=attempt['attempt_id'], outcome='failed', retryable=False,
                    summary='Repair environment unavailable', repair_required=True, **context)
        self.pass_steps(context, ('e2e', 'integration'))
        self.record('complete', **context, blockers_path='blockers.md')
        self.record('recover', recovery_id='env-restored', evidence={'healthcheck': 'passed'})
        renewed = self.reserve(2)
        self.assertEqual(renewed['chat_id'], 'chat-2')
        context['request_id'] = renewed['request_id']
        self.record('start', **context)
        resumed = self.record('attempt_start', stage='code_review', **context)
        self.assertEqual((resumed['phase'], resumed['retry_number']), ('repair_coding', 0))

    def test_integration_and_closeout_remain_bounded_and_resumable(self):
        context = self.start()
        self.time += timedelta(days=1)
        self.pass_steps(context, ('material_check', 'design', 'plan', 'coding', 'unit_test', 'code_review', 'e2e'))
        integration = self.record('attempt_start', stage='integration', **context)
        self.assertEqual(datetime.fromisoformat(integration['deadline_at']), self.time + timedelta(minutes=45))
        self.record('attempt_finish', attempt_id=integration['attempt_id'],
                    outcome='success', summary='Merged inspected revision', **context)
        self.time += timedelta(days=1)
        self.assertEqual(self.record('complete', **context)['conclusion'], '已交付')
        self.assertEqual(self.reserve(3)['status'], 'reserved')

    def test_successful_closeout_writes_stage_receipts_and_releases_slot(self):
        context = self.start()
        stages = ('material_check', 'design', 'plan', 'coding', 'unit_test', 'code_review', 'e2e', 'integration')
        self.pass_steps(context, stages)
        completed = self.record('complete', **context)
        self.assertEqual(completed['conclusion'], '已交付')
        self.assertEqual([step['stage'] for step in completed['steps']], list(stages))
        self.assertTrue(all(step['outcome'] == 'success' for step in completed['steps']))
        self.assertIsNone(completed['attachment_id'])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM writebacks').fetchone()[0], 9)
        self.assertEqual(self.reserve(3)['status'], 'reserved')

    def test_missing_requirement_file_closes_immediately_and_releases_slot(self):
        context = self.start()
        trial = self.record('attempt_start', stage='material_check', **context)
        finish = self.record('attempt_finish', attempt_id=trial['attempt_id'], outcome='failed',
                             summary='Missing intent.md', evidence={'material_file_status': 'missing'}, **context)
        self.assertEqual((finish['disposition'], finish['next_stage']), ('blocked', None))
        result = self.record('complete', **context, blockers_path='blockers.md')
        self.assertEqual(result['conclusion'], '待补材料')
        self.assertEqual([step['outcome'] for step in result['steps']], ['failed'] + ['skipped'] * 7)
        self.assertIsNotNone(result['attachment_id'])
        self.assertEqual(self.reserve(3)['status'], 'reserved')

    def test_unreadable_material_repository_does_not_close_the_item(self):
        context = self.start()
        trial = self.record('attempt_start', stage='material_check', **context)
        event = dict(outcome='failed', summary='Repository unavailable', completion='待补材料',
                     evidence={'material_file_status': 'unknown'}, **context)
        first = self.record('attempt_finish', attempt_id=trial['attempt_id'], **event)
        self.assertEqual(first['disposition'], 'retry')
        retry = self.record('attempt_start', stage='material_check', **context)
        final = self.record('attempt_finish', attempt_id=retry['attempt_id'], retryable=False, **event)
        self.assertEqual(final['next_stage'], 'design')
        design = self.record('attempt_start', stage='design', **context)
        result = self.record('attempt_finish', attempt_id=design['attempt_id'], retryable=False, outcome='failed',
                             summary='Missing file at design', evidence={'material_file_status': 'missing'}, **context)
        self.assertEqual(result['next_stage'], 'plan')

    def test_missing_file_on_bug_does_not_trigger_requirement_closeout(self):
        self.scan('initial', [])
        self.scan('new', [self.row(2, kind='bug')])
        item = self.item(2, kind='bug')
        lease = self.reserve(2, item=item)
        context = {'item': item, 'request_id': lease['request_id'], 'chat_id': 'bug-chat'}
        self.record('bind', desktop_project_id='desktop-1', **context)
        self.record('start', **context)
        trial = self.record('attempt_start', stage='material_check', **context)
        result = self.record('attempt_finish', attempt_id=trial['attempt_id'], retryable=False, outcome='failed',
                             summary='No attachment', evidence={'material_file_status': 'missing'}, **context)
        self.assertEqual(result['next_stage'], 'design')

    def test_timeout_requires_stop_proof_and_three_retries_then_advances(self):
        context = self.start()
        for index in range(4):
            trial = self.record('attempt_start', stage='material_check', **context)
            self.assertEqual(trial['retry_number'], index)
            self.time += timedelta(seconds=61)
            event = {'attempt_id': trial['attempt_id'], 'outcome': 'timed_out', 'summary': 'Deadline',
                     'evidence': {}, **context}
            self.assertEqual(self.record('attempt_finish', **event)['status'], 'stop_unverified')
            stopped = self.record('attempt_finish', **event, stop_confirmed=True,
                                  stop_evidence={'process_exit': True})
            self.assertEqual(stopped['disposition'], 'retry' if index < 3 else 'next')
        self.pass_steps(context, ('design', 'plan', 'coding', 'unit_test', 'code_review', 'e2e', 'integration'))
        end = self.record('complete', **context, blockers_path='blockers.md', blockers_location='team/2')
        self.assertEqual(end['conclusion'], '阻断')
        self.assertEqual([row['outcome'] for row in end['steps']], ['failed'] + ['success'] * 7)
        self.assertEqual(self.reserve(3)['status'], 'reserved')
        self.assertEqual(self.record('recover', recovery_id='materials-rev2', evidence={'commit': 'changed'})['status'],
                         'recovered')
        self.assertEqual(self.record('recover', recovery_id='materials-rev2', evidence={'commit': 'changed'})['status'],
                         'recovery_seen')

    def test_writeback_expiry_requires_prior_outcome_reconciliation(self):
        context = self.start()
        check = self.record('attempt_start', stage='material_check', **context)
        result = self.record('attempt_finish', attempt_id=check['attempt_id'], outcome='success',
                             summary='Ready', evidence={}, **context)
        operation_id = result['operation_id']
        first = self.record('writeback_claim', operation_id=operation_id, owner='chat-2')
        self.assertFalse(first['reconcile_required'])
        self.assertEqual(self.record('writeback_claim', operation_id=operation_id, owner='monitor')['status'], 'leased')
        self.time += timedelta(seconds=31)
        again = self.record('writeback_claim', operation_id=operation_id, owner='monitor')
        self.assertTrue(again['reconcile_required'])
        self.assertEqual(self.record('writeback_confirm', operation_id=operation_id,
                                     lease_id=first['lease_id'], receipt={'id': 'external'})['status'], 'stale_lease')
        payload = {'operation_id': operation_id, 'lease_id': again['lease_id']}
        self.assertEqual(self.record('writeback_reconcile', **payload, prior_ended=False, missing=True)['status'],
                         'reconciliation_needed')
        self.assertEqual(self.record('writeback_reconcile', **payload, prior_ended=True, missing=True)['status'],
                         'ready_to_send')
        self.assertEqual(self.record('writeback_confirm', **payload, receipt={'id': 'external'})['status'],
                         'writeback_confirmed')


if __name__ == '__main__':
    unittest.main()
