"""Fixed host Git operations; the sandbox-facing workflow only requests/results."""

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from time import monotonic
from uuid import uuid4

from ledger import Ledger, item_identity, message, transaction, validate_config
from material_sync import git_until, requirement_checkout
from stage_runner import recover_job_stop, run_stage, utc_now, write_json
from workflow import (active_material_trial, compute_material_attempt, publish_material_result,
                      sync_material_attempt)


def load_config(path):
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    validate_config(config)
    return config


@contextmanager
def checkout_lock(directory, checkout):
    """Nonblocking lock shared by this project's sync and preflight operations."""
    directory.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(str(checkout).casefold().encode()).hexdigest() + '.lock'
    with (directory / name).open('a+b') as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def pending_material_sync(marker, database, config_path):
    if not database.is_file():
        return message('material_sync_not_pending')
    config = load_config(config_path)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        item = conn.execute('SELECT * FROM work_items WHERE marker=?', (marker,)).fetchone()
        if not item or item['state'] != 'running' or item['stage'] != 'material_check':
            return message('material_sync_not_pending')
        trial = conn.execute('SELECT * FROM attempts WHERE item_seq=? AND outcome IS NULL',
                             (item['seq'],)).fetchone()
        if not trial:
            return message('material_sync_not_pending')
        data = {'item': item_identity(item), 'request_id': item['request_id'],
                'chat_id': item['chat_id'], 'attempt_id': trial['attempt_id']}
    status = sync_material_attempt(database, config, data, None, config_path.parent)
    if status['status'] != 'material_sync_worker_required':
        return message('material_sync_not_pending')
    return message('material_sync_pending', data=data, deadline_at=status['deadline_at'])


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def recover_proof(folder, attempt, phase, deadline):
    receipt = read_json(folder / 'result.json')
    if receipt:
        if (receipt.get('attempt_id'), receipt.get('phase'), receipt.get('deadline_at')) != (
                attempt, phase, deadline.isoformat()):
            return None
        if (receipt.get('stop_confirmed') is True
                and receipt.get('stop_evidence', {}).get('owned_processes_active_after_stop') == 0):
            return receipt
        # A prior inability to verify cleanup is not permanent: re-query the
        # original named Job Object, without launching a second synchronization.
    launch = read_json(folder / 'launch.json')
    if not launch or (launch.get('attempt_id'), launch.get('phase'), launch.get('deadline_at')) != (
            attempt, phase, deadline.isoformat()):
        return None
    stopped = recover_job_stop(launch)
    if not stopped:
        return None
    # A completed computation may precede a supervisor crash. Publication still
    # rechecks the live binding and checkout after this independently proven stop.
    computed = read_json(folder / 'stdout.log')
    proof = {**launch, **(receipt or {}), 'stop_confirmed': True, 'stop_evidence': stopped,
             'outcome': receipt['outcome'] if receipt else ('success' if computed else 'failed'),
             'receipt_path': str(folder / 'result.json')}
    if receipt:
        proof['previous_stop_evidence'] = receipt.get('stop_evidence')
    write_json(folder / 'result.json', proof)
    return proof


def run_material_sync(marker, database, config_path, project_root, *, recover=False):
    if not database.is_file():
        return message('material_sync_not_pending')
    pending = pending_material_sync(marker, database, config_path)
    if pending['status'] != 'material_sync_pending':
        # A caller may be reconciling an already finalized attempt.
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            item = conn.execute('SELECT * FROM work_items WHERE marker=?', (marker,)).fetchone()
            trial = conn.execute('SELECT * FROM attempts WHERE item_seq=? AND outcome IS NULL',
                                 (item['seq'],)).fetchone() if item else None
        if item and trial:
            return sync_material_attempt(database, load_config(config_path), {
                'item': item_identity(item), 'request_id': item['request_id'], 'chat_id': item['chat_id'],
                'attempt_id': trial['attempt_id']}, None, config_path.parent)
        return pending
    data = pending['data']
    config = load_config(config_path)
    with transaction(database) as conn:
        _, _, checkout = active_material_trial(Ledger(conn, config, utc_now()), data, config_path.parent)
    if checkout is None:
        return message('material_sync_inactive')
    root = database.parent / 'items' / marker / 'material-sync'
    phase = 'host-sync'
    folder = root / data['attempt_id'] / phase
    deadline = datetime.fromisoformat(pending['deadline_at'])
    with checkout_lock(database.parent / 'requirement-locks', checkout) as acquired:
        if not acquired:
            return message('material_sync_busy', attempt_id=data['attempt_id'])
        current = pending_material_sync(marker, database, config_path)
        if current.get('data') != data:
            return message('material_sync_inactive')
        if folder.exists():
            proof = recover_proof(folder, data['attempt_id'], phase, deadline) if recover else None
            if not proof:
                return message('material_sync_stop_unverified', attempt_id=data['attempt_id'])
        else:
            root.mkdir(parents=True, exist_ok=True)
            request = root / (data['attempt_id'] + '-request.json')
            write_json(request, data)
            proof = run_stage(data['attempt_id'], phase, deadline, project_root, root, None,
                              [sys.executable, '-B', __file__, 'execute', '--config', str(config_path),
                               '--database', str(database), '--request', str(request)], recoverable=True)
        computed = read_json(folder / 'stdout.log') or {}
        # Reload current configuration after execution; revoked/changed paths
        # cannot be grandfathered by the snapshot captured before launch.
        return publish_material_result(database, load_config(config_path), data, computed, proof, config_path.parent)


