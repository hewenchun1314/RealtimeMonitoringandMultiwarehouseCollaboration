import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from ledger import Ledger, transaction
import task_sync
from task_remote import RemoteRejected
from task_sync import reconcile_missing, register_split, set_desired, sync_once


class Remote:
    def __init__(self):
        self.tasks = {}
        self.calls = []
        self.lose_response = False
        self.available = True

    def target(self, story, project):
        if not self.available:
            raise ValueError('无法定位唯一迭代')
        return {'execution': '7', 'assignee': 'Tester'}

    def find(self, execution, marker):
        return [t for t in self.tasks.values() if t['execution'] == execution and marker in t['description']]

    def create(self, target, story, title, description, marker):
        number = str(len(self.tasks) + 100)
        self.calls.append('create')
        self.tasks[number] = dict(target, id=number, story=story, description=marker, status='wait')
        if self.lose_response:
            raise TimeoutError('response lost')
        return number

    def get(self, number):
        return self.tasks[str(number)]

    def transition(self, number, action, *, assignee=None):
        self.calls.append(action)
        self.tasks[number]['status'] = 'closed' if action == 'close' else 'doing'
        if assignee:
            self.tasks[number]['assignee'] = assignee

    def start(self, number, started_at):
        self.calls.append('start')
        self.tasks[number]['status'] = 'doing'

    def comment(self, number, text, marker):
        self.calls.append('comment')
        self.tasks[number].setdefault('comments', {})[marker] = text

    def has_comment(self, number, marker):
        return marker in self.tasks[number].get('comments', {})


class TaskSyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = Path(temp.name) / 'ledger.db'
        self.remote = Remote()
        self.conf = {'runtime': {'split_task_sync': True}, 'workflow_version': 2}
        with transaction(self.database) as conn:
            conn.execute("""INSERT INTO work_items(instance,project_id,kind,item_id,title,assigned_to,
                source_status,eligible,observed_at,state,marker,config_snapshot)
                VALUES('local','2','story','89','Story','Tester','active',1,'now','running','abc',?)""",
                         (json.dumps(self.conf),))
        self.tasks = {'tasks': {'frontend': [
            {'id': 'F1', 'description': '详情', 'acceptance': ['可查看']},
            {'id': 'F2', 'description': '编辑', 'acceptance': ['可保存']}]}}

    def change(self, fn, *args):
        with transaction(self.database) as conn:
            store = Ledger(conn, self.conf, datetime.now(timezone.utc))
            item = conn.execute('SELECT * FROM work_items').fetchone()
            return fn(store, item, *args)

    def tick(self):
        return sync_once(self.database, 'abc', lambda _: self.remote)

    def ready(self):
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("UPDATE split_tasks SET retry_at=NULL")

    def test_start_and_blocker_are_scoped_and_receipts_prevent_duplicate_comments(self):
        self.change(register_split, self.tasks)
        self.change(task_sync.record_start, 'F1', '2026-11-23T06:10:00Z')
        for _ in range(2):
            self.change(task_sync.record_note, 'F1', '开发已合回，部署失败')
            self.change(task_sync.record_note, 'F2', '等待后端部署')
        while self.tick():
            pass
        self.assertEqual(self.remote.tasks['100']['status'], 'doing')
        self.assertEqual(self.remote.tasks['101']['status'], 'wait')
        self.assertEqual(self.remote.calls.count('comment'), 2)

    def test_missing_effort_does_not_block_notes_or_eventual_close(self):
        self.tasks['tasks']['frontend'].pop()
        self.change(register_split, self.tasks)
        self.change(task_sync.record_start, 'F1', '2026-11-23T06:10:00Z')
        def blocked(*args):
            raise RemoteRejected('缺少有效工时')
        self.remote.start = blocked
        self.change(task_sync.record_note, 'F1', '部署失败')
        while self.tick():
            pass
        task = self.remote.tasks['100']
        self.assertEqual(task['status'], 'wait')
        self.assertIn('部署失败', task['comments'].values())
        self.assertTrue(any('已开工' in note for note in task['comments'].values()))
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertIn('缺少有效工时', conn.execute('SELECT last_error FROM split_tasks').fetchone()[0])
        self.change(set_desired, 'F1', 'closed')
        while self.tick():
            pass
        self.assertEqual(task['status'], 'closed')

    def test_lost_comment_response_is_reconciled_by_marker(self):
        self.tasks['tasks']['frontend'].pop()
        self.change(register_split, self.tasks)
        self.change(task_sync.record_note, 'F1', '部署失败')
        comment = self.remote.comment

        def lost(*args):
            comment(*args)
            raise TimeoutError('response lost')

        self.remote.comment = lost
        self.tick()
        self.tick()
        self.ready()
        self.tick()
        self.assertEqual(self.remote.calls.count('comment'), 1)

    def test_deferred_start_retries_with_original_start_time_after_effort_is_available(self):
        started_at = '2026-11-23T06:10:00Z'
        self.tasks['tasks']['frontend'].pop()
        self.change(register_split, self.tasks)
        self.change(task_sync.record_start, 'F1', started_at)
        original = self.remote.start
        starts = []

        def start(task_id, when):
            starts.append(when)
            if len(starts) == 1:
                raise RemoteRejected('缺少有效工时')
            original(task_id, when)

        self.remote.start = start
        while self.tick():
            pass
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("UPDATE writebacks SET expires_at='2000-01-01' WHERE state='deferred'")
        while self.tick():
            pass
        self.assertEqual(starts, [started_at, started_at])
        self.assertEqual(self.remote.tasks['100']['status'], 'doing')
        self.assertEqual(self.remote.calls.count('comment'), 1)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertIsNone(conn.execute('SELECT last_error FROM split_tasks').fetchone()[0])

    def test_split_create_close_reopen_and_distinct_new_requirement(self):
        self.change(register_split, self.tasks)
        self.change(register_split, self.tasks)
        self.change(set_desired, 'F1', 'closed')
        while self.tick():
            pass
        self.assertEqual(self.remote.calls.count('create'), 2)
        self.assertEqual(self.remote.tasks['100']['status'], 'closed')
        self.assertEqual(self.remote.tasks['101']['status'], 'wait')
        self.change(set_desired, 'F1', 'open')
        self.assertTrue(self.tick())
        self.assertEqual(self.remote.tasks['100']['status'], 'doing')
        self.change(set_desired, 'F1', 'closed')
        self.assertTrue(self.tick())
        self.assertEqual(self.remote.calls.count('close'), 2)
        self.tasks['tasks']['frontend'].append({'id': 'F3', 'description': '新增', 'acceptance': ['可用']})
        self.change(register_split, self.tasks)
        self.assertTrue(self.tick())
        self.assertEqual(len(self.remote.tasks), 3)

    def test_lost_response_does_not_create_twice_even_after_reassignment(self):
        self.tasks['tasks']['frontend'].pop()
        self.change(register_split, self.tasks)
        self.remote.lose_response = True
        self.assertTrue(self.tick())
        self.remote.available = False
        self.ready()
        self.assertTrue(self.tick())
        self.assertEqual(self.remote.calls, ['create'])
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT remote_id FROM split_tasks').fetchone()[0], '100')

    def test_ambiguous_iteration_then_resume(self):
        self.change(register_split, self.tasks)
        self.remote.available = False
        while self.tick():
            pass
        self.assertEqual(self.remote.calls, [])
        self.remote.available = True
        self.ready()
        while self.tick():
            pass
        self.assertEqual(len(self.remote.tasks), 2)

    def test_revision_cannot_drop_registered_tasks(self):
        self.change(register_split, self.tasks)
        self.tasks['tasks']['frontend'].pop(0)
        with self.assertRaises(ValueError):
            self.change(register_split, self.tasks)

    def test_reassignment_during_dedup_uses_current_owner(self):
        self.tasks['tasks']['frontend'].pop()
        self.change(register_split, self.tasks)
        owners = iter(('old', 'new'))
        self.remote.target = lambda *_: {'assignee': next(owners), 'execution': '7'}
        self.tick()
        self.assertEqual(self.remote.tasks['100']['assignee'], 'new')

    def test_competing_consumers_during_remote_create(self):
        self.tasks['tasks']['frontend'].pop()
        self.change(register_split, self.tasks)
        entered, release = Event(), Event()
        create = self.remote.create

        def slow_create(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError('worker did not release create')
            return create(*args)

        self.remote.create = slow_create
        thread = Thread(target=self.tick)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertFalse(self.tick())
            self.change(set_desired, 'F1', 'closed')
            self.assertFalse(self.tick())
        finally:
            release.set()
            thread.join(6)
        self.assertTrue(self.tick())
        self.assertEqual(self.remote.calls, ['create', 'close'])

    def test_no_replay_after_ambiguous_send_until_original_target_and_absence_proven(self):
        self.tasks['tasks']['frontend'].pop()
        self.change(register_split, self.tasks)
        create = self.remote.create

        def interrupted(*args):
            raise TimeoutError('server response unknown')

        self.remote.create = interrupted
        self.tick()
        self.ready()
        self.remote.create = create
        self.tick()
        self.assertFalse(self.remote.tasks)
        with closing(sqlite3.connect(self.database)) as conn:
            op, data = conn.execute('SELECT operation_id,payload FROM writebacks').fetchone()
        repair = {'operation_id': op, 'original_target': json.loads(data)['sent']}
        self.assertEqual(self.change(reconcile_missing, repair)['status'], 'reconciliation_needed')
        repair.update(prior_ended=True, missing=True, termination_evidence='request log ended',
                      readback_evidence='all pages in original execution contain no such marker')
        self.assertEqual(self.change(reconcile_missing, repair)['status'], 'task_ready_to_retry')
        self.tick()
        self.assertEqual(self.remote.calls, ['create'])

    def test_rejected_request_can_retry_but_a_later_read_failure_cannot_repeat_create(self):
        class Rejected(Exception):
            safe_to_retry = True

        self.tasks['tasks']['frontend'].pop()
        self.change(register_split, self.tasks)
        create, get = self.remote.create, self.remote.get

        def reject(*args):
            raise Rejected('request rejected')

        self.remote.create = reject
        self.tick()
        self.ready()
        self.remote.create = create
        self.remote.get = reject
        self.tick()
        self.ready()
        self.remote.get = get
        self.tick()
        self.assertEqual(self.remote.calls, ['create'])
        self.assertFalse(self.tick())
