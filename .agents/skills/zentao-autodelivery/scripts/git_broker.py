"""Host-side dispatch for fixed Git requests from one live, bound work-item CLI.

The notification carries no command, path or credential. All effects are derived
again from the admitted operation and current configuration by the host runners.
"""

from contextlib import closing
from datetime import datetime, timezone
import json
import os
import sqlite3
import time


TABLE = '''CREATE TABLE IF NOT EXISTS git_broker_calls (
    turn TEXT NOT NULL, operation TEXT NOT NULL, kind TEXT NOT NULL, response TEXT,
    PRIMARY KEY(turn,operation))'''


def connect(database):
    return sqlite3.connect(database, timeout=10)


def prepare(database):
    with closing(connect(database)) as conn, conn:
        conn.execute(TABLE)


def wait_for_receipt(response, data, database, config, config_directory):
    status = response.get('status')
    turn = os.getenv('ZENTAO_GIT_BROKER_TURN')
    if not turn or status not in ('host_git_worker_required', 'material_sync_worker_required'):
        return response
    material = status == 'material_sync_worker_required'
    operation = response['attempt_id'] if material else response['operation_id']
    with closing(connect(database)) as conn, conn:
        if material:
            deadline = response['deadline_at']
        else:
            row = conn.execute('SELECT request FROM development_git_operations WHERE operation_id=?',
                               (operation,)).fetchone()
            deadline = json.loads(row[0])['context']['deadline_at']
        conn.execute('''INSERT INTO git_broker_calls(turn,operation,kind) VALUES(?,?,?)
                        ON CONFLICT(turn,operation) DO UPDATE SET response=NULL''',
                     (turn, operation, 'material' if material else 'development'))
    deadline = datetime.fromisoformat(deadline)
    while datetime.now(timezone.utc) < deadline:
        with closing(connect(database)) as conn:
            row = conn.execute('SELECT response FROM git_broker_calls WHERE turn=? AND operation=?',
                               (turn, operation)).fetchone()
        if row and row[0]:
            # The request notification does not certify success. Read the actual
            # receipt from the ledger, checking the current attempt and binding.
            if material:
                from workflow import sync_material_attempt
                actual = sync_material_attempt(database, config, data, None, config_directory)
            else:
                from development_git import host_git_status
                actual = host_git_status(database, config, {**data, 'operation_id': operation}, None, config_directory)
            if actual['status'] != status:
                return actual
            return {'status': 'git_request_unconfirmed', 'operation': operation, 'host_status': row[0]}
        time.sleep(0.25)
    return {'status': 'git_request_unconfirmed', 'operation': operation, 'reason': 'attempt deadline elapsed'}


def service(turn, item, database, config_path, project_root, stop):
    from desktop_runtime import runtime_check
    from development_git_runner import pending_development_git, run_development_git
    from material_sync_runner import pending_material_sync, run_material_sync

    while not stop.wait(0.25):
        with closing(connect(database)) as conn:
            call = conn.execute('''SELECT kind,operation FROM git_broker_calls
                                  WHERE turn=? AND response IS NULL ORDER BY rowid LIMIT 1''', (turn,)).fetchone()
        if call is None:
            continue
        kind, operation = call
        result = {'status': 'git_request_inactive'}
        if runtime_check(item['chat_id'])['status'] == 'runtime_unattended':
            marker = item['marker']
            if kind == 'material':
                pending = pending_material_sync(marker, database, config_path)
                if pending.get('data', {}).get('attempt_id') == operation:
                    result = run_material_sync(marker, database, config_path, project_root, recover=True)
            elif kind == 'development':
                pending = pending_development_git(marker, database, config_path)
                if pending.get('operation_id') == operation:
                    result = run_development_git(marker, database, config_path, project_root, recover=True)
        else:
            result = {'status': 'runtime_blocked'}
        with closing(connect(database)) as conn, conn:
            conn.execute('UPDATE git_broker_calls SET response=? WHERE turn=? AND operation=?',
                         (result['status'], turn, operation))
