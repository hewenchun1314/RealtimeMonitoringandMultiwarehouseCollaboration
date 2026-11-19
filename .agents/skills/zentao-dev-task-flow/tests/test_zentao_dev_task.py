#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`zentao-dev-task-flow/scripts/zentao_dev_task.py` 的单元/集成测试。

不访问真实禅道/外网：
  * 禅道侧使用同目录 `mock_zentao_server.py`（v2 API；v1 仅登录/用户列表兜底；
    随机端口、线程、请求记录、失败注入）；
  * Git 侧使用系统临时目录里的临时仓库（git init + commit + remote add，不 push）；
  * 用户配置/工作区都在临时目录（环境变量 ZENTAO_DEV_TASK_CONFIG），测试结束清理。

运行：python zentao-dev-task-flow/tests/test_zentao_dev_task.py
退出码 0 = 全部 PASS；非 0 = 有失败；末行打印 PASS/FAIL 计数。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

try:  # Windows 控制台可能是 cp936，测试输出统一 UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))

import mock_zentao_server as mock_server  # noqa: E402
import zentao_dev_task as zdt  # noqa: E402

SCRIPT = SCRIPTS / "zentao_dev_task.py"
PASSWORD = "admin123"
SECRET_PASSWORD = "TopSecret!42"
TASK_ID = 101
WS_ROOT_NAME = ".zentao-dev-task"


def run_script(args, *, env, cwd, stdin_text=None, timeout=180):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(cwd),
        env=env,
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_path = Path(tempfile.mkdtemp(prefix="zdt-flow-test-"))
        self.config_path = self.tmp_path / "zentao.json"
        self.env = os.environ.copy()
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.env["ZENTAO_DEV_TASK_CONFIG"] = str(self.config_path)
        for key in (
            "ZENTAO_BASE_URL",
            "ZENTAO_USERNAME",
            "ZENTAO_PASSWORD",
            "ZENTAO_DEV_TASK_DEBUG",
        ):
            self.env.pop(key, None)
        self.servers = []
        self.repo: Path | None = None
        self.commit = ""

    def tearDown(self) -> None:
        for server in self.servers:
            try:
                server.stop()
            except Exception:
                pass
        shutil.rmtree(self.tmp_path, ignore_errors=True)

    # -- 进程 / mock --------------------------------------------------------
    def start_mock(self, **kwargs):
        if "users" not in kwargs:
            kwargs["users"] = [
                {"account": "admin", "realname": "管理员", "password": PASSWORD},
                {"account": "zhangsan", "realname": "张三", "password": PASSWORD},
                {"account": "lisi", "realname": "李四", "password": PASSWORD},
            ]
        server = mock_server.start_in_thread(**kwargs)
        self.servers.append(server)
        return server

    def use_creds(self, server, *, username="admin", password=PASSWORD) -> None:
        self.env["ZENTAO_BASE_URL"] = server.base_url
        self.env["ZENTAO_USERNAME"] = username
        self.env["ZENTAO_PASSWORD"] = password

    def clear_creds_env(self) -> None:
        for key in (
            "ZENTAO_BASE_URL",
            "ZENTAO_USERNAME",
            "ZENTAO_PASSWORD",
            "ZENTAO_DEV_TASK_DEBUG",
        ):
            self.env.pop(key, None)

    def run_cli(self, *args, stdin_text=None, env=None, timeout=180):
        return run_script(
            args,
            env=env or self.env,
            cwd=self.tmp_path,
            stdin_text=stdin_text,
            timeout=timeout,
        )

    def parse_json_stdout(self, proc) -> dict:
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.fail(
                f"stdout 不是纯 JSON：\n--- stdout ---\n{proc.stdout[:3000]}"
                f"\n--- stderr ---\n{proc.stderr[:3000]}"
            )

    @staticmethod
    def all_output(proc) -> str:
        return (proc.stdout or "") + "\n" + (proc.stderr or "")

    def assert_rc(self, proc, expected: int) -> None:
        self.assertEqual(
            proc.returncode,
            expected,
            msg=(
                f"退出码期望 {expected}，实际 {proc.returncode}\n"
                f"--- stdout ---\n{proc.stdout[:3000]}\n"
                f"--- stderr ---\n{proc.stderr[:3000]}"
            ),
        )

    # -- 参数构造 -----------------------------------------------------------
    def init_args(self, server, task_id=TASK_ID, *extra) -> list:
        return [
            "init",
            "--task-id",
            str(task_id),
            "--base-url",
            server.base_url,
            "--username",
            "admin",
            *extra,
        ]

    def do_init(self, server, task_id=TASK_ID, *extra) -> None:
        proc = self.run_cli(*self.init_args(server, task_id, *extra))
        self.assert_rc(proc, 0)
        return proc

    def stage_args(self, task_id=TASK_ID, *extra) -> list:
        return ["--task-id", str(task_id), *extra]

    def run_stage(self, command: str, task_id=TASK_ID, *extra):
        return self.run_cli(command, *self.stage_args(task_id, *extra))

    # -- 工作区 / context ---------------------------------------------------
    def task_ws(self, task_id=TASK_ID) -> Path:
        return self.tmp_path / WS_ROOT_NAME / str(task_id)

    def context_path(self, task_id=TASK_ID) -> Path:
        return self.task_ws(task_id) / "context.json"

    def load_context(self, task_id=TASK_ID) -> dict:
        return json.loads(self.context_path(task_id).read_text(encoding="utf-8"))

    def write_context(self, data: dict, task_id=TASK_ID) -> None:
        ws = self.task_ws(task_id)
        (ws / "documents").mkdir(parents=True, exist_ok=True)
        (ws / "attachments").mkdir(parents=True, exist_ok=True)
        (ws / "context.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def resolve_artifact(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else (self.tmp_path / path)

    def seed_context(
        self,
        task_id=TASK_ID,
        status="initialized",
        *,
        prepare=None,
        acceptance=None,
        creator=None,
        task_snapshot=None,
        story_snapshot=None,
        feasibility=None,
        plan=None,
        delivery=None,
        prerequisites=None,
    ) -> dict:
        data = {
            "version": 1,
            "taskId": str(task_id),
            "taskSnapshot": task_snapshot
            or {
                "id": task_id,
                "name": "训练集管理-基础框架",
                "status": "wait",
                "story": 501,
                "execution": 11,
                "project": 1,
                "estimate": 8,
                "left": 8,
                "consumed": 0,
                "assignedTo": "admin",
                "openedBy": "lisi",
                "estStarted": "2026-01-05",
                "deadline": "2026-02-28",
            },
            "storySnapshot": story_snapshot
            or {"id": 501, "title": "训练集管理需求", "openedBy": "zhangsan"},
            "creator": creator
            or {
                "requirementCreator": "zhangsan",
                "taskCreator": "lisi",
                "resolvedAssignee": "zhangsan",
                "source": "requirements_creator",
            },
            "status": status,
            "timestamps": {"initializedAt": zdt.now_str()},
            "prepare": prepare or {},
            "prerequisites": prerequisites or [],
            "feasibility": feasibility or {},
            "plan": plan or {},
            "acceptance": acceptance or {},
            "delivery": delivery or {},
            "warnings": [],
        }
        self.write_context(data, task_id)
        return data

    def seed_prepared(self, task_id=TASK_ID, status="prepared", **kwargs) -> dict:
        repo = kwargs.pop("repo", None) or self.repo or self.make_git_repo()
        prepare = {
            "repoPath": str(repo),
            "repoUrl": "https://example.com/tetlab/training-set.git",
            "branch": "main",
            "baseCommit": self.commit,
            "clean": True,
            "buildCmd": "mvn -q -DskipTests package",
            "testCmd": "mvn -q test",
            "runtime": "JDK 21 + Maven 3.9",
            "envNotes": ["MySQL 8 本地可用"],
            "docsDirs": [],
        }
        return self.seed_context(task_id, status, prepare=prepare, **kwargs)

    def seed_developing(self, task_id=TASK_ID, status="developing", **kwargs) -> dict:
        acceptance = kwargs.pop(
            "acceptance",
            {"passed": True, "evidenceFile": "evidence.md", "notes": "", "acceptedAt": zdt.now_str()},
        )
        data = self.seed_prepared(
            task_id,
            status,
            plan={
                "file": "dev-plan.md",
                "approved": True,
                "stepCount": 3,
                "riskCount": 2,
            },
            acceptance=acceptance,
            **kwargs,
        )
        ws = self.task_ws(task_id)
        for name in ("feasibility.md", "dev-plan.md", "summary.md"):
            (ws / name).write_text(f"# {name}\n\n- 条目一\n- 条目二\n", encoding="utf-8")
        (ws / "documents").mkdir(exist_ok=True)
        (ws / "documents" / "design.md").write_text("# 设计文档\n设计内容\n", encoding="utf-8")
        (ws / "acceptance.md").write_text("# 用户验收记录\n结论：通过\n", encoding="utf-8")
        return data

    # -- Git 临时仓库 -------------------------------------------------------
    def git(self, *args, check=True):
        assert self.repo is not None
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and proc.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} 失败：{proc.stderr}")
        return proc

    def make_git_repo(self, name="req-repo") -> Path:
        repo = self.tmp_path / name
        repo.mkdir(parents=True, exist_ok=True)
        self.repo = repo
        if not (repo / ".git").exists():
            self.git("init", "-b", "main")
            self.git("config", "user.email", "tester@example.com")
            self.git("config", "user.name", "Tester")
        (repo / "README.md").write_text("# demo repo\n", encoding="utf-8")
        self.git("add", "-A")
        commit_proc = self.git("commit", "-m", "init", check=False)
        remote_proc = self.git("remote", "get-url", "origin", check=False)
        if remote_proc.returncode != 0:
            self.git("remote", "add", "origin", "https://example.com/tetlab/training-set.git")
        self.commit = self.git("rev-parse", "HEAD").stdout.strip()
        return repo


