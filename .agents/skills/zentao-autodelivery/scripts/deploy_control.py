"""Coordinate releases from the Windows worker, outside the work-item Agent."""

import json
from datetime import datetime, timezone
from pathlib import Path

from deploy_state import DEPLOY_STAGE, TAG, deployed_receipt_matches, project_services, release_locks, target_key
from execution import active_attempt, attempt_finish, attempt_start, execution_matches, repair_advance
from ledger import Ledger, item_identity, message, transaction, validate_config
from route_evidence import deployment_requirements
from route_execution import get_split, is_routed, snapshot


def deployment_pending(store, item):
    if item['state'] != 'running' or not is_routed(item):
        return False
    if item['stage'] == DEPLOY_STAGE:
        return True
    trial = active_attempt(store, item)
    if trial:
        return trial['phase'] == DEPLOY_STAGE
    if item['stage'] != 'frontend.e2e':
        return False
    previous = store.conn.execute('''SELECT result FROM attempts WHERE item_seq=? AND run_round=? AND stage=?
        AND outcome IS NOT NULL ORDER BY rowid DESC LIMIT 1''', (item['seq'], item['run_round'], item['stage'])).fetchone()
    result = json.loads(previous['result']) if previous else {}
    return result.get('phase') == DEPLOY_STAGE and not result.get('terminal_step')


def acquire_locks(store, item, scope, resources):
    for key in resources:
        lock = store.conn.execute('SELECT item_seq FROM deployment_locks WHERE scope=? AND resource=?',
                                  (scope, key)).fetchone()
        if lock is not None and lock['item_seq'] != item['seq']:
            return False
    store.conn.executemany('INSERT OR IGNORE INTO deployment_locks(scope,resource,item_seq) VALUES(?,?,?)',
                           [(scope, key, item['seq']) for key in resources])
    return True


def release_requests(store, item, directory):
    selected, skipped, versions, blocked = deployment_requirements(store, item)
    config = snapshot(item)
    current = project_services(store.config)
    if blocked or not selected:
        return {}, skipped, None, blocked
    backend_root = (directory / config['workspaces']['backend']).resolve()
    if backend_root != (directory / store.config['workspaces']['backend']).resolve():
        return {}, skipped, None, '后端工作区授权已变更'
    requests = {}
    for key, target in selected.items():
        service = project_services(config)[key]
        live = current.get(key, {})
        release, current_release = service['deployment'], live.get('deployment', {})
        if (not current_release.get('enabled')
                or target != current_release.get('targets', {}).get(release['target'])
                or release.get('build_dependencies', {}) != current_release.get('build_dependencies', {})
                or any(service[field] != live.get(field) for field in ('repository', 'branch', 'build_command'))):
            return {}, skipped, None, f'{key} 的部署目标或构建授权已变更'
        repository = (backend_root / service['repository']).resolve()
        if not repository.is_relative_to(backend_root):
            return {}, skipped, None, f'{key} 的仓库超出配置后端工作区'
        requests[key] = {'service': key, 'sha': versions[key], 'service_config': service, 'target': target,
                         'repository': str(repository), 'backend_root': str(backend_root),
                         'build_dependencies': release.get('build_dependencies', {})}
    gateways = {target['gateway_url'].rstrip('/') for target in selected.values()}
    if len(gateways) != 1 and get_split(store, item)['scope'] == 'fullstack':
        return {}, skipped, None, '前后端工单的部署服务必须使用同一验收网关'
    gateway = next(iter(gateways)) + '/' if len(gateways) == 1 else None
    return requests, skipped, gateway, None


def binding(item):
    return {'item': item_identity(item), 'request_id': item['request_id'], 'chat_id': item['chat_id']}


def trial_fields(item, trial):
    return {'attempt_id': trial['attempt_id'], 'phase': trial['phase'],
            'deadline_at': trial['deadline_at'], 'run_round': item['run_round']}


def claim_deployment(store, item, directory):
    if not execution_matches(store, item, binding(item)):
        return message('stale_execution')
    active = active_attempt(store, item)
    if active and store.conn.execute('SELECT 1 FROM deployment_claims WHERE attempt_id=?',
                                     (active['attempt_id'],)).fetchone():
        return message('deployment_busy', **trial_fields(item, active))
    requests, skipped, gateway, blocked = release_requests(store, item, directory)
    targets = sorted({target_key(request['target']) for request in requests.values()})
    if not acquire_locks(store, item, 'target', targets):
        return message('deployment_busy', reason='同一目标已有工单在发布或验收')
    trial = attempt_start(store, item, {**binding(item), 'stage': item['stage']},
                          config_directory=directory, deployment_worker=True)
    if trial['status'] not in ('attempt_started', 'attempt_active'):
        return trial
    if item['stage'] != DEPLOY_STAGE and trial['phase'] != DEPLOY_STAGE:
        raise ValueError('The active attempt is no longer a deployment')
    store.conn.execute('INSERT INTO deployment_claims VALUES(?,?)', (trial['attempt_id'], item['seq']))
    return message('deployment_claimed', **trial_fields(item, trial), requests=requests, skipped=skipped,
                   gateway_url=gateway, **({'blocked': blocked} if blocked else {}))


