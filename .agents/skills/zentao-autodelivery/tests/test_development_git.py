"""Host Git handoff contracts exercised against disposable real Git repositories."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import unittest
from unittest.mock import patch
import test_route_drills


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True,
                          encoding='utf-8', errors='replace', check=True, timeout=15).stdout.strip()


class DevelopmentGitTests(unittest.TestCase):
    def setUp(self):
        self.start_side('frontend')

    def start_side(self, side, extra=None, registry=None, key=None):
        self.side = side
        self.d = test_route_drills.RouteDrills()
        self.d.setUp()
        self.addCleanup(self.d.doCleanups)
        if key:
            apps = self.d.config['development']['frontend_bundle']['applications']
            apps[key] = dict(apps[self.d.key])
            self.d.key = key
        if registry is not None:
            self.d.config['runtime']['worker_git_write_repositories'] = registry
        self.d.begin_drill(side + '_only', '纯前端' if side == 'frontend' else '纯后端', extra_backend=extra)
        for stage in ('material_check', 'split', side + '.design', side + '.plan'):
            attempt = self.d.record('attempt_start', stage=stage)
            evidence = self.d.evidence(stage, attempt)
            if extra and stage.endswith('.plan'):
                evidence['services'].append(extra)
            self.d.finish(stage, attempt, evidence)
        self.attempt = self.d.record('attempt_start', stage=side + '.branch_check')
        self.config_path = self.d.root / 'project.json'
        self.config_path.write_text(json.dumps(self.d.config), encoding='utf-8')

    def request(self, action, **fields):
        return self.d.record(action, attempt_id=self.attempt['attempt_id'],
                             phase=self.attempt['phase'], **fields)

    def host(self):
        from development_git_runner import run_development_git
        with closing(sqlite3.connect(self.d.database)) as conn:
            marker = conn.execute('SELECT marker FROM work_items WHERE item_id=17').fetchone()[0]
        return run_development_git(marker, self.d.database, self.config_path, self.d.root, recover=True)

    def test_branch_prepare_is_a_status_only_batch_handoff(self):
        with patch('workflow.prepare_local_worktree') as mutate:
            result = self.request('branch_prepare', application=self.d.key,
                                  desktop_reconciliation={'status': 'unavailable', 'evidence': 'fixture'})
        self.assertEqual(result['status'], 'host_git_worker_required')
        mutate.assert_not_called()

    def test_host_preparation_returns_verified_deterministic_worktree(self):
        request = self.request('branch_prepare')
        result = self.host()
        self.assertEqual(result['status'], 'host_git_completed', result)
        proof = result['repositories'][self.d.key]
        self.assertEqual(proof['status'], 'verified')
        self.assertEqual(git(Path(proof['worktree']), 'rev-parse', 'HEAD'), proof['base_commit'])

    def test_failed_postcheckout_retries_original_check_before_preparation_success(self):
        hook = self.install_hook('post-checkout', 'echo "$@" >> "$PWD/checkout-args.log"\nexit 1\n')
        self.request('branch_prepare')
        failed = self.host()
        self.assertEqual(failed['status'], 'host_git_failed', failed)
        self.request('branch_prepare')
        again = self.host()
        self.assertEqual(again['status'], 'host_git_failed', again)
        journal = next((self.d.database.parent / 'worktrees').rglob('checkout-args.log'))
        arguments = journal.read_text().splitlines()
        self.assertEqual(len(arguments), 2)
        self.assertEqual(arguments[0], arguments[1])
        hook.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8', newline='\n')
        self.request('branch_prepare')
        self.assertEqual(self.host()['status'], 'host_git_completed')

    def begin_coding(self):
        self.request('branch_prepare')
        prepared = self.host()
        proof = prepared['repositories'][self.d.key]
        self.d.worktrees[self.side] = Path(proof['worktree'])
        self.d.finish(self.side + '.branch_check', self.attempt, {'repositories': prepared['repositories']})
        self.attempt = self.d.record('attempt_start', stage=self.side + '.coding')
        return Path(proof['worktree'])

    def candidate(self, root, files=None, event=None, verification=''):
        fields = {'commits': {self.d.key: {'files': files if files is not None else ['index.html'],
                  'expected_head': git(root, 'rev-parse', 'HEAD'), 'message': 'feat: deliver fixture',
                  'verification': verification}}}
        if event:
            fields['event_id'] = event
        return self.request('candidate_commit', **fields), fields

    def install_hook(self, name, body):
        folder = self.d.repositories[self.side] / '.git' / 'hooks'
        hook = folder / name
        hook.write_text('#!/bin/sh\n' + body, encoding='utf-8', newline='\n')
        hook.chmod(0o755)
        return hook

    def integration_phase(self, result):
        self.d.versions[self.side] = result['versions']
        result = self.d.finish(self.side + '.coding', self.attempt, {'versions': result['versions']})
        stage = result['next_stage']
        while stage != self.side + '.integration':
            attempt = self.d.record('attempt_start', stage=stage)
            result = self.d.finish(stage, attempt, self.d.evidence(stage, attempt))
            stage = result['next_stage']
        self.attempt = self.d.record('attempt_start', stage=stage)

    def test_host_candidate_and_integration_keep_hooks_enabled(self):
        root = self.begin_coding()
        self.install_hook('commit-msg', 'grep -q \"feat: deliver fixture\" "$1" || exit 1\n')
        (root / 'index.html').write_text('<output>approved</output>\n')
        self.candidate(root)
        result = self.host()
        self.assertEqual(result['status'], 'host_git_completed', result)
        self.assertEqual(result['versions'][self.d.key], git(root, 'rev-parse', 'HEAD'))
        self.integration_phase(result)
        self.request('integration_merge')
        merged = self.host()
        self.assertEqual(merged['status'], 'host_git_completed', merged)
        self.assertEqual(merged['merged_versions'], result['versions'])

    def test_failed_precommit_returns_real_diagnostics_and_can_be_fixed(self):
        root = self.begin_coding()
        self.install_hook('pre-commit', 'echo "fixture lint rejection" >&2\nexit 1\n')
        (root / 'index.html').write_text('<output>approved</output>\n')
        before = git(root, 'rev-parse', 'HEAD')
        self.candidate(root)
        result = self.host()
        self.assertEqual(result['status'], 'host_git_failed')
        self.assertEqual(git(root, 'rev-parse', 'HEAD'), before)
        logs = list((self.d.database.parent / 'items').rglob('git-diagnostics/*.json'))
        self.assertTrue(any('fixture lint rejection' in p.read_text() for p in logs))
        self.install_hook('pre-commit', 'exit 0\n')
        self.candidate(root)
        self.assertEqual(self.host()['status'], 'host_git_completed')

    def test_hook_formatted_commit_rereads_stable_event_without_duplicate_commit(self):
        root = self.begin_coding()
        self.install_hook('pre-commit', 'printf \"<output>approved</output>\\n\" > index.html\ngit add -- index.html\n')
        (root / 'index.html').write_text('<output>unformatted</output>\n')
        request, fields = self.candidate(root, event='stable-format')
        result = self.host()
        self.assertEqual(result['repositories'][self.d.key]['status'], 'candidate_retest_required', result)
        head = git(root, 'rev-parse', 'HEAD')
        again = self.request('candidate_commit', **fields)
        self.assertEqual(again, result)
        self.candidate(root, files=[], verification='Fixture asserted the formatted current candidate')
        confirmed = self.host()
        self.assertEqual(confirmed['status'], 'host_git_completed', confirmed)
        self.assertEqual(git(root, 'rev-parse', 'HEAD'), head)

    def test_failed_postcommit_cannot_be_cleared_by_noop_without_passing_hook(self):
        root = self.begin_coding()
        self.install_hook('post-commit', 'echo "post check failed" >&2\nexit 1\n')
        (root / 'index.html').write_text('<output>approved</output>\n')
        self.candidate(root)
        result = self.host()
        self.assertEqual(result['repositories'][self.d.key]['status'], 'candidate_retest_required', result)
        head = git(root, 'rev-parse', 'HEAD')
        self.candidate(root, files=[], verification='retested fixture')
        blocked = self.host()
        self.assertEqual(blocked['status'], 'host_git_failed', blocked)
        self.assertEqual(git(root, 'rev-parse', 'HEAD'), head)
        self.install_hook('post-commit', 'exit 0\n')
        self.candidate(root, files=[], verification='retested fixture after fixing original posthook')
        self.assertEqual(self.host()['status'], 'host_git_completed')
        self.assertEqual(git(root, 'rev-parse', 'HEAD'), head)

    def test_failed_postmerge_is_not_silently_accepted_as_already_contained(self):
        root = self.begin_coding()
        (root / 'index.html').write_text('<output>approved</output>\n')
        self.candidate(root)
        candidate = self.host()
        self.integration_phase(candidate)
        self.install_hook('post-merge', 'echo "merge check failed" >&2\nexit 1\n')
        self.request('integration_merge')
        result = self.host()
        self.assertEqual(result['repositories'][self.d.key]['status'], 'integration_retest_required', result)
        self.request('integration_merge', verification='retested merged fixture')
        self.assertEqual(self.host()['status'], 'host_git_failed')
        self.install_hook('post-merge', 'exit 0\n')
        self.request('integration_merge', verification='retested after fixing merge hook')
        self.assertEqual(self.host()['status'], 'host_git_completed')

    def test_lost_commit_computation_recovers_existing_candidate_without_second_commit(self):
        import development_git_runner as runner
        root = self.begin_coding()
        (root / 'index.html').write_text('<output>approved</output>\n')
        self.candidate(root)
        real = runner.run_stage
        calls = []
        def lose_result(*args, **kwargs):
            proof = real(*args, **kwargs)
            calls.append(proof)
            if len(calls) == 1:
                folder = Path(proof['receipt_path']).parent
                (folder / 'computed.json').unlink()
                for path in folder.glob('effects/*/result.json'):
                    path.unlink()
            return proof
        with patch.object(runner, 'run_stage', side_effect=lose_result):
            result = self.host()
        self.assertEqual(result['repositories'][self.d.key]['status'], 'candidate_retest_required', result)
        self.assertEqual(git(root, 'rev-list', '--count', 'HEAD'), '2')
        self.assertEqual(len(calls), 2)

    def test_wrong_identity_stop_receipt_cannot_publish_or_release_checkout(self):
        self.request('branch_prepare')
        with patch('development_git_runner.run_stage', return_value={
                'attempt_id': 'foreign', 'phase': 'host-git', 'deadline_at': self.attempt['deadline_at'],
                'stop_confirmed': True, 'stop_evidence': {'owned_processes_active_after_stop': 0},
                'receipt_path': 'foreign'}):
            result = self.host()
        self.assertEqual(result['status'], 'host_git_stop_unverified', result)
        self.assertTrue(list((self.d.database.parent / 'development-git-locks').glob('*-pending.json')))

    def test_backend_batch_keeps_partial_candidates_and_integrates_both_repositories(self):
        self.start_side('backend', extra='upstream-api')
        self.request('branch_prepare')
        prepared = self.host()
        self.assertEqual(set(prepared['repositories']), {self.d.key, 'upstream-api'})
        self.d.finish('backend.branch_check', self.attempt, {'repositories': prepared['repositories']})
        self.attempt = self.d.record('attempt_start', stage='backend.coding')
        commits = {}
        for key, proof in prepared['repositories'].items():
            root = Path(proof['worktree'])
            (root / 'status.json').write_text('{"status":"approved"}\n')
            commits[key] = {'expected_head': git(root, 'rev-parse', 'HEAD'), 'files': ['status.json'], 'message': 'feat: fixture backend'}
            if key == self.d.key:
                self.d.worktrees['backend'] = root
        hook = self.d.repositories['backend'] / 'upstream-api' / '.git' / 'hooks' / 'pre-commit'
        hook.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8', newline='\n')
        hook.chmod(0o755)
        self.request('candidate_commit', commits=commits)
        partial = self.host()
        self.assertEqual(partial['status'], 'host_git_failed', partial)
        self.assertEqual(set(partial['versions']), {self.d.key})
        preserved = partial['versions'][self.d.key]
        hook.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8', newline='\n')
        self.request('candidate_commit', commits={'upstream-api': commits['upstream-api']})
        repaired = self.host()
        versions = {**partial['versions'], **repaired['versions']}
        self.assertEqual(git(self.d.worktrees['backend'], 'rev-parse', 'HEAD'), preserved)
        self.integration_phase({'versions': versions})
        self.request('integration_merge')
        merged = self.host()
        self.assertEqual(merged['status'], 'host_git_completed', merged)
        self.assertEqual(merged['merged_versions'], versions)

    def test_saved_failed_hook_result_survives_missing_batch_computation(self):
        import development_git_runner as runner
        root = self.begin_coding()
        self.install_hook('post-commit', 'exit 1\n')
        (root / 'index.html').write_text('<output>approved</output>\n')
        self.candidate(root)
        real = runner.run_stage
        removed = False
        def lose_summary(*args, **kwargs):
            nonlocal removed
            proof = real(*args, **kwargs)
            if not removed:
                (Path(proof['receipt_path']).parent / 'computed.json').unlink()
                removed = True
            return proof
        with patch.object(runner, 'run_stage', side_effect=lose_summary):
            failed = self.host()
        self.assertEqual(failed['repositories'][self.d.key]['failed_hooks'], ['post-commit'])
        self.candidate(root, files=[], verification='retested')
        self.assertEqual(self.host()['status'], 'host_git_failed')

    def test_canonical_pending_gate_blocks_source_and_linked_checkout_until_fresh_stop(self):
        import development_git_runner as runner
        from development_git_effects import common_directory
        from stage_runner import write_json
        source = self.d.repositories['frontend']
        linked = self.d.root / 'user-linked'
        git(source, 'worktree', 'add', '--detach', str(linked), 'HEAD')
        common = common_directory(source, self.attempt['deadline_at'])
        self.assertEqual(common_directory(linked, self.attempt['deadline_at']), common)
        old_id = '7' * 64
        marker = 'HNF-' + '7' * 20
        old = self.d.database.parent / 'items' / marker / 'development-git' / old_id / 'host-git-resume-1'
        old.mkdir(parents=True)
        path = runner.lock_path(self.d.database, common_directory(linked, self.attempt['deadline_at']))
        path.parent.mkdir(parents=True)
        write_json(path, {'common': str(common), 'operation_id': old_id, 'marker': marker,
                          'deadline_at': self.attempt['deadline_at'], 'phase': 'host-git-resume-1'})
        self.request('branch_prepare')
        with patch.object(runner, 'recover_proof', return_value=None) as recover, \
                patch.object(runner, 'run_stage') as launch:
            self.assertEqual(self.host()['status'], 'host_git_busy')
        launch.assert_not_called()
        self.assertEqual(recover.call_args.args[2], 'host-git-resume-1')
        self.assertTrue(path.exists())
        with patch.object(runner, 'recover_proof', return_value={'stop_confirmed': True}):
            self.assertEqual(self.host()['status'], 'host_git_completed')

    def test_integration_never_moves_target_checked_out_in_another_user_worktree(self):
        root = self.begin_coding()
        (root / 'index.html').write_text('<output>approved</output>\n')
        self.candidate(root)
        candidate = self.host()
        self.integration_phase(candidate)
        source = self.d.repositories['frontend']
        before = git(source, 'rev-parse', 'HEAD')
        git(source, 'switch', '--detach')
        user = self.d.root / 'user-target'
        git(source, 'worktree', 'add', str(user), 'AI-development')
        self.request('integration_merge')
        blocked = self.host()
        self.assertEqual(blocked['status'], 'host_git_failed')
        self.assertIn('another user worktree', blocked['repositories'][self.d.key]['reason'])
        self.assertEqual(git(user, 'rev-parse', 'HEAD'), before)

    def test_live_git_write_registry_revocation_blocks_public_and_queued_host_operations(self):
        key = 'frontend:aggregation-platform'
        self.start_side('frontend', registry=[key])
        self.d.config['runtime']['worker_git_write_repositories'] = []
        rejected = self.request('branch_prepare')
        self.assertEqual(rejected['status'], 'host_git_rejected', rejected)
        self.d.config['runtime']['worker_git_write_repositories'] = [key]
        self.request('branch_prepare')
        self.d.config['runtime'].pop('worker_git_write_repositories')
        self.config_path.write_text(json.dumps(self.d.config), encoding='utf-8')
        self.assertEqual(self.host()['status'], 'host_git_not_pending')
        self.assertFalse((self.d.database.parent / 'worktrees').exists())

    def test_integration_owned_temporary_checkout_keeps_failed_checkout_gate(self):
        root = self.begin_coding()
        (root / 'index.html').write_text('<output>approved</output>\n')
        self.candidate(root)
        self.integration_phase(self.host())
        source = self.d.repositories['frontend']
        git(source, 'switch', '-c', 'user-context')
        hook = self.install_hook('post-checkout', 'exit 1\n')
        self.request('integration_merge')
        first = self.host()
        self.assertEqual(first['status'], 'host_git_failed', first)
        self.request('integration_merge')
        self.assertEqual(self.host()['status'], 'host_git_failed')
        hook.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8', newline='\n')
        self.request('integration_merge')
        merged = self.host()
        self.assertEqual(merged['status'], 'host_git_completed', merged)
        self.assertEqual(git(source, 'branch', '--show-current'), 'user-context')

    def test_unpublished_failed_posthook_still_blocks_new_attempt_noop(self):
        import development_git_runner as runner
        root = self.begin_coding()
        self.install_hook('post-commit', 'exit 1\n')
        (root / 'index.html').write_text('<output>approved</output>\n')
        self.candidate(root)
        real = runner.run_stage
        expired = False
        deadline = datetime.fromisoformat(self.attempt['deadline_at'])
        def expire_after_child(*args, **kwargs):
            nonlocal expired
            proof = real(*args, **kwargs)
            expired = True
            return proof
        with patch.object(runner, 'run_stage', side_effect=expire_after_child), \
                patch.object(runner, 'utc_now', side_effect=lambda: deadline + timedelta(seconds=1) if expired else datetime.now(timezone.utc)):
            self.assertEqual(self.host()['status'], 'host_git_inactive')
        self.d.now = deadline + timedelta(seconds=1)
        self.d.record('attempt_finish', attempt_id=self.attempt['attempt_id'], outcome='timed_out',
                      summary='Original fixture deadline', evidence={}, stop_confirmed=True,
                      stop_evidence={'process_exit': True})
        self.attempt = self.d.record('attempt_start', stage='frontend.coding')
        self.candidate(root, files=[])
        failed = self.host()
        self.assertEqual(failed['status'], 'host_git_failed', failed)

    def test_recovered_integration_reuses_valid_prior_round_branch_receipt(self):
        root = self.begin_coding()
        (root / 'index.html').write_text('<output>approved</output>\n')
        self.candidate(root)
        candidate = self.host()
        self.integration_phase(candidate)
        stage = 'frontend.integration'
        self.d.record('attempt_finish', attempt_id=self.attempt['attempt_id'], outcome='failed',
                      retryable=False, summary='Fixture target was temporarily locked', evidence={})
        self.d.record('complete', blockers_path='blockers.md')
        restored = self.d.record('recover', recovery_id='target-restored', evidence='Target lock was released')
        self.assertEqual(restored['stage'], stage)
        receipt = self.d.call('claim', {'action': 'reserve', 'item': self.d.item,
            'observed_at': self.d.now.isoformat(), 'assigned_to': 'drill', 'status': 'active', 'desktop_project_id': 'drill'})
        self.d.binding['request_id'] = receipt['request_id']
        self.assertEqual(self.d.record('start')['stage'], stage)
        self.attempt = self.d.record('attempt_start', stage=stage)
        requested = self.request('integration_merge')
        self.assertEqual(requested['status'], 'host_git_worker_required', requested)
        result = self.host()
        self.assertEqual(result['status'], 'host_git_completed', result)
        self.assertEqual(result['merged_versions'], candidate['versions'])

    def test_missing_target_is_fetched_only_by_host_from_registered_origin(self):
        source = self.d.repositories['frontend']
        base = git(source, 'rev-parse', 'HEAD')
        remote = self.d.root / 'origin.git'
        git(source, 'clone', '--bare', '--no-hardlinks', str(source), str(remote))
        git(source, 'remote', 'add', 'origin', str(remote))
        git(source, 'switch', '-c', 'user-working-branch')
        git(source, 'branch', '-D', 'AI-development')
        git(source, 'branch', 'AI-development/local-child')
        requested = self.request('branch_prepare')
        self.assertEqual(requested['status'], 'host_git_worker_required')
        refs = git(source, 'for-each-ref', '--format=%(refname)')
        self.assertNotIn('refs/remotes/origin/AI-development', refs)
        completed = self.host()
        self.assertEqual(completed['status'], 'host_git_completed', completed)
        proof = completed['repositories'][self.d.key]
        self.assertEqual(proof['source_ref'], 'refs/remotes/origin/AI-development')
        self.assertEqual(proof['base_commit'], base)
        self.assertEqual(git(source, 'branch', '--show-current'), 'user-working-branch')

    def test_commit_rejects_escaping_paths_and_foreign_staged_changes(self):
        root = self.begin_coding()
        for files in (['../outside'], ['.git/config'], ['C:/outside'], ['/outside']):
            with self.subTest(files=files):
                rejected, _ = self.candidate(root, files=files)
                self.assertEqual(rejected['status'], 'host_git_rejected', rejected)
        (root / 'foreign.txt').write_text('unrelated user staging\n')
        git(root, 'add', '--', 'foreign.txt')
        (root / 'index.html').write_text('<output>approved</output>\n')
        self.candidate(root)
        failed = self.host()
        self.assertEqual(failed['status'], 'host_git_failed')
        self.assertIn('outside', failed['repositories'][self.d.key]['reason'])
        self.assertEqual(git(root, 'rev-list', '--count', 'HEAD'), '1')

    def test_host_commit_rechecks_expected_head_and_requested_file_bytes(self):
        root = self.begin_coding()
        (root / 'index.html').write_text('<output>approved</output>\n')
        self.candidate(root)
        (root / 'index.html').write_text('<output>changed after request</output>\n')
        failed = self.host()
        self.assertIn('contents changed', failed['repositories'][self.d.key]['reason'])
        self.candidate(root)
        git(root, 'commit', '--allow-empty', '-m', 'Concurrent fixture actor')
        failed = self.host()
        self.assertIn('stale', failed['repositories'][self.d.key]['reason'])

    def test_wrong_binding_and_expired_attempt_cannot_enqueue_host_work(self):
        wrong = self.request('branch_prepare', chat_id='another-chat')
        self.assertEqual(wrong['status'], 'host_git_rejected')
        wrong = self.request('branch_prepare', application='unplanned')
        self.assertEqual(wrong['status'], 'host_git_rejected')
        self.d.now = datetime.fromisoformat(self.attempt['deadline_at']) + timedelta(seconds=1)
        self.assertEqual(self.request('branch_prepare')['status'], 'host_git_rejected')
        with closing(sqlite3.connect(self.d.database)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM development_git_operations').fetchone()[0], 0)

    def test_configured_plan_key_cannot_escape_host_evidence_or_worktree_roots(self):
        for key in ('../escaped', 'C:/host-path', '/rooted', '\\rooted'):
            with self.subTest(key=key):
                self.start_side('frontend', key=key)
                requested = self.request('branch_prepare')
                self.assertEqual(requested['status'], 'host_git_rejected', requested)

    def test_integration_preserves_user_ignored_file_in_source_checkout(self):
        source = self.d.repositories['frontend']
        (source / '.gitignore').write_text('notes.txt\n')
        git(source, 'add', '--', '.gitignore')
        git(source, 'commit', '-m', 'Fixture ignore user notes')
        (source / 'notes.txt').write_text('USER LOCAL NOTES\n')
        before = git(source, 'rev-parse', 'HEAD')
        root = self.begin_coding()
        (root / '.gitignore').write_text('')
        (root / 'notes.txt').write_text('DELIVERY NOTES\n')
        (root / 'index.html').write_text('<output>approved</output>\n')
        self.candidate(root, files=['.gitignore', 'notes.txt', 'index.html'])
        self.integration_phase(self.host())
        self.request('integration_merge')
        result = self.host()
        self.assertEqual(result['status'], 'host_git_failed', result)
        self.assertEqual((source / 'notes.txt').read_text(), 'USER LOCAL NOTES\n')
        self.assertEqual(git(source, 'rev-parse', 'HEAD'), before)

    def test_missing_generated_husky_runtime_cannot_silently_skip_tracked_check(self):
        source = self.d.repositories['frontend']
        checks = source / '.husky'
        checks.mkdir()
        (checks / 'pre-commit').write_text('exit 1\n')
        git(source, 'config', 'core.hooksPath', '.husky/_')
        git(source, 'add', '--', '.husky/pre-commit')
        git(source, 'commit', '-m', 'Fixture original Husky check without installed runtime')
        before = git(source, 'rev-parse', 'HEAD')
        self.request('branch_prepare')
        failed = self.host()
        self.assertEqual(failed['status'], 'host_git_failed', failed)
        self.assertIn('runtime', failed['repositories'][self.d.key]['reason'].lower())
        self.assertEqual(git(source, 'rev-parse', 'HEAD'), before)

    def test_preparation_requires_original_postcheckout_husky_runtime(self):
        source = self.d.repositories['frontend']
        checks = source / '.husky'
        checks.mkdir()
        (checks / 'post-checkout').write_text('exit 1\n')
        git(source, 'config', 'core.hooksPath', '.husky/_')
        git(source, 'add', '--', '.husky/post-checkout')
        git(source, 'commit', '-m', 'Fixture tracked checkout check without generated runtime')
        self.request('branch_prepare')
        failed = self.host()
        self.assertEqual(failed['status'], 'host_git_failed', failed)
        self.assertIn('runtime', failed['repositories'][self.d.key]['reason'].lower())
        self.assertFalse((self.d.database.parent / 'worktrees').exists())

    def test_target_only_husky_check_requires_runtime_even_when_source_branch_has_no_check(self):
        source = self.d.repositories['frontend']
        base = git(source, 'rev-parse', 'HEAD')
        checks = source / '.husky'
        checks.mkdir()
        (checks / 'pre-commit').write_text('exit 1\n')
        git(source, 'add', '--', '.husky/pre-commit')
        git(source, 'commit', '-m', 'Fixture target adds original precommit')
        git(source, 'switch', '-c', 'user-branch-without-check', base)
        git(source, 'config', 'core.hooksPath', '.husky/_')
        root = self.begin_coding()
        self.assertFalse((source / '.husky/pre-commit').exists())
        self.assertTrue((root / '.husky/pre-commit').is_file())
        (root / 'index.html').write_text('<output>approved</output>\n')
        self.candidate(root)
        failed = self.host()
        self.assertEqual(failed['status'], 'host_git_failed', failed)
        self.assertIn('runtime', failed['repositories'][self.d.key]['reason'].lower())

    def test_target_only_postcheckout_requires_runtime_before_first_checkout(self):
        source = self.d.repositories['frontend']
        base = git(source, 'rev-parse', 'HEAD')
        checks = source / '.husky'
        checks.mkdir()
        (checks / 'post-checkout').write_text('exit 1\n')
        git(source, 'add', '--', '.husky/post-checkout')
        git(source, 'commit', '-m', 'Fixture target adds original checkout check')
        git(source, 'switch', '-c', 'user-branch-without-check', base)
        git(source, 'config', 'core.hooksPath', '.husky/_')
        self.request('branch_prepare')
        failed = self.host()
        self.assertEqual(failed['status'], 'host_git_failed', failed)
        self.assertIn('runtime', failed['repositories'][self.d.key]['reason'].lower())
        self.assertFalse((self.d.database.parent / 'worktrees').exists())

    def test_launcher_queue_poll_does_not_migrate_a_legacy_ledger(self):
        from development_git_runner import pending_development_git
        with closing(sqlite3.connect(self.d.database)) as conn, conn:
            marker = conn.execute('SELECT marker FROM work_items WHERE item_id=17').fetchone()[0]
            conn.execute('DROP TABLE development_git_operations')
        self.assertEqual(pending_development_git(marker, self.d.database, self.config_path)['status'], 'host_git_not_pending')
        with closing(sqlite3.connect(self.d.database)) as conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='development_git_operations'").fetchone())

    def source_checkout_versions(self, source_exit):
        """Tracked SOURCE/TARGET scripts with a real argv0-relative wrapper."""
        source = self.d.repositories['frontend']
        folder = source / '.husky'
        folder.mkdir()
        events = self.d.root / 'checkout-events.log'
        def script(tag, status):
            return ("printf '%s|%s|%s\\n' " + tag + ' "$PWD" "$*" >> '
                    + shlex.quote(events.as_posix()) + '\nexit ' + str(status) + '\n')
        original = folder / 'post-checkout'
        original.write_text(script('TARGET', 0), encoding='utf-8', newline='\n')
        (source / '.gitignore').write_text('.husky/_/\n')
        git(source, 'add', '--', '.gitignore', '.husky/post-checkout')
        git(source, 'commit', '-m', 'Fixture target checkout check')
        git(source, 'switch', '-c', 'user-source-hook-context')
        original.write_text(script('SOURCE', source_exit), encoding='utf-8', newline='\n')
        git(source, 'add', '--', '.husky/post-checkout')
        git(source, 'commit', '-m', 'Fixture distinct source checkout check')
        runtime = folder / '_'
        runtime.mkdir()
        wrapper = runtime / 'post-checkout'
        wrapper.write_text('#!/bin/sh\nexec sh "$(dirname "$(dirname "$0")")/post-checkout" "$@"\n',
                           encoding='utf-8', newline='\n')
        wrapper.chmod(0o755)
        git(source, 'config', 'core.hooksPath', '.husky/_')
        def fix():
            original.write_text(script('SOURCE', 0), encoding='utf-8', newline='\n')
            git(source, 'add', '--', '.husky/post-checkout')
            git(source, 'commit', '-m', 'Fixture fix original source checkout check')
        return events, fix

    def test_preparation_retry_retains_failed_source_hook_executable_cwd_and_args(self):
        events, fix_source = self.source_checkout_versions(source_exit=1)
        self.request('branch_prepare')
        self.assertEqual(self.host()['status'], 'host_git_failed')
        self.request('branch_prepare')
        failed = self.host()
        self.assertEqual(failed['status'], 'host_git_failed', failed)
        lines = events.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], lines[1])
        self.assertTrue(lines[0].startswith('SOURCE|'), lines)
        fix_source()
        self.request('branch_prepare')
        self.assertEqual(self.host()['status'], 'host_git_completed')
        self.assertTrue(all(line.startswith('SOURCE|') for line in events.read_text().splitlines()))

    def test_temp_integration_retry_retains_failed_source_checkout_dispatch(self):
        events, fix_source = self.source_checkout_versions(source_exit=0)
        root = self.begin_coding()
        (root / 'index.html').write_text('<output>approved</output>\n')
        self.candidate(root)
        candidate = self.host()
        self.integration_phase(candidate)
        source = self.d.repositories['frontend']
        original = source / '.husky/post-checkout'
        original.write_text(original.read_text().replace('exit 0', 'exit 1'), encoding='utf-8', newline='\n')
        git(source, 'add', '--', '.husky/post-checkout')
        git(source, 'commit', '-m', 'Fixture source checkout check fails before integration')
        events.write_text('')
        self.request('integration_merge')
        self.assertEqual(self.host()['status'], 'host_git_failed')
        self.request('integration_merge')
        failed = self.host()
        self.assertEqual(failed['status'], 'host_git_failed', failed)
        lines = events.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], lines[1])
        self.assertTrue(lines[0].startswith('SOURCE|'), lines)
        fix_source()
        self.request('integration_merge')
        result = self.host()
        self.assertEqual(result['status'], 'host_git_completed', result)
        self.assertTrue(all(line.startswith('SOURCE|') for line in events.read_text().splitlines()))
