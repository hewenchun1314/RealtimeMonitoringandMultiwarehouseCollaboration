import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from branch_check import branch_context, planned_applications


class SideBranchTests(unittest.TestCase):
    def test_each_side_reads_its_own_plan_and_workspace(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute('CREATE TABLE attempts(item_seq,stage,run_round,retry_number,result,outcome)')
        for side, kind in [('backend', 'services'), ('frontend', 'applications')]:
            connection.execute("INSERT INTO attempts VALUES(1,?,1,0,?,'success')", (
                side + '.plan', json.dumps({'evidence': {kind: ['shared']}})))
        config = {'workspaces': {'backend': 'api', 'frontend': 'web'}, 'development': {
            'backend_bundle': {'name': 'api', 'services': {'shared': {'branch': 'server'}}},
            'frontend_bundle': {'name': 'web', 'applications': {'shared': {'branch': 'ui'}}}}}
        item = {'seq': 1, 'config_snapshot': json.dumps(config), 'marker': 'ZT-1', 'run_round': 1}
        store = type('Store', (), {'conn': connection})()
        branches = []
        for side, branch in [('backend', 'server'), ('frontend', 'ui')]:
            item['stage'] = side + '.branch_check'
            context = branch_context(store, item)
            self.assertEqual(context['branch_targets']['shared']['branch'], branch)
            self.assertEqual(context['workspace'], config['workspaces'][side])
            branches.append(context['worktree_branch'])
        self.assertNotEqual(*branches)
        self.assertIsNone(planned_applications({'applications': ['shared']}, 'backend'))
        connection.execute("UPDATE attempts SET outcome='failed' WHERE stage='frontend.plan'")
        self.assertEqual(branch_context(store, item)['branch_targets'], {})
