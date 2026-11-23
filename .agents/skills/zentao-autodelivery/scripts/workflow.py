"""Two JSON commands for project intake and durable step records."""

import argparse
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from backend_dependencies import dependency_locations, writable_dependencies
from branch_check import BRANCH_STAGES, branch_context, inspect_branches, prepare_local_worktree, valid_evidence
from claim import reserve, save_scan
from desktop_runtime import runtime_check
from dingtalk_notify import dispatch as dispatch_dingtalk, enabled as dingtalk_enabled, enqueue_notification
from execution import (
    active_attempt, attempt_finish, attempt_start, bind, complete, defer_dispatch, execution_matches,
    expire_dispatch, material_sync_required, recover, repair_advance, resolve_dispatch, start, stop,
)
from ledger import Ledger, encode, message, require_mapping, require_text, transaction, validate_config
from material_sync import requirement_checkout, sync_requirement
from route_execution import snapshot
from status_board import publish_status
from task_sync import reconcile_missing
from writeback import authorize_writeback, claim_writeback, confirm_writeback, find_entry, reconcile_writeback


PROJECT_ROOT = Path(__file__).resolve().parents[4]
CONFIG_PATH = PROJECT_ROOT / '.agents' / 'zentao-project.json'
DATABASE_PATH = PROJECT_ROOT / '.local' / 'zentao-delivery' / 'workflow.sqlite3'

CLAIM_ACTIONS = {'scan': save_scan, 'reserve': reserve}
RECORD_ACTIONS = {
    'task_reconcile_missing': reconcile_missing,
    'bind': bind,
    'start': start,
    'attempt_start': attempt_start,
    'attempt_finish': attempt_finish,
    'repair_advance': repair_advance,
    'defer_dispatch': defer_dispatch,
    'expire_dispatch': expire_dispatch,
    'resolve_dispatch': resolve_dispatch,
    'complete': complete,
    'stop': stop,
    'recover': recover,
    'writeback_claim': claim_writeback,
    'writeback_reconcile': reconcile_writeback,
    'writeback_send': authorize_writeback,
    'writeback_confirm': confirm_writeback,
}
REPLAYED_PERMISSIONS = {
    'started': 'already_started',
    'attempt_started': 'attempt_active',
    'repair_advanced': 'phase_already_advanced',
    'send_authorized': 'already_authorized',
}


def read_branch_attempt(database, config, data, now, config_directory):
    database = Path(database).resolve()
    if not database.is_file():
        return None
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        store = Ledger(conn, config, now)
        item = store.find_item(data.get('item'))
        if item['state'] != 'running' or item['stage'] not in BRANCH_STAGES or not execution_matches(store, item, data):
            return None
        trial = active_attempt(store, item)
        if not trial or trial['attempt_id'] != data.get('attempt_id') or store.time >= trial['deadline_at']:
            return None
        context = branch_context(store, item)
        live_dependencies = writable_dependencies(config)
        revoked = [key for key, workspace in context.get('dependency_workspaces', {}).items()
                   if key not in live_dependencies or (config_directory / workspace).resolve() !=
                   (config_directory / live_dependencies[key]['source_path']).resolve()]
        if revoked:
            context['revoked_dependencies'] = revoked
        return {**context, 'deadline_at': trial['deadline_at']}


def sync_material_attempt(database, config, data, now, config_directory):
    if not Path(database).is_file():
        return message('material_sync_inactive')

    def active_trial(store):
        item = store.find_item(data.get('item'))
        if not (item['state'] == 'running' and execution_matches(store, item, data)
                and material_sync_required(store, item)):
            return None, None
        trial = active_attempt(store, item)
        if not trial or trial['attempt_id'] != data.get('attempt_id') or store.time >= trial['deadline_at']:
            return None, None
        return item, trial

    with transaction(database) as conn:
        store = Ledger(conn, config, now or datetime.now(timezone.utc))
        item, trial = active_trial(store)
        if trial is None:
            return message('material_sync_inactive')
        previous = conn.execute('SELECT receipt FROM requirement_sync_receipts WHERE attempt_id=?',
                                (trial['attempt_id'],)).fetchone()
        if previous:
            return message('material_synced', **json.loads(previous['receipt']))
        checkout = requirement_checkout(snapshot(item), config_directory)
        if checkout is None or checkout != requirement_checkout(config, config_directory):
            return message('material_sync_failed', reason='The configured requirement checkout changed or is invalid')
        budget = (datetime.fromisoformat(trial['deadline_at']) - store.now).total_seconds()

    try:
        receipt = sync_requirement(checkout, budget)
    except (OSError, ValueError) as error:
        return message('material_sync_failed', reason=str(error))

    with transaction(database) as conn:
        _, trial = active_trial(Ledger(conn, config, now or datetime.now(timezone.utc)))
        if trial is None:
            return message('material_sync_inactive')
        conn.execute('INSERT OR IGNORE INTO requirement_sync_receipts(attempt_id, receipt) VALUES (?, ?)',
                     (trial['attempt_id'], encode(receipt)))
        saved = conn.execute('SELECT receipt FROM requirement_sync_receipts WHERE attempt_id=?',
                             (trial['attempt_id'],)).fetchone()
    return message('material_synced', **json.loads(saved['receipt']))


