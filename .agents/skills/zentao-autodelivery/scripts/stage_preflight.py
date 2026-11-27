"""Prepare a ledger-bound stage with the project Codex permission profile."""

from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tomllib
from uuid import uuid4

from backend_dependencies import codex_directory, worker_permission_options, worker_roots
from ledger import Ledger, stamp
from route_execution import snapshot, stage_evidence


def evidence_directory(database, item):
    return database.parent / 'items' / item['marker'] / f'round-{item["run_round"]}' / 'stage-runs'


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None


def request_path(database, item, trial):
    return evidence_directory(database, item) / trial['attempt_id'] / f'{trial["phase"]}-host-request.json'


def receipt_path(database, item, trial):
    return evidence_directory(database, item) / trial['attempt_id'] / trial['phase'] / 'result.json'


def blocked_preflight(database, item, trial):
    path = receipt_path(database, item, trial)
    try:
        receipt = read_json(path) or {}
    except (OSError, ValueError):
        return None
    failure = receipt.get('preflight_failure')
    if (receipt.get('attempt_id') == trial['attempt_id'] and receipt.get('phase') == trial['phase']
            and receipt.get('stop_confirmed') is True
            and receipt.get('stop_evidence', {}).get('worker_started') is False
            and isinstance(failure, dict) and failure.get('check') in ('sandbox', 'frontend_dependencies')):
        return {**failure, 'receipt': str(path)}
    return None


def request_stage(database, attempt_id, phase, deadline, chat_id, cwd, prompt):
    if not chat_id or not database.is_file():
        return None
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
        conn.row_factory = sqlite3.Row
        trial = conn.execute('SELECT * FROM attempts WHERE attempt_id=? AND outcome IS NULL',
                             (attempt_id,)).fetchone()
        if not trial:
            return None
        item = conn.execute('SELECT * FROM work_items WHERE seq=?', (trial['item_seq'],)).fetchone()
        if (item['chat_id'] != chat_id or item['state'] != 'running' or item['stage'] != trial['stage']
                or item['run_round'] != trial['run_round'] or trial['phase'] != phase
                or trial['deadline_at'] != stamp(deadline)):
            raise ValueError('Codex stage must use the active work item, phase and deadline')
    path = receipt_path(database, item, trial)
    receipt = read_json(path)
    if receipt is not None:
        return receipt
    request = request_path(database, item, trial)
    if not request.is_file():
        payload = {'attempt_id': attempt_id, 'phase': phase, 'chat_id': chat_id, 'cwd': str(cwd),
                   'prompt': prompt.read_text(encoding='utf-8-sig') if prompt else None}
        request.parent.mkdir(parents=True, exist_ok=True)
        temp = request.with_name(f'{request.name}.{uuid4().hex}.pending')
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            # Windows rename does not overwrite an existing request.
            os.rename(temp, request)
        except FileExistsError:
            pass
        finally:
            temp.unlink(missing_ok=True)
    return {'status': 'host_stage_required', 'attempt_id': attempt_id, 'phase': phase,
            'receipt_path': str(path)}


def pending_stage(database, marker):
    if not database.is_file():
        return None
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
        conn.row_factory = sqlite3.Row
        trial = conn.execute('''SELECT a.*, w.marker FROM attempts a JOIN work_items w ON w.seq=a.item_seq
                             WHERE w.marker=? AND w.state='running' AND a.run_round=w.run_round
                             AND a.stage=w.stage AND a.outcome IS NULL''', (marker,)).fetchone()
    if trial is None:
        return None
    result = read_json(receipt_path(database, trial, trial))
    if result is not None and result.get('stop_confirmed') is True:
        return None
    path = request_path(database, trial, trial)
    return (trial, path) if path.is_file() else None


def frontend_worktrees(store, database, item, stage):
    branch_stage = 'branch_check' if stage in ('coding', 'repair_coding') else 'frontend.branch_check'
    repositories = (stage_evidence(store, item, branch_stage) or {}).get('repositories', {})
    if not repositories:
        raise ValueError('Frontend dependency preparation requires successful branch evidence')
    worktrees_root = database.parent / 'worktrees'
    paths = []
    for key, proof in repositories.items():
        branch = str(proof.get('worktree_branch', ''))
        prefix = re.escape(f'zentao/{item["marker"].lower()}-')
        if not re.fullmatch(prefix + r'[1-9][0-9]*(?:-frontend)?', branch):
            raise ValueError(f'Invalid frontend worktree branch for {key}')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', key):
            raise ValueError('Invalid frontend repository name')
        expected = worktrees_root / branch.rsplit('/', 1)[-1] / key
        if Path(proof['worktree']) != expected or expected.resolve(strict=True) != expected:
            raise ValueError(f'Frontend worktree does not match its registration for {key}')
        paths.append(expected)
    return paths


