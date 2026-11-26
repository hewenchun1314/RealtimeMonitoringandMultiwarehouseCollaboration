import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from delivery_scope import route_stages
from deploy_control import deployment_action
from execution import attempt_start
from git_delivery_runner import push_repositories, run_git_delivery
from ledger import Ledger, encode, transaction
from route_execution import v2_repair
from task_evidence import evaluate


class GitDeliveryTests(unittest.TestCase):
    def test_push_is_required_even_without_deployment(self):
        for side in ('backend', 'frontend'):
            with self.subTest(side=side):
                stages = route_stages(side + '_only', git_push_enabled=True)
                self.assertEqual(stages[-2:], (side + '.integration', side + '.push'))

    def test_joint_backend_repair_pushes_the_new_version_before_deploy(self):
        stages = route_stages('fullstack', git_push_enabled=True, deploy_enabled=True,
                              frontend_deploy_enabled=True)
        self.assertEqual(stages[-3:], ('frontend.integration', 'frontend.push', 'frontend.deploy'))
        phases = v2_repair('frontend.e2e', {'repair_side': 'backend'}, {'scope': 'fullstack', 'stages': stages})
        index = phases.index('backend.integration')
        self.assertEqual(phases[index:index + 4], ['backend.integration', 'backend.push', 'backend.deploy', 'handoff'])

    def test_task_waits_for_git_confirmation_of_its_exact_merge(self):
        sha, old, tip = 'a' * 40, 'b' * 40, 'c' * 40
        route = {'scope': 'backend_only', 'stages': ['backend.push'],
                 'tasks': {'backend': [{'id': 'B1'}]}}
        stages = {
            'backend.plan': {'evidence': {'task_repositories': {'B1': ['service']}}},
            'backend.coding': {'evidence': {'versions': {'service': sha}}},
            'backend.acceptance': {'evidence': {'versions': {'service': sha},
                                                'task_results': {'B1': {'outcome': 'passed'}}}},
            'backend.integration': {'evidence': {'versions': {'service': sha}, 'merged_versions': {'service': sha}}},
        }
        for stage in stages.values():
            stage['outcome'] = 'success'
        self.assertEqual(evaluate(route, stages)['B1'][0], 'open')
        stages['backend.push'] = {'outcome': 'success', 'evidence': {
            'repositories': {'service': {'sha': old, 'remote_sha': tip}}}}
        self.assertEqual(evaluate(route, stages)['B1'][0], 'open')
        stages['backend.push']['evidence']['repositories']['service']['sha'] = sha
        self.assertEqual(evaluate(route, stages)['B1'][0], 'closed')


class GitDeliveryWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_path = self.root / 'zentao-project.json'
        self.database = self.root / 'workflow.sqlite3'
        self.config = json.loads((Path(__file__).resolve().parents[3] / 'zentao-project.json').read_text(
            encoding='utf-8'))
        self.config['runtime'].update(git_push_enabled=True, split_task_sync=False)
        self.config['development']['max_retries_per_step'] = 1
        self.marker = 'push-fixture'

    def git(self, directory, *args):
        return subprocess.check_output(['git', *args], cwd=directory, text=True,
                                       stderr=subprocess.DEVNULL).strip()

    def setup_item(self, side, keys, *, accepted=True, deployment=False, unit_test_passed=True):
        workspace = self.root / side
        self.config['workspaces'][side] = side
        self.config['backend_dependencies'] = {}
        field = 'services' if side == 'backend' else 'applications'
        targets = self.config['development'][side + '_bundle'][field]
        repositories = {}
        for key in keys:
            target = targets[key]
            target.update(repository=key, branch='AI-development')
            repo, remote = workspace / key, self.root / f'{key}.git'
            repo.mkdir(parents=True)
            self.git(self.root, 'init', '--bare', str(remote))
            self.git(repo, 'init', '-b', target['branch'])
            self.git(repo, 'config', 'user.email', 'fixture@example.invalid')
            self.git(repo, 'config', 'user.name', 'fixture')
            self.git(repo, 'remote', 'add', 'origin', str(remote))
            self.git(repo, 'commit', '--allow-empty', '-m', 'merged')
            repositories[key] = (repo.resolve(), remote, self.git(repo, 'rev-parse', 'HEAD'))
        scope = side + '_only'
        stages = route_stages(scope, git_push_enabled=True, deploy_enabled=deployment,
                              frontend_deploy_enabled=deployment)
        route = {'scope': scope, 'source': 'config', 'tasks': {'backend': [], 'frontend': []},
                 'e2e_enabled': True, 'blockers': [], 'stages': stages}
        self.config_path.write_text(encode(self.config), encoding='utf-8')
        evidence = {
            side + '.plan': {field: keys},
            side + '.branch_check': {'repositories': {
                key: {'source_root': str(repo), 'target_branch': 'AI-development', 'status': 'verified'}
                for key, (repo, _, _) in repositories.items()}},
            side + '.integration': {'merged_versions': {key: sha for key, (_, _, sha) in repositories.items()}},
        }
        stage_to_run = side + '.push'
        with transaction(self.database) as conn:
            conn.execute("""INSERT INTO work_items(seq,instance,project_id,kind,item_id,title,assigned_to,
                         source_status,eligible,observed_at,state,marker,config_snapshot,chat_id,request_id,stage)
                         VALUES(1,'fixture','1','story','1','task','worker','active',1,'','running',?,?,'chat','req',?)""",
                         (self.marker, encode({**self.config, 'workflow_version': 2}), stage_to_run))
            conn.execute("INSERT INTO dispatches VALUES('req',1,'desktop','started','')")
            for stage in stages[:stages.index(stage_to_run)]:
                result = {'summary': stage, 'evidence': evidence.get(stage, {}), 'phase': 'verify',
                          'terminal_step': True}
                if stage == 'split':
                    result['route'] = route
                failed = (not accepted and stage.endswith(('.acceptance', '.e2e'))
                          or not unit_test_passed and stage.endswith('.unit_test'))
                outcome = 'failed' if failed else 'success'
                conn.execute("""INSERT INTO attempts(attempt_id,item_seq,run_round,stage,retry_number,
                             started_at,ended_at,outcome,result,phase,phase_started_at)
                             VALUES(?,1,1,?,0,'','',?,?,'verify','')""",
                             (stage, stage, outcome, encode(result)))
        return repositories

    def run_push(self):
        def execute(attempt_id, phase, deadline, cwd, output, prompt, argv, **kwargs):
            push_repositories(json.loads(Path(argv[-2]).read_text(encoding='utf-8')), Path(argv[-1]))
            return {'attempt_id': attempt_id, 'phase': phase, 'outcome': 'success', 'stop_confirmed': True,
                    'stop_evidence': {'owned_processes_active_after_stop': 0}}

        with patch('git_delivery_runner.run_stage', side_effect=execute):
            return run_git_delivery(self.marker, self.database, self.config_path, self.root)

    def test_pushes_without_deployment_and_reuses_success_after_another_repo_fails(self):
        repos = self.setup_item('backend', ['smart-geo', 'model-manage-srv'])
        first, first_remote, first_sha = repos['smart-geo']
        second, second_remote, second_sha = repos['model-manage-srv']
        self.git(second, 'remote', 'remove', 'origin')
        self.assertEqual(self.run_push()['disposition'], 'retry')
        self.assertEqual(self.git(first_remote, 'rev-parse', 'refs/heads/AI-development'), first_sha)
        self.git(first, 'commit', '--allow-empty', '-m', 'unrelated later local commit')
        self.git(second, 'remote', 'add', 'origin', str(second_remote))
        self.assertEqual(self.run_push()['disposition'], 'close')
        self.assertEqual(self.git(first_remote, 'rev-parse', 'refs/heads/AI-development'), first_sha)
        self.assertEqual(self.git(second_remote, 'rev-parse', 'refs/heads/AI-development'), second_sha)

    def test_partial_merge_blocks_push_and_deployment_even_with_a_previous_push_receipt(self):
        self.config['runtime']['split_task_sync'] = True
        repos = self.setup_item('backend', ['smart-geo', 'model-manage-srv'], deployment=True)
        _, _, sha = repos['smart-geo']
        versions = {key: value[2] for key, value in repos.items()}
        updates = {
            'backend.plan': ('success', {'services': list(repos),
                                         'task_repositories': {'B1': ['smart-geo'], 'B2': ['model-manage-srv']}}),
            'backend.coding': ('success', {'versions': versions}),
            'backend.acceptance': ('failed', {'versions': versions, 'task_results': {'B1': {'outcome': 'passed'}}}),
            'backend.integration': ('failed', {'versions': versions, 'merged_versions': {'smart-geo': sha}}),
        }
        with transaction(self.database) as conn:
            for stage, (outcome, evidence) in updates.items():
                conn.execute('UPDATE attempts SET outcome=?,result=? WHERE stage=?', (
                    outcome, encode({'evidence': evidence, 'phase': 'verify', 'terminal_step': True,
                                     'summary': stage}), stage))
        with patch('git_delivery_runner.run_stage') as execute:
            result = run_git_delivery(self.marker, self.database, self.config_path, self.root)
        execute.assert_not_called()
        self.assertEqual(result['outcome'], 'skipped', result)
        self.assertEqual(result.get('next_stage'), 'backend.deploy', result)
        with transaction(self.database) as conn:
            conn.execute("UPDATE attempts SET outcome='success', result=? WHERE stage='backend.push'", (encode({
                'evidence': {'repositories': {'smart-geo': {'sha': sha}}},
                'phase': 'verify', 'terminal_step': True, 'summary': 'Historical partial push',
            }),))
        claim = deployment_action('claim', self.marker, self.database, self.config_path)
        self.assertTrue(claim.get('blocked'), claim)
        self.assertEqual(claim['requests'], {})

    def test_frontend_push_and_deployment_claim_after_failed_tests_and_successful_merge(self):
        _, remote, sha = self.setup_item('frontend', ['shell-hub'], accepted=False, unit_test_passed=False,
                                         deployment=True)['shell-hub']
        result = self.run_push()
        self.assertEqual((result['outcome'], result['next_stage']), ('success', 'frontend.deploy'), result)
        self.assertEqual(self.git(remote, 'rev-parse', 'refs/heads/AI-development'), sha)
        claim = deployment_action('claim', self.marker, self.database, self.config_path)
        self.assertFalse(claim.get('blocked'), claim)
        self.assertEqual(claim['frontend_request']['applications']['shell-hub']['sha'], sha)

    @unittest.skipUnless(os.name == 'nt', 'Git delivery uses Windows Job Objects')
    def test_frontend_push_runs_in_controlled_process(self):
        _, remote, sha = self.setup_item('frontend', ['shell-hub'])['shell-hub']
        result = run_git_delivery(self.marker, self.database, self.config_path, self.root)
        self.assertEqual(result.get('disposition'), 'close', result)
        self.assertEqual(self.git(remote, 'rev-parse', 'refs/heads/AI-development'), sha)

    def test_revoked_mapping_leaves_remote_untouched(self):
        self.setup_item('backend', ['smart-geo'])
        self.config['development']['backend_bundle']['services']['smart-geo']['branch'] = 'other'
        self.config_path.write_text(encode(self.config), encoding='utf-8')
        claim = deployment_action('claim', self.marker, self.database, self.config_path)
        self.assertEqual(claim['push_requests'], {})
        self.assertIn('smart-geo', claim['errors'])

    def test_backend_push_requires_the_worker_and_advances_to_deployment_after_failed_tests(self):
        self.setup_item('backend', ['smart-geo'], accepted=False, unit_test_passed=False, deployment=True)
        with transaction(self.database) as conn:
            item = conn.execute('SELECT * FROM work_items').fetchone()
            data = {'item': {'instance': 'fixture', 'project_id': '1', 'kind': 'story', 'id': '1'},
                    'request_id': 'req', 'chat_id': 'chat', 'stage': 'backend.push'}
            direct = attempt_start(Ledger(conn, self.config, datetime.now(timezone.utc)), item, data,
                                   config_directory=self.config_path.parent)
        self.assertEqual(direct['status'], 'deployment_worker_required')
        self.assertEqual(deployment_action('peek', self.marker, self.database, self.config_path)['stage'],
                         'backend.push')
        self.assertEqual(self.run_push()['next_stage'], 'backend.deploy')
        claim = deployment_action('claim', self.marker, self.database, self.config_path)
        self.assertFalse(claim.get('blocked'), claim)
        self.assertIn('smart-geo', claim['requests'])
