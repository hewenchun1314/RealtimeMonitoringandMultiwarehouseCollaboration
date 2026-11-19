"""Lease individual pending writes and reconcile before retrying uncertain outcomes."""

import json
from uuid import uuid4

from ledger import encode, item_identity, message, require_text


def find_entry(store, item, data):
    operation_id = require_text(data.get('operation_id'), 'operation_id')
    row = store.conn.execute('SELECT * FROM writebacks WHERE operation_id=? AND item_seq=?',
                              (operation_id, item['seq'])).fetchone()
    if row is None:
        raise ValueError('Unknown writeback operation for this work item')
    return row


def lease_matches(store, entry, data):
    lease_id = require_text(data.get('lease_id'), 'lease_id')
    return (entry['state'] in ('leased', 'sending') and entry['lease_id'] == lease_id
            and store.time < entry['expires_at'])


def claim_writeback(store, item, data):
    entry = find_entry(store, item, data)
    owner = require_text(data.get('owner'), 'owner')
    if entry['state'] == 'confirmed':
        return message('already_confirmed', receipt=json.loads(entry['receipt']))
    if entry['state'] in ('leased', 'sending') and store.time < entry['expires_at']:
        return message('leased', expires_at=entry['expires_at'])
    lease_id = uuid4().hex
    expires_at = store.expiry('writeback_lease_seconds', 120)
    reconcile = entry['state'] in ('leased', 'sending') or bool(entry['reconcile_required'])
    store.conn.execute('''UPDATE writebacks SET state='leased', lease_id=?, owner=?, expires_at=?,
                                                reconcile_required=? WHERE operation_id=?''',
                       (lease_id, owner, expires_at, int(reconcile), entry['operation_id']))
    return message('writeback_claimed', lease_id=lease_id, expires_at=expires_at,
                   reconcile_required=reconcile, entry={'operation_id': entry['operation_id'],
                       'operation_type': entry['operation_type'], 'payload': json.loads(entry['payload']),
                       'item': item_identity(item)})


def reconcile_writeback(store, item, data):
    entry = find_entry(store, item, data)
    if not lease_matches(store, entry, data):
        return message('stale_lease')
    if entry['state'] != 'leased':
        return message('already_authorized')
    if data.get('prior_ended') is not True or data.get('missing') is not True:
        return message('reconciliation_needed')
    store.conn.execute('UPDATE writebacks SET reconcile_required=0 WHERE operation_id=?',
                       (entry['operation_id'],))
    return message('ready_to_send', expires_at=entry['expires_at'])


def authorize_writeback(store, item, data):
    entry = find_entry(store, item, data)
    if not lease_matches(store, entry, data):
        return message('stale_lease')
    if entry['state'] == 'sending':
        return message('already_authorized')
    if entry['reconcile_required']:
        return message('reconciliation_needed')
    store.conn.execute("UPDATE writebacks SET state='sending' WHERE operation_id=?", (entry['operation_id'],))
    return message('send_authorized', expires_at=entry['expires_at'])


def confirm_writeback(store, item, data):
    entry = find_entry(store, item, data)
    if entry['state'] == 'confirmed':
        return message('already_confirmed', receipt=json.loads(entry['receipt']))
    if not lease_matches(store, entry, data):
        return message('stale_lease')
    receipt = data.get('receipt')
    if not isinstance(receipt, dict) or not receipt:
        raise ValueError('receipt must be a non-empty object')
    store.conn.execute('''UPDATE writebacks SET state='confirmed',receipt=?,reconcile_required=0
                          WHERE operation_id=?''', (encode(receipt), entry['operation_id']))
    return message('writeback_confirmed', receipt=receipt)
