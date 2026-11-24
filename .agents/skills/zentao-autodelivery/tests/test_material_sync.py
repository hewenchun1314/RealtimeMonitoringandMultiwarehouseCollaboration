from contextlib import closing
import sqlite3
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from test_workflow import WorkflowFixture
from worker import worker_roots


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True,
                          encoding='utf-8').stdout.strip()


class MaterialSyncTests(WorkflowFixture):
    def setUp(self):
        super().setUp()
        self.config['runtime']['sync_requirement_before_review'] = True
        self.config['workspaces'] = {'requirement': 'checkout'}
        self.remote = self.fixture / 'remote'
        self.checkout = self.fixture / 'checkout'
        git(self.fixture, 'init', '-b', 'welllog/dev', str(self.remote))
        (self.remote / 'README.md').write_text('requirements\n', encoding='utf-8')
        self.commit_remote('initial requirements')
        git(self.fixture, 'clone', str(self.remote), str(self.checkout))
        self.time = datetime.now(timezone.utc)

    def commit_remote(self, message):
        git(self.remote, 'add', '--all')
        git(self.remote, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            '-c', 'commit.gpgsign=false', 'commit', '-m', message)
        return git(self.remote, 'rev-parse', 'HEAD')

    def start_review(self):
        context = self.start()
        review = self.record('attempt_start', **context, stage='material_check')
        self.assertIs(review['material_sync_required'], True)
        return {**context, 'attempt_id': review['attempt_id']}

    def test_public_sync_requests_host_without_running_git(self):
        attempt = self.start_review()
        with patch('workflow.sync_requirement', return_value={'branch': 'welllog/dev',
                   'upstream': 'origin/welllog/dev', 'commit': 'a' * 40}) as sync:
            result = self.record('material_sync', **attempt)
        self.assertEqual(result['status'], 'material_sync_worker_required')
        sync.assert_not_called()

    def run_sync(self, attempt):
        from material_sync_runner import run_material_sync
        config_file = self.fixture / 'project.json'
        config_file.write_text(json.dumps(self.config), encoding='utf-8')
        with closing(sqlite3.connect(self.database)) as conn:
            marker = conn.execute('SELECT marker FROM work_items WHERE item_id=2').fetchone()[0]
        return run_material_sync(marker, self.database, config_file, self.fixture, recover=True)

    def test_story_review_requires_fresh_sync_and_reads_new_requirement(self):
        self.time = datetime.now(timezone.utc)
        intent = Path('requirements/REQ-001/intent.md')
        (self.remote / intent).parent.mkdir(parents=True)
        (self.remote / intent).write_text('new requirement\n', encoding='utf-8')
        latest = self.commit_remote('publish requirement')
        (self.checkout / 'README.md').write_text('local draft\n', encoding='utf-8')

        attempt = self.start_review()
        finish = {'outcome': 'success', 'summary': 'reviewed', 'evidence': {'path': intent.as_posix()}}
        self.assertEqual(self.record('attempt_finish', **attempt, **finish)['status'], 'material_sync_required')
        self.assertFalse((self.checkout / intent).exists())
        synced = self.run_sync(attempt)
        self.assertEqual((synced['status'], synced['commit']), ('material_synced', latest))
        self.assertEqual((self.checkout / intent).read_text(encoding='utf-8'), 'new requirement\n')
        self.assertEqual((self.checkout / 'README.md').read_text(encoding='utf-8'), 'local draft\n')
        self.assertEqual(self.record('attempt_finish', **attempt, **finish)['next_stage'], 'split')
        self.assertEqual(self.record('material_sync', **attempt)['status'], 'material_sync_inactive')

    def test_update_conflict_retries_without_claiming_material_is_missing(self):
        self.config['development']['max_retries_per_step'] = 1
        (self.remote / 'README.md').write_text('remote edit\n', encoding='utf-8')
        self.commit_remote('publish remote edit')
        (self.checkout / 'README.md').write_text('local draft\n', encoding='utf-8')
        attempt = self.start_review()
        old_head = git(self.checkout, 'rev-parse', 'HEAD')

        sync_failure = self.run_sync(attempt)
        self.assertEqual(sync_failure['status'], 'material_sync_failed')
        self.assertIn('Local requirement edits', sync_failure['reason'])
        missing = self.record('attempt_finish', **attempt, outcome='failed', summary='missing',
                              evidence={'material_file_status': 'missing'})
        self.assertEqual(missing['status'], 'material_sync_required')
        failed = self.record('attempt_finish', **attempt, outcome='failed', summary='sync failed')
        self.assertEqual(failed['disposition'], 'retry')
        self.assertEqual(git(self.checkout, 'rev-parse', 'HEAD'), old_head)
        self.assertEqual((self.checkout / 'README.md').read_text(encoding='utf-8'), 'local draft\n')
        retry = self.record('attempt_start', chat_id=attempt['chat_id'], request_id=attempt['request_id'],
                            stage='material_check')
        attempt['attempt_id'] = retry['attempt_id']
        self.assertEqual(self.run_sync(attempt)['status'], 'material_sync_failed')
        final = self.record('attempt_finish', **attempt, outcome='failed', summary='sync failed')
        self.assertEqual(final['disposition'], 'blocked')
        self.assertIsNone(final['next_stage'])

    def test_review_rejects_intent_that_differs_from_synchronized_commit(self):
        intent = 'requirements/REQ-001/intent.md'
        (self.remote / intent).parent.mkdir(parents=True)
        (self.remote / intent).write_text('published requirement\n', encoding='utf-8')
        self.commit_remote('publish requirement')
        attempt = self.start_review()
        self.assertEqual(self.run_sync(attempt)['status'], 'material_synced')

        (self.checkout / intent).write_text('draft requirement\n', encoding='utf-8')
        review = self.record('attempt_finish', **attempt, outcome='success', summary='reviewed',
                             evidence={'path': intent})
        self.assertEqual(review['status'], 'evidence_required')
        self.assertEqual((self.checkout / intent).read_text(encoding='utf-8'), 'draft requirement\n')
        (self.checkout / intent).unlink()
        missing = self.record('attempt_finish', **attempt, outcome='failed', summary='missing',
                              evidence={'path': intent, 'material_file_status': 'missing'})
        self.assertEqual(missing['status'], 'evidence_required')

    def test_sync_preserves_ignored_file_that_incoming_commit_would_replace(self):
        name = 'draft.txt'
        (self.checkout / '.git' / 'info' / 'exclude').write_text(name + '\n', encoding='utf-8')
        (self.checkout / name).write_text('local draft\n', encoding='utf-8')
        (self.remote / name).write_text('remote content\n', encoding='utf-8')
        self.commit_remote('publish file')

        attempt = self.start_review()
        self.assertEqual(self.run_sync(attempt)['status'], 'material_sync_failed')
        self.assertEqual((self.checkout / name).read_text(encoding='utf-8'), 'local draft\n')

    def test_worker_only_grants_matching_requirement_checkout(self):
        codex_root = self.fixture / 'codex'
        config = self.config
        _, allowed = worker_roots([config, config], self.fixture, self.fixture, codex_root)
        self.assertNotIn(self.checkout / '.git', allowed)
        protected, _ = worker_roots([config, config], self.fixture, self.fixture, codex_root)
        self.assertIn(self.checkout, protected)
        self.assertNotIn(self.remote / '.git', allowed)

        old_config = {**config, 'workspaces': {'requirement': 'remote'}}
        _, allowed = worker_roots([config, old_config], self.fixture, self.fixture, codex_root)
        self.assertNotIn(self.checkout / '.git', allowed)
        self.assertNotIn(self.remote / '.git', allowed)

    def test_readonly_verification_does_not_change_directory_into_checkout(self):
        from material_sync import verify_requirement_intent
        intent = 'requirements/REQ-001/intent.md'
        (self.remote / intent).parent.mkdir(parents=True)
        (self.remote / intent).write_text('published requirement\n', encoding='utf-8')
        self.commit_remote('publish requirement')
        attempt = self.start_review()
        synced = self.run_sync(attempt)
        actual_run = subprocess.run
        def readonly_process(argv, **kwargs):
            if '-C' in argv and Path(argv[argv.index('-C') + 1]) == self.checkout:
                raise subprocess.CalledProcessError(128, argv, stderr='fatal: cannot change directory: Permission denied')
            return actual_run(argv, **kwargs)
        with patch('material_sync.subprocess.run', side_effect=readonly_process):
            try:
                verify_requirement_intent(self.checkout, synced['commit'], intent)
            except ValueError as error:
                self.fail('Read-only verification required directory traversal: ' + str(error))

    def test_readonly_verification_resolves_linked_worktree_gitfile(self):
        from material_sync import verify_requirement_intent
        intent = 'intent.md'
        (self.checkout / intent).write_text('published material\n', encoding='utf-8')
        git(self.checkout, 'add', intent)
        git(self.checkout, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            '-c', 'commit.gpgsign=false', 'commit', '-m', 'material')
        commit = git(self.checkout, 'rev-parse', 'HEAD')
        linked = self.fixture / 'linked'
        git(self.checkout, 'worktree', 'add', '--detach', str(linked), commit)
        self.assertTrue((linked / '.git').is_file())
        actual_run = subprocess.run
        def readonly_process(argv, **kwargs):
            if '-C' in argv:
                raise subprocess.CalledProcessError(128, argv, stderr='fatal: cannot change directory: Permission denied')
            return actual_run(argv, **kwargs)
        with patch('material_sync.subprocess.run', side_effect=readonly_process):
            try:
                verify_requirement_intent(linked, commit, intent)
            except ValueError as error:
                self.fail('Read-only linked worktree verification failed: ' + str(error))

    def test_compute_does_not_publish_before_cleanup(self):
        from workflow import compute_material_attempt, publish_material_result
        import sqlite3
        attempt = self.start_review()
        data = {'item': self.item(2), **attempt}
        computed = compute_material_attempt(self.database, self.config, data, self.fixture)
        self.assertEqual(computed['status'], 'material_synced')
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM requirement_sync_receipts').fetchone()[0], 0)
        result = publish_material_result(self.database, self.config, data, computed,
                                        {'attempt_id': attempt['attempt_id'], 'stop_confirmed': False}, self.fixture)
        self.assertEqual(result['status'], 'material_sync_stop_unverified')
        finish = self.record('attempt_finish', **attempt, outcome='success', summary='reviewed', evidence={})
        self.assertEqual(finish['status'], 'material_sync_required')
