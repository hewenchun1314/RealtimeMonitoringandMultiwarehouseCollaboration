#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""禅道 22.6 v2 API mock 服务（zentao-dev-task-flow 测试专用，纯标准库）。

实现统一 v2 API：
  POST /api.php/v2/users/login               登录（200 success/failed；可注入 404 以测 v1 兜底）
  GET  /api.php/v2/tasks/{id}                任务详情（task + actions；files 为 {id:file} map）
  POST /api.php/v2/tasks                     创建前置任务（必填 name,executionID）
  PUT  /api.php/v2/tasks/{id}/start          开始任务
  PUT  /api.php/v2/tasks/{id}/finish         完成任务（业务失败 200 status:fail）
  POST /api.php/v2/files                     附件上传（multipart：file/objectType/objectID）
  POST /api.php/v2/actions/comment           独立备注（query objectType/objectID，JSON comment）
  GET  /api.php/v2/stories/{id}              需求详情
  GET  /api.php/v2/users                     用户列表（v2，可注入 403）
v1 兜底：
  POST /api.php/v1/tokens                    登录兜底（201 {token}）
  GET  /api.php/v1/users                     用户列表兜底

特性：随机端口、线程安全、记录请求、失败注入（v2 登录失败/404、v1 登录失败、v2/v1 用户列表 403、
创建任务失败/500、开始失败、完成失败、上传失败、评论失败、任务已完成、网络断开）、可选 UTF-8 BOM。
不访问外网、不依赖第三方库。

用法：
    from mock_zentao_server import start_in_thread
    server = start_in_thread()                 # 或 start_in_thread(fail_v2_login=True)
    server.base_url / server.token
    server.mock.requests / server.mock.task(101) / server.mock.files_of(101)
    server.stop()
