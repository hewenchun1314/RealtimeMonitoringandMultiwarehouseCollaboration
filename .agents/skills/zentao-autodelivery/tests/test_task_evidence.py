from pathlib import Path
import sys
import sqlite3
from contextlib import closing
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from task_evidence import evaluate, validate_merge_tests, validate_task_fields


def step(data, outcome='success'):
    return {'outcome': outcome, 'evidence': data}


class TaskEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.scope = {'scope': 'frontend_only', 'tasks': {'frontend': [{'id': 'A'}, {'id': 'B'}]}}
        self.versions = {'app': '1' * 40, 'shell': '2' * 40}
        self.plan = {'applications': list(self.versions), 'task_repositories': {'A': ['app'], 'B': ['app', 'shell']}}
        self.results = {key: {'outcome': 'passed', 'verification': 'page accepted'} for key in ('A', 'B')}
        self.stages = {'frontend.plan': step(self.plan), 'frontend.coding': step({'versions': self.versions}),
                       'frontend.e2e': step({'versions': self.versions, 'task_results': self.results}),
                       'frontend.integration': step({'versions': self.versions,
                                                     'merged_versions': {'app': self.versions['app']},
                                                     'task_changed_merges': []}, 'failed')}

    def flags(self):
        return {key: value[0] for key, value in evaluate(self.scope, self.stages).items()}

    def test_one_of_two_tasks_can_close_after_partial_merge(self):
        self.assertEqual(self.flags(), {'A': 'closed', 'B': 'open'})

    def test_changed_tree_needs_per_task_reverification(self):
        merge = self.stages['frontend.integration']['evidence']
        merge.update(task_changed_merges=['app'], merged_versions={'app': '3' * 40})
        self.assertEqual(self.flags()['A'], 'open')
        merge['merged_task_verification'] = {'A': 'E2E on integrated version passed'}
        self.assertEqual(self.flags()['A'], 'closed')

    def test_successful_merge_cannot_finish_with_unverified_changed_trees(self):
        mapping = self.plan['task_repositories']
        self.assertIsNotNone(validate_merge_tests(mapping, ['app'], {'A': 'test passed'}, True))
        self.assertIsNone(validate_merge_tests(mapping, ['app'], {'A': 'test passed'}, False))
        self.assertIsNone(validate_merge_tests(mapping, ['shell'], {'B': 'test passed'}, True))

    def test_failed_overall_verification_preserves_passed_peer(self):
        self.stages['frontend.e2e']['outcome'] = 'failed'
        self.results['B']['outcome'] = 'failed'
        self.assertEqual(self.flags(), {'A': 'closed', 'B': 'open'})
        self.stages['frontend.coding'] = step({'versions': {'app': '4' * 40, 'shell': '2' * 40}})
        self.assertEqual(self.flags()['A'], 'open')

    def test_separate_partial_receipts_accumulate_on_the_same_candidates(self):
        first = self.stages['frontend.e2e']
        first['outcome'] = 'failed'
        first['evidence']['task_results'] = {'B': self.results['B']}
        decisions = evaluate(self.scope, self.stages)
        earlier = {key: value[2] for key, value in decisions.items()}
        first['evidence']['task_results'] = {'A': self.results['A']}
        self.stages['frontend.integration'] = step({
            'versions': self.versions, 'merged_versions': {'shell': self.versions['shell']}}, 'failed')
        decisions = evaluate(self.scope, self.stages, earlier)
        self.assertEqual(decisions['B'][0], 'closed')
        first['evidence']['task_results']['B'] = {'outcome': 'failed', 'verification': 'regression'}
        self.assertEqual(evaluate(self.scope, self.stages, earlier)['B'][0], 'open')

    def test_plan_requires_unique_covered_repository_keys(self):
        self.plan['task_repositories']['B'] = ['missing']
        self.assertIsNotNone(validate_task_fields(self.scope, 'frontend.plan', self.plan, True))
        self.plan['task_repositories']['B'] = ['app']
        self.assertIsNotNone(validate_task_fields(self.scope, 'frontend.plan', self.plan, True))

    def test_joint_regression_reopens_original_backend(self):
        self.scope = {'scope': 'fullstack', 'tasks': {'frontend': [{'id': 'F'}], 'backend': [{'id': 'B'}]}}
        versions = {'api': 'a' * 40}
        passed = {'outcome': 'passed', 'verification': 'endpoint passed'}
        self.stages.update({
            'backend.plan': step({'task_repositories': {'B': ['api']}}),
            'backend.coding': step({'versions': versions}),
            'backend.acceptance': step({'versions': versions, 'task_results': {'B': passed}}),
            'backend.integration': step({'versions': versions, 'merged_versions': versions, 'task_changed_merges': []}),
            'handoff': step({'backend_versions': versions}),
            'frontend.plan': step({'task_repositories': {'F': ['app']}}),
            'frontend.e2e': step({'versions': self.versions, 'backend_versions': versions,
                                  'task_results': {'B': {'outcome': 'failed', 'verification': 'wrong API value'}}}, 'failed')
        })
        self.assertEqual(self.flags()['B'], 'open')
        self.stages['frontend.e2e']['evidence']['backend_versions'] = {'api': 'b' * 40}
        self.assertEqual(self.flags()['B'], 'closed')

    def test_only_tasks_with_matching_deployed_service_require_deploy_receipt(self):
        route = {'scope': 'backend_only', 'stages': ['backend.deploy'],
                 'tasks': {'backend': [{'id': 'A'}, {'id': 'B'}, {'id': 'C'}]}}
        versions = {'first': 'a' * 40, 'second': 'b' * 40, 'local': 'c' * 40}
        target = {'gateway_url': 'https://example.test/', 'registry': 'registry.example.test',
                  'image_repository': 'team/backend'}
        targets = {'first': target, 'second': target}
        passed = {'outcome': 'passed', 'verification': 'endpoint passed'}
        stages = {
            'backend.plan': step({'task_repositories': {'A': ['first'], 'B': ['second'], 'C': ['local']}}),
            'backend.coding': step({'versions': versions}),
            'backend.acceptance': step({'versions': versions, 'task_results': dict.fromkeys('ABC', passed)}),
            'backend.integration': step({'versions': versions, 'merged_versions': versions}),
            'backend.deploy': step({'services': {
                'first': {'status': 'deployed', 'service': 'first', 'target': target, 'sha': versions['first'],
                          'digest': 'sha256:' + 'a' * 64, 'image': 'registry.example.test/team/backend:1.0.1',
                          'verification': {'sha': versions['first']}},
                'second': {'status': 'deployed', 'service': 'second', 'target': target, 'sha': 'd' * 40,
                           'digest': 'sha256:' + 'b' * 64, 'image': 'registry.example.test/team/backend:1.0.2',
                           'verification': {'sha': 'd' * 40}},
            }}, 'failed'),
        }
        decisions = evaluate(route, stages, deployment_targets=targets)
        self.assertEqual({key: value[0] for key, value in decisions.items()},
                         {'A': 'closed', 'B': 'open', 'C': 'closed'})
        self.assertEqual(decisions['B'][2]['deployment_pending'], ['second'])

    def test_notes_distinguish_merged_backend_from_waiting_frontend(self):
        route = {'scope': 'fullstack', 'tasks': {
            'backend': [{'id': 'B'}], 'frontend': [{'id': 'F', 'depends_on': ['B']}]}}
        stages = {
            'backend.plan': step({'task_repositories': {'B': ['smart-geo']}}),
            'backend.integration': step({'merged_versions': {'smart-geo': 'a' * 40}}),
            'backend.deploy': {**step({'services': {'smart-geo': {'reason': 'mvn -B failed (exit 1)'}}}, 'failed'),
                               'task_blockers': {'B': '待核验部署：smart-geo'}},
        }
        decisions = evaluate(route, stages)
        self.assertIn('代码已合回（smart-geo aaaaaaaaaaaa）', decisions['B'][1])
        self.assertIn('mvn -B failed', decisions['B'][1])
        self.assertIn('等待前置任务：B（待核验部署：smart-geo）', decisions['F'][1])


