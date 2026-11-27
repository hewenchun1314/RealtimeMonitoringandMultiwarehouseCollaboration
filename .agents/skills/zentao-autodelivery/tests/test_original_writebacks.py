"""Exercise actual authenticated requests, leases and readback, on loopback only."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from ledger import encode, transaction
from outbox import drain_writebacks


class ZenTaoStub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value, headers=()):
        self.send_response(200)
        for key, val in headers:
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(value if isinstance(value, bytes) else json.dumps(value).encode())

    def do_GET(self):
        path, query = urlsplit(self.path).path, parse_qs(urlsplit(self.path).query)
        if self.headers.get('Token') != 'stub-token':
            self.send_error(403)
            return
        if path == '/api.php/v2/files/7/download':
            if self.server.legacy_only:
                self.send_error(404)
                return
            self.reply(self.server.content)
            return
        if path == '/index.php' and query == {'m': ['file'], 'f': ['download'], 'fileID': ['7']}:
            self.server.legacy_reads += 1
            if self.server.uploads_only:
                self.send_error(404)
                return
            self.reply(self.server.content)
            return
        if path == '/data/upload/fixture':
            self.reply(self.server.content)
            return
        kind = 'bug' if '/bugs/' in path else 'story'
        obj = {'id': 4, 'project': self.server.project, 'product': 3,
               'files': self.server.files.get(kind, []), 'deleted': '0'}
        self.reply({kind: obj, 'actions': self.server.actions.get(kind, [])})

    def do_POST(self):
        query = parse_qs(urlsplit(self.path).query)
        body = self.rfile.read(int(self.headers.get('Content-Length') or '0'))
        if self.path == '/api.php/v2/users/login':
            self.reply({'token': 'stub-token', 'user': {'account': 'bot'}})
        elif self.headers.get('Token') != 'stub-token':
            self.send_error(403)
        elif self.path == '/api.php/v2/files':
            self.server.uploads += 1
            parts = body.split(b'\r\n\r\n')
            kind = parts[1].partition(b'\r\n')[0].decode()
            title = body.partition(b'filename="')[2].partition(b'"')[0].decode()
            self.server.content = parts[-1].split(b'\r\n--')[0]
            self.server.files[kind] = [{'id': 7, 'objectID': 4, 'objectType': kind, 'title': title,
                                        'webPath': '/data/upload/fixture'}]
            self.reply({'id': 7})
        elif urlsplit(self.path).path == '/api.php/v2/actions/comment':
            kind = query['objectType'][0]
            self.server.notes += 1
            if self.server.persist:
                self.server.actions[kind] = [{'action': 'commented', 'objectID': 4, 'objectType': kind, 'id': 2,
                                              'actor': self.server.actor, 'comment': json.loads(body)['comment']}]
            # Simulate a dropped response after the server commits a comment.
            self.close_connection = True
        else:
            self.send_error(400)


class OriginalWritebackTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.database = self.root / 'db.sqlite'
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), ZenTaoStub)
        self.server.files, self.server.actions = {}, {}
        self.server.project = 1
        self.server.notes = self.server.uploads = 0
        self.server.persist = True
        self.server.legacy_only, self.server.legacy_reads = False, 0
        self.server.uploads_only = False
        self.server.actor = 'bot'
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.config = {'zentao': {'base_url': self.base, 'project_name': 'Queue'}}
        secret = self.root / 'creds.json'
        secret.write_text(encode({'base_url': self.base, 'username': 'bot', 'password': 'dummy'}))
        self.enterContext(patch('task_remote.CREDENTIAL_PATH', secret))

    def insert(self, kind):
        marker = 'work-' + kind
        with transaction(self.database) as conn:
            conn.execute("INSERT OR IGNORE INTO scans VALUES(?,'1','Queue','scan','2026')", (self.base,))
            seq = conn.execute('''INSERT INTO work_items(instance,project_id,kind,item_id,title,assigned_to,
                source_status,eligible,observed_at,state,marker) VALUES(?,'1',?,'4','t','bot','active',1,
                '2026','completed',?)''', (self.base, kind, marker)).lastrowid
            op = marker + '/1/final'
            conn.execute("INSERT INTO writebacks(operation_id,item_seq,run_round,operation_type,payload) VALUES(?,?,1,'comment',?)",
                         (op, seq, encode({'text': 'A < B & C\nsecond line [' + op + ']'})))
            directory = self.root / 'items' / marker / 'round-1'
            directory.mkdir(parents=True)
            (directory / 'blockers.md').write_bytes(b'Blocked: local verification')
            conn.execute("INSERT INTO writebacks(operation_id,item_seq,run_round,operation_type,payload) VALUES(?,?,1,'attachment',?)",
                         (marker + '/1/blockers', seq, encode({'path': 'blockers.md'})))

    def test_story_and_bug_write_back_once_including_when_post_disconnects(self):
        for kind in ('story', 'bug'):
            self.insert(kind)
        report = drain_writebacks(self.config, self.database)
        self.assertEqual(report['processed'], {'writeback_confirmed': 4}, report)
        self.assertEqual(drain_writebacks(self.config, self.database)['status'], 'idle')
        self.assertEqual((self.server.notes, self.server.uploads), (2, 2))
        with transaction(self.database) as conn:
            for row in conn.execute("SELECT receipt FROM writebacks WHERE operation_type='attachment'"):
                self.assertEqual(json.loads(row['receipt'])['sha256'], hashlib.sha256(self.server.content).hexdigest())

    def test_uncertain_send_is_not_repeated_after_lease_expiry(self):
        self.server.persist = False
        self.insert('story')
        self.assertEqual(drain_writebacks(self.config, self.database)['processed']['needs_readback'], 1)
        with transaction(self.database) as conn:
            conn.execute("UPDATE writebacks SET expires_at='2000' WHERE state='sending'")
        self.assertEqual(drain_writebacks(self.config, self.database)['processed'], {'needs_readback': 1})
        self.assertEqual(self.server.notes, 1)

    def test_wrong_author_cannot_confirm_and_legacy_download_verifies_original_bytes(self):
        self.server.actor = 'someone-else'
        self.server.legacy_only = True
        self.insert('story')
        report = drain_writebacks(self.config, self.database)
        self.assertEqual(report['processed'], {'verification_pending': 1, 'writeback_confirmed': 1}, report)
        self.assertEqual(self.server.legacy_reads, 1)
        self.assertEqual((self.server.notes, self.server.uploads), (1, 1))

    def test_original_bug_file_with_only_a_web_download_path(self):
        self.server.legacy_only = self.server.uploads_only = True
        self.insert('bug')
        report = drain_writebacks(self.config, self.database)
        self.assertEqual(report['processed'], {'writeback_confirmed': 2}, report)

    def test_superseded_entries_cannot_be_reclaimed_or_consume_a_drain_slot(self):
        from datetime import datetime, timezone
        from ledger import Ledger
        from writeback import claim_writeback
        self.insert('bug')
        with transaction(self.database) as conn:
            conn.execute("UPDATE writebacks SET state='superseded' WHERE operation_type='attachment'")
            item = conn.execute('SELECT * FROM work_items').fetchone()
            result = claim_writeback(Ledger(conn, self.config, datetime.now(timezone.utc)), item,
                                     {'operation_id': 'work-bug/1/blockers', 'owner': 'other'})
        self.assertEqual(result['status'], 'writeback_inactive')
        self.assertEqual(drain_writebacks(self.config, self.database, limit=1)['processed'], {'writeback_confirmed': 1})
        self.assertEqual((self.server.notes, self.server.uploads), (1, 0))
        self.assertEqual(drain_writebacks(self.config, self.database)['status'], 'idle')

    def test_moved_item_is_never_written_and_transient_read_error_can_retry(self):
        self.insert('bug')
        self.server.project = 7
        self.assertEqual(drain_writebacks(self.config, self.database)['processed'], {'verification_pending': 2})
        self.assertEqual((self.server.notes, self.server.uploads), (0, 0))
        self.server.project = 1
        with transaction(self.database) as conn:
            conn.execute("UPDATE writebacks SET expires_at='2000'")
        self.assertEqual(drain_writebacks(self.config, self.database)['processed'], {'writeback_confirmed': 2})
