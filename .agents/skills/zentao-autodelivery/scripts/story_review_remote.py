"""ZenTao 22.6's native Story review form and review history."""

import html
import json
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from task_remote import RemoteRejected, RemoteUnknown, _ApiRedirects, _credentials, _id, get_remote


class ReviewBlocked(RemoteRejected):
    """Current Story or review form cannot safely accept the proposed vote."""


def rows(value):
    if isinstance(value, dict):
        return value.values()
    if isinstance(value, list):
        return value
    raise RemoteRejected('禅道返回的评审数据结构无法核实')


class StoryReviewRemote:
    def __init__(self, config):
        self.config = config
        self.base = config['zentao']['base_url'].rstrip('/')
        self.opener = build_opener(_ApiRedirects(), HTTPCookieProcessor(CookieJar()))
        try:
            self.account, password = _credentials(config['zentao'])
        except Exception:
            raise RemoteRejected('无法加载当前实例的禅道凭据') from None
        reply = self.request('user', 'login', {}, {'account': self.account, 'password': password})
        if reply.get('status') != 'success' or (reply.get('user') or {}).get('account') != self.account:
            raise RemoteRejected('禅道网页登录身份未核实')

    def request(self, module, method, params, form=None, *, submit=False):
        url = self.base + '/index.php?' + urlencode({'m': module, 'f': method, **params, 't': 'json'})
        headers = {'Origin': self.base, 'Referer': url, 'Accept': 'application/json',
                   'X-Requested-With': 'XMLHttpRequest'}
        body = None
        if form is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            body = urlencode(form).encode('utf-8')
        try:
            with self.opener.open(Request(url, data=body, headers=headers), timeout=10) as response:
                value = json.load(response)
        except (OSError, ValueError) as exc:
            if submit and not (isinstance(exc, HTTPError) and exc.code in (401, 403, 404, 405)):
                raise RemoteUnknown('禅道评审提交结果未知，待回读历史') from None
            raise RemoteRejected('禅道评审页面读取、认证或权限校验失败') from None
        if not isinstance(value, dict):
            if submit:
                raise RemoteUnknown('禅道评审响应无法核实，待回读历史')
            raise RemoteRejected('禅道评审页面响应无法核实')
        return value

    def page(self, item, method):
        params = {'storyID': _id(item['item_id'])}
        if method == 'review':
            params.update({'from': 'project', 'storyType': 'story'})
        response = self.request('story', method, params)
        data = response.get('data')
        try:
            if isinstance(data, str):
                data = json.loads(data)
        except ValueError:
            raise RemoteRejected('禅道需求页面数据无法解析') from None
        story = data.get('story') if isinstance(data, dict) else None
        if (response.get('status') != 'success' or not isinstance(story, dict) or _id(story.get('id')) != params['storyID']
                or story.get('type') != 'story' or str(story.get('deleted')) not in ('0', 'False')):
            raise RemoteRejected('无法核实原研发需求或打开评审表单')
        return data

    def in_project(self, item, story):
        project = _id(item['project_id'])
        if any(isinstance(row, dict) and str(row.get('project')) == project for row in rows(story.get('executions', []))):
            return True
        # A Story can belong to a project without being assigned to an iteration.
        rest = get_remote(self.config)
        return any(_id(row.get('id')) == str(item['item_id'])
                   for row in rest._pages(f'/projects/{project}/stories', 'stories'))

    def observation(self, item, data):
        story = data['story']
        if not self.in_project(item, story):
            raise ReviewBlocked('原需求已无法核实归属当前项目')
        actions = []
        for row in rows(data.get('actions')):
            if (not isinstance(row, dict) or str(row.get('objectID')) != str(item['item_id'])
                    or row.get('objectType') != 'story'):
                continue
            actions.append({'id': _id(row.get('id')), 'action': str(row.get('action', '')).lower(),
                            'actor': str(row.get('actor', '')), 'extra': str(row.get('extra', '')),
                            'comment': html.unescape(str(row.get('comment', '')))})
        rounds = [int(row['id']) for row in actions if row['action'] in ('opened', 'changed', 'submitreview')]
        if not rounds or not story.get('status'):
            raise RemoteRejected('无法从禅道操作历史核实当前评审轮次')
        return {'target': {'version': _id(story.get('version')), 'round': str(max(rounds)),
                           'account': self.account, 'status': story['status']}, 'actions': actions}

    def observe(self, item):
        return self.observation(item, self.page(item, 'view'))

    def verify_pending(self, item, payload):
        data = self.page(item, 'review')
        target = self.observation(item, data)['target']
        if target != {**{key: payload[key] for key in ('version', 'round', 'account')}, 'status': 'reviewing'}:
            raise RemoteRejected('禅道需求版本或评审轮次变化，暂缓提交')
        reviewers, fields = data.get('reviewers'), data.get('fields')
        if (not isinstance(reviewers, dict) or self.account not in reviewers or reviewers[self.account]
                or self.account in str(data['story'].get('reviewedBy', '')).split(',')):
            raise ReviewBlocked('当前自动化账号没有本轮待评审资格')
        if str(data['story'].get('twins') or '').strip(','):
            raise ReviewBlocked('该需求的禅道评审会同步孪生需求，需核实关联范围')
        if not isinstance(fields, dict) or payload['result'] not in fields.get('result', {}).get('options', {}):
            raise ReviewBlocked('该版本评审表单不支持本次评审选项')
        defaults = ('reviewedDate', 'assignedTo', 'pri', 'estimate', 'status')
        if any(key not in fields or 'default' not in fields[key] for key in defaults):
            raise RemoteRejected('禅道评审表单缺少当前字段的默认值')
        return {key: fields[key]['default'] for key in defaults}

    def submit(self, item, payload):
        form = self.verify_pending(item, payload)
        form.update({'result': payload['result'], 'comment': html.escape(payload['comment'])})
        response = self.request('story', 'review', {'storyID': _id(item['item_id']), 'from': 'project', 'storyType': 'story'},
                                form, submit=True)
        if response.get('status') == 'success' or response.get('result') == 'success':
            return
        # The controller can emit the same validation response before or after updating the vote.
        raise RemoteUnknown('禅道评审未返回明确结果，待回读历史')
