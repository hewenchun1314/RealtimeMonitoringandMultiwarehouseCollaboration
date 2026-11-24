import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from route_evidence import delivery_summary, deployment_requirements, validate_success
from status_board import table_row


class RouteEvidenceTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        conn = sqlite3.connect(self.root / 'ledger.sqlite3')
        self.addCleanup(conn.close)
        self.store = SimpleNamespace(conn=conn)
        self.item = {'marker': 'item-1', 'run_round': 1}
        self.doc = self.root / 'items/item-1/round-1/backend/handoff.md'
        self.doc.parent.mkdir(parents=True)
        self.doc.write_text('GET /reports returns the total', encoding='utf-8')
        self.merged = {'reports': 'b' * 40}
        self.handoff = dict(backend_versions=self.merged, document=str(self.doc),
                            environment='local reports service', verification='GET /reports at release b')
        self.history = {'backend.' + key: {'verified': True} for key in
                        ('design', 'plan', 'branch_check', 'coding', 'unit_test', 'code_review', 'acceptance')}
        self.history['backend.integration'] = {'merged_versions': self.merged}
        self.route = {'scope': 'fullstack', 'blockers': [], 'tasks': {
            'frontend': [{'id': 'F1'}, {'id': 'F2'}], 'backend': [{'id': 'B1'}]}}
        for name, lookup in [('stage_evidence', lambda store, item, key: self.history.get(key)),
                             ('get_split', lambda *args: self.route),
                             ('read_history', lambda *args: (self.route, {}))]:
            mock = patch('route_evidence.' + name, side_effect=lookup)
            mock.start()
            self.addCleanup(mock.stop)

    def check(self, stage, evidence):
        return validate_success(self.store, self.item, stage, evidence)

    def test_handoff_uses_only_callable_versions_after_partial_deployment(self):
        self.route['stages'] = ['backend.deploy']
        self.history['backend.integration']['merged_versions'] = {**self.merged, 'archive': 'c' * 40}
        target = {'registry': 'registry.local', 'image_repository': 'reports', 'gateway_url': 'http://localhost:8080'}
        deployed = {'status': 'deployed', 'service': 'reports', 'sha': 'b' * 40, 'target': target,
                    'image': 'registry.local/reports:current', 'digest': 'sha256:' + 'd' * 64,
                    'verification': {'sha': 'b' * 40}}
        stages = {'backend.deploy': {'task_blockers': {'B2': 'Archive release failed'},
                                     'evidence': {'services': {'reports': deployed}}}}
        proof = {**self.handoff, 'deployment': {'reports': deployed}, 'gateway_url': target['gateway_url']}
        with patch('route_evidence.read_history', return_value=(self.route, stages)), patch(
                'route_evidence.deployment_requirements',
                return_value=({'reports': target, 'archive': target}, [],
                              self.history['backend.integration']['merged_versions'], None)):
            self.assertIsNone(self.check('handoff', proof))
            wrong = {**proof, 'backend_versions': self.history['backend.integration']['merged_versions']}
            self.assertIsNotNone(self.check('handoff', wrong))

    def test_handoff_excludes_backends_that_failed_git_delivery(self):
        self.route['stages'] = ['backend.push']
        self.history['backend.integration']['merged_versions'] = {**self.merged, 'archive': 'c' * 40}
        push = {'backend.push': {'outcome': 'failed', 'terminal_step': True,
                                 'task_blockers': {'B2': 'Archive push failed'},
                                 'evidence': {'repositories': {'reports': {'sha': self.merged['reports']}}}}}
        with patch('route_evidence.read_history', return_value=(self.route, push)):
            self.assertIsNone(self.check('handoff', self.handoff))
            self.assertIsNotNone(self.check('handoff', {
                **self.handoff, 'backend_versions': self.history['backend.integration']['merged_versions']}))

    def test_unpushed_deploy_target_does_not_block_another_verified_local_service(self):
        self.route['stages'] = ['backend.push', 'backend.deploy']
        self.history['backend.plan'] = {'services': ['reports', 'archive']}
        versions = {**self.merged, 'archive': 'c' * 40}
        stages = {
            'backend.integration': {'evidence': {'merged_versions': versions}},
            'backend.push': {'outcome': 'failed', 'terminal_step': True,
                             'evidence': {'repositories': {'reports': {'sha': versions['reports']}}}},
        }
        with (patch('route_evidence.read_history', return_value=(self.route, stages)),
              patch('route_evidence.snapshot', return_value={}),
              patch('route_evidence.configured_targets', return_value={'archive': {'target': 'test'}})):
            selected, skipped, verified, reason = deployment_requirements(self.store, self.item)
        self.assertEqual(selected, {})
        self.assertEqual(skipped, ['reports', 'archive'])
        self.assertEqual(verified, self.merged)
        self.assertIsNone(reason)

    def local_candidate(self):
        source, tree = self.root / 'source', self.root / 'item'
        source.mkdir()
        def git(path, *args):
            return subprocess.run(['git', '-C', str(path), *args], check=True,
                                  capture_output=True, text=True, timeout=10).stdout.strip()
        git(source, 'init', '-b', 'main')
        git(source, 'config', 'user.name', 'Route Test')
        git(source, 'config', 'user.email', 'fixture@example.invalid')
        (source / 'code').write_text('base', encoding='utf-8')
        git(source, 'add', 'code')
        git(source, 'commit', '-m', 'baseline')
        sha = git(source, 'rev-parse', 'HEAD')
        git(source, 'worktree', 'add', '-b', 'task', str(tree))
        (source / 'code').write_text('user edit in original checkout', encoding='utf-8')
        proof = {'worktree': str(tree), 'worktree_branch': 'task', 'base_commit': sha, 'source_root': str(source)}
        self.history['frontend.branch_check'] = {'repositories': {'page': proof}}
        self.history['frontend.coding'] = {'versions': {'page': sha}}
        return tree, {'page': sha}

    def test_dirty_tracked_and_indexed_changes_in_item_worktree_reject_candidate_and_tests(self):
        tree, versions = self.local_candidate()
        evidence = {'versions': versions, 'verification': 'Ran local tests'}
        for stage in ('frontend.coding', 'frontend.unit_test'):
            self.assertIsNone(self.check(stage, evidence))
        (tree / 'code').write_text('different from submitted code', encoding='utf-8')
        for staged in (False, True):
            if staged:
                subprocess.run(['git', '-C', str(tree), 'add', 'code'], check=True)
                (tree / 'code').write_text('base', encoding='utf-8')
            for stage in ('frontend.coding', 'frontend.unit_test'):
                with self.subTest(staged=staged, stage=stage):
                    self.assertTrue(self.check(stage, evidence))

    def test_handoff_requires_current_document_versions_and_successful_dependencies(self):
        self.assertIsNone(self.check('handoff', self.handoff))
        for field, value in [('backend_versions', {'reports': 'a' * 40}), ('environment', '  '),
                             ('verification', ''), ('document', str(self.root / 'other.md'))]:
            with self.subTest(field=field):
                self.assertTrue(self.check('handoff', {**self.handoff, field: value}))
        del self.history['backend.code_review']
        self.assertTrue(self.check('handoff', self.handoff))

    def test_empty_or_unrelated_document_cannot_unlock_frontend(self):
        self.doc.write_text(' ', encoding='utf-8')
        self.assertTrue(self.check('handoff', self.handoff))
        other = self.root / 'other.md'
        other.write_text('valid file from another item', encoding='utf-8')
        self.assertTrue(self.check('handoff', {**self.handoff, 'document': str(other)}))

    def test_backend_blocker_prevents_handoff_even_with_one_working_service(self):
        self.route['blockers'] = ['第二个接口缺分页']
        self.assertTrue(self.check('handoff', self.handoff))
        self.route['handoff_blockers'] = []
        self.assertIsNone(self.check('handoff', self.handoff))

    def test_joint_verification_is_pinned_to_both_sides(self):
        _, versions = self.local_candidate()
        self.history['handoff'] = self.handoff
        proof = dict(versions=versions, verification='page displayed the total', backend_versions=self.merged,
                     verified_tasks=['F1', 'F2'])
        self.assertIsNone(self.check('frontend.e2e', proof))
        self.assertTrue(self.check('frontend.e2e', {**proof, 'backend_versions': {}}))
        self.assertTrue(self.check('frontend.e2e', {**proof, 'versions': {'page': 'c' * 40}}))
        self.assertTrue(self.check('frontend.e2e', {**proof, 'verification': ''}))
        self.assertTrue(self.check('frontend.e2e', {**proof, 'verified_tasks': ['F1']}))

    def test_summary_distinguishes_partial_merge_from_acceptance(self):
        stages = {'backend.integration': {'outcome': 'failed', 'evidence': {'merged_versions': self.merged}},
                  'backend.acceptance': {'outcome': 'failed'}}
        summary = delivery_summary({'scope': 'fullstack', 'source': 'document', 'blockers': ['总数不匹配']}, stages)
        for expected in ('前后端', '需求标记', '服务验收失败', '部分合回', 'reports', 'bbbbbbbb', '总数不匹配'):
            self.assertIn(expected, summary)

    def test_status_page_shows_active_backend_repair_within_frontend_acceptance(self):
        row = dict(item_id='1', title='审批状态', state='running', conclusion=None, stage='frontend.e2e',
                   phase='backend.integration', pending=0, retry_number=1)
        html = table_row(row, 'http://localhost')
        self.assertIn('后端合回', html)
