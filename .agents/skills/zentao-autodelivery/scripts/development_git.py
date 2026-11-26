"""Ledger-bound fixed host Git requests; public entry points never mutate Git."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess

from backend_dependencies import backend_services, writable_dependencies
from branch_check import branch_context, git, is_repo_root, planned_applications, source_workspace, valid_target
from execution import active_attempt, execution_matches
from ledger import Ledger, encode, item_identity, message, require_text, transaction
from route_execution import get_split, repository_plan, snapshot, stage_evidence, task_scope

ACTIONS = {'branch_prepare': 'prepare', 'candidate_commit': 'commit', 'integration_merge': 'integrate'}
COMMIT = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')


def is_main_frontend_repair(item, phase, route):
    return bool(item['stage'] == 'frontend.e2e' and phase == 'frontend.coding'
                and route and route.get('post_merge_e2e'))


def overlapping_paths(selected, dirty):
    def parts(value):
        return PurePosixPath(os.path.normcase(value).replace('\\', '/')).parts

    reserved = [parts(path) for path in dirty]
    for path in selected:
        current = parts(path)
        if any(current[:len(other)] == other or other[:len(current)] == current for other in reserved):
            return True
    return False


def active_context(store, data, directory, kind):
    item = store.find_item(data.get('item'))
    trial = active_attempt(store, item)
    if (item['state'] != 'running' or not execution_matches(store, item, data) or not trial
            or trial['attempt_id'] != data.get('attempt_id') or store.time >= trial['deadline_at']
            or data.get('phase', trial['phase']) != trial['phase']):
        raise ValueError('The host Git request no longer belongs to an active attempt and phase')
    stage = item['stage'] if trial['phase'] == 'verify' else trial['phase']
    if stage == 'repair_coding':
        stage = 'coding'
    name = stage.rsplit('.', 1)[-1]
    expected = {'prepare': 'branch_check', 'commit': 'coding', 'integrate': 'integration'}[kind]
    if name != expected:
        raise ValueError('This host Git action is not allowed in the current coding/branch/integration phase')
    side = stage.split('.')[0] if '.' in stage else 'frontend'
    scoped = '.' in stage
    admitted = snapshot(item)
    frontend_route = get_split(store, item) if scoped and side == 'frontend' else None
    main_repair = kind == 'commit' and is_main_frontend_repair(item, trial['phase'], frontend_route)
    main_integration = (kind == 'integrate' and frontend_route and frontend_route.get('post_merge_e2e'))
    merged = {}
    if main_repair or main_integration:
        from frontend_delivery import owns_slot

        if not owns_slot(store, item, directory):
            raise ValueError('The main frontend Git operation requires the current delivery lock')
    if main_repair:
        merged = (stage_evidence(store, item, 'frontend.integration') or {}).get('merged_versions', {})
    plan = repository_plan(store, item, side) if scoped else stage_evidence(store, item, 'plan')
    keys = planned_applications(plan, side)
    if not keys:
        raise ValueError('Host Git requires confirmed repositories for this side')
    if kind == 'integrate' and scoped:
        scope = task_scope(store, item, stage)
        if scope and scope['blocked']:
            if not scope['active']:
                raise ValueError('No tasks are eligible for integration')
            withheld = {key for task, repositories in plan.get('task_repositories', {}).items()
                        if task in scope['blocked'] for key in repositories}
            keys = [key for key in keys if key not in withheld]
            if not keys:
                raise ValueError('The planned repositories still contain blocked tasks')
    synthetic = dict(item)
    synthetic['stage'] = side + '.branch_check' if scoped else 'branch_check'
    context = branch_context(store, synthetic)
    targets = context['branch_targets']
    current_targets = (backend_services(store.config) if side == 'backend' else
                       store.config['development'].get('frontend_bundle', {}).get('applications', {}))
    branches = (stage_evidence(store, item, side + '.branch_check' if scoped else 'branch_check') or {}).get('repositories', {})
    coding = stage_evidence(store, item, side + '.coding' if scoped else 'coding') or {}
    sources = {}
    for key in keys:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', key):
            raise ValueError('The configured plan key must be a safe repository basename')
        dependency = admitted.get('backend_dependencies', {}).get(key) if side == 'backend' else None
        registries = [cfg.get('runtime', {}) for cfg in (admitted, store.config)]
        if (not dependency and any('worker_git_write_repositories' in runtime for runtime in registries)
                and any(side + ':' + key not in runtime.get('worker_git_write_repositories', []) for runtime in registries)):
            raise ValueError('The current and admitted Git write registry no longer authorize this repository')
        target = targets.get(key)
        current = current_targets.get(key)
        if not valid_target(target) or not valid_target(current) or current['branch'] != target['branch']:
            raise ValueError('The configured repository or target branch is missing or changed')
        source = (source_workspace(context, directory, key) / target['repository']).resolve()
        if dependency:
            live = writable_dependencies(store.config).get(key)
            if dependency.get('access') != 'writable' or not live:
                raise ValueError('The dependency is not authorized for writes')
            live_workspace = (directory / live['source_path']).resolve()
        else:
            configured = store.config.get('workspaces', {}).get(side)
            if not configured:
                raise ValueError('The current side workspace is unavailable')
            live_workspace = (directory / configured).resolve()
        live_source = (live_workspace / current['repository']).resolve()
        if (source != live_source or not source.is_relative_to(source_workspace(context, directory, key))
                or not live_source.is_relative_to(live_workspace)):
            raise ValueError('The admitted and current repository locations differ')
        sources[key] = {'source_root': str(source), 'target_branch': target['branch'],
                        'worktree_branch': context['worktree_branch']}
        if kind != 'prepare':
            proof = branches.get(key)
            branch_pattern = 'zentao/' + re.escape(item['marker'].lower()) + r'-([1-9][0-9]*)' + (re.escape('-' + side) if scoped else '')
            owned_branch = re.fullmatch(branch_pattern, str(proof.get('worktree_branch', ''))) if isinstance(proof, dict) else None
            if (not isinstance(proof, dict) or proof.get('source_root') != str(source)
                    or proof.get('target_branch') != target['branch'] or not owned_branch
                    or int(owned_branch.group(1)) > item['run_round']):
                raise ValueError('The repository lacks successful item-owned branch/worktree evidence')
            # Recovery can retain a successful prior-round branch check. Its
            # registered ownership proof, rather than the new round number,
            # identifies the checkout that already holds the tested candidate.
            sources[key].update(proof)
        if main_repair:
            sha = merged.get(key)
            if not isinstance(sha, str) or not COMMIT.fullmatch(sha):
                raise ValueError('The main frontend repair requires a verified merged SHA per repository')
            sources[key].update(main_repair=True, worktree=str(source), worktree_branch=target['branch'], base_commit=sha)
        if kind == 'integrate':
            sha = coding.get('versions', {}).get(key)
            if not isinstance(sha, str) or not COMMIT.fullmatch(sha):
                raise ValueError('Integration requires a recorded coding candidate for every repository')
            sources[key]['candidate'] = sha
            if main_integration:
                sources[key].update(main_delivery=True, verify_after_merge='frontend.e2e' in frontend_route['stages'])
    return {'item': item_identity(item), 'item_seq': item['seq'], 'marker': item['marker'],
            'request_id': item['request_id'], 'chat_id': item['chat_id'], 'attempt_id': trial['attempt_id'],
            'phase': trial['phase'], 'phase_started_at': trial['phase_started_at'],
            'deadline_at': trial['deadline_at'], 'kind': kind, 'side': side,
            'repositories': sources}, item, trial


def main_repair_baseline(store, item_seq, key, proof):
    row = store.conn.execute('''SELECT dirty FROM frontend_main_repair_baselines
        WHERE item_seq=? AND repository=? AND target_ref=? AND base_commit=?''',
        (item_seq, proof['source_root'], proof['target_branch'], proof['base_commit'])).fetchone()
    if row is None:
        raise ValueError('Capture the main frontend repair baseline before editing ' + key)
    return json.loads(row['dirty'])


def dirty_paths(root):
    result = subprocess.run(['git', '-C', str(root), '--no-optional-locks', 'status', '--porcelain=v1',
                             '-z', '--no-renames', '--untracked-files=all', '--ignore-submodules=untracked'],
                            capture_output=True, text=True, encoding='utf-8', timeout=5, check=True)
    paths = [line[3:] for line in result.stdout.split('\0') if line]
    if any(line[:2] in ('DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU') for line in result.stdout.split('\0')):
        raise ValueError('The main frontend checkout has unresolved Git conflicts')
    return sorted(paths)


def check_main_ref(proof, sha):
    root = Path(proof['source_root'])
    target = 'refs/heads/' + proof['target_branch']
    if (not is_repo_root(root) or git(root, 'symbolic-ref', '--quiet', 'HEAD', allowed=(0, 1)) != target
            or git(root, 'rev-parse', '--verify', target) != sha):
        raise ValueError('The main frontend checkout is no longer at the exact target branch and revision')
    return root


def prepare_main_repair(store, item, trial, config_directory, previous_versions):
    """Record pre-existing edits once for a merged baseline, including retries."""
    binding = {key: item[key] for key in ('request_id', 'chat_id')}
    data = {**binding, 'item': item_identity(item), 'attempt_id': trial['attempt_id'], 'phase': trial['phase']}
    context, _, _ = active_context(store, data, config_directory, 'commit')
    proofs = context['repositories']
    if set(proofs) != set(previous_versions) or any(
            not proof.get('main_repair') or proof['base_commit'] != previous_versions[key]
            for key, proof in proofs.items()):
        raise ValueError('The main repair does not match the effective merged repository versions')
    store.conn.execute('''CREATE TABLE IF NOT EXISTS frontend_main_repair_baselines (
        item_seq INTEGER NOT NULL REFERENCES work_items(seq), repository TEXT NOT NULL,
        target_ref TEXT NOT NULL, base_commit TEXT NOT NULL, dirty TEXT NOT NULL,
        PRIMARY KEY (item_seq, repository, target_ref, base_commit))''')
    repos = {}
    for key, proof in proofs.items():
        root = proof['source_root']
        ref = proof['target_branch']
        sha = proof['base_commit']
        row = store.conn.execute('''SELECT dirty FROM frontend_main_repair_baselines
            WHERE item_seq=? AND repository=? AND target_ref=? AND base_commit=?''',
            (item['seq'], root, ref, sha)).fetchone()
        if row is None:
            try:
                dirty = dirty_paths(check_main_ref(proof, sha))
            except subprocess.SubprocessError as error:
                raise ValueError('Read the main frontend checkout status before starting repair') from error
            store.conn.execute('INSERT INTO frontend_main_repair_baselines VALUES (?,?,?,?,?)',
                               (item['seq'], root, ref, sha, encode(dirty)))
        else:
            dirty = json.loads(row['dirty'])
        repos[key] = {'worktree': root, 'target_branch': ref, 'expected_head': sha, 'protected_files': dirty}
    return {'repositories': repos}


def repair_commit_error(store, item, versions, previous_versions):
    """Require a bound host commit receipt and an unchanged target ref per app."""
    if not isinstance(versions, dict) or versions.keys() != previous_versions.keys():
        return 'The main repair must declare all per-application target revisions'
    branches = (stage_evidence(store, item, 'frontend.branch_check') or {}).get('repositories', {})
    rows = store.conn.execute('''SELECT g.*, a.run_round FROM development_git_operations g
        JOIN attempts a ON a.attempt_id=g.attempt_id
        WHERE g.item_seq=? AND g.kind='commit' AND a.stage='frontend.e2e' AND a.run_round=? AND g.result IS NOT NULL''',
        (item['seq'], item['run_round'])).fetchall()
    evidence = {}
    for row in rows:
        request = json.loads(row['request'])['context']
        result = json.loads(row['result']).get('repositories', {})
        for key, receipt in result.items():
            proof = request['repositories'][key]
            if (proof.get('main_repair') is True and proof['base_commit'] == previous_versions.get(key)
                    and receipt.get('status') == 'verified'):
                evidence[key] = receipt.get('candidate')
    for key, sha in versions.items():
        if not isinstance(sha, str) or not COMMIT.fullmatch(sha):
            return 'The main repairs require exact Git commit revisions'
        if sha != previous_versions[key] and evidence.get(key) != sha:
            return f'Main frontend repair for {key} requires a verified host Git commit receipt'
        try:
            check_main_ref(branches[key], sha)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            return str(error)
    if versions == previous_versions:
        return 'A successful main frontend repair must update at least one target revision'
    return None


def relative_files(root, files):
    if not isinstance(files, list) or len(files) != len(set(files)):
        raise ValueError('Commit files must be a unique literal relative file list')
    fingerprints = {}
    for value in files:
        if not isinstance(value, str) or not value or '\x00' in value:
            raise ValueError('Commit files must be literal relative paths')
        normalized = value.replace('\\', '/')
        path = PurePosixPath(normalized)
        if (path.is_absolute() or ':' in normalized or any(part in ('..', '.git') for part in path.parts)
                or any(part.lower() == '.git' for part in path.parts)):
            raise ValueError('Commit files cannot escape the item worktree or include Git metadata')
        actual = (root / normalized).resolve()
        if not actual.is_relative_to(root) or actual.is_dir():
            raise ValueError('Commit files must be files inside the item worktree')
        fingerprints[normalized] = hashlib.sha256(actual.read_bytes()).hexdigest() if actual.exists() else None
    return fingerprints


def operation_history(conn, database, context, exclude=None):
    """Keep effect obligations even when a prior attempt could not publish.

    This is diagnostic/check continuity only. Old results never authorize a new
    attempt; the current operation still gets fresh binding and stop validation.
    """
    history = []
    for row in conn.execute('SELECT * FROM development_git_operations WHERE item_seq=? AND kind=? ORDER BY created_at,rowid',
                            (context['item_seq'], context['kind'])):
        if row['operation_id'] == exclude:
            continue
        saved_request = json.loads(row['request'])
        previous = saved_request['context']
        if previous['marker'] != context['marker']:
            continue
        repositories = {}
        published = json.loads(row['result']).get('repositories', {}) if row['result'] else {}
        for key, proof in context['repositories'].items():
            old = previous['repositories'].get(key, {})
            if any(old.get(field) != proof.get(field) for field in ('source_root', 'worktree_branch', 'target_branch')):
                continue
            journal = database.parent / 'items' / context['marker'] / 'development-git' / row['operation_id'] / 'host-git' / 'effects' / key
            saved = journal / 'result.json'
            value = published.get(key)
            if value is None and saved.is_file():
                value = json.loads(saved.read_text(encoding='utf-8'))
            effect = journal / {'prepare': 'prepare.json', 'commit': 'commit.json', 'integrate': 'integration.json'}[context['kind']]
            if value is None and effect.is_file():
                value = {'status': {'prepare': 'failed', 'commit': 'candidate_retest_required',
                                    'integrate': 'integration_retest_required'}[context['kind']], 'uncertain_candidate': True}
                reports = journal / 'hook-results'
                failed = [p for p in reports.glob('*.args') if not p.with_suffix('.exit').exists()
                          or p.with_suffix('.exit').read_text().strip() != '0']
                value['failed_hooks'] = [p.stem for p in failed]
                value['failed_hook_args'] = {p.stem: p.read_bytes().decode('utf-8').rstrip('\0').split('\0') for p in failed}
            if value is not None:
                if old.get('main_repair') and context['kind'] == 'commit':
                    entry = saved_request['payload'][key]
                    value = {**value, 'repair_files': entry.get('confirmed_files', entry['files'])}
                repositories[key] = value
        history.append({'repositories': repositories})
    return history


def apply_obligations(request, history):
    context, payload = request['context'], request['payload']
    latest = {key: next((h['repositories'][key] for h in reversed(history) if key in h['repositories']), {})
              for key in context['repositories']}
    if context['kind'] == 'prepare':
        request['payload'] = latest
    elif context['kind'] == 'commit':
        for key, entry in payload.items():
            previous = next((h['repositories'][key] for h in reversed(history) if key in h['repositories']
                             and (h['repositories'][key].get('candidate') == entry['expected_head']
                                  or h['repositories'][key].get('uncertain_candidate'))), {})
            entry.update(needs_retest=previous.get('status') == 'candidate_retest_required',
                         failed_hooks=previous.get('failed_hooks', []), failed_hook_args=previous.get('failed_hook_args', {}))
    else:
        payload.update(needs_retest=[key for key, value in latest.items() if value.get('status') == 'integration_retest_required'],
                       failed_hooks={key: value['failed_hooks'] for key, value in latest.items() if value.get('failed_hooks')},
                       failed_hook_args={key: value.get('failed_hook_args', {}) for key, value in latest.items()})
    return request


def request_host_git(database, config, data, now, directory):
    kind = ACTIONS[data['action']]
    with transaction(database) as conn:
        store = Ledger(conn, config, now or datetime.now(timezone.utc))
        try:
            context, item, _ = active_context(store, data, directory, kind)
            event = require_text(data.get('event_id'), 'event_id')
            selected = data.get('service', data.get('application'))
            if selected is not None and selected not in context['repositories']:
                raise ValueError('Only repositories in the successful plan can be selected')
            operation = hashlib.sha256(encode([context['attempt_id'], context['phase'], kind, event]).encode()).hexdigest()
            submission = {key: data.get(key) for key in ('commits', 'application', 'service', 'verification')}
            old = conn.execute('SELECT * FROM development_git_operations WHERE operation_id=?', (operation,)).fetchone()
            if old:
                previous = validate_operation(store, old, directory)
                if previous.get('submission') != submission:
                    raise ValueError('A stable host Git event cannot be reused with different submitted data')
                return json.loads(old['result']) if old['result'] else message('host_git_worker_required', operation_id=operation)
            history = operation_history(conn, database, context)
            payload = {}
            if kind == 'prepare':
                payload = {key: next((h.get('repositories', {}).get(key, {}) for h in reversed(history)
                                     if key in h.get('repositories', {})), {}) for key in context['repositories']}
            if kind == 'commit':
                commits = data.get('commits')
                if not isinstance(commits, dict) or not commits or not commits.keys() <= context['repositories'].keys():
                    raise ValueError('Provide literal commit data for registered item worktrees')
                for key, entry in commits.items():
                    if not isinstance(entry, dict) or not COMMIT.fullmatch(str(entry.get('expected_head', ''))):
                        raise ValueError('Commit requires the expected item worktree HEAD')
                    message_text = require_text(entry.get('message'), 'message')
                    if '\x00' in message_text or len(message_text) > 10000:
                        raise ValueError('Invalid commit message')
                    proof = context['repositories'][key]
                    root = Path(proof['worktree']).resolve()
                    files = relative_files(root, entry.get('files'))
                    if proof.get('main_repair'):
                        normalized = {PurePosixPath(path).as_posix(): digest for path, digest in files.items()}
                        if len(normalized) != len(files) or len({os.path.normcase(path) for path in normalized}) != len(files):
                            raise ValueError('Repair paths must resolve to unique literal files')
                        files = normalized
                    payload[key] = {'expected_head': entry['expected_head'], 'message': message_text,
                                    'files': list(files), 'fingerprints': files,
                                    'verification': entry.get('verification', '')}
                    if proof.get('main_repair'):
                        baseline = main_repair_baseline(store, item['seq'], key, proof)
                        if overlapping_paths(files, baseline):
                            raise ValueError('Requested repair files overlap pre-existing main checkout changes')
                        if entry['expected_head'] != proof['base_commit']:
                            previous_repair = next((h['repositories'][key] for h in reversed(history)
                                                   if key in h['repositories']
                                                   and h['repositories'][key].get('candidate') == entry['expected_head']
                                                   and h['repositories'][key].get('status') == 'candidate_retest_required'), None)
                            if files or not previous_repair or not str(entry.get('verification', '')).strip():
                                raise ValueError('Main repair requires the merged base or a known host commit awaiting verification')
                            payload[key]['confirmed_files'] = previous_repair['repair_files']
                        payload[key]['baseline_dirty'] = baseline
                context['repositories'] = {key: context['repositories'][key] for key in commits}
            if kind == 'integrate':
                payload = {'verification': data.get('verification', '')}
            request = {'context': context, 'payload': payload, 'submission': submission}
            apply_obligations(request, history)
            # One outstanding request per phase keeps batches and their stop
            # evidence ordered; a failed completed operation can be revised.
            pending = conn.execute('SELECT operation_id FROM development_git_operations WHERE item_seq=? AND attempt_id=? AND phase=? AND result IS NULL',
                                   (item['seq'], context['attempt_id'], context['phase'])).fetchone()
            if pending:
                return message('host_git_worker_required', operation_id=pending['operation_id'])
            conn.execute('INSERT INTO development_git_operations(operation_id,item_seq,attempt_id,phase,kind,request,created_at) VALUES (?,?,?,?,?,?,?)',
                         (operation, item['seq'], context['attempt_id'], context['phase'], kind, encode(request), store.time))
            return message('host_git_worker_required', operation_id=operation)
        except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as error:
            return message('host_git_rejected', reason=str(error))


def validate_operation(store, row, directory):
    request = json.loads(row['request'])
    recorded = request['context']
    current, _, _ = active_context(store, recorded, directory, row['kind'])
    if row['kind'] == 'commit':
        current['repositories'] = {k: current['repositories'][k] for k in recorded['repositories']}
    if current != recorded:
        raise ValueError('The host Git authorization changed since the request')
    return request


def host_git_status(database, config, data, now, directory):
    with transaction(database) as conn:
        store = Ledger(conn, config, now or datetime.now(timezone.utc))
        row = conn.execute('SELECT * FROM development_git_operations WHERE operation_id=?',
                           (data.get('operation_id'),)).fetchone()
        try:
            if row is None:
                raise ValueError('Unknown host Git operation')
            current, _, _ = active_context(store, data, directory, row['kind'])
            request = validate_operation(store, row, directory)
            if current['item_seq'] != row['item_seq'] or any(current[k] != request['context'][k] for k in
                    ('request_id', 'chat_id', 'attempt_id', 'phase', 'phase_started_at')):
                raise ValueError('The host Git result belongs to a different binding')
            return json.loads(row['result']) if row['result'] else message('host_git_worker_required', operation_id=row['operation_id'])
        except (KeyError, TypeError, ValueError, OSError) as error:
            return message('host_git_rejected', reason=str(error))
