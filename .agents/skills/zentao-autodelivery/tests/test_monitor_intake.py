import copy
import io
import json
import sqlite3
from contextlib import closing, redirect_stdout
from datetime import timedelta
from unittest.mock import patch
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from test_workflow import WorkflowFixture

try:
    import monitor_intake
except ModuleNotFoundError:
    monitor_intake = None


class Session:
    def __init__(self, lists, details=None):
        self.lists = lists
        self.details = details or {}
        self.calls = []

    def _v2(self, method, url):
        assert method == 'GET'
        self.calls.append(url)
        parsed = urlsplit(url)
        if parsed.path == '/my/profile':
            return {'user': {'account': 'Test-user'}}
        if parsed.path in self.details:
            return copy.deepcopy(self.details[parsed.path])
        values = copy.deepcopy(self.lists[parsed.path])
        page = int(parse_qs(parsed.query).get('pageID', ['1'])[0])
        key = parsed.path.rsplit('/', 1)[1]
        # Two records per page exercises pagination even for this small fixture.
        return {key: values[(page - 1) * 2:page * 2],
                'pager': {'pageID': page, 'recTotal': len(values), 'pageTotal': (len(values) + 1) // 2}}


class MonitorIntakeTests(WorkflowFixture):
    def setUp(self):
        super().setUp()
        self.config['zentao'].update(scope_mode='project_or_product', product_id='2', product_name='Sample product')
        self.bug = {'id': 2, 'title': 'Product-only Bug', 'project': 0, 'product': 2,
                    'assignedTo': 'Test-user', 'status': 'active', 'pri': 2}
        self.project_bug = {**self.bug, 'id': 3, 'project': 1, 'product': 9}
        self.both = {**self.bug, 'id': 4, 'project': 1}
        self.lists = {'/projects': [{'id': 1, 'name': 'Sample'}],
                      '/products': [{'id': 2, 'name': 'Sample product'}],
                      '/projects/1/bugs': [self.project_bug, self.both],
                      '/products/2/bugs': [self.bug, self.both],
                      '/projects/1/stories': [], '/products/2/stories': []}
        self.lists = copy.deepcopy(self.lists)
        # Avoid sharing the same mutable dict across the two endpoint fixtures.
        self.lists['/products/2/bugs'] = copy.deepcopy(self.lists['/products/2/bugs'])

    def collect(self, session=None):
        self.assertIsNotNone(monitor_intake, 'Project-or-product collector is missing')
        return monitor_intake.collect_scan(self.config, session or Session(self.lists),
                                          scan_id='scope-test', observed_at=self.time.isoformat(),
                                          expected_account='Test-user')

    def test_union_includes_either_owner_and_deduplicates_both(self):
        request, proof = self.collect()
        self.assertCountEqual([r['id'] for r in request['items']], ['2', '3', '4'])
        self.assertEqual(request['project_id'], '1')
        self.assertEqual(next(r for r in request['items'] if r['id'] == '2')['source'],
                         {'project_id': '0', 'product_id': '2'})
        self.assertTrue(proof['complete'])

    def test_product_matching_another_project_and_stories_are_included(self):
        self.lists['/products/2/bugs'][0]['project'] = 8
        self.lists['/products/2/stories'] = [{**self.bug, 'id': 20}]
        request, _ = self.collect()
        self.assertIn(('story', '20'), [(r['kind'], r['id']) for r in request['items']])
        self.assertIn('2', [r['id'] for r in request['items']])

    def test_story_product_list_can_omit_project_without_losing_membership_or_duplicating(self):
        project_story = {**self.bug, 'id': 20, 'project': 1}
        product_story = {key: value for key, value in project_story.items() if key != 'project'}
        self.lists['/projects/1/stories'] = [project_story]
        self.lists['/products/2/stories'] = [product_story, {**product_story, 'id': 21}]
        request, _ = self.collect()
        stories = [row for row in request['items'] if row['kind'] == 'story']
        self.assertEqual(len(stories), 2)
        self.assertEqual(stories[0]['source']['project_id'], '1')
        self.assertIsNone(stories[1]['source']['project_id'])

    def test_filters_assignee_status_deleted_and_disabled_type(self):
        self.lists['/products/2/bugs'] += [{**self.bug, 'id': 5, 'assignedTo': 'test-user'},
                                         {**self.bug, 'id': 6, 'status': 'closed'},
                                         {**self.bug, 'id': 7, 'deleted': 1}]
        request, proof = self.collect()
        self.assertCountEqual([r['id'] for r in request['items']], ['2', '3', '4'])
        self.assertTrue(any(p['page'] == 3 for p in proof['pages']))
        self.config['zentao']['accepted_statuses']['bug'] = []
        session = Session(self.lists)
        request, _ = self.collect(session)
        self.assertEqual(request['items'], [])
        self.assertFalse(any('/bugs' in path for path in session.calls))

    def test_bad_ownership_or_conflicting_duplicate_rejects_complete_scan(self):
        self.lists['/products/2/bugs'][0]['product'] = 9
        self.assertIsNotNone(monitor_intake, 'Project-or-product collector is missing')
        with self.assertRaises(ValueError):
            self.collect()
        self.lists['/products/2/bugs'][0]['product'] = 2
        self.lists['/products/2/bugs'][1]['status'] = 'closed'
        with self.assertRaises(ValueError):
            self.collect()

    def test_incomplete_pagination_and_identity_fail_closed(self):
        self.assertIsNotNone(monitor_intake, 'Project-or-product collector is missing')
        class Incomplete(Session):
            def _v2(self, method, url):
                result = super()._v2(method, url)
                if urlsplit(url).path == '/products/2/bugs':
                    result['pager']['recTotal'] = 3
                return result
        with self.assertRaises(ValueError):
            self.collect(Incomplete(self.lists))
        self.lists['/products'][0]['name'] = 'Different product'
        with self.assertRaises(ValueError):
            self.collect()

    def test_product_only_bug_is_persisted_and_detail_rechecked_before_reserve(self):
        self.scan('baseline', [])
        row = {**self.row(2, kind='bug'), 'source': {'project_id': '0', 'product_id': '2'}}
        result = self.scan('product-bug', [row])
        self.assertEqual(result['candidate'], self.item(2, kind='bug'))
        denied = self.reserve(2, item=self.item(2, kind='bug'))
        self.assertEqual(denied['status'], 'source_verification_required')
        source = {'project_id': '0', 'product_id': '2'}
        reserved = self.reserve(2, item=self.item(2, kind='bug'), source=source)
        self.assertEqual(reserved['status'], 'reserved')
        with closing(sqlite3.connect(self.database)) as conn:
            saved = json.loads(conn.execute('SELECT source FROM work_item_sources').fetchone()[0])
        self.assertEqual(saved, source)

    def test_outside_scope_scan_and_reassigned_detail_cannot_reserve(self):
        self.scan('baseline', [])
        row = {**self.row(2, kind='bug'), 'source': {'project_id': '0', 'product_id': '9'}}
        self.assertEqual(self.scan('outside', [row])['status'], 'scope_mismatch')
        row['source']['product_id'] = '2'
        self.scan('inside', [row])
        result = self.reserve(2, item=self.item(2, kind='bug'),
                              source={'project_id': '0', 'product_id': '9'})
        self.assertEqual(result['status'], 'not_eligible')

    def test_removing_product_scope_revokes_product_only_queued_item_before_next_scan(self):
        self.scan('baseline', [])
        source = {'project_id': '0', 'product_id': '2'}
        self.scan('first', [{**self.row(2, kind='bug'), 'source': source}])
        self.config['zentao']['scope_mode'] = 'project'
        self.assertEqual(self.reserve(2, item=self.item(2, kind='bug'), source=source)['status'], 'not_eligible')

    def test_detail_verification_uses_either_current_owner(self):
        self.assertIsNotNone(monitor_intake, 'Project-or-product collector is missing')
        item = self.item(2, kind='bug')
        session = Session(self.lists, {'/bugs/2': {'status': 'success', 'bug': self.bug}})
        detail = monitor_intake.verify_candidate(self.config, session, item)
        self.assertTrue(detail['eligible'])
        self.assertEqual(detail['source'], {'project_id': '0', 'product_id': '2'})
        session.details['/bugs/2']['bug'] = {**self.bug, 'product': 9}
        self.assertFalse(monitor_intake.verify_candidate(self.config, session, item)['eligible'])

    def test_story_detail_can_verify_project_membership_when_product_does_not_match(self):
        self.assertIsNotNone(monitor_intake, 'Project-or-product collector is missing')
        story = {key: value for key, value in {**self.project_bug, 'id': 20}.items() if key != 'project'}
        self.lists['/projects/1/stories'] = [story]
        session = Session(self.lists, {'/stories/20': story})
        detail = monitor_intake.verify_candidate(self.config, session, self.item(20))
        self.assertTrue(detail['eligible'])
        self.assertEqual(detail['source']['project_id'], '1')

    def test_rescanning_finished_or_stopped_item_keeps_original_identity_and_state(self):
        self.scan('baseline', [])
        row = {**self.row(2, kind='bug'), 'source': {'project_id': '1', 'product_id': '2'}}
        self.scan('first', [row])
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("UPDATE work_items SET state='completed',conclusion='主动停跑',chat_id='old-chat'")
            marker = conn.execute('SELECT marker FROM work_items').fetchone()[0]
        row['source']['project_id'] = '0'
        self.assertIsNone(self.scan('second', [row])['candidate'])
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT marker,chat_id,state FROM work_items').fetchall(),
                             [(marker, 'old-chat', 'completed')])

    def test_older_complete_scan_does_not_overwrite_newer_detail_source(self):
        self.scan('baseline', [])
        row = {**self.row(2, kind='bug'), 'source': {'project_id': '1', 'product_id': '2'}}
        self.scan('first', [row])
        self.time += timedelta(seconds=10)
        latest = {'project_id': '8', 'product_id': '2'}
        self.reserve(2, item=self.item(2, kind='bug'), source=latest)
        self.scan('older-scan', [row], since=-5)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(json.loads(conn.execute('SELECT source FROM work_item_sources').fetchone()[0]), latest)

    def test_product_only_worker_uses_original_source(self):
        self.scan('baseline', [])
        row = {**self.row(2, kind='bug'), 'source': {'project_id': '0', 'product_id': '2'}}
        self.scan('first', [row])
        self.reserve(2, item=self.item(2, kind='bug'), source=row['source'])
        with closing(sqlite3.connect(self.database)) as conn:
            conn.row_factory = sqlite3.Row
            item = dict(conn.execute('SELECT * FROM work_items').fetchone())
        from worker import worker_prompt
        with patch('worker.DATABASE_PATH', self.database):
            prompt = worker_prompt('run', item)
        self.assertIn('自动化队列项目', prompt)
        self.assertIn('"project_id":"0"', prompt)

    def test_monitor_cli_incomplete_union_preserves_existing_queue_and_writes_partial_proof(self):
        import monitor
        from workflow import run
        self.scan('baseline', [])
        row = {**self.row(2, kind='bug'), 'source': {'project_id': '0', 'product_id': '2'}}
        self.scan('first', [row])
        config_path = self.fixture / 'config.json'
        config_path.write_text(json.dumps(self.config), encoding='utf-8')
        del self.lists['/products/2/bugs']

        def invoke(command, payload, config, database):
            if payload['action'] == 'runtime_check':
                return {'status': 'runtime_unattended', 'approval_policy': 'never'}
            return run(command, payload, config, database, now=self.time)

        output = io.StringIO()
        with (patch.object(monitor, 'CONFIG_PATH', config_path),
              patch.object(monitor, 'DATABASE_PATH', self.database),
              patch.object(monitor, 'PROJECT_ROOT', self.fixture),
              patch.object(monitor, 'run', side_effect=invoke),
              patch.object(monitor, 'get_remote', return_value=SimpleNamespace(session=Session(self.lists), account='Test-user')),
              patch('sys.argv', ['monitor.py']), redirect_stdout(output)):
            self.assertEqual(monitor.main(), 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result['scan']['complete'])
        self.assertEqual(result['scan']['claim']['status'], 'partial')
        self.assertNotIn('secret', output.getvalue())
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT state,eligible FROM work_items').fetchall(), [('queued', 1)])
            self.assertEqual(conn.execute('SELECT scan_id FROM scans').fetchone()[0], 'first')