def compute_git_preflight(config, config_directory):
    checkout = requirement_checkout(config, config_directory)
    if checkout is None:
        return message('git_preflight_failed', reason='A valid requirement checkout is required')
    try:
        git = git_until(checkout, monotonic() + 25)
        root = git('resolve the requirement checkout', 'rev-parse', '--show-toplevel')
        if Path(root).resolve() != checkout:
            raise ValueError('invalid checkout')
        branch = git('resolve the requirement branch', 'symbolic-ref', '--short', 'HEAD')
        remote = git('resolve the requirement remote', 'config', '--get', f'branch.{branch}.remote')
        ref = git('resolve the requirement tracking ref', 'config', '--get-all', f'branch.{branch}.merge')
        if not remote or remote == '.' or not ref.startswith('refs/heads/') or len(ref.splitlines()) != 1:
            raise ValueError('invalid upstream')
        result = git('read the upstream branch', 'ls-remote', '--exit-code', '--', remote, ref).splitlines()
        if len(result) != 1:
            raise ValueError('ambiguous upstream')
        sha, observed_ref = result[0].split()
        if observed_ref != ref or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha):
            raise ValueError('invalid upstream response')
        return message('git_preflight_passed', branch=branch, commit=sha)
    except (OSError, ValueError):
        return message('git_preflight_failed', reason='Requirement Git authentication or upstream verification failed')


def run_git_preflight(config_path, database, project_root, item=None):
    config = load_config(config_path)
    required = config.get('runtime', {}).get('sync_requirement_before_review')
    if item is not None:
        admitted = json.loads(item['config_snapshot'])
        required = required and item['kind'] == 'story' and admitted.get('runtime', {}).get(
            'sync_requirement_before_review')
        if required and requirement_checkout(admitted, config_path.parent) != requirement_checkout(config, config_path.parent):
            return message('git_preflight_failed', reason='The configured requirement checkout changed',
                           stop_confirmed=True, stop_evidence={'owned_processes_active_after_stop': 0})
    if not required:
        return message('git_preflight_skipped', stop_confirmed=True)
    checkout = requirement_checkout(config, config_path.parent)
    if checkout is None:
        return message('git_preflight_failed', reason='A valid requirement checkout is required', stop_confirmed=True,
                       stop_evidence={'owned_processes_active_after_stop': 0})
    with checkout_lock(database.parent / 'requirement-locks', checkout) as acquired:
        if not acquired:
            return message('git_preflight_busy', stop_confirmed=False)
        run_id = uuid4().hex
        root = database.parent / 'worker-probes'
        proof = run_stage(run_id, 'git-preflight', utc_now() + timedelta(seconds=30), project_root, root, None,
                          [sys.executable, '-B', __file__, 'preflight', '--config', str(config_path)], recoverable=True)
        checked = read_json(Path(proof['stdout_path'])) or {}
        stopped = (proof.get('stop_confirmed') is True
                   and proof.get('stop_evidence', {}).get('owned_processes_active_after_stop') == 0)
        passed = stopped and proof['outcome'] == 'success' and checked.get('status') == 'git_preflight_passed'
        return message('git_preflight_passed' if passed else 'git_preflight_failed',
                       **{k: checked[k] for k in ('branch', 'commit') if passed and k in checked},
                       receipt=proof['receipt_path'], stop_confirmed=stopped,
                       stop_evidence=proof.get('stop_evidence', {}))


def release_preflight_reservation(database, config, item, preflight):
    if (preflight.get('status') != 'git_preflight_failed' or preflight.get('stop_confirmed') is not True
            or preflight.get('stop_evidence', {}).get('owned_processes_active_after_stop') != 0):
        return message('preflight_reservation_retained')
    with transaction(database) as conn:
        current = conn.execute('SELECT * FROM work_items WHERE seq=?', (item['seq'],)).fetchone()
        lease = conn.execute('SELECT * FROM dispatches WHERE request_id=? AND item_seq=?',
                             (item['request_id'], item['seq'])).fetchone()
        if (not current or current['request_id'] != item['request_id'] or current['state'] not in ('reserved', 'bound')
                or not lease or lease['state'] != 'reserved' or conn.execute(
                    'SELECT 1 FROM attempts WHERE item_seq=? AND outcome IS NULL', (item['seq'],)).fetchone()):
            return message('preflight_reservation_retained')
        conn.execute("UPDATE dispatches SET state='preflight_blocked' WHERE request_id=?", (item['request_id'],))
        state = 'queued' if current['eligible'] else 'ineligible'
        conn.execute('UPDATE work_items SET state=? WHERE seq=?', (state, item['seq']))
        return message('preflight_reservation_released', item_state=state, chat_id=current['chat_id'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('execute', 'preflight'))
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--database', type=Path)
    parser.add_argument('--request', type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    if args.operation == 'preflight':
        result = compute_git_preflight(config, args.config.parent)
    else:
        result = compute_material_attempt(args.database, config, read_json(args.request), args.config.parent)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
