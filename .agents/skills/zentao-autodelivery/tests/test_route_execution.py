import sqlite3
import json
from contextlib import closing
from pathlib import Path

from test_workflow import WorkflowFixture


class RouteExecutionTests(WorkflowFixture):
    def setUp(self):
        super().setUp()
        self.config['workspaces'] = {'requirement': 'requirements'}
        self.material = self.fixture / 'requirements' / 'example' / 'intent.md'
        self.material.parent.mkdir(parents=True)
        self.material.write_text('研发范围：前后端', encoding='utf-8')

    def finish_stage(self, context, stage, outcome='success', **fields):
        attempt = self.record('attempt_start', **context, stage=stage)
        self.assertEqual(attempt['status'], 'attempt_started', attempt)
        return self.record('attempt_finish', **context, attempt_id=attempt['attempt_id'],
                           outcome=outcome, summary='test ' + stage, **fields)

    def test_new_claim_keeps_frontend_route_from_own_material_snapshot(self):
        context = self.start()
        with closing(sqlite3.connect(self.database)) as conn:
            snapshot = json.loads(conn.execute("SELECT config_snapshot FROM work_items WHERE item_id='2'").fetchone()[0])
        self.assertEqual(snapshot['workflow_version'], 2)
        result = self.finish_stage(context, 'material_check', evidence={'path': 'example/intent.md'})
        self.assertEqual(result['next_stage'], 'split')
        evidence = {'tasks': {'frontend': [{'id': 'F1', 'description': 'Render data', 'acceptance': ['Visible']}]},
                    'material': {'markers': ['纯后端']}}
        result = self.finish_stage(context, 'split', evidence=evidence)
        self.assertEqual(result['next_stage'], 'frontend.design')
        with closing(sqlite3.connect(self.database)) as conn:
            result = json.loads(conn.execute("SELECT result FROM attempts WHERE stage='split'").fetchone()[0])
        self.assertEqual(result['route']['source'], 'config')
        self.assertEqual(result['route']['material']['markers'], ['前后端'])

    def test_reviewing_story_with_material_can_go_straight_to_local_development(self):
        self.config['zentao']['accepted_statuses']['story'].append('reviewing')
        self.scan('baseline', [])
        self.scan('next', [{**self.row(2), 'status': 'reviewing'}])
        context = {'request_id': self.reserve(2, status='reviewing')['request_id'], 'chat_id': 'chat-2'}
        self.record('bind', **context, desktop_project_id='desktop-1')
        self.record('start', **context)
        step = self.finish_stage(context, 'material_check', evidence={'path': 'example/intent.md'})
        self.assertEqual(step['next_stage'], 'split', step)
        split = {'tasks': {'frontend': [{'id': 'F1', 'description': 'Render', 'acceptance': ['Visible']}]}}
        self.assertEqual(self.finish_stage(context, 'split', evidence=split)['next_stage'], 'frontend.design')
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT DISTINCT operation_type FROM writebacks').fetchall(), [('comment',)])

    def configure_backend_dependencies(self):
        self.config['workspaces']['backend'] = 'backend'
        service = {'branch': 'main', 'build_command': 'build', 'test_command': 'test',
                   'acceptance_command': 'check', 'service_url': 'http://127.0.0.1:1'}
        self.config['development']['backend_bundle'] = {'name': 'Business API', 'services': {
            name: {**service, 'repository': name} for name in ('business-api', 'local-api')
        }}
        self.config['backend_dependencies'] = {'upstream': {
            'source_path': 'providers/upstream', 'api_source': 'contracts', 'used_by': ['business-api']
        }}

    def test_scoped_design_blocker_advances_and_propagates_to_dependent_tasks(self):
        self.configure_backend_dependencies()
        self.config['development']['allowed_requirement_types'] = ['frontend_only', 'backend_only', 'fullstack']
        context = self.start()
        self.finish_stage(context, 'material_check', evidence={'path': 'example/intent.md'})
        def task(key, deps=()):
            return {'id': key, 'description': key, 'acceptance': ['Verified'], 'depends_on': list(deps)}
        self.finish_stage(context, 'split', evidence={
            'tasks': {'backend': [task('B1'), task('B2')],
                      'frontend': [task('F1', ['B1']), task('F2')]},
            'interface_contract': 'B1 serves F1',
        })
        result = self.finish_stage(context, 'backend.design', outcome='failed', retryable=False,
                                   evidence={'task_blockers': {'B1': 'Upstream contract is unavailable'}})
        self.assertEqual(result['next_stage'], 'backend.plan', result)
        plan = self.record('attempt_start', **context, stage='backend.plan')
        self.assertEqual(plan['task_scope']['active'], ['B2'])
        self.assertEqual(set(plan['task_scope']['blocked']), {'B1', 'F1'})

    def test_all_backend_tasks_blocked_can_still_run_independent_frontend(self):
        self.configure_backend_dependencies()
        self.config['development']['allowed_requirement_types'] = ['frontend_only', 'backend_only', 'fullstack']
        context = self.start()
        self.finish_stage(context, 'material_check', evidence={'path': 'example/intent.md'})
        task = lambda key: {'id': key, 'description': key, 'acceptance': ['Verified']}
        self.finish_stage(context, 'split', evidence={
            'tasks': {'backend': [task('B1')], 'frontend': [task('F1')]},
            'interface_contract': 'No dependency for F1',
        })
        result = self.finish_stage(context, 'backend.design', outcome='failed', retryable=False,
                                   evidence={'task_blockers': {'B1': 'Unavailable source'}})
        stage = result['next_stage']
        while stage != 'frontend.design':
            attempt = self.record('attempt_start', **context, stage=stage)
            self.assertEqual(attempt['status'], 'attempt_started', attempt)
            if stage != 'handoff':
                self.assertEqual(attempt['task_scope']['active'], [], attempt)
            result = self.record('attempt_finish', **context, attempt_id=attempt['attempt_id'],
                                 outcome='skipped', summary='No eligible tasks for ' + stage)
            self.assertEqual(result['status'], 'attempt_finished', result)
            stage = result['next_stage']
        self.assertEqual(self.record('attempt_start', **context, stage=stage)['task_scope']['active'], ['F1'])

    def test_dependency_discovery_uses_admitted_snapshot_and_selected_services(self):
        self.configure_backend_dependencies()
        self.config['development']['allowed_requirement_types'] = ['backend_only']
        self.material.write_text('研发范围：纯后端', encoding='utf-8')
        context = self.start()
        self.config['backend_dependencies']['upstream']['api_source'] = 'new-contracts'
        self.finish_stage(context, 'material_check', evidence={'path': 'example/intent.md'})
        task = {'id': 'B1', 'description': 'Serve local data', 'acceptance': ['Returns local records']}
        self.finish_stage(context, 'split', evidence={'tasks': {'backend': [task]}})
        design = self.record('attempt_start', **context, stage='backend.design')
        api_source = self.fixture / 'providers' / 'upstream' / 'contracts'
        self.assertEqual(design.get('backend_dependencies', {}).get('upstream'), {
            'source_path': str(api_source.parent), 'api_source': str(api_source), 'used_by': ['business-api'],
            'access': 'read_only',
        })
        active = self.record('attempt_start', **context, stage='backend.design')
        self.assertEqual(active['backend_dependencies'], design['backend_dependencies'])
        self.record('attempt_finish', **context, attempt_id=design['attempt_id'],
                    outcome='success', summary='Design local implementation', evidence={'path': 'design.md'})
        plan = self.record('attempt_start', **context, stage='backend.plan')
        wrong = self.record('attempt_finish', **context, attempt_id=plan['attempt_id'],
                            outcome='success', summary='Invalid write target', evidence={'services': ['upstream']})
        self.assertEqual(wrong['status'], 'evidence_required')
        self.record('attempt_finish', **context, attempt_id=plan['attempt_id'],
                    outcome='success', summary='Plan local implementation', evidence={'services': ['local-api']})
        branch = self.record('attempt_start', **context, stage='backend.branch_check')
        self.assertEqual(set(branch['branch_targets']), {'local-api'})
        self.assertNotIn('backend_dependencies', branch)

    def test_frontend_attempt_has_no_backend_dependency_context(self):
        self.configure_backend_dependencies()
        context = self.start()
        self.finish_stage(context, 'material_check', evidence={'path': 'example/intent.md'})
        task = {'id': 'F1', 'description': 'Show data', 'acceptance': ['Uses the business API']}
        self.finish_stage(context, 'split', evidence={'tasks': {'frontend': [task]}})
        design = self.record('attempt_start', **context, stage='frontend.design')
        self.assertEqual(design['status'], 'attempt_started')
        self.assertNotIn('backend_dependencies', design)

    def test_configuration_rejects_unknown_consumers_and_escaped_contract_sources(self):
        self.configure_backend_dependencies()
        upstream = self.config['backend_dependencies']['upstream']
        upstream['used_by'] = ['missing']
        with self.assertRaisesRegex(ValueError, 'used_by'):
            self.scan('bad-consumer', [])
        upstream['used_by'] = ['business-api']
        upstream['api_source'] = '../other-repo'
        with self.assertRaisesRegex(ValueError, 'api_source'):
            self.scan('escaped-source', [])

    def test_configuration_rejects_a_dependency_at_a_writable_repository_path(self):
        self.configure_backend_dependencies()
        self.config['backend_dependencies']['upstream']['source_path'] = 'backend/business-api'
        with self.assertRaisesRegex(ValueError, 'source_path'):
            self.record('runtime_check', chat_id='chat-2')

    def test_writable_dependency_requires_delivery_settings(self):
        self.configure_backend_dependencies()
        upstream = self.config['backend_dependencies']['upstream']
        upstream['access'] = 'writable'
        with self.assertRaisesRegex(ValueError, 'delivery'):
            self.record('runtime_check', chat_id='chat-2')
        upstream['access'] = 'writeable'
        with self.assertRaisesRegex(ValueError, 'access'):
            self.record('runtime_check', chat_id='chat-2')

    def test_writable_dependency_uses_its_own_target_branch_and_worktree(self):
        from datetime import datetime, timezone
        from test_route_drills import git

        self.time = datetime.now(timezone.utc)
        self.configure_backend_dependencies()
        self.config['development']['allowed_requirement_types'] = ['backend_only']
        self.material.write_text('研发范围：纯后端', encoding='utf-8')
        upstream = self.config['backend_dependencies']['upstream']
        upstream.update(access='writable', delivery={
            'branch': 'provider-dev', 'build_command': 'build', 'test_command': 'test',
            'acceptance_command': 'check', 'service_url': 'http://127.0.0.1:1',
        })
        # Regular bundle Git metadata grants are separate from the explicit
        # writable dependency authorization; this registry deliberately omits it.
        self.config['runtime']['worker_git_write_repositories'] = ['backend:business-api']
        repo = (self.fixture / upstream['source_path']).resolve()
        repo.mkdir(parents=True)
        git(repo, 'init', '-b', 'provider-dev')
        git(repo, '-c', 'user.name=Drill', '-c', 'user.email=drill@example.invalid',
            'commit', '--allow-empty', '-m', 'Provider baseline')
        context = self.start()
        self.finish_stage(context, 'material_check', evidence={'path': 'example/intent.md'})
        task = {'id': 'B1', 'description': 'Update provider API', 'acceptance': ['Returns the updated contract']}
        self.finish_stage(context, 'split', evidence={'tasks': {'backend': [task]}})
        self.finish_stage(context, 'backend.design', evidence={'path': 'design.md'})
        plan = self.finish_stage(context, 'backend.plan', evidence={'services': ['upstream']})
        self.assertEqual(plan.get('next_stage'), 'backend.branch_check', plan)
        branch = self.record('attempt_start', **context, stage='backend.branch_check')
        self.assertEqual(branch['backend_dependencies']['upstream']['access'], 'writable')
        prepare = {**context, 'attempt_id': branch['attempt_id'], 'service': 'upstream',
                   'desktop_reconciliation': {'status': 'unavailable', 'evidence': 'Local drill'}}
        upstream['access'] = 'read_only'
        blocked = self.record('branch_prepare', **prepare)
        self.assertEqual(blocked['status'], 'host_git_rejected', blocked)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM development_git_operations').fetchone()[0], 0)
        upstream['access'] = 'writable'
        requested = self.record('branch_prepare', **prepare)
        self.assertEqual(requested['status'], 'host_git_worker_required', requested)
        prepared = self.host_git()
        self.assertEqual(prepared['status'], 'host_git_completed', prepared)
        proof = prepared['repositories']['upstream']
        self.assertEqual(proof['source_root'], str(repo))
        self.assertEqual(proof['target_branch'], 'provider-dev')
        self.assertNotEqual(proof['worktree'], str(repo))
        finished = self.record('attempt_finish', **context, attempt_id=branch['attempt_id'], outcome='success',
                               summary='Verified worktree', evidence={'repositories': {'upstream': proof}})
        self.assertEqual(finished.get('next_stage'), 'backend.coding', finished)
        worktree = Path(proof['worktree'])
        (worktree / 'contract.txt').write_text('updated contract', encoding='utf-8')
        git(worktree, 'add', 'contract.txt')
        git(worktree, '-c', 'user.name=Drill', '-c', 'user.email=drill@example.invalid',
            'commit', '-m', 'Update provider contract')
        candidate = git(worktree, 'rev-parse', 'HEAD')
        versions = {'upstream': candidate}
        self.assertEqual(self.finish_stage(context, 'backend.coding', evidence={'versions': versions})['next_stage'],
                         'backend.unit_test')
        self.assertEqual(git(worktree, 'show', 'HEAD:contract.txt'), 'updated contract')
        self.assertEqual(git(worktree, 'diff', '--check', 'HEAD^', 'HEAD'), '')
        for stage in ('backend.unit_test', 'backend.code_review', 'backend.acceptance'):
            evidence = {'versions': versions, 'verification': 'Verified the candidate contract file and commit diff'}
            if stage == 'backend.acceptance':
                evidence['verified_tasks'] = ['B1']
            self.assertEqual(self.finish_stage(context, stage, evidence=evidence)['status'], 'attempt_finished')
        git(repo, 'merge', '--ff-only', candidate)
        merged = self.finish_stage(context, 'backend.integration',
                                   evidence={'versions': versions, 'merged_versions': versions})
        self.assertEqual(merged['status'], 'attempt_finished')
        self.assertEqual(git(repo, 'rev-parse', 'refs/heads/provider-dev'), candidate)

    def test_failed_split_stops_before_development_and_recovery_reads_material_again(self):
        context = self.start()
        self.finish_stage(context, 'material_check', evidence={'path': 'example/intent.md'})
        finish = self.finish_stage(context, 'split', 'failed', retryable=False)
        self.assertIsNone(finish['next_stage'])
        closeout = self.record('complete', blockers_path='blockers.md', **context)
        self.assertEqual(closeout['conclusion'], '阻断')
        self.assertEqual([s['stage'] for s in closeout['steps']], ['material_check', 'split'])
        self.assertEqual(self.record('recover', recovery_id='fixed', evidence='updated material')['stage'],
                         'material_check')

    def test_recovery_restarts_material_and_split_when_reviewed_intent_changes(self):
        context = self.start()
        self.finish_stage(context, 'material_check', evidence={'path': 'example/intent.md'})
        evidence = {'tasks': {'frontend': [{'id': 'F1', 'description': 'Show data', 'acceptance': ['Visible']}]}}
        self.finish_stage(context, 'split', evidence=evidence)
        failed = self.finish_stage(context, 'frontend.design', 'failed', retryable=False,
                                   evidence={'task_blockers': {'F1': 'Product rule is unclear'}})
        stage = failed['next_stage']
        while stage:
            skipped = self.finish_stage(context, stage, 'skipped')
            self.assertEqual(skipped['status'], 'attempt_finished', skipped)
            stage = skipped['next_stage']
        self.record('complete', blockers_path='blockers.md', **context)
        self.material.write_text('研发范围：前后端\n历史曲线应追溯原始输入。', encoding='utf-8')
        restored = self.record('recover', recovery_id='new-material', evidence='Confirmed historic curve policy')
        self.assertEqual(restored['stage'], 'material_check')
        context['request_id'] = self.reserve(2)['request_id']
        self.record('start', **context)
        self.assertEqual(self.finish_stage(context, 'material_check', evidence={'path': 'example/intent.md'})[
            'next_stage'], 'split')
        self.assertEqual(self.finish_stage(context, 'split', evidence=evidence)['next_stage'], 'frontend.design')

    def test_blocked_split_has_no_developer_attempt_and_can_reread_corrected_material(self):
        context = self.start()
        self.finish_stage(context, 'material_check', evidence={'path': 'example/intent.md'})
        blocked = self.finish_stage(context, 'split', evidence={'tasks': {}, 'blockers': ['Missing API']})
        self.assertEqual((blocked['disposition'], blocked['next_stage']), ('blocked', None))
        closed = self.record('complete', blockers_path='blockers.md', **context)
        self.assertEqual(closed['conclusion'], '阻断')
        self.assertIn('Missing API', closed['delivery_summary'])
        self.material.write_text('研发范围：纯前端', encoding='utf-8')
        restored = self.record('recover', recovery_id='corrected', evidence='Updated intent')
        self.assertEqual(restored['stage'], 'material_check')
        context['request_id'] = self.reserve(2)['request_id']
        self.record('start', **context)
        self.finish_stage(context, 'material_check', evidence={'path': 'example/intent.md'})
        task = {'id': 'f', 'description': 'Render data', 'acceptance': ['Visible']}
        self.finish_stage(context, 'split', evidence={'tasks': {'frontend': [task]}})
        resumed = self.record('attempt_start', stage='frontend.design', **context)
        self.assertEqual(resumed['route']['material']['markers'], ['纯前端'])
        self.assertEqual(resumed['route']['blockers'], [])


