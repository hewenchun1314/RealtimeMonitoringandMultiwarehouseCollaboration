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
