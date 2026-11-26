import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import agentlimb_e2e as e2e
from route_evidence import agentlimb_receipt_error


class PreflightStartupTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(__file__).resolve().parent
        self.config = {
            'workspaces': {'frontend': str(self.workspace)},
            'e2e': {
                'workspace': 'frontend', 'start_command': 'npm run serve-shell-hub',
                'login': {'path': '/login'}, 'platform_entry_path': '/platform',
                'default_work_area': {'name': 'Test area'},
                'agentlimb': {
                    'preflight_origin': 'http://localhost:5000', 'preflight_entry_path': '/data-overview',
                    'preflight_checks': [{'name': 'overview', 'text': '井列表'}],
                    'preflight_startup_ports': [5010],
                },
            },
        }
        self.args = argparse.Namespace(profile=None, scenario=None, marker=None, base_url=None)

    def test_default_browser_check_needs_no_work_item_fixture(self):
        options = e2e.browser_options(self.config, self.args)
        self.assertEqual(options['entryPath'], '/data-overview')
        self.assertEqual(options['checks'], [{'name': 'overview', 'text': '井列表'}])
        self.assertNotIn('credentials', options)

    def test_previous_snapshot_still_uses_agentlimb_without_credentials(self):
        settings = self.config['e2e']
        settings['playwright'] = settings.pop('agentlimb')
        settings['login']['credential_ref'] = 'windows-dpapi:retired'
        options = e2e.browser_options(self.config, self.args)
        self.assertEqual(options['browser'], 'ZenTao-E2E')
        self.assertNotIn('credentials', options)

    def test_receipt_requires_same_candidate_attempt_and_business_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / '.local/zentao-delivery/items/HNF-test/runner.json'
            output.parent.mkdir(parents=True)
            evidence = {'agentlimb_receipt': str(output)}
            versions = {'shell': 'a' * 40}
            receipt = {'runner': 'agentlimb', 'marker': 'HNF-test', 'attempt_id': 'current', 'status': 'failed',
                       'stage': 'acceptance', 'repair_required': True, 'versions': versions,
                       'checks': ['overview'], 'candidate_resources': [{'app': 'shell'}]}

            def check(attempt_id):
                output.write_text(json.dumps(receipt), encoding='utf-8')
                with (patch('route_evidence.stage_evidence', return_value={'versions': versions}),
                      patch('route_evidence.get_split', return_value={})):
                    return agentlimb_receipt_error(None, {'marker': 'HNF-test'}, attempt_id,
                                                 evidence, 'failed', root / '.agents')

            self.assertIsNone(check('current'))
            self.assertIn('active attempt', check('expired'))
            receipt['repair_required'] = False
            self.assertIn('business assertion', check('current'))

    def test_optional_profile_uses_its_own_entry_and_assertions(self):
        with tempfile.TemporaryDirectory() as folder:
            self.args.profile = Path(folder) / 'item.json'
            self.args.profile.write_text(json.dumps({'entry_path': '/item', 'checks': []}), encoding='utf-8')
            options = e2e.browser_options(self.config, self.args)
        self.assertEqual(options['entryPath'], '/item')
        self.assertEqual(options['checks'], [])

    def test_open_origin_port_reuses_service_without_starting_another(self):
        options = e2e.browser_options(self.config, self.args)
        with patch.object(e2e, 'port_open', return_value=True), patch.object(e2e.subprocess, 'Popen') as start:
            self.assertEqual(e2e.prepare_frontend(self.config['e2e'], options, None), {'mode': 'reused'})
        start.assert_not_called()

    def test_frontend_only_candidate_refuses_to_reuse_the_base_service(self):
        options = e2e.browser_options(self.config, self.args)
        options['startupWorkspace'] = str(self.workspace / 'candidate')
        with patch.object(e2e, 'port_open', return_value=True), patch.object(e2e.subprocess, 'Popen') as start:
            self.assertEqual(e2e.prepare_frontend(self.config['e2e'], options, None)['reason'],
                             'candidate-port-in-use')
        start.assert_not_called()

    def test_post_merge_only_restarts_when_loaded_resource_is_incompatible(self):
        options = e2e.browser_options(self.config, self.args)
        options.update(postMerge=True, startupWorkspace=str(self.workspace))
        args = argparse.Namespace(startup_log=Path('frontend.log'), scenario=Path('scenario.json'))
        reports = [Mock(stdout=b'{"status":"blocked","stage":"candidate-resource"}'),
                   Mock(stdout=b'{"status":"passed","stage":"acceptance"}')]
        with (patch.object(e2e, 'prepare_frontend', side_effect=[{'mode': 'reused'}, {'mode': 'started'}]) as start,
              patch.object(e2e.subprocess, 'run', side_effect=reports) as browser):
            result = e2e.run_browser(self.config, options, args)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(start.call_args.kwargs, {'restart': True})
        self.assertEqual(browser.call_count, 2)

    def test_post_merge_starts_from_main_checkout_after_reclaiming_configured_ports(self):
        options = e2e.browser_options(self.config, self.args)
        settings = self.config['e2e']
        settings['startup_ports'] = [5000, 5010, 5080]
        options.update(postMerge=True, startupWorkspace=str(self.workspace),
                       resources=[{'url': 'http://localhost:5010/index.js'}])
        child = Mock(pid=37, poll=Mock(return_value=None))
        with (tempfile.TemporaryDirectory() as directory,
              patch.object(e2e, 'stop_frontend_ports', create=True, return_value=[{'port': 5080, 'pid': 59}]) as stop,
              patch.object(e2e, 'port_open', return_value=True),
              patch.object(e2e, 'http_ready', return_value=True),
              patch.object(e2e.subprocess, 'Popen', return_value=child) as start):
            result = e2e.prepare_frontend(settings, options, Path(directory) / 'front.log', restart=True)
        stop.assert_called_once_with([5000, 5010, 5080])
        self.assertEqual(result['mode'], 'started')
        self.assertEqual(start.call_args.kwargs['cwd'], str(self.workspace))
        self.assertIn('agentlimb_resource_witness.cjs', start.call_args.kwargs['env']['NODE_OPTIONS'])

    def test_post_merge_does_not_restart_a_matching_service(self):
        options = e2e.browser_options(self.config, self.args)
        options.update(postMerge=True, startupWorkspace=str(self.workspace))
        args = argparse.Namespace(startup_log=Path('frontend.log'), scenario=Path('scenario.json'))
        with (patch.object(e2e, 'prepare_frontend', return_value={'mode': 'reused'}) as start,
              patch.object(e2e.subprocess, 'run', return_value=Mock(stdout=b'{"status":"passed"}')) as browser):
            result = e2e.run_browser(self.config, options, args)
        self.assertEqual(result['frontend']['mode'], 'reused')
        start.assert_called_once()
        browser.assert_called_once()

    def test_post_merge_rejects_resource_port_outside_config_without_stopping_anything(self):
        options = e2e.browser_options(self.config, self.args)
        options.update(postMerge=True, startupWorkspace=str(self.workspace),
                       resources=[{'url': 'http://localhost:5011/index.js'}])
        settings = dict(self.config['e2e'], startup_ports=[5000, 5010])
        with patch.object(e2e, 'stop_frontend_ports') as stop:
            with self.assertRaisesRegex(ValueError, 'configured startup ports'):
                e2e.prepare_frontend(settings, options, None, restart=True)
        stop.assert_not_called()

    def test_post_merge_setup_prefers_actual_integration_version_and_main_workspace(self):
        options = e2e.browser_options(self.config, self.args)
        options['resources'] = [{'app': 'shell'}]
        sha = 'a' * 40
        route = {'scope': 'frontend_only', 'post_merge_e2e': True}
        repositories = {'shell': {'source_root': str(self.workspace)}}
        stages = {
            'frontend.branch_check': {'evidence': {'repositories': repositories}},
            'frontend.coding': {'evidence': {'versions': {'shell': 'b' * 40}}},
            'frontend.integration': {'outcome': 'success', 'evidence': {'merged_versions': {'shell': sha}}},
        }
        connection = Mock()
        connection.execute.return_value.fetchone.side_effect = [
            {'seq': 1, 'run_round': 1, 'state': 'running', 'stage': 'frontend.e2e'},
            {'phase': 'verify', 'attempt_id': 'current'},
        ]
        args = argparse.Namespace(marker='HNF-test', candidate_workspace=None, scenario=Path('scenario.json'))
        with (patch.object(e2e.sqlite3, 'connect', return_value=connection),
              patch('deploy_state.configured_targets', return_value={}),
              patch('route_execution.read_history', return_value=(route, stages)),
              patch.object(e2e, 'merged_workspace', return_value={}) as check):
            self.assertEqual(e2e.acceptance_setup(self.config, options, args), {})
        self.assertEqual(options['startupWorkspace'], str(self.workspace))
        self.assertEqual(options['candidateVersions'], {'shell': sha})
        check.assert_called_once_with(options, self.config, repositories, {'shell': sha})

    def test_cold_start_waits_for_owned_command_and_required_ports(self):
        options = e2e.browser_options(self.config, self.args)
        child = Mock(pid=37)
        child.poll.return_value = None
        with (tempfile.TemporaryDirectory() as folder,
              patch.object(e2e, 'port_open', side_effect=[False, True, False, True, True]) as port,
              patch.object(e2e, 'http_ready', return_value=True) as ready,
              patch.object(e2e, 'sleep'), patch.object(e2e.subprocess, 'Popen', return_value=child) as start):
            log = Path(folder) / 'frontend.log'
            result = e2e.prepare_frontend(self.config['e2e'], options, log)
        self.assertEqual(result['mode'], 'started')
        self.assertEqual(result['pid'], 37)
        self.assertEqual([call.args[1] for call in port.call_args_list], [5000, 5000, 5010, 5000, 5010])
        self.assertEqual(ready.call_args.args, ('http://localhost:5000/login',))
        self.assertEqual(start.call_args.args[0][-1], 'npm run serve-shell-hub')
        self.assertEqual(start.call_args.kwargs['cwd'], str(self.workspace))

    def test_start_command_exit_is_reported_before_browser(self):
        options = e2e.browser_options(self.config, self.args)
        with (tempfile.TemporaryDirectory() as folder, patch.object(e2e, 'port_open', return_value=False),
              patch.object(e2e.subprocess, 'Popen', return_value=Mock(pid=37, poll=Mock(return_value=1)))):
            self.assertEqual(e2e.prepare_frontend(self.config['e2e'], options, Path(folder) / 'frontend.log')['reason'],
                             'start-command-exited')

    def test_deployed_candidate_refuses_reuse_of_unproven_local_service(self):
        options = e2e.browser_options(self.config, self.args)
        options['gatewayUrl'] = 'http://deployment.example.test:31111/'
        with patch.object(e2e, 'port_open', return_value=True), patch.object(e2e.subprocess, 'Popen') as start:
            result = e2e.prepare_frontend(self.config['e2e'], options, None)
        self.assertEqual(result['mode'], 'start-failed')
        self.assertEqual(result['reason'], 'candidate-port-in-use')
        start.assert_not_called()

    def test_deployed_candidate_uses_the_recorded_gateway(self):
        self.config['e2e']['start_command'] = 'npm run serve-shell-hub --gateway=http://old.example.test/'
        options = e2e.browser_options(self.config, self.args)
        options['gatewayUrl'] = 'http://deployment.example.test:31111/'
        child = Mock(pid=37, poll=Mock(return_value=None))
        with (tempfile.TemporaryDirectory() as folder,
              patch.object(e2e, 'port_open', side_effect=[False, False, True, True]),
              patch.object(e2e, 'http_ready', return_value=True),
              patch.object(e2e.subprocess, 'Popen', return_value=child) as start):
            result = e2e.prepare_frontend(self.config['e2e'], options, Path(folder) / 'frontend.log')
        self.assertEqual(result['gateway'], options['gatewayUrl'])
        self.assertEqual(start.call_args.args[0][-1],
                         "npm run serve-shell-hub --gateway='http://deployment.example.test:31111/'")

    def test_wrong_live_backend_blocks_the_browser_before_agentlimb(self):
        options = e2e.browser_options(self.config, self.args)
        target = {'gateway_url': 'http://deployment.example.test:31111/', 'verification_path': '/version'}
        options['gatewayUrl'] = target['gateway_url']
        deployment = {'smart-geo': {'sha': 'a' * 40, 'target': target}}
        args = argparse.Namespace(startup_log=Path('frontend.log'), scenario=Path('scenario.cjs'))
        ordered = Mock()
        ordered.start.return_value = {'mode': 'started'}
        ordered.verify.side_effect = ValueError('live SHA changed')
        with (patch.object(e2e, 'prepare_frontend', ordered.start),
              patch.object(e2e, 'verify_deployment', ordered.verify),
              patch.object(e2e.subprocess, 'run') as browser):
            result = e2e.run_browser(self.config, options, args, deployment=deployment)
        self.assertEqual(result['stage'], 'backend-deployment')
        self.assertEqual([method[0] for method in ordered.mock_calls], ['start', 'verify'])
        browser.assert_not_called()

    def test_version_drift_after_agentlimb_invalidates_acceptance(self):
        options = e2e.browser_options(self.config, self.args)
        args = argparse.Namespace(startup_log=Path('frontend.log'), scenario=Path('scenario.cjs'))
        deployment = {'smart-geo': {'sha': 'a' * 40}}
        with (patch.object(e2e, 'prepare_frontend', return_value={'mode': 'started'}),
              patch.object(e2e, 'verify_deployment', side_effect=[{}, ValueError('live SHA changed')]),
              patch.object(e2e.subprocess, 'run', return_value=Mock(stdout=b'{"status": "passed"}'))):
            result = e2e.run_browser(self.config, options, args, deployment=deployment)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'backend-version-changed-during-acceptance')

    def test_deployed_acceptance_uses_only_the_current_ledger_handoff(self):
        options = e2e.browser_options(self.config, self.args)
        options['resources'] = [{'app': 'shell'}]
        self.config['development'] = {'frontend_bundle': {'applications': {'shell': {'repository': '.'}}}}
        target = {'gateway_url': 'http://deployment.example.test:31111/', 'verification_path': '/version',
                  'registry': 'registry.example.test', 'image_repository': 'team/backend'}
        deployment = {'smart-geo': {'sha': 'a' * 40, 'target': target, 'status': 'deployed', 'service': 'smart-geo',
                                   'digest': 'sha256:' + 'a' * 64, 'image': 'registry.example.test/team/backend:1.0.1',
                                   'verification': {'sha': 'a' * 40}}}
        route = {'scope': 'fullstack', 'stages': ['backend.deploy', 'handoff', 'frontend.e2e']}
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory)
            (candidate / 'package.json').write_text('{}', encoding='utf-8')
            proof = {'source_root': str(self.workspace), 'worktree': str(candidate)}
            stages = {
                'backend.deploy': {'outcome': 'success', 'evidence': {'services': deployment}},
                'handoff': {'outcome': 'success', 'evidence': {'deployment': deployment,
                            'gateway_url': target['gateway_url'], 'backend_versions': {'smart-geo': 'a' * 40}}},
                'frontend.branch_check': {'evidence': {'repositories': {'shell': proof}}},
                'frontend.coding': {'evidence': {'versions': {'shell': 'b' * 40}}},
            }
            connection = Mock()
            connection.execute.return_value.fetchone.side_effect = [
                {'seq': 1, 'run_round': 1, 'state': 'running', 'stage': 'frontend.e2e'},
                {'phase': 'verify', 'attempt_id': 'current'},
                {'seq': 1, 'run_round': 1, 'state': 'running', 'stage': 'frontend.e2e'},
                {'phase': 'verify', 'attempt_id': 'current'},
            ]
            args = argparse.Namespace(marker='HNF-test', startup_log=candidate / 'frontend.log',
                                      candidate_workspace=None)
            with (patch.object(e2e.sqlite3, 'connect', return_value=connection),
                  patch('deploy_state.configured_targets', return_value={'smart-geo': target}),
                  patch('route_execution.read_history', return_value=(route, stages)),
                  patch('route_evidence.check_candidate', return_value=None) as verify):
                self.assertEqual(e2e.acceptance_setup(self.config, options, args), deployment)
                verify.assert_called_once_with(proof, 'b' * 40)
                self.assertEqual(options['startupWorkspace'], str(candidate.resolve()))
                self.assertEqual(options['gatewayUrl'], target['gateway_url'])
                stages['handoff']['evidence']['backend_versions']['smart-geo'] = 'c' * 40
                with self.assertRaisesRegex(ValueError, 'versions do not match'):
                    e2e.acceptance_setup(self.config, options, args)


if __name__ == '__main__':
    unittest.main()
