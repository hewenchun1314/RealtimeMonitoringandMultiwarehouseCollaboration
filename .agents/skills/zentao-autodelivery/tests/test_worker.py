import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import worker
from worker import command, worker_roots


class WorkerTests(unittest.TestCase):
    def setUp(self):
        pending = patch.object(worker, 'pending_development_git', return_value={'status': 'host_git_not_pending'}, create=True)
        pending.start()
        self.addCleanup(pending.stop)
        stage = patch.object(worker, 'pending_stage', return_value=None)
        stage.start()
        self.addCleanup(stage.stop)

    def test_stage_handoff_resumes_original_chat_after_host_preflight_failure(self):
        item = {'state': 'running', 'chat_id': 'fixed-chat', 'stage': 'frontend.coding'}
        calls = []

        def chat(current):
            self.assertIs(current, item)
            calls.append('chat')
            return {'receipt_path': 'chat', 'stop_confirmed': True, 'outcome': 'success'}, None

        def stage(*args):
            calls.append('host')
            return {'stop_confirmed': True, 'outcome': 'failed',
                    'preflight_failure': {'check': 'sandbox'}}

        with (
            patch.object(worker, 'read_item', return_value=item),
            patch.object(worker, 'deployment_action', return_value={'status': 'deployment_not_pending'}),
            patch.object(worker, 'pending_stage', side_effect=lambda *args: 'request' if calls == ['chat'] else None),
            patch.object(worker, 'run_pending_stage', side_effect=stage),
            patch.object(worker, 'runtime_check', return_value={'status': 'runtime_unattended'}),
        ):
            result = worker.dispatch_work_item('HNF-' + '1' * 20, chat)
        self.assertEqual(calls, ['chat', 'host', 'chat'])
        self.assertEqual(result['host_stage']['preflight_failure']['check'], 'sandbox')

    def test_development_git_handoff_stops_host_and_resumes_same_chat(self):
        item = {'state': 'running', 'chat_id': 'fixed-chat', 'stage': 'backend.coding'}
        required = False
        calls = []
        def pending(*args):
            return {'status': 'host_git_pending' if required else 'host_git_not_pending'}
        def chat(current):
            nonlocal required
            self.assertIs(current, item)
            required = not calls
            calls.append('chat')
            return {'receipt_path': 'chat', 'stop_confirmed': True, 'outcome': 'success'}, None
        def host(*args, **kwargs):
            nonlocal required
            calls.append('host')
            required = False
            return {'status': 'host_git_failed', 'repositories': {'fixture': {'reason': 'hook rejected'}}}
        with patch.object(worker, 'read_item', return_value=item), \
                patch.object(worker, 'deployment_action', return_value={'status': 'deployment_not_pending'}), \
                patch.object(worker, 'pending_development_git', side_effect=pending), \
                patch.object(worker, 'run_development_git', side_effect=host, create=True), \
                patch.object(worker, 'runtime_check', return_value={'status': 'runtime_unattended'}):
            result = worker.dispatch_work_item('HNF-' + '1' * 20, chat)
        self.assertEqual(calls, ['chat', 'host', 'chat'])
        self.assertEqual(result['development_git']['status'], 'host_git_failed')

    def test_development_git_cannot_launch_until_prior_worker_stop_proven(self):
        item = {'state': 'running', 'chat_id': 'fixed-chat', 'stage': 'frontend.branch_check'}
        with patch.object(worker, 'read_item', return_value=item), \
                patch.object(worker, 'deployment_action', return_value={'status': 'deployment_not_pending'}), \
                patch.object(worker, 'pending_development_git', return_value={'status': 'host_git_pending'}), \
                patch.object(worker, 'run_development_git', create=True) as host:
            result = worker.dispatch_work_item('HNF-' + '1' * 20, lambda item: self.fail('must not resume'))
        self.assertEqual(result['status'], 'worker_host_git_stop_unverified')
        host.assert_not_called()

    def deployment_route(self, peek, deployments, chat_outcomes):
        item = {'state': 'running', 'chat_id': '01a1f1df-ddb1-77f1-aa07-a96f2a74d43a'}
        calls = []

        def chat(current_item):
            self.assertEqual(current_item, item)
            calls.append('chat')
            return next(chat_outcomes)

        def deploy(*args, **kwargs):
            calls.append('deploy')
            return next(deployments)

        with patch.object(worker, 'read_item', return_value=item), \
                patch.object(worker, 'runtime_check', return_value={'status': 'runtime_unattended'}), \
                patch.object(worker, 'deployment_action',
                             side_effect=[{'status': state} for state in peek]), \
                patch.object(worker, 'run_deployment', side_effect=deploy):
            result = worker.dispatch_work_item('HNF-' + '1' * 20, chat)
        return result, calls

    def test_work_item_switches_from_chat_to_deployment_and_back(self):
        result, calls = self.deployment_route(
            ['deployment_not_pending', 'deployment_pending', 'deployment_not_pending', 'deployment_not_pending'],
            iter([{'status': 'attempt_finished'}]),
            iter([({'receipt_path': 'chat-before', 'stop_confirmed': True, 'outcome': 'success'}, None),
                  ({'receipt_path': 'chat-after', 'stop_confirmed': True, 'outcome': 'success'}, None)]))
        self.assertEqual(calls, ['chat', 'deploy', 'chat'])
        self.assertEqual(result['receipt'], 'chat-after')

    def test_resumed_work_item_recovers_deployment_before_starting_chat(self):
        result, calls = self.deployment_route(
            ['deployment_pending', 'deployment_not_pending', 'deployment_not_pending'],
            iter([{'status': 'repair_advanced'}]),
            iter([({'receipt_path': 'chat-after', 'stop_confirmed': True, 'outcome': 'success'}, None)]))
        self.assertEqual(calls, ['deploy', 'chat'])
        self.assertEqual(result['status'], 'worker_stopped')

    def test_unverified_process_ownership_prevents_deployment_and_chat_resume(self):
        cases = [
            (['deployment_not_pending'], iter([]),
             iter([({'receipt_path': 'chat', 'stop_confirmed': False, 'outcome': 'failed'}, None)]),
             ['chat'], 'worker_stop_unverified'),
            (['deployment_pending'], iter([{'status': 'deployment_busy', 'attempt_id': 'existing'}]),
             iter([]), ['deploy'], 'worker_deployment_blocked'),
        ]
        for peek, deployments, chat_outcomes, expected_calls, expected_status in cases:
            with self.subTest(status=expected_status):
                result, calls = self.deployment_route(peek, deployments, chat_outcomes)
                self.assertEqual(calls, expected_calls)
                self.assertEqual(result['status'], expected_status)

    def test_material_handoff_uses_host_then_resumes_same_chat(self):
        item = {'state': 'running', 'chat_id': 'fixed-chat', 'stage': 'material_check'}
        required = False
        calls = []
        def pending(*args):
            return {'status': 'material_sync_pending' if required else 'material_sync_not_pending'}
        def chat(current):
            nonlocal required
            self.assertIs(current, item)
            required = not calls
            calls.append('chat')
            return {'receipt_path': 'chat', 'stop_confirmed': True, 'outcome': 'success'}, None
        def host(*args, **kwargs):
            nonlocal required
            calls.append('host')
            required = False
            return {'status': 'material_sync_failed'}
        with patch.object(worker, 'read_item', return_value=item), \
                patch.object(worker, 'deployment_action', return_value={'status': 'deployment_not_pending'}), \
                patch.object(worker, 'pending_material_sync', side_effect=pending, create=True), \
                patch.object(worker, 'run_material_sync', side_effect=host, create=True), \
                patch.object(worker, 'runtime_check', return_value={'status': 'runtime_unattended'}):
            result = worker.dispatch_work_item('HNF-' + '1' * 20, chat)
        self.assertEqual(calls, ['chat', 'host', 'chat'])
        self.assertEqual(result['material_sync']['status'], 'material_sync_failed')

    def test_interrupted_material_handoff_requires_prior_stop_before_host(self):
        item = {'state': 'running', 'chat_id': 'fixed-chat', 'stage': 'material_check'}
        with patch.object(worker, 'read_item', return_value=item), \
                patch.object(worker, 'deployment_action', return_value={'status': 'deployment_not_pending'}), \
                patch.object(worker, 'pending_material_sync', return_value={'status': 'material_sync_pending'}, create=True), \
                patch.object(worker, 'run_material_sync', create=True) as host:
            result = worker.dispatch_work_item('HNF-' + '1' * 20, lambda item: self.fail('must not resume'))
        self.assertEqual(result['status'], 'worker_material_stop_unverified')
        host.assert_not_called()

    def test_material_only_turn_and_task_jobs_are_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = root / 'config.json'
            config = {'workflow_version': 2,
                      'runtime': {'sync_requirement_before_review': True, 'split_task_sync': True},
                      'zentao': {'base_url': 'http://localhost:8088', 'project_name': 'Sample',
                                 'assigned_to_accounts': [], 'accepted_statuses': {'story': [], 'bug': []}},
                      'monitor': {'enabled': False},
                      'development': {'max_concurrent_items': 1, 'max_retries_per_step': 0,
                                      'step_timeout_minutes': {}}}
            cfg.write_text(json.dumps(config), encoding='utf-8')
            item = {'config_snapshot': json.dumps(config), 'chat_id': '01a1f1df-ddb1-77f1-aa07-a96f2a74d43a',
                    'request_id': '1' * 32, 'marker': 'HNF-' + '1' * 20,
                    'instance': 'http://localhost:8088', 'project_id': '1', 'kind': 'story', 'item_id': '2'}
            with patch.object(worker, 'CONFIG_PATH', cfg), patch.object(worker, 'validate_config'), \
                    patch.object(worker, 'worker_roots', return_value=([], [])), \
                    patch.object(worker, 'run_stage', return_value={}) as run_stage:
                worker.launch_turn(root / 'codex.exe', 'run', item, root, worker.utc_now())
            self.assertEqual(run_stage.call_count, 2)
            self.assertTrue(all(call.kwargs.get('recoverable') for call in run_stage.call_args_list))

    def test_worker_uses_never_for_new_and_resumed_chats(self):
        cli = Path('codex.exe')
        chat = '01a1f1df-ddb1-77f1-aa07-a96f2a74d43a'
        for operation in ('prepare', 'run', 'probe'):
            argv = command(cli, operation, chat)
            self.assertEqual(argv[:4], [str(cli), '--ask-for-approval', 'never', 'exec'])
            self.assertEqual(argv[-1], '-')
            self.assertIn('--json', argv)
            self.assertEqual('--ephemeral' in argv, operation == 'probe')
            self.assertEqual('resume' in argv, operation == 'run')
            self.assertEqual(chat in argv, operation == 'run')

    def test_new_and_resumed_workers_get_read_only_dependency_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dependency = root / 'upstream.with space'
            chat = '01a1f1df-ddb1-77f1-aa07-a96f2a74d43a'
            for operation in ('prepare', 'run', 'probe'):
                argv = command(Path('codex.exe'), operation, chat,
                               protected_roots=[dependency], writable_roots=[root])
                self.assertIn('default_permissions="zentao_delivery"', argv)
                self.assertIn('windows.sandbox="mxc"', argv)
                filesystem = next(arg for arg in argv if arg.startswith('permissions.zentao_delivery.filesystem='))
                self.assertIn(json.dumps(dependency.as_posix()) + '="read"', filesystem)
                self.assertIn(json.dumps(root.as_posix()) + '="write"', filesystem)
                self.assertIn('":root"="read"', filesystem)
                if operation == 'run':
                    self.assertLess(argv.index(filesystem), argv.index('resume'))

    def test_resume_protects_dependency_paths_from_old_and_current_configurations(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            settings = project / '.agents'
            workspace = project / 'frontend'
            runtime = project / 'codex-data'

            def configuration(root):
                return {'workspaces': {'frontend': '../frontend'},
                        'runtime': {'worker_cache_paths': ['../build-cache']},
                        'development': {'backend_bundle': {'services': {'service': {'repository': 'service'}}}},
                        'backend_dependencies': {'upstream': {
                            'source_path': root, 'api_source': 'api', 'used_by': ['service'],
                        }}}

            protected, writable = worker_roots([configuration('../current'), configuration('../original')],
                                               settings, project, runtime)
            self.assertCountEqual(protected, [project / 'current', project / 'original'])
            self.assertCountEqual(writable, [project, workspace, runtime, project / 'build-cache'])

    def test_dependency_write_access_requires_matching_current_and_admitted_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            upstream = project / 'providers' / 'upstream'
            config = {'development': {'backend_bundle': {'services': {'api': {'repository': 'api'}}}},
                      'backend_dependencies': {'upstream': {
                          'source_path': 'providers/upstream', 'api_source': 'api', 'used_by': ['api'],
                          'access': 'writable',
                      }}}
            read_only = {**config, 'backend_dependencies': {'upstream': {
                **config['backend_dependencies']['upstream'], 'access': 'read_only',
            }}}
            for configs, expected in (([config, config], True), ([config, read_only], False),
                                      ([read_only, config], False), ([config, {}], False)):
                with self.subTest(configs=configs):
                    protected, writable = worker_roots(configs, project, project, project / 'runtime')
                    self.assertEqual(upstream in writable, expected)
                    self.assertEqual(upstream in protected, not expected)

    def test_git_metadata_grant_requires_a_matching_configured_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            checkout = project / 'backend' / 'service'
            checkout.mkdir(parents=True)
            metadata = project / 'backend' / '.git' / 'modules' / 'service'
            metadata.parent.mkdir(parents=True)
            subprocess.run(['git', 'init', f'--separate-git-dir={metadata}', checkout],
                           check=True, capture_output=True)
            settings = project / '.agents'
            admitted = {
                'workspaces': {'backend': '../backend'},
                'development': {'backend_bundle': {'services': {'service': {'repository': 'service'}}}},
                'backend_dependencies': {'upstream': {
                    'source_path': '../upstream', 'api_source': '.', 'used_by': ['service'],
                }},
            }
            current = {**admitted, 'runtime': {'worker_git_write_repositories': ['backend:service']}}
            protected, writable = worker_roots([current, admitted], settings, project, project / 'runtime')
            self.assertIn(metadata, writable)
            self.assertNotIn(metadata.parents[1], writable)
            self.assertIn(project / 'upstream', protected)
            other = {**admitted, 'workspaces': {'backend': '../other'}}
            _, writable = worker_roots([current, other], settings, project, project / 'runtime')
            self.assertNotIn(metadata, writable)


if __name__ == '__main__':
    unittest.main()
