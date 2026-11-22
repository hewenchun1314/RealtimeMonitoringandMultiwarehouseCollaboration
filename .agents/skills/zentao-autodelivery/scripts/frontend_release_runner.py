"""Start or reconcile a controlled frontend publication attempt."""

from datetime import datetime
from pathlib import Path
import json
import sys

from backend_deploy_runner import deployment_action, record_finish, recover_stage
from frontend_release import tag_for
from stage_runner import run_stage, write_json


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}


def run_frontend_release(marker, database, config_path, project_root, *, recover=False):
    def action(name, **fields):
        return deployment_action(name, marker, database, config_path, **fields)

    claim = action('claim')
    status = claim['status']
    if status not in ('deployment_claimed', 'deployment_busy') or 'attempt_id' not in claim:
        return claim
    attempt_id = claim['attempt_id']
    deadline = datetime.fromisoformat(claim['deadline_at'])
    root = database.parent / 'items' / marker / 'frontend-releases'
    attempts = root / 'attempts'
    checkpoint = attempts / attempt_id / 'finish.json'
    request_path = checkpoint.with_name('request.json')
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    phase = 'publish-frontend'

    def finish(result):
        return record_finish(action, checkpoint, deadline, result, timeout_summary='前端镜像发布已到执行截止时间')

    if status == 'deployment_busy':
        if not recover:
            return claim
        saved = read_json(checkpoint)
        if saved:
            if saved.get('attempt_id') != attempt_id or not saved.get('stop_confirmed'):
                return claim
            return finish(saved)
        request = read_json(request_path)
        proof = recover_stage(checkpoint.parent / phase, attempt_id, phase)
    elif claim.get('blocked'):
        return finish({
            'attempt_id': attempt_id, 'outcome': 'failed', 'summary': claim['blocked'],
            'retryable': False, 'evidence': {}, 'stop_confirmed': True,
            'stop_evidence': {'mechanism': 'no_launch_records'},
        })
    else:
        request = claim['frontend_request']
        write_json(request_path, request)
        journal = root / (tag_for(request).rsplit(':', 1)[1] + '.json')
        publisher = Path(__file__).with_name('frontend_release.py')
        proof = run_stage(attempt_id, phase, deadline, project_root, attempts, None,
                          [sys.executable, '-B', str(publisher), '--request', str(request_path),
                           '--journal', str(journal)], recoverable=True)

    if (not proof or proof.get('attempt_id') != attempt_id or proof.get('phase') != phase
            or not proof.get('stop_confirmed') or not proof.get('stop_evidence')):
        return {'status': 'deployment_busy', 'attempt_id': attempt_id}
    evidence = read_json(root / (tag_for(request).rsplit(':', 1)[1] + '.json')) if request else {}
    outcome = proof['outcome']
    if outcome == 'success' and evidence.get('status') != 'published':
        outcome = 'failed'
    summary = (f'前端镜像已推送 Harbor {evidence["image"]}（{evidence["digest"]}）'
               if outcome == 'success' else '前端镜像发布未通过，详见本单发布日志')
    return finish({
        'attempt_id': attempt_id, 'outcome': outcome, 'summary': summary, 'evidence': evidence,
        'stop_confirmed': True, 'stop_evidence': proof['stop_evidence'],
    })