"""

from __future__ import annotations

import copy
import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

DEFAULT_USER_PASSWORD = "admin123"


def _default_users() -> List[dict]:
    return [
        {"account": "admin", "realname": "管理员", "password": DEFAULT_USER_PASSWORD},
        {"account": "zhangsan", "realname": "张三", "password": "zhangsan123"},
        {"account": "lisi", "realname": "李四", "password": "lisi123"},
    ]


def _default_stories() -> Dict[int, dict]:
    return {
        501: {
            "id": 501,
            "title": "训练集管理需求",
            "status": "active",
            "openedBy": "zhangsan",
            "deleted": 0,
        },
        502: {
            "id": 502,
            "title": "无创建人需求",
            "status": "active",
            "openedBy": "",
            "deleted": 0,
        },
        503: {
            "id": 503,
            "title": "已删除需求",
            "status": "closed",
            "openedBy": "zhangsan",
            "deleted": 1,
        },
    }


def _default_tasks() -> Dict[int, dict]:
    base = {
        "execution": 11,
        "project": 1,
        "module": 0,
        "type": "devel",
        "pri": 3,
        "consumed": 0,
        "realStarted": "",
        "finishedDate": "",
        "openedBy": "lisi",
        "assignedTo": "admin",
        "files": [],
        "actions": [],
    }
    return {
        101: {
            **base,
            "id": 101,
            "name": "训练集管理-基础框架",
            "status": "wait",
            "story": 501,
            "estimate": 8,
            "left": 8,
            "estStarted": "2026-01-05",
            "deadline": "2026-02-28",
        },
        102: {
            **base,
            "id": 102,
            "name": "训练集管理-业务模块",
            "status": "wait",
            "story": 502,
            "estimate": 5,
            "left": 5,
            "estStarted": "2026-01-05",
            "deadline": "2026-02-28",
        },
        103: {
            **base,
            "id": 103,
            "name": "无需求任务",
            "status": "doing",
            "story": 0,
            "estimate": 3,
            "left": 3,
            "estStarted": "2026-01-05",
            "deadline": "2026-02-28",
            "realStarted": "2026-01-06 09:00:00",
        },
        104: {
            **base,
            "id": 104,
            "name": "已完成的老任务",
            "status": "done",
            "story": 501,
            "estimate": 2,
            "left": 0,
            "consumed": 2,
            "estStarted": "2026-01-05",
            "deadline": "2026-02-28",
            "realStarted": "2026-01-06 09:00:00",
            "finishedDate": "2026-01-07 18:00:00",
            "assignedTo": "zhangsan",
        },
    }


class MockZenTaoState:
    """mock 禅道 v2 状态 + 路由分发（线程安全）。"""

    def __init__(
        self,
        *,
        users: Optional[Sequence[dict]] = None,
        stories: Optional[Dict[int, dict]] = None,
        tasks: Optional[Dict[int, dict]] = None,
        fail_v2_login: bool = False,
        v2_login_fail_status: str = "failed",
        v2_login_404: bool = False,
        fail_v1_login: bool = False,
        fail_users_v2_403: bool = False,
        fail_users_v1_403: bool = False,
        fail_create_task: bool = False,
        fail_create_task_500: bool = False,
        fail_start: bool = False,
        fail_finish: bool = False,
        omit_finish_action: bool = False,
        fail_upload: bool = False,
        fail_comment: bool = False,
        fail_network: bool = False,
        response_bom: bool = False,
        not_found_http: bool = False,
        require_assignee_exists: bool = False,
        fail_task_403_ids: Optional[Sequence[int]] = None,
    ) -> None:
        self.lock = threading.RLock()
        self.users = copy.deepcopy(list(users if users is not None else _default_users()))
        self.stories = copy.deepcopy(
            stories if stories is not None else _default_stories()
        )
        self.tasks = copy.deepcopy(tasks if tasks is not None else _default_tasks())
        self.fail_v2_login = bool(fail_v2_login)
        self.v2_login_fail_status = str(v2_login_fail_status or "failed")
        self.v2_login_404 = bool(v2_login_404)
        self.fail_v1_login = bool(fail_v1_login)
        self.fail_users_v2_403 = bool(fail_users_v2_403)
        self.fail_users_v1_403 = bool(fail_users_v1_403)
        self.fail_create_task = bool(fail_create_task)
        self.fail_create_task_500 = bool(fail_create_task_500)
        self.fail_start = bool(fail_start)
        self.fail_finish = bool(fail_finish)
        self.omit_finish_action = bool(omit_finish_action)
        self.fail_upload = bool(fail_upload)
        self.fail_comment = bool(fail_comment)
        self.fail_network = bool(fail_network)
        self.response_bom = bool(response_bom)
        self.not_found_http = bool(not_found_http)
        self.require_assignee_exists = bool(require_assignee_exists)
        self.fail_task_403_ids = {int(item) for item in (fail_task_403_ids or [])}

        self.tokens: Dict[str, dict] = {}
        self._token_seq = 0
        self._task_seq = max([int(key) for key in self.tasks] + [899]) + 1
        self._file_seq = 700
        self._action_seq = 500
        self._prereq_seq = 899
        self.requests: List[dict] = []
        self.token = ""

    # -- 测试便捷方法 -------------------------------------------------------
    def reset_requests(self) -> None:
        with self.lock:
            self.requests = []

    def request_records(
        self, *, method: Optional[str] = None, path_contains: Optional[str] = None
    ) -> List[dict]:
        with self.lock:
            records = copy.deepcopy(self.requests)
        result = []
        for record in records:
            if method and record.get("method") != method.upper():
                continue
            if path_contains and path_contains not in record.get("path", ""):
                continue
            result.append(record)
        return result

    def write_requests(self) -> List[dict]:
        result = []
        for item in self.request_records():
            if item.get("method") not in ("POST", "PUT", "PATCH", "DELETE"):
                continue
            path = item.get("path", "")
            if path.endswith("/users/login") or path.endswith("/tokens"):
                continue
            result.append(item)
        return result

    def task(self, task_id: int) -> dict:
        with self.lock:
            task = self.tasks.get(int(task_id))
            if task is None:
                raise KeyError(f"mock task not found: {task_id}")
            return copy.deepcopy(task)

    def files_of(self, task_id: int) -> List[dict]:
        with self.lock:
            return copy.deepcopy(list(self.tasks[int(task_id)].get("files") or []))

    def actions_of(self, task_id: int) -> List[dict]:
        with self.lock:
            return copy.deepcopy(list(self.tasks[int(task_id)].get("actions") or []))

    def set_task_status(self, task_id: int, status: str) -> None:
        with self.lock:
            self.tasks[int(task_id)]["status"] = status

    def add_task(self, task: dict) -> dict:
        with self.lock:
            task_id = int(task.get("id") or self._task_seq)
            self._task_seq = max(self._task_seq, task_id) + 1
            stored = copy.deepcopy(task)
            stored.setdefault("files", [])
            stored.setdefault("actions", [])
            self.tasks[task_id] = stored
            return copy.deepcopy(stored)

    def prereq_tasks(self) -> List[dict]:
        with self.lock:
            return [
                copy.deepcopy(task)
                for task_id, task in self.tasks.items()
                if int(task_id) >= 900 and int(task_id) != 104
            ]

    # -- 请求处理 -----------------------------------------------------------
    def handle(
        self, method: str, raw_path: str, headers: Dict[str, str], body_bytes: bytes
    ) -> Optional[Tuple[int, dict]]:
        parsed = urlparse(raw_path)
        path = parsed.path or "/"
        query = {
            key: (values[0] if len(values) == 1 else values)
            for key, values in parse_qs(parsed.query).items()
        }
        body_info = _parse_body(headers, body_bytes)
        record: Dict[str, Any] = {
            "method": method.upper(),
            "path": path,
            "query": copy.deepcopy(query),
            "headers": dict(headers),
            "body": body_info,
            "status": None,
        }
        with self.lock:
            self.requests.append(record)
        if self.fail_network:
            record["status"] = "network-error"
            return None
        status, payload = self._dispatch(method.upper(), path, query, headers, body_info)
        record["status"] = status
        return status, payload

    # -- 路由 ---------------------------------------------------------------
    def _dispatch(
        self,
        method: str,
        path: str,
        query: Dict[str, Any],
        headers: Dict[str, str],
        body: Any,
    ) -> Tuple[int, dict]:
        if path == "/api.php/v2/users/login":
            if method != "POST":
                return 405, {"error": "method not allowed"}
            return self._v2_login(body)
        if path == "/api.php/v1/tokens":
            if method != "POST":
                return 405, {"error": "method not allowed"}
            return self._v1_login(body)
        if not self._token_valid(headers):
            return 401, {"error": "Unauthorized"}
        if path.startswith("/api.php/v2/"):
            return self._dispatch_v2(method, path, query, body)
        if path.startswith("/api.php/v1/"):
            return self._dispatch_v1(method, path, query)
        return 404, {"error": f"not found: {method} {path}"}

    def _dispatch_v2(
        self, method: str, path: str, query: Dict[str, Any], body: Any
    ) -> Tuple[int, dict]:
        match = re.fullmatch(r"/api\.php/v2/tasks/(\d+)", path)
        if match:
            if method != "GET":
                return 405, {"error": "method not allowed"}
            return self._v2_get_task(int(match.group(1)))
        match = re.fullmatch(r"/api\.php/v2/stories/(\d+)", path)
        if match:
            if method != "GET":
                return 405, {"error": "method not allowed"}
            return self._v2_get_story(int(match.group(1)))
        if path == "/api.php/v2/users":
            if method != "GET":
                return 405, {"error": "method not allowed"}
            return self._v2_users(query)
        if path == "/api.php/v2/tasks":
            if method != "POST":
                return 405, {"error": "method not allowed"}
            return self._v2_create_task(body)
        match = re.fullmatch(r"/api\.php/v2/tasks/(\d+)/start", path)
        if match:
            if method != "PUT":
                return 405, {"error": "method not allowed"}
            return self._v2_start_task(int(match.group(1)), body)
        match = re.fullmatch(r"/api\.php/v2/tasks/(\d+)/finish", path)
        if match:
            if method != "PUT":
                return 405, {"error": "method not allowed"}
            return self._v2_finish_task(int(match.group(1)), body)
        if path == "/api.php/v2/files":
            if method != "POST":
                return 405, {"error": "method not allowed"}
            return self._v2_upload(body)
        if path == "/api.php/v2/actions/comment":
            if method != "POST":
                return 405, {"error": "method not allowed"}
            return self._v2_comment(query, body)
        return 404, {"error": f"not found: {method} {path}"}

    def _dispatch_v1(
        self, method: str, path: str, query: Dict[str, Any]
    ) -> Tuple[int, dict]:
        if path == "/api.php/v1/users":
            if method != "GET":
                return 405, {"error": "method not allowed"}
            return self._v1_users(query)
        return 404, {"error": f"not found: {method} {path}"}

    # -- 认证 ---------------------------------------------------------------
    def _token_valid(self, headers: Dict[str, str]) -> bool:
        token = ""
        for key, value in headers.items():
            if str(key).lower() == "token":
                token = str(value)
                break
        if not token:
            return False
        with self.lock:
            return token in self.tokens

    def _issue_token(self, account: str) -> str:
        with self.lock:
            self._token_seq += 1
            token = f"v2token{self._token_seq:04d}"
            self.tokens[token] = {"account": account}
            self.token = token
            return token

    # -- 登录 ---------------------------------------------------------------
    def _v2_login(self, body: Any) -> Tuple[int, dict]:
        if self.v2_login_404:
            return 404, {"error": "not found"}
        if self.fail_v2_login:
            return 200, {
                "status": self.v2_login_fail_status,
                "reason": "账号或密码错误（mock 注入）",
            }
        fields = body if isinstance(body, dict) else {}
        account = str(fields.get("account") or "").strip()
        password = str(fields.get("password") or "")
        with self.lock:
            user = next(
                (
                    item
                    for item in self.users
                    if item.get("account") == account and item.get("password") == password
                ),
                None,
            )
        if user is None:
            return 200, {"status": "failed", "reason": "账号或密码错误"}
        token = self._issue_token(account)
        return 200, {
            "status": "success",
            "token": token,
            "user": {"account": account, "realname": user.get("realname") or account},
        }

    def _v1_login(self, body: Any) -> Tuple[int, dict]:
        if self.fail_v1_login:
            return 400, {"error": "登录失败（mock 注入）"}
        fields = body if isinstance(body, dict) else {}
        account = str(fields.get("account") or "").strip()
        password = str(fields.get("password") or "")
        with self.lock:
            user = next(
                (
                    item
                    for item in self.users
                    if item.get("account") == account and item.get("password") == password
                ),
                None,
            )
        if user is None:
            return 400, {"error": "登录失败，请检查账号密码"}
        token = self._issue_token(account)
        return 201, {"token": token}

    # -- 读接口 -------------------------------------------------------------
    def _v2_get_task(self, task_id: int) -> Tuple[int, dict]:
        with self.lock:
            if task_id in self.fail_task_403_ids:
                return 403, {"error": "Access not allowed"}
            task = self.tasks.get(task_id)
            if task is None:
                if self.not_found_http:
                    return 404, {"error": "not found"}
                return 200, {"status": "fail", "code": 404, "message": "404 Not found"}
            return (
                200,
                {
                    "status": "success",
                    "task": _task_payload(task),
                    "actions": copy.deepcopy(list(task.get("actions") or [])),
                },
            )

    def _v2_get_story(self, story_id: int) -> Tuple[int, dict]:
        with self.lock:
            story = self.stories.get(story_id)
        if story is None:
            return 404, {"error": "not found"}
        return 200, {"status": "success", "story": copy.deepcopy(story), "actions": []}

    def _v2_users(self, query: Dict[str, Any]) -> Tuple[int, dict]:
        if self.fail_users_v2_403:
            return 403, {"error": "Access not allowed"}
        try:
            rec_per_page = max(1, int(str(query.get("recPerPage") or 200)))
        except ValueError:
            rec_per_page = 200
        try:
            page_id = max(1, int(str(query.get("pageID") or 1)))
        except ValueError:
            page_id = 1
        with self.lock:
            users = [
                {"account": item.get("account"), "realname": item.get("realname")}
                for item in self.users
            ]
        start = (page_id - 1) * rec_per_page
        return (
            200,
            {
                "status": "success",
                "total": len(users),
                "users": copy.deepcopy(users[start : start + rec_per_page]),
            },
        )

    def _v1_users(self, query: Dict[str, Any]) -> Tuple[int, dict]:
        if self.fail_users_v1_403:
            return 403, {"error": "Access not allowed"}
        try:
            limit = max(1, int(str(query.get("limit") or 200)))
        except ValueError:
            limit = 200
        try:
            page = max(1, int(str(query.get("page") or 1)))
        except ValueError:
            page = 1
        with self.lock:
            users = [
                {"account": item.get("account"), "realname": item.get("realname")}
                for item in self.users
            ]
        start = (page - 1) * limit
        return (
            200,
            {
                "page": page,
                "total": len(users),
                "limit": limit,
                "users": copy.deepcopy(users[start : start + limit]),
            },
        )

    # -- 写接口 -------------------------------------------------------------
    def _v2_create_task(self, body: Any) -> Tuple[int, dict]:
        if self.fail_create_task_500:
            return 500, {"error": "internal error（mock 注入）"}
        if self.fail_create_task:
            return 200, {"status": "fail", "message": "创建任务失败（mock 注入）"}
        fields = body if isinstance(body, dict) else {}
        missing = [
            name
            for name in ("name", "executionID")
            if not str(fields.get(name) or "").strip()
        ]
        if missing:
            return 200, {
                "status": "fail",
                "message": "缺少必填字段：" + "、".join(missing),
            }
        if self.require_assignee_exists:
            with self.lock:
                accounts = {str(item.get("account")) for item in self.users}
            if str(fields.get("assignedTo")) not in accounts:
                return 200, {"status": "fail", "message": "指派账号不存在"}
        with self.lock:
            self._prereq_seq += 1
            task_id = self._prereq_seq
            self.tasks[task_id] = {
                "id": task_id,
                "name": fields.get("name"),
                "status": "wait",
                "story": fields.get("story") or 0,
                "execution": int(str(fields.get("executionID"))),
                "project": 1,
                "module": fields.get("module") or 0,
                "type": fields.get("type") or "devel",
                "pri": fields.get("pri", 3),
                "estimate": fields.get("estimate") or 0,
                "consumed": 0,
                "left": fields.get("estimate") or 0,
                "estStarted": fields.get("estStarted"),
                "deadline": fields.get("deadline"),
                "realStarted": "",
                "finishedDate": "",
                "openedBy": "admin",
                "assignedTo": fields.get("assignedTo") or "admin",
                "desc": fields.get("desc") or "",
                "files": [],
                "actions": [],
            }
        return 200, {"status": "success", "id": task_id}

    def _v2_start_task(self, task_id: int, body: Any) -> Tuple[int, dict]:
        if self.fail_start:
            return 200, {"status": "fail", "message": "开始任务失败（mock 注入）"}
        fields = body if isinstance(body, dict) else {}
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                return 200, {"status": "fail", "code": 404, "message": "404 Not found"}
            if task.get("status") != "wait":
                return 200, {
                    "status": "fail",
                    "message": f"任务状态 {task.get('status')} 不允许开始",
                }
            task["status"] = "doing"
            task["realStarted"] = fields.get("realStarted") or "2026-01-01 00:00:00"
            if fields.get("left") is not None:
                task["left"] = fields.get("left")
            if fields.get("consumed") is not None:
                task["consumed"] = fields.get("consumed")
            self._action_seq += 1
            task.setdefault("actions", []).append(
                {
                    "id": self._action_seq,
                    "action": "started",
                    "actor": "admin",
                    "date": "2026-01-01 00:00:00",
                    "comment": str(fields.get("comment") or ""),
                }
            )
        return 200, {"status": "success"}

    def _v2_finish_task(self, task_id: int, body: Any) -> Tuple[int, dict]:
        if self.fail_finish:
            return 200, {
                "status": "fail",
                "message": {"currentConsumed": "currentConsumed 必须大于 0（mock 注入）"},
            }
        fields = body if isinstance(body, dict) else {}
        missing = [
            name
            for name in ("currentConsumed", "realStarted", "finishedDate")
            if fields.get(name) in (None, "")
        ]
        if missing:
            return 200, {"status": "fail", "message": "缺少必填字段：" + "、".join(missing)}
        try:
            consumed = float(fields.get("currentConsumed"))
        except (TypeError, ValueError):
            return 200, {"status": "fail", "message": "currentConsumed 必须是数字"}
        if consumed <= 0:
            return 200, {"status": "fail", "message": "currentConsumed 必须大于 0"}
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                return 200, {"status": "fail", "code": 404, "message": "404 Not found"}
            if task.get("status") in ("done", "closed", "cancel"):
                return 200, {
                    "status": "fail",
                    "message": f"任务状态 {task.get('status')} 不允许完成",
                }
            task["status"] = "done"
            task["finishedDate"] = fields.get("finishedDate")
            task["consumed"] = consumed
            task["left"] = 0
            assigned = fields.get("assignedTo")
            if assigned:
                task["assignedTo"] = str(assigned)
            if not self.omit_finish_action:
                self._action_seq += 1
                task.setdefault("actions", []).append(
                    {
                        "id": self._action_seq,
                        "action": "finished",
                        "actor": "admin",
                        "date": str(fields.get("finishedDate") or ""),
                        "comment": str(fields.get("comment") or ""),
                    }
                )
        return 200, {"status": "success"}

    def _v2_upload(self, body: Any) -> Tuple[int, dict]:
        if self.fail_upload:
            return 200, {"status": "fail", "message": "上传文件失败（mock 注入）"}
        if not isinstance(body, dict):
            return 200, {"status": "fail", "message": "请求体格式不正确"}
        fields = body.get("fields") or {}
        files = body.get("files") or []
        object_type = str(fields.get("objectType") or "")
        object_id = _as_int_safe(fields.get("objectID"))
        if object_type != "task" or object_id is None:
            return 200, {
                "status": "fail",
                "message": "objectType/objectID 必须作为 multipart 表单字段传入",
            }
        file_parts = [item for item in files if item.get("field") == "file"]
        if not file_parts:
            return 200, {"status": "fail", "message": "缺少 multipart 文件字段 file"}
        upload = file_parts[0]
        with self.lock:
            task = self.tasks.get(object_id)
            if task is None:
                return 200, {"status": "fail", "code": 404, "message": "404 Not found"}
            self._file_seq += 1
            file_record = {
                "id": self._file_seq,
                "title": upload.get("filename") or "upload.bin",
                "filename": upload.get("filename") or "upload.bin",
                "size": upload.get("size") or 0,
                "url": f"/file-read-{self._file_seq}.zip",
            }
            task.setdefault("files", []).append(file_record)
        return 200, {
            "status": "success",
            "id": file_record["id"],
            "url": file_record["url"],
        }

    def _v2_comment(self, query: Dict[str, Any], body: Any) -> Tuple[int, dict]:
        if self.fail_comment:
            return 200, {"status": "fail", "message": ["提交备注失败（mock 注入）"]}
        object_type = str(query.get("objectType") or "")
        object_id = _as_int_safe(query.get("objectID"))
        fields = body if isinstance(body, dict) else {}
        comment_text = str(fields.get("comment") or "")
        if object_type != "task" or object_id is None:
            return 200, {"status": "fail", "message": "objectType/objectID 不正确"}
        if not comment_text.strip():
            return 200, {"status": "success", "data": 0}
        with self.lock:
            task = self.tasks.get(object_id)
            if task is None:
                return 200, {"status": "fail", "code": 404, "message": "404 Not found"}
            self._action_seq += 1
            action_id = self._action_seq
            task.setdefault("actions", []).append(
                {
                    "id": action_id,
                    "action": "commented",
                    "objectType": "task",
                    "objectID": object_id,
                    "actor": "admin",
                    "date": "2026-01-01 00:00:00",
                    "comment": comment_text,
                }
            )
        return 200, {"status": "success", "data": action_id}


# ---------------------------------------------------------------------------
# 请求体解析 / HTTP handler / 线程服务
# ---------------------------------------------------------------------------


def _as_int_safe(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _task_payload(task: dict) -> dict:
    payload = copy.deepcopy(task)
    files = payload.get("files") or []
    payload["files"] = {
        str(item.get("id")): {key: value for key, value in item.items() if key != "id"}
        for item in files
        if isinstance(item, dict)
    }
    payload["actions"] = copy.deepcopy(list(task.get("actions") or []))
    return payload


def _parse_body(headers: Dict[str, str], body_bytes: bytes) -> Any:
    if not body_bytes:
        return {}
    content_type = ""
    for key, value in headers.items():
        if str(key).lower() == "content-type":
            content_type = str(value)
            break
    if "multipart/form-data" in content_type:
        return _parse_multipart(content_type, body_bytes)
    text = body_bytes.decode("utf-8", errors="replace")
    if "application/json" in content_type or text.lstrip().startswith(("{", "[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"__raw__": text}
    parsed = parse_qs(text, keep_blank_values=True)
    return {key: (values[0] if len(values) == 1 else values) for key, values in parsed.items()}


def _parse_multipart(content_type: str, body: bytes) -> dict:
    match = re.search(r"boundary=([^;]+)", content_type)
    if not match:
        return {"files": [], "fields": {}}
    boundary = match.group(1).strip().strip('"').encode("utf-8")
    result: Dict[str, Any] = {"files": [], "fields": {}}
    for part in body.split(b"--" + boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        head, content = part.split(b"\r\n\r\n", 1)
        head_text = head.decode("utf-8", errors="replace")
        name_match = re.search(r'name="([^"]+)"', head_text)
        filename_match = re.search(r'filename="([^"]*)"', head_text)
        if filename_match is not None:
            result["files"].append(
                {
                    "field": name_match.group(1) if name_match else "",
                    "filename": filename_match.group(1),
                    "size": len(content),
                    "content": content,
                }
            )
        elif name_match:
            result["fields"][name_match.group(1)] = content.decode(
                "utf-8", errors="replace"
            )
    return result


class _MockHandler(BaseHTTPRequestHandler):
    server_version = "MockZenTaoV226/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        pass

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length else b""
        app: MockZenTaoState = self.server.app  # type: ignore[attr-defined]
        headers = {key: value for key, value in self.headers.items()}
        result = app.handle(method, self.path, headers, body)
        if result is None:
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.connection.close()
            except OSError:
                pass
            return
        status, payload = result
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if app.response_bom:
            data = b"\xef\xbb\xbf" + data
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class MockZenTaoServer:
    def __init__(self, **kwargs: Any) -> None:
        self.mock = MockZenTaoState(**kwargs)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
        self.httpd.daemon_threads = True
        self.httpd.app = self.mock  # type: ignore[attr-defined]
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def token(self) -> str:
        return self.mock.token

    def start(self) -> "MockZenTaoServer":
        if self._thread is None:
            self._thread = threading.Thread(
                target=self.httpd.serve_forever,
                kwargs={"poll_interval": 0.05},
                daemon=True,
                name="mock-zentao-v2",
            )
            self._thread.start()
        return self

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "MockZenTaoServer":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()


def start_in_thread(**kwargs: Any) -> MockZenTaoServer:
    return MockZenTaoServer(**kwargs).start()


if __name__ == "__main__":  # pragma: no cover - 手工调试
    import time

    server = start_in_thread()
    print(f"mock 禅道 v2 已启动：{server.base_url}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