# ---------------------------------------------------------------------------
# 0) CLI 基础
# ---------------------------------------------------------------------------


class TestCliBasics(BaseCase):
    def test_version(self) -> None:
        proc = self.run_cli("--version")
        self.assert_rc(proc, 0)
        self.assertIn("zentao_dev_task.py", proc.stdout)

    def test_all_subcommand_help(self) -> None:
        for command in (
            "init",
            "prepare",
            "prereq",
            "feasibility",
            "plan",
            "approve-plan",
            "start",
            "accept",
            "finish",
            "status",
            "config-show",
            "config-forget",
        ):
            proc = self.run_cli(command, "--help")
            self.assert_rc(proc, 0)
            self.assertIn("usage:", proc.stdout)

    def test_no_forbidden_imports(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("import yaml", source)
        self.assertNotIn("import requests", source)
        self.assertNotIn('"--password"', source)
        self.assertNotIn("--password'", source)
        # v2 重构后不应再有旧 Web 会话方案相关代码
        self.assertNotIn("Cookie", source)
        self.assertNotIn("ajaxUpload", source)
        self.assertNotIn("ss_{", source)
        self.assertIn("/api.php/v2", source)

    def test_no_git_write_operations(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ('"git", "add"', '"git", "commit"', '"git", "push"', '"git", "checkout"'):
            self.assertNotIn(forbidden, source)

    def test_workspace_args_documented(self) -> None:
        proc = self.run_cli("status", "--help")
        self.assert_rc(proc, 0)
        self.assertIn("--task-id", proc.stdout)
        self.assertIn("--workspace", proc.stdout)


# ---------------------------------------------------------------------------
# 1) 凭据 / 复用 / config
# ---------------------------------------------------------------------------


class TestCredentials(BaseCase):
    def test_missing_credentials_exit_2(self) -> None:
        mock = self.start_mock()
        proc = self.run_cli("init", "--task-id", str(TASK_ID), "--base-url", mock.base_url)
        self.assert_rc(proc, 2)
        self.assertIn("缺少禅道用户名和密码", self.all_output(proc))

    def test_password_stdin(self) -> None:
        mock = self.start_mock()
        proc = self.run_cli(
            "init",
            "--task-id",
            str(TASK_ID),
            "--base-url",
            mock.base_url,
            "--username",
            "admin",
            "--password-stdin",
            stdin_text=PASSWORD + "\n",
        )
        self.assert_rc(proc, 0)
        self.assertEqual(self.load_context()["status"], "initialized")

    def test_reuse_gate_and_confirmation(self) -> None:
        mock = self.start_mock()
        self.use_creds(mock)
        self.do_init(mock)
        self.clear_creds_env()
        proc = self.run_cli("status", *self.stage_args())
        self.assert_rc(proc, 2)
        output = self.all_output(proc)
        self.assertIn("复用", output)
        self.assertIn("--reuse", output)
        self.assertNotIn(PASSWORD, output)
        proc = self.run_cli("status", *self.stage_args(), "--reuse")
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(
            self.run_cli("status", *self.stage_args(), "--reuse", "--json")
        )
        self.assertTrue(payload["ok"])

    def test_reuse_rejected_when_base_url_changes(self) -> None:
        mock = self.start_mock()
        self.use_creds(mock)
        self.do_init(mock)
        other = self.start_mock()
        self.clear_creds_env()
        proc = self.run_cli(
            "init",
            "--task-id",
            str(TASK_ID),
            "--base-url",
            other.base_url,
            "--reuse",
        )
        self.assert_rc(proc, 2)
        output = self.all_output(proc)
        self.assertIn("另一个禅道地址", output)

    def test_config_show_and_forget(self) -> None:
        mock = self.start_mock()
        self.use_creds(mock)
        self.do_init(mock)
        proc = self.run_cli("config-show", "--json")
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertTrue(payload["exists"])
        self.assertEqual(payload["username"], "admin")
        self.assertTrue(payload["passwordSaved"])
        if sys.platform == "win32":
            self.assertEqual(payload["passwordScheme"], "dpapi")
        self.assertEqual(payload["lastTaskId"], str(TASK_ID))
        self.assertNotIn(PASSWORD, proc.stdout)
        proc = self.run_cli("config-forget")
        self.assert_rc(proc, 2)
        proc = self.run_cli("config-forget", "--yes")
        self.assert_rc(proc, 0)
        self.assertFalse(self.config_path.is_file())

    def test_no_remember(self) -> None:
        mock = self.start_mock()
        self.use_creds(mock)
        self.do_init(mock, TASK_ID, "--no-remember")
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("password", config)
        self.clear_creds_env()
        proc = self.run_cli("status", *self.stage_args(), "--reuse")
        self.assert_rc(proc, 2)
        self.assertIn("未保存密码", self.all_output(proc))

    def test_v2_login_success(self) -> None:
        mock = self.start_mock()
        self.use_creds(mock)
        proc = self.run_cli(*self.init_args(mock, TASK_ID, "--json"))
        self.assert_rc(proc, 0)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["login"]["loginMode"], "v2")
        self.assertTrue(payload["login"]["capabilities"]["attachment"])
        self.assertTrue(payload["login"]["capabilities"]["comment"])

    def test_v2_login_failure_exit_5(self) -> None:
        mock = self.start_mock(fail_v2_login=True)
        self.use_creds(mock)
        proc = self.run_cli(*self.init_args(mock))
        self.assert_rc(proc, 5)
        output = self.all_output(proc)
        self.assertIn("登录失败", output)
        self.assertIn("账号或密码错误", output)

    def test_v2_login_fail_status_exit_5(self) -> None:
        # 防御：status=="fail"（而非 "failed"）也按登录失败处理（exit 5，不重试）
        mock = self.start_mock(fail_v2_login=True, v2_login_fail_status="fail")
        self.use_creds(mock)
        proc = self.run_cli(*self.init_args(mock))
        self.assert_rc(proc, 5)
        output = self.all_output(proc)
        self.assertIn("登录失败", output)
        self.assertIn("账号或密码错误", output)

    def test_v1_login_fallback_when_v2_login_404(self) -> None:
        mock = self.start_mock(v2_login_404=True)
        self.use_creds(mock)
        proc = self.run_cli(*self.init_args(mock, TASK_ID, "--json"))
        self.assert_rc(proc, 0)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["login"]["loginMode"], "v1-fallback")
        self.assertIn("404", self.all_output(proc))


# ---------------------------------------------------------------------------
# 2) init
# ---------------------------------------------------------------------------


