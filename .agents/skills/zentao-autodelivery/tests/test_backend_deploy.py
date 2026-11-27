import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import backend_deploy as deploy


SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
CONFIG = 'sha256:' + 'c' * 64
IMAGE = 'registry.example:6443/internalrd/smart-geo-srv:1.2.37'
REFERENCE = IMAGE.rsplit(':', 1)[0] + '@' + DIGEST
PLATFORM_DIGEST = 'sha256:' + 'd' * 64


class BackendDeployTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'publish-version.txt').write_text('smart-geo-srv=1\n', encoding='utf-8')
        self.request = {
            'service': 'smart-geo',
            'sha': SHA,
            'repository': str(self.root / 'smart-geo'),
            'backend_root': str(self.root),
            'receipt_path': str(self.root / 'receipt.json'),
            'service_config': {'build_command': 'mvn -B -pl smart-geo/smart-geo-server -am package'},
            'target': {'registry': 'registry.example:6443', 'image_repository': 'internalrd/smart-geo-srv',
                       'platform': 'linux/amd64', 'kube_context': 'test', 'namespace': 'internalrd',
                       'deployment': 'smart-geo-new', 'container': 'app', 'gateway_url': 'https://test',
                       'verification_path': '/tet/smart-geo/actuator/info'},
        }

    def test_tag_uses_service_version_and_requested_commit(self):
        with patch.object(deploy, 'command', return_value=Mock(stdout='237\n', returncode=0)) as command:
            self.assertEqual(deploy.image_tag(self.request), IMAGE)
        self.assertEqual(command.call_args.args[-2:], ('--count', SHA))

    def test_http_checks_exact_commit(self):
        response = Mock(status=200)
        response.read.return_value = json.dumps({'git': {'commit': {'id': SHA}}}).encode()
        with patch.object(deploy, 'HTTPSConnection') as connection:
            connection.return_value.getresponse.return_value = response
            self.assertEqual(deploy.verify_http(self.request['target'], SHA), {
                'sha': SHA, 'url': 'https://test/tet/smart-geo/actuator/info',
            })
            response.read.return_value = json.dumps({'git': {'commit': {'id': SHA[:7]}}}).encode()
            with self.assertRaisesRegex(deploy.DeploymentError, 'version'):
                deploy.verify_http(self.request['target'], SHA)

    def test_build_context_only_contains_dockerfile_and_verified_runtime_jar(self):
        def workspace(_, temporary):
            source = temporary / 'smart-geo'
            (source / 'smart-geo-server' / 'target').mkdir(parents=True)
            (source / 'Dockerfile').write_text('FROM example\n', encoding='utf-8')
            (source / '.env').write_text('SECRET=test\n', encoding='utf-8')
            (source / 'smart-geo-server' / 'target' / 'server.jar').touch()
            (source / 'smart-geo-server' / 'target' / 'server-sources.jar').touch()
            return temporary, source

        invocations = []

        def command(*args, **kwargs):
            invocations.append(args)
            if args[:3] == ('docker', 'buildx', 'build'):
                self.assertIn('VCS_REF=' + SHA, args)
                self.assertIn('--load', args)
                context = Path(args[-1])
                self.assertEqual({path.relative_to(context).as_posix() for path in context.rglob('*')
                                  if path.is_file()}, {'Dockerfile', 'smart-geo-server/target/server.jar'})

        with (
            patch.object(deploy, 'build_workspace', side_effect=workspace),
            patch.object(deploy, 'command', side_effect=command),
            patch.object(deploy, 'local_image', return_value=CONFIG) as inspect,
        ):
            self.assertEqual(deploy.build_image(self.request, IMAGE), CONFIG)
            inspect.assert_called_once_with(IMAGE, 'linux/amd64', SHA)
        self.assertIn(('mvn', '-B', '-pl', 'smart-geo/smart-geo-server', '-am', 'package'), invocations)

    def test_remote_index_uses_immutable_platform_reference(self):
        base = IMAGE.rsplit(':', 1)[0]
        manifest = {'manifests': [{'digest': PLATFORM_DIGEST,
                                   'platform': {'os': 'linux', 'architecture': 'amd64'}}]}
        config = {'os': 'linux', 'architecture': 'amd64', 'config': {'Labels': {deploy.REVISION: SHA}}}
        outputs = [f'Name: {IMAGE}\nDigest:    {DIGEST}\n', json.dumps(manifest),
                   json.dumps({'config': {'digest': CONFIG}}), json.dumps(config)]
        with patch.object(deploy, 'command', side_effect=[Mock(stdout=out, returncode=0) for out in outputs]) as call:
            self.assertEqual(deploy.remote_image(IMAGE, 'linux/amd64'),
                             {'digest': DIGEST, 'platform_digest': PLATFORM_DIGEST,
                              'config_digest': CONFIG, 'sha': SHA})
        self.assertEqual(call.call_args_list[1].args[-2:], (f'{base}@{DIGEST}', '--raw'))
        self.assertEqual(call.call_args_list[2].args[-2:], (f'{base}@{PLATFORM_DIGEST}', '--raw'))

    def test_remote_image_reads_reference_digest_and_rejects_a_different_digest(self):
        config = {'os': 'linux', 'architecture': 'amd64', 'config': {'Labels': {deploy.REVISION: SHA}}}
        outputs = [f'Digest: {DIGEST}\n', json.dumps({'config': {'digest': CONFIG}}), json.dumps(config)]
        with patch.object(deploy, 'command', side_effect=[Mock(stdout=out, returncode=0) for out in outputs]) as call:
            self.assertEqual(deploy.remote_image(REFERENCE, 'linux/amd64')['sha'], SHA)
            self.assertEqual(call.call_args_list[1].args[-2:], (REFERENCE, '--raw'))
        with patch.object(deploy, 'command', return_value=Mock(stdout=f'Digest: {PLATFORM_DIGEST}\n', returncode=0)):
            with self.assertRaisesRegex(deploy.DeploymentError, 'digest'):
                deploy.remote_image(REFERENCE, 'linux/amd64')

    def test_mcp_release_deploys_harbor_digest_and_checks_health(self):
        target = self.request['target'] = {
            **self.request['target'], 'mcp_url': 'http://mcp.example/mcp',
            'health_url': 'http://service.example/health',
        }
        pod_proof = {'image': REFERENCE, 'ready_replicas': 1, 'pods': ['next'], 'previous_pods': ['old']}

        def rollout(approved, image, progress, record):
            self.assertEqual(approved, target)
            self.assertEqual(image, REFERENCE)
            self.assertEqual(progress['digest'], DIGEST)
            record(mcp_baseline={'image': IMAGE})
            self.assertIn('mcp_baseline', json.loads(Path(self.request['receipt_path']).read_text()))
            return pod_proof

        with (
            patch.object(deploy, 'image_tag', return_value=IMAGE),
            patch.object(deploy, 'build_image', return_value=CONFIG),
            patch.object(deploy, 'remote_image', return_value={'digest': DIGEST,
                         'platform_digest': PLATFORM_DIGEST, 'config_digest': CONFIG, 'sha': SHA}),
            patch.object(deploy, 'Kubernetes') as kube,
            patch.object(deploy, 'deploy_mcp', side_effect=rollout) as update,
            patch.object(deploy, 'verify_mcp_health') as health,
        ):
            report = deploy.publish(self.request)
            kube.assert_not_called()
            update.assert_called_once()
            health.assert_called_once_with(target['health_url'])
        self.assertEqual(report['pod_verification'], pod_proof)
        self.assertEqual(report['verification'], {'sha': SHA, 'url': target['health_url'],
                                                  'provider': 'rancher_mcp', 'image': REFERENCE})

    def test_mcp_health_requires_up_json(self):
        response = Mock(status=200)
        endpoint = 'http://service.example:31191/actuator/health'
        with patch.object(deploy, 'HTTPConnection') as connection:
            connection.return_value.getresponse.return_value = response
            response.read.return_value = b'{"status":"UP"}'
            deploy.verify_mcp_health(endpoint)
            connection.assert_called_once_with('service.example', 31191, timeout=10)
            response.read.return_value = b'{"status":"DOWN"}'
            with self.assertRaisesRegex(deploy.DeploymentError, 'not UP'):
                deploy.verify_mcp_health(endpoint)

    def test_live_mcp_acceptance_detects_image_change(self):
        receipt = {'image': IMAGE, 'digest': DIGEST, 'sha': SHA,
                   'target': {'mcp_url': 'http://mcp.example/mcp', 'health_url': 'http://service.example/health'}}
        with patch.object(deploy, 'verify_mcp_image', side_effect=deploy.DeploymentError('image changed')) as check:
            with self.assertRaisesRegex(deploy.DeploymentError, 'image changed'):
                deploy.verify_published_release(receipt)
        check.assert_called_once_with(receipt['target'], REFERENCE)

    def test_release_pushes_verified_image_and_deploys_digest(self):
        with (
            patch.object(deploy, 'image_tag', return_value=IMAGE),
            patch.object(deploy, 'build_image', return_value=CONFIG),
            patch.object(deploy, 'local_image', return_value=CONFIG),
            patch.object(deploy, 'remote_image', side_effect=[None, None, {'digest': DIGEST,
                             'platform_digest': DIGEST, 'config_digest': CONFIG, 'sha': SHA}]),
            patch.object(deploy, 'command') as command,
            patch.object(deploy, 'Kubernetes') as kube,
            patch.object(deploy, 'verify_http', return_value={'sha': SHA}),
        ):
            kube.return_value.preflight.return_value = 'old'
            kube.return_value.current_image.return_value = 'old'
            kube.return_value.verify_rollout.return_value = {'ready_replicas': 1}
            report = deploy.publish(self.request)
            self.assertEqual(report['status'], 'deployed')
            self.assertEqual(report['digest'], DIGEST)
            command.assert_any_call('docker', 'push', IMAGE)
            kube.return_value.update.assert_called_once_with('old', REFERENCE)
            kube.return_value.verify_rollout.assert_called_once_with(REFERENCE, {DIGEST, CONFIG})
        self.assertEqual(json.loads(Path(self.request['receipt_path']).read_text())['status'], 'deployed')

    def test_rancher_direct_backend_release_and_later_acceptance_verify_digest_and_health(self):
        from deploy_state import deployed_receipt_matches
        target = self.request['target']
        target.update(kube_server='https://rancher.example/k8s/clusters/test', health_url='https://gateway/health')
        target.pop('verification_path')
        observed = {'digest': DIGEST, 'platform_digest': PLATFORM_DIGEST, 'config_digest': CONFIG, 'sha': SHA}
        proof = {'pods': ['new'], 'ready_replicas': 1, 'deployment_uid': 'uid', 'revision': '4', 'image': REFERENCE}
        with (patch.object(deploy, 'image_tag', return_value=IMAGE), patch.object(deploy, 'build_image', return_value=CONFIG),
              patch.object(deploy, 'remote_image', return_value=observed), patch.object(deploy, 'Kubernetes') as kube,
              patch.object(deploy, 'verify_mcp_health') as health, patch.object(deploy, 'deploy_mcp') as mcp):
            api = kube.return_value
            api.uid = 'uid'
            api.preflight.return_value = api.current_image.return_value = 'old'
            api.verify_rollout.return_value = proof
            receipt = deploy.publish(self.request)
            self.assertTrue(deployed_receipt_matches(receipt, 'smart-geo', SHA, target))
            self.assertEqual(deploy.verify_published_release(receipt)['provider'], 'rancher_api')
            api.update.assert_called_once_with('old', REFERENCE)
            self.assertEqual(api.verify_rollout.call_count, 2)
            self.assertEqual(health.call_count, 2)
            mcp.assert_not_called()

    def test_existing_tag_with_different_config_cannot_be_pushed_or_deployed(self):
        with (
            patch.object(deploy, 'image_tag', return_value=IMAGE),
            patch.object(deploy, 'build_image', return_value=CONFIG),
            patch.object(deploy, 'remote_image', return_value={'digest': DIGEST, 'platform_digest': DIGEST,
                             'config_digest': 'sha256:' + 'd' * 64, 'sha': SHA}),
            patch.object(deploy, 'command') as command,
            patch.object(deploy, 'Kubernetes') as kube,
        ):
            kube.return_value.preflight.return_value = 'old'
            with self.assertRaisesRegex(deploy.DeploymentError, 'conflict'):
                deploy.publish(self.request)
            command.assert_not_called()
            kube.return_value.update.assert_not_called()

    def test_resume_reads_remote_and_existing_deployment_without_rebuilding(self):
        Path(self.request['receipt_path']).write_text(json.dumps({
            'service': 'smart-geo', 'sha': SHA, 'target': self.request['target'], 'image': IMAGE,
            'config_digest': CONFIG, 'previous_image': 'old', 'digest': DIGEST,
        }), encoding='utf-8')
        with (
            patch.object(deploy, 'image_tag', return_value=IMAGE),
            patch.object(deploy, 'build_image') as build,
            patch.object(deploy, 'command') as command,
            patch.object(deploy, 'remote_image', return_value={'digest': DIGEST, 'platform_digest': DIGEST,
                             'config_digest': CONFIG, 'sha': SHA}),
            patch.object(deploy, 'Kubernetes') as kube,
            patch.object(deploy, 'verify_http', return_value={'sha': SHA}),
        ):
            kube.return_value.preflight.return_value = REFERENCE
            kube.return_value.current_image.return_value = REFERENCE
            kube.return_value.verify_rollout.return_value = {'ready_replicas': 1}
            self.assertEqual(deploy.publish(self.request)['status'], 'deployed')
            build.assert_not_called()
            command.assert_not_called()
            kube.return_value.update.assert_not_called()

    def test_changed_image_since_preflight_stops_before_kubernetes_update(self):
        with (
            patch.object(deploy, 'image_tag', return_value=IMAGE),
            patch.object(deploy, 'build_image', return_value=CONFIG),
            patch.object(deploy, 'remote_image', return_value={'digest': DIGEST, 'platform_digest': DIGEST,
                             'config_digest': CONFIG, 'sha': SHA}),
            patch.object(deploy, 'Kubernetes') as kube,
        ):
            kube.return_value.preflight.return_value = 'old'
            kube.return_value.current_image.return_value = 'an-outside-publisher'
            with self.assertRaisesRegex(deploy.DeploymentError, 'changed by another release'):
                deploy.publish(self.request)
            kube.return_value.update.assert_not_called()
        receipt = json.loads(Path(self.request['receipt_path']).read_text(encoding='utf-8'))
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['stage'], 'deployment_update')
        self.assertEqual(receipt['previous_image'], 'old')

    def test_deployment_patch_checks_uid_container_and_current_image(self):
        kubernetes = deploy.Kubernetes(self.request['target'])
        metadata = {'uid': 'deployment-uid'}
        spec = {'template': {'spec': {'containers': [{'name': 'app', 'image': 'old'}]}}}
        with patch.object(kubernetes, 'deployment', return_value={'metadata': metadata, 'spec': spec}):
            with patch.object(kubernetes, 'run') as run:
                kubernetes.update('old', REFERENCE)
        commands = json.loads(run.call_args.args[-1])
        self.assertEqual([entry['op'] for entry in commands], ['test', 'test', 'test', 'replace'])
        self.assertEqual([entry['value'] for entry in commands], ['deployment-uid', 'app', 'old', REFERENCE])

    def test_rollout_reads_ready_pods_owned_by_active_replica_set(self):
        kubernetes = deploy.Kubernetes(self.request['target'])
        deployment = {
            'metadata': {'uid': 'deployment-uid', 'generation': 2,
                         'annotations': {'deployment.kubernetes.io/revision': '2'}},
            'spec': {'replicas': 1, 'template': {'spec': {'containers': [{'name': 'app', 'image': REFERENCE}]}}},
            'status': {'observedGeneration': 2, 'updatedReplicas': 1, 'readyReplicas': 1},
        }
        replicasets = {'items': [{
            'metadata': {'uid': 'rs-uid', 'ownerReferences': [
                {'kind': 'Deployment', 'uid': 'deployment-uid', 'controller': True}],
                'annotations': {'deployment.kubernetes.io/revision': '2'}},
            'spec': deployment['spec'],
        }]}
        pods = {'items': [{
            'metadata': {'name': 'ready-pod', 'ownerReferences': [{'kind': 'ReplicaSet', 'uid': 'rs-uid', 'controller': True}]},
            'status': {'containerStatuses': [{'name': 'app', 'ready': True, 'imageID': f'docker://{PLATFORM_DIGEST}'}]},
        }]}
        for digest, pass_check in ((PLATFORM_DIGEST, True), (DIGEST, False)):
            with (
                patch.object(kubernetes, 'deployment', return_value=deployment),
                patch.object(kubernetes, 'run', side_effect=['', json.dumps(replicasets), json.dumps(pods)]),
            ):
                if pass_check:
                    self.assertEqual(kubernetes.verify_rollout(REFERENCE, {digest})['ready_replicas'], 1)
                else:
                    with self.assertRaisesRegex(deploy.DeploymentError, 'match the published image'):
                        kubernetes.verify_rollout(REFERENCE, {digest})


if __name__ == '__main__':
    unittest.main()
