"""Push and verify the admitted merge commits in a controlled worker process."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

from backend_deploy_runner import deployment_action, record_finish, recover_stage
from branch_check import is_repo_root
from frontend_release import DeploymentError, synchronize_branch
from stage_runner import run_stage, write_json


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}


def push_repositories(request, journal):
    evidence = {'repositories': {}, 'errors': dict(request['errors'])}
    write_json(journal, evidence)
    for key, target in request['repositories'].items():
        root = Path(target['repository'])
        try:
            if root.resolve() != root or not is_repo_root(root):
                raise DeploymentError('配置路径已不再指向已核验的 Git 仓库')
            remote = synchronize_branch(root, target['branch'], target['sha'])
            evidence['repositories'][key] = {'branch': target['branch'], 'sha': target['sha'],
                                             'remote_sha': remote}
        except (DeploymentError, OSError, ValueError, subprocess.TimeoutExpired) as error:
            evidence['errors'][key] = str(error)
        write_json(journal, evidence)
    return evidence


def run_git_delivery(marker, database, config_path, project_root, *, recover=False):
    def action(name, **fields):
        return deployment_action(name, marker, database, config_path, **fields)

    claim = action('claim')
    status = claim['status']
    if status not in ('deployment_claimed', 'deployment_busy') or 'attempt_id' not in claim:
        return claim
    attempt_id = claim['attempt_id']
    deadline = datetime.fromisoformat(claim['deadline_at'])
    attempts = database.parent / 'items' / marker / 'git-delivery' / 'attempts'
    folder = attempts / attempt_id
    folder.mkdir(parents=True, exist_ok=True)
    # A repair can revisit the same attempt ID in other controlled phases.
    phase = claim['phase'].replace('.', '-') if claim['phase'] != 'verify' else 'push-git'
    checkpoint = folder / (phase + '-finish.json')
    request = folder / (phase + '-request.json')
    journal = folder / (phase + '-receipts.json')

    def finish(result):
        return record_finish(action, checkpoint, deadline, result, timeout_summary='Git 推送达到执行截止时间')

    if status == 'deployment_busy':
        if not recover:
            return claim
        saved = read_json(checkpoint)
        if saved:
            if saved.get('attempt_id') != attempt_id or not saved.get('stop_confirmed'):
                return claim
            return finish(saved)
        proof = recover_stage(folder / phase, attempt_id, phase)
    elif claim.get('blocked') or not claim['push_requests']:
        errors = claim['errors']
        summary = claim.get('blocked') or '；'.join(f'{key}：{reason}' for key, reason in sorted(errors.items()))
        return finish({
            'attempt_id': attempt_id, 'outcome': 'skipped', 'summary': summary or '没有可推送的仓库',
            'retryable': False, 'evidence': {'repositories': {}, 'errors': errors}, 'stop_confirmed': True,
            'stop_evidence': {'mechanism': 'no_launch_records'},
        })
    else:
        write_json(request, {'repositories': claim['push_requests'], 'errors': claim['errors']})
        proof = run_stage(attempt_id, phase, deadline, project_root, attempts, None,
                          [sys.executable, '-B', __file__, str(request), str(journal)], recoverable=True)

    if (not proof or proof.get('attempt_id') != attempt_id or proof.get('phase') != phase
            or not proof.get('stop_confirmed') or not proof.get('stop_evidence')):
        return {'status': 'deployment_busy', 'attempt_id': attempt_id}
    evidence = read_json(journal) or {'repositories': {}, 'errors': {}}
    outcome = proof['outcome']
    if outcome == 'success' and (evidence['errors'] or not evidence['repositories']):
        outcome = 'failed'
    refused = read_json(request).get('errors', {})
    retryable = outcome != 'failed' or not evidence['errors'] or bool(evidence['errors'].keys() - refused.keys())
    verified = '、'.join(sorted(evidence['repositories'])) or '无'
    errors = '；'.join(f'{key}：{reason}' for key, reason in sorted(evidence['errors'].items()))
    return finish({
        'attempt_id': attempt_id, 'outcome': outcome, 'evidence': evidence,
        'retryable': retryable,
        'summary': f'Git 远端已核验：{verified}' + (f'。推送受阻：{errors}' if errors else ''),
        'stop_confirmed': True, 'stop_evidence': proof['stop_evidence'],
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('request', type=Path)
    parser.add_argument('journal', type=Path)
    args = parser.parse_args()
    os.environ.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
    push_repositories(read_json(args.request), args.journal)


if __name__ == '__main__':
    main()
