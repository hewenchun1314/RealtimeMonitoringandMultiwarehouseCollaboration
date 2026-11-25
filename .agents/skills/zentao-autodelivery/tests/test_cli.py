from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'workflow.py'
CONFIG = SCRIPT.parents[3] / 'zentao-project.json'


class WorkflowCommandTests(unittest.TestCase):
    def run_command(self, directory, command, payload, *, config=None):
        database = Path(directory) / 'ledger.sqlite3'
        arguments = [sys.executable, '-B', str(SCRIPT), command, '--database', str(database)]
        if config is not None:
            arguments.extend(['--config', str(config)])
        completed = subprocess.run(
            arguments,
            input=json.dumps(payload, ensure_ascii=False), capture_output=True, encoding='utf-8',
            cwd=directory, timeout=10,
        )
        self.assertTrue(completed.stdout, completed.stderr)
        response = json.loads(completed.stdout)
        return completed, response, database

    def test_disabled_configuration_disables_intake_before_opening_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            config = json.loads(CONFIG.read_text(encoding='utf-8'))
            config['monitor']['enabled'] = False
            config_path = Path(directory) / 'config.json'
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
            completed, response, database = self.run_command(directory, 'claim', {
                'action': 'scan', 'scan_id': 'disabled-probe', 'complete': True,
                'observed_at': '2026-11-18T10:00:00Z', 'instance': 'http://127.0.0.1:8088',
                'project_id': '2', 'project_name': '海能发地质工程一体化', 'items': [],
            }, config=config_path)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(response['status'], 'disabled')
            self.assertFalse(database.exists())

    def test_invalid_record_action_returns_machine_readable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            completed, response, _ = self.run_command(directory, 'record', {
                'action': 'not-a-workflow-event', 'event_id': 'invalid-probe',
                'item': {'instance': 'http://127.0.0.1:8088', 'project_id': '2',
                         'kind': 'story', 'id': '99999'},
            })
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(response['ok'])
            self.assertNotIn('Traceback', completed.stderr)

    def test_competing_command_processes_reserve_only_one_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            config = json.loads(CONFIG.read_text(encoding='utf-8'))
            config['monitor']['enabled'] = True
            config.setdefault('runtime', {})['desktop_project_id'] = 'test-project'
            config_path = Path(directory) / 'config.json'
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
            scope = {'instance': config['zentao']['base_url'], 'project_id': '2',
                     'project_name': config['zentao']['project_name']}
            sample = {'kind': 'story', 'id': '32001', 'title': '工区切换验证', 'status': 'active',
                      'source': {'project_id': '2', 'product_id': '2'},
                      'assigned_to': config['zentao']['assigned_to_accounts'][0], 'priority': 1}
            for scan_id, items, expected in (('baseline', [], 'baseline'),
                                              ('new-story', [sample], 'scanned')):
                completed, response, database = self.run_command(directory, 'claim', {
                    **scope, 'action': 'scan', 'scan_id': scan_id, 'complete': True, 'items': items,
                    'observed_at': datetime.now(timezone.utc).isoformat(),
                }, config=config_path)
                self.assertEqual(completed.returncode, 0, response)
                self.assertEqual(response['status'], expected)
            recheck = {'action': 'reserve', 'item': response['candidate'], 'status': sample['status'],
                       'source': sample['source'],
                       'assigned_to': sample['assigned_to'], 'desktop_project_id': 'test-project',
                       'observed_at': datetime.now(timezone.utc).isoformat()}
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self.run_command, directory, 'claim', recheck, config=config_path)
                           for _ in range(2)]
                results = [future.result() for future in futures]
            self.assertCountEqual([result[1]['status'] for result in results], ['reserved', 'busy'])
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM dispatches').fetchone()[0], 1)
                self.assertEqual(connection.execute('SELECT title FROM work_items').fetchone()[0], sample['title'])

    def test_bug_intake_respects_project_settings_and_can_be_disabled(self):
        for bug_enabled in (True, False):
            with self.subTest(bug_enabled=bug_enabled), tempfile.TemporaryDirectory() as directory:
                config = json.loads(CONFIG.read_text(encoding='utf-8'))
                config['monitor']['enabled'] = True
                if not bug_enabled:
                    config['zentao']['accepted_statuses']['bug'] = []
                config_path = Path(directory) / 'config.json'
                config_path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
                scope = {'instance': config['zentao']['base_url'], 'project_id': '2',
                         'project_name': config['zentao']['project_name']}
                bug = {'kind': 'bug', 'id': '32002', 'title': 'Bug 接收验证', 'status': 'active',
                       'source': {'project_id': '2', 'product_id': '2'},
                       'assigned_to': config['zentao']['assigned_to_accounts'][0], 'priority': 1}
                story = {**bug, 'kind': 'story', 'id': '32003', 'title': '需求接收验证', 'priority': 2}
                closed_bug = {**bug, 'id': '32004', 'status': 'closed'}
                other_assignee = {**bug, 'id': '32005', 'assigned_to': 'other-account'}
                for scan_id, items in [('baseline', []), ('mixed', [closed_bug, other_assignee, bug, story])]:
                    completed, result, database = self.run_command(directory, 'claim', {
                        **scope, 'action': 'scan', 'scan_id': scan_id, 'complete': True, 'items': items,
                        'observed_at': datetime.now(timezone.utc).isoformat(),
                    }, config=config_path)
                    self.assertEqual(completed.returncode, 0, result)
                expected = bug if bug_enabled else story
                self.assertEqual(result['candidate']['kind'], expected['kind'])
                self.assertEqual(result['candidate']['id'], expected['id'])
                with closing(sqlite3.connect(database)) as connection:
                    queued = connection.execute("SELECT kind, item_id FROM work_items WHERE state='queued'").fetchall()
                expected_queue = [('story', story['id'])]
                if bug_enabled:
                    expected_queue.append(('bug', bug['id']))
                self.assertCountEqual(queued, expected_queue)


if __name__ == '__main__':
    unittest.main()