def lock_image_tags(store, item, directory, fields):
    requests, _, _, blocked = release_requests(store, item, directory)
    if blocked:
        return message('deployment_revoked', reason=blocked)
    tags = fields.get('tags')
    if (not isinstance(tags, list) or any(not isinstance(tag, str) or not TAG.fullmatch(tag) for tag in tags)
            or {tag.rsplit(':', 1)[0] for tag in tags} != {
                f'{request["target"]["registry"]}/{request["target"]["image_repository"]}'
                for request in requests.values()}):
        raise ValueError('Image tag locks must match the release targets')
    status = 'deployment_tags_locked' if acquire_locks(store, item, 'tag', sorted(set(tags))) else 'deployment_busy'
    return message(status)


def finish_deployment(store, item, trial, directory, fields):
    data = {**fields, **binding(item), 'phase': trial['phase']}
    evidence = data.get('evidence')
    if not isinstance(evidence, dict) or not isinstance(evidence.get('services'), dict):
        raise ValueError('Deployment completion requires per-service receipts')
    selected, _, versions, _ = deployment_requirements(store, item)
    for service, receipt in evidence['services'].items():
        if service not in selected or not isinstance(receipt, dict):
            raise ValueError('Deployment receipt names an unplanned service')
        if receipt.get('status') == 'deployed':
            if not deployed_receipt_matches(receipt, service, versions.get(service), selected[service]):
                return message('evidence_required', reason=f'Invalid deployment receipt for {service}')
            target = selected[service]
            endpoint = target['gateway_url'].rstrip('/') + target['verification_path']
            pods = receipt.get('pod_verification')
            if (receipt['verification'].get('url') != endpoint or not isinstance(pods, dict)
                    or not pods.get('deployment_uid') or type(pods.get('ready_replicas')) is not int
                    or pods['ready_replicas'] < 1):
                return message('evidence_required', reason=f'Missing pod or gateway readback for {service}')
            tag_locked = store.conn.execute('''SELECT 1 FROM deployment_locks
                WHERE scope='tag' AND resource=? AND item_seq=?''', (receipt['image'], item['seq'])).fetchone()
            if not tag_locked:
                return message('evidence_required', reason=f'Missing image tag lock for {service}')
    if data.get('outcome') == 'success' and trial['phase'] != 'verify':
        result = repair_advance(store, item, data, config_directory=directory, deployment_worker=True)
    else:
        data['completion'] = '待验证'
        result = attempt_finish(store, item, data, config_directory=directory, deployment_worker=True)
    if result['status'] in ('repair_advanced', 'attempt_finished'):
        release_locks(store, item, tags_only=True)
        store.conn.execute('DELETE FROM deployment_claims WHERE attempt_id=?', (trial['attempt_id'],))
    return result


def deployment_action(action, marker, database, config_path, **fields):
    """Worker-only entry point, using the admitted item and current project permissions."""
    config_path = Path(config_path).resolve()
    directory = config_path.parent
    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    validate_config(config)
    with transaction(database) as conn:
        store = Ledger(conn, config, datetime.now(timezone.utc))
        item = conn.execute('SELECT * FROM work_items WHERE marker=?', (marker,)).fetchone()
        if not item or not deployment_pending(store, item):
            return message('deployment_not_pending')
        if action == 'peek':
            return message('deployment_pending')
        if action == 'claim':
            return claim_deployment(store, item, directory)
        trial = active_attempt(store, item)
        if (not trial or trial['attempt_id'] != fields.get('attempt_id')
                or not conn.execute('SELECT 1 FROM deployment_claims WHERE attempt_id=? AND item_seq=?',
                                    (trial['attempt_id'], item['seq'])).fetchone()):
            return message('stale_attempt')
        if action == 'lock_tags':
            return lock_image_tags(store, item, directory, fields)
        if action == 'finish':
            return finish_deployment(store, item, trial, directory, fields)
        raise ValueError('Unknown deployment action')
