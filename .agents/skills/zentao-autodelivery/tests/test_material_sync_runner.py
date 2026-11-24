from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

from test_workflow import WorkflowFixture
from test_material_sync import git
import material_sync_runner as runner
import worker
from workflow import compute_material_attempt, publish_material_result


class HostSyncRunnerTests(WorkflowFixture):
    def setUp(self):
        super().setUp()
        self.time = datetime.now(timezone.utc)
        self.config['runtime']['sync_requirement_before_review'] = True
        self.config['workspaces'] = {'requirement': 'checkout'}
        self.remote = self.fixture / 'remote'
        self.checkout = self.fixture / 'checkout'
        git(self.fixture, 'init', '-b', 'welllog/dev', str(self.remote))
        (self.remote / 'README.md').write_text('requirements')
        git(self.remote, 'add', '.')
        git(self.remote, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            '-c', 'commit.gpgsign=false', 'commit', '-m', 'initial')
        git(self.fixture, 'clone', str(self.remote), str(self.checkout))
        self.config_path = self.fixture / 'project.json'
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')

    def admit(self, start=True):
        if start:
            binding = self.start()
            attempt = self.record('attempt_start', **binding, stage='material_check')
            binding['attempt_id'] = attempt['attempt_id']
        else:
            self.scan('initial', [])
            self.scan('new', [self.row(2)])
            reserved = self.reserve(2)
            binding = {'request_id': reserved['request_id']}
        with closing(sqlite3.connect(self.database)) as conn:
            conn.row_factory = sqlite3.Row
            item = dict(conn.execute('SELECT * FROM work_items WHERE item_id=2').fetchone())
        return item, {'item': self.item(2), **binding}

    def counts(self):
        with closing(sqlite3.connect(self.database)) as conn:
            return (conn.execute('SELECT COUNT(*) FROM requirement_sync_receipts').fetchone()[0],
                    conn.execute('SELECT COUNT(*) FROM requirement_sync_results').fetchone()[0])

    def test_preflight_reads_actual_upstream_without_fetch_or_checkout_mutation(self):
        before = git(self.checkout, 'rev-parse', 'HEAD', '@{upstream}')
        result = runner.run_git_preflight(self.config_path, self.database, self.fixture)
        self.assertEqual(result['status'], 'git_preflight_passed')
        self.assertTrue(result['stop_confirmed'])
        self.assertEqual(result['stop_evidence']['owned_processes_active_after_stop'], 0)
        self.assertEqual(git(self.checkout, 'rev-parse', 'HEAD', '@{upstream}'), before)
        self.assertFalse((self.checkout / '.git' / 'FETCH_HEAD').exists())

    def test_preflight_failure_is_generic_and_does_not_disclose_remote_url(self):
        git(self.checkout, 'config', 'branch.welllog/dev.merge', 'refs/heads/missing')
        result = runner.compute_git_preflight(self.config, self.fixture)
        self.assertEqual(result['status'], 'git_preflight_failed')
        self.assertNotIn(str(self.remote), json.dumps(result))

    def test_prepare_failure_requeues_never_started_reservation_without_cli(self):
        item, _ = self.admit(start=False)
        failed = {'status': 'git_preflight_failed', 'stop_confirmed': True,
                  'stop_evidence': {'owned_processes_active_after_stop': 0}}
        with patch.object(worker, 'DATABASE_PATH', self.database), \
                patch.object(worker, 'CONFIG_PATH', self.config_path), \
                patch.object(worker, 'PROJECT_ROOT', self.fixture), \
                patch.object(worker, 'run_git_preflight', return_value=failed), \
                patch.object(worker, 'launch_turn') as launch:
            result = worker.run('prepare', item['marker'], Path('codex.exe'))
        self.assertEqual(result['reservation']['item_state'], 'queued')
        launch.assert_not_called()
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT state FROM dispatches').fetchone()[0], 'preflight_blocked')

    def test_initial_run_failure_requeues_and_preserves_bound_chat(self):
        item, data = self.admit(start=False)
        self.record('bind', request_id=data['request_id'], chat_id='chat-2', desktop_project_id='desktop-1')
        failed = {'status': 'git_preflight_failed', 'stop_confirmed': True,
                  'stop_evidence': {'owned_processes_active_after_stop': 0}}
        with patch.object(worker, 'DATABASE_PATH', self.database), \
                patch.object(worker, 'CONFIG_PATH', self.config_path), \
                patch.object(worker, 'PROJECT_ROOT', self.fixture), \
                patch.object(worker, 'run_git_preflight', return_value=failed), \
                patch.object(worker, 'launch_turn') as launch:
            result = worker.run('run', item['marker'], Path('codex.exe'))
        self.assertEqual(result['reservation']['chat_id'], 'chat-2')
        self.assertEqual(result['reservation']['item_state'], 'queued')
        launch.assert_not_called()

    def test_live_or_uncertain_preflight_cannot_release_reservation(self):
        item, _ = self.admit(start=False)
        result = runner.release_preflight_reservation(self.database, self.config, item,
                    {'status': 'git_preflight_failed', 'stop_confirmed': False})
        self.assertEqual(result['status'], 'preflight_reservation_retained')
        self.record('bind', request_id=item['request_id'], chat_id='chat-2', desktop_project_id='desktop-1')
        self.record('start', request_id=item['request_id'], chat_id='chat-2')
        result = runner.release_preflight_reservation(self.database, self.config, item,
                    {'status': 'git_preflight_failed', 'stop_confirmed': True,
                     'stop_evidence': {'owned_processes_active_after_stop': 0}})
        self.assertEqual(result['status'], 'preflight_reservation_retained')

    def test_foreign_request_and_changed_checkout_cannot_sync(self):
        item, data = self.admit()
        self.assertEqual(self.record('material_sync', **{k: v for k, v in data.items() if k != 'item'},
                         )['status'], 'material_sync_worker_required')
        wrong = {**data, 'request_id': 'foreign'}
        self.assertEqual(compute_material_attempt(self.database, self.config, wrong, self.fixture)['status'],
                         'material_sync_inactive')
        changed = {**self.config, 'workspaces': {'requirement': 'remote'}}
        self.assertEqual(compute_material_attempt(self.database, changed, data, self.fixture)['status'],
                         'material_sync_failed')
        self.assertEqual(self.counts(), (0, 0))

    def test_expired_attempt_and_checkout_lock_do_not_start_host_process(self):
        item, data = self.admit()
        with runner.checkout_lock(self.database.parent / 'requirement-locks', self.checkout):
            with patch.object(runner, 'run_stage') as stage:
                self.assertEqual(runner.run_material_sync(item['marker'], self.database,
                    self.config_path, self.fixture)['status'], 'material_sync_busy')
                stage.assert_not_called()
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute('UPDATE attempts SET deadline_at=?',
                         ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
            conn.commit()
        with patch.object(runner, 'run_stage') as stage:
            self.assertEqual(runner.run_material_sync(item['marker'], self.database,
                self.config_path, self.fixture)['status'], 'material_sync_inactive')
            stage.assert_not_called()

    def test_completed_computation_with_uncertain_stop_never_publishes_or_relaunches(self):
        item, data = self.admit()
        def stage(attempt, phase, deadline, cwd, root, *args, **kwargs):
            folder = root / attempt / phase
            folder.mkdir(parents=True)
            computed = compute_material_attempt(self.database, self.config, data, self.fixture)
            (folder / 'stdout.log').write_text(json.dumps(computed))
            proof = {'attempt_id': attempt, 'phase': phase, 'deadline_at': deadline.isoformat(),
                     'receipt_path': str(folder / 'result.json'), 'outcome': 'success',
                     'stop_confirmed': False, 'stop_evidence': {'owned_processes_active_after_stop': 1}}
            (folder / 'result.json').write_text(json.dumps(proof))
            return proof
        with patch.object(runner, 'run_stage', side_effect=stage) as launch:
            first = runner.run_material_sync(item['marker'], self.database, self.config_path, self.fixture)
            second = runner.run_material_sync(item['marker'], self.database, self.config_path, self.fixture, recover=True)
        self.assertEqual(first['status'], 'material_sync_stop_unverified')
        self.assertEqual(second['status'], 'material_sync_stop_unverified')
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(self.counts(), (0, 0))

    def test_crash_after_compute_recovers_only_after_named_job_stop(self):
        item, data = self.admit()
        pending = runner.pending_material_sync(item['marker'], self.database, self.config_path)
        deadline = datetime.fromisoformat(pending['deadline_at'])
        computed = compute_material_attempt(self.database, self.config, data, self.fixture)
        folder = self.database.parent / 'items' / item['marker'] / 'material-sync' / data['attempt_id'] / 'host-sync'
        folder.mkdir(parents=True)
        (folder / 'stdout.log').write_text(json.dumps(computed))
        (folder / 'launch.json').write_text(json.dumps({'attempt_id': data['attempt_id'], 'phase': 'host-sync',
            'deadline_at': deadline.isoformat(), 'receipt_path': str(folder / 'result.json')}))
        with patch.object(runner, 'recover_job_stop', return_value=None), patch.object(runner, 'run_stage') as launch:
            self.assertEqual(runner.run_material_sync(item['marker'], self.database, self.config_path,
                self.fixture, recover=True)['status'], 'material_sync_stop_unverified')
            launch.assert_not_called()
        with patch.object(runner, 'recover_job_stop', return_value={'owned_processes_active_after_stop': 0}), \
                patch.object(runner, 'run_stage') as launch:
            result = runner.run_material_sync(item['marker'], self.database, self.config_path, self.fixture, recover=True)
            launch.assert_not_called()
        self.assertEqual(result['status'], 'material_synced')
        self.assertEqual(self.counts(), (1, 1))

    def test_persisted_unverified_stop_can_be_reconciled_by_fresh_named_job_query(self):
        item, data = self.admit()
        pending = runner.pending_material_sync(item['marker'], self.database, self.config_path)
        deadline = datetime.fromisoformat(pending['deadline_at'])
        computed = compute_material_attempt(self.database, self.config, data, self.fixture)
        folder = self.database.parent / 'items' / item['marker'] / 'material-sync' / data['attempt_id'] / 'host-sync'
        folder.mkdir(parents=True)
        (folder / 'stdout.log').write_text(json.dumps(computed))
        launch = {'attempt_id': data['attempt_id'], 'phase': 'host-sync',
                  'deadline_at': deadline.isoformat(), 'receipt_path': str(folder / 'result.json')}
        (folder / 'launch.json').write_text(json.dumps(launch))
        prior = {**launch, 'outcome': 'success', 'stop_confirmed': False,
                 'stop_evidence': {'owned_processes_active_after_stop': 1}}
        (folder / 'result.json').write_text(json.dumps(prior))
        with patch.object(runner, 'recover_job_stop', return_value=None), patch.object(runner, 'run_stage') as stage:
            result = runner.run_material_sync(item['marker'], self.database, self.config_path, self.fixture, recover=True)
            self.assertEqual(result['status'], 'material_sync_stop_unverified')
            stage.assert_not_called()
        self.assertEqual(self.counts(), (0, 0))
        with patch.object(runner, 'recover_job_stop', return_value={'owned_processes_active_after_stop': 0}), \
                patch.object(runner, 'run_stage') as stage:
            result = runner.run_material_sync(item['marker'], self.database, self.config_path, self.fixture, recover=True)
            stage.assert_not_called()
        self.assertEqual(result['status'], 'material_synced')
        saved = json.loads((folder / 'result.json').read_text())
        self.assertTrue(saved['stop_confirmed'])
        self.assertEqual(saved['stop_evidence']['owned_processes_active_after_stop'], 0)
        self.assertEqual(saved['previous_stop_evidence']['owned_processes_active_after_stop'], 1)

    def test_disabled_material_sync_and_bug_skip_story_preflight(self):
        config = {**self.config, 'runtime': {}}
        self.config_path.write_text(json.dumps(config))
        with patch.object(runner, 'run_stage') as stage:
            self.assertEqual(runner.run_git_preflight(self.config_path, self.database,
                self.fixture)['status'], 'git_preflight_skipped')
            stage.assert_not_called()
        self.config_path.write_text(json.dumps(self.config))
        with patch.object(runner, 'run_stage') as stage:
            bug = {'kind': 'bug', 'config_snapshot': json.dumps(self.config)}
            self.assertEqual(runner.run_git_preflight(self.config_path, self.database,
                self.fixture, bug)['status'], 'git_preflight_skipped')
            stage.assert_not_called()
