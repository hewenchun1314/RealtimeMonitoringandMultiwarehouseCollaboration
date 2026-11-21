"""SQLite persistence and shared input contracts for the two workflow commands."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from delivery_scope import project_mode, route_stages
from task_evidence import EVIDENCE_SCHEMA

STAGES = ('material_check', 'design', 'plan', 'branch_check', 'coding', 'unit_test', 'code_review', 'e2e', 'integration')
STAGE_NAMES = dict(zip(STAGES, ('材料评审', '设计', '计划', '分支核验', '编码', '单元测试', 'Code Review', 'E2E', '合回')))
STAGE_NAMES.update(split='范围判定与任务拆分', handoff='后端联调交接')
for scoped_stage in route_stages('fullstack'):
    if '.' in scoped_stage:
        side, step = scoped_stage.split('.')
        STAGE_NAMES[scoped_stage] = ('前端' if side == 'frontend' else '后端') + (
            '服务验收' if step == 'acceptance' else STAGE_NAMES[step])
ITEM_FIELDS = ('instance', 'project_id', 'kind', 'id')

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    instance TEXT NOT NULL, project_id TEXT NOT NULL, project_name TEXT NOT NULL,
    scan_id TEXT NOT NULL, observed_at TEXT NOT NULL,
    PRIMARY KEY(instance, project_id)
);
CREATE TABLE IF NOT EXISTS work_items (
    seq INTEGER PRIMARY KEY,
    instance TEXT NOT NULL, project_id TEXT NOT NULL, kind TEXT NOT NULL, item_id TEXT NOT NULL,
    title TEXT NOT NULL, assigned_to TEXT NOT NULL, source_status TEXT NOT NULL,
    priority INTEGER, eligible INTEGER NOT NULL, observed_at TEXT NOT NULL,
    state TEXT NOT NULL, marker TEXT NOT NULL UNIQUE, config_snapshot TEXT,
    chat_id TEXT UNIQUE, desktop_project_id TEXT, request_id TEXT,
    stage TEXT, run_round INTEGER NOT NULL DEFAULT 1, conclusion TEXT,
    UNIQUE(instance, project_id, kind, item_id)
);
CREATE INDEX IF NOT EXISTS queue ON work_items(instance, project_id, state, priority, seq);
CREATE TABLE IF NOT EXISTS dispatches (
    request_id TEXT PRIMARY KEY, item_seq INTEGER NOT NULL REFERENCES work_items(seq),
    desktop_project_id TEXT NOT NULL, state TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY, item_seq INTEGER NOT NULL REFERENCES work_items(seq),
    run_round INTEGER NOT NULL, stage TEXT NOT NULL, retry_number INTEGER NOT NULL,
    started_at TEXT NOT NULL, deadline_at TEXT, ended_at TEXT, outcome TEXT, result TEXT,
    phase TEXT NOT NULL, phase_started_at TEXT NOT NULL, phase_history TEXT NOT NULL DEFAULT '[]',
    UNIQUE(item_seq, run_round, stage, retry_number)
);
CREATE UNIQUE INDEX IF NOT EXISTS single_attempt ON attempts(item_seq) WHERE outcome IS NULL;
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, request TEXT NOT NULL, response TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recoveries (
    item_seq INTEGER NOT NULL REFERENCES work_items(seq), recovery_id TEXT NOT NULL, evidence TEXT NOT NULL,
    PRIMARY KEY(item_seq, recovery_id)
);
CREATE TABLE IF NOT EXISTS dingtalk_notifications (
    operation_id TEXT PRIMARY KEY, item_seq INTEGER NOT NULL REFERENCES work_items(seq),
    event_type TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS writebacks (
    operation_id TEXT PRIMARY KEY, item_seq INTEGER NOT NULL REFERENCES work_items(seq),
    run_round INTEGER NOT NULL, operation_type TEXT NOT NULL, payload TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending', lease_id TEXT, owner TEXT, expires_at TEXT,
    reconcile_required INTEGER NOT NULL DEFAULT 0, receipt TEXT
);
CREATE INDEX IF NOT EXISTS pending_writebacks ON writebacks(state, expires_at);
CREATE INDEX IF NOT EXISTS split_task_operations ON writebacks(item_seq,json_extract(payload,'$.key'))
    WHERE operation_type IN ('task_create','task_close','task_reopen');
CREATE TABLE IF NOT EXISTS split_tasks (
    item_seq INTEGER NOT NULL REFERENCES work_items(seq), task_key TEXT NOT NULL,
    instance TEXT NOT NULL, side TEXT NOT NULL, definition TEXT NOT NULL,
    desired TEXT NOT NULL DEFAULT 'open', generation INTEGER NOT NULL DEFAULT 0,
    remote_id TEXT, remote_status TEXT, execution_id TEXT, assignee TEXT,
    retry_at TEXT, last_error TEXT, PRIMARY KEY(item_seq,task_key), UNIQUE(instance,remote_id)
);
""" + EVIDENCE_SCHEMA


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def message(status, **fields):
    return {'ok': True, 'status': status, **fields}


