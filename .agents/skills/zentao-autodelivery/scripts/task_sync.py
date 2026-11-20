"""Durable split-task operations. All remote calls run outside ledger transactions."""

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from ledger import Ledger, encode, message, require_text, stamp, transaction
from writeback import claim_writeback, confirm_writeback, lease_matches


def enabled(item):
    snapshot = json.loads(item['config_snapshot'] or '{}')
    return (item['kind'] == 'story' and snapshot.get('workflow_version') == 2
            and snapshot.get('runtime', {}).get('split_task_sync') is True)


def task_marker(item, task_key):
    identity = encode([item['instance'], item['project_id'], item['item_id'], task_key])
    return 'ZT-SPLIT-' + hashlib.sha256(identity.encode()).hexdigest()[:32]


def queue(store, item, key, action, generation):
    marker = task_marker(item, key)
    operation_id = f'{marker}/{generation}/{action}'
    store.conn.execute("""INSERT INTO writebacks(operation_id,item_seq,run_round,operation_type,payload)
                          VALUES(?,?,?,?,?) ON CONFLICT(operation_id) DO NOTHING""",
                       (operation_id, item['seq'], item['run_round'], 'task_' + action,
                        encode({'key': key, 'marker': marker, 'generation': generation})))


def register_split(store, item, route):
    if not enabled(item):
        return
    entries = [(task['id'], side, task) for side, tasks in route['tasks'].items() for task in tasks]
    owners = {key: side for key, side, _ in entries}
    existing = store.conn.execute('SELECT task_key,side FROM split_tasks WHERE item_seq=?', (item['seq'],))
    if any(owners.get(row['task_key']) != row['side'] for row in existing):
        raise ValueError('A revised split must retain every registered task ID on its original side')
    for key, side, task in entries:
        store.conn.execute("""INSERT INTO split_tasks(item_seq,task_key,instance,side,definition)
                              VALUES(?,?,?,?,?) ON CONFLICT(item_seq,task_key)
                              DO UPDATE SET definition=excluded.definition""",
                           (item['seq'], key, item['instance'], side, encode(task)))
        queue(store, item, key, 'create', 0)


def set_desired(store, item, task_id, desired):
    if desired not in ('open', 'closed'):
        raise ValueError('Invalid task state')
    task = store.conn.execute('SELECT * FROM split_tasks WHERE item_seq=? AND task_key=?',
                              (item['seq'], task_id)).fetchone()
    if task is None:
        raise ValueError('Unknown split task')
    if task['desired'] == desired:
        return
    generation = task['generation'] + 1
    store.conn.execute('UPDATE split_tasks SET desired=?,generation=? WHERE item_seq=? AND task_key=?',
                       (desired, generation, item['seq'], task_id))
    queue(store, item, task_id, 'close' if desired == 'closed' else 'reopen', generation)


def claim_next(database, marker):
    with transaction(database) as conn:
        now = datetime.now(timezone.utc)
        current = stamp(now)
        row = conn.execute("""SELECT b.operation_id,b.operation_type,b.payload,b.item_seq,t.*,w.marker AS item_marker
            FROM writebacks b JOIN split_tasks t ON b.item_seq=t.item_seq AND json_extract(b.payload,'$.key')=t.task_key
            JOIN work_items w ON w.seq=b.item_seq
            WHERE b.operation_type IN ('task_create','task_close','task_reopen')
            AND (? IS NULL OR w.marker=?) AND (t.retry_at IS NULL OR t.retry_at<=?)
            AND (b.state='pending' OR (b.state IN ('leased','sending') AND b.expires_at<=?))
            AND NOT EXISTS (SELECT 1 FROM writebacks older WHERE older.item_seq=b.item_seq
                AND older.operation_type IN ('task_create','task_close','task_reopen')
                AND json_extract(older.payload,'$.key')=t.task_key AND older.rowid<b.rowid
                AND older.state NOT IN ('confirmed','superseded')) ORDER BY b.rowid LIMIT 1""",
                           (marker, marker, current, current)).fetchone()
        if row is None:
            return None
        item = dict(conn.execute('SELECT * FROM work_items WHERE seq=?', (row['item_seq'],)).fetchone())
        snapshot = json.loads(item['config_snapshot'])
        if not enabled(item):
            raise ValueError('Pending task operations require an enabled Story snapshot')
        store = Ledger(conn, snapshot, now)
        lease = claim_writeback(store, item, {'operation_id': row['operation_id'], 'owner': 'split-task-sync'})
        return dict(row), item, snapshot, lease['lease_id']


