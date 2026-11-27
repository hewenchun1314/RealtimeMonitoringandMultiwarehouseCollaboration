"""Regression tests for the new post-merge delivery policy."""

import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from delivery_scope import route_stages
from route_evidence import git_push_requirements
from route_execution import replay_main_repair


class PostMergeDeliveryTests(unittest.TestCase):
    def test_new_route_merges_first_and_legacy_route_keeps_order(self):
        old = route_stages('frontend_only')
        current = route_stages('fullstack', post_merge_e2e=True, git_push_enabled=True)
        self.assertLess(old.index('frontend.e2e'), old.index('frontend.integration'))
        self.assertLess(current.index('frontend.integration'), current.index('frontend.e2e'))
        self.assertLess(current.index('frontend.e2e'), current.index('frontend.push'))

    def test_terminal_failure_can_push_the_exact_merged_version(self):
        sha = 'a' * 40
        versions = {'app': sha}
        route = {'post_merge_e2e': True, 'stages': ['frontend.e2e', 'frontend.push'],
                 'tasks': {'frontend': []}}
        history = {f'frontend.{name}': {'outcome': 'success', 'evidence': {}}
                   for name in ('plan', 'branch_check', 'coding', 'unit_test', 'code_review', 'integration')}
        history['frontend.plan']['evidence'] = {'applications': ['app']}
        branches = {'app': {'target_branch': 'release'}}
        history['frontend.branch_check']['evidence'] = {'repositories': branches}
        history['frontend.integration']['evidence'] = {'merged_versions': versions}
        history['frontend.e2e'] = {'outcome': 'failed', 'terminal_step': True,
                                  'release_versions': versions, 'task_blockers': {'F1': 'failed assertion'}}
        with patch('route_evidence.read_history', return_value=(route, history)):
            self.assertEqual(git_push_requirements(None, {}, 'frontend.push'), (versions, branches, {}, None))
            history['frontend.e2e']['terminal_step'] = False
            self.assertTrue(git_push_requirements(None, {}, 'frontend.push')[-1])
            history['frontend.e2e'].update(terminal_step=True, release_versions={'app': 'b' * 40})
            self.assertTrue(git_push_requirements(None, {}, 'frontend.push')[-1])

    def test_started_e2e_gate_and_terminal_blocker_on_the_production_ledger(self):
        self.check_started_e2e_gate_and_terminal_blocker('story')

    def test_streamlined_bug_push_checks_use_intake_repositories(self):
        self.check_started_e2e_gate_and_terminal_blocker('bug')

    def check_started_e2e_gate_and_terminal_blocker(self, kind):
        from contextlib import closing
        from route_execution import read_history
        from test_route_drills import RouteDrills

        drill = RouteDrills()
        self.addCleanup(drill.doCleanups)
        drill.setUp()
        drill.item['kind'] = kind
        drill.config['e2e'].update(after_integration=True, agentlimb={'browser': 'test'})
        drill.config['runtime'].update(git_push_enabled=True, streamlined_bug_intake=True)
        drill.begin_drill('frontend_only', None)
        stage = 'material_check'
        while stage != 'frontend.e2e':
            trial = drill.record('attempt_start', stage=stage)
            self.assertEqual(trial['status'], 'attempt_started', trial)
            result = drill.finish(stage, trial, drill.evidence(stage, trial))
            self.assertEqual(result['status'], 'attempt_finished', result)
            stage = result['next_stage']

        trial = drill.record('attempt_start', stage=stage)
        self.assertEqual(trial['host_e2e']['versions'], drill.merged['frontend'])
        self.assertIn(trial['attempt_id'], trial['host_e2e']['request'])

        def eligible():
            with closing(sqlite3.connect(drill.database)) as connection:
                connection.row_factory = sqlite3.Row
                item = connection.execute('SELECT * FROM work_items').fetchone()
                store = SimpleNamespace(conn=connection)
                _, history = read_history(store, item)
                return git_push_requirements(store, item, 'frontend.push'), history

        self.assertIn('须结束', eligible()[0][-1])
        result = drill.record('attempt_finish', attempt_id=trial['attempt_id'], outcome='failed',
                              retryable=False, summary='Chrome test login unavailable', evidence={})
        self.assertEqual(result['status'], 'evidence_required')
        result = drill.record('attempt_finish', attempt_id=trial['attempt_id'], outcome='failed',
                              retryable=False, summary='Chrome test login unavailable',
                              evidence={'e2e_blocker': {'stage': 'login', 'reason': 'session unavailable',
                                                        'cases': 'not-run'}})
        self.assertEqual(result['next_stage'], 'frontend.push', result)
        delivery, history = eligible()
        self.assertEqual(history[stage]['outcome'], 'failed')
        self.assertEqual(delivery[0], drill.merged['frontend'])

    def test_a_pending_host_request_cannot_finish_with_an_environment_note(self):
        import tempfile
        from json import dumps
        from route_evidence import host_e2e_blocker_error

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            item = {'marker': 'fixture'}
            stage = project / '.local/zentao-delivery-v2/items/fixture/frontend/e2e-123'
            stage.mkdir(parents=True)
            config_directory = project / '.agents'
            blocker = {'e2e_blocker': {'stage': 'login', 'reason': 'login unavailable', 'cases': 'not-run'}}
            self.assertIsNone(host_e2e_blocker_error(item, '123', blocker, config_directory, 'failed'))
            (stage / 'host-request.json').write_text('{}', encoding='utf-8')
            self.assertIn('Wait for the host',
                          host_e2e_blocker_error(item, '123', blocker, config_directory, 'failed'))
            (stage / 'host-run/123/e2e-host').mkdir(parents=True)
            receipt = {'marker': 'fixture', 'attempt_id': '123', 'runner': 'agentlimb', 'status': 'blocked',
                       'host_run': {'stop_confirmed': False, 'stop_evidence': {'owned_processes_active_after_stop': 1}}}
            path = stage / 'agentlimb.json'
            path.write_text(dumps(receipt), encoding='utf-8')
            self.assertIn('confirm that its processes stopped',
                          host_e2e_blocker_error(item, '123', blocker, config_directory, 'failed'))
            receipt['host_run'].update(stop_confirmed=True, stop_evidence={'owned_processes_active_after_stop': 0})
            path.write_text(dumps(receipt), encoding='utf-8')
            self.assertIsNone(host_e2e_blocker_error(item, '123', blocker, config_directory, 'failed'))

    def test_shared_workspace_stays_reserved_until_closeout(self):
        from frontend_delivery import acquire_slot, is_waiting, owns_slot, release_slot, slot_conflicts

        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.execute('CREATE TABLE deployment_locks (scope, resource, item_seq, UNIQUE(scope, resource))')
        config = {
            'workspaces': {'frontend': 'frontend'},
            'development': {'frontend_bundle': {'applications': {'app': {'repository': 'app', 'branch': 'release'}}}},
            'e2e': {'startup_ports': [5000, 5010]},
        }
        from json import dumps
        first = {'seq': 1, 'config_snapshot': dumps(config)}
        second = {'seq': 2, 'config_snapshot': dumps(config)}
        store = SimpleNamespace(conn=connection)
        folder = Path.cwd()
        self.assertTrue(acquire_slot(store, first, folder))
        self.assertTrue(owns_slot(store, first, folder))
        self.assertEqual(slot_conflicts(store, second, folder), 1)
        self.assertFalse(acquire_slot(store, second, folder))
        self.assertTrue(is_waiting(store, second))
        release_slot(store, first)
        self.assertTrue(is_waiting(store, second))
        self.assertTrue(acquire_slot(store, second, folder))
        self.assertFalse(is_waiting(store, second))

    def test_repair_commit_effective_merge_is_new_and_old_verification_is_removed(self):
        old, new = {'app': 'a' * 40}, {'app': 'b' * 40}
        original = {'outcome': 'success', 'evidence': {'versions': old, 'merged_versions': old}}
        history = {'frontend.integration': original, 'frontend.e2e': {'outcome': 'failed'},
                   'frontend.push': {'outcome': 'success'}, 'frontend.unit_test': {'outcome': 'success'},
                   'frontend.code_review': {'outcome': 'success'}}
        replay_main_repair(history, {'post_merge_e2e': True}, new)
        self.assertEqual(original['evidence']['merged_versions'], old)
        self.assertEqual(history['frontend.integration']['evidence']['merged_versions'], new)
        replay_main_repair(history, {'post_merge_e2e': True}, new)
        self.assertEqual(history['frontend.integration']['evidence']['main_repair_base_versions'], old)
        self.assertNotIn('frontend.e2e', history)
        self.assertNotIn('frontend.push', history)
        self.assertNotIn('frontend.unit_test', history)


if __name__ == '__main__':
    unittest.main()