def run(command, data, config, database, *, now=None, config_directory=CONFIG_PATH.parent):
    validate_config(config)
    dependency_locations(config, config_directory)
    data = require_mapping(data, 'request')
    action = require_text(data.get('action'), 'action')
    if command == 'record' and action == 'runtime_check':
        return runtime_check(require_text(data.get('chat_id'), 'chat_id'))
    if command == 'record' and action == 'material_sync':
        return sync_material_attempt(database, config, data, now, config_directory)
    check_time = now or datetime.now(timezone.utc)
    if command == 'record' and action in ('branch_inspect', 'branch_prepare'):
        context = read_branch_attempt(database, config, data, check_time, config_directory)
        if context is None:
            return message('branch_attempt_inactive')
        if action == 'branch_prepare':
            reconciliation = data.get('desktop_reconciliation')
            if (not isinstance(reconciliation, dict) or reconciliation.get('status') not in
                    ('no_receipt', 'failed', 'unavailable') or not reconciliation.get('evidence')):
                return message('desktop_reconciliation_required')
            field = 'service' if context.get('side') == 'backend' else 'application'
            app = require_text(data.get(field), field)
            budget = (datetime.fromisoformat(context['deadline_at']) - check_time).total_seconds()
            try:
                repo = prepare_local_worktree(context, config_directory, database, app, budget)
            except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                return message('branch_prepare_failed', **{field: app}, reason=str(exc))
            status = 'branch_prepared' if repo['status'] == 'verified' else 'branch_prepare_blocked'
            return message(status, **{field: app}, repository=repo, backend='git',
                           desktop_reconciliation=reconciliation)
        worktrees = require_mapping(data.get('worktrees', {}), 'worktrees')
        if worktrees.keys() - context['branch_targets'].keys():
            raise ValueError('Every worktree must match a planned application')
        return message('branch_inspected', **context,
                       repositories=inspect_branches(context, config_directory, worktrees))
    actions = {'claim': CLAIM_ACTIONS, 'record': RECORD_ACTIONS}.get(command)
    if actions is None or action not in actions:
        raise ValueError('Unknown command or action')
    if command == 'claim' and (not config['monitor']['enabled'] or not config['zentao']['assigned_to_accounts']):
        return message('disabled')
    branches_verified = False
    evidence = data.get('evidence')
    if (command == 'record' and action == 'attempt_finish' and data.get('outcome') == 'success'
            and isinstance(evidence, dict) and 'repositories' in evidence):
        context = read_branch_attempt(database, config, data, check_time, config_directory)
        if context and valid_evidence(context, evidence):
            claimed = evidence['repositories']
            actual = inspect_branches(context, config_directory, {
                application: proof['worktree'] for application, proof in claimed.items()
            })
            fields = ('target_branch', 'source_ref', 'source_root', 'base_commit', 'worktree', 'worktree_branch')
            branches_verified = all(actual[application]['status'] == 'verified'
                                    and all(proof[field] == actual[application][field] for field in fields)
                                    for application, proof in claimed.items())
    with transaction(database) as conn:
        store = Ledger(conn, config, now or datetime.now(timezone.utc))
        if command == 'claim':
            return actions[action](store, data)

        event_id = require_text(data.get('event_id'), 'event_id')
        request = encode(data)
        previous = conn.execute('SELECT request,response FROM events WHERE event_id=?', (event_id,)).fetchone()
        if previous:
            if previous['request'] != request:
                raise ValueError('An event_id cannot be reused with different data')
            response = json.loads(previous['response'])
            if response['status'] in REPLAYED_PERMISSIONS:
                response['status'] = REPLAYED_PERMISSIONS[response['status']]
                response['replayed'] = True
            return response
        item = store.find_item(data.get('item'))
        if action.startswith('writeback_'):
            operation_type = find_entry(store, item, data)['operation_type']
            if operation_type.startswith('task_'):
                return message('task_sync_required')
            if operation_type == 'story_review':
                return message('story_review_worker_required')
        if action == 'attempt_finish':
            response = attempt_finish(store, item, data, branches_verified=branches_verified,
                                      config_directory=config_directory)
        elif action in ('attempt_start', 'repair_advance', 'recover'):
            response = actions[action](store, item, data, config_directory=config_directory)
        else:
            response = actions[action](store, item, data)
        if dingtalk_enabled(config) and response['status'] in ('started', 'completed'):
            enqueue_notification(store, item, response)
        conn.execute('INSERT INTO events(event_id,request,response) VALUES(?,?,?)',
                     (event_id, request, encode(response)))
        return response


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('claim', 'record'))
    parser.add_argument('--config', type=Path, default=CONFIG_PATH)
    parser.add_argument('--database', type=Path, default=DATABASE_PATH)
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding='utf-8-sig'))
        data = json.loads(sys.stdin.buffer.read().decode('utf-8-sig'))
        response = run(args.command, data, config, args.database, config_directory=args.config.resolve().parent)
        if data['action'] != 'runtime_check' and args.database.resolve() == DATABASE_PATH and args.database.is_file():
            try:
                publish_status(DATABASE_PATH, config)
            except (OSError, sqlite3.Error, ValueError) as error:
                print(f'Status page refresh failed: {error}', file=sys.stderr)
        if (dingtalk_enabled(config) and args.config.resolve() == CONFIG_PATH
                and args.database.resolve() == DATABASE_PATH and response['status'] in ('started', 'completed')):
            response['dingtalk'] = dispatch_dingtalk()
        exit_code = 0
    except (ValueError, OSError, sqlite3.Error) as error:
        response = {'ok': False, 'status': 'error', 'error_type': type(error).__name__, 'message': str(error)}
        exit_code = 1
    print(json.dumps(response, ensure_ascii=True, allow_nan=False))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