def reconcile_missing(store, item, data):
    """Release an uncertain write only with evidence for its original target and transmission."""
    operation = require_text(data.get('operation_id'), 'operation_id')
    entry = store.conn.execute('SELECT * FROM writebacks WHERE item_seq=? AND operation_id=?',
                               (item['seq'], operation)).fetchone()
    if not enabled(item) or not entry or entry['operation_type'] not in ('task_create', 'task_close', 'task_reopen'):
        raise ValueError('Unknown split-task operation')
    if entry['state'] in ('confirmed', 'superseded'):
        return message('task_already_resolved')
    if entry['state'] != 'pending' and entry['expires_at'] > store.time:
        return message('leased')
    payload = json.loads(entry['payload'])
    target = payload.get('sent')
    if not target or data.get('original_target') != target:
        return message('task_target_mismatch')
    if data.get('prior_ended') is not True or data.get('missing') is not True:
        return message('reconciliation_needed')
    require_text(data.get('termination_evidence'), 'termination_evidence')
    require_text(data.get('readback_evidence'), 'readback_evidence')
    del payload['sent']
    store.conn.execute("""UPDATE writebacks SET state='pending',payload=?,lease_id=NULL,expires_at=NULL,
                          reconcile_required=0 WHERE operation_id=?""", (encode(payload), operation))
    store.conn.execute('UPDATE split_tasks SET retry_at=NULL WHERE item_seq=? AND task_key=?',
                       (item['seq'], payload['key']))
    return message('task_ready_to_retry')


class LeaseLost(Exception):
    pass


@contextmanager
def owned(database, job):
    row, item, snapshot, lease = job
    with transaction(database) as conn:
        store = Ledger(conn, snapshot, datetime.now(timezone.utc))
        data = {'operation_id': row['operation_id'], 'lease_id': lease}
        entry = conn.execute('SELECT * FROM writebacks WHERE operation_id=?', (row['operation_id'],)).fetchone()
        if not lease_matches(store, entry, data):
            raise LeaseLost()
        yield store, data, entry


def prepare_send(database, job, intent):
    row, item, _, _ = job
    with owned(database, job) as (store, _, entry):
        payload = json.loads(entry['payload'])
        if 'sent' in payload:
            raise ValueError('发送结果待对账')
        task = store.conn.execute('SELECT * FROM split_tasks WHERE item_seq=? AND task_key=?',
                                  (item['seq'], row['task_key'])).fetchone()
        if row['operation_type'] != 'task_create' and task['generation'] != payload['generation']:
            store.conn.execute("UPDATE writebacks SET state='superseded' WHERE operation_id=?", (row['operation_id'],))
            return False
        payload['sent'] = intent
        store.conn.execute("UPDATE writebacks SET state='sending',payload=? WHERE operation_id=?",
                           (encode(payload), row['operation_id']))
        return True


def matches_task(actual, row, item, target):
    marker = json.loads(row['payload'])['marker']
    return (str(actual.get('story')) == item['item_id'] and str(actual.get('execution')) == str(target['execution'])
            and marker in actual.get('description', '') and str(actual.get('id', '')).isdecimal()
            and (not row['remote_id'] or str(actual['id']) == row['remote_id']))


def send(database, job, intent, method, *args, **kwargs):
    if not prepare_send(database, job, intent):
        raise LeaseLost()
    try:
        return method(*args, **kwargs)
    except Exception as exc:
        # Only the adapter that issued this write can prove a rejection.
        # A failure on a later GET must never invalidate a prior send.
        if getattr(exc, 'safe_to_retry', False):
            with owned(database, job) as (store, _, entry):
                payload = json.loads(entry['payload'])
                payload.pop('sent')
                store.conn.execute('UPDATE writebacks SET payload=? WHERE operation_id=?',
                                   (encode(payload), entry['operation_id']))
        raise