class TaskWorkflowDrill(unittest.TestCase):
    def test_added_requirement_preserves_pending_close_of_delivered_peer(self):
        from test_route_drills import RouteDrills
        drill = RouteDrills()
        drill.setUp()
        self.addCleanup(drill.doCleanups)
        drill.begin_drill('fullstack', '前后端')
        stage = 'material_check'
        while stage != 'frontend.design':
            attempt = drill.record('attempt_start', stage=stage)
            evidence = drill.evidence(stage, attempt)
            if stage == 'backend.plan':
                evidence['task_repositories'] = {'backend': [drill.key]}
            elif stage == 'backend.acceptance':
                evidence['task_results'] = {'backend': {'outcome': 'passed', 'verification': evidence['verification']}}
            result = drill.finish(stage, attempt, evidence)
            self.assertEqual(result['status'], 'attempt_finished', result)
            stage = result['next_stage']
        attempt = drill.record('attempt_start', stage=stage)
        drill.record('attempt_finish', attempt_id=attempt['attempt_id'], outcome='failed', summary='Added requirement',
                     repair_required=True, contract_changed=True)
        repair = drill.record('attempt_start', stage=stage)
        split = drill.split()
        split['tasks']['frontend'].append({'id': 'new', 'description': 'New requirement', 'acceptance': ['Extra page']})
        result = drill.record('repair_advance', attempt_id=repair['attempt_id'], phase='split',
                              summary='Add independent task', evidence=split)
        self.assertEqual(result['status'], 'route_revised', result)
        with closing(sqlite3.connect(drill.database)) as conn:
            self.assertEqual(dict(conn.execute('SELECT task_key,desired FROM split_tasks')),
                             {'frontend': 'open', 'backend': 'closed', 'new': 'open'})

    def test_joint_task_waits_for_frontend_integration_in_normal_record_flow(self):
        from test_route_drills import RouteDrills, git
        drill = RouteDrills()
        drill.setUp()
        self.addCleanup(drill.doCleanups)
        drill.begin_drill('fullstack', '前后端')
        stage = 'material_check'
        while stage:
            attempt = drill.record('attempt_start', stage=stage)
            evidence = drill.evidence(stage, attempt)
            side = stage.split('.')[0]
            if stage == 'split':
                evidence['tasks']['backend'].append({'id': 'joint', 'description': 'Both ends show the new value',
                                                    'acceptance': ['Page displays API response'], 'joint_acceptance': True})
            if stage.endswith('.plan'):
                missing = drill.finish(stage, attempt, {**evidence, 'task_repositories': {}})
                self.assertEqual(missing['status'], 'evidence_required')
                evidence['task_repositories'] = {key: [drill.key] for key in (side, 'joint')}
            if stage.endswith(('.e2e', '.acceptance')):
                if side == 'backend':
                    evidence['verified_tasks'].append('joint')
                evidence['task_results'] = {key: {'outcome': 'passed', 'verification': evidence['verification']}
                                            for key in (side, 'joint')}
            if stage == 'frontend.e2e':
                defect = dict(evidence, task_results={'backend': {'outcome': 'failed', 'verification': 'API regression'}})
                failed = drill.record('attempt_finish', attempt_id=attempt['attempt_id'], outcome='failed',
                                      repair_required=True, repair_side='backend', summary='Joint regression',
                                      evidence=defect)
                self.assertEqual(failed['disposition'], 'retry')
                with closing(sqlite3.connect(drill.database)) as conn:
                    self.assertEqual(conn.execute("SELECT desired FROM split_tasks WHERE task_key='backend'").fetchone()[0],
                                     'open')
                attempt = drill.record('attempt_start', stage=stage)
                while attempt['phase'] != 'verify':
                    phase = attempt['phase']
                    if phase == 'backend.coding':
                        tree = drill.worktrees['backend']
                        (tree / 'status.json').write_text('{ "status": "approved" }', encoding='utf-8')
                        git(tree, 'commit', '-am', 'Fix status response')
                        drill.versions['backend'] = {drill.key: git(tree, 'rev-parse', 'HEAD')}
                        proof = {'versions': drill.versions['backend']}
                    else:
                        proof = drill.evidence(phase, attempt)
                    if phase == 'backend.acceptance':
                        proof['verified_tasks'].append('joint')
                        proof['task_results'] = {key: {'outcome': 'passed', 'verification': proof['verification']}
                                                 for key in ('backend', 'joint')}
                    attempt = drill.record('repair_advance', attempt_id=attempt['attempt_id'], phase=phase,
                                           summary=phase, evidence=proof)
                    self.assertEqual(attempt['status'], 'repair_advanced', attempt)
                evidence = drill.evidence(stage, attempt)
                evidence['task_results'] = {key: {'outcome': 'passed', 'verification': evidence['verification']}
                                            for key in ('frontend', 'joint')}
            result = drill.finish(stage, attempt, evidence, phase='verify')
            self.assertEqual(result['status'], 'attempt_finished', (stage, result))
            if stage.endswith('.integration'):
                with closing(sqlite3.connect(drill.database)) as conn:
                    states = dict(conn.execute('SELECT task_key,desired FROM split_tasks'))
                self.assertEqual(states['backend'], 'closed')
                self.assertEqual(states['frontend'], 'closed' if side == 'frontend' else 'open')
                self.assertEqual(states['joint'], 'closed' if side == 'frontend' else 'open')
            stage = result['next_stage']
        with closing(sqlite3.connect(drill.database)) as conn:
            self.assertEqual([op[0] for op in conn.execute('''SELECT operation_type FROM writebacks
               WHERE json_extract(payload,'$.key')='backend' ORDER BY rowid''')],
                             [])
