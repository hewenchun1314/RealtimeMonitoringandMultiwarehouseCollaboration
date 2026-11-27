from datetime import datetime, timezone
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from delivery_scope import route_stages
from deploy_control import deployment_action
from frontend_release import DeploymentError, publish, read_remote, release_base, tag_for
from ledger import Ledger, encode, transaction
from execution import attempt_start
from frontend_release_runner import run_frontend_release
from worker import dispatch_work_item


class FrontendRouteTests(unittest.TestCase):
    def test_frontend_release_follows_integration_in_both_frontend_routes(self):
        for scope in ('frontend_only', 'fullstack', 'backend_only'):
            with self.subTest(scope=scope):
                self.assertNotIn('frontend.deploy', route_stages(scope))
                stages = route_stages(scope, frontend_deploy_enabled=True)
                if scope == 'backend_only':
                    self.assertNotIn('frontend.deploy', stages)
                else:
                    self.assertEqual(stages[-2:], ('frontend.integration', 'frontend.deploy'))

    def test_windows_worker_dispatches_the_frontend_publisher_then_resumes_chat(self):
        pending = [{'status': 'deployment_pending', 'stage': 'frontend.deploy'},
                   *[{'status': 'deployment_not_pending'}] * 2]
        launches = [({'receipt_path': 'resumed', 'outcome': 'success', 'stop_confirmed': True}, None)]
        with (patch('worker.deployment_action', side_effect=pending),
              patch('worker.read_item', return_value={'state': 'running', 'chat_id': 'fixture'}),
              patch('worker.runtime_check', return_value={'status': 'runtime_unattended'}),
              patch('worker.run_deployment') as backend,
              patch('worker.run_frontend_release', return_value={'status': 'attempt_finished'}) as frontend):
            result = dispatch_work_item('fixture', lambda _: launches.pop(), recover_deployment=True)
        backend.assert_not_called()
        frontend.assert_called_once()
        self.assertEqual(result['receipt'], 'resumed')
        self.assertEqual(result['frontend_deployment']['status'], 'attempt_finished')


class FrontendPushTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.check_output(['git', *args], cwd=root, text=True,
                                       stderr=subprocess.DEVNULL).strip()

    def test_publish_exact_commit_and_reuse_remote_descendant(self):
        from frontend_release import synchronize_branch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / 'remote.git'
            source = root / 'source'
            source.mkdir()
            self.git(root, 'init', '--bare', str(remote))
            self.git(source, 'init', '-b', 'AI-development')
            self.git(source, 'config', 'user.email', 'fixture@example.invalid')
            self.git(source, 'config', 'user.name', 'fixture')
            self.git(source, 'remote', 'add', 'origin', str(remote))
            self.git(source, 'commit', '--allow-empty', '-m', 'first')
            first = self.git(source, 'rev-parse', 'HEAD')
            self.git(source, 'commit', '--allow-empty', '-m', 'second')
            second = self.git(source, 'rev-parse', 'HEAD')
            self.assertEqual(synchronize_branch(source, 'AI-development', first), first)
            self.assertEqual(synchronize_branch(source, 'AI-development', second), second)
            self.assertEqual(synchronize_branch(source, 'AI-development', first), second)
            self.assertEqual(self.git(remote, 'rev-parse', 'refs/heads/AI-development'), second)
            self.git(source, 'checkout', '-b', 'diverged', first)
            self.git(source, 'commit', '--allow-empty', '-m', 'diverged')
            diverged = self.git(source, 'rev-parse', 'HEAD')
            self.git(source, 'branch', '-f', 'AI-development', diverged)
            with self.assertRaisesRegex(DeploymentError, 'diverged'):
                synchronize_branch(source, 'AI-development', diverged)
            self.assertEqual(self.git(remote, 'rev-parse', 'refs/heads/AI-development'), second)

    def test_remote_tip_moving_during_fetch_retries(self):
        before = 'a' * 40
        after = 'b' * 40
        with (patch('frontend_release.remote_tip', side_effect=[before, after, after, after]),
              patch('frontend_release.git', side_effect=[SimpleNamespace(returncode=0),
                    SimpleNamespace(returncode=0), SimpleNamespace(stdout='commit\n')]) as git):
            self.assertEqual(read_remote(Path('.'), 'refs/heads/AI-development'), after)
        self.assertEqual(git.call_count, 3)


class FrontendPublisherTests(unittest.TestCase):
    config_digest = 'sha256:' + 'c' * 64
    digest = 'sha256:' + 'd' * 64
    request = {
        'marker': 'fixture',
        'settings': {'image_repository': 'registry.example.test/internalrd/web',
                     'base_image': 'registry.example.test/internalrd/web@sha256:' + 'b' * 64,
                     'platform': 'linux/amd64'},
        'applications': {
            'aggregation-platform': {'repository': 'root', 'branch': 'AI-development', 'sha': 'a' * 40},
            'shell-hub': {'repository': 'child', 'branch': 'AI-development', 'sha': 'b' * 40},
        },
    }

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.journal = Path(directory.name) / 'receipt.json'
        self.events = []

    def test_push_then_build_and_retry_from_registry(self):
        observed = {'digest': self.digest, 'config_digest': self.config_digest}

        def synchronize(root, branch, sha):
            self.events.append('source:' + root.name)
            return sha

        def docker(*args):
            self.events.append(args[1])
            if args[1] == 'push':
                saved = json.loads(self.journal.read_text(encoding='utf-8'))
                self.assertEqual(saved['config_digest'], self.config_digest)
                self.assertNotIn('digest', saved)
                raise DeploymentError('launcher interrupted during push')
            return SimpleNamespace(stdout=self.config_digest + ' linux/amd64')

        with (patch('frontend_release.verify_toolchain'),
              patch('frontend_release.synchronize_branch', side_effect=synchronize),
              patch('frontend_release.build_archive',
                    side_effect=lambda *_: self.events.append('build') or Path('fixture.tar')) as build,
              patch('frontend_release.archive_config', return_value=self.config_digest),
              patch('frontend_release.command', side_effect=docker),
              patch('frontend_release.remote_image', side_effect=[None, observed])):
            with self.assertRaisesRegex(DeploymentError, 'interrupted'):
                publish(self.request, self.journal)
            reuse = {**self.request, 'settings': {**self.request['settings'], 'image_mode': 'reuse'}}
            publish(reuse, self.journal)
        self.assertEqual(self.events, ['source:root', 'source:child', 'build', 'load', 'image',
                                       'tag', 'push', 'source:root', 'source:child'])
        build.assert_called_once()
        receipt = json.loads(self.journal.read_text(encoding='utf-8'))
        self.assertEqual(receipt['digest'], self.digest)
        self.assertTrue(receipt['image'].endswith('-zentao-ai-development'))

    def test_build_permitted_with_unrelated_live_frontend(self):
        import socket
        with (socket.socket() as external, patch('frontend_release.verify_toolchain'),
              patch('frontend_release.synchronize_branch', side_effect=lambda _, __, sha: sha),
              patch('frontend_release.remote_image', return_value=None),
              patch('frontend_release.build_archive', side_effect=DeploymentError('reached build')) as build):
            external.bind(('127.0.0.1', 0))
            external.listen()
            request = {**self.request, 'settings': {**self.request['settings'], 'local_ports': [external.getsockname()[1]]}}
            with self.assertRaisesRegex(DeploymentError, 'reached build'):
                publish(request, self.journal)
            build.assert_called_once()
            with socket.create_connection(external.getsockname(), timeout=1):
                pass

    def test_tag_and_digest_baseline_keeps_the_original_template_for_rollout(self):
        settings = {**self.request['settings'], 'rancher': {'container': 'web'}}
        repo = settings['image_repository']
        before = repo + ':100.1.16-release@' + self.digest
        with (patch('frontend_release.current_image', return_value=before),
              patch('frontend_release.remote_image', return_value={'digest': self.digest}) as read):
            self.assertEqual(release_base(settings, {}), (repo + '@' + self.digest, before))
            read.assert_called_once_with(before, settings['platform'])
        for invalid in [before.replace(repo, repo + '-other'), before + '!', repo + ':@' + self.digest]:
            with (self.subTest(invalid=invalid), patch('frontend_release.current_image', return_value=invalid),
                  patch('frontend_release.remote_image') as read, self.assertRaises(DeploymentError)):
                try:
                    release_base(settings, {})
                finally:
                    read.assert_not_called()

    def test_direct_rollout_resumes_after_patch_without_pushing_or_patching_twice(self):
        target = {'kube_server': 'https://rancher.example/k8s/clusters/test', 'kube_context': 'test'}
        settings = {**self.request['settings'], 'rancher': target}
        request = {**self.request, 'settings': settings}
        repo = settings['image_repository']
        base_digest = 'sha256:' + 'e' * 64
        before, after = repo + '@' + base_digest, repo + '@' + self.digest
        observed = {'digest': self.digest, 'config_digest': self.config_digest, 'platform_digest': self.digest}
        with (patch('frontend_release.Kubernetes') as kubernetes,
              patch('frontend_release.verify_toolchain'), patch('frontend_release.synchronize_branch', return_value='a'*40),
              patch('frontend_release.build_archive', return_value=Path('fixture.tar')) as build,
              patch('frontend_release.archive_config', return_value=self.config_digest),
              patch('frontend_release.command', return_value=SimpleNamespace(stdout=self.config_digest + ' linux/amd64')) as command,
              patch('frontend_release.remote_image', side_effect=[{'digest': base_digest}, None, observed, observed]),
              patch('frontend_release.update_and_verify') as mcp):
            kube = kubernetes.return_value
            kube.uid = 'original'
            kube.preflight.return_value = before
            kube.current_image.side_effect = [before, after]
            kube.verify_rollout.side_effect = [DeploymentError('read interrupted'), {'image': after}]
            with self.assertRaisesRegex(DeploymentError, 'read interrupted'):
                publish(request, self.journal)
            publish(request, self.journal)
            kube.update.assert_called_once_with(before, after)
            self.assertEqual(kube.verify_rollout.call_count, 2)
            self.assertEqual(sum(call.args[:2] == ('docker', 'push') for call in command.call_args_list), 1)
            self.assertEqual(json.loads(self.journal.read_text())['deployment_uid'], 'original')
            build.assert_called_once()
            mcp.assert_not_called()

    def test_partial_bundle_uses_live_base_and_reuses_harbor_after_rollout_failure(self):
        settings = {**self.request['settings'], 'rancher': {
            'mcp_url': 'http://127.0.0.1:40140/mcp', 'namespace': 'internalrd',
            'deployment': 'web', 'container': 'web', 'cluster_id': 'cluster',
        }}
        request = {**self.request, 'settings': settings}
        repo = settings['image_repository']
        live_base = 'sha256:' + 'e' * 64
        old_ref = repo + '@' + live_base
        new_ref = repo + '@' + self.digest
        observed = {'digest': self.digest, 'config_digest': self.config_digest}
        proof = {'image': new_ref, 'ready_replicas': 1, 'previous_pods': ['old'], 'pods': ['new']}

        def build(release, _):
            self.assertEqual(release['settings']['base_image'], old_ref)
            return Path('fixture.tar')

        def deploy(target, image, receipt, record):
            self.assertEqual(target, settings['rancher'])
            self.assertEqual(image, new_ref)
            self.assertEqual(receipt['expected_previous_image'], old_ref)
            if 'mcp_baseline' not in receipt:
                record(mcp_baseline={'previous_image': old_ref, 'previous_pods': ['old'], 'desired_replicas': 1})
                raise DeploymentError('rollout interrupted')
            return proof

        with (patch('frontend_release.verify_toolchain'),
              patch('frontend_release.synchronize_branch', side_effect=lambda _, __, sha: sha),
              patch('frontend_release.current_image', return_value=old_ref) as current,
              patch('frontend_release.build_archive', side_effect=build) as archive,
              patch('frontend_release.archive_config', return_value=self.config_digest),
              patch('frontend_release.command', return_value=SimpleNamespace(
                  stdout=self.config_digest + ' linux/amd64')),
              patch('frontend_release.remote_image', side_effect=[
                  {'digest': live_base}, None, observed, observed, {**observed, 'digest': 'sha256:' + 'f' * 64}]),
              patch('frontend_release.update_and_verify', side_effect=deploy)):
            with self.assertRaisesRegex(DeploymentError, 'interrupted'):
                publish(request, self.journal)
            publish(request, self.journal)
            with self.assertRaisesRegex(DeploymentError, 'digest changed'):
                publish(request, self.journal)
        current.assert_called_once()
        archive.assert_called_once()
        receipt = json.loads(self.journal.read_text(encoding='utf-8'))
        self.assertEqual(receipt['base_image'], old_ref)
        self.assertEqual(receipt['rancher'], {'target': settings['rancher'], 'pod_verification': proof})


class FrontendLedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_path = self.root / '.agents' / 'zentao-project.json'
        self.config_path.parent.mkdir()
        self.database = self.root / 'ledger.sqlite3'
        self.config = json.loads((Path(__file__).resolve().parents[3] / 'zentao-project.json').read_text(
            encoding='utf-8'))
        self.config['runtime']['split_task_sync'] = False
        settings = {**FrontendPublisherTests.request['settings'], 'enabled': True}
        self.config['development']['frontend_bundle']['deployment'] = settings
        self.config_path.write_text(encode(self.config), encoding='utf-8')
        self.sha = 'a' * 40

    def setup_item(self, seq, *, validated=True, post_merge=False):
        marker = f'fixture-{seq}'
        stages = route_stages('frontend_only', frontend_deploy_enabled=True, post_merge_e2e=post_merge)
        route = {'scope': 'frontend_only', 'source': 'config', 'tasks': {'backend': [], 'frontend': []},
                 'e2e_enabled': True, 'blockers': [], 'stages': stages, 'post_merge_e2e': post_merge}
        before = {'split': {}, 'frontend.plan': {'applications': ['shell-hub']},
                  'frontend.integration': {'merged_versions': {'shell-hub': self.sha}}}
        with transaction(self.database) as conn:
            conn.execute("""INSERT INTO work_items(seq,instance,project_id,kind,item_id,title,assigned_to,
                         source_status,eligible,observed_at,state,marker,config_snapshot,chat_id,request_id,stage)
                         VALUES(?, 'fixture', '1','story',?,'task','worker','active',1,'','running',?,?,?,?,
                                'frontend.deploy')""",
                         (seq, str(seq), marker, encode({**self.config, 'workflow_version': 2}),
                          f'chat-{seq}', f'request-{seq}'))
            conn.execute("INSERT INTO dispatches VALUES(?,?,'desktop','started','')", (f'request-{seq}', seq))
            for stage in stages[:-1]:
                result = {'summary': stage, 'evidence': before.get(stage, {}),
                          'phase': 'verify', 'terminal_step': True}
                if stage == 'split':
                    result['route'] = route
                failed = stage == 'frontend.e2e' and not validated
                if stage == 'frontend.e2e' and post_merge:
                    result['release_versions'] = {'shell-hub': self.sha}
                conn.execute("""INSERT INTO attempts(attempt_id,item_seq,run_round,stage,retry_number,
                             started_at,ended_at,outcome,result,phase,phase_started_at)
                             VALUES(?,?,1,?,0,'','',?,?,'verify','')""",
                             (f'{marker}-{stage}', seq, stage, 'failed' if failed else 'success', encode(result)))
        return marker

    def action(self, operation, marker, **kwargs):
        return deployment_action(operation, marker, self.database, self.config_path, **kwargs)

    def test_claim_filters_failed_e2e_and_blocks_direct_agent_attempt(self):
        marker = self.setup_item(1, validated=False)
        with transaction(self.database) as conn:
            item = conn.execute('SELECT * FROM work_items WHERE marker=?', (marker,)).fetchone()
            fields = {'item': {'instance': 'fixture', 'project_id': '1', 'kind': 'story', 'id': '1'},
                      'request_id': 'request-1', 'chat_id': 'chat-1', 'stage': 'frontend.deploy'}
            direct = attempt_start(Ledger(conn, self.config, datetime.now(timezone.utc)), item, fields,
                                   config_directory=self.config_path.parent)
        self.assertEqual(direct['status'], 'deployment_worker_required')
        with patch('frontend_release_runner.run_stage') as run:
            result = run_frontend_release(marker, self.database, self.config_path, self.root)
        run.assert_not_called()
        self.assertEqual(result['status'], 'attempt_finished')
        self.assertEqual((result['outcome'], result['disposition']), ('skipped', 'close'))

    def test_post_merge_terminal_e2e_failure_still_claims_frontend_deployment(self):
        marker = self.setup_item(1, validated=False, post_merge=True)
        claimed = self.action('claim', marker)
        self.assertEqual(claimed['status'], 'deployment_claimed', claimed)
        self.assertIsNone(claimed['blocked'])
        self.assertEqual(claimed['frontend_request']['applications']['shell-hub']['sha'], self.sha)

    def test_runner_records_harbor_receipt_and_unlocks_repository(self):
        marker = self.setup_item(1)
        other = self.setup_item(2)

        def execute(attempt_id, phase, deadline, cwd, output, prompt, argv, **_):
            self.assertEqual(self.action('claim', other)['status'], 'deployment_busy')
            request = json.loads(Path(argv[-3]).read_text(encoding='utf-8'))
            sha = request['applications']['shell-hub']['sha']
            self.assertEqual(sha, self.sha)
            receipt = {'status': 'published', 'image': tag_for(request),
                       'versions': {'shell-hub': sha}, 'remote_versions': {'shell-hub': sha},
                       'base_image': request['settings']['base_image'],
                       'config_digest': FrontendPublisherTests.config_digest, 'digest': FrontendPublisherTests.digest}
            Path(argv[-1]).write_text(encode(receipt), encoding='utf-8')
            return {'attempt_id': attempt_id, 'phase': phase, 'outcome': 'success', 'stop_confirmed': True,
                    'stop_evidence': {'owned_processes_active_after_stop': 0}}

        self.assertEqual(self.action('peek', marker)['stage'], 'frontend.deploy')
        with patch('frontend_release_runner.run_stage', side_effect=execute):
            result = run_frontend_release(marker, self.database, self.config_path, self.root)
        self.assertEqual(result['status'], 'attempt_finished')
        self.assertEqual(self.action('claim', other)['status'], 'deployment_claimed')
