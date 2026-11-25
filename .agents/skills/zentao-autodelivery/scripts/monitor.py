"""Persist a scope-verified scan, or read back one candidate; never launch work."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from monitor_intake import collect_scan, verify_candidate
from status_board import publish_status
from task_remote import _credentials, get_remote
from workflow import CONFIG_PATH, DATABASE_PATH, PROJECT_ROOT, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recheck', nargs=2, metavar=('KIND', 'ID'))
    parser.add_argument('--project-id')
    args = parser.parse_args()
    config = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
    runtime = run('record', {'action': 'runtime_check', 'chat_id': os.environ['CODEX_THREAD_ID']},
                  config, DATABASE_PATH)
    if runtime.get('status') != 'runtime_unattended':
        print(json.dumps({'status': 'runtime_blocked', 'runtime': runtime}))
        return 1
    if not config['monitor']['enabled'] or not config['zentao']['assigned_to_accounts']:
        print(json.dumps({'status': 'disabled', 'runtime': runtime}))
        return 0
    if args.recheck:
        if args.recheck[0] not in ('story', 'bug') or not args.project_id:
            parser.error('--recheck requires story/bug, an item ID and --project-id')
        item = {'instance': config['zentao']['base_url'], 'project_id': args.project_id,
                'kind': args.recheck[0], 'id': args.recheck[1]}
        try:
            detail = verify_candidate(config, get_remote(config).session, item)
        except Exception as error:
            print(json.dumps({'runtime': runtime, 'item': item, 'status': 'detail_unverified',
                              'error': f'{type(error).__name__}: candidate verification failed'}))
            return 1
        print(json.dumps({'runtime': runtime, 'item': item, 'detail': detail}, ensure_ascii=False))
        return 0
    scan_id = 'automation-' + uuid4().hex
    directory = PROJECT_ROOT / '.local/zentao-delivery/monitor-runs' / scan_id
    directory.mkdir(parents=True)
    (directory.parent.parent / 'current-monitor-path.txt').write_text(str(directory), encoding='utf-8')
    proof = {'runtime': runtime, 'scan_id': scan_id}
    observed_at = datetime.now(timezone.utc).isoformat()
    try:
        session = get_remote(config).session
        account, password = _credentials(config['zentao'])
        del password
        request, _ = collect_scan(config, session, scan_id=scan_id, observed_at=observed_at,
                                  expected_account=account, proof=proof)
    except Exception as error:
        proof.update(complete=False, error=f'{type(error).__name__}: scope scan verification failed')
        request = {'action': 'scan', 'scan_id': scan_id, 'observed_at': observed_at, 'complete': False,
                   'instance': config['zentao']['base_url'], 'project_id': proof.get('project_id'),
                   'project_name': config['zentao']['project_name'], 'items': []}
    if request.get('project_id'):
        proof['claim'] = run('claim', request, config, DATABASE_PATH)
        publish_status(DATABASE_PATH, config)
    for filename, value in [('scan-proof.json', proof), ('scan-request.json', request)]:
        (directory / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'directory': str(directory), 'scan': proof}, ensure_ascii=False))
    return 0 if proof.get('complete') else 1


if __name__ == '__main__':
    raise SystemExit(main())
