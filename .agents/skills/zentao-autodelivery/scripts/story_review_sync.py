"""Drive the formal Story review and resume the original work item from its receipt."""

import json
from datetime import datetime, timezone

from ledger import Ledger, encode, message, transaction
from story_review_remote import ReviewBlocked
from task_remote import RemoteRejected, RemoteUnknown
from writeback import authorize_writeback, claim_writeback, confirm_writeback


def release_handoff(database, config, identity):
    with transaction(database) as conn:
        store = Ledger(conn, config, datetime.now(timezone.utc))
        item = store.find_item(identity)
        if item['state'] != 'review_handoff':
            return False
        store.update_item(item, state='review_wait')
        return True


def pending_review(store, identity):
    item = store.find_item(identity)
    if item['state'] not in ('review_wait', 'review_ready') or item['stage'] != 'material_check':
        return item, None
    entry = store.conn.execute('''SELECT * FROM writebacks WHERE item_seq=? AND run_round=?
                                  AND operation_type='story_review' ''', (item['seq'], item['run_round'])).fetchone()
    return item, entry


def vote_action(observed, payload):
    for action in observed['actions']:
        if (int(action['id']) > int(payload['round']) and action['action'] == 'reviewed'
                and action['actor'] == payload['account'] and f'[{payload["vote_key"]}]' in action['comment']
                and action['extra'].lower().split('|', 1)[0] == payload['result']):
            return str(action['id'])
    return None


def review_result(observed, payload):
    target = observed['target']
    base = {'story_status': target['status'], 'version': target['version'], 'round': target['round']}
    if any(target[key] != payload[key] for key in ('version', 'round', 'account')):
        return {**base, 'progress': 'blocked', 'reason': '禅道需求版本、评审轮次或评审账号已变化，须重新评审材料'}
    action_id = vote_action(observed, payload)
    if action_id:
        receipt = {**base, 'action_id': action_id, 'result': payload['result']}
        if payload['result'] == 'clarify':
            return {**receipt, 'progress': 'clarified'}
        if target['status'] == 'active':
            return {**receipt, 'progress': 'passed'}
        if target['status'] == 'reviewing':
            return {**receipt, 'progress': 'waiting_votes'}
        return {**receipt, 'progress': 'blocked', 'reason': '禅道评审结束后需求未激活'}
    if target['status'] == 'active' and payload['status'] == 'active' and payload['result'] == 'pass':
        return {**base, 'progress': 'passed', 'existing_active': True}
    if target['status'] != 'reviewing':
        return {**base, 'progress': 'blocked', 'reason': '本轮禅道评审已结束或尚未发起，当前状态不支持提交该意见'}
    return None