def require_mapping(value, field):
    if not isinstance(value, dict):
        raise ValueError(f'{field} must be an object')
    return value


def require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field} must be a non-empty string')
    return value


def source_id(value, field):
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdecimal() and int(value) > 0:
        return str(int(value))
    raise ValueError(f'{field} must be a positive integer or its string representation')


def source_kind(value):
    if value not in ('story', 'bug'):
        raise ValueError('kind must be story or bug')
    return value


def item_key(item):
    item = require_mapping(item, 'item')
    return (require_text(item.get('instance'), 'instance').rstrip('/'),
            source_id(item.get('project_id'), 'project_id'), source_kind(item.get('kind')),
            source_id(item.get('id'), 'id'))


def item_identity(row):
    return dict(zip(ITEM_FIELDS, (row['instance'], row['project_id'], row['kind'], row['item_id'])))


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def observed(value, field):
    try:
        parsed = datetime.fromisoformat(require_text(value, field))
        if parsed.utcoffset() is not None:
            return stamp(parsed)
    except ValueError:
        pass
    raise ValueError(f'{field} must be an ISO timestamp with an offset')


def runtime_int(config, name, default, minimum=1):
    value = config.get('runtime', {}).get(name, default)
    if type(value) is not int or value < minimum:
        raise ValueError(f'runtime.{name} must be an integer >= {minimum}')
    return value


def workflow_stages(development):
    return STAGES if 'frontend_bundle' in development else tuple(stage for stage in STAGES if stage != 'branch_check')


def validate_backend(config, development):
    require_text(require_mapping(config.get('workspaces'), 'workspaces').get('backend'), 'workspaces.backend')
    bundle = require_mapping(development.get('backend_bundle'), 'development.backend_bundle')
    require_text(bundle.get('name'), 'backend_bundle.name')
    services = require_mapping(bundle.get('services'), 'backend_bundle.services')
    if not services:
        raise ValueError('backend_bundle.services must contain a service')
    for key, service in services.items():
        require_text(key, 'service key')
        service = require_mapping(service, key)
        for field in ('repository', 'branch', 'build_command', 'test_command', 'acceptance_command'):
            require_text(service.get(field), f'{key}.{field}')
        path = Path(service['repository'])
        if path.anchor or '..' in path.parts:
            raise ValueError(f'{key}.repository must be relative to the backend workspace')
        require_text(service.get('start_command') or service.get('service_url'), f'{key}.start_command or service_url')


