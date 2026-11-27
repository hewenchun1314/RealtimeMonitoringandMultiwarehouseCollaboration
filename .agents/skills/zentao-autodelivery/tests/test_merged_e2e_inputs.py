import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import agentlimb_e2e as e2e
import merged_e2e_sources as sources
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

    def test_only_scope_startup_when_all_pages_and_candidates_are_covered(self):
        extra = self.child.parent / 'extra'
        git(self.workspace, 'clone', '--local', str(self.child), str(extra))
        apps = self.config['development']['frontend_bundle']['applications']
        apps['extra'] = {'repository': 'children/extra', 'branch': 'target'}
        self.config['e2e'] = {'startup_routes': {'/plot': {'shell': ['serve', 5000], 'app': ['dev', 5050]}}}
        scenario = self.log.with_name('scenario.json')
        self.options.update(automaticAttestation=True, entryPath='/plot', scenario=str(scenario))
        for page, candidate, scoped in [('/plot', 'app', True), ('/other', 'app', False), ('/plot', 'extra', False)]:
            with self.subTest(page=page, candidate=candidate):
                scenario.write_text(json.dumps({'cases': [{'steps': [{'action': 'navigate', 'path': page}]}]}))
                repo = self.workspace / apps[candidate]['repository']
                versions = {candidate: git(repo, 'rev-parse', 'HEAD')}
                branches = {candidate: {'target_branch': 'target', 'source_root': str(repo)}}
                checkouts = sources.merged_workspace(self.options, self.config, branches, versions)
                self.assertEqual(str(extra) in checkouts, not scoped)
                self.assertEqual(self.options.get('startupServices'),
                                 [('.', 'serve', 5000), ('children/app', 'dev', 5050)] if scoped else None)

    def prepare(self, port=5000):
        options = self.options
        options['mergedCheckouts'] = e2e.merged_workspace(options, self.config, self.branches, self.versions)
        with (patch.object(e2e, 'port_open', side_effect=[False, True]),
              patch.object(e2e, 'http_ready', return_value=True),
              patch.object(e2e, 'launch_frontend', return_value=Mock(pid=42, poll=lambda: None)) as launch):
            result = e2e.prepare_frontend({'start_command': ['npm', 'run', 'serve'], 'startup_ports': [port]}, options, self.log)
        self.assertEqual(result['mode'], 'started')
        self.assertEqual(launch.call_args.args[1], result['source_workspace'])
        self.launch_argv = e2e.resolve_command(launch.call_args.args[0])
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
        output = subprocess.check_output(self.launch_argv, cwd=compiled, env=self.launch_env, text=True)
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

    def test_short_checkout_runs_three_npm_layers_and_marks_the_served_candidate(self):
        stage = self.log.parent / 'items' / ('a' * 24) / 'frontend' / ('e2e-' + 'b' * 32) / 'host-run' / ('b' * 32) / 'host'
        stage.mkdir(parents=True)
        self.log = stage / 'frontend.log'
        self.options.update(automaticAttestation=True, appPaths={'app': 'children/app'})
        (self.workspace / 'package.json').write_text(json.dumps({'scripts': {
            'serve': 'npm run children', 'children': 'npm run serve --prefix children/app'}}))
        # Use an ephemeral port for the loopback server and authorize just that
        # port in the preload before the third npm process starts.
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        (self.child / 'run.cjs').write_text("""
const http = require('node:http');
const port = Number(process.env.TEST_PORT);
const server = http.createServer((req, res) => {
  res.setHeader('Content-Type', 'application/javascript');
  res.end(require('node:fs').readFileSync('input.js'));
});
server.listen(port, '127.0.0.1', async () => {
  try {
    const r = await fetch('http://127.0.0.1:' + port + '/asset.js');
    console.log(JSON.stringify([await r.text(), r.headers.get('Server-Timing')]));
  } finally { server.close(); }
});
""", encoding='utf-8')
        for repo in (self.workspace, self.child):
            git(repo, 'commit', '-am', 'nested npm startup')
        self.versions['app'] = git(self.child, 'rev-parse', 'HEAD')
        self.options['baseUrl'] = f'http://127.0.0.1:{port}'
        compiled = self.prepare(port)
        self.addCleanup(remove_committed_sources, stage)
        self.assertEqual(compiled.parent, Path(tempfile.gettempdir()).resolve())
        self.assertFalse(compiled.is_relative_to(stage))
        self.assertNotEqual(sources.short_checkout_paths(stage.parent), sources.short_checkout_paths(stage))
        output = subprocess.check_output(self.launch_argv, cwd=compiled, timeout=45, text=True,
                                         env={**self.launch_env, 'TEST_PORT': str(port)})
        body, timing = json.loads(output.splitlines()[-1])
        self.assertEqual(body, 'committed input')
        self.assertIn(self.options['launchProof']['token'] + '.app', timing)
        verify_runtime_sources(self.options, compiled)
        self.assertEqual(remove_committed_sources(stage), 'removed')
        self.assertFalse(any(path.exists() for path in sources.short_checkout_paths(stage)))
        self.assertTrue((self.child / 'node_modules/fixture.txt').is_file())

    def test_recovery_verifies_the_short_checkout_owner_before_deleting(self):
        self.options.update(automaticAttestation=True, appPaths={'app': 'children/app'})
        compiled = self.prepare()
        stage = self.log.parent
        owner = stage / 'frontend-short.owner'
        recorded = owner.read_bytes()
        try:
            owner.write_bytes(b'wrong attempt')
            self.assertEqual(remove_committed_sources(stage), 'ownership-changed')
            self.assertTrue((compiled / 'children/app/input.js').is_file())
        finally:
            owner.write_bytes(recorded)
            self.assertEqual(remove_committed_sources(stage), 'removed')

    @unittest.skipUnless(os.name == 'nt', 'Exercises Windows host path limits')
    def test_deep_host_directory_prepares_recorded_sources_and_preserves_local_edits(self):
        # Real item/attempt host directories put nested repository hook templates
        # beyond MAX_PATH before clone has written its repository configuration.
        desired_destination_length = 218
        suffix_size = desired_destination_length - len(str(self.log.parent)) - len('/frontend-source') - 1
        self.assertGreater(suffix_size, 0)
        stage = self.log.parent / ('h' * suffix_size)
        stage.mkdir()
        self.log = stage / 'frontend.log'
        renamed = self.workspace / 'children' / 'micro-double-sweet-spot'
        self.assertTrue(self.child.resolve().is_relative_to(self.workspace.resolve()))
        self.assertTrue(renamed.resolve().is_relative_to(self.workspace.resolve()))
        self.child.rename(renamed)
        self.child = renamed
        self.branches['app']['source_root'] = str(self.child)
        relative = 'children/' + self.child.name
        self.config['development']['frontend_bundle']['applications']['app']['repository'] = relative
        source = self.child / 'input.js'
        source.write_bytes(b'local draft must survive')

        compiled = self.prepare()

        self.assertEqual(len(str(compiled)), desired_destination_length)
        self.assertEqual((compiled / relative / 'input.js').read_bytes(), b'committed input')
        self.assertEqual(git(compiled / relative, 'cat-file', '-t', self.versions['app']), 'commit')
        self.assertEqual(source.read_bytes(), b'local draft must survive')
        verify_runtime_sources(self.options, compiled)
        self.assertEqual(remove_committed_sources(stage), 'removed')
        self.assertFalse((stage / 'frontend-git').exists())
        self.assertTrue((self.child / 'node_modules/fixture.txt').is_file())

    def test_owned_source_and_git_storage_can_be_cleaned_after_clone_failure(self):
        (self.child / 'input.js').write_bytes(b'local draft')
        self.options['mergedCheckouts'] = e2e.merged_workspace(
            self.options, self.config, self.branches, self.versions)
        actual_git = sources.git
        clones = []

        def fail_second_clone(root, *args, **kwargs):
            if 'clone' in args:
                clones.append(args)
                if len(clones) == 2:
                    raise ValueError('simulated clone failure')
            return actual_git(root, *args, **kwargs)

        with patch.object(sources, 'git', side_effect=fail_second_clone):
            with self.assertRaisesRegex(ValueError, 'simulated clone failure'):
                sources.prepare_committed_sources(self.options, self.log.parent)
        self.assertTrue((self.log.parent / 'frontend-git').is_dir())

        self.assertEqual(remove_committed_sources(self.log.parent), 'removed')

        self.assertFalse((self.log.parent / 'frontend-source').exists())
        self.assertFalse((self.log.parent / 'frontend-git').exists())
        self.assertEqual((self.child / 'input.js').read_bytes(), b'local draft')
        self.assertTrue((self.child / 'node_modules/fixture.txt').is_file())

    def test_cleanup_preserves_sources_if_git_storage_ownership_changed(self):
        (self.child / 'input.js').write_bytes(b'local draft')
        compiled = self.prepare()
        storage = self.log.parent / 'frontend-git'
        self.assertTrue(storage.is_dir())
        (self.log.parent / 'frontend-git.owner').write_text('changed owner', encoding='ascii')

        self.assertEqual(remove_committed_sources(self.log.parent), 'ownership-changed')

        self.assertTrue(compiled.is_dir())
        self.assertTrue(storage.is_dir())
        self.assertEqual((self.child / 'node_modules/fixture.txt').read_text(), 'installed')

    def test_cleanup_can_resume_after_git_storage_removed_but_source_removal_failed(self):
        (self.child / 'input.js').write_bytes(b'local draft')
        compiled = self.prepare()
        storage = self.log.parent / 'frontend-git'
        self.assertTrue(storage.is_dir())
        actual_remove = sources.shutil.rmtree

        def fail_source(path, *args, **kwargs):
            if Path(path) == compiled:
                raise OSError('simulated interrupted source cleanup')
            return actual_remove(path, *args, **kwargs)

        with patch.object(sources.shutil, 'rmtree', side_effect=fail_source):
            self.assertEqual(remove_committed_sources(self.log.parent), 'retained')
        self.assertFalse(storage.exists())
        self.assertTrue(compiled.exists())
        self.assertEqual(remove_committed_sources(self.log.parent), 'removed')
        self.assertFalse(compiled.exists())
        self.assertTrue((self.child / 'node_modules/fixture.txt').is_file())

    def test_legacy_source_only_cleanup_remains_supported(self):
        compiled = self.log.parent / 'frontend-source'
        compiled.mkdir()
        (compiled / 'owned.txt').write_bytes(b'old attempt')
        (self.log.parent / 'frontend-source.owner').write_text(str(compiled.stat().st_ino), encoding='ascii')

        self.assertEqual(remove_committed_sources(self.log.parent), 'removed')

        self.assertFalse(compiled.exists())

    def test_cleanup_resumes_when_git_owner_unlink_failed_after_directory_removal(self):
        (self.child / 'input.js').write_bytes(b'local draft')
        compiled = self.prepare()
        owner = self.log.parent / 'frontend-git.owner'
        actual_unlink = Path.unlink
        failed = []

        def fail_once(path, *args, **kwargs):
            if path == owner and not failed:
                failed.append(True)
                raise OSError('simulated owner unlink failure')
            return actual_unlink(path, *args, **kwargs)

        with patch.object(Path, 'unlink', fail_once):
            self.assertEqual(remove_committed_sources(self.log.parent), 'retained')
            self.assertFalse((self.log.parent / 'frontend-git').exists())
            self.assertTrue(owner.exists())
            self.assertTrue(compiled.exists())
            self.assertEqual(remove_committed_sources(self.log.parent), 'removed')
        self.assertFalse(owner.exists())
        self.assertFalse(compiled.exists())
        self.assertEqual((self.child / 'input.js').read_bytes(), b'local draft')
        self.assertTrue((self.child / 'node_modules/fixture.txt').is_file())

    def run_with_browser(self, browser):
        self.options['mergedCheckouts'] = e2e.merged_workspace(self.options, self.config, self.branches, self.versions)
        self.options['candidateVersions'] = self.versions
        config = {'e2e': {'start_command': 'npm run serve', 'startup_ports': [5000]}}
        with (patch.object(e2e, 'port_open', side_effect=[False, True]),
              patch.object(e2e, 'http_ready', return_value=True),
              patch.object(e2e, 'launch_frontend', return_value=Mock(pid=42, poll=lambda: None)),
              patch.object(e2e, 'browser_result', side_effect=browser) as visited):
            result = e2e.run_browser(config, self.options, SimpleNamespace(startup_log=self.log, scenario=True))
        return result, visited.call_count

    def test_local_edit_during_scenario_does_not_change_the_running_checkout(self):
        def browser(options, args):
            (self.child / 'input.js').write_bytes(b'changed while testing')
            pinned = self.log.parent / 'frontend-source'
            self.assertEqual((pinned / 'children/app/input.js').read_bytes(), b'committed input')
            return {'status': 'passed'}
        result, calls = self.run_with_browser(browser)
        self.assertEqual(calls, 1)
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
                        if when == 'scenario':
                            hide_edit()
                        return {'status': 'passed'}

                    if when == 'startup':
                        hide_edit()
                    result, calls = self.run_with_browser(browser)
                    self.assertEqual(result['status'], 'passed')
                    self.assertEqual(calls, 1)
                    pinned = Path(result['frontend']['source_workspace'])
                    self.assertEqual((pinned / 'children/app/input.js').read_bytes(), b'committed input')
                    self.assertEqual(source.read_bytes(), b'hidden draft')
                    self.assertEqual(remove_committed_sources(self.log.parent), 'removed')
                    source.write_bytes(b'committed input')
                    git(self.child, 'update-index', '--no-' + flag, 'input.js')
