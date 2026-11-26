"""Windows launcher for ledger-authorized frontend/backend Git batches."""
import argparse
from contextlib import ExitStack, closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

from development_git import validate_operation, operation_history, apply_obligations
from development_git_effects import DIAGNOSTICS, failed_hooks, failed_hook_args, common_directory, git, integrate, prepare, commit, tracked_dirty, validate_worktree
from ledger import Ledger, encode, message, transaction
from material_sync_runner import checkout_lock, load_config, read_json, recover_proof
from stage_runner import run_stage, utc_now, write_json


def pending_development_git(marker, database, config_path):
    if not database.is_file():
        return message('host_git_not_pending')
    config = load_config(config_path)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='development_git_operations'").fetchone():
            return message('host_git_not_pending')
        store = Ledger(conn, config, utc_now())
        rows = conn.execute('SELECT g.* FROM development_git_operations g JOIN work_items w ON w.seq=g.item_seq WHERE w.marker=? AND g.result IS NULL ORDER BY g.created_at,g.rowid', (marker,)).fetchall()
        for row in rows:
            try:
                request = validate_operation(store, row, config_path.parent)
                return message('host_git_pending', operation_id=row['operation_id'], request=request)
            except (KeyError, ValueError, OSError):
                continue
    return message('host_git_not_pending')


def verify_result(context, key, result):
    if result.get('status') != 'verified':
        return
    deadline = context['deadline_at']
    proof = context['repositories'][key]
    if context['kind'] == 'prepare':
        root, head = validate_worktree(result, deadline)
        if head != result['base_commit'] or tracked_dirty(root, deadline):
            raise ValueError('Prepared worktree changed before host publication')
    elif context['kind'] == 'commit':
        root, head = validate_worktree(proof, deadline)
        if head != result['candidate'] or tracked_dirty(root, deadline):
            raise ValueError('Candidate changed or became dirty before host publication')
    else:
        source = Path(proof['source_root'])
        actual = git(source, 'rev-parse', 'refs/heads/' + proof['target_branch'], deadline=deadline)
        if actual != result['merged'] or git(source, 'merge-base', '--is-ancestor', proof['candidate'], actual,
                                            deadline=deadline, allowed=(0, 1)) is None:
            raise ValueError('Integration result changed before host publication')


def compute(database, config_path, operation, folder):
    config = load_config(config_path)
    with transaction(database) as conn:
        row = conn.execute('SELECT * FROM development_git_operations WHERE operation_id=?', (operation,)).fetchone()
        request = validate_operation(Ledger(conn, config, utc_now()), row, config_path.parent)
        apply_obligations(request, operation_history(conn, database, request['context'], exclude=operation))
    context = request['context']
    results = {}
    for key, proof in context['repositories'].items():
        journal = folder / 'effects' / key
        if journal.resolve() != journal or not journal.is_relative_to(folder / 'effects'):
            raise ValueError('The effect journal must remain inside the stable operation evidence root')
        journal.mkdir(parents=True, exist_ok=True)
        saved = read_json(journal / 'result.json')
        token = DIAGNOSTICS.set(journal / 'git-diagnostics')
        try:
            if saved:
                verify_result(context, key, saved)
                results[key] = saved
            elif context['kind'] == 'prepare':
                result = prepare(proof, context, key, database, journal, request['payload'].get(key))
                results[key] = result
            elif context['kind'] == 'commit':
                result = commit(proof, request['payload'][key], context, operation, key, journal)
                results[key] = result
            else:
                result = integrate(proof, context, operation, key, journal, database, request['payload'])
                results[key] = result
        except (OSError, ValueError, KeyError) as error:
            results[key] = {'status': 'failed', 'reason': str(error), 'diagnostics': str(journal / 'git-diagnostics')}
            if context['kind'] == 'prepare' and request['payload'].get(key, {}).get('failed_hooks'):
                results[key].update({field: request['payload'][key][field] for field in ('failed_hooks', 'failed_hook_args')})
            if context['kind'] == 'commit' and request['payload'][key].get('failed_hooks'):
                results[key].update(status='candidate_retest_required', candidate=request['payload'][key]['expected_head'],
                                    failed_hooks=request['payload'][key]['failed_hooks'])
                results[key]['failed_hook_args'] = request['payload'][key].get('failed_hook_args', {})
            elif context['kind'] == 'integrate' and key in request['payload'].get('failed_hooks', {}):
                results[key].update(status='integration_retest_required' if key in request['payload'].get('needs_retest', []) else 'failed', candidate=proof['candidate'],
                                    failed_hooks=request['payload']['failed_hooks'][key])
                results[key]['failed_hook_args'] = request['payload'].get('failed_hook_args', {}).get(key, {})
        finally:
            DIAGNOSTICS.reset(token)
        if failed_hooks(journal):
            results[key].update(failed_hooks=failed_hooks(journal), failed_hook_args=failed_hook_args(journal))
        write_json(journal / 'result.json', results[key])
        write_json(folder / 'computed.json', {'repositories': results})
    return {'repositories': results}


def lock_path(database, common):
    digest = hashlib.sha256(str(common).casefold().encode()).hexdigest()
    return database.parent / 'development-git-locks' / (digest + '-pending.json')


