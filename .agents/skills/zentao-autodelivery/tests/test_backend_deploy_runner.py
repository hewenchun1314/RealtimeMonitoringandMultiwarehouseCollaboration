import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import backend_deploy_runner as runner
from stage_runner import utc_now, write_json


class DeploymentRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / 'ledger.sqlite3'
        self.config = self.root / 'config.json'
        self.marker = 'HNF-' + '1' * 20
        self.claim = {
            'status': 'deployment_claimed', 'attempt_id': 'attempt-1', 'phase': 'verify',
            'deadline_at': (utc_now() + timedelta(minutes=1)).isoformat(), 'run_round': 1,
            'gateway_url': 'http://gateway.test/', 'skipped': [],
            'requests': {
                'smart-geo': {
                    'service': 'smart-geo', 'sha': 'a' * 40,
                    'target': {'namespace': 'internalrd'}, 'repository': str(self.root),
                },
            },
        }
        self.tag = 'registry.test/internalrd/smart-geo-srv:1.2.3'

    def run_one(self, *, recover=False):
        return runner.run_deployment(self.marker, self.database, self.config, self.root, recover=recover)

    def publish(self, attempt_id, phase, deadline, cwd, evidence_dir, prompt, command, *, recoverable=False):
        self.assertEqual(attempt_id, 'attempt-1')
        self.assertEqual(cwd, self.root)
        self.assertIsNone(prompt)
        self.assertEqual(command[-2], '--request')
        request = json.loads(Path(command[-1]).read_text(encoding='utf-8'))
        self.assertEqual(request['expected_image_tag'], self.tag)
        receipt = {key: request[key] for key in ('service', 'sha', 'target')}
        write_json(Path(request['receipt_path']), {
            **receipt, 'image': self.tag, 'status': 'deployed',
        })
        return {'outcome': 'success', 'stop_confirmed': True, 'stop_evidence': {'owned': 0}}

    def test_claim_lock_publish_and_finish_are_automatic(self):
        order = []

        def action(name, marker, database, config, **params):
            self.assertEqual((marker, database, config), (self.marker, self.database, self.config))
            order.append(name)
            if name == 'claim':
                return self.claim
            if name == 'lock_tags':
                self.assertEqual(params['tags'], [self.tag])
                return {'status': 'deployment_tags_locked'}
            self.assertEqual(params['outcome'], 'success')
            self.assertEqual(params['evidence']['services']['smart-geo']['sha'], 'a' * 40)
            self.assertEqual(params['evidence']['gateway_url'], 'http://gateway.test/')
            return {'status': 'attempt_finished'}

        with patch.object(runner, 'deployment_action', side_effect=action), \
                patch.object(runner, 'image_tag', return_value=self.tag), \
                patch.object(runner, 'run_stage', side_effect=self.publish):
            result = self.run_one()

        self.assertEqual(order, ['claim', 'lock_tags', 'finish'])
        self.assertEqual(result['status'], 'attempt_finished')

    def test_unknown_process_stop_blocks_finish_and_relaunch(self):
        busy = {**self.claim, 'status': 'deployment_busy'}
        with patch.object(runner, 'deployment_action', return_value=busy) as action, \
                patch.object(runner, 'run_stage') as stage:
            self.assertEqual(self.run_one()['status'], 'deployment_busy')
        self.assertEqual([call.args[0] for call in action.call_args_list], ['claim'])
        stage.assert_not_called()

    def test_completed_result_can_be_replayed_after_ledger_write_failure(self):
        busy = {**self.claim, 'status': 'deployment_busy'}
        with patch.object(runner, 'deployment_action', side_effect=[
            self.claim, {'status': 'deployment_tags_locked'}, OSError('ledger offline'),
            busy, {'status': 'attempt_finished'},
        ]) as action, patch.object(runner, 'image_tag', return_value=self.tag), \
                patch.object(runner, 'run_stage', side_effect=self.publish) as stage:
            with self.assertRaises(OSError):
                self.run_one()
            self.assertEqual(self.run_one(recover=True)['status'], 'attempt_finished')

        self.assertEqual(stage.call_count, 1)
        finishes = [call.kwargs for call in action.call_args_list if call.args[0] == 'finish']
        self.assertEqual(finishes[0], finishes[1])

    def test_reuses_stage_stop_proof_if_launcher_dies_before_final_checkpoint(self):
        busy = {**self.claim, 'status': 'deployment_busy'}

        def stopped_then_disconnected(attempt_id, phase, deadline, cwd, evidence_dir, prompt, command, **options):
            proof = self.publish(attempt_id, phase, deadline, cwd, evidence_dir, prompt, command)
            directory = evidence_dir / attempt_id / phase
            directory.mkdir()
            write_json(directory / 'result.json', {**proof, 'attempt_id': attempt_id, 'phase': phase})
            raise OSError('supervisor interrupted')

        with patch.object(runner, 'deployment_action', side_effect=[
            self.claim, {'status': 'deployment_tags_locked'}, busy, {'status': 'attempt_finished'},
        ]) as action, patch.object(runner, 'image_tag', return_value=self.tag), \
                patch.object(runner, 'run_stage', side_effect=stopped_then_disconnected) as stage:
            with self.assertRaises(OSError):
                self.run_one()
            self.assertEqual(self.run_one(recover=True)['status'], 'attempt_finished')

        self.assertEqual(stage.call_count, 1)
        self.assertEqual(action.call_args.kwargs['outcome'], 'success')

    def test_claim_without_workspace_records_is_retryable_only_with_worker_lock(self):
        busy = {**self.claim, 'status': 'deployment_busy'}
        with patch.object(runner, 'deployment_action', side_effect=[
            busy, busy, {'status': 'attempt_finished'},
        ]) as action, patch.object(runner, 'run_stage') as stage:
            self.assertEqual(self.run_one()['status'], 'deployment_busy')
            self.assertEqual(self.run_one(recover=True)['status'], 'attempt_finished')

        stage.assert_not_called()
        self.assertEqual(action.call_args.kwargs['outcome'], 'failed')
        self.assertEqual(action.call_args.kwargs['stop_evidence']['mechanism'], 'no_launch_records')

    def test_only_verified_windows_stop_recovers_from_missing_supervisor_result(self):
        busy = {**self.claim, 'status': 'deployment_busy'}

        def disconnected(attempt_id, phase, deadline, cwd, evidence_dir, prompt, command, **options):
            directory = evidence_dir / attempt_id / phase
            directory.mkdir()
            write_json(directory / 'launch.json', {'attempt_id': attempt_id, 'phase': phase})
            raise OSError('supervisor interrupted')

        with patch.object(runner, 'deployment_action', side_effect=[
            self.claim, {'status': 'deployment_tags_locked'}, busy, busy, {'status': 'attempt_finished'},
        ]) as action, patch.object(runner, 'image_tag', return_value=self.tag), \
                patch.object(runner, 'run_stage', side_effect=disconnected) as stage, \
                patch.object(runner, 'recover_job_stop', side_effect=[None, {'mechanism': 'windows_job_object'}]):
            with self.assertRaises(OSError):
                self.run_one()
            self.assertEqual(self.run_one(recover=True)['status'], 'deployment_busy')
            self.assertEqual(self.run_one(recover=True)['status'], 'attempt_finished')

        self.assertEqual(stage.call_count, 1)
        self.assertEqual(action.call_args.kwargs['outcome'], 'failed')

    def test_skipped_and_blocked_deployments_do_not_launch_publisher(self):
        for blocked, outcome in ((None, 'success'), ('目标授权已撤销', 'skipped')):
            with self.subTest(blocked=blocked):
                claim = dict(self.claim, requests={}, skipped=['other'], blocked=blocked)
                with patch.object(runner, 'deployment_action',
                                  side_effect=[claim, {'status': 'attempt_finished'}]) as action, \
                        patch.object(runner, 'run_stage') as stage:
                    self.assertEqual(self.run_one()['status'], 'attempt_finished')
                stage.assert_not_called()
                self.assertEqual(action.call_args.kwargs['outcome'], outcome)

    def test_revoked_authorization_releases_claim_without_launching(self):
        with patch.object(runner, 'deployment_action', side_effect=[
            self.claim, {'status': 'deployment_revoked', 'reason': '部署目标授权已变更'},
            {'status': 'attempt_finished'},
        ]) as action, patch.object(runner, 'image_tag', return_value=self.tag), \
                patch.object(runner, 'run_stage') as stage:
            self.assertEqual(self.run_one()['status'], 'attempt_finished')

        stage.assert_not_called()
        self.assertEqual(action.call_args.kwargs['outcome'], 'failed')
        self.assertFalse(action.call_args.kwargs['retryable'])
