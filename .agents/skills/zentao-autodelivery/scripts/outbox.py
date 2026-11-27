"""Deliver each committed stage note and blocker attachment to its original Story or Bug."""

import html
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urlencode, urlsplit, urljoin
from urllib.request import Request
from uuid import uuid4

from intake_scope import matches_source, source_of
from ledger import Ledger, transaction
from monitor_intake import pages
from task_remote import ZenTaoError, get_remote
from writeback import authorize_writeback, claim_writeback, confirm_writeback


def rows(value):
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError('Cannot verify complete ZenTao history or file list')
    return value


def attachment(database, item, payload):
    items = (database.parent / 'items').resolve()
    directory = (items / item['marker']).resolve()
    if directory.parent != items:
        raise ValueError('Invalid item evidence directory')
    path = Path(payload['path'])
    if not path.is_absolute():
        if len(path.parts) == 1:
            path = directory / f'round-{item["run_round"]}' / path
        elif path.parts[0] == f'round-{item["run_round"]}':
            path = directory / path
        else:
            path = Path(__file__).resolve().parents[4] / path
    path = path.resolve(strict=True)
    if path.name != 'blockers.md' or not path.is_relative_to(directory) or not path.is_file():
        raise ValueError('Expected blockers.md inside the original item directory')
    return path.read_bytes()


class OriginalItemRemote:
    def __init__(self, config):
        self.cfg = config['zentao']
        self.base = self.cfg['base_url'].rstrip('/')
        self.api = get_remote(config).session
        self.account = self.api.login_status.username

    def detail(self, item):
        kind = item['kind']
        plural = {'story': 'stories', 'bug': 'bugs'}[kind]
        reply = self.api._v2('GET', f'/{plural}/{int(item["item_id"])}')
        obj = reply.get(kind, reply)
        if (item['instance'].rstrip('/') != self.base or str(obj.get('id')) != item['item_id']
                or str(obj.get('deleted', 0)) not in ('0', 'False')):
            raise ValueError('Unexpected original item')
        source = source_of(obj, project_optional=kind == 'story')
        if not matches_source(self.cfg, item['project_id'], source):
            if kind != 'story' or source['project_id'] is not None:
                raise ValueError('Original item left configured scope')
            members = pages(self.api, f'/projects/{item["project_id"]}/stories', 'stories', [])
            if not any(str(row.get('id')) == item['item_id'] for row in members):
                raise ValueError('Original Story has no confirmed project association')
        return obj, reply

    def match(self, item, op, kind, body, filename):
        obj, reply = self.detail(item)
        data = obj.get('files') if kind == 'attachment' else reply.get('actions', obj.get('actions'))
        if data is None and kind == 'attachment':
            data = reply.get('files')
        matches = []
        for row in rows(data):
            if row.get('objectType', item['kind']) != item['kind'] or str(row.get('objectID', item['item_id'])) != item['item_id']:
                raise ValueError('Readback included a different original item')
            if kind == 'comment':
                comment = row.get('comment') or ''
                if f'[{op}]' not in html.unescape(comment):
                    continue
                actor = row.get('actor')
                if isinstance(actor, dict):
                    actor = actor.get('account')
                if row.get('action') != 'commented' or ('actor' in row and actor != self.account):
                    raise ValueError('Comment marker belongs to an unexpected action or account')
                plain = re.sub(r'(?i)<br\s*/?>[ \t]*(?:\r?\n)?', '\n', comment)
                plain = html.unescape(re.sub(r'<[^<>]+>', '', plain)).replace('\r\n', '\n').strip()
                if plain != body.replace('\r\n', '\n').strip():
                    raise ValueError('Existing comment marker has different text')
            elif not (row.get('name') == filename or row.get('title') in (filename, Path(filename).stem)):
                continue
            matches.append(row)
        if not matches:
            return None
        if len(matches) != 1 or int(matches[0]['id']) <= 0:
            raise ValueError('Ambiguous writeback readback')
        identity = int(matches[0]['id'])
        receipt = {'operation_id': op, 'object_type': item['kind'], 'object_id': item['item_id']}
        if kind == 'comment':
            return {**receipt, 'action_id': identity}
        request = Request(f'{self.api.api_v2}/files/{identity}/download', headers={'Token': self.api.token})
        status, _, received = self.api._open(request)
        if status in (404, 405, 501):
            query = urlencode({'m': 'file', 'f': 'download', 'fileID': identity})
            request = Request(f'{self.base}/index.php?{query}', headers={'Token': self.api.token})
            status, _, received = self.api._open(request)
            # Some ZenTao installs expose attached Bug files only through
            # the upload path reported by the original item's file row.
            path = matches[0].get('webPath')
            if (status != 200 or received != body) and isinstance(path, str) and path.startswith('/data/upload/'):
                url = urljoin(self.base + '/', path)
                source, target = urlsplit(self.base), urlsplit(url)
                if (source.scheme, source.netloc) == (target.scheme, target.netloc) and target.path.startswith('/data/upload/'):
                    status, _, received = self.api._open(Request(url, headers={'Token': self.api.token}))
        if status != 200 or received != body:
            raise ValueError('Attachment content could not be verified')
        return {**receipt, 'file_id': identity, 'sha256': sha256(body).hexdigest()}

    def send(self, item, kind, body, filename):
        target = {'objectType': item['kind'], 'objectID': item['item_id']}
        if kind == 'comment':
            content = html.escape(body).replace('\r\n', '\n').replace('\n', '<br>')
            self.api._v2('POST', '/actions/comment?' + urlencode(target),
                         {'comment': '<p>' + content + '</p>'})
            return
        boundary = uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
                 for key, value in target.items()]
        parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                      'Content-Type: text/markdown\r\n\r\n'.encode(), body, f'\r\n--{boundary}--\r\n'.encode()])
        self.api._send('POST', self.api.api_v2 + '/files', raw_body=b''.join(parts),
                       content_type='multipart/form-data; boundary=' + boundary)


