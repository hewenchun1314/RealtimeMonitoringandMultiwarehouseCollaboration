"""Coordinate releases from the Windows worker, outside the work-item Agent."""

import json
from datetime import datetime, timezone
from pathlib import Path

from backend_dependencies import backend_services, writable_dependencies
from branch_check import valid_target
from deploy_state import (COMMIT, DEPLOY_STAGE, FRONTEND_DEPLOY_STAGE, GIT_PUSH_STAGES, TAG, WORKER_STAGES,
                          deployed_receipt_matches, frontend_release_config, project_services, release_locks, target_key)
from execution import active_attempt, attempt_finish, attempt_seed, attempt_start, execution_matches, repair_advance
from frontend_delivery import acquire_slot, delivery_stage
from ledger import Ledger, item_identity, message, transaction, validate_config
from route_evidence import (deployment_requirements, frontend_acceptance_terminal, git_push_evidence_error,
                            git_push_requirements, push_covers)
from route_execution import contract_revision_pending, get_split, is_routed, read_history, snapshot


def pending_worker_stage(store, item):
    if item['state'] != 'running' or not is_routed(item):
        return None
    if item['stage'] in WORKER_STAGES:
        return item['stage']
    trial = active_attempt(store, item)
    if trial:
        return trial['phase'] if trial['phase'] in WORKER_STAGES else None
    if item['stage'] != 'frontend.e2e':
        return None
    seed = attempt_seed(store, item, routed=True)
    return seed[1] if seed and seed[1] in WORKER_STAGES else None


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


def frontend_request(store, item, directory):
    config = snapshot(item)
    settings = frontend_release_config(config)
    live = frontend_release_config(store.config)
    approved = {**settings}
    if live.get('image_mode') == 'reuse':
        approved['image_mode'] = 'reuse'
    source = (directory / config['workspaces']['frontend']).resolve()
    if (not settings.get('enabled') or approved != live
            or source != (directory / store.config.get('workspaces', {}).get('frontend', '.')).resolve()):
        raise ValueError('前端镜像发布或前端工作区授权已变更')
    route, steps = read_history(store, item)
    if contract_revision_pending(steps):
        raise ValueError('接口契约变更，等待重新拆分后核验前端发布')
    required = [name for name in route['stages'] if name.startswith('frontend.') and name not in
                (FRONTEND_DEPLOY_STAGE, 'frontend.push')]
    post_merge = route.get('post_merge_e2e')
    if post_merge:
        required = [name for name in required if name != 'frontend.e2e']
    if any(steps.get(name, {}).get('outcome') != 'success' for name in required):
        raise ValueError('前端发布需要本单前端验证及合回成功')
    planned = steps['frontend.plan']['evidence']['applications']
    versions = steps['frontend.integration']['evidence']['merged_versions']
    if post_merge and not frontend_acceptance_terminal(route, steps, versions):
        raise ValueError('前端发布需要最终版本的 E2E 已结束')
    if not planned or set(planned) != versions.keys():
        raise ValueError('前端应用与合回 SHA 清单不一致')
    if not push_covers(store, item, 'frontend.push', versions):
        raise ValueError('前端整包发布需要全部计划应用的 Git 远端回执')
    application_settings = config['development']['frontend_bundle']['applications']
    live_apps = store.config['development']['frontend_bundle']['applications']
    applications = {}
    for key in planned:
        app = application_settings.get(key)
        if app is None or app != live_apps.get(key) or not COMMIT.fullmatch(versions[key]):
            raise ValueError(f'{key} 的发布版本或仓库授权无效')
        relative = app['repository']
        repository = (source / relative).resolve()
        if not repository.is_relative_to(source):
            raise ValueError(f'{key} 的仓库超出前端工作区')
        applications[key] = {'repository': str(repository), 'path': relative, 'branch': app['branch'],
                             'sha': versions[key]}
    if len({app['repository'] for app in applications.values()}) != len(applications):
        raise ValueError('前端发布应用必须映射到不同仓库')
    return {'marker': item['marker'], 'settings': approved, 'applications': applications,
            'git_push_verified': 'frontend.push' in route['stages'],
            'release_dir': str(directory.parent / 'customized-release')}


def push_location(config, directory, side, key):
    dependency = writable_dependencies(config).get(key) if side == 'backend' else None
    if side == 'backend':
        target = backend_services(config).get(key)
    else:
        target = config['development'].get('frontend_bundle', {}).get('applications', {}).get(key)
    workspace = dependency['source_path'] if dependency else config.get('workspaces', {}).get(side)
    if not workspace or not valid_target(target):
        return None
    root = (directory / workspace).resolve()
    repository = (root / target['repository']).resolve()
    if not repository.is_relative_to(root):
        return None
    return repository, target['branch']


