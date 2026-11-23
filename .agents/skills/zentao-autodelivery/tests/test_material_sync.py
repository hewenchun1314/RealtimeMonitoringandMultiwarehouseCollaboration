import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

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

    def run_sync(self, attempt):
        if os.name != 'nt':
            return self.record('material_sync', **attempt)

        config_file = self.fixture / 'project.json'
        config_file.write_text(json.dumps(self.config), encoding='utf-8')
        input_file = self.fixture / 'sync.json'
        input_file.write_text(json.dumps({'action': 'material_sync', 'item': self.item(2), **attempt}),
                              encoding='utf-8')
        review = self.record('attempt_start', chat_id=attempt['chat_id'], request_id=attempt['request_id'],
                             stage='material_check')
        result = subprocess.run(
            [sys.executable, '-B', str(SCRIPTS / 'stage_runner.py'), '--attempt-id', attempt['attempt_id'],
             '--phase', 'material-sync', '--deadline-at', review['deadline_at'], '--cwd', str(self.fixture),
             '--evidence-dir', str(self.fixture / 'evidence'), '--stdin-file', str(input_file),
             '--', sys.executable, '-B', str(SCRIPTS / 'workflow.py'), 'record',
             '--config', str(config_file), '--database', str(self.database)],
            check=True, capture_output=True, encoding='utf-8', timeout=25)
        receipt = json.loads(result.stdout)
        self.assertTrue(receipt['stop_confirmed'], receipt)
        return json.loads(Path(receipt['stdout_path']).read_text(encoding='utf-8'))

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

        self.assertEqual(self.record('material_sync', **attempt)['status'], 'material_sync_failed')
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
        self.assertEqual(self.record('material_sync', **attempt)['status'], 'material_sync_failed')
        final = self.record('attempt_finish', **attempt, outcome='failed', summary='sync failed')
        self.assertEqual(final['disposition'], 'blocked')
        self.assertIsNone(final['next_stage'])

    def test_review_rejects_intent_that_differs_from_synchronized_commit(self):
        intent = 'requirements/REQ-001/intent.md'
        (self.remote / intent).parent.mkdir(parents=True)
        (self.remote / intent).write_text('published requirement\n', encoding='utf-8')
        self.commit_remote('publish requirement')
        attempt = self.start_review()
        self.assertEqual(self.record('material_sync', **attempt)['status'], 'material_synced')

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
        self.assertEqual(self.record('material_sync', **attempt)['status'], 'material_sync_failed')
        self.assertEqual((self.checkout / name).read_text(encoding='utf-8'), 'local draft\n')

    def test_worker_only_grants_matching_requirement_checkout(self):
        codex_root = self.fixture / 'codex'
        config = self.config
        _, allowed = worker_roots([config, config], self.fixture, self.fixture, codex_root)
        self.assertIn(self.checkout / '.git', allowed)
        self.assertNotIn(self.remote / '.git', allowed)

        old_config = {**config, 'workspaces': {'requirement': 'remote'}}
        _, allowed = worker_roots([config, old_config], self.fixture, self.fixture, codex_root)
        self.assertNotIn(self.checkout / '.git', allowed)
        self.assertNotIn(self.remote / '.git', allowed)
