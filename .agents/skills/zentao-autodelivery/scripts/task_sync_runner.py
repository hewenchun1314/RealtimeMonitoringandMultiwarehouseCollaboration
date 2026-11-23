"""Drain durable Task operations for one worker or one project monitor."""

import argparse
from contextlib import closing
import json
import os
import sqlite3
import sys
import time

from desktop_runtime import runtime_check
from status_board import publish_status
from task_sync import OPERATIONS, enabled, sync_once
from workflow import CONFIG_PATH, DATABASE_PATH


def can_sync_item(item, chat_id):
    return bool(item and enabled(item) and item['state'] in ('running', 'closing')
                and item['chat_id'] == chat_id
                and runtime_check(chat_id)['status'] == 'runtime_unattended')


def pending_task_markers(database, config):
    if not database.is_file():
        return []
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        scope = config['zentao']
        instance = scope['base_url'].rstrip('/')
        projects = conn.execute('SELECT project_id FROM scans WHERE instance=? AND project_name=?',
                                (instance, scope['project_name'])).fetchall()
        if len(projects) != 1:
            return []
        return [row[0] for row in conn.execute(f"""
            SELECT w.marker FROM work_items w
            WHERE w.instance=? AND w.project_id=? AND w.kind='story'
              AND json_extract(w.config_snapshot,'$.runtime.split_task_sync')=1
              AND json_extract(w.config_snapshot,'$.workflow_version')=2
              AND EXISTS (SELECT 1 FROM writebacks b WHERE b.item_seq=w.seq
                AND b.operation_type IN {OPERATIONS}
                AND b.state NOT IN ('confirmed','superseded'))
            ORDER BY w.seq""", (instance, projects[0][0]))]


def sync_and_publish(marker, config):
    changed = sync_once(DATABASE_PATH, marker=marker)
    if changed:
        try:
            publish_status(DATABASE_PATH, config)
        except (OSError, ValueError, sqlite3.Error) as error:
            print(f'Status page refresh failed: {error}', file=sys.stderr)
    return changed


def monitor_batch(config, limit=32):
    chat_id = os.environ.get('CODEX_THREAD_ID', '')
    if runtime_check(chat_id)['status'] != 'runtime_unattended':
        raise ValueError('Task synchronization requires an unattended monitor session')
    markers = pending_task_markers(DATABASE_PATH, config)
    processed = 0
    while markers and processed < limit:
        active = []
        for marker in markers:
            if runtime_check(chat_id)['status'] != 'runtime_unattended':
                raise ValueError('Monitor session permission changed')
            if sync_and_publish(marker, config):
                processed += 1
                active.append(marker)
                if processed == limit:
                    break
        markers = active
    return {'status': 'tasks_checked', 'processed': processed}


def watch_item(config, marker, chat_id):
    # The parent Job Object owns this process and cuts off slow network calls on exit.
    with closing(sqlite3.connect(DATABASE_PATH.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        while True:
            item = conn.execute('SELECT * FROM work_items WHERE marker=?', (marker,)).fetchone()
            if not item or item['chat_id'] != chat_id or item['state'] not in ('bound', 'running', 'closing'):
                return {'status': 'worker_released'}
            pending = conn.execute(f"""SELECT 1 FROM writebacks WHERE item_seq=?
                AND state NOT IN ('confirmed','superseded') AND operation_type IN {OPERATIONS} LIMIT 1""",
                                   (item['seq'],)).fetchone()
            if pending and can_sync_item(item, chat_id) and sync_and_publish(marker, config):
                continue
            time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--marker')
    parser.add_argument('--chat-id')
    args = parser.parse_args()
    if bool(args.marker) != bool(args.chat_id):
        parser.error('--marker and --chat-id must be supplied together')
    config = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
    result = watch_item(config, args.marker, args.chat_id) if args.marker else monitor_batch(config)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({'status': 'task_sync_failed', 'reason': str(error)}), file=sys.stderr)
        sys.exit(1)