def sync_review(database, config, remote, identity):
    def ledger(conn):
        return Ledger(conn, config, datetime.now(timezone.utc))

    with transaction(database) as conn:
        item, entry = pending_review(ledger(conn), identity)
        if not entry:
            return message('review_not_pending')
        if item['state'] == 'review_ready' and entry['state'] == 'confirmed':
            receipt = json.loads(entry['receipt'])
            if receipt['progress'] == 'blocked':
                return message('review_ready', receipt=receipt)
        payload = json.loads(entry['payload'])
        params = {'operation_id': entry['operation_id'], 'owner': 'story-review-worker'}

    def publish(receipt):
        with transaction(database) as conn:
            store = ledger(conn)
            current, latest = pending_review(store, identity)
            if not latest or latest['operation_id'] != params['operation_id']:
                return message('review_not_pending')
            receipt = {**receipt, 'checked_at': store.time}
            if latest['state'] == 'confirmed':
                conn.execute('UPDATE writebacks SET receipt=? WHERE operation_id=?',
                             (encode(receipt), latest['operation_id']))
            elif latest['state'] in ('leased', 'sending') and latest['lease_id'] != params.get('lease_id'):
                if store.time < latest['expires_at']:
                    return message('review_busy')
                conn.execute('''UPDATE writebacks SET state='confirmed',receipt=?,reconcile_required=0
                                WHERE operation_id=?''', (encode(receipt), latest['operation_id']))
            elif latest['state'] in ('leased', 'sending'):
                confirm_writeback(store, current, {**params, 'receipt': receipt})
                saved = conn.execute('SELECT state FROM writebacks WHERE operation_id=?',
                                     (latest['operation_id'],)).fetchone()
                if saved['state'] != 'confirmed':
                    return message('review_busy')
            else:
                conn.execute('''UPDATE writebacks SET state='confirmed',receipt=?,reconcile_required=0
                                WHERE operation_id=?''', (encode(receipt), latest['operation_id']))
            if receipt['progress'] == 'waiting_votes':
                store.update_item(current, state='review_wait')
                return message('review_waiting_votes', receipt=receipt)
            store.update_item(current, state='review_ready')
            return message('review_ready', receipt=receipt)

    def defer(status, reason, *, uncertain=False):
        with transaction(database) as conn:
            store = ledger(conn)
            _, latest = pending_review(store, identity)
            if latest and latest['operation_id'] == params['operation_id']:
                diagnostic = {'status': status, 'reason': reason, 'checked_at': store.time}
                if latest['state'] == 'confirmed':
                    receipt = json.loads(latest['receipt'])
                    conn.execute('UPDATE writebacks SET receipt=? WHERE operation_id=?',
                                 (encode({**receipt, 'checked_at': store.time}), latest['operation_id']))
                elif (latest['state'] == 'pending' or latest['lease_id'] == params.get('lease_id')
                      or (latest['expires_at'] and store.time >= latest['expires_at'])):
                    expired_send = latest['state'] == 'sending' and latest['lease_id'] != params.get('lease_id')
                    conn.execute('''UPDATE writebacks SET state='pending',reconcile_required=?,receipt=?
                                    WHERE operation_id=?''',
                                 (int(uncertain or expired_send or latest['reconcile_required']),
                                  encode(diagnostic), latest['operation_id']))
        return message(status, reason=reason)

    try:
        receipt = review_result(remote.observe(item), payload)
    except ReviewBlocked as exc:
        return publish({'progress': 'blocked', 'reason': str(exc)})
    except RemoteRejected as exc:
        return defer('review_retry', str(exc))
    if receipt:
        return publish(receipt)
    if entry['state'] == 'confirmed' or entry['reconcile_required'] or entry['state'] == 'sending':
        return defer('review_write_uncertain', '评审历史暂未核实，保留操作供后续对账')

    with transaction(database) as conn:
        store = ledger(conn)
        current, latest = pending_review(store, identity)
        if not latest or latest['operation_id'] != params['operation_id']:
            return message('review_not_pending')
        if latest['state'] in ('confirmed', 'sending') or latest['reconcile_required']:
            return message('review_busy')
        if latest['state'] == 'leased' and store.time >= latest['expires_at']:
            # A lease alone has not authorized a remote request.
            conn.execute("UPDATE writebacks SET state='pending' WHERE operation_id=?", (latest['operation_id'],))
        lease = claim_writeback(store, current, params)
        if lease['status'] != 'writeback_claimed':
            return message('review_busy')
        params['lease_id'] = lease['lease_id']
        if authorize_writeback(store, current, params)['status'] != 'send_authorized':
            return message('review_busy')

    try:
        remote.submit(item, payload)
    except ReviewBlocked as exc:
        return publish({'progress': 'blocked', 'reason': str(exc)})
    except RemoteRejected as exc:
        return defer('review_retry', str(exc))
    except RemoteUnknown as exc:
        return defer('review_write_uncertain', str(exc), uncertain=True)
    try:
        receipt = review_result(remote.observe(item), payload)
    except ReviewBlocked as exc:
        return publish({'progress': 'blocked', 'reason': str(exc)})
    except RemoteRejected:
        receipt = None
    if receipt is None:
        return defer('review_write_uncertain', '禅道已应答，评审记录待回读确认', uncertain=True)
    return publish(receipt)


def rearm_uncertain(database, config, remote, identity, *, prior_ended):
    if not prior_ended:
        return message('review_prior_request_unverified')

    def uncertain(store, item, entry):
        return (item['state'] == 'review_wait' and entry and (entry['reconcile_required'] or entry['state'] == 'sending')
                and not (entry['state'] in ('leased', 'sending') and store.time < entry['expires_at']))

    with transaction(database) as conn:
        store = Ledger(conn, config, datetime.now(timezone.utc))
        item, entry = pending_review(store, identity)
        if not uncertain(store, item, entry):
            return message('review_not_rearmable')
        payload = json.loads(entry['payload'])
    try:
        if review_result(remote.observe(item), payload):
            return message('review_readback_available')
        remote.verify_pending(item, payload)
    except RemoteRejected as exc:
        return message('review_retry', reason=str(exc))
    with transaction(database) as conn:
        store = Ledger(conn, config, datetime.now(timezone.utc))
        current, latest = pending_review(store, identity)
        if (not uncertain(store, current, latest) or latest['operation_id'] != entry['operation_id']
                or latest['lease_id'] != entry['lease_id']):
            return message('review_not_rearmable')
        receipt = encode({'status': 'review_rearmed', 'checked_at': store.time})
        conn.execute('''UPDATE writebacks SET state='pending',reconcile_required=0,receipt=?
                        WHERE operation_id=?''', (receipt, entry['operation_id']))
    return message('review_rearmed', operation_id=entry['operation_id'])


def resume_ready(database, config, identity):
    with transaction(database) as conn:
        store = Ledger(conn, config, datetime.now(timezone.utc))
        item, entry = pending_review(store, identity)
        if item['state'] != 'review_ready' or not entry or entry['state'] != 'confirmed':
            return message('review_not_ready')
        in_use = conn.execute('''SELECT COUNT(*) FROM work_items
                                 WHERE state IN ('reserved','bound','running','closing','review_handoff')''').fetchone()[0]
        if in_use >= config['development']['max_concurrent_items']:
            return message('review_capacity_wait')
        receipt = json.loads(entry['receipt'])
        progress = receipt['progress']
        if progress == 'passed':
            state, stage, conclusion = 'running', 'split', None
        elif progress == 'clarified':
            state, stage, conclusion = 'closing', 'material_check', '待补材料'
        elif progress == 'blocked':
            state, stage, conclusion = 'closing', 'material_check', '阻断'
        else:
            return message('review_not_ready')
        store.update_item(item, state=state, stage=stage, conclusion=conclusion)
        return message('review_resumed', stage=stage, item_state=state, conclusion=conclusion, receipt=receipt)
