from contextlib import closing
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import frontend_e2e_runner as runner
from ledger import SCHEMA
from stage_runner import utc_now


class FrontendHostTests(unittest.TestCase):
    marker = 'HNF-' + '1' * 20
    attempt = '2' * 32

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / 'workflow.sqlite3'
        self.config = self.root / 'config.json'
        config = {'workspaces': {'frontend': '.'}, 'e2e': {'workspace': 'frontend', 'enabled': True,
                  'source_origins': ['http://10.10.4.140:31111'],
                  'after_integration': True, 'agentlimb': {'preflight_origin': 'http://localhost:5000'}}}
        self.config.write_text(json.dumps(config), encoding='utf-8')
        self.deadline = utc_now() + timedelta(minutes=2)
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.executescript(SCHEMA)
            conn.execute("""INSERT INTO work_items
              (seq, instance, project_id, kind, item_id, title, assigned_to, source_status, eligible, observed_at,
              state, marker, config_snapshot, chat_id, request_id, stage)
              VALUES (1,'example','1','bug','3','test','a','active',1,'now','running',?,?,'chat','dispatch','frontend.e2e')""",
                         (self.marker, json.dumps(config)))
            conn.execute("""INSERT INTO dispatches (request_id,item_seq,desktop_project_id,state,expires_at)
                         VALUES ('dispatch',1,'project','started',?)""", (self.deadline.isoformat(),))
            conn.execute("""INSERT INTO attempts
              (attempt_id,item_seq,run_round,stage,retry_number,started_at,phase,phase_started_at,outcome,result)
              VALUES ('split',1,1,'split',0,'now','verify','now','success',?)""",
                         (json.dumps({'route': {'post_merge_e2e': True, 'stages': ['frontend.e2e']},
                                      'summary': '', 'evidence': {}}),))
            conn.execute("""INSERT INTO attempts
              (attempt_id,item_seq,run_round,stage,retry_number,started_at,deadline_at,phase,phase_started_at)
              VALUES (?,1,1,'frontend.e2e',0,'now',?,'verify','now')""",
                         (self.attempt, self.deadline.isoformat()))
        acquired = patch.object(runner, 'owns_slot', return_value=True)
        self.slot = acquired.start()
        self.addCleanup(acquired.stop)
        self.item = self.root / 'items' / self.marker
        self.folder = self.item / 'frontend' / ('e2e-' + self.attempt)
        self.folder.mkdir(parents=True)
        for name in ('profile.json', 'scenario.json'):
            (self.folder / name).write_text('{}', encoding='utf-8')
        (self.folder / 'scenario.json').write_text(json.dumps({
            'steps': [{'action': 'assert-visible', 'selector': '#fixture'}],
        }), encoding='utf8')

    def request(self, **changes):
        relative = self.folder.relative_to(self.item).as_posix()
        payload = {'attempt_id': self.attempt, 'profile': relative + '/profile.json',
                   'scenario': relative + '/scenario.json', 'base_url': 'http://localhost:5000', **changes}
        (self.folder / 'host-request.json').write_text(json.dumps(payload), encoding='utf8')

    def run_host(self):
        return runner.run_frontend_e2e(self.marker, self.database, self.config, self.root)

    def test_new_item_host_builds_inputs_from_one_business_scenario(self):
        with closing(sqlite3.connect(self.database)) as conn, conn:
            cfg = json.loads(self.config.read_text('utf-8'))
            cfg['e2e_attestation'] = 'launch'
            conn.execute('UPDATE work_items SET config_snapshot=?', (json.dumps(cfg),))
        scenario = self.folder / 'scenario.json'
        local = '/aggregation-platform/shell-hub/data-overview/gas-profile?well=1#summary'
        online = 'http://10.10.4.140:31111' + local
        scenario.write_text(json.dumps({'entry_path': online, 'cases': [{'name': 'gas-profile', 'steps': [
            {'action': 'navigate', 'path': online},
            {'action': 'navigate', 'path': '/charts'},
            {'action': 'assert-visible', 'selector': '#fixture'},
        ]}]}),
                            encoding='utf-8')
        request = {'attempt_id': self.attempt, 'scenario': scenario.relative_to(self.item).as_posix()}
        (self.folder / 'host-request.json').write_text(json.dumps(request), encoding='utf-8')
        job = runner._active_job(self.marker, self.database, self.config)
        self.assertTrue(job.automatic)
        stage_root = self.folder / 'host-run'
        command = runner._command(self.marker, job, stage_root)
        self.assertIn('http://localhost:5000', command)
        self.assertEqual(json.loads((stage_root / 'inputs/profile.json').read_text('utf-8')),
                         {'entry_path': local})
        prepared = json.loads((stage_root / 'inputs/scenario.json').read_text('utf-8'))
        self.assertEqual([step['path'] for step in prepared['cases'][0]['steps'][:2]], [local, '/charts'])
        self.assertEqual(json.loads(scenario.read_text('utf-8'))['entry_path'], online)

    def test_unconfigured_and_ambiguous_frontend_urls_are_rejected(self):
        origins = {'http://10.10.4.140:31111'}
        for url in ('https://other.example/page', 'http://10.10.4.140:31111@other.example/page',
                    '//other.example/page', 'http://10.10.4.140:31111//other.example/page'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                runner._local_page_path(url, origins)

    def test_external_code_out_of_scope_is_rejected_without_host_process(self):
        self.request(scenario='../other/scenario.cjs')
        with patch.object(runner, 'run_stage') as stage:
            result = self.run_host()
        stage.assert_not_called()
        self.assertEqual(result['status'], 'host_e2e_finished')
        self.assertEqual(json.loads((self.folder / 'agentlimb.json').read_text(encoding='utf8'))['status'], 'blocked')

    def test_slot_must_be_held_before_running_host_process(self):
        self.request()
        self.slot.return_value = False
        with patch.object(runner, 'run_stage') as stage:
            result = self.run_host()
        stage.assert_not_called()
        self.assertEqual(result['status'], 'host_e2e_not_pending')
        self.assertFalse((self.folder / 'agentlimb.json').exists())

    def test_success_requires_stage_stop_and_does_not_run_twice(self):
        self.request()
        stdout = self.root / 'stdout.log'
        stdout.write_text(json.dumps({'runner': 'agentlimb', 'status': 'passed', 'stage': 'acceptance',
                                      'marker': self.marker, 'attempt_id': self.attempt,
                                      'scenario_cases': [{'name': 'scenario', 'status': 'passed'}]}), encoding='utf-8')
        receipt = self.root / 'result.json'
        stage_proof = {'outcome': 'success', 'stop_confirmed': True, 'stdout_path': str(stdout),
                       'receipt_path': str(receipt), 'stop_evidence': {'owned_processes_active_after_stop': 0}}
        with patch.object(runner, 'run_stage', return_value=stage_proof) as stage:
            self.assertEqual(self.run_host()['status'], 'host_e2e_finished')
            self.assertEqual(self.run_host()['status'], 'host_e2e_not_pending')
        stage.assert_called_once()
        command = stage.call_args.args[-1]
        self.assertIn(str(self.folder / 'host-run' / 'inputs' / 'scenario.json'), command)
        self.assertEqual(json.loads((self.folder / 'agentlimb.json').read_text(encoding='utf8'))['status'], 'passed')

    def test_passed_report_missing_cases_becomes_environment_blocker(self):
        self.request()
        stdout = self.root / 'stdout.log'
        stdout.write_text(json.dumps({'runner': 'agentlimb', 'status': 'passed', 'stage': 'acceptance',
                                      'marker': self.marker, 'attempt_id': self.attempt,
                                      'scenario_cases': []}), encoding='utf8')
        stage_proof = {'outcome': 'success', 'stop_confirmed': True, 'stdout_path': str(stdout),
                       'receipt_path': str(self.root / 'result.json'),
                       'stop_evidence': {'owned_processes_active_after_stop': 0}}
        with patch.object(runner, 'run_stage', return_value=stage_proof):
            self.assertEqual(self.run_host()['status'], 'host_e2e_finished')
        check = json.loads((self.folder / 'agentlimb.json').read_text(encoding='utf8'))
        self.assertEqual(check['status'], 'blocked')
        self.assertEqual(check['stage'], 'scenario-results')

    def test_missing_stop_proof_blocks_publication(self):
        self.request()
        with patch.object(runner, 'run_stage', return_value={'stop_confirmed': False}):
            self.assertEqual(self.run_host()['status'], 'host_e2e_stop_unverified')
        self.assertFalse((self.folder / 'agentlimb.json').exists())


if __name__ == '__main__':
    unittest.main()