def push_request(store, item, directory, stage):
    config = snapshot(item)
    if not all(settings.get('runtime', {}).get('git_push_enabled') for settings in (config, store.config)):
        return {}, {}, 'Git 推送授权已关闭'
    versions, branches, errors, blocked = git_push_requirements(store, item, stage)
    side = stage.split('.')[0]
    requests = {}
    for key, sha in sorted(versions.items()):
        location = push_location(config, directory, side, key)
        current = push_location(store.config, directory, side, key)
        proof = branches[key]
        if (not location or location != current or proof.get('status') != 'verified'
                or (str(location[0]), location[1]) != (proof.get('source_root'), proof.get('target_branch'))):
            errors[key] = '仓库或目标分支授权已变更，或不匹配分支核验回执'
            continue
        repository, branch = location
        requests[key] = {'repository': str(repository), 'branch': branch, 'sha': sha}
    return requests, errors, blocked


def claim_push(store, item, directory, stage):
    requests, errors, blocked = push_request(store, item, directory, stage)
    resources = sorted({json.dumps([str(Path(request['repository'])), request['branch']])
                        for request in requests.values()})
    if not acquire_locks(store, item, 'git-ref', resources):
        return message('deployment_busy', reason='同一仓库目标分支已有工单在推送')
    trial = attempt_start(store, item, {**binding(item), 'stage': item['stage']},
                          config_directory=directory, deployment_worker=True)
    if trial['status'] not in ('attempt_started', 'attempt_active'):
        return trial
    store.conn.execute('INSERT INTO deployment_claims VALUES(?,?)', (trial['attempt_id'], item['seq']))
    return message('deployment_claimed', **trial_fields(item, trial), push_requests=requests,
                   errors=errors, blocked=blocked)


def claim_frontend(store, item, directory):
    request, blocked = None, None
    try:
        request = frontend_request(store, item, directory)
    except (ValueError, TypeError, KeyError) as error:
        blocked = str(error)
    if request:
        settings = request['settings']
        resources = ['frontend-image/' + settings['image_repository']]
        if target := settings.get('rancher'):
            resources.append(target_key(target))
        if not acquire_locks(store, item, 'target', resources):
            return message('deployment_busy', reason='同一前端镜像仓库或工作负载已有工单在发布')
    trial = attempt_start(store, item, {**binding(item), 'stage': item['stage']},
                          config_directory=directory, deployment_worker=True)
    if trial['status'] not in ('attempt_started', 'attempt_active'):
        return trial
    store.conn.execute('INSERT INTO deployment_claims VALUES(?,?)', (trial['attempt_id'], item['seq']))
    return message('deployment_claimed', **trial_fields(item, trial), frontend_request=request, blocked=blocked)


def claim_deployment(store, item, directory, stage):
    if not execution_matches(store, item, binding(item)):
        return message('stale_execution')
    active = active_attempt(store, item)
    if active and store.conn.execute('SELECT 1 FROM deployment_claims WHERE attempt_id=?',
                                     (active['attempt_id'],)).fetchone():
        return message('deployment_busy', **trial_fields(item, active))
    if delivery_stage(item['stage'], get_split(store, item)) and not acquire_slot(store, item, directory):
        return message('deployment_busy', reason='等待前端合回及发布占用释放')
    if stage in GIT_PUSH_STAGES:
        return claim_push(store, item, directory, stage)
    if item['stage'] == FRONTEND_DEPLOY_STAGE:
        return claim_frontend(store, item, directory)
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
    stage = trial['phase'] if trial['phase'] != 'verify' else item['stage']
    if stage in GIT_PUSH_STAGES:
        if not isinstance(evidence, dict):
            return message('evidence_required', reason='Git 推送需要逐仓回执')
        if reason := git_push_evidence_error(store, item, stage, evidence, partial=data.get('outcome') != 'success'):
            return message('evidence_required', reason=reason)
        if data.get('outcome') == 'success':
            requests, errors, blocked = push_request(store, item, directory, stage)
            if blocked or errors or requests.keys() != evidence['repositories'].keys():
                data.update(outcome='failed', summary=blocked or 'Git 推送授权在执行期间变更', retryable=False)
        if data.get('outcome') == 'success' and trial['phase'] != 'verify':
            result = repair_advance(store, item, data, config_directory=directory, deployment_worker=True)
        else:
            result = attempt_finish(store, item, data, config_directory=directory, deployment_worker=True)
        if result['status'] in ('attempt_finished', 'repair_advanced'):
            store.conn.execute("DELETE FROM deployment_locks WHERE scope='git-ref' AND item_seq=?", (item['seq'],))
            store.conn.execute('DELETE FROM deployment_claims WHERE attempt_id=?', (trial['attempt_id'],))
        return result
    if item['stage'] == FRONTEND_DEPLOY_STAGE:
        if data.get('outcome') == 'success':
            try:
                frontend_request(store, item, directory)
            except (ValueError, TypeError, KeyError) as error:
                data.update(outcome='failed', summary=str(error), retryable=False)
        data['completion'] = '待验证'
        result = attempt_finish(store, item, data, config_directory=directory, deployment_worker=True)
        if result['status'] == 'attempt_finished':
            store.conn.execute('DELETE FROM deployment_claims WHERE attempt_id=?', (trial['attempt_id'],))
        return result
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
            if not target.get('mcp_url'):
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
        stage = pending_worker_stage(store, item) if item else None
        if not stage:
            return message('deployment_not_pending')
        if action == 'peek':
            return message('deployment_pending', stage=stage)
        if action == 'claim':
            return claim_deployment(store, item, directory, stage)
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
