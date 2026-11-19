"""Two JSON commands for project intake and durable step records."""

import argparse
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from branch_check import branch_context, inspect_branches, prepare_local_worktree, valid_evidence
from claim import reserve, save_scan
from desktop_runtime import runtime_check
from execution import (
    active_attempt, attempt_finish, attempt_start, bind, complete, defer_dispatch, execution_matches,
    expire_dispatch, recover, repair_advance, resolve_dispatch, start, stop,
)
from ledger import Ledger, encode, message, require_mapping, require_text, transaction, validate_config
from status_board import publish_status
from writeback import authorize_writeback, claim_writeback, confirm_writeback, reconcile_writeback


PROJECT_ROOT = Path(__file__).resolve().parents[4]
CONFIG_PATH = PROJECT_ROOT / '.agents' / 'zentao-project.json'
DATABASE_PATH = PROJECT_ROOT / '.local' / 'zentao-delivery' / 'workflow.sqlite3'

CLAIM_ACTIONS = {'scan': save_scan, 'reserve': reserve}
RECORD_ACTIONS = {
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


def read_branch_attempt(database, config, data, now):
    database = Path(database).resolve()
    if not database.is_file():
        return None
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        store = Ledger(conn, config, now)
        item = store.find_item(data.get('item'))
        if item['state'] != 'running' or item['stage'] != 'branch_check' or not execution_matches(store, item, data):
            return None
        trial = active_attempt(store, item)
        if not trial or trial['attempt_id'] != data.get('attempt_id') or store.time >= trial['deadline_at']:
            return None
        return {**branch_context(store, item), 'deadline_at': trial['deadline_at']}


def run(command, data, config, database, *, now=None, config_directory=CONFIG_PATH.parent):
    validate_config(config)
    data = require_mapping(data, 'request')
    action = require_text(data.get('action'), 'action')
    if command == 'record' and action == 'runtime_check':
        return runtime_check(require_text(data.get('chat_id'), 'chat_id'))
    check_time = now or datetime.now(timezone.utc)
    if command == 'record' and action in ('branch_inspect', 'branch_prepare'):
        context = read_branch_attempt(database, config, data, check_time)
        if context is None:
            return message('branch_attempt_inactive')
        if action == 'branch_prepare':
            reconciliation = data.get('desktop_reconciliation')
            if (not isinstance(reconciliation, dict) or reconciliation.get('status') not in
                    ('no_receipt', 'failed', 'unavailable') or not reconciliation.get('evidence')):
                return message('desktop_reconciliation_required')
            app = require_text(data.get('application'), 'application')
            budget = (datetime.fromisoformat(context['deadline_at']) - check_time).total_seconds()
            try:
                repo = prepare_local_worktree(context, config_directory, database, app, budget)
            except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                return message('branch_prepare_failed', application=app, reason=str(exc))
            status = 'branch_prepared' if repo['status'] == 'verified' else 'branch_prepare_blocked'
            return message(status, application=app, repository=repo, backend='git',
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
        context = read_branch_attempt(database, config, data, check_time)
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
        if action == 'attempt_finish':
            response = attempt_finish(store, item, data, branches_verified=branches_verified)
        else:
            response = actions[action](store, item, data)
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
        exit_code = 0
    except (ValueError, OSError, sqlite3.Error) as error:
        response = {'ok': False, 'status': 'error', 'error_type': type(error).__name__, 'message': str(error)}
        exit_code = 1
    print(json.dumps(response, ensure_ascii=True, allow_nan=False))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
