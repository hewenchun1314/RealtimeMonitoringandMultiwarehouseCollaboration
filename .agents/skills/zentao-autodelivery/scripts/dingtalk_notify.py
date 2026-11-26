"""DingTalk notifications queued by work-item state transitions."""

import argparse
import base64
from datetime import datetime, timedelta, timezone
from getpass import getpass
import hmac
from http.client import HTTPException
import json
from pathlib import Path
import re
from socket import gaierror
import sqlite3
import subprocess
import sys
from time import time_ns
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ledger import stamp, transaction
from task_remote import _dpapi


PROJECT_ROOT = Path(__file__).resolve().parents[4]
CONFIG = PROJECT_ROOT / '.agents' / 'zentao-project.json'
DATABASE = PROJECT_ROOT / '.local' / 'zentao-delivery' / 'workflow.sqlite3'
CONCLUSIONS = {
    '待补材料': '请查看禅道原单，按收尾备注补充材料。',
    '待验证': '请查看禅道原单中的待验证项。',
    '阻断': '请查看禅道原单中的阻断原因。',
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect())


def enabled(config):
    return config.get('notifications', {}).get('dingtalk', {}).get('enabled') is True


def enqueue_notification(store, item, result):
    started = result['status'] == 'started'
    event_type = 'started' if started else 'closed'
    operation = 'started' if started else f'closed/{item["run_round"]}'
    headline = '已接单' if started else result['conclusion']
    title = ' '.join(item['title'].split())[:160]
    kind = '需求' if item['kind'] == 'story' else '缺陷'
    local_time = store.now.astimezone().strftime('%Y-%m-%d %H:%M %z')
    body = f'【{headline}】禅道{kind} #{item["item_id"]} {title}\n{local_time}'
    details = (result.get('task_summary'), CONCLUSIONS.get(headline))
    for detail in details:
        if detail:
            body += '\n' + detail
    store.conn.execute('''INSERT INTO dingtalk_notifications
        (operation_id, item_seq, event_type, body, available_at) VALUES (?,?,?,?,?)
        ON CONFLICT(operation_id) DO NOTHING''',
        (f'{item["marker"]}/{operation}', item['seq'], event_type, body, store.time))


def validate_credentials(webhook, signing_secret):
    try:
        parts = urlsplit(webhook)
        query = parse_qs(parts.query, strict_parsing=True)
    except ValueError:
        raise ValueError('钉钉 Webhook 地址格式不正确') from None
    tokens = query.get('access_token', [])
    if (parts.scheme != 'https' or parts.netloc != 'oapi.dingtalk.com' or parts.path != '/robot/send'
            or parts.fragment or set(query) != {'access_token'} or len(tokens) != 1
            or not re.fullmatch(r'[A-Za-z0-9_-]{16,}', tokens[0])):
        raise ValueError('请使用钉钉群机器人的 HTTPS Webhook 地址')
    if not signing_secret.startswith('SEC') or len(signing_secret) <= 3 or any(c.isspace() for c in signing_secret):
        raise ValueError('请使用「加签」下方以 SEC 开头的密钥')
    return parts, tokens[0]


