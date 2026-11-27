"""Execute and record the ledger's backend deployment outside the work-item chat."""

from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sys
import time

from stage_runner import recover_job_stop, run_stage, utc_now, write_json


def deployment_action(action, marker, database, config_path, **parameters):
    from deploy_control import deployment_action as control

    return control(action, marker, database, config_path, **parameters)


def image_tag(request):
    from backend_deploy import image_tag as get_tag

    return get_tag(request)


def service_key(service, request):
    data = json.dumps([service, request['sha'], request['target']], sort_keys=True).encode('utf-8')
    return sha256(data).hexdigest()[:24]


def observed_receipt(path, request):
    try:
        receipt = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if (isinstance(receipt, dict)
            and all(receipt.get(key) == request[key] for key in ('service', 'sha', 'target'))):
        return receipt
    return None


def attempt_location(database, marker, claim):
    root = database.parent / 'items' / marker / f'round-{claim["run_round"]}' / 'backend' / 'deploy'
    return root, root / 'attempts' / claim['attempt_id'] / 'finish.json'


def record_finish(action, checkpoint, deadline, result, *, timeout_summary='后端部署已到执行截止时间'):
    timed_out = (utc_now() >= deadline and result['outcome'] != 'timed_out')
    if timed_out:
        result = {**result, 'outcome': 'timed_out', 'summary': timeout_summary}
    write_json(checkpoint, result)
    response = action('finish', **result)
    if response['status'] == 'deadline_elapsed' and result['outcome'] != 'timed_out':
        result = {**result, 'outcome': 'timed_out', 'summary': timeout_summary}
        write_json(checkpoint, result)
        response = action('finish', **result)
    return response


def wait_for_tag_locks(action, attempt_id, tags, deadline):
    while utc_now() < deadline:
        response = action('lock_tags', attempt_id=attempt_id, tags=tags)
        if response['status'] != 'deployment_busy':
            return response
        remaining = (deadline - utc_now()).total_seconds()
        if remaining > 0:
            time.sleep(min(remaining, 2))
    return {'status': 'deadline_elapsed'}


def initial_result(claim):
    return {
        'attempt_id': claim['attempt_id'], 'outcome': 'success',
        'summary': '后端部署核验通过',
        'evidence': {'services': {}, 'skipped': claim.get('skipped', []),
                     'gateway_url': claim.get('gateway_url')},
        'stop_confirmed': True, 'stop_evidence': {'services': {}},
    }


def register_service(result, service, approved, journal, proof):
    result['stop_evidence']['services'][service] = proof['stop_evidence']
    receipt = observed_receipt(journal, approved)
    succeeded = proof['outcome'] == 'success' and receipt and receipt.get('status') == 'deployed'
    if not succeeded:
        if proof['outcome'] == 'timed_out':
            result['outcome'] = 'timed_out'
        elif result['outcome'] != 'timed_out':
            result['outcome'] = 'failed'
        receipt = {**(receipt or {}), 'service': service, 'sha': approved['sha'],
                   'target': approved['target'], 'status': 'failed'}
    result['evidence']['services'][service] = receipt
    return bool(succeeded)


