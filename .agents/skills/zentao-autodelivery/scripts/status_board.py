"""Publish a local, read-only snapshot of the formal demand queue."""

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from html import escape
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlencode

from ledger import STAGE_NAMES


SECTIONS = (
    ('执行中', ('reserved', 'bound', 'running', 'closing')),
    ('排队中', ('queued', 'dispatch_uncertain')),
    ('已收尾', ('completed',)),
)
STATES = {
    'reserved': '已领取', 'bound': '待启动', 'running': '执行中', 'closing': '收尾中',
    'queued': '排队中', 'dispatch_uncertain': '待核对派发', 'completed': '已收尾',
}


def local_time(value):
    if not value:
        return '—'
    return datetime.fromisoformat(value).astimezone().strftime('%Y-%m-%d %H:%M:%S %z')


def table_row(item, base_url):
    url = f'{base_url}/index.php?' + urlencode({'m': 'story', 'f': 'view', 'storyID': item['item_id']})
    label = escape(item['title'] or '查看禅道需求')
    status = item['conclusion'] or STATES[item['state']]
    if item['state'] == 'queued' and item['dispatch_state'] == 'runtime_blocked':
        status = '待核验无人值守权限'
    progress = STAGE_NAMES.get(item['stage'], '—')
    if item['retry_number'] is not None:
        progress += f'（第 {item["retry_number"] + 1} 次尝试）'
    pending = str(item['pending']) if item['pending'] else '—'
    return (
        f'<tr><td><a href="{escape(url, quote=True)}">#{escape(item["item_id"])} {label}</a></td>'
        f'<td>{escape(status)}</td><td>{escape(progress)}</td><td>{pending}</td></tr>'
    )


def publish_status(database, config):
    """Read one consistent SQLite snapshot and replace the HTML file atomically."""
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=3)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        scope = (config['zentao']['base_url'].rstrip('/'), config['zentao']['project_name'])
        scan = conn.execute('SELECT project_id,observed_at FROM scans WHERE instance=? AND project_name=?',
                            scope).fetchone()
        rows = [] if scan is None else conn.execute('''
            SELECT w.*, a.retry_number, d.state AS dispatch_state,
                   (SELECT COUNT(*) FROM writebacks b WHERE b.item_seq=w.seq AND b.state<>'confirmed') AS pending
            FROM work_items w
            LEFT JOIN attempts a ON a.item_seq=w.seq AND a.outcome IS NULL
            LEFT JOIN dispatches d ON d.request_id=w.request_id
            WHERE w.instance=? AND w.project_id=? AND w.kind='story'
            ORDER BY w.priority IS NULL, w.priority, w.seq''', (scope[0], scan['project_id'])).fetchall()
    sections = []
    for title, states in SECTIONS:
        selected = [item for item in rows if item['state'] in states]
        content = ''.join(table_row(row, scope[0]) for row in selected)
        sections.append(
            f'<section><h2>{title} <span>{len(selected)}</span></h2>' + (
                f'<table><thead><tr><th>禅道需求</th><th>状态</th><th>当前步骤</th><th>待回写</th></tr></thead>'
                f'<tbody>{content}</tbody></table>' if content else '<p>暂无需求</p>'
            ) + '</section>'
        )
    title = escape(config['zentao']['project_name'])
    last_scan = local_time(scan['observed_at'] if scan else None)
    enabled = '已开启' if config['monitor']['enabled'] else '已关闭'
    html = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30"><title>{title} · 需求进度</title>
<style>
body{{font:16px/1.55 system-ui,sans-serif;background:#f2f5f6;color:#183039;margin:0}}
main{{max-width:1080px;margin:auto;padding:32px 20px}}h1{{font-size:28px;margin:0 0 10px}}
header p,section p{{color:#52666e}}section{{background:#fff;border:1px solid #d9e3e6;border-radius:12px;padding:18px;margin:16px 0;overflow:auto}}
h2{{font-size:19px;margin:0 0 12px}}h2 span{{background:#e3efee;color:#145851;border-radius:12px;padding:2px 8px}}
table{{border-collapse:collapse;width:100%;text-align:left}}th,td{{padding:10px;border-bottom:1px solid #e5ecee}}a{{color:#005b66}}
</style></head><body><main><header><h1>{title} · 需求进度</h1>
<p>接单{enabled} · 最近一次完整扫描 {last_scan}</p></header>{"".join(sections)}</main></body></html>
'''
    destination = database.parent / 'status.html'
    with NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination.parent, delete=False) as tmp:
        temporary = Path(tmp.name)
        tmp.write(html)
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[4]
    config = json.loads((root / '.agents/zentao-project.json').read_text(encoding='utf-8-sig'))
    print(publish_status(root / '.local/zentao-delivery/workflow.sqlite3', config))
