"""Serialize delivery across work items that share the frontend workspace or local ports."""

import json
from os.path import normcase
from urllib.parse import urlsplit

from deploy_state import FRONTEND_DEPLOY_STAGE

SCOPE = 'frontend-delivery'
WAIT_SCOPE = 'frontend-delivery-wait'


def delivery_stage(stage, route):
    return bool(route and stage in ('frontend.integration', 'frontend.e2e', 'frontend.push', FRONTEND_DEPLOY_STAGE))


def resources(item, config_directory):
    config = json.loads(item['config_snapshot'])
    frontend = config.get('workspaces', {}).get('frontend')
    if not frontend:
        return set()
    workspace = (config_directory / frontend).resolve()
    keys = {f'workspace:{normcase(str(workspace))}'}
    applications = config['development'].get('frontend_bundle', {}).get('applications', {})
    for application in applications.values():
        repository = (workspace / application['repository']).resolve()
        keys.add(f'branch:{normcase(str(repository))}:{application["branch"]}')
    e2e = config.get('e2e', {})
    startup_ports = e2e.get('startup_ports')
    if startup_ports is None:
        browser = e2e.get('agentlimb', e2e.get('playwright', {}))
        startup_ports = list(browser.get('preflight_startup_ports', []))
        origin = urlsplit(browser.get('preflight_origin', ''))
        if origin.hostname:
            startup_ports.append(origin.port or (443 if origin.scheme == 'https' else 80))
    keys.update(f'port:{port}' for port in startup_ports)
    return keys


def slot_conflicts(store, item, config_directory):
    wanted = resources(item, config_directory)
    return next((seq for key, seq in store.conn.execute(
        'SELECT resource,item_seq FROM deployment_locks WHERE scope=? AND item_seq!=?', (SCOPE, item['seq']))
                 if key in wanted), None)


def holds_delivery(store, item):
    return store.conn.execute('SELECT 1 FROM deployment_locks WHERE scope=? AND item_seq=? LIMIT 1',
                              (SCOPE, item['seq'])).fetchone() is not None


def owns_slot(store, item, config_directory):
    wanted = resources(item, config_directory)
    owned = {row[0] for row in store.conn.execute(
        'SELECT resource FROM deployment_locks WHERE scope=? AND item_seq=?', (SCOPE, item['seq']))}
    return bool(wanted) and wanted <= owned


def is_waiting(store, item):
    return store.conn.execute('SELECT 1 FROM deployment_locks WHERE scope=? AND item_seq=? LIMIT 1',
                              (WAIT_SCOPE, item['seq'])).fetchone() is not None


def acquire_slot(store, item, config_directory):
    # Called inside the same immediate transaction that opens the attempt.
    if slot_conflicts(store, item, config_directory) is not None:
        store.conn.execute('INSERT OR IGNORE INTO deployment_locks(scope,resource,item_seq) VALUES(?,?,?)',
                           (WAIT_SCOPE, str(item['seq']), item['seq']))
        return False
    store.conn.execute('DELETE FROM deployment_locks WHERE scope=? AND item_seq=?', (WAIT_SCOPE, item['seq']))
    store.conn.executemany('INSERT OR IGNORE INTO deployment_locks(scope,resource,item_seq) VALUES(?,?,?)',
                           [(SCOPE, key, item['seq']) for key in sorted(resources(item, config_directory))])
    return True


def release_slot(store, item):
    store.conn.execute('DELETE FROM deployment_locks WHERE scope IN (?,?) AND item_seq=?',
                       (SCOPE, WAIT_SCOPE, item['seq']))
