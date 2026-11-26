"""Coordinate a fixed Windows-host AgentLimb run for a post-merge attempt."""

from contextlib import closing
from datetime import datetime
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import sys
from tempfile import NamedTemporaryFile
from types import SimpleNamespace
from urllib.parse import urlsplit

from ledger import Ledger, message
from material_sync_runner import checkout_lock, read_json, recover_proof
from merged_e2e_sources import remove_committed_sources
from route_execution import get_split
from stage_runner import run_stage, utc_now


HOST_PHASE = 'e2e-host'


def owns_slot(store, item, config_directory):
    from frontend_delivery import owns_slot as check

    return check(store, item, config_directory)


def frontend_e2e_lock(database):
    return checkout_lock(database.parent / 'frontend-e2e-locks', database.resolve())


def frontend_delivery_in_use(database):
    """Defer diagnostics while a formal run may still own local ports."""
    from frontend_delivery import SCOPE

    if not database.is_file():
        return False
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        return conn.execute('SELECT 1 FROM deployment_locks WHERE scope=? LIMIT 1', (SCOPE,)).fetchone() is not None


def frontend_queue_owner(marker, database, config_path):
    """Return an owner to wait for before starting a shared-delivery stage."""
    from frontend_delivery import delivery_stage, slot_conflicts

    if not database.is_file():
        return None
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        item = conn.execute("SELECT * FROM work_items WHERE marker=? AND state='running'", (marker,)).fetchone()
        if not item or not item['stage'] or not item['stage'].startswith('frontend.'):
            return None
        store = Ledger(conn, json.loads(config_path.read_text(encoding='utf-8-sig')), utc_now())
        route = get_split(store, item)
        if route and delivery_stage(item['stage'], route):
            return slot_conflicts(store, item, config_path.parent)
    return None


def frontend_waiting(marker, database, config_path):
    if not database.is_file():
        return False
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        item = conn.execute("SELECT * FROM work_items WHERE marker=? AND state='running'", (marker,)).fetchone()
        if not item:
            return False
        from frontend_delivery import is_waiting

        store = Ledger(conn, json.loads(config_path.read_text(encoding='utf-8-sig')), utc_now())
        return is_waiting(store, item)


def _active_job(marker, database, config_path):
    if not database.is_file() or not re.fullmatch(r'HNF-[0-9a-f]{20}', marker):
        return None
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        item = conn.execute('''SELECT w.* FROM work_items w JOIN dispatches d
                               ON w.seq=d.item_seq AND w.request_id=d.request_id
                               WHERE w.marker=? AND w.state='running' AND d.state='started'
                                 AND w.chat_id IS NOT NULL AND w.stage='frontend.e2e' ''', (marker,)).fetchone()
        if not item:
            return None
        trial = conn.execute('''SELECT * FROM attempts WHERE item_seq=? AND run_round=? AND outcome IS NULL
                                AND stage='frontend.e2e' AND phase='verify' ''',
                             (item['seq'], item['run_round'])).fetchone()
        if not trial or not re.fullmatch(r'[0-9a-f]{32}', trial['attempt_id']):
            return None
        config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        store = Ledger(conn, config, utc_now())
        route = get_split(store, item)
        if not route or route.get('post_merge_e2e') is not True or not owns_slot(store, item, config_path.parent):
            return None
        deadline = datetime.fromisoformat(trial['deadline_at'])
        if deadline.utcoffset() is None:
            return None
        item_root = database.resolve().parent / 'items' / marker
        folder = item_root / 'frontend' / ('e2e-' + trial['attempt_id'])
        if folder.resolve() != folder or not folder.is_dir():
            return None
        request_path = folder / 'host-request.json'
        if request_path.resolve() != request_path or not request_path.is_file():
            return None
        admitted = json.loads(item['config_snapshot'])
        e2e = admitted.get('e2e', {})
        frontend_key = e2e.get('workspace')
        workspace = admitted.get('workspaces', {}).get(frontend_key)
        config_current = (bool(workspace) and e2e.get('enabled', True) and config.get('e2e') == e2e
                          and config.get('workspaces', {}).get(frontend_key) == workspace)
        return SimpleNamespace(folder=folder, item_root=item_root, attempt_id=trial['attempt_id'],
                               request_id=item['request_id'], chat_id=item['chat_id'], deadline=deadline,
                               request=read_json(request_path), e2e=e2e, config_current=config_current)


def pending_frontend_e2e(marker, database, config_path):
    job = _active_job(marker, database, config_path)
    if not job or (job.folder / 'agentlimb.json').is_file():
        return message('host_e2e_not_pending')
    return message('host_e2e_pending', attempt_id=job.attempt_id)