def managed_stage(database, config_path, project_root, item, trial, cwd):
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
        conn.row_factory = sqlite3.Row
        stage = trial['stage'] if trial['phase'] == 'verify' else trial['phase']
        config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        store = Ledger(conn, config, datetime.now(timezone.utc))
        error = None
        worktrees = []
        if stage in ('frontend.coding', 'coding', 'repair_coding'):
            try:
                worktrees = frontend_worktrees(store, database, item, stage)
            except (OSError, ValueError, KeyError) as exc:
                error = {'check': 'frontend_dependencies', 'reason': str(exc)}
        output = evidence_directory(database, item)
        npm_cache = database.parent / 'npm-installed-cache'
        try:
            protected, writable = worker_roots([config, snapshot(item)], config_path.parent,
                                               project_root, codex_directory())
            if not protected and not writable:
                writable = [project_root.resolve(), codex_directory().resolve()]
            if not any(cwd.is_relative_to(root) for root in (*writable, project_root)):
                raise ValueError('Stage working directory is outside the configured project workspaces')
            for parent in (cwd, *cwd.parents):
                local_config = parent / '.codex' / 'config.toml'
                if local_config.is_file():
                    with local_config.open('rb') as stream:
                        if 'sandbox_mode' in tomllib.load(stream):
                            raise ValueError(f'Legacy sandbox_mode would override the stage profile in {local_config}')
            writable = sorted(set(writable) | {output.resolve()} | set(worktrees))
            options = worker_permission_options(protected, writable)
            agent_options = worker_permission_options([*protected, npm_cache], writable) if worktrees else options
        except (OSError, ValueError) as exc:
            return output, [], [], {'check': 'sandbox', 'reason': str(exc)}

    cli = str(codex_directory() / 'packages' / 'app-server-daemon' / 'current' / 'bin' / 'codex.exe')
    command = [cli, '--ask-for-approval', 'never', 'exec', *agent_options,
               '--ignore-user-config', '--ephemeral', '--json', '--color', 'never', '--skip-git-repo-check', '-']
    sandbox = [cli, 'sandbox', '--include-managed-config', '--permission-profile', 'zentao_delivery', *options, '--']
    probe = {'check': 'sandbox', 'cwd': str(cwd),
             'timeout': 20, 'expected_stdout': 'ZENTAO_STAGE_SANDBOX_OK',
             'command': [*sandbox, 'cmd.exe', '/d', '/c', 'echo ZENTAO_STAGE_SANDBOX_OK']}
    checks = [probe]
    for worktree in worktrees:
        checks.append({'check': 'frontend_dependencies', 'cwd': str(worktree),
                       'command': [*sandbox, sys.executable, '-B',
                                   str(Path(__file__).with_name('stage_npm.py')), str(npm_cache)]})
    return output, command, checks, error


def run_pending_stage(database, config_path, project_root, item, pending):
    from stage_runner import run_stage

    trial, path = pending
    receipt = receipt_path(database, item, trial)
    current = pending_stage(database, item['marker'])
    if current is None or current[1] != path:
        return read_json(receipt) or {'status': 'host_stage_request_stale', 'receipt_path': str(receipt)}
    if receipt.parent.exists():
        return read_json(receipt) or {'status': 'host_stage_stop_unverified', 'receipt_path': str(receipt)}
    request = read_json(path)
    if (request is None or request.get('attempt_id') != trial['attempt_id'] or request.get('phase') != trial['phase']
            or request.get('chat_id') != item['chat_id']):
        return {'status': 'host_stage_request_invalid', 'receipt_path': str(receipt)}
    deadline = datetime.fromisoformat(trial['deadline_at'])
    try:
        cwd = Path(request['cwd']).resolve(strict=True)
        output, command, checks, error = managed_stage(database, config_path, project_root, item, trial, cwd)
    except (OSError, TypeError, KeyError, ValueError) as exc:
        cwd = project_root
        output, command, checks = evidence_directory(database, item), [], []
        error = {'check': 'sandbox', 'reason': str(exc)}
    prompt = None
    if not error and request.get('prompt') is not None:
        prompt = path.with_suffix('.prompt.txt')
        prompt.write_text(request['prompt'], encoding='utf-8')
    return run_stage(trial['attempt_id'], trial['phase'], deadline, cwd, output, prompt, command,
                     preflight=checks, preflight_error=error, recoverable=True, chat_id=item['chat_id'])
