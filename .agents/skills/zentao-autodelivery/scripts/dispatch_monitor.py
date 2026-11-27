"""Reserve one freshly rechecked item and supervise each bound CLI under its own host lock."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

from claim import candidate
from ledger import item_identity, stamp, transaction
from material_sync_runner import run_git_preflight
from portable_lock import file_lock
from monitor_intake import verify_candidate
from task_remote import ZenTaoError
from workflow import CONFIG_PATH, PROJECT_ROOT, run


def reserve_queue(config, database, project_id, session):
    """Keep the ledger transaction short. ZenTao and Git preflight run outside the lock."""
    results = []
    e2e = config.get('e2e', {})
    browser_checked = False
    for _ in range(64):
        with transaction(database) as conn:
            row = candidate(conn, config['zentao']['base_url'].rstrip('/'), project_id)
            occupied = conn.execute("""SELECT count(*) FROM work_items
                WHERE state IN ('reserved','bound','running','closing')""").fetchone()[0]
        if row is None or occupied >= config['development']['max_concurrent_items']:
            break
        item = item_identity(row)
        try:
            detail = verify_candidate(config, session, item)
            if (detail['eligible'] and item['kind'] == 'story'
                    and config.get('runtime', {}).get('sync_requirement_before_review')):
                check = run_git_preflight(CONFIG_PATH, database, PROJECT_ROOT)
                if check['status'] not in ('git_preflight_passed', 'git_preflight_skipped'):
                    defer_candidate(database, row, '等待需求仓库认证或上游核验')
                    results.append(check)
                    continue
                detail = verify_candidate(config, session, item)
        except (OSError, ValueError, KeyError, TypeError, ZenTaoError) as error:
            defer_candidate(database, row, '暂时无法复核原单，下轮重新读取')
            results.append({'status': 'detail_unverified', 'item': item, 'error': type(error).__name__})
            continue
        if (detail['eligible'] and e2e.get('enabled', True) and e2e.get('agentlimb', {}).get('intake_gate')
                and config['development'].get('allowed_requirement_types') != ['backend_only'] and not browser_checked):
            from worker import run_e2e_preflight
            browser = run_e2e_preflight(None)
            if browser['status'] != 'e2e_preflight_passed':
                results.append(browser)
                break
            browser_checked = True
            # The browser diagnostic may outlive a fresh observation.
            continue
        request = {'action': 'reserve', 'item': item, 'observed_at': stamp(datetime.now(timezone.utc)),
                   'assigned_to': detail['assigned_to'], 'status': detail['status'],
                   'source': detail['source'], 'eligible': detail['eligible']}
        if 'materials' in detail:
            request['materials'] = detail['materials']
        result = run('claim', request, config, database)
        results.append(result)
        if result['status'] not in ('reserved', 'not_eligible', 'queue_changed'):
            break
    return results


def defer_candidate(database, row, reason):
    retry_at = stamp(datetime.now(timezone.utc) + timedelta(minutes=1))
    with transaction(database) as conn:
        conn.execute("""INSERT OR REPLACE INTO intake_retries(item_seq,retry_at,reason)
                        SELECT seq,?,? FROM work_items WHERE seq=? AND state='queued' AND observed_at=?""",
                     (retry_at, reason, row['seq'], row['observed_at']))


def active_items(config, database):
    if not database.is_file():
        return []
    scope = (config['zentao']['base_url'].rstrip('/'), config['zentao']['project_name'])
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("""SELECT w.*,d.expires_at FROM work_items w
                    JOIN dispatches d ON d.request_id=w.request_id
                    JOIN scans s ON s.instance=w.instance AND s.project_id=w.project_id
                    WHERE s.instance=? AND s.project_name=?
                    AND w.state IN ('reserved','bound','running','closing','dispatch_uncertain')
                    ORDER BY w.seq""", scope)]


def prepare_receipt(directory, key):
    from worker import read_prepared_turn

    result = read_prepared_turn(directory / 'worker' / key.replace('-', '') / 'prepare')
    return result['chat_id'] if result['status'] == 'worker_ready' else None


def dispatch_one(config, database, marker, request_id):
    from worker import resolve_cli, run as run_worker

    if not re.fullmatch(r'HNF-[0-9a-f]{20}', marker):
        raise ValueError('Invalid work item marker')
    directory = database.parent / 'items' / marker
    with file_lock(directory / 'monitor.lock') as acquired:
        if not acquired:
            return {'status': 'worker_busy'}
        item = next((r for r in active_items(config, database) if r['marker'] == marker and
                     r['request_id'] == request_id), None)
        if not item:
            return {'status': 'stale_dispatch'}

        def record(action, **values):
            return run('record', {'action': action, 'item': item_identity(item), 'request_id': request_id,
                                  'event_id': uuid4().hex, **values}, config, database)

        now = stamp(datetime.now(timezone.utc))
        state = item['state']
        if state in ('reserved', 'bound') and item['expires_at'] <= now:
            with file_lock(directory / 'worker' / 'worker.lock') as stopped:
                if not stopped:
                    return {'status': 'worker_busy'}
                expired = record('expire_dispatch')
                if expired['status'] != 'dispatch_uncertain':
                    return expired
                # prepare creates its evidence directory before the CLI can start.
                if not (directory / 'worker' / request_id.replace('-', '') / 'prepare').exists():
                    return record('resolve_dispatch', not_created=True,
                                  evidence='Exclusive worker lock; original prepare was never launched')
                state = 'dispatch_uncertain'
        if state == 'dispatch_uncertain':
            chat = prepare_receipt(directory, request_id)
            if chat:
                return record('bind', chat_id=chat, desktop_project_id=item['desktop_project_id'])
            return {'status': 'dispatch_uncertain', 'reason': 'Check the original prepare receipt'}
        cli = resolve_cli()
        if state == 'reserved':
            prepared = run_worker('prepare', marker, cli)
            if prepared['status'] != 'worker_ready':
                if prepared['status'] == 'worker_prepare_uncertain':
                    record('defer_dispatch', reason='prepare_uncertain')
                return prepared
            bound = record('bind', chat_id=prepared['chat_id'], desktop_project_id=item['desktop_project_id'])
            if bound['status'] != 'bound':
                return bound
        return run_worker('run', marker, cli)


def schedule_workers(config, database):
    launched = []
    for item in active_items(config, database):
        directory = database.parent / 'items' / item['marker']
        with file_lock(directory / 'monitor.lock') as idle:
            if not idle:
                continue
            if item['state'] == 'dispatch_uncertain' and not prepare_receipt(directory, item['request_id']):
                continue
            log = directory / 'dispatch.log'
            # A short cooldown avoids repeated launches on a busy or unavailable host.
            if log.exists() and time.time() - log.stat().st_mtime < 30:
                continue
            argv = [sys.executable, '-B', str(Path(__file__).with_name('monitor.py')),
                    '--dispatch', item['marker'], item['request_id']]
            kwargs = {'start_new_session': True} if os.name != 'nt' else {
                'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
            with log.open('ab') as output:
                output.write((stamp(datetime.now(timezone.utc)) + ' dispatch\n').encode())
                output.flush()
        # The child checks its request and reacquires the per-item lock before any operation.
        with log.open('ab') as output:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=output, cwd=PROJECT_ROOT, **kwargs)
        launched.append(item['marker'])
    return launched
