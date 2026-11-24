from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import stage_runner
import status_board
import task_sync_runner as sync_runner
import worker
from test_workflow import WorkflowFixture


class SyncRuntimeTests(unittest.TestCase):
    def test_worker_obeys_story_snapshot_and_binding(self):
        item = {'kind': 'story', 'state': 'running', 'chat_id': 'bound',
                'config_snapshot': json.dumps({'workflow_version': 2, 'runtime': {'split_task_sync': True}})}
        with patch.object(sync_runner, 'runtime_check', return_value={'status': 'runtime_unattended'}):
            self.assertTrue(sync_runner.can_sync_item(item, 'bound'))
            for data, owner in [({**item, 'kind': 'bug'}, 'bound'), (item, 'wrong'),
                                ({**item, 'state': 'queued'}, 'bound'),
                                ({**item, 'config_snapshot': '{}'}, 'bound')]:
                self.assertFalse(sync_runner.can_sync_item(data, owner))
        with patch.object(sync_runner, 'runtime_check', return_value={'status': 'runtime_requires_approval'}):
            self.assertFalse(sync_runner.can_sync_item(item, 'bound'))

    def test_monitor_enumerates_only_pending_tasks_in_selected_project(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'workflow.sqlite3'
            with closing(sqlite3.connect(database)) as conn, conn:
                conn.executescript("""
                    CREATE TABLE scans (instance, project_id, project_name);
                    CREATE TABLE work_items (seq, instance, project_id, kind, marker, config_snapshot, state);
                    CREATE TABLE writebacks (item_seq, operation_type, state);
                    INSERT INTO scans VALUES ('example','2','Alpha'), ('example','3','Beta');
                """)
                snapshot = json.dumps({'workflow_version': 2, 'runtime': {'split_task_sync': True}})
                for i, project in [(1, '2'), (2, '3'), (3, '2'), (4, '2')]:
                    conn.execute('INSERT INTO work_items VALUES (?,?,?,?,?,?,?)',
                                 (i, 'example', project, 'story', f'm{i}', snapshot, 'completed'))
                conn.executemany('INSERT INTO writebacks VALUES (?,?,?)',
                                 [(1, 'task_close', 'pending'), (2, 'task_create', 'pending'),
                                  (3, 'comment', 'pending'), (4, 'task_close', 'superseded')])
            config = {'zentao': {'base_url': 'example', 'project_name': 'Alpha'}}
            self.assertEqual(sync_runner.pending_task_markers(database, config), ['m1'])

    def test_board_separates_readback_and_verified_delivery_and_escapes_fields(self):
        rows = [{'task_key': 'F1', 'side': 'frontend', 'definition': '{"description":"<title>"}',
                 'remote_id': '65', 'remote_status': 'done', 'desired': 'closed',
                 'execution_id': '2', 'assignee': 'developer', 'last_error': '<retry>'},
                {'task_key': 'B1', 'side': 'backend', 'definition': '{}', 'remote_id': None,
                 'remote_status': None, 'desired': 'open', 'execution_id': None, 'assignee': None,
                 'last_error': '迭代待定'}]
        content = status_board.task_progress(rows, 'https://zentao')
        for expected in ['研发 1/2', '已关闭 0/2', 'taskID=65', '待关闭', '待建单', '迭代待定',
                         '&lt;title&gt;', '&lt;retry&gt;']:
            self.assertIn(expected, content)
        self.assertNotIn('<title>', content)

    @unittest.skipUnless(os.name == 'nt', 'Windows Job Object')
    def test_worker_can_stop_parallel_job_before_its_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stop = threading.Event()
            deadline = stage_runner.utc_now() + timedelta(seconds=12)
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(stage_runner.run_stage, 'parallel', 'task-sync', deadline,
                                         root, root, None, [sys.executable, '-c',
                                         'import time; from pathlib import Path; Path("ready").touch(); time.sleep(30)'],
                                         stop_event=stop)
                until = time.monotonic() + 6
                while not (root / 'ready').exists() and time.monotonic() < until:
                    time.sleep(.025)
                stop.set()
                receipt = result.result(timeout=4)
            self.assertTrue((root / 'ready').exists())
            self.assertTrue(receipt['stop_confirmed'])
            self.assertEqual(receipt['outcome'], 'stopped')
            self.assertEqual(receipt['stop_evidence']['owned_processes_active_after_stop'], 0)

    def test_monitor_schedules_work_evenly_and_requires_the_actual_chat_policy(self):
        calls = []
        with patch.object(sync_runner, 'runtime_check', return_value={'status': 'runtime_requires_approval'}), \
             patch.object(sync_runner, 'sync_and_publish') as sync:
            with self.assertRaises(ValueError):
                sync_runner.monitor_batch({})
            sync.assert_not_called()
        with patch.object(sync_runner, 'runtime_check', return_value={'status': 'runtime_unattended'}), \
             patch.object(sync_runner, 'pending_task_markers', return_value=['A','B']), \
             patch.object(sync_runner, 'sync_and_publish', side_effect=lambda marker, _: calls.append(marker) or True):
            self.assertEqual(sync_runner.monitor_batch({}, limit=3)['processed'], 3)
        self.assertEqual(calls, ['A', 'B', 'A'])

    @unittest.skipUnless(os.name == 'nt', 'Windows work-item file lock')
    def test_normal_run_starts_and_stops_sync_alongside_development(self):
        marker = 'HNF-' + '0' * 20
        item = {'marker': marker, 'request_id': 'r', 'state': 'running', 'kind': 'story', 'instance': 'test',
                'project_id': '2', 'item_id': '55', 'chat_id': '00000000-0000-4000-a000-000000000123',
                'config_snapshot': json.dumps({'workflow_version': 2, 'runtime': {'split_task_sync': True}})}
        entered = threading.Event()
        def stage(run_id, phase, *args, stop_event=None):
            if phase == 'task-sync':
                entered.set()
                self.assertTrue(stop_event.wait(2))
            else:
                self.assertTrue(entered.wait(2))
            return {'stop_confirmed': True, 'receipt_path': phase, 'outcome': 'success'}
        with tempfile.TemporaryDirectory() as folder, patch.object(
             worker, 'os', SimpleNamespace(environ=dict(os.environ))), \
             patch.object(worker, 'DATABASE_PATH', Path(folder) / 'db'), \
             patch.object(worker, 'read_item', return_value=item), patch.object(worker, 'run_stage', side_effect=stage):
            result = worker.run('run', marker, Path('codex.exe'))
        self.assertEqual(result['status'], 'worker_stopped')
        self.assertEqual(result['task_sync_receipt'], 'task-sync')


class CloseoutTests(WorkflowFixture):
    def test_final_comment_displays_development_and_remote_progress(self):
        self.config['runtime']['split_task_sync'] = True
        binding = self.start()
        with closing(sqlite3.connect(self.database)) as conn, conn:
            seq = conn.execute("SELECT seq FROM work_items WHERE item_id='2'").fetchone()[0]
            for key, desired, status, remote_id in [('F1','closed','closed','3'), ('F2','closed','done','4'),
                                                    ('F3','open',None,None)]:
                conn.execute('''INSERT INTO split_tasks(item_seq,task_key,side,instance,definition,desired,
                                remote_status,remote_id) VALUES(?,?,?,?,?,?,?,?)''',
                             (seq, key, 'frontend', 'test', '{}', desired, status, remote_id))
        response = self.record('stop', **binding, stop_evidence={'chat_status': 'idle',
                                                               'owned_processes_active_after_stop': 0})
        self.assertEqual(response['status'], 'completed')
        with closing(sqlite3.connect(self.database)) as conn:
            note = json.loads(conn.execute('SELECT payload FROM writebacks WHERE operation_id=?',
                                           (response['operation_id'],)).fetchone()[0])['text']
        for summary in ['研发已验收并合回 2/3 项', '禅道已关闭 1/3 张', '待建单 1 张', '待关闭 1 张']:
            self.assertIn(summary, note)
        board = status_board.publish_status(self.database, self.config).read_text(encoding='utf-8')
        for expected in ['研发 2/3', '已关闭 1/3', '待关闭', '待建单', 'F3']:
            self.assertIn(expected, board)