def sync_remote(database, job, remote):
    row, item, _, _ = job
    action = row['operation_type'][5:]
    payload = json.loads(row['payload'])
    sent = payload.get('sent')
    marker = payload['marker']
    if action == 'create':
        target = sent or remote.target(item['item_id'], item['project_id'])
        if not target.get('assignee') or not str(target.get('execution', '')).isdecimal():
            raise ValueError('缺少唯一执行或原需求指派人')
        found = remote.find(target['execution'], marker)
        if len(found) > 1:
            raise ValueError('拆分标识对应多张禅道任务，待核对')
        if found:
            actual = remote.get(str(found[0]['id']))
        elif sent:
            raise ValueError('创建已发送，尚未找到可唯一核实的回执')
        else:
            latest = remote.target(item['item_id'], item['project_id'])
            if str(latest['execution']) != str(target['execution']):
                raise ValueError('查重期间需求迭代已变更，待核对新迭代')
            target = latest
            definition = json.loads(row['definition'])
            side = {'backend': '后端', 'frontend': '前端'}[row['side']]
            title = f'【{side}】' + definition['description'].strip().splitlines()[0][:160]
            description = '\n'.join([row['task_key'], definition['description'], '验收条件',
                                     *('- ' + criterion for criterion in definition['acceptance'])])
            number = send(database, job, target, remote.create, target, item['item_id'], title, description, marker)
            actual = remote.get(str(number))
        if actual.get('assignee') != target['assignee']:
            raise ValueError('初始指派账号与建单目标不符，待核对')
    else:
        target = {'execution': row['execution_id'], 'assignee': row['assignee']}
        actual = remote.get(row['remote_id'])
        if not matches_task(actual, row, item, target):
            raise ValueError('远端任务身份与已登记的拆分项不符')
        acceptable = {'closed'} if action == 'close' else {'wait', 'doing', 'pause'}
        if actual.get('status') not in acceptable:
            if sent:
                raise ValueError('状态变更已发送，尚未读回目标状态')
            options = {'assignee': target['assignee']} if action == 'reopen' else {}
            send(database, job, {'id': row['remote_id'], 'action': action},
                 remote.transition, row['remote_id'], action, **options)
            actual = remote.get(row['remote_id'])
            if actual.get('status') not in acceptable:
                raise ValueError('禅道尚未确认目标状态')
    if not matches_task(actual, row, item, target) or not actual.get('status'):
        raise ValueError('未核实禅道任务身份或状态')
    return {'id': str(actual['id']), 'story': item['item_id'], 'project': item['project_id'],
            'execution': str(target['execution']), 'assignee': target['assignee'], 'status': actual['status']}


def sync_once(database, marker=None, remote_factory=None):
    """Process one eligible operation, or return False when the queue is idle."""
    job = claim_next(database, marker)
    if not job:
        return False
    row, item, snapshot, _ = job
    identity = (item['seq'], row['task_key'])
    try:
        if remote_factory is None:
            from task_remote import get_remote
            remote_factory = get_remote
        receipt = sync_remote(database, job, remote_factory(snapshot))
        if receipt is not None:
            with owned(database, job) as (store, data, _):
                store.conn.execute("""UPDATE split_tasks SET remote_id=?,remote_status=?,execution_id=?,assignee=?,
                                     last_error=NULL,retry_at=NULL WHERE item_seq=? AND task_key=?""",
                                   (receipt['id'], receipt['status'], receipt['execution'], receipt['assignee'], *identity))
                confirm_writeback(store, item, {**data, 'receipt': receipt})
    except LeaseLost:
        pass
    except Exception as exc:
        # No retry of an uncertain external send; the saved target is reconciled on the next pass.
        try:
            with owned(database, job) as (store, _, entry):
                store.conn.execute("UPDATE writebacks SET state='pending',lease_id=NULL WHERE operation_id=?",
                                   (entry['operation_id'],))
                store.conn.execute('UPDATE split_tasks SET retry_at=?,last_error=? WHERE item_seq=? AND task_key=?',
                                   (stamp(store.now + timedelta(minutes=1)), str(exc)[:350], *identity))
        except LeaseLost:
            pass
    return True
