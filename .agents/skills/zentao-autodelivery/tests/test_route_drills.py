"""Run complete synthetic work items using the production ledger, local Git and loopback HTTP."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid4

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / 'scripts'))
from status_board import publish_status
from workflow import CONFIG_PATH, run


def git(repository, *args):
    return subprocess.run(['git', '-C', str(repository), *args], check=True, text=True,
                          capture_output=True, timeout=15).stdout.strip()


class QuietFiles(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        super().end_headers()

    def log_message(self, *_):
        pass


class RouteDrills(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.database = self.root / 'ledger' / 'workflow.sqlite3'
        self.now = datetime.now(timezone.utc)
        self.config = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
        self.config.pop('backend_dependencies', None)
        self.config.get('e2e', {}).pop('enabled', None)
        self.config.get('e2e', {}).pop('agentlimb', None)
        self.config.get('e2e', {}).pop('after_integration', None)
        # These drills end at integration. The frontend publisher has its own worker tests.
        self.config['development']['frontend_bundle'].pop('deployment', None)
        self.config['development']['allowed_requirement_types'] = ['frontend_only']
        self.config['zentao'].update(base_url='http://127.0.0.1:1', assigned_to_accounts=['drill'])
        self.config['zentao'].pop('scope_mode', None)
        self.config['zentao']['accepted_statuses'] = {'story': ['active'], 'bug': ['active']}
        self.config['runtime'] = {'desktop_project_id': 'drill'}
        self.config['workspaces'] = {'requirement': 'requirements'}
        self.item = {'instance': 'http://127.0.0.1:1', 'project_id': '1', 'kind': 'story', 'id': '17'}
        self.binding = {'chat_id': 'local-route-drill'}
        self.repositories = {}
        self.worktrees = {}
        self.versions = {}
        self.merged = {}
        self.calls = []
        self.api = None
        self.key = 'aggregation-platform'

    def configure_side(self, side):
        workspace = self.root / side
        workspace.mkdir()
        self.config['workspaces'][side] = side
        development = self.config['development']
        if side == 'frontend':
            target = development['frontend_bundle']['applications'][self.key]
        else:
            target = {'repository': '.', 'branch': 'main', 'build_command': 'python -m json.tool status.json',
                      'test_command': 'python -m json.tool status.json', 'start_command': 'python -m http.server',
                      'acceptance_command': 'GET /status.json'}
            development['backend_bundle'] = {'name': 'drill-api', 'services': {self.key: target}}
        git(workspace, 'init', '-b', target['branch'])
        git(workspace, 'config', 'user.name', 'Local route drill')
        git(workspace, 'config', 'user.email', 'local-drill@example.invalid')
        git(workspace, 'commit', '--allow-empty', '-m', 'initial')
        self.repositories[side] = workspace

    def call(self, command, data):
        self.now += timedelta(milliseconds=1)
        return run(command, data, self.config, self.database, now=self.now, config_directory=self.root)

    def record(self, action, **fields):
        return self.call('record', {'action': action, 'event_id': uuid4().hex, 'item': self.item,
                                    **self.binding, **fields})

    def admit(self):
        scan = {'action': 'scan', 'complete': True, 'instance': self.item['instance'], 'project_id': '1',
                'project_name': self.config['zentao']['project_name']}
        self.assertEqual(self.call('claim', dict(scan, scan_id='1', items=[],
                                                 observed_at=self.now.isoformat()))['status'], 'baseline')
        entry = {'kind': self.item['kind'], 'id': '17', 'title': 'Show status', 'status': 'active', 'priority': 1,
                 'assigned_to': 'drill'}
        self.assertEqual(self.call('claim', dict(scan, scan_id='2', items=[entry],
                                                 observed_at=self.now.isoformat()))['candidate'], self.item)
        receipt = self.call('claim', {'action': 'reserve', 'item': self.item, 'observed_at': self.now.isoformat(),
                                      'assigned_to': 'drill', 'status': 'active', 'desktop_project_id': 'drill'})
        self.assertEqual(receipt['status'], 'reserved')
        self.binding['request_id'] = receipt['request_id']
        self.artifacts = self.database.parent / 'items' / receipt['marker'] / 'round-1'
        self.artifacts.mkdir(parents=True)
        self.assertEqual(self.record('bind', desktop_project_id='drill')['status'], 'bound')
        self.assertEqual(self.record('start')['stage'], 'material_check')

    def serve(self, checkout):
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietFiles, directory=str(checkout)))
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.03}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:' + str(server.server_port)

    def check_api(self, checkout):
        self.api = self.serve(checkout)
        with urlopen(self.api + '/status.json', timeout=3) as response:
            result = json.load(response)
        self.assertEqual(result, {'status': 'approved'})
        call = f'GET {self.api}/status.json = {result} @ {git(checkout, "rev-parse", "HEAD")}'
        self.calls.append(call)
        return call

    def check_page(self, checkout):
        url = self.serve(checkout) + '/'
        if self.api:
            url += '?' + urlencode({'api': self.api + '/status.json'})
        with urlopen(url, timeout=3) as response:
            self.assertEqual(response.read(), (checkout / 'index.html').read_bytes())
        verification = 'HTTP served candidate page'
        if browser := os.environ.get('ZENTAO_DRILL_BROWSER'):
            profile = self.root / 'drill-browser'
            page = subprocess.run([browser, '--headless=new', '--disable-gpu', '--disable-extensions',
                                   '--no-first-run', '--disable-background-networking',
                                   f'--user-data-dir={profile}', '--virtual-time-budget=4000', '--dump-dom', url],
                                  capture_output=True, timeout=35, check=True)
            self.assertIn(b'<output>approved</output>', page.stdout)
            verification = 'Headless browser DOM'
        self.calls.append(f'{verification} {url} @ {git(checkout, "rev-parse", "HEAD")}')
        return self.calls[-1]

    def split(self):
        tasks = {side: [{'id': side, 'description': 'Deliver approved status',
                         'acceptance': ['Status reads approved']}]
                 for side in self.repositories}
        if len(tasks) == 2:
            tasks['frontend'][0]['depends_on'] = ['backend']
        data = {'tasks': tasks}
        if len(tasks) == 2:
            data['interface_contract'] = 'GET /status.json -> {"status":"approved"}'
        (self.artifacts / 'split.md').write_text(json.dumps(data, indent=2), encoding='utf-8')
        return data

    def prepare(self, side, attempt, key=None):
        key = key or self.key
        identity = {'attempt_id': attempt['attempt_id']}
        field = 'service' if side == 'backend' else 'application'
        args = {field: key, **identity, 'desktop_reconciliation': {
            'status': 'unavailable', 'evidence': 'Isolated CLI test repository'}}
        args['event_id'] = 'prepare-' + side + '-' + key + '-' + attempt['attempt_id']
        prepared = self.record('branch_prepare', **args)
        self.assertEqual(prepared['status'], 'host_git_worker_required', prepared)
        from development_git_runner import run_development_git
        config_file = self.root / 'host-project.json'
        config_file.write_text(json.dumps(self.config), encoding='utf-8')
        with closing(sqlite3.connect(self.database)) as connection:
            marker = connection.execute('SELECT marker FROM work_items WHERE item_id=17').fetchone()[0]
        prepared = run_development_git(marker, self.database, config_file, self.root, recover=True)
        self.assertEqual(prepared['status'], 'host_git_completed', prepared)
        proof = prepared['repositories'][key]
        if key == self.key:
            self.worktrees[side] = Path(proof['worktree'])
        again = self.record('branch_prepare', **args)
        self.assertEqual(again['repositories'][key], proof)
        inspected = self.record('branch_inspect', worktrees={key: proof['worktree']}, **identity)
        self.assertEqual(inspected['repositories'][key]['status'], 'verified')
        return {'repositories': {key: inspected['repositories'][key]}}

    def change(self, side):
        checkout = self.worktrees[side]
        if side == 'backend':
            (checkout / 'status.json').write_text(json.dumps({'status': 'approved'}), encoding='utf-8')
        else:
            page = '<!doctype html><output>approved</output>'
            if self.api:
                page = '''<!doctype html><output>loading</output><script>
fetch(new URLSearchParams(location.search).get('api')).then(response => response.json())
  .then(data => document.querySelector('output').textContent = data.status);
</script>'''
            (checkout / 'index.html').write_text(page, encoding='utf-8')
        git(checkout, 'add', '.')
        git(checkout, 'commit', '-m', 'Implement status')
        self.versions[side] = {self.key: git(checkout, 'rev-parse', 'HEAD')}
        return {'versions': self.versions[side]}

    def evidence(self, stage, attempt):
        if stage == 'material_check':
            if self.item['kind'] == 'bug':
                return {'detail': 'The status page repeats its empty-state title.'}
            return {'path': 'requirements/17/intent.md'}
        if stage == 'split':
            return self.split()
        if stage == 'handoff':
            document = self.artifacts / 'backend' / 'handoff.md'
            call = self.check_api(self.repositories['backend'])
            document.write_text(call, encoding='utf-8')
            return {'document': str(document), 'backend_versions': self.merged['backend'],
                    'environment': self.api, 'verification': call}
        side, action = stage.split('.')
        side_dir = self.artifacts / side
        side_dir.mkdir(exist_ok=True)
        if action in ('design', 'plan'):
            document = side_dir / (action + '.md')
            document.write_text(f'{side} {action}: deliver approved status.', encoding='utf-8')
            field = 'services' if side == 'backend' else 'applications'
            return {'document': str(document), field: [self.key]}
        if action == 'branch_check':
            return self.prepare(side, attempt)
        if action == 'coding':
            return self.change(side)
        checkout = self.worktrees[side]
        versions = self.versions[side]
        if action == 'integration':
            target = self.repositories[side]
            git(target, 'merge', '--ff-only', versions[self.key])
            self.merged[side] = {self.key: git(target, 'rev-parse', 'HEAD')}
            return {'versions': versions, 'merged_versions': self.merged[side]}
        if action == 'code_review':
            diff = git(checkout, 'diff', '--check', 'HEAD^', 'HEAD')
            self.assertEqual(diff, '')
            verification = 'git diff --check HEAD^ HEAD'
        elif action == 'unit_test':
            if side == 'backend':
                output = subprocess.check_output([sys.executable, '-m', 'json.tool', 'status.json'],
                                                 text=True, cwd=checkout, timeout=10)
                self.assertEqual(json.loads(output)['status'], 'approved')
                verification = 'python -m json.tool status.json; verified status'
            else:
                self.assertIn('<output>', (checkout / 'index.html').read_text(encoding='utf-8'))
                verification = 'assert output element exists'
        elif action == 'e2e':
            verification = self.check_page(checkout)
        else:
            self.assertEqual(action, 'acceptance')
            verification = self.check_api(checkout)
        data = {'versions': versions, 'verification': verification}
        if action in ('e2e', 'acceptance'):
            data['verified_tasks'] = [side]
        if action == 'e2e' and 'backend' in self.merged:
            data['backend_versions'] = self.merged['backend']
        return data

    def begin_drill(self, scope, mark, extra_backend=None):
        if scope == 'backend_only':
            del self.config['development']['frontend_bundle']
            del self.config['e2e']
            self.config['development']['allowed_requirement_types'] = [scope]
        if scope == 'fullstack':
            self.config['development']['allowed_requirement_types'] = ['frontend_only', 'backend_only', scope]
        if scope != 'frontend_only':
            self.configure_side('backend')
            if extra_backend:
                services = self.config['development']['backend_bundle']['services']
                services[extra_backend] = {**services[self.key], 'repository': extra_backend}
                repository = self.repositories['backend'] / extra_backend
                repository.mkdir()
                git(repository, 'init', '-b', 'main')
                git(repository, 'config', 'user.email', 'local-drill@example.invalid')
                git(repository, 'config', 'user.name', 'Local route drill')
                git(repository, 'commit', '--allow-empty', '-m', 'Initial extra backend')
        if scope != 'backend_only':
            self.configure_side('frontend')
        intent = self.root / 'requirements' / 'requirements' / '17' / 'intent.md'
        intent.parent.mkdir(parents=True)
        intent.write_text('# Display status\n' + ('研发范围：' + mark if mark else ''), encoding='utf-8')
        self.admit()

    def finish(self, stage, attempt, evidence, **fields):
        return self.record('attempt_finish', attempt_id=attempt['attempt_id'], outcome='success',
                           summary=stage, evidence=evidence, **fields)

    def drill(self, scope, mark):
        self.begin_drill(scope, mark)
        completed = []
        stage = 'material_check'
        while stage:
            self.assertNotIn(stage, completed)
            attempt = self.record('attempt_start', stage=stage)
            self.assertEqual(attempt['status'], 'attempt_started', attempt)
            evidence = self.evidence(stage, attempt)
            if stage == 'handoff':
                # The backend is already merged. Waking the same chat must only
                # resume its active handoff and cannot start the frontend yet.
                self.assertEqual(self.record('start')['stage'], 'handoff')
                resumed = self.record('attempt_start', stage=stage)
                self.assertEqual(resumed['attempt_id'], attempt['attempt_id'])
                wrong = {**evidence, 'backend_versions': {self.key: '0' * 40}}
                self.assertEqual(self.finish(stage, attempt, wrong)['status'], 'evidence_required')
                absent = {**evidence, 'document': str(self.artifacts / 'absent.md')}
                self.assertEqual(self.finish(stage, attempt, absent)['status'], 'evidence_required')
                self.assertEqual(self.record('attempt_start', stage='frontend.design')['status'], 'stage_mismatch')
            result = self.finish(stage, attempt, evidence)
            self.assertEqual(result['status'], 'attempt_finished', (stage, result))
            completed.append(stage)
            stage = result['next_stage']
        expected = ['material_check', 'split']
        for side in self.repositories:
            acceptance = 'e2e' if side == 'frontend' else 'acceptance'
            expected.extend(side + '.' + s for s in ('design', 'plan', 'branch_check', 'coding', 'unit_test',
                                                      'code_review', acceptance, 'integration'))
            if side == 'backend' and scope == 'fullstack':
                expected.append('handoff')
        self.assertEqual(completed, expected)
        final = self.record('complete')
        self.assertEqual(final['conclusion'], '已交付', final)
        publish_status(self.database, self.config)
        self.assertIn('已交付', (self.database.parent / 'status.html').read_text(encoding='utf-8'))
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM attempts').fetchone()[0], len(expected))
            self.assertEqual(conn.execute('SELECT count(*) FROM writebacks').fetchone()[0], len(expected) + 1)
        if output := os.environ.get('ZENTAO_DRILL_REPORT_DIR'):
            report = {'scenario': self._testMethodName, 'conclusion': final['conclusion'], 'stages': completed,
                      'merged_versions': self.merged, 'calls': self.calls}
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / (self._testMethodName + '.json')).write_text(json.dumps(report, ensure_ascii=False,
                                                                                  indent=2), encoding='utf-8')

    def test_current_frontend_with_fullstack_mark(self):
        self.drill('frontend_only', '前后端')

    def test_current_frontend_without_mark(self):
        self.config['e2e']['enabled'] = True
        self.drill('frontend_only', '')

    def test_backend_without_frontend_settings(self):
        self.drill('backend_only', '纯前端')

    def test_fullstack_backend_then_frontend(self):
        self.drill('fullstack', '前后端')

    def check_unavailable_e2e_continues_to_release_checks(self):
        from development_git_runner import run_development_git
        from frontend_release_runner import run_frontend_release
        from git_delivery_runner import run_git_delivery
        from unittest.mock import patch

        self.config['runtime']['git_push_enabled'] = True
        project = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
        self.config['development']['frontend_bundle']['deployment'] = project['development']['frontend_bundle']['deployment']
        self.begin_drill('frontend_only', '纯前端')
        stage = 'material_check'
        while stage != 'frontend.e2e':
            attempt = self.record('attempt_start', stage=stage)
            result = self.finish(stage, attempt, self.evidence(stage, attempt))
            self.assertEqual(result['status'], 'attempt_finished', result)
            stage = result['next_stage']

        attempt = self.record('attempt_start', stage=stage)
        result = self.record('attempt_finish', attempt_id=attempt['attempt_id'], outcome='failed',
                             retryable=False, summary='Credentials unavailable',
                             evidence={'task_blockers': {'frontend': 'Credentials unavailable'}})
        self.assertEqual((result['disposition'], result['next_stage']), ('next', 'frontend.integration'))
        integration = self.record('attempt_start', stage='frontend.integration')
        self.assertEqual(integration['task_scope']['active'], ['frontend'])
        config_file = self.root / 'delivery-config.json'
        config_file.write_text(json.dumps(self.config), encoding='utf-8')
        marker = self.artifacts.parent.name
        merge = self.record('integration_merge', attempt_id=integration['attempt_id'])
        self.assertEqual(merge['status'], 'host_git_worker_required', merge)
        merged = run_development_git(marker, self.database, config_file, self.root, recover=True)
        self.assertEqual(merged['status'], 'host_git_completed', merged)
        self.assertEqual(merged['merged_versions'], self.versions['frontend'])
        integrated = self.finish('frontend.integration', integration, {
            'versions': self.versions['frontend'], 'merged_versions': merged['merged_versions'],
        })
        self.assertEqual(integrated['next_stage'], 'frontend.push', integrated)

        with patch('git_delivery_runner.run_stage') as push, patch('frontend_release_runner.run_stage') as publish:
            pushed = run_git_delivery(marker, self.database, config_file, self.root)
            self.assertEqual((pushed['outcome'], pushed['next_stage']), ('skipped', 'frontend.deploy'), pushed)
            released = run_frontend_release(marker, self.database, config_file, self.root)
            self.assertEqual((released['outcome'], released['disposition']), ('skipped', 'close'), released)
            push.assert_not_called()
            publish.assert_not_called()

        final = self.record('complete', blockers_path='blockers.md')
        self.assertEqual(final['conclusion'], '待验证')
        self.assertEqual([step['outcome'] for step in final['steps'][-4:]],
                         ['failed', 'success', 'skipped', 'skipped'])
        self.assertTrue(all(step.get('started_at') for step in final['steps'][-4:]))
        self.assertEqual(self.record('recover', recovery_id='credentials-restored', evidence='Login available')['stage'],
                         'frontend.e2e')

    def test_bug_continues_to_release_checks_after_e2e_failure(self):
        self.item['kind'] = 'bug'
        self.check_unavailable_e2e_continues_to_release_checks()

    def test_story_continues_to_release_checks_after_e2e_failure(self):
        self.check_unavailable_e2e_continues_to_release_checks()

    def test_independent_frontend_is_verified_and_merged_after_backend_blocker(self):
        self.begin_drill('fullstack', '前后端')
        stage = 'material_check'
        completed = []
        while stage:
            attempt = self.record('attempt_start', stage=stage)
            self.assertEqual(attempt['status'], 'attempt_started', attempt)
            if stage == 'backend.design':
                result = self.record('attempt_finish', attempt_id=attempt['attempt_id'], outcome='failed',
                                     retryable=False, summary='Missing upstream contract',
                                     evidence={'task_blockers': {'backend': 'Missing upstream contract'}})
            elif attempt.get('task_scope', {}).get('active') == []:
                result = self.record('attempt_finish', attempt_id=attempt['attempt_id'], outcome='skipped',
                                     summary='No eligible tasks for ' + stage)
            else:
                evidence = self.evidence(stage, attempt)
                if stage == 'split':
                    evidence['tasks']['frontend'][0]['id'] = 'dependent'
                    evidence['tasks']['frontend'].append({
                        'id': 'frontend', 'description': 'Show locally computed status',
                        'acceptance': ['Independent page renders'],
                    })
                    evidence['tasks']['frontend'].append({
                        'id': 'fixture', 'description': 'Validate a separate fixture',
                        'acceptance': ['Fixture can be exercised'],
                    })
                if stage == 'frontend.unit_test':
                    evidence['task_blockers'] = {'fixture': 'Required fixture is unavailable'}
                if stage == 'frontend.e2e':
                    self.assertEqual(attempt['task_scope']['active'], ['frontend'])
                    self.assertEqual(set(attempt['task_scope']['blocked']), {'backend', 'dependent', 'fixture'})
                result = self.finish(stage, attempt, evidence)
            self.assertEqual(result['status'], 'attempt_finished', result)
            completed.append(stage)
            stage = result['next_stage']
        self.assertEqual(completed[3:11], ['backend.plan', 'backend.branch_check', 'backend.coding',
                                          'backend.unit_test', 'backend.code_review', 'backend.acceptance',
                                          'backend.integration', 'handoff'])
        self.assertEqual(completed[-1], 'frontend.integration')
        closeout = self.record('complete', blockers_path=str(self.artifacts / 'blockers.md'))
        self.assertEqual(closeout['conclusion'], '待验证', closeout)
        self.assertEqual(set(closeout['blocked_tasks']), {'backend', 'dependent', 'fixture'})
        self.assertEqual(git(self.repositories['frontend'], 'rev-parse', 'HEAD'), self.merged['frontend'][self.key])

    def test_failed_backend_acceptance_allows_safe_merge_but_blocks_frontend(self):
        self.begin_drill('fullstack', '前后端')
        stage = 'material_check'
        while stage != 'handoff':
            attempt = self.record('attempt_start', stage=stage)
            self.assertEqual(attempt['status'], 'attempt_started', attempt)
            if stage == 'backend.acceptance':
                result = self.record('attempt_finish', attempt_id=attempt['attempt_id'], outcome='failed',
                                     summary='Service acceptance failed', retryable=False)
            else:
                result = self.finish(stage, attempt, self.evidence(stage, attempt))
            self.assertEqual(result['status'], 'attempt_finished', result)
            stage = result['next_stage']
        attempt = self.record('attempt_start', stage='handoff')
        self.assertEqual(self.finish(stage, attempt, self.evidence(stage, attempt))['status'], 'evidence_required')
        blocked = self.record('attempt_finish', attempt_id=attempt['attempt_id'], outcome='failed',
                              summary='The required service acceptance failed', retryable=False)
        self.assertEqual(blocked['next_stage'], 'frontend.design')
        stage = blocked['next_stage']
        skipped = []
        while stage:
            attempt = self.record('attempt_start', stage=stage)
            self.assertEqual(attempt['task_scope']['active'], [])
            receipt = self.record('attempt_finish', attempt_id=attempt['attempt_id'], outcome='skipped',
                                  summary='The required service acceptance failed')
            self.assertEqual(receipt['status'], 'attempt_finished', receipt)
            skipped.append(stage)
            stage = receipt['next_stage']
        final = self.record('complete', blockers_path='blockers.md')
        self.assertEqual(final['conclusion'], '待验证')
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual([row[0] for row in conn.execute(
                "SELECT stage FROM attempts WHERE stage LIKE 'frontend.%' AND outcome='skipped' ORDER BY rowid")],
                skipped)
        recovered = self.record('recover', recovery_id='new-run', evidence='Service endpoint repaired')
        self.assertEqual(recovered['stage'], 'backend.acceptance')

    def test_partial_backend_merge_recovers_only_the_remaining_repository(self):
        key = 'audit'
        self.begin_drill('backend_only', '纯后端', extra_backend=key)
        primary = self.repositories['backend']
        secondary = primary / key
        stage = 'material_check'
        while stage != 'backend.integration':
            attempt = self.record('attempt_start', stage=stage)
            evidence = self.evidence(stage, attempt)
            if stage == 'backend.plan':
                evidence['services'].append(key)
            elif stage == 'backend.branch_check':
                second_proof = self.prepare('backend', attempt, key)['repositories'][key]
                evidence['repositories'][key] = second_proof
                extra_tree = Path(second_proof['worktree'])
            elif stage == 'backend.coding':
                (extra_tree / 'audit.json').write_text('{"event":"approved"}', encoding='utf-8')
                git(extra_tree, 'add', 'audit.json')
                git(extra_tree, 'commit', '-m', 'Record approval audit')
                self.versions['backend'][key] = git(extra_tree, 'rev-parse', 'HEAD')
            elif stage in ('backend.unit_test', 'backend.acceptance'):
                url = self.serve(extra_tree)
                with urlopen(url + '/audit.json', timeout=3) as response:
                    self.assertEqual(json.load(response), {'event': 'approved'})
                evidence['verification'] += f'; GET {url}/audit.json confirmed approval event'
            elif stage == 'backend.code_review':
                self.assertEqual(git(extra_tree, 'diff', '--check', 'HEAD^', 'HEAD'), '')
            result = self.finish(stage, attempt, evidence)
            self.assertEqual(result['status'], 'attempt_finished', result)
            stage = result['next_stage']

        versions = self.versions['backend']
        first = self.record('attempt_start', stage=stage)
        git(primary, 'merge', '--ff-only', versions[self.key])
        merged_first = git(primary, 'rev-parse', 'HEAD')
        git(secondary, 'commit', '--allow-empty', '-m', 'Concurrent local commit')
        blocked_target = git(secondary, 'rev-parse', 'HEAD')
        with self.assertRaises(subprocess.CalledProcessError):
            git(secondary, 'merge', '--ff-only', versions[key])
        partial = {'versions': versions, 'merged_versions': {self.key: merged_first},
                   'blocked_repositories': {key: 'Diverged target requires review'}}
        for false_receipt in ({self.key: '0' * 40}, {key: blocked_target}, {'wrong-service': merged_first}):
            rejected = self.record('attempt_finish', attempt_id=first['attempt_id'], outcome='failed',
                                   retryable=False, summary='Invalid partial receipt',
                                   evidence={**partial, 'merged_versions': false_receipt})
            self.assertEqual(rejected['status'], 'evidence_required', rejected)
        outcome = self.record('attempt_finish', attempt_id=first['attempt_id'], outcome='failed', retryable=False,
                              summary='Only the primary service merged', evidence=partial)
        self.assertEqual(outcome['disposition'], 'close', outcome)
        final = self.record('complete', blockers_path='blockers.md')
        self.assertEqual(final['conclusion'], '待验证')
        self.assertIn(merged_first, final['delivery_summary'])
        self.assertEqual(git(secondary, 'rev-parse', 'HEAD'), blocked_target)

        recovered = self.record('recover', recovery_id='audit-review', evidence='Audit target reviewed')
        self.assertEqual(recovered['stage'], stage)
        receipt = self.call('claim', {'action': 'reserve', 'item': self.item, 'observed_at': self.now.isoformat(),
                                      'assigned_to': 'drill', 'status': 'active', 'desktop_project_id': 'drill'})
        self.binding['request_id'] = receipt['request_id']
        self.assertEqual(self.record('start')['stage'], stage)
        second = self.record('attempt_start', stage=stage)
        # Reconcile the successful receipt against Git before choosing what to merge.
        self.assertEqual(git(primary, 'rev-parse', 'HEAD'), partial['merged_versions'][self.key])
        self.assertEqual(git(primary, 'merge-base', '--is-ancestor', versions[self.key], 'HEAD'), '')
        self.assertEqual(git(secondary, 'rev-parse', 'HEAD'), blocked_target)
        git(secondary, 'merge', '--no-ff', '--no-edit', versions[key])
        merged = {self.key: git(primary, 'rev-parse', 'HEAD'), key: git(secondary, 'rev-parse', 'HEAD')}
        self.assertEqual(merged[self.key], merged_first)
        incomplete = self.finish(stage, second, partial)
        self.assertEqual(incomplete['status'], 'evidence_required')
        result = self.finish(stage, second, {'versions': versions, 'merged_versions': merged})
        self.assertEqual(result['disposition'], 'close', result)
        final = self.record('complete')
        self.assertEqual(final['conclusion'], '已交付')
        self.assertIn(merged[key], final['delivery_summary'])
        with closing(sqlite3.connect(self.database)) as conn:
            events = conn.execute('SELECT run_round,outcome FROM attempts WHERE stage=? ORDER BY run_round',
                                  (stage,)).fetchall()
            self.assertEqual(events, [(1, 'failed'), (2, 'success')])
