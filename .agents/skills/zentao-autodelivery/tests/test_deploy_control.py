import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / 'scripts'))

from delivery_scope import route_stages
from ledger import encode, transaction, validate_config
from route_execution import v2_repair


def target_config():
    return {'enabled': True, 'target': 'test', 'build_dependencies': {'tet-common-java': 'b' * 40}, 'targets': {'test': {
        'registry': 'registry.example.test:6443', 'image_repository': 'internalrd/smart-geo-srv',
        'platform': 'linux/amd64', 'cluster_id': 'test-cluster', 'kube_context': 'test-kube', 'namespace': 'internalrd',
        'deployment': 'smart-geo-new', 'container': 'smart-geo', 'gateway_url': 'http://example.test:31111/',
        'verification_path': '/tet/smart-geo/actuator/info',
    }}}


class DeployRouteTests(unittest.TestCase):
    def test_only_backend_routes_include_deployment_and_joint_repair_redeploys(self):
        deploy = 'backend.deploy'
        for scope in ('frontend_only', 'backend_only', 'fullstack'):
            self.assertNotIn(deploy, route_stages(scope))
            enabled = route_stages(scope, deploy_enabled=True)
            self.assertEqual(deploy in enabled, scope != 'frontend_only')
            if scope != 'frontend_only':
                self.assertEqual(enabled[enabled.index('backend.integration') + 1], deploy)
        route = {'scope': 'fullstack', 'stages': route_stages('fullstack', deploy_enabled=True)}
        phases = v2_repair('frontend.e2e', {'repair_side': 'backend'}, route)
        self.assertEqual(phases[phases.index('backend.integration') + 1:][:2], [deploy, 'handoff'])

    def test_configuration_rejects_unsigned_target_inputs(self):
        config = json.loads((SKILL.parents[1] / 'zentao-project.json').read_text(encoding='utf-8'))
        service = config['development']['backend_bundle']['services']['smart-geo']
        service['deployment'] = target_config()
        validate_config(config)
        service['deployment']['targets']['test']['deployment'] = '--all-namespaces'
        with self.assertRaisesRegex(ValueError, 'deployment'):
            validate_config(config)


class DeployWorkerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.config_path = self.root / 'project.json'
        self.database = self.root / 'ledger.sqlite3'
        self.config = json.loads((SKILL.parents[1] / 'zentao-project.json').read_text(encoding='utf-8'))
        self.config['development']['backend_bundle']['services']['smart-geo']['deployment'] = target_config()
        self.config_path.write_text(encode(self.config), encoding='utf-8')
        self.sha = 'a' * 40

    def setup_item(self, number, service='smart-geo', accepted=True, scope='backend_only'):
        marker = 'test-marker-' + str(number)
        tasks = {'backend': [], 'frontend': []}
        if scope == 'fullstack':
            tasks = {'backend': [{'id': 'B1'}], 'frontend': [{'id': 'F1', 'depends_on': ['B1']}]}
        route = {'scope': scope, 'source': 'config', 'tasks': tasks,
                 'blockers': [], 'stages': route_stages(scope, deploy_enabled=True)}
        with transaction(self.database) as conn:
            conn.execute("""INSERT INTO work_items(seq,instance,project_id,kind,item_id,title,assigned_to,
                         source_status,eligible,observed_at,state,marker,config_snapshot,chat_id,request_id,stage)
                         VALUES(?, 'fixture', '1','story',?,'task','worker','active',1,'','running',?,?,?,?,
                                'backend.deploy')""",
                         (number, str(number), marker, encode({**self.config, 'workflow_version': 2}),
                          f'chat-{number}', f'request-{number}'))
            conn.execute("INSERT INTO dispatches VALUES(?,?,'desktop','started','')", (f'request-{number}', number))
            stages = [('split', {}, {'route': route}), ('backend.plan', {'services': [service]}, {}),
                      ('backend.code_review', {'versions': {service: self.sha}}, {}),
                      ('backend.coding', {'versions': {service: self.sha}}, {}),
                      ('backend.acceptance', {'versions': {service: self.sha}}, {}),
                      ('backend.integration', {'merged_versions': {service: self.sha}}, {})]
            for stage, evidence, extra in stages:
                result = {'summary': stage, 'evidence': evidence, 'phase': 'verify', 'terminal_step': True, **extra}
                conn.execute("""INSERT INTO attempts(attempt_id,item_seq,run_round,stage,retry_number,
                             started_at,ended_at,outcome,result,phase,phase_started_at)
                             VALUES(?,?,1,?,0,'','',?,?,'verify','')""",
                             (f'{marker}-{stage}', number, stage,
                              'failed' if stage == 'backend.acceptance' and not accepted else 'success', encode(result)))
        return marker

    def action(self, operation, marker, **fields):
        from deploy_control import deployment_action

        return deployment_action(operation, marker, self.database, self.config_path, **fields)

    def test_worker_exclusively_claims_and_locks_same_target(self):
        marker = self.setup_item(1)
        other = self.setup_item(2)
        self.assertEqual(self.action('peek', marker)['status'], 'deployment_pending')
        claim = self.action('claim', marker)
        self.assertEqual(claim.get('status'), 'deployment_claimed', claim)
        self.assertEqual(claim['requests']['smart-geo']['sha'], self.sha)
        self.assertEqual(self.action('claim', marker)['status'], 'deployment_busy')
        self.assertEqual(self.action('claim', other)['status'], 'deployment_busy')
        tag = 'registry.example.test:6443/internalrd/smart-geo-srv:1.0.01'
        result = self.action('lock_tags', marker, attempt_id=claim['attempt_id'], tags=[tag])
        self.assertEqual(result['status'], 'deployment_tags_locked')
        result = self.action('finish', marker, attempt_id=claim['attempt_id'], summary='Release failed',
                             outcome='failed', retryable=False, evidence={'services': {}, 'skipped': []})
        self.assertEqual(result['disposition'], 'close')
        self.assertEqual(self.action('claim', other)['status'], 'deployment_claimed')

    def test_failed_backend_deployment_still_enters_handoff(self):
        self.config['runtime']['split_task_sync'] = False
        marker = self.setup_item(1, scope='fullstack')
        claim = self.action('claim', marker)
        self.assertEqual(claim['status'], 'deployment_claimed', claim)
        result = self.action('finish', marker, attempt_id=claim['attempt_id'], outcome='failed',
                             retryable=False, summary='Release failed',
                             evidence={'services': {}, 'task_blockers': {'B1': 'Release failed'}})
        self.assertEqual((result['disposition'], result['next_stage']), ('next', 'handoff'))

    def test_backend_deployment_claim_after_failed_acceptance_and_successful_merge(self):
        marker = self.setup_item(1, accepted=False)
        claim = self.action('claim', marker)
        self.assertFalse(claim.get('blocked'), claim)
        self.assertEqual(claim['requests']['smart-geo']['sha'], self.sha)

    def test_revoked_target_blocks_external_writes(self):
        marker = self.setup_item(1)
        self.config['development']['backend_bundle']['services']['smart-geo']['deployment']['enabled'] = False
        self.config_path.write_text(encode(self.config), encoding='utf-8')
        revoked = self.action('claim', marker)
        self.assertTrue(revoked.get('blocked'), revoked)
        self.assertFalse(revoked['requests'])

    def test_unconfigured_service_has_explicit_noop(self):
        marker = self.setup_item(1, service='model-python')
        claim = self.action('claim', marker)
        self.assertEqual(claim.get('skipped'), ['model-python'], claim)
        self.assertFalse(claim['requests'])
        self.assertNotIn('blocked', claim)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM deployment_locks').fetchone()[0], 0)

    def test_independent_targets_cannot_push_the_same_tag_concurrently(self):
        deployment = self.config['development']['backend_bundle']['services']['smart-geo']['deployment']
        deployment['targets']['another'] = {**deployment['targets']['test'], 'deployment': 'other-test'}
        first = self.setup_item(1)
        deployment['target'] = 'another'
        second = self.setup_item(2)
        self.config_path.write_text(encode(self.config), encoding='utf-8')
        one = self.action('claim', first)
        two = self.action('claim', second)
        self.assertEqual([one['status'], two['status']], ['deployment_claimed', 'deployment_claimed'])
        tag = 'registry.example.test:6443/internalrd/smart-geo-srv:1.0.01'
        self.assertEqual(self.action('lock_tags', first, attempt_id=one['attempt_id'], tags=[tag])['status'],
                         'deployment_tags_locked')
        self.assertEqual(self.action('lock_tags', second, attempt_id=two['attempt_id'], tags=[tag])['status'],
                         'deployment_busy')

    def test_kube_context_aliases_share_the_target_lock(self):
        deployment = self.config['development']['backend_bundle']['services']['smart-geo']['deployment']
        deployment['targets']['alias'] = {**deployment['targets']['test'], 'kube_context': 'same-cluster-alias'}
        first = self.setup_item(1)
        deployment['target'] = 'alias'
        second = self.setup_item(2)
        self.config_path.write_text(encode(self.config), encoding='utf-8')
        self.assertEqual(self.action('claim', first)['status'], 'deployment_claimed')
        self.assertEqual(self.action('claim', second)['status'], 'deployment_busy')

    def test_release_runner_advances_real_ledger_and_rejects_chat_claim(self):
        import backend_deploy_runner as runner
        from workflow import run

        marker = self.setup_item(1)
        data = {'action': 'attempt_start', 'event_id': 'chat-claim', 'stage': 'backend.deploy',
                'item': {'instance': 'fixture', 'project_id': '1', 'kind': 'story', 'id': '1'},
                'chat_id': 'chat-1', 'request_id': 'request-1'}
        self.assertEqual(run('record', data, self.config, self.database,
                             config_directory=self.root)['status'], 'deployment_worker_required')
        tag = 'registry.example.test:6443/internalrd/smart-geo-srv:1.0.01'

        def simulate_publisher(*args, **kwargs):
            self.assertTrue(kwargs.get('recoverable'))
            command = args[-1]
            request = json.loads(Path(command[-1]).read_text(encoding='utf-8'))
            self.assertEqual(request['sha'], self.sha)
            target = request['target']
            receipt = {'service': request['service'], 'target': target, 'sha': self.sha,
                       'status': 'deployed', 'image': tag, 'digest': 'sha256:' + 'c' * 64,
                       'pod_verification': {'deployment_uid': 'mock-uid', 'ready_replicas': 1},
                       'verification': {'sha': self.sha, 'url': target['gateway_url'].rstrip('/') +
                                                               target['verification_path']}}
            Path(request['receipt_path']).write_text(encode(receipt), encoding='utf-8')
            return {'outcome': 'success', 'stop_confirmed': True, 'stop_evidence': {'mock': True}}

        with (patch.object(runner, 'image_tag', return_value=tag),
              patch.object(runner, 'run_stage', side_effect=simulate_publisher) as publish):
            result = runner.run_deployment(marker, self.database, self.config_path, self.root)
        publish.assert_called_once()
        self.assertEqual(result['status'], 'attempt_finished', result)
        self.assertEqual(result['disposition'], 'close', result)
        self.assertEqual(self.action('peek', marker)['status'], 'deployment_not_pending')
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM deployment_locks').fetchone()[0], 0)