def _deliver(config, database, remote, job):
    op, item, kind, payload, claim = job
    keys = {'operation_id': op, 'lease_id': claim['lease_id']}
    filename = sha256(op.encode()).hexdigest() + '-blockers.md'
    body = payload['text'] if kind == 'comment' else attachment(database, item, payload)
    found = remote.match(item, op, kind, body, filename)
    if not found:
        if claim['reconcile_required']:
            return 'needs_readback'
        with transaction(database) as conn:
            store = Ledger(conn, config, datetime.now(timezone.utc))
            if authorize_writeback(store, item, keys)['status'] != 'send_authorized':
                return 'lease_changed'
        try:
            remote.send(item, kind, body, filename)
        except (OSError, ValueError, ZenTaoError):
            # The server may already have applied it, even after a client timeout.
            pass
        found = remote.match(item, op, kind, body, filename)
        if not found:
            return 'needs_readback'
    with transaction(database) as conn:
        store = Ledger(conn, config, datetime.now(timezone.utc))
        return confirm_writeback(store, item, {**keys, 'receipt': found})['status']


def drain_writebacks(config, database, *, limit=32, remote_factory=OriginalItemRemote):
    """Drain a bounded set. Expired sends are read back; ambiguous writes never auto-repeat."""
    database = Path(database)
    if not database.is_file():
        return {'status': 'idle', 'processed': {}}
    scope = (config['zentao']['base_url'].rstrip('/'), config['zentao']['project_name'])
    with transaction(database) as conn:
        store = Ledger(conn, config, datetime.now(timezone.utc))
        candidates = conn.execute('''SELECT b.operation_id,b.item_seq FROM writebacks b
            JOIN work_items w ON w.seq=b.item_seq
            JOIN scans s ON s.instance=w.instance AND s.project_id=w.project_id
            WHERE s.instance=? AND s.project_name=? AND w.kind IN ('story','bug')
            AND b.operation_type IN ('comment','attachment') AND b.state IN ('pending','leased','sending')
            AND (b.expires_at IS NULL OR b.expires_at<=?)
            ORDER BY b.expires_at, CASE b.operation_type WHEN 'attachment' THEN 0 ELSE 1 END,b.rowid LIMIT ?''',
                                  (*scope, store.time, limit)).fetchall()
    if not candidates:
        return {'status': 'idle', 'processed': {}}
    try:
        remote = remote_factory(config)
    except (OSError, ValueError, ZenTaoError):
        return {'status': 'remote_unavailable', 'processed': {}}
    counts = {}
    pending = []
    for op, seq in candidates:
        with transaction(database) as conn:
            store = Ledger(conn, config, datetime.now(timezone.utc))
            entry = conn.execute('SELECT * FROM writebacks WHERE operation_id=?', (op,)).fetchone()
            item = dict(conn.execute('SELECT * FROM work_items WHERE seq=?', (seq,)).fetchone())
            item['run_round'] = entry['run_round']
            claim = claim_writeback(store, item, {'owner': 'project-monitor', 'operation_id': op})
            if claim['status'] != 'writeback_claimed':
                continue
            job = op, item, entry['operation_type'], json.loads(entry['payload']), claim
        try:
            result = _deliver(config, database, remote, job)
        except (OSError, ValueError, KeyError, TypeError, ZenTaoError):
            result = 'verification_pending'
        if result in ('needs_readback', 'verification_pending'):
            reason = ('上次发送结果不明，等待读回原单历史；核实前不重发'
                      if result == 'needs_readback' else '暂未核实目标工单、回写内容或远端连接，下一轮重查')
            with transaction(database) as conn:
                conn.execute('''UPDATE writebacks SET last_error=? WHERE operation_id=? AND lease_id=?
                                AND state IN ('leased','sending')''', (reason, op, claim['lease_id']))
            pending.append({'operation_id': op, 'reason': reason})
        counts[result] = counts.get(result, 0) + 1
    return {'status': 'checked', 'processed': counts, 'pending': pending}
