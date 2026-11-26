import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import agentlimb_e2e as e2e
from merged_e2e_sources import check_merged_checkout, remove_committed_sources, verify_runtime_sources


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


class MergedInputsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name, 'main workspace')
        self.child = self.workspace / 'children' / 'app'
        self.log = Path(temp.name, 'frontend.log')
        for root in (self.workspace, self.child):
            root.mkdir(parents=True)
            git(root, 'init', '-b', 'target')
            git(root, 'config', 'user.name', 'E2E fixture')
            git(root, 'config', 'user.email', 'fixture@example.invalid')
            (root / '.gitignore').write_text('children/\nnode_modules/\n')
            serve_script = 'npm run serve --prefix children/app' if root == self.workspace else 'node run.cjs'
            (root / 'package.json').write_text(json.dumps({'scripts': {'serve': serve_script}}))
            (root / 'run.cjs').write_text("console.log(require('node:fs').readFileSync('input.js', 'utf8'))")
            (root / 'input.js').write_text('committed input')
            git(root, 'add', '.')
            git(root, 'commit', '-m', 'fixture')
            (root / 'node_modules').mkdir()
            (root / 'node_modules' / 'fixture.txt').write_text('installed')
        self.versions = {'app': git(self.child, 'rev-parse', 'HEAD')}
        self.branches = {'app': {'target_branch': 'target', 'source_root': str(self.child)}}
        self.config = {'development': {'frontend_bundle': {'applications': {
            'shell': {'repository': '.', 'branch': 'target'},
            'app': {'repository': 'children/app', 'branch': 'target'}}}}}
        self.options = {'workspace': str(self.workspace), 'startupWorkspace': str(self.workspace),
                        'baseUrl': 'http://localhost:5000', 'loginPath': '/login', 'postMerge': True}

    def prepare(self):
        options = self.options
        options['mergedCheckouts'] = e2e.merged_workspace(options, self.config, self.branches, self.versions)
        with (patch.object(e2e, 'stop_frontend_ports', return_value=[]),
              patch.object(e2e, 'port_open', return_value=True),
              patch.object(e2e, 'http_ready', return_value=True),
              patch.object(e2e, 'launch_frontend', return_value=Mock(pid=42, poll=lambda: None)) as launch):
            result = e2e.prepare_frontend({'start_command': 'npm run serve', 'startup_ports': [5000]}, options, self.log)
        self.assertEqual(result['mode'], 'started')
        self.assertEqual(launch.call_args.args[1], str(self.workspace))
        self.assertIn('npm --prefix ', launch.call_args.args[0])
        self.launch_argv = ['pwsh', '-NoProfile', '-NonInteractive', '-Command', launch.call_args.args[0]]
        self.launch_env = launch.call_args.args[3]['env']
        return Path(result['source_workspace'])

    def test_dirty_inputs_start_from_recorded_commits_without_reusing_live_service(self):
        source = self.child / 'input.js'
        source.write_text('staged input')
        git(self.child, 'add', 'input.js')
        source.write_text('unstaged input')
        compiled = self.prepare()
        self.assertEqual((compiled / 'children/app/input.js').read_text(), 'committed input')
        self.assertTrue((compiled / 'children/app/node_modules').samefile(self.child / 'node_modules'))
        self.assertTrue((compiled / 'node_modules').samefile(self.workspace / 'node_modules'))
        self.assertEqual(git(self.child, 'show', ':input.js'), 'staged input')
        self.assertEqual(source.read_text(), 'unstaged input')
        output = subprocess.check_output(self.launch_argv, cwd=self.workspace, env=self.launch_env, text=True)
        self.assertEqual(output.splitlines()[-1], 'committed input')
        verify_runtime_sources(self.options, compiled)
        (compiled / 'children/app/input.js').write_text('changed during acceptance')
        with self.assertRaisesRegex(ValueError, 'sources changed'):
            verify_runtime_sources(self.options, compiled)
        self.assertEqual(remove_committed_sources(self.log.parent), 'removed')
        self.assertTrue((self.child / 'node_modules' / 'fixture.txt').is_file())
        self.assertFalse(compiled.exists())

    def test_dirty_other_branch_reads_target_ref_and_final_check_rejects_ref_movement(self):
        git(self.child, 'switch', '-c', 'user')
        (self.child / 'input.js').write_text('local feature')
        git(self.child, 'commit', '-am', 'feature')
        compiled = self.prepare()
        self.assertEqual((compiled / 'children/app/input.js').read_text(), 'committed input')
        self.assertEqual(git(self.child, 'branch', '--show-current'), 'user')
        git(self.child, 'branch', '-f', 'target', 'HEAD')
        with self.assertRaises(ValueError):
            for checkout in self.options['mergedCheckouts'].values():
                check_merged_checkout(**checkout)

    def run_with_browser(self, browser):
        self.options['mergedCheckouts'] = e2e.merged_workspace(self.options, self.config, self.branches, self.versions)
        self.options['candidateVersions'] = self.versions
        config = {'e2e': {'start_command': 'npm run serve', 'startup_ports': [5000]}}
        with (patch.object(e2e, 'port_open', return_value=True),
              patch.object(e2e, 'http_ready', return_value=True),
              patch.object(e2e, 'stop_frontend_ports', return_value=[]),
              patch.object(e2e, 'launch_frontend', return_value=Mock(pid=42, poll=lambda: None)),
              patch.object(e2e, 'browser_result', side_effect=browser) as visited):
            result = e2e.run_browser(config, self.options, SimpleNamespace(startup_log=self.log, scenario=True))
        return result, visited.call_count

    def test_local_edit_during_scenario_rechecks_business_result_on_pinned_sources_once(self):
        def browser(options, args):
            if options.get('forceCommittedSources'):
                pinned = self.log.parent / 'frontend-source'
                self.assertEqual((pinned / 'children/app/input.js').read_bytes(), b'committed input')
                return {'status': 'passed'}
            (self.child / 'input.js').write_bytes(b'changed while testing')
            return {'status': 'failed', 'stage': 'acceptance', 'repair_required': True}
        result, calls = self.run_with_browser(browser)
        self.assertEqual(calls, 2)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['versions'], self.versions)

    def test_target_drift_with_a_simultaneous_shell_edit_blocks_without_retry(self):
        def browser(options, args):
            (self.workspace / 'input.js').write_bytes(b'new draft')
            git(self.child, 'commit', '--allow-empty', '-m', 'new release')
            return {'status': 'passed'}
        result, calls = self.run_with_browser(browser)
        self.assertEqual(calls, 1)
        self.assertEqual(result['reason'], 'candidate-changed-during-acceptance')

    def test_hidden_index_edits_use_committed_sources_at_start_and_after_a_live_scenario(self):
        source = self.child / 'input.js'
        for flag in ('assume-unchanged', 'skip-worktree'):
            for when in ('startup', 'scenario'):
                with self.subTest(flag=flag, when=when):
                    def hide_edit():
                        git(self.child, 'update-index', '--' + flag, 'input.js')
                        source.write_bytes(b'hidden draft')
                        self.assertEqual(git(self.child, 'status', '--porcelain'), '')

                    def browser(options, args):
                        if when == 'scenario' and not options.get('forceCommittedSources'):
                            hide_edit()
                        return {'status': 'passed'}

                    if when == 'startup':
                        hide_edit()
                    result, calls = self.run_with_browser(browser)
                    self.assertEqual(result['status'], 'passed')
                    self.assertEqual(calls, 2 if when == 'scenario' else 1)
                    pinned = Path(result['frontend']['source_workspace'])
                    self.assertEqual((pinned / 'children/app/input.js').read_bytes(), b'committed input')
                    self.assertEqual(source.read_bytes(), b'hidden draft')
                    self.assertEqual(remove_committed_sources(self.log.parent), 'removed')
                    source.write_bytes(b'committed input')
                    git(self.child, 'update-index', '--no-' + flag, 'input.js')