class TestInit(BaseCase):
    def test_reads_requirement_creator(self) -> None:
        mock = self.start_mock()
        self.use_creds(mock)
        proc = self.do_init(mock)
        self.assert_rc(proc, 0)
        context = self.load_context()
        self.assertEqual(context["status"], "initialized")
        self.assertEqual(context["taskSnapshot"]["name"], "训练集管理-基础框架")
        self.assertEqual(context["taskSnapshot"]["story"], 501)
        self.assertEqual(context["storySnapshot"]["openedBy"], "zhangsan")
        self.assertEqual(context["creator"]["requirementCreator"], "zhangsan")
        self.assertEqual(context["creator"]["taskCreator"], "lisi")
        self.assertEqual(context["creator"]["resolvedAssignee"], "zhangsan")
        self.assertEqual(context["creator"]["source"], "requirements_creator")
        self.assertTrue(self.context_path().is_file())

    def test_task_creator_fallback(self) -> None:
        mock = self.start_mock()
        self.use_creds(mock)
        self.do_init(mock, 102)
        context = self.load_context(102)
        self.assertEqual(context["creator"]["requirementCreator"], "")
        self.assertEqual(context["creator"]["taskCreator"], "lisi")
        self.assertEqual(context["creator"]["resolvedAssignee"], "lisi")
        self.assertEqual(context["creator"]["source"], "task_creator")
        self.assertTrue(any("兜底" in item for item in context["warnings"]))

    def test_no_story_records_empty(self) -> None:
        mock = self.start_mock()
        self.use_creds(mock)
        self.do_init(mock, 103)
        context = self.load_context(103)
        self.assertEqual(context["storySnapshot"], {})
        self.assertEqual(context["creator"]["requirementCreator"], "")
        self.assertEqual(context["creator"]["resolvedAssignee"], "lisi")

    def test_invalid_task_status_exit_7(self) -> None:
        mock = self.start_mock()
        self.use_creds(mock)
        proc = self.run_cli(*self.init_args(mock, 104))
        self.assert_rc(proc, 7)
        self.assertIn("不允许接入", self.all_output(proc))

    def test_task_not_found_exit_5(self) -> None:
        mock = self.start_mock()
        self.use_creds(mock)
        proc = self.run_cli(*self.init_args(mock, 999))
        self.assert_rc(proc, 5)
        self.assertIn("不存在或当前账号不可见", self.all_output(proc))

    def test_init_idempotent(self) -> None:
        mock = self.start_mock()
        self.use_creds(mock)
        self.do_init(mock)
        first = self.load_context()
        proc = self.do_init(mock)
        self.assert_rc(proc, 0)
        second = self.load_context()
        self.assertEqual(second["status"], "initialized")
        self.assertTrue(any("工作区已存在" in item for item in second["warnings"]))

    def test_force_requires_confirm(self) -> None:
        mock = self.start_mock()
        self.use_creds(mock)
        self.do_init(mock)
        context = self.load_context()
        context["status"] = "prepared"
        self.write_context(context)
        proc = self.run_cli(*self.init_args(mock, TASK_ID, "--force"))
        self.assert_rc(proc, 2)
        self.assertIn("--confirm", self.all_output(proc))
        proc = self.run_cli(*self.init_args(mock, TASK_ID, "--force", "--confirm"))
        self.assert_rc(proc, 0)
        self.assertEqual(self.load_context()["status"], "initialized")

    def test_init_json_pure(self) -> None:
        mock = self.start_mock()
        self.use_creds(mock)
        proc = self.run_cli(*self.init_args(mock, TASK_ID, "--json"))
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["taskId"], TASK_ID)
        self.assertEqual(payload["creator"]["resolvedAssignee"], "zhangsan")
        self.assertEqual(payload["login"]["loginMode"], "v2")
        self.assertTrue(payload["login"]["capabilities"]["attachment"])
        self.assertTrue(proc.stderr.strip())


# ---------------------------------------------------------------------------
# 3) prepare
# ---------------------------------------------------------------------------