def send_message(webhook, signing_secret, body):
    parts, token = validate_credentials(webhook, signing_secret)
    timestamp = str(time_ns() // 1_000_000)
    signature = hmac.digest(signing_secret.encode(), f'{timestamp}\n{signing_secret}'.encode(), 'sha256')
    query = urlencode({'access_token': token, 'timestamp': timestamp, 'sign': base64.b64encode(signature).decode()})
    data = json.dumps({'msgtype': 'text', 'text': {'content': body}}, ensure_ascii=False).encode()
    request = Request(urlunsplit(parts._replace(query=query)), data=data,
                      headers={'Content-Type': 'application/json; charset=utf-8'})
    try:
        with OPENER.open(request, timeout=5) as response:
            receipt = json.loads(response.read(8192))
    except HTTPError as error:
        error.close()
        if error.code == 429:
            return 'retry'
        return 'rejected' if 400 <= error.code < 500 else 'unknown'
    except URLError as error:
        return 'retry' if isinstance(error.reason, (ConnectionRefusedError, gaierror)) else 'unknown'
    except (OSError, HTTPException, ValueError, RecursionError):
        return 'unknown'
    if not isinstance(receipt, dict) or type(receipt.get('errcode')) is not int:
        return 'unknown'
    if receipt['errcode'] == 0:
        return 'sent'
    # 钉钉群机器人明确返回限流时可以重试。
    return 'retry' if receipt['errcode'] == 130101 else 'rejected'


def credential_path(database):
    return database.with_name('dingtalk.dpapi')


def read_credentials(database):
    record = json.loads(_dpapi(credential_path(database).read_bytes(), protect=False))
    webhook, signing_secret = record['webhook'], record['signing_secret']
    validate_credentials(webhook, signing_secret)
    return webhook, signing_secret


def claim_notification(database, now):
    with transaction(database) as conn:
        conn.execute("""UPDATE dingtalk_notifications SET state='unknown'
            WHERE state='sending' AND available_at<=?""", (stamp(now),))
        row = conn.execute("""SELECT n.* FROM dingtalk_notifications n
            WHERE n.state='pending' AND n.available_at<=? AND NOT EXISTS (
                SELECT 1 FROM dingtalk_notifications prior
                WHERE prior.item_seq=n.item_seq AND prior.rowid<n.rowid
                    AND prior.state IN ('sending','pending')
            ) ORDER BY n.rowid LIMIT 1""", (stamp(now),)).fetchone()
        if row:
            conn.execute("""UPDATE dingtalk_notifications SET state='sending', attempts=attempts+1,
                available_at=? WHERE operation_id=?""",
                (stamp(now + timedelta(minutes=2)), row['operation_id']))
        return row


def drain(database, config, *, now=None, limit=3):
    if not enabled(config) or not database.is_file():
        return {'status': 'idle'}
    try:
        credentials = read_credentials(database)
    except FileNotFoundError:
        return {'status': 'credentials_missing'}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {'status': 'credentials_invalid'}
    counts = {}
    try:
        for _ in range(limit):
            current = now or datetime.now(timezone.utc)
            row = claim_notification(database, current)
            if row is None:
                break
            result = send_message(*credentials, row['body'])
            state = result
            if result == 'retry':
                state = 'pending' if row['attempts'] < 3 else 'rejected'
            next_at = stamp((now or datetime.now(timezone.utc)) + timedelta(minutes=2))
            with transaction(database) as conn:
                conn.execute("""UPDATE dingtalk_notifications SET state=?, available_at=?
                    WHERE operation_id=? AND state='sending'""", (state, next_at, row['operation_id']))
            counts[state] = counts.get(state, 0) + 1
    except (OSError, sqlite3.Error, ValueError):
        return {'status': 'local_error', 'processed': counts}
    return {'status': 'checked', 'processed': counts}


def dispatch():
    try:
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), 'deliver'],
                                timeout=18, capture_output=True, check=True)
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return {'status': 'worker_timeout'}
    except (subprocess.CalledProcessError, OSError, ValueError):
        return {'status': 'worker_error'}


def main():
    parser = argparse.ArgumentParser(description='在本机配置钉钉接单通知凭据或发送一条测试消息')
    parser.add_argument('operation', choices=('configure', 'test', 'deliver'))
    args = parser.parse_args()
    if args.operation == 'deliver':
        config = json.loads(CONFIG.read_text(encoding='utf-8-sig'))
        print(json.dumps(drain(DATABASE, config)))
        return 0
    if args.operation == 'configure':
        if not sys.stdin.isatty():
            print('请在本机交互式 PowerShell 窗口运行 configure。', file=sys.stderr)
            return 1
        try:
            webhook = getpass('钉钉 Webhook 地址（隐藏输入）：').strip()
            signing_secret = getpass('钉钉加签密钥（隐藏输入）：').strip()
            validate_credentials(webhook, signing_secret)
            encrypted = _dpapi(json.dumps({'webhook': webhook, 'signing_secret': signing_secret}).encode(),
                               protect=True)
            destination = credential_path(DATABASE)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with NamedTemporaryFile(dir=destination.parent, delete=False) as output:
                    temporary = Path(output.name)
                    output.write(encrypted)
                temporary.replace(destination)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        except (OSError, ValueError, EOFError, KeyboardInterrupt):
            print('凭据配置失败。请检查 Webhook 格式、加签密钥及本机存储权限。', file=sys.stderr)
            return 1
        print('钉钉凭据已加密保存在本机。')
        return 0
    try:
        status = send_message(*read_credentials(DATABASE), '【测试通知】禅道自动接单 · 钉钉渠道连通性测试。')
    except (OSError, TypeError, ValueError, KeyError, AttributeError):
        status = 'credentials_unavailable'
    print(json.dumps({'status': status}))
    return int(status != 'sent')


if __name__ == '__main__':
    raise SystemExit(main())