def validate_config(config):
    config = require_mapping(config, 'config')
    zentao = require_mapping(config.get('zentao'), 'zentao')
    require_text(zentao.get('base_url'), 'zentao.base_url')
    require_text(zentao.get('project_name'), 'zentao.project_name')
    require_strings(zentao.get('assigned_to_accounts'), 'zentao.assigned_to_accounts')
    accepted = require_mapping(zentao.get('accepted_statuses'), 'zentao.accepted_statuses')
    for kind in ('story', 'bug'):
        require_strings(accepted.get(kind), f'zentao.accepted_statuses.{kind}')
    monitor = require_mapping(config.get('monitor'), 'monitor')
    if type(monitor.get('enabled')) is not bool:
        raise ValueError('monitor.enabled must be a boolean')
    e2e = require_mapping(config.get('e2e', {}), 'e2e')
    if type(e2e.get('enabled', True)) is not bool:
        raise ValueError('e2e.enabled must be a boolean')
    development = require_mapping(config.get('development'), 'development')
    if type(development.get('max_concurrent_items')) is not int or development['max_concurrent_items'] < 1:
        raise ValueError('development.max_concurrent_items must be a positive integer')
    if type(development.get('max_retries_per_step')) is not int or not 0 <= development['max_retries_per_step'] <= 3:
        raise ValueError('development.max_retries_per_step must be an integer from zero to three')
    timeouts = require_mapping(development.get('step_timeout_minutes'), 'development.step_timeout_minutes')
    mode = project_mode(development)
    if mode != 'frontend_only':
        validate_backend(config, development)
    if mode != 'backend_only' and 'frontend_bundle' in development:
        bundle = require_mapping(development['frontend_bundle'], 'development.frontend_bundle')
        require_text(bundle.get('name'), 'development.frontend_bundle.name')
        applications = require_mapping(bundle.get('applications'), 'development.frontend_bundle.applications')
        for application in applications:
            require_text(application, 'application')
        workspaces = require_mapping(config.get('workspaces'), 'workspaces')
        require_text(workspaces.get('frontend'), 'workspaces.frontend')
    required = set(workflow_stages(development)) - {'integration'}
    if mode == 'backend_only':
        required.discard('branch_check')
        if 'e2e' not in timeouts:
            required.discard('e2e')
            if 'backend.acceptance' not in timeouts:
                required.add('acceptance')
    for stage in required | set(timeouts):
        if type(timeouts.get(stage)) is not int or not 1 <= timeouts[stage] <= 1440:
            raise ValueError(f'development.step_timeout_minutes.{stage} must be an integer from one to 1440')
    runtime = require_mapping(config.get('runtime', {}), 'runtime')
    if 'split_task_sync' in runtime and type(runtime['split_task_sync']) is not bool:
        raise ValueError('runtime.split_task_sync must be a boolean')
    if 'desktop_project_id' in runtime:
        require_text(runtime['desktop_project_id'], 'runtime.desktop_project_id')
    if 'require_unattended_approval' in runtime and type(runtime['require_unattended_approval']) is not bool:
        raise ValueError('runtime.require_unattended_approval must be a boolean')
    notifications = require_mapping(config.get('notifications', {}), 'notifications')
    dingtalk = require_mapping(notifications.get('dingtalk', {}), 'notifications.dingtalk')
    if type(dingtalk.get('enabled', False)) is not bool:
        raise ValueError('notifications.dingtalk.enabled must be a boolean')
    for name, default in (('dispatch_lease_seconds', 300), ('writeback_lease_seconds', 120),
                          ('observation_max_age_seconds', 300)):
        runtime_int(config, name, default)


def require_strings(value, field):
    if not isinstance(value, list):
        raise ValueError(f'{field} must be an array')
    for entry in value:
        require_text(entry, field)


class Ledger:
    def __init__(self, conn, config, now):
        self.conn = conn
        self.config = config
        self.now = now
        self.time = stamp(now)

    def expiry(self, option, default):
        return stamp(self.now + timedelta(seconds=runtime_int(self.config, option, default)))

    def find_item(self, item):
        row = self.conn.execute('''SELECT * FROM work_items WHERE instance=? AND project_id=?
                                   AND kind=? AND item_id=?''', item_key(item)).fetchone()
        if row is None:
            raise ValueError('Unknown work item')
        return row

    def update_item(self, row, **fields):
        assignments = ', '.join(f'{key}=?' for key in fields)
        self.conn.execute(f'UPDATE work_items SET {assignments} WHERE seq=?', (*fields.values(), row['seq']))

    def fresh_observation(self, when):
        window = timedelta(seconds=runtime_int(self.config, 'observation_max_age_seconds', 300))
        return stamp(self.now - window) <= when <= self.time


@contextmanager
def transaction(database):
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA foreign_keys = ON')
        conn.executescript(SCHEMA)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
    finally:
        conn.close()
