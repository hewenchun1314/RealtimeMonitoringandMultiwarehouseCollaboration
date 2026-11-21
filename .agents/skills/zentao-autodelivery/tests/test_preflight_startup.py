import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import playwright_e2e as e2e


class PreflightStartupTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(__file__).resolve().parent
        self.config = {
            'workspaces': {'frontend': str(self.workspace)},
            'e2e': {
                'workspace': 'frontend', 'start_command': 'npm run serve-shell-hub',
                'login': {'path': '/login'}, 'platform_entry_path': '/platform',
                'default_work_area': {'name': 'Test area'},
                'playwright': {
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


if __name__ == '__main__':
    unittest.main()
