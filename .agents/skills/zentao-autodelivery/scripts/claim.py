"""Queue a complete scan and reserve its current, freshly verified candidate."""

import hashlib
from uuid import uuid4

from ledger import encode, item_identity, item_key, message, observed, require_mapping, require_text, source_id, source_kind


def candidate(conn, instance, project_id):
    return conn.execute('''SELECT * FROM work_items WHERE instance=? AND project_id=?
                           AND state='queued' AND eligible=1
                           ORDER BY priority IS NULL, priority, seq LIMIT 1''', (instance, project_id)).fetchone()


def eligible(zentao, kind, assigned, status):
    return (assigned in zentao['assigned_to_accounts'] and status in zentao['accepted_statuses'][kind])


def scan_item(value):
    value = require_mapping(value, 'items entry')
    if not isinstance(value.get('title'), str):
        raise ValueError('items entry title must be a string')
    assigned = value.get('assigned_to')
    if not isinstance(assigned, str):
        raise ValueError('items entry assigned_to must be a string')
    priority = value.get('priority')
    if type(priority) is not int or priority < 1:
        priority = None
    return (source_kind(value.get('kind')), source_id(value.get('id'), 'id'), value['title'], assigned,
            require_text(value.get('status'), 'status'), priority)


def save_scan(store, data):
    zentao = store.config['zentao']
    instance = require_text(data.get('instance'), 'instance').rstrip('/')
    project_id = source_id(data.get('project_id'), 'project_id')
    if instance != zentao['base_url'].rstrip('/') or data.get('project_name') != zentao['project_name']:
        return message('scope_mismatch', candidate=None)
    if type(data.get('complete')) is not bool:
        raise ValueError('complete must be a boolean')
    if not data['complete']:
        return message('partial', candidate=None)
    scan_id = require_text(data.get('scan_id'), 'scan_id')
    received_at = observed(data.get('observed_at'), 'observed_at')
    scope = (instance, project_id)
    latest = store.conn.execute('SELECT * FROM scans WHERE instance=? AND project_id=?', scope).fetchone()
    if not store.fresh_observation(received_at) or (latest and (
            latest['scan_id'] == scan_id or received_at <= latest['observed_at'])):
        current = candidate(store.conn, *scope)
        return message('stale', candidate=item_identity(current) if current else None)

    items = data.get('items')
    if not isinstance(items, list):
        raise ValueError('items must be an array')
    normalized = {}
    for value in items:
        row = scan_item(value)
        key = row[:2]
        if key in normalized and normalized[key] != row:
            raise ValueError('Scan contains conflicting copies of a work item')
        normalized[key] = row

    for (kind, item_id), (_, _, title, assigned, status, priority) in normalized.items():
        current_eligibility = int(eligible(zentao, kind, assigned, status))
        marker = 'HNF-' + hashlib.sha256(encode((*scope, kind, item_id)).encode()).hexdigest()[:20]
        state = 'baseline' if latest is None else ('queued' if current_eligibility else 'ineligible')
        store.conn.execute('''
            INSERT INTO work_items(instance, project_id, kind, item_id, title, assigned_to,
                                   source_status, priority, eligible, observed_at, state, marker)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(instance, project_id, kind, item_id) DO UPDATE SET
                title=excluded.title, assigned_to=excluded.assigned_to, source_status=excluded.source_status,
                priority=excluded.priority, eligible=excluded.eligible, observed_at=excluded.observed_at,
                state=CASE WHEN work_items.state IN ('queued','ineligible') THEN excluded.state
                           ELSE work_items.state END
            WHERE work_items.observed_at <= excluded.observed_at
        ''', (*scope, kind, item_id, title, assigned, status, priority, current_eligibility, received_at, state, marker))

    # A complete scoped scan is evidence of absence. Newer individual rechecks take priority.
    store.conn.execute('''
        UPDATE work_items SET eligible=0, observed_at=?,
            state=CASE WHEN state='queued' THEN 'ineligible' ELSE state END
        WHERE instance=? AND project_id=? AND observed_at < ? AND eligible=1
    ''', (received_at, *scope, received_at))
    store.conn.execute('''INSERT INTO scans(instance,project_id,project_name,scan_id,observed_at) VALUES(?,?,?,?,?)
                          ON CONFLICT(instance,project_id) DO UPDATE SET
                              project_name=excluded.project_name, scan_id=excluded.scan_id,
                              observed_at=excluded.observed_at''', (*scope, data['project_name'], scan_id, received_at))
    current = candidate(store.conn, *scope)
    return message('baseline' if latest is None else 'scanned',
                   candidate=item_identity(current) if current else None)


def reserve(store, data):
    requested = item_key(data.get('item'))
    zentao = store.config['zentao']
    if requested[0] != zentao['base_url'].rstrip('/'):
        return message('scope_mismatch')
    scope = store.conn.execute('SELECT project_name FROM scans WHERE instance=? AND project_id=?',
                               requested[:2]).fetchone()
    if scope is None or scope['project_name'] != zentao['project_name']:
        return message('scope_mismatch')
    in_use = store.conn.execute('''SELECT COUNT(*) FROM work_items
                                  WHERE state IN ('reserved','bound','running','closing')''').fetchone()[0]
    if in_use >= store.config['development']['max_concurrent_items']:
        return message('busy')
    head = candidate(store.conn, *requested[:2])
    if head is None:
        return message('no_candidate')
    if item_key(item_identity(head)) != requested:
        return message('queue_changed', candidate=item_identity(head))
    check_time = observed(data.get('observed_at'), 'observed_at')
    if check_time < head['observed_at'] or not store.fresh_observation(check_time):
        return message('stale_check', candidate=item_identity(head))
    assigned = data.get('assigned_to')
    if not isinstance(assigned, str):
        raise ValueError('assigned_to must be a string')
    status = require_text(data.get('status'), 'status')
    if not eligible(zentao, head['kind'], assigned, status):
        store.update_item(head, state='ineligible', eligible=0, assigned_to=assigned,
                          source_status=status, observed_at=check_time)
        return message('not_eligible')
    desktop_project = require_text(data.get('desktop_project_id'), 'desktop_project_id')
    expected = store.config.get('runtime', {}).get('desktop_project_id') or head['desktop_project_id']
    if expected and desktop_project != expected:
        return message('desktop_project_mismatch')
    request_id = uuid4().hex
    expires_at = store.expiry('dispatch_lease_seconds', 300)
    store.conn.execute('''INSERT INTO dispatches(request_id,item_seq,desktop_project_id,state,expires_at)
                         VALUES(?,?,?,'reserved',?)''', (request_id, head['seq'], desktop_project, expires_at))
    # Keep configuration for the original admission across retries and recovery rounds.
    snapshot = head['config_snapshot'] or encode({**store.config, 'workflow_version': 2})
    state = 'bound' if head['chat_id'] else 'reserved'
    store.update_item(head, request_id=request_id, state=state, assigned_to=assigned, source_status=status,
                      observed_at=check_time, desktop_project_id=desktop_project, config_snapshot=snapshot)
    return message('reserved', item=item_identity(head), request_id=request_id, expires_at=expires_at,
                   marker=head['marker'], chat_id=head['chat_id'], desktop_project_id=desktop_project)
