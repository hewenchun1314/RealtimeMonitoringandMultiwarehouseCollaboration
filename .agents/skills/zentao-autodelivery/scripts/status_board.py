"""Publish a local, read-only snapshot of the formal demand queue."""

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlencode

from ledger import Ledger, STAGE_NAMES
from route_evidence import delivery_summary
from route_execution import read_history


SECTIONS = (
    ('执行中', ('reserved', 'bound', 'running', 'closing', 'review_handoff')),
    ('等待禅道评审', ('review_wait', 'review_ready')),
    ('排队中', ('queued', 'dispatch_uncertain')),
    ('已收尾', ('completed',)),
)
STATES = {
    'reserved': '已领取', 'bound': '待启动', 'running': '执行中', 'closing': '收尾中',
    'queued': '排队中', 'dispatch_uncertain': '待核对派发', 'completed': '已收尾',
    'review_wait': '等待禅道评审', 'review_ready': '评审已核对，待续接',
    'review_handoff': '等待评审执行交接',
}


def local_time(value):
    if not value:
        return '—'
    return datetime.fromisoformat(value).astimezone().strftime('%Y-%m-%d %H:%M:%S %z')


def task_progress(tasks, base_url):
    if not tasks:
        return '—'
    delivered = sum(task['desired'] == 'closed' for task in tasks)
    closed = sum(task['remote_status'] == 'closed' for task in tasks)
    details = []
    for task in tasks:
        task_id = task['remote_id']
        link = '待建单'
        if task_id:
            url = base_url + '/index.php?' + urlencode({'m': 'task', 'f': 'view', 'taskID': task_id})
            link = f'<a href="{escape(url, quote=True)}">#{escape(str(task_id))}</a>'
        status = task['remote_status'] or '待建单'
        pending = ''
        if task_id and task['desired'] == 'closed' and status != 'closed':
            pending = ' · 待关闭'
        elif task_id and task['desired'] == 'open' and status == 'closed':
            pending = ' · 待重开'
        description = json.loads(task['definition']).get('description', '')
        remote = {'closed': '已关闭', 'done': '已完成', 'wait': '未开始', 'doing': '进行中'}.get(status, status)
        state = '待交付'
        if task['desired'] == 'closed':
            state = '已合回，页面验收已跳过' if task.get('e2e_skipped') else '已验收并合回'
        reason = task.get('evidence_reason') or ''
        error = task['last_error'] or ''
        side = '前端' if task['side'] == 'frontend' else '后端'
        fields = [task['task_key'], side, description, state, reason, '迭代 ' + str(task['execution_id'] or '待定'),
                  '指派 ' + (task['assignee'] or '待定'), remote + pending, error]
        details.append('<li>' + link + ' · ' + ' · '.join(escape(str(part)) for part in fields if part) + '</li>')
    return (f'<details><summary>研发 {delivered}/{len(tasks)} · 禅道已关闭 {closed}/{len(tasks)}</summary>'
            f'<ul>{"".join(details)}</ul></details>')


def table_row(item, base_url, delivery='', tasks=()):
    url = f'{base_url}/index.php?' + urlencode({'m': 'story', 'f': 'view', 'storyID': item['item_id']})
    label = escape(item['title'] or '查看禅道需求')
    status = item['conclusion'] or STATES[item['state']]
    if item['state'] == 'queued' and item['dispatch_state'] == 'runtime_blocked':
        status = '待核验无人值守权限'
    progress = STAGE_NAMES.get(item['stage'], '—')
    if item['state'] in ('review_wait', 'review_ready') and item['review_receipt']:
        review = json.loads(item['review_receipt'])
        if review.get('progress') == 'waiting_votes':
            progress = '已提交通过，等待其他评审结果'
        elif review.get('reason'):
            progress = review['reason']
    if item['retry_number'] is not None:
        progress += f'（第 {item["retry_number"] + 1} 次尝试）'
        if item['phase'] and item['phase'] != 'verify':
            progress += ' → ' + STAGE_NAMES.get(item['phase'], item['phase'])
    pending = str(item['pending']) if item['pending'] else '—'
    return (
        f'<tr><td><a href="{escape(url, quote=True)}">#{escape(item["item_id"])} {label}</a></td>'
        f'<td>{escape(status)}</td><td>{escape(progress)}</td><td>{escape(delivery) or "—"}</td>'
        f'<td>{task_progress(tasks, base_url)}</td><td>{pending}</td></tr>'
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
            SELECT w.*, a.retry_number, a.phase, d.state AS dispatch_state,
                   (SELECT b.receipt FROM writebacks b WHERE b.item_seq=w.seq AND b.run_round=w.run_round
                    AND b.operation_type='story_review' LIMIT 1) AS review_receipt,
                   (SELECT COUNT(*) FROM writebacks b WHERE b.item_seq=w.seq
                    AND b.state NOT IN ('confirmed','superseded')) AS pending
            FROM work_items w
            LEFT JOIN attempts a ON a.item_seq=w.seq AND a.outcome IS NULL
            LEFT JOIN dispatches d ON d.request_id=w.request_id
            WHERE w.instance=? AND w.project_id=? AND w.kind='story'
            ORDER BY w.priority IS NULL, w.priority, w.seq''', (scope[0], scan['project_id'])).fetchall()
        store = Ledger(conn, config, datetime.now(timezone.utc))
        summaries = {row['seq']: delivery_summary(*read_history(store, row)) for row in rows
                     if row['config_snapshot'] and json.loads(row['config_snapshot']).get('workflow_version') == 2}
        tasks = {}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if scan and 'split_tasks' in tables:
            join = ('LEFT JOIN task_evidence e ON e.item_seq=t.item_seq AND e.task_key=t.task_key'
                    if 'task_evidence' in tables else '')
            reason = 'e.reason' if join else 'NULL'
            skipped = "json_extract(e.evidence, '$.e2e_skipped')" if join else 'NULL'
            for task in conn.execute(f'''SELECT t.*, {reason} AS evidence_reason, {skipped} AS e2e_skipped
                    FROM split_tasks t
                    JOIN work_items w ON w.seq=t.item_seq {join}
                    WHERE w.instance=? AND w.project_id=? AND w.kind='story'
                    ORDER BY t.item_seq,t.task_key''', (scope[0], scan['project_id'])):
                tasks.setdefault(task['item_seq'], []).append(dict(task))
    sections = []
    for title, states in SECTIONS:
        selected = [item for item in rows if item['state'] in states]
        content = ''.join(table_row(row, scope[0], summaries.get(row['seq'], ''), tasks.get(row['seq'], []))
                          for row in selected)
        sections.append(
            f'<section><h2>{title} <span>{len(selected)}</span></h2>' + (
                f'<table><thead><tr><th>禅道需求</th><th>状态</th><th>当前步骤</th><th>范围与交付</th>'
                f'<th>拆分任务</th><th>待回写</th></tr></thead>'
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
