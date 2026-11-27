"""Monitor ZenTao once or continuously; no desktop session required."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
import time
from uuid import uuid4

from dingtalk_notify import drain as drain_notifications, enabled as notifications_enabled
from dispatch_monitor import dispatch_one, reserve_queue, schedule_workers
from ledger import validate_config
from monitor_intake import collect_scan, verify_candidate
from status_board import publish_status
from task_remote import RemoteRejected, ZenTaoError, get_remote
from workflow import CONFIG_PATH, DATABASE_PATH, PROJECT_ROOT, run


def read_materials(config, database, marker):
    scope = (marker, config['zentao']['base_url'].rstrip('/'), config['zentao']['project_name'])
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        row = conn.execute('''SELECT m.materials FROM intake_materials m JOIN work_items w ON w.seq=m.item_seq
                             JOIN scans s ON s.instance=w.instance AND s.project_id=w.project_id
                             WHERE w.marker=? AND s.instance=? AND s.project_name=?''', scope).fetchone()
    if not row:
        raise ValueError('No verified source materials for this item in this project')
    return json.loads(row[0])


def scan_once(config, *, dispatch):
    if not config['monitor']['enabled'] or not config['zentao']['assigned_to_accounts']:
        return {'status': 'disabled'}
    scan_id = 'scan-' + uuid4().hex
    directory = DATABASE_PATH.parent / 'monitor-runs' / scan_id
    directory.mkdir(parents=True)
    observed_at = datetime.now(timezone.utc).isoformat()
    proof = {'scan_id': scan_id}
    try:
        remote = get_remote(config)
        session = remote.session
        request, _ = collect_scan(config, session, scan_id=scan_id, observed_at=observed_at,
                                  expected_account=remote.account, proof=proof)
    except Exception as error:
        reason = str(error) if isinstance(error, RemoteRejected) else f'{type(error).__name__}: scope scan verification failed'
        proof.update(complete=False, error=reason)
        request = {'action': 'scan', 'scan_id': scan_id, 'observed_at': observed_at, 'complete': False,
                   'instance': config['zentao']['base_url'], 'project_id': proof.get('project_id'),
                   'project_name': config['zentao']['project_name'], 'items': []}
    if request.get('project_id'):
        proof['claim'] = run('claim', request, config, DATABASE_PATH)
    for filename, value in [('scan-proof.json', proof), ('scan-request.json', request)]:
        (directory / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    result = {'directory': str(directory), 'scan': proof}
    if dispatch and proof.get('complete') and proof['claim']['status'] == 'scanned':
        result['admissions'] = reserve_queue(config, DATABASE_PATH, request['project_id'], session)
    if not dispatch and DATABASE_PATH.is_file():
        publish_status(DATABASE_PATH, config)
    return result


def service_existing(config, *, schedule=True):
    from outbox import drain_writebacks

    if not DATABASE_PATH.exists():
        return {}
    jobs = []
    if schedule:
        jobs.append(('workers', lambda: schedule_workers(config, DATABASE_PATH)))
    jobs.append(('writebacks', lambda: drain_writebacks(config, DATABASE_PATH)))
    if notifications_enabled(config):
        jobs.append(('notifications', lambda: drain_notifications(DATABASE_PATH, config)))
    result = {}
    for name, fn in jobs:
        try:
            result[name] = fn()
        except MONITOR_ERRORS as error:
            result[name] = {'status': 'service_error', 'reason': type(error).__name__}
    publish_status(DATABASE_PATH, config)
    return result


MONITOR_ERRORS = (OSError, KeyError, TypeError, ValueError, RuntimeError, ZenTaoError, sqlite3.Error)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--once', action='store_true', help='Scan, reserve, launch workers and process writebacks')
    group.add_argument('--watch', action='store_true', help='Keep scanning and servicing work until interrupted')
    group.add_argument('--dispatch', nargs=2, metavar=('MARKER', 'REQUEST'), help=argparse.SUPPRESS)
    group.add_argument('--materials', metavar='MARKER', help='Read original materials saved at admission')
    group.add_argument('--recheck', nargs=2, metavar=('KIND', 'ID'))
    parser.add_argument('--project-id')
    args = parser.parse_args(argv)
    next_scan = 0
    next_queue = 0
    project_id = None
    previous = None
    while True:
        service = False
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
            validate_config(config)
            service = args.once or args.watch or args.dispatch is not None
            if args.dispatch:
                result = dispatch_one(config, DATABASE_PATH, *args.dispatch)
            elif args.materials:
                result = {'materials': read_materials(config, DATABASE_PATH, args.materials)}
            elif args.recheck:
                kind, identity = args.recheck
                if not args.project_id or kind not in ('story', 'bug') or not identity.isdecimal():
                    parser.error('--recheck requires a kind, numeric id and --project-id')
                item = {'instance': config['zentao']['base_url'], 'project_id': args.project_id,
                        'kind': kind, 'id': identity}
                result = {'detail': verify_candidate(config, get_remote(config).session, item)}
            else:
                result = {}
                if time.monotonic() >= next_scan:
                    project_id = None
                    result = scan_once(config, dispatch=args.once or args.watch)
                    proof = result.get('scan', {})
                    if proof.get('complete') and proof.get('claim', {}).get('status') in ('baseline', 'scanned'):
                        project_id = proof['project_id']
                    next_scan = time.monotonic() + 60 * config['monitor'].get('interval_minutes', 60)
                    next_queue = time.monotonic() + 30
                elif (config['monitor']['enabled'] and project_id and time.monotonic() >= next_queue):
                    next_queue = time.monotonic() + 30
                    # Only contact ZenTao between scans when an eligible item is waiting.
                    with closing(sqlite3.connect(DATABASE_PATH.as_uri() + '?mode=ro', uri=True)) as conn:
                        waiting = conn.execute("SELECT 1 FROM work_items WHERE state='queued' AND eligible=1 LIMIT 1").fetchone()
                    if waiting and config['zentao']['assigned_to_accounts']:
                        try:
                            result['admissions'] = reserve_queue(config, DATABASE_PATH, project_id, get_remote(config).session)
                        except (RemoteRejected, ZenTaoError, OSError):
                            result['admissions'] = [{'status': 'remote_unavailable'}]
            failed = not result.get('scan', {}).get('complete', True)
        except MONITOR_ERRORS as error:
            reason = 'ZenTao request failed' if isinstance(error, ZenTaoError) else str(error)
            result = {'status': 'monitor_error', 'reason': reason}
            failed = True
        if service:
            try:
                serviced = service_existing(config, schedule=not args.dispatch)
                if args.dispatch:
                    result['closeout'] = serviced
                else:
                    result.update(serviced)
                failed |= any(isinstance(value, dict) and value.get('status') == 'service_error'
                              for value in serviced.values())
            except MONITOR_ERRORS as error:
                result['maintenance_error'] = type(error).__name__
                failed = True
        # Suppress empty maintenance heartbeats in long-running service logs.
        if not args.watch or result != previous:
            print(json.dumps(result, ensure_ascii=True), flush=True)
        previous = result
        if not args.watch:
            return int(failed)
        time.sleep(10)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass
