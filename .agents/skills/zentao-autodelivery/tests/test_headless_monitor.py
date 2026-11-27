"""Exercise baseline, fresh admission and stable dispatch without a desktop chat."""
import concurrent.futures
import json
import io
from contextlib import redirect_stdout
import os
import sqlite3
import subprocess
import sys
from http.server import ThreadingHTTPServer
import threading
from contextlib import closing
from unittest.mock import patch
from types import SimpleNamespace

from test_workflow import WorkflowFixture
from test_monitor_intake import Session
from test_original_writebacks import ZenTaoStub
from portable_lock import file_lock
import monitor
from dispatch_monitor import dispatch_one
from workflow import run


class HeadlessMonitorTests(WorkflowFixture):
    def test_first_scan_skips_history_and_next_scan_claims_once(self):
        cfg = self.config
        cfg["e2e_attestation"] = "launch"
        cfg['runtime'].pop('desktop_project_id', None)
        cfg['runtime']['sync_requirement_before_review'] = False
        cfg['zentao'].update(project_name='Project', scope_mode='project')
        cfg['zentao']['accepted_statuses'] = {'story': ['reviewing'], 'bug': []}
        cfg['monitor']['enabled'] = True
        cfg['zentao']['assigned_to_accounts'] = ['Test-user']
        row = {'id': 1, 'title': 'test only', 'status': 'reviewing', 'project': 1,
               'product': 2, 'pri': 1, 'assignedTo': 'Test-user'}
        rows = [row]
        session = Session({'/projects': [{'id': 1, 'name': 'Project'}], '/projects/1/stories': rows},
                          {'/stories/2': {**row, 'id': 2}})
        with (patch.object(monitor, 'DATABASE_PATH', self.database),
              patch.object(monitor, 'get_remote', return_value=SimpleNamespace(session=session, account='Test-user')),
              patch.object(monitor, 'schedule_workers', return_value=[])):
            baseline = monitor.scan_once(cfg, dispatch=True)
            self.assertEqual(baseline['scan']['claim']['status'], 'baseline')
            # A transient detail failure on the first new item must not starve the next one.
            rows.extend([{**row, 'id': 3}, {**row, 'id': 2}])
            session.details['/stories/2'].update(spec='docs/REQ-2.md', verify='open the dashboard',
                                                token='private', files={'8': {'id': 8, 'title': 'design.md',
                                                                               'download': 'private'}})
            admitted = monitor.scan_once(cfg, dispatch=True)
            self.assertEqual([r['status'] for r in admitted['admissions']], ['detail_unverified', 'reserved'])
            marker = admitted['admissions'][1]['marker']
            material = monitor.read_materials(cfg, self.database, marker)
            self.assertEqual(material['spec'], 'docs/REQ-2.md')
            self.assertEqual(material['verify'], 'open the dashboard')
            self.assertEqual(material['files'], [{'id': '8', 'title': 'design.md'}])
            self.assertNotIn('private', json.dumps(material))
            monitor.scan_once(cfg, dispatch=True)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM dispatches').fetchone(), (1,))
            snap, = conn.execute("SELECT config_snapshot FROM work_items WHERE item_id='2'").fetchone()
            self.assertNotIn('test-password', snap)
            self.assertEqual(json.loads(snap)['e2e_attestation'], 'launch')
            self.assertEqual(json.loads(snap)['zentao']['project_name'], 'Project')


    def test_cli_logs_in_with_local_file_and_claims_from_a_separate_process(self):
        story = {'id': 17, 'title': 'Portable intake', 'project': 1, 'product': 2,
                 'status': 'active', 'assignedTo': 'Test-user', 'spec': 'requirements/test/intent.md'}
        rows = []
        api = Session({'/projects': [{'id': 1, 'name': 'Sample'}], '/projects/1/stories': rows},
                      {'/stories/17': story})

        class IntakeServer(ZenTaoStub):
            def do_GET(self):
                if self.headers.get('Token') != 'stub-token' or not self.path.startswith('/api.php/v2/'):
                    self.send_error(403)
                    return
                self.reply(api._v2('GET', self.path.removeprefix('/api.php/v2')))

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path != '/api.php/v2/users/login' or payload != {'account': 'Test-user', 'password': 'fixture-only'}:
                    self.send_error(403)
                    return
                self.reply({'token': 'stub-token', 'user': {'account': 'Test-user'}})

        server = ThreadingHTTPServer(('127.0.0.1', 0), IntakeServer)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:' + str(server.server_port)
        self.config['zentao'].update(base_url=base, accepted_statuses={'story': ['active'], 'bug': []})
        self.config['runtime'].pop('desktop_project_id')
        file = self.fixture / '.agents/zentao-project.json'
        file.parent.mkdir()
        file.write_text(json.dumps(self.config), encoding='utf-8')
        credentials = self.fixture / '.local/zentao-credentials.json'
        credentials.parent.mkdir()
        credentials.write_text(json.dumps({'base_url': base, 'username': 'Test-user', 'password': 'fixture-only'}),
                               encoding='utf-8')

        def invoke(*args):
            env = {**os.environ, 'ZENTAO_DELIVERY_CONFIG': str(file), 'ZENTAO_DELIVERY_DATABASE': str(self.database)}
            output = subprocess.run([sys.executable, '-B', monitor.__file__, *args], cwd=self.fixture,
                                    env=env, capture_output=True, timeout=20, check=True).stdout
            self.assertNotIn(b'fixture-only', output)
            return json.loads(output)

        self.assertEqual(invoke('--once')['scan']['claim']['status'], 'baseline')
        rows.append(story)
        self.assertEqual(invoke()['scan']['claim']['candidate']['id'], '17')
        with closing(sqlite3.connect(self.database)) as conn:
            marker, = conn.execute('SELECT marker FROM work_items').fetchone()
        # Stand in for an existing host. Real Codex must never start in this offline test.
        with file_lock(self.database.parent / 'items' / marker / 'monitor.lock') as acquired:
            self.assertTrue(acquired)
            result = invoke('--once')
            self.assertEqual(result['admissions'][0]['status'], 'reserved')
            self.assertEqual(result['workers'], [])
            self.assertEqual(invoke('--once')['admissions'], [])
        self.assertEqual(invoke('--materials', marker)['materials']['spec'], story['spec'])

    def test_existing_work_is_serviced_even_when_intake_fails(self):
        config_path = self.fixture / 'config.json'
        config_path.write_text(json.dumps(self.config), encoding='utf-8')
        with (patch.object(monitor, 'CONFIG_PATH', config_path),
              patch.object(monitor, 'scan_once', side_effect=ValueError('invalid detail')),
              patch.object(monitor, 'service_existing', return_value={'workers': []}) as maintenance,
              redirect_stdout(io.StringIO())):
            self.assertEqual(monitor.main(['--once']), 1)
        maintenance.assert_called_once()

    def test_two_dispatchers_open_one_chat_then_resume_same_binding_after_restart(self):
        self.config['runtime'].pop('desktop_project_id')
        self.config['runtime']['dispatch_lease_seconds'] = 300
        from datetime import datetime, timezone
        self.time = datetime.now(timezone.utc)
        self.scan('initial', [])
        self.scan('arrival', [self.row(2)])
        request = self.reserve(2, desktop_project_id=None)
        self.assertEqual(request['status'], 'reserved')
        entered, proceed = threading.Event(), threading.Event()
        chat = '6c52377c-2c61-48e4-8a57-0b15686738da'
        calls = []

        def worker(operation, marker, cli):
            calls.append(operation)
            if operation == 'prepare':
                entered.set()
                if not proceed.wait(5):
                    raise AssertionError('Lost synchronization between dispatchers')
                return {'status': 'worker_ready', 'chat_id': chat}
            with closing(sqlite3.connect(self.database)) as conn:
                binding, = conn.execute('SELECT chat_id FROM work_items WHERE marker=?', (marker,)).fetchone()
            self.assertEqual(binding, chat)
            result = run('record', {'action': 'start', 'item': self.item(2),
                                    'request_id': request['request_id'], 'chat_id': chat,
                                    'event_id': 'execution-' + str(len(calls))}, self.config, self.database)
            self.assertIn(result['status'], ('started', 'already_started'))
            return result

        args = self.config, self.database, request['marker'], request['request_id']
        with (patch('worker.resolve_cli', return_value='fixture-cli'), patch('worker.run', side_effect=worker),
              concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool):
            first = pool.submit(dispatch_one, *args)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(dispatch_one(*args)['status'], 'worker_busy')
            finally:
                proceed.set()
            self.assertEqual(first.result()['status'], 'started')
            self.assertEqual(dispatch_one(*args)['status'], 'already_started')
        self.assertEqual(calls, ['prepare', 'run', 'run'])