class TestPrepare(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.mock = self.start_mock()
        self.use_creds(self.mock)
        self.do_init(self.mock)

    def prepare_args(self, *extra) -> list:
        return ["prepare", *self.stage_args(TASK_ID, *extra)]

    def test_repo_path_missing_exit_3(self) -> None:
        proc = self.run_cli(*self.prepare_args("--repo-path", str(self.tmp_path / "nope")))
        self.assert_rc(proc, 3)
        self.assertIn("不存在", self.all_output(proc))

    def test_not_git_repo_exit_3(self) -> None:
        plain = self.tmp_path / "plain"
        plain.mkdir()
        proc = self.run_cli(*self.prepare_args("--repo-path", str(plain)))
        self.assert_rc(proc, 3)
        self.assertIn("不是 Git 仓库", self.all_output(proc))

    def test_prepare_success_records_facts(self) -> None:
        repo = self.make_git_repo()
        proc = self.run_cli(
            *self.prepare_args(
                "--repo-path",
                str(repo),
                "--build-cmd",
                "mvn -q package",
                "--test-cmd",
                "mvn -q test",
                "--runtime",
                "JDK 21 + Maven 3.9",
                "--env-note",
                "MySQL 8 本地",
                "--no-detect-tools",
                "--json",
            )
        )
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertEqual(payload["repo"]["url"], "https://example.com/tetlab/training-set.git")
        self.assertEqual(payload["repo"]["branch"], "main")
        self.assertEqual(payload["repo"]["head"], self.commit)
        self.assertTrue(payload["repo"]["clean"])
        self.assertEqual(payload["commands"]["build"], "mvn -q package")
        self.assertEqual(payload["toolchain"], [])
        context = self.load_context()
        self.assertEqual(context["status"], "prepared")
        self.assertEqual(context["prepare"]["baseCommit"], self.commit)
        self.assertEqual(context["prepare"]["envNotes"], ["MySQL 8 本地"])
        self.assertTrue(Path(context["prepare"]["repoPath"]).is_dir())

    def test_prepare_dirty_warning_and_allow_dirty(self) -> None:
        repo = self.make_git_repo()
        (repo / "README.md").write_text("# dirty\n", encoding="utf-8")
        proc = self.run_cli(*self.prepare_args("--repo-path", str(repo), "--json"))
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertFalse(payload["repo"]["clean"])
        self.assertIn("未提交修改", payload["warnings"][0])
        self.assertFalse(payload["repo"]["allowDirty"] if "allowDirty" in payload["repo"] else False)
        proc = self.run_cli(
            *self.prepare_args("--repo-path", str(repo), "--allow-dirty", "--json")
        )
        self.assert_rc(proc, 0)
        context = self.load_context()
        self.assertTrue(context["prepare"]["allowDirty"])

    def test_prepare_state_gate(self) -> None:
        self.seed_context(TASK_ID, "plan_approved")
        repo = self.make_git_repo()
        proc = self.run_cli(*self.prepare_args("--repo-path", str(repo)))
        self.assert_rc(proc, 3)
        self.assertIn("不允许跳阶段", self.all_output(proc))

    def test_prepare_rerun_allowed(self) -> None:
        repo = self.make_git_repo()
        self.assert_rc(self.run_cli(*self.prepare_args("--repo-path", str(repo))), 0)
        proc = self.run_cli(*self.prepare_args("--repo-path", str(repo)))
        self.assert_rc(proc, 0)
        self.assertIn("重跑", self.all_output(proc))

    def test_docs_dir_missing_exit_3(self) -> None:
        repo = self.make_git_repo()
        proc = self.run_cli(
            *self.prepare_args(
                "--repo-path",
                str(repo),
                "--docs-dir",
                str(self.tmp_path / "missing-docs"),
            )
        )
        self.assert_rc(proc, 3)
        self.assertIn("--docs-dir", self.all_output(proc))


# ---------------------------------------------------------------------------
# 4) prereq：创建前置任务
# ---------------------------------------------------------------------------


class TestPrereq(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.mock = self.start_mock()
        self.use_creds(self.mock)
        self.do_init(self.mock)

    def prereq_args(self, *extra) -> list:
        return ["prereq", *self.stage_args(TASK_ID, *extra)]

    def test_assignee_required_exit_2(self) -> None:
        proc = self.run_cli(*self.prereq_args("--dry-run"))
        self.assert_rc(proc, 2)

    def test_dry_run_zero_writes(self) -> None:
        config_before = self.config_path.read_bytes()
        config_mtime = self.config_path.stat().st_mtime_ns
        proc = self.run_cli(*self.prereq_args("--assignee", "zhangsan", "--dry-run", "--json"))
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(self.mock.mock.write_requests(), [])
        self.assertEqual(self.load_context(TASK_ID)["prerequisites"], [])
        self.assertEqual(self.mock.mock.prereq_tasks(), [])
        # dry-run 不得写用户配置（内容与 mtime 都不变）
        self.assertEqual(self.config_path.read_bytes(), config_before)
        self.assertEqual(self.config_path.stat().st_mtime_ns, config_mtime)

    def test_not_confirmed_exit_2(self) -> None:
        proc = self.run_cli(*self.prereq_args("--assignee", "zhangsan"))
        self.assert_rc(proc, 2)
        self.assertIn("--confirm", self.all_output(proc))
        self.assertEqual(self.mock.mock.write_requests(), [])

    def test_confirm_creates_prerequisite_with_desc(self) -> None:
        proc = self.run_cli(
            *self.prereq_args(
                "--assignee",
                "zhangsan",
                "--name",
                "[前置] 开通数据权限",
                "--confirm",
                "--json",
            )
        )
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertTrue(payload["ok"])
        created = payload["prerequisite"]
        self.assertEqual(created["assignee"], "zhangsan")
        self.assertTrue(created["notified"])
        posts = [
            item
            for item in self.mock.mock.request_records(method="POST")
            if item["path"] == "/api.php/v2/tasks"
        ]
        self.assertEqual(len(posts), 1)
        body = posts[0]["body"]
        for key in ("name", "assignedTo", "type", "estStarted", "deadline", "desc"):
            self.assertIn(key, body)
        self.assertEqual(body["name"], "[前置] 开通数据权限")
        self.assertEqual(body["executionID"], 11)
        self.assertEqual(body["assignedTo"], "zhangsan")
        self.assertEqual(body["type"], "devel")
        self.assertEqual(body["estStarted"], "2026-01-05")
        self.assertEqual(body["deadline"], "2026-02-28")
        self.assertIn(f"#{TASK_ID}", body["desc"])
        self.assertIn("训练集管理-基础框架", body["desc"])
        self.assertIn("task-view-101.html", body["desc"])
        self.assertIn("期望交付物", body["desc"])
        context = self.load_context(TASK_ID)
        self.assertEqual(len(context["prerequisites"]), 1)
        self.assertEqual(context["prerequisites"][0]["taskId"], created["taskId"])
        actions = self.mock.mock.actions_of(TASK_ID)
        self.assertTrue(any("已创建前置任务" in item.get("comment", "") for item in actions))

    def test_no_notify(self) -> None:
        proc = self.run_cli(
            *self.prereq_args("--assignee", "zhangsan", "--no-notify", "--confirm", "--json")
        )
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertFalse(payload["prerequisite"]["notified"])
        comments = [
            item
            for item in self.mock.mock.request_records(method="POST")
            if item["path"] == "/api.php/v2/actions/comment"
        ]
        self.assertEqual(comments, [])

    def test_invalid_assignee_exit_2(self) -> None:
        proc = self.run_cli(*self.prereq_args("--assignee", "nobody", "--confirm"))
        self.assert_rc(proc, 2)
        self.assertIn("指派账号不存在", self.all_output(proc))
        self.assertEqual(self.mock.mock.write_requests(), [])

    def test_users_v2_403_falls_back_to_v1(self) -> None:
        mock = self.start_mock(fail_users_v2_403=True)
        self.use_creds(mock)
        self.seed_context(TASK_ID, "initialized")
        proc = self.run_cli(
            "prereq",
            *self.stage_args(TASK_ID, "--assignee", "zhangsan", "--confirm", "--json"),
        )
        self.assert_rc(proc, 0)
        self.assertIn("回退 v1", self.all_output(proc))
        self.assertEqual(len(mock.mock.prereq_tasks()), 1)

    def test_users_both_fail_degrades(self) -> None:
        mock = self.start_mock(fail_users_v2_403=True, fail_users_v1_403=True)
        self.use_creds(mock)
        self.seed_context(TASK_ID, "initialized")
        proc = self.run_cli(
            "prereq",
            *self.stage_args(TASK_ID, "--assignee", "zhangsan", "--confirm", "--json"),
        )
        self.assert_rc(proc, 0)
        output = self.all_output(proc)
        self.assertIn("无法拉取禅道用户列表", output)
        self.assertEqual(len(mock.mock.prereq_tasks()), 1)

    def test_priority_mapping(self) -> None:
        proc = self.run_cli(
            *self.prereq_args("--assignee", "zhangsan", "--priority", "P0", "--confirm")
        )
        self.assert_rc(proc, 0)
        body = [
            item
            for item in self.mock.mock.request_records(method="POST")
            if item["path"] == "/api.php/v2/tasks"
        ][0]["body"]
        self.assertEqual(body["pri"], 1)
        proc = self.run_cli(
            *self.prereq_args("--assignee", "lisi", "--confirm")
        )
        self.assert_rc(proc, 0)
        bodies = [
            item
            for item in self.mock.mock.request_records(method="POST")
            if item["path"] == "/api.php/v2/tasks"
        ]
        self.assertEqual(bodies[-1]["body"]["pri"], 3)

    def test_desc_file_used(self) -> None:
        desc_file = self.tmp_path / "prereq.md"
        desc_file.write_text("需要开通训练集数据读取权限", encoding="utf-8")
        proc = self.run_cli(
            *self.prereq_args(
                "--assignee",
                "zhangsan",
                "--desc-file",
                str(desc_file),
                "--confirm",
            )
        )
        self.assert_rc(proc, 0)
        body = [
            item
            for item in self.mock.mock.request_records(method="POST")
            if item["path"] == "/api.php/v2/tasks"
        ][0]["body"]
        self.assertIn("需要开通训练集数据读取权限", body["desc"])


# ---------------------------------------------------------------------------
# 5) feasibility / plan / approve-plan
# ---------------------------------------------------------------------------


class TestFeasibilityPlan(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.mock = self.start_mock()
        self.use_creds(self.mock)
        self.make_git_repo()

    def write_file(self, name: str, content: str) -> Path:
        path = self.tmp_path / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_feasibility_requires_prepared(self) -> None:
        self.seed_context(TASK_ID, "initialized")
        report = self.write_file("feasibility.md", "# 可行性\n可行\n")
        proc = self.run_stage(
            "feasibility", TASK_ID, "--report-file", str(report), "--conclusion", "feasible"
        )
        self.assert_rc(proc, 3)
        self.assertIn("不允许跳阶段", self.all_output(proc))

    def test_feasibility_missing_report_exit_3(self) -> None:
        self.seed_prepared()
        proc = self.run_stage(
            "feasibility",
            TASK_ID,
            "--report-file",
            str(self.tmp_path / "missing.md"),
            "--conclusion",
            "feasible",
        )
        self.assert_rc(proc, 3)
        self.assertIn("不存在", self.all_output(proc))

    def test_feasibility_feasible(self) -> None:
        self.seed_prepared()
        report = self.write_file("feasibility.md", "# 可行性分析\n\n- 依赖满足\n")
        proc = self.run_stage(
            "feasibility",
            TASK_ID,
            "--report-file",
            str(report),
            "--conclusion",
            "feasible",
            "--json",
        )
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertEqual(payload["conclusion"], "feasible")
        context = self.load_context()
        self.assertEqual(context["status"], "feasibility_done")
        self.assertEqual(context["feasibility"]["conclusion"], "feasible")

    def test_feasibility_infeasible(self) -> None:
        self.seed_prepared()
        report = self.write_file("feasibility.md", "# 不可行\n")
        proc = self.run_stage(
            "feasibility",
            TASK_ID,
            "--report-file",
            str(report),
            "--conclusion",
            "infeasible",
            "--blocker",
            "缺少数据权限",
            "--json",
        )
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertEqual(payload["blockers"], ["缺少数据权限"])
        context = self.load_context()
        # infeasible 不再是独立状态：保持 feasibility_done，由 conclusion 控制 plan 门禁
        self.assertEqual(context["status"], "feasibility_done")
        self.assertEqual(context["feasibility"]["conclusion"], "infeasible")
        status_proc = self.run_cli("status", *self.stage_args(), "--json")
        self.assert_rc(status_proc, 0)
        status_payload = self.parse_json_stdout(status_proc)
        self.assertEqual(status_payload["state"]["status"], "feasibility_done")
        self.assertEqual(status_payload["state"]["stageIndex"], 2)
        self.assertIn("infeasible", status_payload["nextStep"])

    def test_feasibility_rerun_recovers_from_infeasible(self) -> None:
        self.seed_context(
            TASK_ID,
            "feasibility_done",
            feasibility={"conclusion": "infeasible", "blockers": ["缺少权限"]},
        )
        report = self.write_file("feasibility-2.md", "# 复查\n依赖已解决\n")
        proc = self.run_stage(
            "feasibility",
            TASK_ID,
            "--report-file",
            str(report),
            "--conclusion",
            "feasible",
            "--json",
        )
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertEqual(payload["conclusion"], "feasible")
        context = self.load_context()
        self.assertEqual(context["status"], "feasibility_done")
        self.assertEqual(context["feasibility"]["conclusion"], "feasible")
        plan = self.write_file("dev-plan.md", "# 计划\n1. 步骤一\n")
        proc = self.run_stage("plan", TASK_ID, "--plan-file", str(plan))
        self.assert_rc(proc, 0)

    def test_plan_requires_feasibility_done(self) -> None:
        self.seed_prepared()
        plan = self.write_file("dev-plan.md", "# 计划\n1. 步骤一\n")
        proc = self.run_stage("plan", TASK_ID, "--plan-file", str(plan))
        self.assert_rc(proc, 3)
        self.assertIn("不允许跳阶段", self.all_output(proc))

    def test_plan_infeasible_rejected(self) -> None:
        self.seed_context(
            TASK_ID,
            "feasibility_done",
            feasibility={"conclusion": "infeasible", "blockers": ["缺少权限"]},
        )
        plan = self.write_file("dev-plan.md", "# 计划\n1. 步骤一\n")
        proc = self.run_stage("plan", TASK_ID, "--plan-file", str(plan))
        self.assert_rc(proc, 3)
        self.assertIn("不允许进入开发计划", self.all_output(proc))

    def test_plan_missing_file_exit_3(self) -> None:
        self.seed_context(
            TASK_ID, "feasibility_done", feasibility={"conclusion": "feasible"}
        )
        proc = self.run_stage(
            "plan", TASK_ID, "--plan-file", str(self.tmp_path / "no-plan.md")
        )
        self.assert_rc(proc, 3)

    def test_plan_success(self) -> None:
        self.seed_context(
            TASK_ID, "feasibility_done", feasibility={"conclusion": "feasible"}
        )
        content = "# 开发计划\n\n1. 搭建骨架\n2. 实现接口\n3. 补测试\n\n## 风险与回滚\n- 风险一\n"
        plan = self.write_file("dev-plan.md", content)
        proc = self.run_stage("plan", TASK_ID, "--plan-file", str(plan), "--json")
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertGreaterEqual(payload["stepCount"], 3)
        self.assertGreaterEqual(payload["riskCount"], 1)
        context = self.load_context()
        self.assertEqual(context["status"], "plan_done")
        self.assertFalse(context["plan"]["approved"])

    def test_plan_rerun_and_approved_rejected(self) -> None:
        self.seed_context(
            TASK_ID, "feasibility_done", feasibility={"conclusion": "feasible"}
        )
        plan = self.write_file("dev-plan.md", "# 计划\n1. 步骤一\n")
        self.assert_rc(self.run_stage("plan", TASK_ID, "--plan-file", str(plan)), 0)
        # 用户修改计划后允许重跑 plan 刷新记录
        self.write_file(
            "dev-plan.md", "# 计划 v2\n1. 步骤一\n2. 步骤二\n3. 步骤三\n## 风险\n- 风险一\n"
        )
        proc = self.run_stage("plan", TASK_ID, "--plan-file", str(plan), "--json")
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertGreaterEqual(payload["stepCount"], 3)
        context = self.load_context()
        self.assertEqual(context["status"], "plan_done")
        self.assertFalse(context["plan"]["approved"])
        # 已批准后拒绝重跑并提示重新审批
        self.assert_rc(self.run_stage("approve-plan", TASK_ID, "--confirm"), 0)
        proc = self.run_stage("plan", TASK_ID, "--plan-file", str(plan))
        self.assert_rc(proc, 3)
        output = self.all_output(proc)
        self.assertIn("已获用户批准", output)
        self.assertIn("重新", output)

    def test_approve_requires_confirm(self) -> None:
        self.seed_context(TASK_ID, "plan_done", plan={"file": "dev-plan.md"})
        proc = self.run_stage("approve-plan", TASK_ID)
        self.assert_rc(proc, 2)
        self.assertIn("--confirm", self.all_output(proc))

    def test_approve_wrong_state_exit_3(self) -> None:
        self.seed_context(TASK_ID, "plan_approved")
        proc = self.run_stage("approve-plan", TASK_ID, "--confirm")
        self.assert_rc(proc, 3)

    def test_approve_success(self) -> None:
        self.seed_context(TASK_ID, "plan_done", plan={"file": "dev-plan.md"})
        proc = self.run_stage("approve-plan", TASK_ID, "--confirm", "--json")
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertEqual(payload["status"], "plan_approved")
        context = self.load_context()
        self.assertTrue(context["plan"]["approved"])


# ---------------------------------------------------------------------------
# 6) start
# ---------------------------------------------------------------------------


class TestStart(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.mock = self.start_mock()
        self.use_creds(self.mock)
        self.make_git_repo()

    def test_start_requires_plan_approved(self) -> None:
        self.seed_prepared(status="plan_done")
        proc = self.run_stage("start", TASK_ID)
        self.assert_rc(proc, 3)
        self.assertIn("不允许跳阶段", self.all_output(proc))

    def test_start_wait_syncs_status(self) -> None:
        self.seed_prepared(status="plan_approved")
        proc = self.run_stage("start", TASK_ID, "--json")
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertEqual(payload["action"], "sync")
        self.assertEqual(payload["startPayload"]["consumed"], 0)
        self.assertEqual(payload["startPayload"]["left"], 8)
        self.assertRegex(
            payload["startPayload"]["realStarted"],
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",
        )
        self.assertEqual(self.mock.mock.task(TASK_ID)["status"], "doing")
        starts = self.mock.mock.request_records(method="PUT", path_contains="/start")
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["path"], f"/api.php/v2/tasks/{TASK_ID}/start")
        context = self.load_context()
        self.assertEqual(context["status"], "developing")
        self.assertIn("startedAt", context["timestamps"])

    def test_start_doing_skips_api(self) -> None:
        self.seed_prepared(status="plan_approved")
        self.mock.mock.set_task_status(TASK_ID, "doing")
        proc = self.run_stage("start", TASK_ID, "--json")
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertEqual(payload["action"], "already-doing")
        self.assertEqual(
            self.mock.mock.request_records(method="PUT", path_contains="/start"), []
        )
        self.assertEqual(self.load_context()["status"], "developing")

    def test_start_done_task_exit_7(self) -> None:
        self.seed_prepared(104, status="plan_approved")
        proc = self.run_stage("start", 104)
        self.assert_rc(proc, 7)

    def test_start_no_sync_status(self) -> None:
        self.seed_prepared(status="plan_approved")
        proc = self.run_stage("start", TASK_ID, "--no-sync-status", "--json")
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertEqual(payload["action"], "local-only")
        self.assertEqual(self.mock.mock.task(TASK_ID)["status"], "wait")
        self.assertEqual(self.mock.mock.write_requests(), [])
        self.assertEqual(self.load_context()["status"], "developing")

    def test_start_api_failure_exit_5(self) -> None:
        self.seed_prepared(status="plan_approved")
        self.mock.mock.fail_start = True
        proc = self.run_stage("start", TASK_ID)
        self.assert_rc(proc, 5)
        self.assertEqual(self.load_context()["status"], "plan_approved")


# ---------------------------------------------------------------------------
# 7) accept（用户验收）
# ---------------------------------------------------------------------------


class TestAccept(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.mock = self.start_mock()
        self.use_creds(self.mock)
        self.make_git_repo()
        self.evidence = self.tmp_path / "evidence.md"
        self.evidence.write_text("# 验收证据\n用户确认功能可用，回归通过。\n", encoding="utf-8")

    def test_accept_requires_developing(self) -> None:
        self.seed_prepared(status="plan_approved")
        proc = self.run_stage(
            "accept", TASK_ID, "--pass", "--evidence-file", str(self.evidence)
        )
        self.assert_rc(proc, 3)

    def test_pass_requires_evidence_exit_2(self) -> None:
        self.seed_developing()
        proc = self.run_stage("accept", TASK_ID, "--pass")
        self.assert_rc(proc, 2)
        self.assertIn("--evidence-file", self.all_output(proc))

    def test_pass_missing_evidence_file_exit_2(self) -> None:
        self.seed_developing()
        proc = self.run_stage(
            "accept", TASK_ID, "--pass", "--evidence-file", str(self.tmp_path / "nope.md")
        )
        self.assert_rc(proc, 2)
        self.assertIn("不存在", self.all_output(proc))

    def test_pass_success(self) -> None:
        self.seed_developing()
        proc = self.run_stage(
            "accept",
            TASK_ID,
            "--pass",
            "--evidence-file",
            str(self.evidence),
            "--notes",
            "用户已确认",
            "--json",
        )
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["status"], "accepted")
        context = self.load_context()
        self.assertEqual(context["status"], "accepted")
        self.assertTrue(context["acceptance"]["passed"])
        acceptance_file = self.task_ws() / "acceptance.md"
        self.assertTrue(acceptance_file.is_file())
        text = acceptance_file.read_text(encoding="utf-8")
        self.assertIn("结论：通过", text)
        self.assertIn("用户确认功能可用", text)

    def test_fail_requires_reason(self) -> None:
        self.seed_developing()
        proc = self.run_stage("accept", TASK_ID, "--fail")
        self.assert_rc(proc, 2)

    def test_fail_records_reason(self) -> None:
        self.seed_developing()
        proc = self.run_stage(
            "accept", TASK_ID, "--fail", "--reason", "接口返回格式不符合 AC", "--json"
        )
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertFalse(payload["passed"])
        context = self.load_context()
        self.assertEqual(context["status"], "developing")
        self.assertFalse(context["acceptance"]["passed"])
        self.assertEqual(len(context["acceptance"]["attempts"]), 1)


# ---------------------------------------------------------------------------
# 8) 工作区定位（--task-id / --workspace / 自动识别 / 复用）
# ---------------------------------------------------------------------------


class TestWorkspaceLocate(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.mock = self.start_mock()
        self.use_creds(self.mock)
        self.do_init(self.mock)

    def test_workspace_option_locates(self) -> None:
        proc = self.run_cli(
            "status", "--workspace", str(self.task_ws()), "--json"
        )
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertEqual(payload["taskId"], TASK_ID)
        self.assertEqual(payload["state"]["status"], "initialized")

    def test_auto_detect_unique_workspace(self) -> None:
        proc = self.run_cli("status", "--json")
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertEqual(payload["taskId"], TASK_ID)

    def test_no_workspace_exit_2(self) -> None:
        shutil.rmtree(self.task_ws())
        proc = self.run_cli("status")
        self.assert_rc(proc, 2)
        self.assertIn("init", self.all_output(proc))

    def test_workspace_without_context_exit_2(self) -> None:
        proc = self.run_cli("status", "--workspace", str(self.tmp_path / "no-ws"))
        self.assert_rc(proc, 2)

    def test_saved_workspace_reuse(self) -> None:
        self.clear_creds_env()
        proc = self.run_cli("status", "--reuse", "--json")
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertEqual(payload["taskId"], TASK_ID)


# ---------------------------------------------------------------------------
# 9) finish：主链路（zip / 附件 / 独立备注 / 完成 / 指派回）
# ---------------------------------------------------------------------------


class TestFinishMain(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.mock = self.start_mock()
        self.use_creds(self.mock)
        self.seed_developing()

    def finish_args(self, *extra) -> list:
        return ["finish", *self.stage_args(TASK_ID, *extra)]

    def post_requests(self, path_contains: str) -> list:
        return self.mock.mock.request_records(method="POST", path_contains=path_contains)

    def test_finish_requires_developing(self) -> None:
        self.seed_prepared(status="plan_approved")
        proc = self.run_cli(*self.finish_args("--confirm"))
        self.assert_rc(proc, 3)

    def test_finish_requires_acceptance(self) -> None:
        self.seed_developing(acceptance={"passed": False})
        proc = self.run_cli(*self.finish_args("--confirm"))
        self.assert_rc(proc, 2)
        self.assertIn("验收尚未通过", self.all_output(proc))

    def test_summary_missing_exit_3(self) -> None:
        (self.task_ws() / "summary.md").unlink()
        proc = self.run_cli(*self.finish_args("--confirm"))
        self.assert_rc(proc, 3)
        self.assertIn("summary.md", self.all_output(proc))

    def test_dry_run_zero_side_effects(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text('{"marker": "keep"}\n', encoding="utf-8")
        config_before = self.config_path.read_bytes()
        config_mtime = self.config_path.stat().st_mtime_ns
        before = self.load_context()["status"]
        proc = self.run_cli(*self.finish_args("--dry-run", "--json"))
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["consumed"], 8)
        names = [item["name"] for item in payload["zipPreview"]["entries"]]
        self.assertIn("summary.md", names)
        self.assertIn("documents/design.md", names)
        self.assertEqual(self.mock.mock.write_requests(), [])
        attachments = self.task_ws() / "attachments"
        self.assertEqual(list(attachments.iterdir()), [])
        context = self.load_context()
        self.assertEqual(context["status"], before)
        self.assertFalse(context.get("delivery"))
        # dry-run 不得写用户配置（内容与 mtime 都不变）
        self.assertEqual(self.config_path.read_bytes(), config_before)
        self.assertEqual(self.config_path.stat().st_mtime_ns, config_mtime)

    def test_not_confirmed_exit_2(self) -> None:
        proc = self.run_cli(*self.finish_args())
        self.assert_rc(proc, 2)
        self.assertIn("--confirm", self.all_output(proc))
        self.assertEqual(self.mock.mock.write_requests(), [])

    def test_confirm_main_chain(self) -> None:
        proc = self.run_cli(*self.finish_args("--confirm", "--json"))
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "publish")
        self.assertEqual(payload["assignBack"], "zhangsan")
        self.assertEqual(payload["assignBackSource"], "requirements_creator")
        self.assertEqual(payload["consumed"], 8)
        self.assertTrue(payload["stages"]["zipGenerated"])
        self.assertTrue(payload["stages"]["attachmentUploaded"])
        self.assertTrue(payload["stages"]["commentAdded"])
        self.assertTrue(payload["stages"]["finishExecuted"])
        delivery = payload["delivery"]
        # P3-1：--json 报告 task.status 用回读后的最终状态
        self.assertEqual(payload["task"]["status"], "done")
        self.assertEqual(delivery.get("finalTaskStatus"), "done")
        self.assertTrue(delivery.get("finalActions", {}).get("finished"))
        self.assertTrue(delivery.get("finalActions", {}).get("commented"))
        # P2-1/P2-2：本地时间格式（不带时区后缀）
        self.assertRegex(
            payload["realStarted"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
        )
        self.assertRegex(
            payload["finishedDate"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
        )
        zip_path = self.resolve_artifact(delivery["zipFile"])
        self.assertTrue(zip_path.is_file())
        self.assertGreater(delivery["zipSize"], 0)
        self.assertTrue(delivery["zipSha256"])
        self.assertIsNotNone(delivery["attachmentFileId"])
        self.assertIsNotNone(delivery["commentActionId"])

        # zip 内容
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            self.assertIn("manifest.json", names)
            self.assertIn("summary.md", names)
            self.assertIn("feasibility.md", names)
            self.assertIn("dev-plan.md", names)
            self.assertIn("acceptance.md", names)
            self.assertIn("documents/design.md", names)
            self.assertIn("git-facts.txt", names)
            self.assertNotIn("context.json", names)
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            self.assertEqual(manifest["taskId"], str(TASK_ID))
            self.assertEqual(manifest["taskName"], "训练集管理-基础框架")
            file_names = [item["name"] for item in manifest["files"]]
            self.assertIn("summary.md", file_names)
            self.assertIn("documents/design.md", file_names)
            git_facts = archive.read("git-facts.txt").decode("utf-8")
            self.assertIn("分支：main", git_facts)
            self.assertIn(self.commit, git_facts)

        # 上传：v2 multipart 字段 file/objectType/objectID
        uploads = [
            item
            for item in self.mock.mock.request_records(method="POST")
            if item["path"] == "/api.php/v2/files"
        ]
        self.assertEqual(len(uploads), 1)
        upload = uploads[0]
        upload_fields = upload["body"]["fields"]
        self.assertEqual(upload_fields.get("objectType"), "task")
        self.assertEqual(str(upload_fields.get("objectID")), str(TASK_ID))
        files = upload["body"]["files"]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["field"], "file")
        self.assertEqual(files[0]["filename"], delivery["zipName"])
        self.assertGreater(files[0]["size"], 0)

        # 独立备注
        comments = [
            item
            for item in self.mock.mock.request_records(method="POST")
            if item["path"] == "/api.php/v2/actions/comment"
        ]
        self.assertEqual(len(comments), 1)
        self.assertEqual((comments[0].get("query") or {}).get("objectType"), "task")
        self.assertEqual(str((comments[0].get("query") or {}).get("objectID")), str(TASK_ID))
        self.assertIn("开发过程总结", comments[0]["body"]["comment"])

        # finish payload + 回读
        finishes = self.mock.mock.request_records(method="PUT", path_contains="/finish")
        self.assertEqual(len(finishes), 1)
        finish_body = finishes[0]["body"]
        self.assertEqual(float(finish_body["currentConsumed"]), 8.0)
        self.assertEqual(finish_body["assignedTo"], "zhangsan")
        self.assertRegex(
            finish_body["realStarted"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
        )
        self.assertRegex(
            finish_body["finishedDate"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
        )
        self.assertIn("comment", finish_body)
        task = self.mock.mock.task(TASK_ID)
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["assignedTo"], "zhangsan")
        self.assertEqual(len(task["files"]), 1)
        self.assertTrue(
            any(
                "开发过程总结" in item.get("comment", "")
                for item in task["actions"]
                if item.get("action") == "commented"
            )
        )

        # context 更新
        context = self.load_context()
        self.assertEqual(context["status"], "finished")
        self.assertIn("finishedAt", context["timestamps"])
        self.assertEqual(context["delivery"]["zipName"], delivery["zipName"])
        self.assertEqual(context["delivery"]["attachmentFileId"], delivery["attachmentFileId"])
        self.assertEqual(context["delivery"]["commentActionId"], delivery["commentActionId"])

    def test_consumed_override_and_assign_back(self) -> None:
        proc = self.run_cli(
            *self.finish_args("--confirm", "--consumed", "2.5", "--assign-back", "lisi", "--json")
        )
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertEqual(payload["consumed"], 2.5)
        self.assertEqual(payload["assignBack"], "lisi")
        body = self.mock.mock.request_records(method="PUT", path_contains="/finish")[0]["body"]
        self.assertEqual(float(body["currentConsumed"]), 2.5)
        self.assertEqual(body["assignedTo"], "lisi")
        self.assertEqual(self.mock.mock.task(TASK_ID)["assignedTo"], "lisi")

    def test_extra_files_and_doc_dir(self) -> None:
        extra = self.tmp_path / "extra-report.txt"
        extra.write_text("附加报告\n", encoding="utf-8")
        doc_dir = self.tmp_path / "more-docs"
        doc_dir.mkdir()
        (doc_dir / "note.md").write_text("# 补充说明\n", encoding="utf-8")
        proc = self.run_cli(
            *self.finish_args(
                "--confirm",
                "--extra-file",
                str(extra),
                "--doc-dir",
                str(doc_dir),
                "--json",
            )
        )
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        with zipfile.ZipFile(self.resolve_artifact(payload["delivery"]["zipFile"])) as archive:
            names = archive.namelist()
        self.assertIn("extra/extra-report.txt", names)
        self.assertIn("documents/note.md", names)

    def test_zip_contains_no_credentials(self) -> None:
        proc = self.run_cli(*self.finish_args("--confirm", "--json"))
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        with zipfile.ZipFile(self.resolve_artifact(payload["delivery"]["zipFile"])) as archive:
            for name in archive.namelist():
                content = archive.read(name)
                self.assertNotIn(PASSWORD.encode("utf-8"), content)
                self.assertNotIn(self.mock.token.encode("utf-8"), content)


# ---------------------------------------------------------------------------
# 10) finish：兜底与降级
# ---------------------------------------------------------------------------


class TestFinishFallbacks(BaseCase):
    def finish_args(self, *extra, task_id=TASK_ID) -> list:
        return ["finish", *self.stage_args(task_id, *extra)]

    def post_requests(self, path_contains: str) -> list:
        return self.mock.mock.request_records(method="POST", path_contains=path_contains)

    def test_requirement_creator_missing_uses_task_creator(self) -> None:
        self.mock = self.start_mock()
        self.use_creds(self.mock)
        self.seed_developing(
            102,
            creator={
                "requirementCreator": "",
                "taskCreator": "lisi",
                "resolvedAssignee": "lisi",
                "source": "task_creator",
            },
        )
        proc = self.run_cli(*self.finish_args("--confirm", "--json", task_id=102))
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertEqual(payload["assignBack"], "lisi")
        body = self.mock.mock.request_records(
            method="PUT", path_contains="/finish"
        )[0]["body"]
        self.assertEqual(body["assignedTo"], "lisi")
        self.assertEqual(self.mock.mock.task(102)["assignedTo"], "lisi")

    def test_creator_and_requirement_missing_exit_2(self) -> None:
        task = {
            "id": 201,
            "name": "无创建人任务",
            "status": "wait",
            "story": 0,
            "execution": 11,
            "project": 1,
            "module": 0,
            "estimate": 2,
            "openedBy": "",
            "assignedTo": {"account": "admin", "realname": "管理员"},
            "files": [],
            "actions": [],
        }
        self.mock = self.start_mock(tasks={201: task})
        self.use_creds(self.mock)
        self.seed_developing(
            201,
            task_snapshot={
                "id": 201,
                "name": "无创建人任务",
                "status": "wait",
                "story": 0,
                "execution": 11,
                "openedBy": "",
                "estimate": 2,
            },
            story_snapshot={},
            creator={
                "requirementCreator": "",
                "taskCreator": "",
                "resolvedAssignee": "",
                "source": "",
            },
        )
        proc = self.run_cli(*self.finish_args("--confirm", task_id=201))
        self.assert_rc(proc, 2)
        self.assertIn("--assign-back", self.all_output(proc))

    def test_v1_login_fallback_finish_still_works(self) -> None:
        # v2 login 404 → v1 /tokens 兜底；其余请求仍走 v2 API，finish 主链路可完成
        mock = self.start_mock(v2_login_404=True)
        self.mock = mock
        self.use_creds(mock)
        self.seed_developing()
        proc = self.run_cli(*self.finish_args("--confirm", "--json"))
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertEqual(payload["login"]["loginMode"], "v1-fallback")
        self.assertTrue(payload["stages"]["finishExecuted"])
        self.assertEqual(self.mock.mock.task(TASK_ID)["status"], "done")

    def test_no_attach_with_web_available(self) -> None:
        self.mock = self.start_mock()
        self.use_creds(self.mock)
        self.seed_developing()
        proc = self.run_cli(*self.finish_args("--confirm", "--no-attach", "--json"))
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertTrue(payload["stages"]["attachmentSkipped"])
        self.assertFalse(payload["stages"]["attachmentUploaded"])
        self.assertEqual(
            [
                item
                for item in self.mock.mock.request_records(method="POST")
                if item["path"] == "/api.php/v2/files"
            ],
            [],
        )

    def test_attachment_idempotent(self) -> None:
        self.mock = self.start_mock()
        self.use_creds(self.mock)
        self.seed_developing()
        zip_name = f"{TASK_ID}-delivery-fixed.zip"
        with self.mock.mock.lock:
            self.mock.mock.tasks[TASK_ID]["files"].append(
                {
                    "id": 999,
                    "title": zip_name,
                    "filename": zip_name,
                    "url": "/file-read-999.zip",
                }
            )
        proc = self.run_cli(
            *self.finish_args("--confirm", "--zip-name", zip_name, "--json")
        )
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertTrue(payload["stages"]["attachmentSkipped"])
        self.assertEqual(payload["delivery"]["attachmentFileId"], 999)
        uploads = [
            item
            for item in self.mock.mock.request_records(method="POST")
            if item["path"] == "/api.php/v2/files"
        ]
        self.assertEqual(uploads, [])
        self.assertEqual(len(self.mock.mock.files_of(TASK_ID)), 1)

    def test_upload_failure_stops_and_reports_stage(self) -> None:
        self.mock = self.start_mock(fail_upload=True)
        self.use_creds(self.mock)
        self.seed_developing()
        proc = self.run_cli(*self.finish_args("--confirm"))
        self.assert_rc(proc, 5)
        output = self.all_output(proc)
        self.assertIn("zip 已生成", output)
        self.assertIn("收尾中断", output)
        context = self.load_context()
        self.assertEqual(context["status"], "developing")
        self.assertTrue(context["delivery"]["stages"]["zipGenerated"])
        self.assertFalse(context["delivery"]["stages"]["attachmentUploaded"])
        self.assertFalse(context["delivery"]["stages"]["finishExecuted"])

    def test_comment_failure_degrades(self) -> None:
        self.mock = self.start_mock(fail_comment=True)
        self.use_creds(self.mock)
        self.seed_developing()
        proc = self.run_cli(*self.finish_args("--confirm", "--json"))
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertTrue(payload["stages"]["commentDegraded"])
        self.assertTrue(payload["stages"]["finishExecuted"])
        task = self.mock.mock.task(TASK_ID)
        finished = [item for item in task["actions"] if item.get("action") == "finished"]
        self.assertIn("开发过程总结", finished[-1].get("comment", ""))

    def test_task_done_exit_7(self) -> None:
        self.mock = self.start_mock()
        self.use_creds(self.mock)
        self.seed_developing(104)
        proc = self.run_cli(*self.finish_args("--confirm", task_id=104))
        self.assert_rc(proc, 7)
        self.assertIn("不允许收尾", self.all_output(proc))

    def test_finish_readback_actions_missing_exit_5(self) -> None:
        self.mock = self.start_mock(omit_finish_action=True)
        self.use_creds(self.mock)
        self.seed_developing()
        proc = self.run_cli(*self.finish_args("--confirm"))
        self.assert_rc(proc, 5)
        self.assertIn("actions", self.all_output(proc))
        self.assertEqual(self.load_context()["status"], "developing")

    def test_finish_business_fail_exit_5(self) -> None:
        self.mock = self.start_mock(fail_finish=True)
        self.use_creds(self.mock)
        self.seed_developing()
        proc = self.run_cli(*self.finish_args("--confirm"))
        self.assert_rc(proc, 5)
        self.assertEqual(self.load_context()["status"], "developing")


# ---------------------------------------------------------------------------
# 11) status / 网络与接口失败 / 安全
# ---------------------------------------------------------------------------


class TestStatusAndFailures(BaseCase):
    def test_status_after_init(self) -> None:
        mock = self.start_mock()
        self.use_creds(mock)
        self.do_init(mock)
        proc = self.run_cli("status", *self.stage_args(), "--json")
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertEqual(payload["state"]["status"], "initialized")
        self.assertEqual(payload["task"]["status"], "wait")
        self.assertEqual(payload["task"]["filesCount"], 0)
        self.assertIn("prepare", payload["nextStep"])
        self.assertEqual(payload["login"]["loginMode"], "v2")

    def test_status_next_steps(self) -> None:
        mock = self.start_mock()
        self.use_creds(mock)
        self.make_git_repo()
        self.seed_prepared()
        proc = self.run_cli("status", *self.stage_args(), "--json")
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        self.assertEqual(payload["state"]["status"], "prepared")
        self.assertIn("feasibility", payload["nextStep"])
        self.assertIn("repoUrl", payload["prepare"])

    def test_status_login_failure_exit_5(self) -> None:
        mock = self.start_mock(fail_v2_login=True)
        self.use_creds(mock)
        self.seed_context(TASK_ID, "initialized")
        proc = self.run_cli("status", *self.stage_args())
        self.assert_rc(proc, 5)

    def test_status_network_failure_exit_6(self) -> None:
        mock = self.start_mock()
        base_url = mock.base_url
        self.use_creds(mock)
        self.seed_context(TASK_ID, "initialized")
        mock.stop()
        self.servers.remove(mock)
        proc = self.run_cli(
            "status", *self.stage_args(), "--base-url", base_url, "--username", "admin", "--timeout", "2"
        )
        self.assert_rc(proc, 6)
        self.assertIn("无法连接", proc.stderr)

    def test_network_failure_init_exit_6(self) -> None:
        mock = self.start_mock()
        base_url = mock.base_url
        self.use_creds(mock)
        mock.stop()
        self.servers.remove(mock)
        proc = self.run_cli(*self.init_args_type(base_url))
        self.assert_rc(proc, 6)

    def init_args_type(self, base_url: str) -> list:
        return [
            "init",
            "--task-id",
            str(TASK_ID),
            "--base-url",
            base_url,
            "--username",
            "admin",
            "--timeout",
            "2",
        ]

    def test_login_fallback_failure_exit_5(self) -> None:
        mock = self.start_mock(v2_login_404=True, fail_v1_login=True)
        self.use_creds(mock)
        proc = self.run_cli(*self.init_args_type(mock.base_url))
        self.assert_rc(proc, 5)

    def test_login_failure_exit_5(self) -> None:
        mock = self.start_mock(fail_v2_login=True)
        self.use_creds(mock)
        proc = self.run_cli(*self.init_args_type(mock.base_url))
        self.assert_rc(proc, 5)

    def test_secrets_not_persisted_or_leaked(self) -> None:
        mock = self.start_mock(
            users=[
                {"account": "admin", "realname": "管理员", "password": SECRET_PASSWORD},
                {"account": "zhangsan", "realname": "张三", "password": SECRET_PASSWORD},
                {"account": "lisi", "realname": "李四", "password": SECRET_PASSWORD},
            ]
        )
        self.use_creds(mock, password=SECRET_PASSWORD)
        self.do_init(mock)
        self.seed_developing()
        proc = self.run_cli("finish", *self.stage_args(TASK_ID, "--confirm", "--json"))
        self.assert_rc(proc, 0)
        payload = self.parse_json_stdout(proc)
        combined = proc.stdout + "\n" + proc.stderr
        self.assertNotIn(SECRET_PASSWORD, combined)
        self.assertNotIn(mock.token, combined)
        context_text = self.context_path().read_text(encoding="utf-8")
        self.assertNotIn(SECRET_PASSWORD, context_text)
        self.assertNotIn(mock.token, context_text)
        self.assertNotIn("ss_", context_text)
        with zipfile.ZipFile(self.resolve_artifact(payload["delivery"]["zipFile"])) as archive:
            for name in archive.namelist():
                self.assertNotIn(SECRET_PASSWORD.encode("utf-8"), archive.read(name))
        config_text = self.config_path.read_text(encoding="utf-8")
        self.assertNotIn(SECRET_PASSWORD, config_text)


# ---------------------------------------------------------------------------
# 测试入口：打印 PASS 计数，失败时非零退出
# ---------------------------------------------------------------------------


class _CountingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.passed = 0

    def addSuccess(self, test) -> None:  # noqa: N802
        super().addSuccess(test)
        self.passed += 1


def _run_suite() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(
        stream=sys.stdout, verbosity=2, resultclass=_CountingResult
    )
    result = runner.run(suite)
    failed = len(result.failures) + len(result.errors)
    print(
        f"\n==== 测试结果：PASS {result.passed}，FAIL {failed}，"
        f"SKIP {len(result.skipped)}，TOTAL {result.testsRun} ====",
        flush=True,
    )
    if result.wasSuccessful():
        print("结果：ALL PASS", flush=True)
        return 0
    print("结果：FAILED（请看上方失败明细）", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(_run_suite())