def recover_stage(directory, attempt_id, phase):
    try:
        return json.loads((directory / 'result.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        try:
            launch = json.loads((directory / 'launch.json').read_text(encoding='utf-8'))
        except FileNotFoundError:
            # The runner records launch.json before opening the bootstrap gate.
            stop = {'mechanism': 'bootstrap_gate_never_opened', 'owned_processes_active_after_stop': 0}
        else:
            if launch.get('attempt_id') != attempt_id or launch.get('phase') != phase:
                return None
            stop = recover_job_stop(launch)
        if stop is None:
            return None
        return {'attempt_id': attempt_id, 'phase': phase, 'stop_confirmed': True,
                'outcome': 'failed', 'stop_evidence': stop}


def recover_attempt(root, checkpoint, claim):
    attempt_id = claim['attempt_id']
    try:
        try:
            batch = json.loads(checkpoint.with_name('batch.json').read_text(encoding='utf-8'))
        except FileNotFoundError:
            # The current worker holds the exclusive work-item lock.
            # Publication cannot start without durable batch and launch files.
            if checkpoint.parent.exists() and any(
                    path.name.startswith('publish-') or path.name.endswith('.request.json')
                    for path in checkpoint.parent.iterdir()):
                return None
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            return {**initial_result(claim), 'outcome': 'failed',
                    'summary': '发布执行器在启动子进程前中断，可恢复',
                    'stop_evidence': {'mechanism': 'no_launch_records'}}
        if batch['attempt_id'] != attempt_id:
            return None
        result = initial_result(batch)
        stopped, not_finished = [], []
        for service, approved in batch['requests'].items():
            key = service_key(service, approved)
            phase = 'publish-' + key
            directory = checkpoint.parent / phase
            if not directory.exists():
                not_finished.append(service)
                continue
            proof = recover_stage(directory, attempt_id, phase)
            if (not proof or proof['attempt_id'] != attempt_id or proof['phase'] != phase
                    or proof['stop_confirmed'] is not True or not proof['stop_evidence']):
                return None
            stopped.append(service)
            if not register_service(result, service, approved, root / 'services' / key / 'receipt.json', proof):
                not_finished.append(service)
        if not_finished:
            if result['outcome'] != 'timed_out':
                result['outcome'] = 'failed'
            result['summary'] = '发布执行中断或未通过：' + '、'.join(not_finished)
            result['stop_evidence']['started_services'] = stopped
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def run_deployment(marker, database, config_path, project_root, *, recover=False):
    def action(name, **parameters):
        return deployment_action(name, marker, database, config_path, **parameters)

    claim = action('claim')
    status = claim['status']
    if status not in ('deployment_claimed', 'deployment_busy') or 'attempt_id' not in claim:
        return claim
    root, checkpoint = attempt_location(database, marker, claim)
    deadline = datetime.fromisoformat(claim['deadline_at'])
    if status == 'deployment_busy':
        if not recover:
            return claim
        # Only replay a known, durably stopped execution. An unknown process
        # outcome must retain the original attempt and target locks.
        try:
            result = json.loads(checkpoint.read_text(encoding='utf-8'))
        except FileNotFoundError:
            result = recover_attempt(root, checkpoint, claim)
        except (ValueError, OSError):
            return claim
        if (not isinstance(result, dict) or result.get('attempt_id') != claim['attempt_id']
                or result.get('stop_confirmed') is not True or not result.get('stop_evidence')):
            return claim
        return record_finish(action, checkpoint, deadline, result)

    attempt_id = claim['attempt_id']
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    result = initial_result(claim)
    requests = claim['requests']
    if claim.get('blocked'):
        result.update(outcome='skipped', summary=claim['blocked'], retryable=False)
        return record_finish(action, checkpoint, deadline, result)
    if not requests:
        result['summary'] = '本轮没有适用的后端部署目标'
        return record_finish(action, checkpoint, deadline, result)

    write_json(checkpoint.with_name('batch.json'), {
        'attempt_id': attempt_id, 'requests': requests, 'skipped': claim.get('skipped', []),
        'gateway_url': claim.get('gateway_url'),
    })
    try:
        tags = {service: image_tag(request) for service, request in sorted(requests.items())}
    except (OSError, TypeError, ValueError, RuntimeError):
        result.update(outcome='failed', summary='无法计算已合回版本的镜像标签', retryable=False)
        return record_finish(action, checkpoint, deadline, result)
    lock_response = wait_for_tag_locks(action, attempt_id, sorted(set(tags.values())), deadline)
    lock = lock_response['status']
    if lock == 'deployment_revoked':
        result.update(outcome='failed', summary=lock_response.get('reason', '部署目标授权已变更'), retryable=False)
        return record_finish(action, checkpoint, deadline, result)
    if lock != 'deployment_tags_locked':
        if lock != 'deadline_elapsed':
            return {'status': lock, 'attempt_id': attempt_id}
        result.update(outcome='timed_out', summary='等待镜像标签占用超时')
        return record_finish(action, checkpoint, deadline, result)

    publisher = Path(__file__).with_name('backend_deploy.py')
    failed_services = []
    for service, approved in sorted(requests.items()):
        key = service_key(service, approved)
        journal = root / 'services' / key / 'receipt.json'
        journal.parent.mkdir(parents=True, exist_ok=True)
        request = {**approved, 'receipt_path': str(journal), 'expected_image_tag': tags[service]}
        request_path = checkpoint.parent / f'{key}.request.json'
        write_json(request_path, request)
        command = [sys.executable, '-B', str(publisher), '--request', str(request_path)]
        proof = run_stage(attempt_id, 'publish-' + key, deadline, project_root,
                          root / 'attempts', None, command, recoverable=True)
        if not proof['stop_confirmed']:
            return {'status': 'deployment_busy', 'attempt_id': attempt_id,
                    'receipt': proof.get('receipt_path')}
        if not register_service(result, service, approved, journal, proof):
            failed_services.append(service)
            if proof['outcome'] == 'timed_out':
                break
    if failed_services:
        result['summary'] = '后端部署未通过：' + '、'.join(failed_services)
    return record_finish(action, checkpoint, deadline, result)
