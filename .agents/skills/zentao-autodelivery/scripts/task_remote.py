"""Bounded ZenTao 22.6 operations for one story's split tasks."""
import base64
import html
import json
import os
import re
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'zentao-dev-task-flow' / 'scripts'))
from zentao_dev_task import ZenTaoError, ZenTaoSession, _dpapi


class RemoteRejected(ValueError):
    """The read failed, or the server rejected the write before applying it."""
    safe_to_retry = True


class RemoteUnknown(RuntimeError):
    """An attempted mutation has an uncertain outcome; reconcile by reading."""
    safe_to_retry = False


class _ApiRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A REST redirect is neither a login receipt nor a write acknowledgement.
        return None


class _TaskSession(ZenTaoSession):
    def __init__(self, base_url):
        super().__init__(base_url, timeout=10)
        self._opener = build_opener(_ApiRedirects())

    def _open(self, request):
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except HTTPError as error:
            response = error
        with response:
            if 300 <= response.status < 400:
                raise ZenTaoError('禅道 REST 接口返回重定向', status=response.status)
            return response.status, response.headers, response.read()


def _read(method):
    @wraps(method)
    def invoke(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except RemoteRejected:
            raise
        except Exception:
            # The shared client may include response bodies or credential metadata.
            raise RemoteRejected('禅道读取失败，待重新核对') from None
    return invoke


def _write(method, *args):
    try:
        result = method(*args)
        if not isinstance(result, dict) or result.get('status') in ('failed', 'fail'):
            raise ValueError('invalid acknowledgement')
        return result
    except ZenTaoError as error:
        if error.status in (401, 403, 404, 405):
            raise RemoteRejected(f'禅道拒绝任务操作（HTTP {error.status}）') from None
        raise RemoteUnknown('禅道写入结果未核实，须先读回对账') from None
    except Exception:
        raise RemoteUnknown('禅道写入结果未核实，须先读回对账') from None


def _id(value):
    if isinstance(value, dict):
        value = value.get('id')
    if not re.fullmatch(r'[1-9][0-9]*', str(value)):
        raise RemoteRejected('缺少有效的禅道对象编号')
    return str(value)


def _account(value):
    if isinstance(value, dict):
        value = value.get('account')
    return value.strip() if isinstance(value, str) else ''


def _marker(marker):
    if not isinstance(marker, str) or not marker or any(c in marker for c in '[]\r\n'):
        raise RemoteRejected('拆分标识无效')
    return '[' + marker + ']'


class TaskRemote:
    def __init__(self, session):
        self.session = session

    def _pages(self, path, key):
        seen = set()
        expected = None
        for page in range(1, 1001):
            query = urlencode({'browseType': 'all', 'orderBy': 'id_asc', 'pageID': page, 'recPerPage': 100})
            data = self.session._v2('GET', f'{path}?{query}')
            pager, rows = data.get('pager'), data.get(key)
            if not isinstance(pager, dict) or not isinstance(rows, list) or int(pager['pageID']) != page:
                raise RemoteRejected('无法核实禅道分页')
            total = int(pager['recTotal'])
            if total < 0 or (expected is not None and total != expected):
                raise RemoteRejected('分页期间记录数已变化，待重新查询')
            expected = total
            for row in rows:
                record = _id(row['id'])
                if record in seen:
                    raise RemoteRejected('禅道分页返回重复编号')
                seen.add(record)
                yield row
            if len(seen) == total:
                return
            if not rows or len(seen) > total:
                break
        raise RemoteRejected('未能取得完整的禅道分页')

    @_read
    def target(self, story_id, project_id):
        story_id, project_id = _id(story_id), _id(project_id)
        story = self.session.get_story(int(story_id))
        if not story or _id(story['id']) != story_id or not _account(story.get('assignedTo')):
            raise RemoteRejected('无法核实原需求或当前指派账号')
        # ZenTao 22.6 returns the authoritative execution memberships in detail.
        if 'executions' in story:
            links = story['executions']
            members = list(links.values()) if isinstance(links, dict) else links
            if not isinstance(members, list):
                raise RemoteRejected('原需求迭代关联无法核实')
            matches = {_id(link['id']) for link in members if _id(link['project']) == project_id}
            if len(matches) != 1:
                raise RemoteRejected('原需求迭代未关联或不唯一，待建单')
            return {'execution': matches.pop(), 'assignee': _account(story['assignedTo'])}
        executions = list(self._pages(f'/projects/{project_id}/executions', 'executions'))
        matches = []
        for execution in executions:
            execution_id = _id(execution['id'])
            if str(execution.get('project', project_id)) != project_id:
                raise RemoteRejected('迭代与原需求项目不匹配')
            ids = {_id(row['id']) for row in self._pages(f'/executions/{execution_id}/stories', 'stories')}
            if story_id in ids:
                matches.append(execution_id)
        if len(matches) != 1:
            raise RemoteRejected('原需求迭代未关联或不唯一，待建单')
        return {'execution': matches[0], 'assignee': _account(story['assignedTo'])}

    @_read
    def _detail(self, task_id):
        task_id = _id(task_id)
        task = self.session.get_task(int(task_id))
        if _id(task.get('id')) != task_id or str(task.get('deleted', '0')) == '1':
            raise RemoteRejected('禅道 Task 已删除或编号与请求不符')
        return task

    @_read
    def get(self, task_id):
        detail = self._detail(task_id)
        story = detail.get('story') or 0
        return {'id': _id(detail['id']), 'execution': _id(detail['execution']),
                'story': str(story.get('id') if isinstance(story, dict) else story),
                'assignee': _account(detail.get('assignedTo')),
                'status': detail['status'], 'description': detail['desc']}

    @_read
    def find(self, execution, marker):
        execution = _id(execution)
        needle = _marker(marker)
        matches = []
        for row in self._pages(f'/executions/{execution}/tasks', 'tasks'):
            if 'desc' in row and needle not in html.unescape(str(row['desc'])):
                continue
            task = self.get(row['id'])
            if task['execution'] != execution:
                raise RemoteRejected('查重结果不属于目标迭代')
            if needle in html.unescape(task['description']):
                matches.append(task)
        return matches

    def create(self, target, story_id, title, description, marker):
        payload = {'type': 'devel', 'name': title, 'executionID': int(_id(target['execution'])),
                   'assignedTo': target['assignee'], 'story': int(_id(story_id)),
                   'desc': '<p>' + html.escape(description).replace('\n', '<br>') + '</p><p>'
                           + html.escape(_marker(marker)) + '</p>'}
        result = _write(self.session.create_task, payload)
        try:
            return _id(result['id'])
        except (KeyError, RemoteRejected):
            raise RemoteUnknown('禅道建单未返回可核对的编号') from None

    def start(self, task_id, started_at):
        task = self._detail(task_id)
        if task['status'] != 'wait':
            return
        consumed = self._hours(task, 'consumed')
        left = self._hours(task, 'left')
        if not left:
            left = self._hours(task, 'estimate') - consumed
        # ZenTao treats starting a single-owner task with zero remaining effort as completing it.
        if left <= 0:
            raise RemoteRejected('缺少有效工时，禅道需要正数预计剩余工时才能登记为进行中')
        owner = _account(task.get('assignedTo'))
        if not owner or owner == 'closed':
            raise RemoteRejected('任务缺少有效指派人')
        local_start = datetime.fromisoformat(started_at).astimezone().strftime('%Y-%m-%d %H:%M:%S')
        _write(self.session.start_task, int(task['id']), {
            'realStarted': local_start, 'left': str(left), 'consumed': float(consumed), 'assignedTo': owner,
            'comment': '拆分任务已实际开工。',
        })

    @_read
    def has_comment(self, task_id, marker):
        actions = self._detail(task_id).get('actions')
        if isinstance(actions, dict):
            actions = actions.values()
        elif not isinstance(actions, list):
            raise RemoteRejected('无法核验任务备注历史')
        return any(_marker(marker) in html.unescape(action.get('comment') or '') for action in actions)

    def comment(self, task_id, text, marker):
        _write(self.session.comment_task, int(_id(task_id)),
               '<p>' + html.escape(text) + '</p><p>' + _marker(marker) + '</p>')

    def transition(self, task_id, action, *, assignee=None):
        task = self._detail(task_id)
        task_id, status = _id(task['id']), task['status']
        if action == 'close':
            if status == 'closed':
                return
            if status == 'cancel':
                raise RemoteRejected('任务已取消，需核实原任务')
            _write(self.session._v2, 'PUT', f'/tasks/{task_id}/close', {'comment': '拆分任务已独立验收并合回。'})
        elif action == 'reopen':
            if status in ('wait', 'doing', 'pause'):
                return
            if status not in ('done', 'closed'):
                raise RemoteRejected('任务状态不允许返工重开')
            # Closing replaces assignedTo with the reserved account "closed".
            owner = _account(assignee)
            if not owner:
                owner = self.get_story_owner(task['story'])
            if not owner or owner == 'closed':
                raise RemoteRejected('重开任务需要有效的原指派账号')
            comment = '联调发现原验收条件未满足，重新打开原任务。'
            left = self._remaining_hours(task)
            if left is None:
                # 22.6 requires positive remaining effort on activate, including
                # tasks whose estimate was left blank when created.
                left = '0.01'
                comment += '原任务未记录正剩余工时或估时，按禅道最小精度登记剩余 0.01 小时；已耗工时不变。'
            _write(self.session._v2, 'PUT', f'/tasks/{task_id}/activate',
                   {'left': left, 'assignedTo': owner, 'comment': comment})
        else:
            raise RemoteRejected('未知禅道任务操作')

    @_read
    def get_story_owner(self, story_id):
        story_id = _id(story_id)
        story = self.session.get_story(int(story_id))
        if not story or _id(story['id']) != story_id:
            raise RemoteRejected('无法核实重开任务的需求指派账号')
        return _account(story.get('assignedTo'))

    @staticmethod
    def _hours(task, field):
        try:
            hours = Decimal(str(task.get(field) or 0))
        except InvalidOperation:
            raise RemoteRejected('无法核实禅道任务工时') from None
        if not hours.is_finite() or hours < 0:
            raise RemoteRejected('无法核实禅道任务工时')
        return hours

    @classmethod
    def _remaining_hours(cls, task):
        for field in ('left', 'estimate'):
            hours = cls._hours(task, field)
            if hours > 0:
                return str(hours)
        return None


def _credentials(cfg):
    if cfg.get('credential_ref') != 'windows-dpapi:dotflow/zentao':
        raise RemoteRejected('不支持的禅道凭据引用')
    local = Path(os.environ['LOCALAPPDATA']) / 'DotFlow' / 'credentials' / 'zentao.json'
    record = json.loads(local.read_text(encoding='utf-8-sig'))
    formats = ('windows-dpapi-current-user', 'CurrentUser', 'DPAPI-CurrentUser', 'windows-dpapi:CurrentUser')
    if (record.get('schema_version') != 1 or record.get('protection') not in formats
            or record['base_url'].rstrip('/') != cfg['base_url'].rstrip('/')):
        raise RemoteRejected('禅道凭据与目标实例不匹配')
    data = record['encrypted_password']
    if re.fullmatch(r'(?:[0-9a-fA-F]{2})+', data):
        password = _dpapi(bytes.fromhex(data), False).decode('utf-16-le')
    else:
        password = _dpapi(base64.b64decode(data, validate=True), False).decode('utf-8')
    return record['username'], password


@_read
def get_remote(config_snapshot):
    cfg = config_snapshot['zentao']
    session = _TaskSession(cfg['base_url'])
    username, password = _credentials(cfg)
    session.login(username, password)
    return TaskRemote(session)
