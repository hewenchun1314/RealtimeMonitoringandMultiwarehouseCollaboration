import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / 'scripts'))

from delivery_scope import project_mode, read_material, resolve_split, route_stages
from ledger import STAGE_NAMES, validate_config


class DeliveryScopeTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((SKILL.parents[1] / 'zentao-project.json').read_text(encoding='utf-8'))
        self.full = ['fullstack', 'frontend_only', 'backend_only']
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config['workspaces']['requirement'] = 'requirements'
        self.intent = self.directory / 'requirements' / 'ZT-1' / 'intent.md'
        self.intent.parent.mkdir(parents=True)

    def decide(self, types, contents, expected_scope, **extra):
        self.config['development']['allowed_requirement_types'] = types
        self.intent.write_text(contents, encoding='utf-8-sig')
        material = read_material(self.config, self.directory, 'story', {'path': 'ZT-1/intent.md'})
        tasks = {}
        for side in ('backend', 'frontend'):
            if expected_scope in (side + '_only', 'fullstack'):
                tasks[side] = [{'id': side, 'description': 'Implement change', 'acceptance': ['Case 1']}]
        return resolve_split(self.config, {'material': material, 'tasks': tasks,
                                          'interface_contract': 'GET /items returns total', **extra})

    def test_project_config_overrides_missing_conflicting_and_illegal_labels(self):
        for scope in ('frontend_only', 'backend_only'):
            for text in ('', '研发范围：前后端', '研发范围:未知\n研发范围：纯后端'):
                with self.subTest(scope=scope, text=text):
                    result = self.decide([scope], text, scope)
                    self.assertEqual((result['scope'], result['source']), (scope, 'config'))

    def test_explicit_marker_or_unmarked_ai_in_two_sided_project(self):
        for value, scope in [('纯前端', 'frontend_only'), ('纯后端', 'backend_only'), ('前后端', 'fullstack')]:
            decision = self.decide(self.full, '研发范围: ' + value, scope)
            self.assertEqual((decision['scope'], decision['source']), (scope, 'document'))
            decision = self.decide(self.full, '~~~md\n研发范围：前后端\n~~~', scope,
                                   ai_scope=scope, rationale='Checked actual service and UI')
            self.assertEqual((decision['scope'], decision['source']), (scope, 'ai'))
        for text in ('研发范围：未知', '研发范围：纯前端\n研发范围：纯后端', '研发范围：', ''):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.decide(self.full, text, 'frontend_only', ai_scope='frontend_only')

    def test_project_configuration_validated(self):
        dev = self.config['development']
        del dev['allowed_requirement_types']
        self.assertEqual(project_mode(dev), 'frontend_only')
        validate_config(self.config)
        for bad in ([], ['fullstack'], self.full + ['frontend_only'], 'backend_only', [False]):
            dev['allowed_requirement_types'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_config(self.config)
        dev['allowed_requirement_types'] = ['backend_only']
        dev.pop('backend_bundle', None)
        with self.assertRaises(ValueError):
            validate_config(self.config)
        service = dict(repository='.', branch='main', build_command='make', test_command='make test',
                       acceptance_command='make smoke', service_url='http://localhost:1234')
        self.config['workspaces'] = {'backend': '../services'}
        del dev['frontend_bundle']
        self.config.pop('e2e', None)
        self.config.pop('backend_dependencies', None)
        dev['backend_bundle'] = {'name': 'service-group', 'services': {'api': service}}
        validate_config(self.config)
        for invalid in ({'repository': '../other'}, {'service_url': ''}, {'branch': ''}, {'test_command': None}):
            config = copy.deepcopy(self.config)
            config['development']['backend_bundle']['services']['api'].update(invalid)
            with self.assertRaises(ValueError):
                validate_config(config)

    def test_material_must_be_the_reviewed_intent_and_bug_reads_actual_detail(self):
        for path in ('../other/intent.md', 'ZT-1/readme.md', ''):
            with self.assertRaises(ValueError):
                read_material(self.config, self.directory, 'story', {'path': path})
        bug = read_material({}, self.directory, 'bug', {'detail': '研发范围：纯后端'})
        self.assertEqual(bug['markers'], ['纯后端'])

    def test_e2e_switch_requires_a_boolean(self):
        for invalid in ('false', 0, None):
            self.config['e2e']['enabled'] = invalid
            with self.subTest(value=invalid), self.assertRaisesRegex(ValueError, 'e2e.enabled'):
                validate_config(self.config)

    def test_inactive_side_and_invalid_dependencies_rejected(self):
        invalid = {'id': 'T1', 'description': 'Do work', 'acceptance': ['tested']}
        for tasks in ({'backend': [invalid]}, {'frontend': [dict(invalid, depends_on=['unknown'])]},
                      {'frontend': [invalid, invalid]}, {'frontend': [dict(invalid, acceptance=[])]}):
            with self.assertRaises(ValueError):
                self.decide(['frontend_only'], '', 'frontend_only', tasks=tasks)
        result = self.decide(['frontend_only'], '', 'frontend_only', tasks={},
                             blockers=['Backend API is required'])
        self.assertTrue(result['blockers'])

    def test_routes_are_distinct_ordered_and_displayable(self):
        combined = route_stages('fullstack')
        self.assertEqual(combined[:2], ('material_check', 'split'))
        self.assertEqual(combined[combined.index('handoff') - 1:combined.index('handoff') + 2],
                         ('backend.integration', 'handoff', 'frontend.design'))
        for scope, side in [('frontend_only', 'frontend'), ('backend_only', 'backend')]:
            route = route_stages(scope)
            self.assertTrue(all(stage.startswith(side + '.') for stage in route[2:]))
            self.assertIn(side + '.branch_check', route)
        self.assertTrue(all(stage in STAGE_NAMES for stage in combined))

    def test_cross_side_blockers_default_to_handoff_dependencies_and_can_be_classified(self):
        known = ['Missing service response', 'Missing screen copy']
        marked = '研发范围：前后端'
        self.assertEqual(self.decide(self.full, marked, 'fullstack', blockers=known)['handoff_blockers'], known)
        partial = self.decide(self.full, marked, 'fullstack', blockers=known, handoff_blockers=known[:1])
        self.assertEqual(partial['handoff_blockers'], known[:1])
        self.assertEqual(partial['blockers'], known)
        self.assertEqual(self.decide(self.full, marked, 'fullstack', blockers=known,
                                     handoff_blockers=[])['handoff_blockers'], [])
        for invalid in (['Unknown'], [False], None, 'Missing service response'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.decide(self.full, marked, 'fullstack', blockers=known, handoff_blockers=invalid)