def _json_input(root, value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('Use a work-item-relative JSON path')
    relative = PurePosixPath(value)
    if relative.is_absolute() or '..' in relative.parts or relative.suffix.lower() != '.json':
        raise ValueError('Use a work-item-relative JSON path')
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or path.suffix.lower() != '.json' or not path.is_file():
        raise ValueError('The request input must resolve to a work-item JSON file')
    return path


def _command(marker, job, stage_root):
    data = job.request
    if not data or data.keys() != {'attempt_id', 'profile', 'scenario', 'base_url'} or data['attempt_id'] != job.attempt_id:
        raise ValueError('Invalid fixed host E2E request')
    profile = _json_input(job.item_root, data['profile'])
    scenario = _json_input(job.item_root, data['scenario'])
    base = data['base_url']
    if not isinstance(base, str) or '\\' in base or any(char.isspace() for char in base):
        raise ValueError('Use a configured local origin')
    address = urlsplit(base)
    preferred = urlsplit(job.e2e.get('agentlimb', {}).get('preflight_origin', ''))
    if (address.scheme not in ('http', 'https') or address.hostname not in ('localhost', '127.0.0.1', '::1')
            or address.username is not None or address.password is not None or address.path not in ('', '/')
            or address.query or address.fragment or (address.scheme, address.port) != (preferred.scheme, preferred.port)):
        raise ValueError('Use the configured local frontend origin')
    inputs = stage_root / 'inputs'
    if inputs.resolve() != inputs:
        raise ValueError('Host E2E inputs must remain in the attempt directory')
    profile_data, scenario_data = read_json(profile), read_json(scenario)
    if profile_data is None or not _case_names(scenario_data):
        raise ValueError('Host E2E requires a JSON profile and a scenario with named cases')
    inputs.mkdir(parents=True, exist_ok=True)
    profile = inputs / 'profile.json'
    scenario = inputs / 'scenario.json'
    _write_receipt(profile, profile_data)
    _write_receipt(scenario, scenario_data)
    startup_log = stage_root / job.attempt_id / HOST_PHASE / 'frontend.log'
    return [sys.executable, '-B', str(Path(__file__).with_name('agentlimb_e2e.py')),
            '--marker', marker, '--base-url', base, '--profile', str(profile), '--scenario', str(scenario),
            '--startup-log', str(startup_log)]


def _case_names(scenario):
    if not isinstance(scenario, dict):
        return []
    if isinstance(scenario.get('steps'), list) and scenario['steps']:
        return ['scenario']
    cases = scenario.get('cases')
    if (not isinstance(cases, list) or not cases or any(
            not isinstance(case, dict) or not isinstance(case.get('name'), str)
            or not re.fullmatch(r'[a-z0-9-]{1,60}', case['name']) for case in cases)):
        return []
    names = [case['name'] for case in cases]
    return names if len(set(names)) == len(names) else []


def _valid_case_results(report, stage_root):
    scenario = stage_root / 'inputs' / 'scenario.json'
    names = _case_names(read_json(scenario)) if scenario.resolve() == scenario else []
    cases = report.get('scenario_cases')
    if not names or not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases):
        return False
    if [case.get('name') for case in cases] != names:
        return False
    statuses = [case.get('status') for case in cases]
    if report['status'] == 'passed':
        return all(status == 'passed' for status in statuses)
    return (report.get('repair_required') is True and 'failed' in statuses
            and all(status in ('passed', 'failed', 'blocked') for status in statuses))


def _write_receipt(path, value):
    pending = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as file:
            pending = Path(file.name)
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write('\n')
        pending.replace(path)
    finally:
        if pending:
            pending.unlink(missing_ok=True)


def _report(marker, job, check, proof=None):
    receipt = job.folder / 'agentlimb.json'
    result = {**check, 'runner': 'agentlimb', 'marker': marker, 'attempt_id': job.attempt_id}
    if proof:
        result['host_run'] = {key: proof[key] for key in ('receipt_path', 'outcome', 'stop_confirmed', 'stop_evidence')}
    _write_receipt(receipt, result)
    return message('host_e2e_finished', attempt_id=job.attempt_id, receipt=str(receipt), result=result['status'])


def run_frontend_e2e(marker, database, config_path, project_root):
    with frontend_e2e_lock(database) as acquired:
        if not acquired:
            return message('host_e2e_busy')
        job = _active_job(marker, database, config_path)
        if not job or (job.folder / 'agentlimb.json').is_file():
            return message('host_e2e_not_pending')
        stage_root = job.folder / 'host-run'
        stage_folder = stage_root / job.attempt_id / HOST_PHASE
        if stage_root.resolve() != stage_root or stage_folder.resolve() != stage_folder:
            return message('host_e2e_stop_unverified', attempt_id=job.attempt_id)
        if stage_folder.exists():
            proof = recover_proof(stage_folder, job.attempt_id, HOST_PHASE, job.deadline)
        else:
            if not job.config_current:
                return _report(marker, job, {'status': 'blocked', 'stage': 'host-authorization'})
            try:
                command = _command(marker, job, stage_root)
            except (OSError, TypeError, ValueError, KeyError):
                return _report(marker, job, {'status': 'blocked', 'stage': 'host-request'})
            proof = run_stage(job.attempt_id, HOST_PHASE, job.deadline, project_root, stage_root, None,
                              command, recoverable=True)
        if not proof or proof.get('stop_confirmed') is not True or proof.get('stop_evidence', {}).get(
                'owned_processes_active_after_stop') != 0:
            return message('host_e2e_stop_unverified', attempt_id=job.attempt_id)
        cleanup = remove_committed_sources(stage_folder)
        current = _active_job(marker, database, config_path)
        if job != current:
            if current and current.attempt_id == job.attempt_id:
                return _report(marker, current, {'status': 'blocked', 'stage': 'host-request-changed'}, proof)
            return message('host_e2e_inactive')
        if not current.config_current:
            return _report(marker, current, {'status': 'blocked', 'stage': 'host-authorization'}, proof)
        check = read_json(Path(proof['stdout_path'])) or {}
        if proof['outcome'] == 'timed_out':
            check = {'status': 'blocked', 'stage': 'host-timeout'}
        elif (check.get('runner') != 'agentlimb' or check.get('status') not in ('passed', 'failed', 'blocked')
              or (check.get('status') == 'passed' and proof['outcome'] != 'success')
              or (check.get('status') in ('passed', 'failed')
                  and (check.get('marker'), check.get('attempt_id')) != (marker, job.attempt_id))):
            check = {'status': 'blocked', 'stage': 'host-runner'}
        elif check['status'] in ('passed', 'failed') and not _valid_case_results(check, stage_root):
            check = {'status': 'blocked', 'stage': 'scenario-results'}
        if cleanup:
            check['source_cleanup'] = cleanup
        return _report(marker, job, check, proof)
