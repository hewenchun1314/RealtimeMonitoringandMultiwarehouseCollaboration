import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import worker
from worker import command, worker_roots


class WorkerTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