def reconcile(database, common):
    path = lock_path(database, common)
    if not path.exists():
        return True
    pending = read_json(path)
    try:
        if (not pending or pending.get('common') != str(common)
                or not re.fullmatch(r'[0-9a-f]{64}', pending.get('operation_id', ''))
                or not re.fullmatch(r'HNF-[0-9a-f]{20}', pending.get('marker', ''))):
            return False
        folder = database.parent / 'items' / pending['marker'] / 'development-git' / pending['operation_id'] / 'host-git'
        phase = pending.get('phase', 'host-git')
        if not re.fullmatch(r'host-git(?:-resume-[1-9][0-9]*)?', phase):
            return False
        folder = folder.parent / phase
        if folder.exists() and not recover_proof(folder, pending['operation_id'], phase, datetime.fromisoformat(pending['deadline_at'])):
            return False
        path.unlink()
        return True
    except (KeyError, TypeError, ValueError):
        return False


def run_development_git(marker, database, config_path, project_root, *, recover=False):
    pending = pending_development_git(marker, database, config_path)
    if pending['status'] != 'host_git_pending':
        return pending
    operation = pending['operation_id']
    request = pending['request']
    context = request['context']
    deadline = datetime.fromisoformat(context['deadline_at'])
    root = database.parent / 'items' / marker / 'development-git'
    folder = root / operation / 'host-git'
    canonical = sorted({common_directory(Path(p['source_root']), context['deadline_at']) for p in context['repositories'].values()})
    with ExitStack() as stack:
        for common in canonical:
            if not stack.enter_context(checkout_lock(database.parent / 'development-git-locks', common)) or not reconcile(database, common):
                return message('host_git_busy', operation_id=operation)
        with transaction(database) as conn:
            row = conn.execute('SELECT * FROM development_git_operations WHERE operation_id=?', (operation,)).fetchone()
            request = validate_operation(Ledger(conn, load_config(config_path), utc_now()), row, config_path.parent)
        control_path = root / operation / 'runner.json'
        control = read_json(control_path) or {'phase': 'host-git', 'resumes': 0}
        phase = control.get('phase', '')
        if not re.fullmatch(r'host-git(?:-resume-[1-9][0-9]*)?', phase):
            return message('host_git_stop_unverified', operation_id=operation)
        phase_folder = root / operation / phase
        def launch():
            control_path.parent.mkdir(parents=True, exist_ok=True)
            for common in canonical:
                write_json(lock_path(database, common), {'common': str(common), 'operation_id': operation,
                           'marker': marker, 'deadline_at': deadline.isoformat(), 'phase': phase})
            write_json(control_path, control)
            return run_stage(operation, phase, deadline, project_root, root, None,
                              [sys.executable, '-B', __file__, '--config', str(config_path), '--database', str(database),
                               '--operation', operation, '--folder', str(folder)], recoverable=True)
        def stopped(proof):
            return (proof and (proof.get('attempt_id'), proof.get('phase'), proof.get('deadline_at')) ==
                    (operation, phase, deadline.isoformat()) and proof.get('stop_confirmed') is True
                    and proof.get('stop_evidence', {}).get('owned_processes_active_after_stop') == 0)
        proof = (recover_proof(phase_folder, operation, phase, deadline) if recover else None) if phase_folder.exists() else launch()
        if not stopped(proof):
            return message('host_git_stop_unverified', operation_id=operation)
        computed = read_json(folder / 'computed.json') or {'repositories': {}}
        if set(computed.get('repositories', {})) != set(context['repositories']):
            # Resume only a stopped process, retaining its effect journal. Each
            # repo reuses its result or reconciles a stable commit/merge marker;
            # no completed effect is blindly replayed.
            control = {'resumes': control['resumes'] + 1, 'phase': 'host-git-resume-' + str(control['resumes'] + 1)}
            phase = control['phase']
            proof = launch()
            if not stopped(proof):
                return message('host_git_stop_unverified', operation_id=operation)
            computed = read_json(folder / 'computed.json') or {'repositories': {}}
            if set(computed.get('repositories', {})) != set(context['repositories']):
                return message('host_git_recovery_required', operation_id=operation)
        for common in canonical:
            path = lock_path(database, common)
            old = read_json(path)
            if old and old.get('operation_id') == operation:
                path.unlink()
        with transaction(database) as conn:
            row = conn.execute('SELECT * FROM development_git_operations WHERE operation_id=?', (operation,)).fetchone()
            try:
                current = validate_operation(Ledger(conn, load_config(config_path), utc_now()), row, config_path.parent)
            except (ValueError, OSError, KeyError):
                return message('host_git_inactive', operation_id=operation)
            results = computed['repositories']
            for key in current['context']['repositories']:
                try:
                    if key not in results:
                        raise ValueError('The host operation did not produce a result before stopping')
                    verify_result(current['context'], key, results[key])
                except (OSError, ValueError, KeyError) as error:
                    results[key] = {'status': 'failed', 'reason': str(error)}
            statuses = {x.get('status') for x in results.values()}
            if statuses == {'verified'}:
                status = 'host_git_completed'
            elif statuses <= {'verified', 'integration_candidate_ready'}:
                status = 'host_git_retest_required'
            else:
                status = 'host_git_failed'
            result = message(status, operation_id=operation,
                             repositories=results, runner_receipt=proof['receipt_path'])
            if context['kind'] == 'commit':
                result['versions'] = {k: v['candidate'] for k, v in results.items() if v.get('status') == 'verified'}
            elif context['kind'] == 'integrate':
                result['versions'] = {k: p['candidate'] for k, p in context['repositories'].items()}
                result['merged_versions'] = {k: v['merged'] for k, v in results.items() if v.get('merged')}
            conn.execute('UPDATE development_git_operations SET result=? WHERE operation_id=?', (encode(result), operation))
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--operation', required=True)
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(compute(args.database, args.config, args.operation, args.folder), ensure_ascii=True))


if __name__ == '__main__':
    main()
