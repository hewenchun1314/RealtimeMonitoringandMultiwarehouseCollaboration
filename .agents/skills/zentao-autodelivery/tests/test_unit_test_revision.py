"""A test fix must revise coding evidence before verification can accept its SHA."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest

import test_route_drills as drills

git = drills.git


class UnitTestRevisionTests(unittest.TestCase):
    def setUp(self):
        self.d = drills.RouteDrills()
        self.d.setUp()
        self.addCleanup(self.d.doCleanups)
        self.d.begin_drill('fullstack', '前后端')
        # This suite isolates revision routing. Host handoff/stop/hook behavior is
        # exercised separately; its disposable baseline can use the local fixture helper.
        self.d.prepare = self.prepare_fixture

    def prepare_fixture(self, side, attempt):
        from branch_check import branch_context, prepare_local_worktree
        from ledger import Ledger, transaction
        with transaction(self.d.database) as conn:
            store = Ledger(conn, self.d.config, self.d.now)
            context = branch_context(store, store.find_item(self.d.item))
        proof = prepare_local_worktree(context, self.d.root, self.d.database, self.d.key, 30)
        self.assertEqual(proof['status'], 'verified', proof)
        self.d.worktrees[side] = Path(proof['worktree'])
        return {'repositories': {self.d.key: proof}}

    def advance_to(self, destination):
        stage = 'material_check'
        while stage != destination:
            trial = self.d.record('attempt_start', stage=stage)
            self.assertEqual(trial['status'], 'attempt_started', trial)
            done = self.d.finish(stage, trial, self.d.evidence(stage, trial))
            self.assertEqual(done['status'], 'attempt_finished', done)
            stage = done['next_stage']

    def revise(self, side, message):
        tree = self.d.worktrees[side]
        file = tree / ('status.json' if side == 'backend' else 'index.html')
        file.write_text(file.read_text(encoding='utf-8') + '\n', encoding='utf-8')
        git(tree, 'commit', '-am', message)
        self.d.versions[side] = {self.d.key: git(tree, 'rev-parse', 'HEAD')}
        return {'versions': self.d.versions[side]}

    def normal_revision(self, side):
        d = self.d
        stage = side + '.unit_test'
        self.advance_to(stage)
        old_versions = d.versions[side].copy()
        trial = d.record('attempt_start', stage=stage)
        failed = d.record('attempt_finish', attempt_id=trial['attempt_id'], outcome='failed',
                          summary='Generated test fixture needs a coding revision', repair_required=True)
        self.assertEqual(failed['disposition'], 'retry')
        repair = d.record('attempt_start', stage=stage)
        self.assertEqual((repair['phase'], repair['retry_number']), (side + '.coding', 1))
        coding = d.record('repair_advance', attempt_id=repair['attempt_id'], phase=side + '.coding',
                          summary='Revise candidate', evidence=self.revise(side, 'Revise test fixture'))
        self.assertEqual(coding['phase'], 'verify', coding)
        verification = d.evidence(stage, coding)
        stale = d.finish(stage, coding, {**verification, 'versions': old_versions}, phase='verify')
        self.assertEqual(stale['status'], 'evidence_required', stale)
        self.assertEqual(d.finish(stage, coding, verification, phase='verify')['next_stage'], side + '.code_review')

    def test_normal_backend_unit_fix_revises_coding_then_retests_exact_new_sha(self):
        self.normal_revision('backend')

    def test_normal_frontend_unit_fix_revises_coding_then_retests_exact_new_sha(self):
        self.normal_revision('frontend')

    def test_nested_unit_fix_restarts_same_side_coding_and_retains_original_itinerary(self):
        d = self.d
        self.advance_to('frontend.e2e')
        initial = d.record('attempt_start', stage='frontend.e2e')
        d.record('attempt_finish', attempt_id=initial['attempt_id'], outcome='failed',
                 summary='Backend issue exposed by page', repair_required=True, repair_side='backend')
        repair = d.record('attempt_start', stage='frontend.e2e')
        coded = d.record('repair_advance', attempt_id=repair['attempt_id'], phase='backend.coding',
                         summary='First backend fix', evidence=self.revise('backend', 'First backend fix'))
        self.assertEqual(coded['phase'], 'backend.unit_test')
        failed = d.record('attempt_finish', attempt_id=repair['attempt_id'], phase='backend.unit_test',
                          outcome='failed', summary='Unit test reveals more code changes', repair_required=True)
        self.assertEqual(failed['disposition'], 'retry')
        restarted = d.record('attempt_start', stage='frontend.e2e')
        self.assertEqual((restarted['phase'], restarted['retry_number']), ('backend.coding', 2))
        with closing(sqlite3.connect(d.database)) as conn:
            result = json.loads(conn.execute('SELECT result FROM attempts WHERE attempt_id=?',
                                             (repair['attempt_id'],)).fetchone()[0])
            history = json.loads(conn.execute('SELECT phase_history FROM attempts WHERE attempt_id=?',
                                              (restarted['attempt_id'],)).fetchone()[0])
        self.assertEqual(result['phase'], 'backend.unit_test')
        self.assertEqual(result['repair_sequence'], ['backend.coding', 'backend.unit_test', 'backend.code_review',
                                                   'backend.acceptance', 'backend.integration', 'handoff', 'verify'])
        self.assertEqual(history, [])
        old_versions = d.versions['backend'].copy()
        coded = d.record('repair_advance', attempt_id=restarted['attempt_id'], phase='backend.coding',
                         summary='Correct backend candidate', evidence=self.revise('backend', 'Correct backend fix'))
        units = d.evidence('backend.unit_test', coded)
        stale = d.record('repair_advance', attempt_id=restarted['attempt_id'], phase='backend.unit_test',
                         summary='Stale test results', evidence={**units, 'versions': old_versions})
        self.assertEqual(stale['status'], 'evidence_required')
        accepted = d.record('repair_advance', attempt_id=restarted['attempt_id'], phase='backend.unit_test',
                            summary='Rerun tests on current candidate', evidence=units)
        self.assertEqual(accepted['phase'], 'backend.code_review')

    def test_nonrevision_nested_unit_failure_retries_same_test_phase(self):
        d = self.d
        self.advance_to('backend.code_review')
        review = d.record('attempt_start', stage='backend.code_review')
        d.record('attempt_finish', attempt_id=review['attempt_id'], outcome='failed',
                 summary='Review requires code fix', repair_required=True)
        repair = d.record('attempt_start', stage='backend.code_review')
        coded = d.record('repair_advance', attempt_id=repair['attempt_id'], phase='backend.coding',
                         summary='Review fix', evidence=self.revise('backend', 'Review fix'))
        self.assertEqual(coded['phase'], 'backend.unit_test')
        d.record('attempt_finish', attempt_id=repair['attempt_id'], phase='backend.unit_test',
                 outcome='failed', summary='Transient test infrastructure failure')
        retry = d.record('attempt_start', stage='backend.code_review')
        self.assertEqual((retry['phase'], retry['retry_number']), ('backend.unit_test', 2))

    def test_nested_frontend_revision_preserves_completed_backend_and_handoff_history(self):
        d = self.d
        self.advance_to('frontend.e2e')
        initial = d.record('attempt_start', stage='frontend.e2e')
        d.record('attempt_finish', attempt_id=initial['attempt_id'], outcome='failed',
                 summary='Joint repair affects both sides', repair_required=True, repair_side='backend',
                 frontend_changed=True)
        repair = d.record('attempt_start', stage='frontend.e2e')
        while repair['phase'] != 'frontend.unit_test':
            phase = repair['phase']
            evidence = (self.revise(phase.split('.')[0], 'Joint candidate revision')
                        if phase.endswith('.coding') else d.evidence(phase, repair))
            repair = d.record('repair_advance', attempt_id=repair['attempt_id'], phase=phase,
                              summary=phase, evidence=evidence)
            self.assertEqual(repair['status'], 'repair_advanced', repair)
        with closing(sqlite3.connect(d.database)) as conn:
            original = json.loads(conn.execute('SELECT phase_history FROM attempts WHERE attempt_id=?',
                                               (repair['attempt_id'],)).fetchone()[0])
        expected = original[:-1]
        self.assertEqual(original[-1]['phase'], 'frontend.coding')
        self.assertEqual(expected[-1]['phase'], 'handoff')
        d.record('attempt_finish', attempt_id=repair['attempt_id'], phase='frontend.unit_test',
                 outcome='failed', summary='Frontend unit fixture needs correction', repair_required=True)
        retry = d.record('attempt_start', stage='frontend.e2e')
        self.assertEqual(retry['phase'], 'frontend.coding')
        with closing(sqlite3.connect(d.database)) as conn:
            history = json.loads(conn.execute('SELECT phase_history FROM attempts WHERE attempt_id=?',
                                              (retry['attempt_id'],)).fetchone()[0])
        self.assertEqual(history, expected)


if __name__ == '__main__':
    unittest.main()
