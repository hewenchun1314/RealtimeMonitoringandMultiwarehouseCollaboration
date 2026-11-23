"""One routed Story, five remote tasks and three independently proven closures."""

import unittest
from contextlib import closing
from datetime import datetime
import sqlite3

import test_route_drills as drills
from task_sync import coding_attempt, record_coding_start, sync_once
from test_task_sync import Remote


class TaskDeliveryTests(unittest.TestCase):
    def test_launched_coding_updates_its_side_and_does_not_backfill_older_snapshots(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                fixture = drills.RouteDrills()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                fixture.config['runtime'].update(split_task_sync=True, split_task_progress_sync=enabled)
                fixture.begin_drill('fullstack', '前后端')
                remote = Remote()
                stage = 'material_check'
                while stage != 'backend.coding':
                    attempt = fixture.record('attempt_start', stage=stage)
                    evidence = fixture.evidence(stage, attempt)
                    if stage == 'backend.plan':
                        evidence['task_repositories'] = {'backend': [fixture.key]}
                    stage = fixture.finish(stage, attempt, evidence)['next_stage']

                while sync_once(fixture.database, remote_factory=lambda _: remote):
                    pass
                attempt = fixture.record('attempt_start', stage=stage)
                self.assertFalse(sync_once(fixture.database, remote_factory=lambda _: remote))
                start_args = (fixture.database, attempt['attempt_id'], attempt['phase'],
                              datetime.fromisoformat(attempt['deadline_at']),
                              datetime.fromisoformat(attempt['phase_started_at']))
                self.assertFalse(coding_attempt(*start_args[:3], 'wrong chat'))
                record_coding_start(*start_args, 'wrong chat')
                self.assertFalse(sync_once(fixture.database, remote_factory=lambda _: remote))
                caller = fixture.binding['chat_id']
                self.assertEqual(coding_attempt(*start_args[:3], caller), enabled)
                for _ in range(2):
                    record_coding_start(*start_args, caller)
                while sync_once(fixture.database, remote_factory=lambda _: remote):
                    pass
                with closing(sqlite3.connect(fixture.database)) as conn:
                    rows = dict(conn.execute('SELECT side,remote_status FROM split_tasks'))
                self.assertEqual(rows, {'backend': 'doing' if enabled else 'wait', 'frontend': 'wait'})
                self.assertEqual(remote.calls.count('start'), int(enabled))

    def test_disabled_page_acceptance_skips_e2e_and_closes_reviewed_merged_tasks(self):
        for scope, mark in [('frontend_only', '纯前端'), ('fullstack', '前后端')]:
            with self.subTest(scope=scope):
                fixture = drills.RouteDrills()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                fixture.config['runtime']['split_task_sync'] = True
                fixture.config['e2e']['enabled'] = False
                fixture.begin_drill(scope, mark)
                fixture.config['e2e']['enabled'] = True
                remote = Remote()
                completed = []
                stage = 'material_check'
                while stage:
                    self.assertNotEqual(stage, 'frontend.e2e')
                    attempt = fixture.record('attempt_start', stage=stage)
                    self.assertEqual(attempt['status'], 'attempt_started', attempt)
                    evidence = fixture.evidence(stage, attempt)
                    side, _, step = stage.partition('.')
                    if stage == 'split' and scope == 'fullstack':
                        evidence['tasks']['backend'][0]['joint_acceptance'] = True
                    elif step == 'plan':
                        evidence['task_repositories'] = {side: [fixture.key]}
                        if scope == 'fullstack' and side == 'frontend':
                            evidence['task_repositories']['backend'] = [fixture.key]
                    elif stage == 'backend.acceptance':
                        evidence['task_results'] = {'backend': {'outcome': 'passed',
                                                                'verification': evidence['verification']}}
                    result = fixture.finish(stage, attempt, evidence)
                    self.assertEqual(result['status'], 'attempt_finished', (stage, result))
                    while sync_once(fixture.database, fixture.artifacts.parent.name, lambda _: remote):
                        pass
                    completed.append(stage)
                    stage = result['next_stage']
                self.assertEqual(completed[-2:], ['frontend.code_review', 'frontend.integration'])
                self.assertEqual('backend.acceptance' in completed, scope == 'fullstack')
                self.assertEqual('handoff' in completed, scope == 'fullstack')
                final = fixture.record('complete')
                self.assertEqual(final['conclusion'], '已交付', final)
                self.assertIn('页面验收已跳过（项目配置关闭）', final['delivery_summary'])
                self.assertEqual(len(remote.tasks), 2 if scope == 'fullstack' else 1)
                self.assertTrue(all(t['status'] == 'closed' for t in remote.tasks.values()))
                drills.publish_status(fixture.database, fixture.config)
                board = (fixture.database.parent / 'status.html').read_text(encoding='utf-8')
                self.assertIn('已合回，页面验收已跳过', board)
                self.assertNotIn('已验收并合回', board)

    def test_single_side_routes_close_every_independently_accepted_task(self):
        for side, count, mark in [('frontend', 3, '纯前端'), ('backend', 4, '纯后端')]:
            with self.subTest(side=side):
                fixture = drills.RouteDrills()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                fixture.config['runtime']['split_task_sync'] = True
                fixture.begin_drill(side + '_only', mark)
                keys = [f'{side}-{n}' for n in range(count)]
                remote = Remote()
                stage = 'material_check'
                while stage:
                    attempt = fixture.record('attempt_start', stage=stage)
                    evidence = fixture.evidence(stage, attempt)
                    if stage == 'split':
                        evidence['tasks'] = {side: [dict(evidence['tasks'][side][0], id=k) for k in keys]}
                    elif stage.endswith('.plan'):
                        evidence['task_repositories'] = {k: [fixture.key] for k in keys}
                    elif stage.endswith(('.e2e', '.acceptance')):
                        evidence['verified_tasks'] = keys
                        evidence['task_results'] = {k: {'outcome': 'passed', 'verification': evidence['verification']}
                                                    for k in keys}
                    result = fixture.finish(stage, attempt, evidence)
                    self.assertEqual(result['status'], 'attempt_finished', (stage, result))
                    while sync_once(fixture.database, fixture.artifacts.parent.name, lambda _: remote):
                        pass
                    stage = result['next_stage']
                self.assertEqual(fixture.record('complete')['conclusion'], '已交付')
                self.assertEqual(remote.calls.count('create'), count)
                self.assertEqual(remote.calls.count('close'), count)
                self.assertTrue(all(t['status'] == 'closed' for t in remote.tasks.values()))

    def test_five_tasks_progress_independently_through_the_normal_workflow(self):
        fixture = drills.RouteDrills()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.config['runtime'].update(split_task_sync=True, split_task_progress_sync=True)
        fixture.begin_drill('fullstack', '前后端')
        ids = {'backend': ['B1', 'B2'], 'frontend': ['F1', 'F2', 'F3']}
        remote = Remote()
        stage = 'material_check'
        while stage:
            attempt = fixture.record('attempt_start', stage=stage)
            self.assertEqual(attempt['status'], 'attempt_started', attempt)
            if stage.endswith('.coding'):
                record_coding_start(fixture.database, attempt['attempt_id'], attempt['phase'],
                                    datetime.fromisoformat(attempt['deadline_at']),
                                    datetime.fromisoformat(attempt['phase_started_at']), fixture.binding['chat_id'])
            evidence = fixture.evidence(stage, attempt)
            side, _, step = stage.partition('.')
            if stage == 'split':
                evidence['tasks'] = {end: [
                    {'id': key, 'description': key + '显示状态', 'acceptance': ['显示已审批状态']}
                    for key in keys] for end, keys in ids.items()}
            if step == 'plan':
                evidence['task_repositories'] = {key: [fixture.key] for key in ids[side]}
            if step in ('acceptance', 'e2e'):
                evidence['task_results'] = {
                    key: {'outcome': 'passed' if key not in ('F2', 'F3') else 'failed',
                          'verification': evidence['verification'] + ' / ' + key}
                    for key in ids[side]}
                evidence['verified_tasks'] = [key for key, result in evidence['task_results'].items()
                                              if result['outcome'] == 'passed']
            if stage == 'frontend.e2e':
                result = fixture.record('attempt_finish', attempt_id=attempt['attempt_id'], outcome='failed',
                                        summary='F2 and F3 failed', evidence=evidence, retryable=False)
            else:
                result = fixture.finish(stage, attempt, evidence)
            self.assertEqual(result['status'], 'attempt_finished', (stage, result))
            while sync_once(fixture.database, fixture.artifacts.parent.name, lambda _: remote):
                pass
            if stage == 'split':
                self.assertEqual(len(remote.tasks), 5)
            stage = result['next_stage']
        final = fixture.record('complete', blockers_path='blockers.md')
        self.assertEqual(final['conclusion'], '阻断')
        with closing(sqlite3.connect(fixture.database)) as conn:
            rows = dict(conn.execute('SELECT task_key,remote_status FROM split_tasks'))
        self.assertEqual(rows, {'B1': 'closed', 'B2': 'closed', 'F1': 'closed', 'F2': 'doing', 'F3': 'doing'})
        self.assertEqual(remote.calls.count('comment'), 2)