class ContractRepairTests(WorkflowFixture):
    def setUp(self):
        # The integration drill supplies real temporary repositories, an HTTP
        # service and the production claim/record entry points.
        from test_route_drills import RouteDrills
        self.drill = RouteDrills()
        self.drill.setUp()
        self.addCleanup(self.drill.doCleanups)
        self.drill.begin_drill('fullstack', '前后端')

    def advance_to(self, destination):
        stage = 'material_check'
        d = self.drill
        while stage != destination:
            trial = d.record('attempt_start', stage=stage)
            self.assertEqual(trial['status'], 'attempt_started', trial)
            done = d.finish(stage, trial, d.evidence(stage, trial))
            self.assertEqual(done['status'], 'attempt_finished', done)
            stage = done['next_stage']

    def test_contract_revision_after_handoff_invalidates_receipts_and_restarts_backend_plan(self):
        from ledger import Ledger, transaction
        from route_execution import read_history
        d = self.drill
        self.advance_to('frontend.design')
        trial = d.record('attempt_start', stage='frontend.design')
        d.record('attempt_finish', attempt_id=trial['attempt_id'], outcome='failed', summary='API changed',
                 repair_required=True, contract_changed=True)
        repair = d.record('attempt_start', stage='frontend.design')
        self.assertEqual((repair['phase'], repair['role']), ('split', '拆分 Agent'))
        split = d.split()
        split['interface_contract'] += ' version 2'
        revised = d.record('repair_advance', attempt_id=repair['attempt_id'], phase='split',
                           summary='New API version', evidence=split)
        self.assertEqual((revised['status'], revised['next_stage'], revised['run_round']),
                         ('route_revised', 'backend.design', 2))
        self.assertEqual(d.record('attempt_start', stage='frontend.design')['status'], 'stage_mismatch')
        with transaction(d.database) as conn:
            store = Ledger(conn, d.config, d.now)
            route, stages = read_history(store, store.find_item(d.item))
            self.assertEqual(route['interface_contract'], split['interface_contract'])
            self.assertEqual(set(stages), {'material_check', 'split'})

    def test_unresolved_contract_enters_integration_without_authorizing_old_candidate(self):
        d = self.drill
        self.advance_to('frontend.e2e')
        trial = d.record('attempt_start', stage='frontend.e2e')
        failure = d.record('attempt_finish', attempt_id=trial['attempt_id'], outcome='failed',
                           summary='The interface contract changed', repair_required=True,
                           contract_changed=True, retryable=False)
        self.assertEqual(failure['next_stage'], 'frontend.integration')
        merge = d.record('attempt_start', stage='frontend.integration')
        requested = d.record('integration_merge', attempt_id=merge['attempt_id'])
        self.assertNotIn('operation_id', requested, requested)
        self.assertEqual(merge['task_scope']['active'], [], merge)
        end = d.record('attempt_finish', attempt_id=merge['attempt_id'], outcome='skipped',
                       summary='The interface contract still needs to be split again')
        self.assertEqual(end['disposition'], 'close')
        d.record('complete', blockers_path='blockers.md')
        recovery = d.record('recover', recovery_id='contract-ready', evidence='New contract supplied')
        self.assertEqual(recovery['stage'], 'frontend.e2e')

    def test_failed_acceptance_assertion_does_not_authorize_integration(self):
        d = self.drill
        self.advance_to('frontend.e2e')
        trial = d.record('attempt_start', stage='frontend.e2e')
        failure = d.record('attempt_finish', attempt_id=trial['attempt_id'], outcome='failed',
                           summary='The status page returned the wrong value', retryable=False,
                           evidence={'versions': d.versions['frontend'], 'task_results': {
                               'frontend': {'outcome': 'failed', 'verification': 'Displayed the wrong status'}}})
        self.assertEqual(failure['next_stage'], 'frontend.integration')
        merge = d.record('attempt_start', stage='frontend.integration')
        self.assertEqual(merge['task_scope']['active'], [])
        requested = d.record('integration_merge', attempt_id=merge['attempt_id'])
        self.assertEqual(requested['status'], 'host_git_rejected', requested)

    def test_joint_test_backend_repair_remerges_and_resumes_failed_handoff(self):
        from test_route_drills import git
        d = self.drill
        self.advance_to('frontend.e2e')
        stage = 'frontend.e2e'
        trial = d.record('attempt_start', stage=stage)
        d.record('attempt_finish', attempt_id=trial['attempt_id'], outcome='failed',
                 summary='Backend bug exposed by page', repair_required=True, repair_side='backend')
        repair = d.record('attempt_start', stage=stage)
        phases = []
        old_backend = d.merged['backend']
        while repair['phase'] != 'handoff':
            phase = repair['phase']
            phases.append(phase)
            if phase == 'backend.coding':
                tree = d.worktrees['backend']
                (tree / 'status.json').write_text('{ "status": "approved" }', encoding='utf-8')
                git(tree, 'commit', '-am', 'Fix backend status serialization')
                d.versions['backend'] = {d.key: git(tree, 'rev-parse', 'HEAD')}
                evidence = {'versions': d.versions['backend']}
            else:
                evidence = d.evidence(phase, repair)
            if phase == 'backend.integration':
                rejected = d.record('attempt_finish', attempt_id=repair['attempt_id'], phase=phase,
                                    outcome='failed', summary='Invalid partial receipt',
                                    evidence={**evidence, 'merged_versions': {d.key: '0' * 40}})
                self.assertEqual(rejected['status'], 'evidence_required')
            repair = d.record('repair_advance', attempt_id=repair['attempt_id'], phase=phase,
                              summary=phase, evidence=evidence)
            self.assertEqual(repair['status'], 'repair_advanced', repair)
        self.assertEqual(phases, ['backend.coding', 'backend.unit_test', 'backend.code_review',
                                  'backend.acceptance', 'backend.integration'])
        d.record('attempt_finish', attempt_id=repair['attempt_id'], phase='handoff', outcome='failed',
                 summary='Environment paused after merge')
        repair = d.record('attempt_start', stage=stage)
        self.assertEqual((repair['phase'], repair['retry_number']), ('handoff', 2))
        repaired = d.record('repair_advance', attempt_id=repair['attempt_id'], phase='handoff', summary='New service',
                            evidence=d.evidence('handoff', repair))
        self.assertEqual(repaired['phase'], 'verify')
        verified = d.evidence(stage, repair)
        stale = d.finish(stage, repair, {**verified, 'backend_versions': old_backend}, phase='verify')
        self.assertEqual(stale['status'], 'evidence_required')
        self.assertEqual(d.finish(stage, repair, verified, phase='verify')['next_stage'], 'frontend.integration')
        merge = d.record('attempt_start', stage='frontend.integration')
        d.finish('frontend.integration', merge, d.evidence('frontend.integration', merge))
        self.assertEqual(d.record('complete')['conclusion'], '已交付')

    def test_recovery_rechecking_earlier_review_does_not_replay_old_joint_repair(self):
        d = self.drill
        self.advance_to('frontend.code_review')
        review = d.record('attempt_start', stage='frontend.code_review')
        d.record('attempt_finish', attempt_id=review['attempt_id'], outcome='failed', summary='Review interrupted',
                 retryable=False)
        joint = d.record('attempt_start', stage='frontend.e2e')
        d.record('attempt_finish', attempt_id=joint['attempt_id'], outcome='failed', summary='Old API issue',
                 repair_required=True, repair_side='backend', retryable=False)
        merge = d.record('attempt_start', stage='frontend.integration')
        d.finish('frontend.integration', merge, d.evidence('frontend.integration', merge))
        d.record('complete', blockers_path='blockers.md')
        self.assertEqual(d.record('recover', recovery_id='new-review', evidence='Verifier restored')['stage'],
                         'frontend.code_review')
        receipt = d.call('claim', {'action': 'reserve', 'item': d.item, 'observed_at': d.now.isoformat(),
                                  'assigned_to': 'drill', 'status': 'active', 'desktop_project_id': 'drill'})
        d.binding['request_id'] = receipt['request_id']
        d.record('start')
        review = d.record('attempt_start', stage='frontend.code_review')
        self.assertEqual(d.finish('frontend.code_review', review, d.evidence('frontend.code_review', review))
                         ['next_stage'], 'frontend.e2e')
        joint = d.record('attempt_start', stage='frontend.e2e')
        self.assertEqual((joint['phase'], joint['retry_number']), ('verify', 0))

    def test_failed_nested_unit_test_preserves_prior_receipts_and_recovers_at_backend(self):
        from test_route_drills import git
        d = self.drill
        self.advance_to('frontend.e2e')
        joint = d.record('attempt_start', stage='frontend.e2e')
        d.record('attempt_finish', attempt_id=joint['attempt_id'], outcome='failed', summary='Backend repair',
                 repair_required=True, repair_side='backend')
        repair = d.record('attempt_start', stage='frontend.e2e')
        tree = d.worktrees['backend']
        (tree / 'status.json').write_text('{ "status": "approved" }', encoding='utf-8')
        git(tree, 'commit', '-am', 'Fix backend format')
        d.versions['backend'] = {d.key: git(tree, 'rev-parse', 'HEAD')}
        coding = d.record('repair_advance', attempt_id=repair['attempt_id'], phase='backend.coding',
                          summary='New candidate', evidence={'versions': d.versions['backend']})
        self.assertEqual(coding['phase'], 'backend.unit_test')
        failure = d.record('attempt_finish', attempt_id=repair['attempt_id'], phase='backend.unit_test',
                           outcome='failed', retryable=False, summary='Unit suite failed on new version')
        self.assertEqual(failure['next_stage'], 'frontend.integration')
        merge = d.record('attempt_start', stage='frontend.integration')
        d.finish('frontend.integration', merge, d.evidence('frontend.integration', merge))
        final = d.record('complete', blockers_path='blockers.md')
        self.assertEqual(final['conclusion'], '待验证')
        steps = {entry['stage']: entry for entry in final['steps']}
        self.assertEqual(steps['backend.unit_test']['outcome'], 'failed')
        self.assertEqual(steps['backend.unit_test']['summary'], 'Unit suite failed on new version')
        self.assertEqual(steps['backend.acceptance']['outcome'], 'skipped')
        with closing(sqlite3.connect(d.database)) as conn:
            ids = [row[0] for row in conn.execute('SELECT operation_id FROM writebacks')]
        acceptance = [key for key in ids if key.endswith('/backend.acceptance')]
        self.assertEqual(len(acceptance), 2)
        self.assertTrue(any(key.endswith('/skipped/backend.acceptance') for key in acceptance))
        recovered = d.record('recover', recovery_id='unit-fix', evidence='Restored unit environment')
        self.assertEqual(recovered['stage'], 'backend.unit_test')
        receipt = d.call('claim', {'action': 'reserve', 'item': d.item, 'observed_at': d.now.isoformat(),
                                  'assigned_to': 'drill', 'status': 'active', 'desktop_project_id': 'drill'})
        d.binding['request_id'] = receipt['request_id']
        self.assertEqual(d.record('start')['stage'], 'backend.unit_test')
        units = d.record('attempt_start', stage='backend.unit_test')
        proof = d.evidence('backend.unit_test', units)
        self.assertEqual(proof['versions'], d.versions['backend'])
        self.assertEqual(d.finish('backend.unit_test', units, proof)['next_stage'], 'backend.code_review')
