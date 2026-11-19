#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""禅道研发任务执行与收尾脚本（zentao-dev-task-flow skill 配套）。

管理**已存在的禅道研发任务**的执行生命周期：
  init（接入任务）→ prepare（环境/Git 仓库）→ feasibility（可行性）→ plan/approve-plan
  → start（开始开发）→ accept（用户验收）→ finish（zip 附件 + 独立备注 + 完成任务
  + 指派回需求创建人）；并支持 prereq（创建前置任务指派协同人员）。

关键设计：
  * 只使用 Python 标准库（3.11+），不依赖 yaml/requests；
  * 禅道 22.6：统一 v2 API（`/api.php/v2`，请求头 `Token`）；登录 `POST /users/login`，
    仅 v2 登录 404 时回退 v1 `POST /api.php/v1/tokens`；创建任务/开始/完成/附件上传/评论全部走 v2；
  * v2 业务失败为 HTTP 200 + `status:fail`（message 兼容 string/object/array），
    401/403/404 为 HTTP 码；附件上传失败严格停止，评论失败降级并入 finish comment；
  * 状态机严格：initialized → prepared → feasibility_done → plan_done → plan_approved
    → developing → accepted → finished，命令校验前置状态，不允许跳步；
  * 本地工作区 `.zentao-dev-task/<taskId>/` 保存 context.json（原子写、不含密码/token）；
  * 所有禅道写操作（prereq 创建前置任务、start、finish 附件/备注/完成）都必须
    dry-run 计划 + 用户明确确认（`--confirm`）；本地 context 写入除外；
  * 密码只接受 `--password-stdin` / 环境变量 `ZENTAO_PASSWORD`，Windows 用 DPAPI 保存；
    密码、token 不打印、不写入 context/zip/仓库。

退出码：
  0 成功（含 dry-run/无操作）；1 未预期异常（ZENTAO_DEV_TASK_DEBUG=1 显示堆栈）；
  2 参数/配置/凭据/用户确认门槛缺失或非法；3 本地工作区/状态机/文件门槛未通过；
  5 禅道登录/接口失败；6 网络失败；7 禅道任务状态冲突。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import getpass
import hashlib
import html as html_lib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:  # Windows 控制台可能是 cp936，统一按 UTF-8 输出
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

SCRIPT_VERSION = "1.0.0"
SKILL_NAME = "zentao-dev-task-flow"
CONFIG_ENV = "ZENTAO_DEV_TASK_CONFIG"
DEBUG_ENV = "ZENTAO_DEV_TASK_DEBUG"
ENV_BASE_URL = "ZENTAO_BASE_URL"
ENV_USERNAME = "ZENTAO_USERNAME"
ENV_PASSWORD = "ZENTAO_PASSWORD"

DEFAULT_BASE_URL = "http://192.168.110.25:30783"
DEFAULT_WORKSPACE_ROOT = ".zentao-dev-task"
DEFAULT_CONFIG_REL = Path(".dev-skills") / SKILL_NAME / "zentao.json"
HTTP_TIMEOUT_DEFAULT = 20
USERS_PAGE_SIZE = 200

STATE_ORDER = [
    "initialized",
    "prepared",
    "feasibility_done",
    "plan_done",
    "plan_approved",
    "developing",
    "accepted",
    "finished",
]
STATE_LABELS = {
    "initialized": "已接入任务",
    "prepared": "已收集环境与仓库",
    "feasibility_done": "已完成可行性分析",
    "plan_done": "已产出开发计划（待用户审核）",
    "plan_approved": "开发计划已获用户批准",
    "developing": "开发中",
    "accepted": "用户验收通过",
    "finished": "已完成收尾",
}
STATE_INDEX = {name: index for index, name in enumerate(STATE_ORDER)}
INIT_ALLOWED_TASK_STATUS = {"wait", "doing", "pause"}
FINISH_FORBIDDEN_TASK_STATUS = {"done", "closed", "cancel"}
START_ALLOWED_TASK_STATUS = {"wait", "doing"}

TASK_TYPES = (
    "design",
    "devel",
    "request",
    "test",
    "study",
    "discuss",
    "ui",
    "affair",
    "misc",
)
PRIORITY_MAP = {"P0": 1, "P1": 2, "P2": 3, "P3": 4}
PRIORITY_LABELS = {1: "P0", 2: "P1", 3: "P2", 4: "P3"}
DEFAULT_TASK_TYPE = "devel"
DEFAULT_PRIORITY = "P2"

TOOLCHAIN_COMMANDS = (
    ("python", ["python", "--version"]),
    ("node", ["node", "--version"]),
    ("java", ["java", "-version"]),
    ("mvn", ["mvn", "-v"]),
    ("gradle", ["gradle", "--version"]),
    ("dotnet", ["dotnet", "--version"]),
    ("docker", ["docker", "--version"]),
    ("git", ["git", "--version"]),
)

CONFIG_ALLOWED_EXTS = {
    "zip",
    "md",
    "txt",
    "doc",
    "docx",
    "pdf",
    "xls",
    "xlsx",
    "ppt",
    "pptx",
    "png",
    "jpg",
    "jpeg",
    "gif",
    "log",
}

# ---------------------------------------------------------------------------
# 控制台输出（--json 模式 stdout 只输出一个 JSON 对象，人读信息走 stderr）
# ---------------------------------------------------------------------------


class Console:
    def __init__(self) -> None:
        self.json_mode = False
        self.json_emitted = False

    def set_json_mode(self, enabled: bool) -> None:
        self.json_mode = bool(enabled)

    def human(self, message: str = "") -> None:
        print(message, file=sys.stderr if self.json_mode else sys.stdout)
        self._flush()

    def error(self, message: str = "") -> None:
        print(message, file=sys.stderr)
        self._flush(sys.stderr)

    def emit_json(self, payload: Any) -> None:
        self.json_emitted = True
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stdout)
        self._flush()

    @staticmethod
    def _flush(stream: Any = None) -> None:
        try:
            (stream or sys.stdout).flush()
        except Exception:  # pragma: no cover
            pass


CONSOLE = Console()


def say(message: str = "") -> None:
    CONSOLE.human(message)


def step(index: int, total: int, title: str) -> None:
    CONSOLE.human(f"[{index}/{total}] {title}")


def bullet(message: str, indent: str = "  - ") -> None:
    CONSOLE.human(f"{indent}{message}")


def warn(message: str) -> None:
    CONSOLE.human(f"  ! {message}")


def emit_json(payload: Any) -> None:
    CONSOLE.emit_json(payload)


# ---------------------------------------------------------------------------
# 异常层级（与退出码一一对应）
# ---------------------------------------------------------------------------


class FlowError(Exception):
    exit_code = 1
    hint = ""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class ConfigError(FlowError):
    exit_code = 2
    hint = "缺失的信息向用户询问；不要把密码回显到日志。"


class GateError(FlowError):
    exit_code = 3
    hint = "请先补齐本地工作区/状态机/文件门槛后重试；不允许跳阶段。"


class ZenTaoError(FlowError):
    exit_code = 5
    hint = "请核对禅道地址、账号权限、任务是否可见以及接口能力。"

    def __init__(self, message: str, detail: str = "", *, status: Optional[int] = None) -> None:
        super().__init__(message, detail)
        self.status = status


class NetworkError(FlowError):
    exit_code = 6
    hint = "请检查禅道地址、网络连通性、代理或防火墙。"


class StateConflictError(FlowError):
    exit_code = 7
    hint = "禅道任务状态与当前操作冲突；请回读任务状态并把下一步建议转告用户。"


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

SOURCE_ARG = "参数"
SOURCE_ENV = "环境变量"
SOURCE_SKILLENV = ".skillenv"
SOURCE_CONFIG = "用户配置（上次保存）"
SOURCE_DEFAULT = "内置默认值"


def now_str() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def today_str() -> str:
    return date.today().isoformat()


def _normalize_url(url: Optional[str]) -> str:
    return (url or "").strip().rstrip("/")


def _excerpt(text: Any, limit: int = 200) -> str:
    compact = re.sub(r"\s+", " ", "" if text is None else str(text)).strip()
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_str_list(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        result: List[str] = []
        for item in value:
            if isinstance(item, dict):
                text = _as_str(item.get("id") or item.get("name") or item.get("account"))
            else:
                text = _as_str(item)
            if text:
                result.append(text)
        return result
    return [_as_str(value)]


def _as_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{label} 必须是整数：{value!r}")
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} 必须是整数：{value!r}") from exc


def _opt_int(value: Any, label: str) -> Optional[int]:
    if value is None or _as_str(value) == "":
        return None
    return _as_int(value, label)


def _as_float(value: Any, label: str) -> Optional[float]:
    if value is None or _as_str(value) == "":
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} 必须是数字：{value!r}") from exc


def _has_non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return bool(value)


def parse_priority(value: Any) -> Tuple[str, int]:
    if isinstance(value, bool):
        raise ConfigError(f"优先级取值不合法：{value!r}（可选 P0/P1/P2/P3）")
    if isinstance(value, int):
        if value in PRIORITY_LABELS:
            return PRIORITY_LABELS[value], value
        raise ConfigError(f"优先级取值不合法：{value}（可选 P0/P1/P2/P3 或 1/2/3/4）")
    text = _as_str(value).upper()
    if not text:
        raise ConfigError("优先级不能为空（可选 P0/P1/P2/P3）")
    if text in PRIORITY_MAP:
        return text, PRIORITY_MAP[text]
    if text.isdigit() and int(text) in PRIORITY_LABELS:
        return PRIORITY_LABELS[int(text)], int(text)
    raise ConfigError(f"优先级取值不合法：{value!r}（可选 P0/P1/P2/P3 或 1/2/3/4）")


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_date_text(value: Any, label: str) -> str:
    text = _as_str(value)
    if not text:
        raise ConfigError(f"{label} 不能为空（格式 YYYY-MM-DD）")
    day = text[:10]
    if not _DATE_RE.match(day):
        raise ConfigError(f"{label} 日期格式不正确（应为 YYYY-MM-DD）：{text}")
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError as exc:
        raise ConfigError(f"{label} 不是合法日期：{text}") from exc
    return day


def _date_or_none(value: Any) -> str:
    text = _as_str(value)[:10]
    if text and _DATE_RE.match(text):
        return text
    return ""


def account_of(value: Any) -> str:
    """禅道 v1 的 openedBy/assignedTo 可能是对象或字符串，统一取 account。"""
    if isinstance(value, dict):
        return _as_str(value.get("account") or value.get("name"))
    return _as_str(value)


def task_link(base_url: str, task_id: Any) -> str:
    return f"{_normalize_url(base_url)}/task-view-{task_id}.html"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human_size(size: int) -> str:
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def _to_win_norm(path: Path) -> str:
    try:
        return os.path.normcase(os.path.abspath(str(path)))
    except OSError:  # pragma: no cover
        return str(path)


def _read_text_file(path: Path, label: str) -> str:
    if not path.is_file():
        raise GateError(f"{label} 不存在：{path}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise GateError(f"{label} 不是 UTF-8 文本：{path}（{exc}）") from exc
    if not text.strip():
        raise GateError(f"{label} 内容为空：{path}")
    return text


def markdown_to_html(markdown: str) -> str:
    """把 summary/acceptance 等 Markdown 转成适合禅道备注的 HTML（宽松、无第三方库）。"""
    lines = str(markdown).splitlines()
    parts: List[str] = []
    in_code = False
    in_list = False
    code_lines: List[str] = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            parts.append("</ul>")
            in_list = False

    for raw_line in lines:
        line = raw_line.rstrip()
        if line.strip().startswith("```"):
            if in_code:
                parts.append("<pre>" + html_lib.escape("\n".join(code_lines), quote=False) + "</pre>")
                code_lines = []
                in_code = False
            else:
                close_list()
                in_code = True
            continue
        if in_code:
            code_lines.append(raw_line)
            continue
        stripped = line.strip()
        if not stripped:
            close_list()
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            close_list()
            level = min(6, max(3, len(heading.group(1)) + 2))
            parts.append(f"<h{level}>{html_lib.escape(heading.group(2), quote=False)}</h{level}>")
            continue
        item = re.match(r"^[-*+]\s+(.*)$", stripped) or re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if item:
            if not in_list:
                parts.append("<ul>")
                in_list = True
            parts.append(f"<li>{html_lib.escape(item.group(1), quote=False)}</li>")
            continue
        close_list()
        parts.append(f"<p>{html_lib.escape(stripped, quote=False)}</p>")
    if in_code:
        parts.append("<pre>" + html_lib.escape("\n".join(code_lines), quote=False) + "</pre>")
    close_list()
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 用户配置 / DPAPI / .skillenv
# ---------------------------------------------------------------------------


def default_config_path() -> Path:
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env).expanduser()
    return Path.home() / DEFAULT_CONFIG_REL


def _read_json(path: Path, label: str) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{label}不是 UTF-8 文本：{path}（{exc}）") from exc
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{label}不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{label}格式不正确（顶层必须是 JSON 对象）：{path}")
    return data


def load_user_config(path: Path) -> dict:
    return _read_json(path, "用户配置文件") or {}


def save_user_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(payload, encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, path)


def discover_skillenv(hint: Optional[Path]) -> Optional[Path]:
    cwd_candidate = Path.cwd() / ".skillenv"
    if cwd_candidate.is_file():
        return cwd_candidate
    if hint is None:
        return None
    probe = hint if hint.is_dir() else hint.parent
    probe = probe.resolve() if probe.exists() else probe
    while True:
        candidate = probe / ".skillenv"
        if candidate.is_file():
            return candidate
        if (probe / ".git").exists() or probe.parent == probe:
            break
        probe = probe.parent
    return None


def load_skillenv(path: Optional[Path]) -> dict:
    if path is None or not path.is_file():
        return {}
    try:
        data = _read_json(path, ".skillenv ")
    except ConfigError as exc:
        warn(f".skillenv 无法解析，已忽略：{exc.message}")
        return {}
    if not data:
        return {}
    section = data.get(SKILL_NAME) or {}
    if not isinstance(section, dict):
        return {}
    safe: Dict[str, Any] = {}
    for key, value in section.items():
        lowered = str(key).lower()
        if "password" in lowered or "token" in lowered or "secret" in lowered:
            warn(f".skillenv 中禁止保存密码/token 等敏感字段（已忽略 {key}）：{path}")
            continue
        safe[str(key)] = value
    return safe


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise OSError("DPAPI 仅适用于 Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = ctypes.create_string_buffer(data, len(data))
    in_blob = _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DATA_BLOB()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "zentao-dev-task-flow credential",
            None,
            None,
            None,
            flags,
            ctypes.byref(out_blob),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), None, None, None, None, flags, ctypes.byref(out_blob)
        )
    if not ok:
        raise OSError(f"DPAPI 调用失败（错误码 {ctypes.get_last_error()}）")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def protect_password(password: str) -> Optional[dict]:
    if not password:
        return None
    if sys.platform == "win32":
        try:
            encrypted = _dpapi(password.encode("utf-8"), protect=True)
            return {"scheme": "dpapi", "value": base64.b64encode(encrypted).decode("ascii")}
        except OSError as exc:
            warn(f"无法用 DPAPI 加密保存密码，本次不记住密码：{exc}")
            return None
    return {"scheme": "plain", "value": password}


def unprotect_password(saved: Any) -> Optional[str]:
    if not saved:
        return None
    if isinstance(saved, str):
        return saved
    if not isinstance(saved, dict):
        return None
    scheme = saved.get("scheme")
    value = saved.get("value")
    if not value:
        return None
    if scheme == "dpapi":
        if sys.platform != "win32":
            return None
        try:
            return _dpapi(base64.b64decode(value), protect=False).decode("utf-8")
        except OSError:
            return None
    if scheme == "plain":
        return str(value)
    return None


def password_saved_description(saved: Any) -> str:
    if not saved:
        return "未保存"
    scheme = saved.get("scheme") if isinstance(saved, dict) else "legacy"
    if scheme == "dpapi":
        return "已保存（Windows DPAPI 加密，仅当前 Windows 用户可解密）"
    if scheme == "plain":
        return "已保存（明文，0600；非 Windows 平台，请注意风险）"
    return "已保存（旧格式）"


def _read_password_from_stdin() -> str:
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass("禅道密码（输入不显示）：")
    line = sys.stdin.readline() if sys.stdin is not None else ""
    password = line.rstrip("\r\n").lstrip("\ufeff")
    if not password:
        raise ConfigError("通过 --password-stdin 未读到密码（stdin 为空）")
    return password


@dataclass
class Credentials:
    base_url: str = ""
    base_url_source: str = ""
    username: str = ""
    password: str = ""
    credential_source: str = ""
    same_instance: bool = False
    reuse_items: List[str] = field(default_factory=list)


def _pick(candidates: Sequence[Tuple[str, Any]]) -> Tuple[Any, str]:
    for source, value in candidates:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value, source
    return None, ""


def resolve_credentials(
    args: argparse.Namespace, config: dict, skillenv: dict
) -> Credentials:
    """解析凭据；凡复用用户配置的值都记入 reuse_items，由调用方统一做 --reuse 门槛。"""
    result = Credentials()
    base_value, base_source = _pick(
        (
            ("参数 --base-url", getattr(args, "base_url", None)),
            (f"环境变量 {ENV_BASE_URL}", os.environ.get(ENV_BASE_URL)),
            (SOURCE_SKILLENV, skillenv.get("baseUrl")),
            (SOURCE_CONFIG, config.get("baseUrl")),
        )
    )
    if base_value is None:
        result.base_url = _normalize_url(DEFAULT_BASE_URL)
        result.base_url_source = SOURCE_DEFAULT
    else:
        result.base_url = _normalize_url(_as_str(base_value))
        result.base_url_source = base_source
    if not re.match(r"^https?://[^\s]+$", result.base_url, re.I):
        raise ConfigError(
            f"禅道地址格式不正确（需以 http:// 或 https:// 开头）："
            f"{result.base_url_source} = {result.base_url}"
        )
    if result.base_url_source == SOURCE_CONFIG:
        result.reuse_items.append(f"禅道地址：{result.base_url}（来源：{SOURCE_CONFIG}）")

    config_base = _normalize_url(config.get("baseUrl"))
    result.same_instance = bool(config_base) and config_base == result.base_url

    stdin_password = ""
    if getattr(args, "password_stdin", False):
        stdin_password = _read_password_from_stdin()
    env_password = os.environ.get(ENV_PASSWORD) or ""
    explicit_username = _as_str(getattr(args, "username", None)) or _as_str(
        os.environ.get(ENV_USERNAME)
    )
    if stdin_password:
        explicit_password = stdin_password
        password_source = "参数 --password-stdin"
    elif env_password:
        explicit_password = env_password
        password_source = f"环境变量 {ENV_PASSWORD}"
    else:
        explicit_password = ""
        password_source = ""

    saved_user = _as_str(config.get("username"))
    saved_password = unprotect_password(config.get("password")) if result.same_instance else None
    if explicit_username and explicit_password:
        result.username = explicit_username
        result.password = explicit_password
        result.credential_source = f"用户当次提供（密码来源：{password_source}）"
        return result
    if explicit_username or explicit_password:
        raise ConfigError(
            "用户名与密码必须同时提供：请通过 --username 与 --password-stdin"
            "（或环境变量 ZENTAO_USERNAME/ZENTAO_PASSWORD）一起传入"
        )
    if saved_user and saved_password:
        result.username = saved_user
        result.password = saved_password
        result.credential_source = SOURCE_CONFIG
        result.reuse_items.append(
            f"用户名：{saved_user}（密码已保存，不回显；来源：{SOURCE_CONFIG}）"
        )
        return result
    if config.get("username") and not result.same_instance:
        raise ConfigError(
            f"已保存的账号属于另一个禅道地址（{config.get('baseUrl')}），"
            f"当前地址 {result.base_url} 需重新输入用户名和密码（换实例禁止复用账号密码）"
        )
    if saved_user and not config.get("password"):
        raise ConfigError(
            "已保存用户名但未保存密码（可能上次使用了 --no-remember）；请重新向用户索取用户名和密码"
        )
    if saved_user and config.get("password") and not saved_password:
        raise ConfigError(
            "已保存的密码无法解密（可能换了 Windows 用户/机器），请重新向用户索取用户名和密码"
        )
    raise ConfigError(
        "缺少禅道用户名和密码：请向用户索取后通过 --username 与 --password-stdin"
        "（或环境变量 ZENTAO_USERNAME/ZENTAO_PASSWORD）传入；不要把密码回显到日志"
    )


def _auto_detect_workspace(base: Path) -> Optional[Path]:
    if not base.is_dir():
        return None
    candidates = [
        child
        for child in sorted(base.iterdir())
        if child.is_dir() and (child / CONTEXT_FILENAME).is_file()
    ]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise ConfigError(
            f"当前目录 {base} 下存在多个任务工作区（{'、'.join(item.name for item in candidates)}），"
            "无法自动确定；请用 --task-id 或 --workspace 指定其中一个"
        )
    return None


def _task_id_from_workspace_dir(directory: Path) -> str:
    context_path = directory / CONTEXT_FILENAME
    if not context_path.is_file():
        return ""
    try:
        data = json.loads(context_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError):
        return ""
    if isinstance(data, dict):
        return _as_str(data.get("taskId"))
    return ""


def resolve_workspace(
    args: argparse.Namespace, config: dict
) -> Tuple[Path, str, str, List[str]]:
    """定位任务工作区，返回 (工作区目录, taskId, 来源, 复用项)。

    除 init 外的命令支持 --task-id 或 --workspace 二选一定位；缺省时：
      1) 复用用户配置中的 lastTaskId/lastWorkspace（需 --reuse 确认）；
      2) 尝试当前目录 .zentao-dev-task/ 下唯一的工作区；
      3) 都无法确定 → exit 2 提示先 init。
    """
    task_id = _as_str(getattr(args, "task_id", None))
    raw_workspace = _as_str(getattr(args, "workspace", None))
    reuse_items: List[str] = []
    config_task = _as_str(config.get("lastTaskId"))
    config_workspace = _as_str(config.get("lastWorkspace"))

    if raw_workspace:
        workspace_dir = Path(raw_workspace).expanduser()
        source = "参数 --workspace"
        if not task_id:
            detected = _task_id_from_workspace_dir(workspace_dir)
            if detected:
                task_id = detected
            elif config_task:
                task_id = config_task
                reuse_items.append(f"任务 id：{task_id}（来源：{SOURCE_CONFIG}）")
            else:
                raise ConfigError(
                    f"--workspace {workspace_dir} 下没有 context.json，无法确定任务 id；"
                    "请同时指定 --task-id，或先运行 init --task-id <id>"
                )
    elif task_id:
        workspace_dir = Path(DEFAULT_WORKSPACE_ROOT).expanduser() / task_id
        source = f"参数 --task-id（默认工作区 {workspace_dir}）"
    else:
        base = Path(DEFAULT_WORKSPACE_ROOT).expanduser()
        detected_dir = _auto_detect_workspace(base)
        if detected_dir is not None:
            workspace_dir = detected_dir
            task_id = _task_id_from_workspace_dir(detected_dir) or detected_dir.name
            source = f"当前目录唯一工作区（{workspace_dir}）"
        elif config_task and config_workspace:
            saved_dir = Path(config_workspace).expanduser()
            if not (saved_dir / CONTEXT_FILENAME).is_file():
                raise ConfigError(
                    f"上次保存的任务工作区已不存在（{saved_dir}）：请重新运行 "
                    "init --task-id <id> 接入任务，或显式指定 --task-id/--workspace"
                )
            task_id = config_task
            workspace_dir = saved_dir
            source = SOURCE_CONFIG
            reuse_items.append(f"任务 id：{task_id}（来源：{SOURCE_CONFIG}）")
            reuse_items.append(f"工作区：{workspace_dir}（来源：{SOURCE_CONFIG}）")
        else:
            raise ConfigError(
                "无法确定任务工作区：请先运行 init --task-id <id> 接入任务，"
                "或在命令中显式指定 --task-id / --workspace"
            )
    return workspace_dir, task_id, source, reuse_items


def apply_reuse_gate(args: argparse.Namespace, reuse_items: Sequence[str]) -> None:
    if not reuse_items:
        return
    if bool(getattr(args, "reuse", False)):
        return
    lines = [
        "检测到将复用上次保存的禅道配置（复用任一保存值前必须先取得用户确认）：",
        *[f"  - {item}" for item in reuse_items],
        "请先把以上内容（密码只说“已保存”，不要回显）复述给用户并取得明确同意，",
        "再加 --reuse 重试；或显式传参覆盖对应值。",
    ]
    raise ConfigError("\n".join(lines))


def remember_session(
    config_path: Path,
    config: dict,
    *,
    credentials: Credentials,
    task_id: str,
    workspace_root: Path,
    remember_password: bool,
) -> dict:
    updated = dict(config)
    updated["version"] = 1
    updated["baseUrl"] = credentials.base_url
    updated["username"] = credentials.username
    if remember_password:
        protected = protect_password(credentials.password)
        if protected is not None:
            updated["password"] = protected
    else:
        updated.pop("password", None)
    if task_id:
        updated["lastTaskId"] = task_id
    if str(workspace_root):
        updated["lastWorkspace"] = str(workspace_root)
    updated["updatedAt"] = now_str()
    save_user_config(config_path, updated)
    return updated


# ---------------------------------------------------------------------------
# 禅道 v2 API 客户端（统一 v2；v1 仅登录/用户列表兜底）
# ---------------------------------------------------------------------------


def _decode_body(raw: bytes) -> str:
    """解码响应体：优先 utf-8-sig（容忍 UTF-8 BOM），再 utf-8 / gb18030。"""
    if not raw:
        return ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _render_message(value: Any) -> str:
    """v2 错误 message 可能是 string/object/array，统一渲染成可读文本。"""
    if isinstance(value, dict):
        return "；".join(f"{key}: {_render_message(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return "；".join(_render_message(item) for item in value if _has_non_empty(item))
    return _excerpt(value, 300)


def _extract_message(payload: Any, *, fallback: str = "") -> str:
    if isinstance(payload, dict):
        for key in ("message", "reason", "error"):
            value = payload.get(key)
            if _has_non_empty(value):
                return _render_message(value)
    return _render_message(payload) or fallback


@dataclass
class LoginStatus:
    base_url: str = ""
    username: str = ""
    display_name: str = ""
    login_mode: str = ""  # v2 / v1-fallback
    user: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    capabilities: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict:
        """登录信息（不含 token）。"""
        return {
            "loginMode": self.login_mode,
            "capabilities": dict(self.capabilities),
            "warnings": list(self.warnings),
        }


V2_CAPABILITIES = {
    "read": True,
    "createTask": True,
    "start": True,
    "finish": True,
    "attachment": True,
    "comment": True,
    "usersFallback": False,
}


def _normalize_task(task: dict) -> dict:
    """v2 task.files 是以 file id 为 key 的 map，这里归一化为列表（含 id）。"""
    files = task.get("files")
    normalized: List[dict] = []
    if isinstance(files, dict):
        for key, value in files.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("id", key)
            else:
                item = {"id": key, "title": _as_str(value)}
            if isinstance(item.get("id"), str) and item["id"].isdigit():
                item["id"] = int(item["id"])
            normalized.append(item)
    elif isinstance(files, list):
        for item in files:
            normalized.append(dict(item) if isinstance(item, dict) else {"id": item})
    task["files"] = normalized
    if not isinstance(task.get("actions"), list):
        task["actions"] = []
    return task


class ZenTaoSession:
    """禅道 22.6 统一 v2 API 客户端（v1 仅登录/用户列表兜底）。"""

    def __init__(self, base_url: str, timeout: int = HTTP_TIMEOUT_DEFAULT) -> None:
        self.base_url = _normalize_url(base_url)
        self.api_v2 = self.base_url + "/api.php/v2"
        self.api_v1 = self.base_url + "/api.php/v1"
        self.timeout = int(timeout)
        self.token = ""
        self.login_status: Optional[LoginStatus] = None
        self.warnings: List[str] = []

    # -- 低层 ---------------------------------------------------------------
    def _open(self, request: urllib.request.Request) -> Tuple[int, Any, bytes]:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return (
                    int(getattr(response, "status", 200) or 200),
                    response.headers,
                    response.read(),
                )
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.headers, exc.read()
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise NetworkError(
                f"无法连接禅道 {self.base_url}：{reason}。请检查地址、网络连通性、代理或防火墙设置"
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise NetworkError(
                f"无法连接禅道 {self.base_url}：{exc}。请检查地址、网络连通性、代理或防火墙设置"
            ) from exc

    def _send(
        self,
        method: str,
        url: str,
        *,
        payload: Optional[dict] = None,
        raw_body: Optional[bytes] = None,
        content_type: str = "",
        allow_failed: bool = False,
    ) -> dict:
        headers = {
            "Accept": "application/json",
            "User-Agent": f"{SKILL_NAME}/{SCRIPT_VERSION}",
        }
        data: Optional[bytes] = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        elif raw_body is not None:
            data = raw_body
            if content_type:
                headers["Content-Type"] = content_type
        if self.token:
            headers["Token"] = self.token
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        status, _response_headers, raw = self._open(request)
        text = _decode_body(raw)
        detail = f"{method} {url}"
        if status in (401, 403):
            try:
                parsed_error = json.loads(text)
            except json.JSONDecodeError:
                parsed_error = {}
            raise ZenTaoError(
                f"禅道接口认证/权限失败（HTTP {status}）："
                f"{_extract_message(parsed_error) or _excerpt(text, 200)}。"
                "请确认账号密码、Token 有效期与项目/任务权限",
                detail=detail,
                status=status,
            )
        if status >= 400:
            try:
                parsed_error = json.loads(text)
            except json.JSONDecodeError:
                parsed_error = {}
            raise ZenTaoError(
                f"禅道接口请求失败（HTTP {status}）："
                f"{_extract_message(parsed_error) or _excerpt(text, 200)}",
                detail=detail,
                status=status,
            )
        if not text.strip():
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ZenTaoError(
                f"禅道返回的不是 JSON（HTTP {status}）：{_excerpt(text, 200)}",
                detail=detail,
                status=status,
            ) from exc
        if isinstance(parsed, dict):
            state = _as_str(parsed.get("status")).lower()
            if state in ("fail", "failed"):
                message = _extract_message(parsed) or "status=fail"
                if allow_failed:
                    return parsed
                raise ZenTaoError(
                    f"禅道 v2 接口失败（{method} {url}）：{message}", detail=detail
                )
            return parsed
        if isinstance(parsed, list):
            return {"data": parsed}
        raise ZenTaoError(
            f"禅道返回结构异常（HTTP {status}）：{_excerpt(text, 200)}",
            detail=detail,
            status=status,
        )

    def _v2(self, method: str, path: str, payload: Optional[dict] = None, **kwargs: Any) -> dict:
        return self._send(method, self.api_v2 + path, payload=payload, **kwargs)

    def _v1(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        return self._send(method, self.api_v1 + path, payload=payload)

    # -- 登录 ---------------------------------------------------------------
    def _v1_login(self, account: str, password: str) -> str:
        data = self._v1("POST", "/tokens", {"account": account, "password": password})
        token = _as_str(data.get("token"))
        if not token:
            raise ZenTaoError(
                "v1 登录成功但未返回 token：" + (_extract_message(data) or "未知原因")
            )
        return token

    def login(self, account: str, password: str) -> LoginStatus:
        """v2 登录优先；仅 v2 login 返回 HTTP 404/不可用时兜底 v1 /tokens；失败不重试。"""
        status = LoginStatus(
            base_url=self.base_url,
            username=account,
            capabilities=dict(V2_CAPABILITIES),
        )
        try:
            data = self._v2(
                "POST",
                "/users/login",
                {"account": account, "password": password},
                allow_failed=True,
            )
            state = _as_str(data.get("status")).lower()
            if state in ("failed", "fail"):
                reason = _extract_message(data) or f"status={state}"
                raise ZenTaoError(f"禅道 v2 登录失败：{reason}")
        except ZenTaoError as exc:
            if exc.status != 404:
                raise
            status.warnings.append(
                "v2 登录接口不可用（HTTP 404），已回退 v1 /tokens（老版本兼容）；"
                "其余接口仍按 v2 调用"
            )
            status.login_mode = "v1-fallback"
            token = self._v1_login(account, password)
            status.display_name = account
        else:
            token = _as_str(data.get("token"))
            if not token:
                raise ZenTaoError(
                    "v2 登录成功但未返回 token：" + (_extract_message(data) or "未知原因")
                )
            status.login_mode = "v2"
            user = data.get("user") if isinstance(data.get("user"), dict) else {}
            status.user = user
            status.display_name = _as_str(user.get("realname") or user.get("account")) or account
        self.token = token
        self.login_status = status
        status.capabilities["usersFallback"] = status.login_mode == "v1-fallback"
        self.warnings = list(status.warnings)
        return status

    # -- 用户（v2 优先，v1 兜底；两者都失败降级告警不阻塞） ------------------
    def list_users(self) -> List[dict]:
        users: List[dict] = []
        seen = set()
        try:
            page_id = 1
            while page_id <= 50:
                data = self._v2(
                    "GET", f"/users?recPerPage={USERS_PAGE_SIZE}&pageID={page_id}"
                )
                batch = data.get("users")
                if batch is None and isinstance(data.get("data"), list):
                    batch = data.get("data")
                if not isinstance(batch, list):
                    break
                new_items = []
                for item in batch:
                    if not isinstance(item, dict):
                        continue
                    account = _as_str(item.get("account"))
                    if account in seen:
                        continue
                    seen.add(account)
                    new_items.append(item)
                users.extend(new_items)
                total = data.get("total")
                if not batch or not new_items:
                    break
                if isinstance(total, int) and len(users) >= total:
                    break
                page_id += 1
            return users
        except ZenTaoError as exc:
            self.warnings.append(
                f"v2 用户列表不可用（{exc.message}），尝试 v1 /users 兜底"
            )
        users = []
        seen = set()
        try:
            page = 1
            while page <= 50:
                data = self._v1("GET", f"/users?limit={USERS_PAGE_SIZE}&page={page}")
                batch = data.get("users")
                if not isinstance(batch, list):
                    break
                new_items = []
                for item in batch:
                    if not isinstance(item, dict):
                        continue
                    account = _as_str(item.get("account"))
                    if account in seen:
                        continue
                    seen.add(account)
                    new_items.append(item)
                users.extend(new_items)
                total = data.get("total")
                if not batch or not new_items:
                    break
                if isinstance(total, int) and len(users) >= total:
                    break
                page += 1
            if users:
                warnings = "已回退 v1 /users 获取指派账号列表（v2 /users 无权限/不可用）"
                if warnings not in self.warnings:
                    self.warnings.append(warnings)
            return users
        except ZenTaoError as exc:
            message = (
                f"v2/v1 用户列表均不可用（{exc.message}），跳过指派账号校验"
                "（请让用户确认指派账号在禅道中存在）"
            )
            if message not in self.warnings:
                self.warnings.append(message)
            return []

    def lookup_display_name(self, account: str) -> str:
        for user in self.list_users():
            if _as_str(user.get("account")).lower() == account.lower():
                return _as_str(user.get("realname")) or account
        return ""

    # -- 读接口（v2） -------------------------------------------------------
    def get_task(self, task_id: int) -> dict:
        data = self._v2(
            "GET", f"/tasks/{int(task_id)}", allow_failed=True
        )
        state = _as_str(data.get("status")).lower()
        if state == "fail":
            message = _extract_message(data) or "status=fail"
            code = _opt_int(data.get("code"), "code") or 0
            if code == 404 or "404" in message.lower():
                raise ZenTaoError(
                    f"禅道任务 id={task_id} 不存在或不可见：{message}", status=404
                )
            raise ZenTaoError(f"读取禅道任务失败：{message}")
        task = data.get("task")
        if not isinstance(task, dict):
            raise ZenTaoError("任务详情响应缺少 task 对象")
        task = _normalize_task(task)
        actions = data.get("actions")
        if isinstance(actions, list):
            task["actions"] = actions
        return task

    def get_story(self, story_id: int) -> Optional[dict]:
        try:
            data = self._v2(
                "GET", f"/stories/{int(story_id)}", allow_failed=True
            )
        except ZenTaoError as exc:
            self.warnings.append(
                f"读取需求 {story_id} 失败（{exc.message}），按“无需求创建人”处理"
            )
            return None
        if _as_str(data.get("status")).lower() == "fail":
            self.warnings.append(
                f"读取需求 {story_id} 失败（{_extract_message(data, fallback='status=fail')}），"
                "按“无需求创建人”处理"
            )
            return None
        story = data.get("story")
        if not isinstance(story, dict) or story.get("id") in (None, ""):
            return None
        if str(story.get("deleted") or "0") == "1":
            self.warnings.append(f"需求 {story_id} 已删除，不使用其创建人")
            return None
        return story

    # -- 写接口（v2） -------------------------------------------------------
    def create_task(self, payload: dict) -> dict:
        data = self._v2("POST", "/tasks", payload)
        task_id = _opt_int(data.get("id") or data.get("taskID"), "前置任务 id")
        if task_id is None:
            raise ZenTaoError(
                "创建前置任务成功但未返回 id："
                + json.dumps(data, ensure_ascii=False)[:300]
            )
        return {"id": task_id, "raw": data}

    def start_task(self, task_id: int, payload: dict) -> dict:
        return self._v2("PUT", f"/tasks/{int(task_id)}/start", payload)

    def finish_task(self, task_id: int, payload: dict) -> dict:
        return self._v2("PUT", f"/tasks/{int(task_id)}/finish", payload)

    def upload_task_file(self, task_id: int, file_path: Path) -> dict:
        filename = file_path.name
        boundary = "----zentao-dev-task-flow-" + os.urandom(8).hex()
        file_bytes = file_path.read_bytes()
        parts: List[bytes] = []
        for field, value in (("objectType", "task"), ("objectID", str(int(task_id)))):
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{field}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
            )
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
            + file_bytes
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)
        data = self._send(
            "POST",
            self.api_v2 + "/files",
            raw_body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        file_id = _opt_int(data.get("id"), "附件 id")
        if file_id is None:
            raise ZenTaoError(
                "附件上传成功但未返回 id："
                + json.dumps(data, ensure_ascii=False)[:300]
            )
        return {"id": file_id, "url": _as_str(data.get("url")), "raw": data}

    def comment_task(self, task_id: int, html: str) -> dict:
        data = self._v2(
            "POST",
            f"/actions/comment?objectType=task&objectID={int(task_id)}",
            {"comment": html},
        )
        action_id = _opt_int(data.get("data"), "备注 action id")
        return {"id": action_id, "raw": data}


# ---------------------------------------------------------------------------
# 工作区与 context.json（原子写、不含密码/token）
# ---------------------------------------------------------------------------

CONTEXT_FILENAME = "context.json"
WORKSPACE_SUBDIRS = ("documents", "attachments")
FORBIDDEN_ZIP_NAMES = {"context.json", "zentao.json", "credentials.json", ".skillenv"}


class TaskWorkspace:
    def __init__(self, directory: Path, task_id: str = "") -> None:
        self.dir = Path(directory)
        self.task_id = str(task_id)
        self.context_path = self.dir / CONTEXT_FILENAME

    # -- 路径 ---------------------------------------------------------------
    def exists(self) -> bool:
        return self.context_path.is_file()

    def ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        for name in WORKSPACE_SUBDIRS:
            (self.dir / name).mkdir(parents=True, exist_ok=True)

    def path(self, *parts: str) -> Path:
        return self.dir.joinpath(*parts)

    def rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.dir.resolve()).as_posix()
        except (ValueError, OSError):
            return path.as_posix()

    # -- context ------------------------------------------------------------
    def load(self) -> dict:
        if not self.context_path.is_file():
            raise GateError(
                f"任务工作区不存在或缺少 context.json：{self.context_path}；"
                "请先运行 init --task-id <id> 接入任务"
            )
        try:
            data = json.loads(self.context_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GateError(f"context.json 无法解析：{self.context_path}（{exc}）") from exc
        if not isinstance(data, dict):
            raise GateError(f"context.json 顶层必须是对象：{self.context_path}")
        if _as_str(data.get("taskId")) != self.task_id:
            raise GateError(
                f"context.json 的 taskId（{data.get('taskId')}）与当前任务 {self.task_id} 不一致"
            )
        # 兼容旧版工作区：infeasible 不再是独立状态，结论字段已能表达
        if _as_str(data.get("status")) == "infeasible":
            data["status"] = "feasibility_done"
        return data

    def save(self, context: dict) -> None:
        self.ensure()
        payload = json.dumps(context, ensure_ascii=False, indent=2) + "\n"
        temp = self.context_path.with_suffix(".json.tmp")
        temp.write_text(payload, encoding="utf-8")
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, self.context_path)


def _context_status(context: dict) -> str:
    return _as_str(context.get("status"))


def _require_status(context: dict, expected: Sequence[str], command: str) -> str:
    current = _context_status(context)
    if current not in expected:
        expected_labels = "、".join(f"{name}（{STATE_LABELS.get(name, name)}）" for name in expected)
        raise GateError(
            f"{command} 要求当前状态为 {expected_labels}，实际是 {current or '未知'}"
            f"（{STATE_LABELS.get(current, current)}）；不允许跳阶段"
        )
    return current


def _set_status(context: dict, status: str, timestamp_key: str = "") -> None:
    context["status"] = status
    timestamps = context.setdefault("timestamps", {})
    if timestamp_key:
        timestamps[timestamp_key] = now_str()
    timestamps["updatedAt"] = now_str()


def _append_warning(context: dict, message: str) -> None:
    warnings = context.setdefault("warnings", [])
    if message not in warnings:
        warnings.append(message)


def _append_degraded(context: dict, message: str) -> None:
    degraded = context.setdefault("delivery", {}).setdefault("degraded", [])
    if message not in degraded:
        degraded.append(message)


def default_context(task_id: str) -> dict:
    return {
        "version": 1,
        "taskId": str(task_id),
        "taskSnapshot": {},
        "storySnapshot": {},
        "creator": {
            "requirementCreator": "",
            "taskCreator": "",
            "resolvedAssignee": "",
            "source": "",
        },
        "status": "initialized",
        "timestamps": {},
        "prepare": {},
        "prerequisites": [],
        "feasibility": {},
        "plan": {},
        "acceptance": {},
        "delivery": {},
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# 只读 Git 查询
# ---------------------------------------------------------------------------


def _run_git(args: Sequence[str], cwd: Path, *, timeout: int = 120) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise GateError("未找到 git 命令：本脚本需要 Git 做只读仓库查询，请先安装 Git") from exc
    except subprocess.TimeoutExpired as exc:
        raise GateError(f"git 命令超时：git {' '.join(args)}") from exc


def _git_stdout(args: Sequence[str], cwd: Path, default: str = "") -> str:
    proc = _run_git(args, cwd)
    if proc.returncode != 0:
        return default
    return proc.stdout.strip()


def resolve_git_repo(path: Path) -> Path:
    """校验目录是 Git 仓库并返回仓库根；失败抛 GateError。"""
    if not path.exists():
        raise GateError(f"仓库路径不存在：{path}")
    if not path.is_dir():
        raise GateError(f"仓库路径不是目录：{path}")
    proc = _run_git(["rev-parse", "--show-toplevel"], path)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise GateError(f"路径不是 Git 仓库（git rev-parse --show-toplevel 失败）：{path}")
    try:
        return Path(proc.stdout.strip()).resolve()
    except OSError as exc:  # pragma: no cover
        raise GateError(f"无法解析 Git 仓库根：{proc.stdout.strip()}") from exc


def collect_git_facts(repo_path: Path, *, remote_name: str = "origin") -> Dict[str, Any]:
    """只读收集 remote/branch/HEAD/clean/diffstat/log（失败项记录为错误文本）。"""
    repo_root = resolve_git_repo(repo_path)
    facts: Dict[str, Any] = {
        "repoPath": str(repo_root),
        "repoUrl": "",
        "branch": "",
        "head": "",
        "clean": False,
        "dirtyFiles": [],
        "status": "",
        "diffstat": "",
        "log": "",
    }
    remote_proc = _run_git(["remote", "get-url", remote_name], repo_root)
    if remote_proc.returncode == 0:
        facts["repoUrl"] = remote_proc.stdout.strip()
    branch_proc = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    if branch_proc.returncode == 0:
        facts["branch"] = branch_proc.stdout.strip()
    head_proc = _run_git(["rev-parse", "HEAD"], repo_root)
    if head_proc.returncode == 0:
        facts["head"] = head_proc.stdout.strip()
    status_proc = _run_git(["status", "--porcelain"], repo_root)
    if status_proc.returncode == 0:
        lines = [line for line in status_proc.stdout.splitlines() if line.strip()]
        facts["dirtyFiles"] = lines
        facts["clean"] = not lines
    facts["status"] = "\n".join(facts["dirtyFiles"]) if facts["dirtyFiles"] else "clean"
    diff_proc = _run_git(["diff", "--stat"], repo_root)
    if diff_proc.returncode == 0:
        facts["diffstat"] = diff_proc.stdout.strip()
    log_proc = _run_git(["log", "-5", "--oneline", "--decorate"], repo_root)
    if log_proc.returncode == 0:
        facts["log"] = log_proc.stdout.strip()
    return facts


def git_facts_text(repo_path: Path) -> str:
    lines = ["# Git 事实（只读查询，由 zentao-dev-task-flow 生成）", f"生成时间：{now_str()}"]
    try:
        facts = collect_git_facts(repo_path)
    except GateError as exc:
        lines.append(f"Git 查询失败：{exc.message}")
        return "\n".join(lines) + "\n"
    lines.extend(
        [
            f"仓库：{facts['repoPath']}",
            f"远端：{facts['repoUrl'] or '（无 remote）'}",
            f"分支：{facts['branch'] or '（未知）'}",
            f"HEAD：{facts['head'] or '（未知）'}",
            f"工作树：{'clean' if facts['clean'] else 'dirty'}",
            "",
            "## git status --porcelain",
            facts["status"] or "clean",
            "",
            "## git diff --stat",
            facts["diffstat"] or "（无差异）",
            "",
            "## git log -5 --oneline",
            facts["log"] or "（无提交）",
        ]
    )
    return "\n".join(lines) + "\n"


def detect_toolchain() -> List[dict]:
    results: List[dict] = []
    for name, command in TOOLCHAIN_COMMANDS:
        item: Dict[str, Any] = {"tool": name, "command": " ".join(command), "available": False, "version": ""}
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            text = (proc.stdout or "") + "\n" + (proc.stderr or "")
            version_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
            item["available"] = proc.returncode == 0 or bool(version_line)
            item["version"] = _excerpt(version_line, 120)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            item["error"] = _excerpt(exc, 120)
        results.append(item)
    return results


def list_files_recursive(directory: Path) -> List[Path]:
    if not directory.is_dir():
        return []
    result: List[Path] = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and not path.is_symlink():
            result.append(path)
    return result


# ---------------------------------------------------------------------------
# 收尾 zip 构建（manifest.json + 总结/计划/验收/文档/git-facts）
# ---------------------------------------------------------------------------


def _safe_arcname(name: str) -> str:
    cleaned = str(name).replace("\\", "/").lstrip("/")
    cleaned = re.sub(r"^[A-Za-z]:/", "", cleaned)
    parts = [part for part in cleaned.split("/") if part not in ("", ".", "..")]
    return "/".join(parts)


def collect_delivery_entries(
    workspace: TaskWorkspace,
    *,
    doc_dirs: Sequence[Path],
    extra_files: Sequence[Path],
) -> Tuple[List[Tuple[str, Path]], List[str]]:
    """收集待打包文件，返回 [(arcname, 源文件)] 与 warnings。"""
    warnings: List[str] = []
    entries: List[Tuple[str, Path]] = []
    used = set()

    def add(arcname: str, source: Path) -> None:
        safe = _safe_arcname(arcname)
        if not safe or safe in used:
            return
        if Path(safe).name in FORBIDDEN_ZIP_NAMES:
            warnings.append(f"已排除敏感/状态文件：{safe}")
            return
        used.add(safe)
        entries.append((safe, source))

    for filename in ("summary.md", "feasibility.md", "dev-plan.md", "acceptance.md"):
        path = workspace.path(filename)
        if path.is_file():
            add(filename, path)
        elif filename == "summary.md":
            continue
    default_docs = workspace.path("documents")
    doc_roots: List[Path] = []
    for directory in [default_docs, *doc_dirs]:
        if directory and directory.is_dir() and directory not in doc_roots:
            doc_roots.append(directory)
    for directory in doc_roots:
        for path in list_files_recursive(directory):
            if path == workspace.context_path or path.name in FORBIDDEN_ZIP_NAMES:
                continue
            try:
                rel = path.resolve().relative_to(directory.resolve()).as_posix()
            except (ValueError, OSError):
                rel = path.name
            add(f"documents/{rel}", path)
    for extra in extra_files:
        if not extra.is_file():
            warnings.append(f"--extra-file 不存在，已跳过：{extra}")
            continue
        add(f"extra/{extra.name}", extra)
    return entries, warnings


def build_delivery_zip(
    *,
    workspace: TaskWorkspace,
    context: dict,
    repo_path: Path,
    doc_dirs: Sequence[Path],
    extra_files: Sequence[Path],
    zip_name: str,
) -> Dict[str, Any]:
    """构建收尾 zip；返回 {zipFile,zipName,zipSize,zipSha256,entries,manifest}。"""
    entries, warnings = collect_delivery_entries(
        workspace, doc_dirs=doc_dirs, extra_files=extra_files
    )
    file_records: List[dict] = []
    payloads: List[Tuple[str, bytes]] = []
    for arcname, source in entries:
        try:
            data = source.read_bytes()
        except OSError as exc:
            warnings.append(f"读取失败已跳过：{source}（{exc}）")
            continue
        payloads.append((arcname, data))
        file_records.append(
            {
                "name": arcname,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    git_text = git_facts_text(repo_path)
    payloads.append(("git-facts.txt", git_text.encode("utf-8")))
    file_records.append(
        {
            "name": "git-facts.txt",
            "size": len(git_text.encode("utf-8")),
            "sha256": hashlib.sha256(git_text.encode("utf-8")).hexdigest(),
        }
    )
    task_snapshot = context.get("taskSnapshot") or {}
    story_snapshot = context.get("storySnapshot") or {}
    prepare = context.get("prepare") or {}
    manifest = {
        "taskId": context.get("taskId"),
        "taskName": _as_str(task_snapshot.get("name")),
        "taskStatus": _as_str(task_snapshot.get("status")),
        "story": {
            "id": story_snapshot.get("id"),
            "title": _as_str(story_snapshot.get("title")),
        },
        "repo": {
            "path": prepare.get("repoPath") or str(repo_path),
            "url": prepare.get("repoUrl") or "",
            "branch": prepare.get("branch") or "",
            "head": prepare.get("baseCommit") or "",
        },
        "generatedAt": now_str(),
        "account": _as_str((context.get("creator") or {}).get("resolvedAssignee")),
        "files": file_records,
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    attachments_dir = workspace.path("attachments")
    attachments_dir.mkdir(parents=True, exist_ok=True)
    zip_path = attachments_dir / zip_name
    temp_zip = zip_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", manifest_bytes)
        for arcname, data in payloads:
            archive.writestr(arcname, data)
    os.replace(temp_zip, zip_path)
    zip_sha = sha256_file(zip_path)
    return {
        "zipFile": str(zip_path),
        "zipName": zip_name,
        "zipSize": zip_path.stat().st_size,
        "zipSha256": zip_sha,
        "entries": [name for name, _ in payloads],
        "manifest": manifest,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 命令公共上下文：配置 / 工作区 / 凭据 / 登录
# ---------------------------------------------------------------------------


@dataclass
class CommandContext:
    args: argparse.Namespace
    config_path: Path
    config: dict
    skillenv: dict
    workspace_root: Path
    task_id: str
    workspace: TaskWorkspace
    workspace_source: str
    credentials: Optional[Credentials] = None
    session: Optional[ZenTaoSession] = None
    login: Optional[LoginStatus] = None

    @property
    def workspace_dir(self) -> Path:
        return self.workspace.dir

    def save_config(self) -> None:
        try:
            save_user_config(self.config_path, self.config)
        except OSError as exc:  # pragma: no cover - 磁盘异常
            warn(f"用户配置保存失败（不影响本次操作）：{exc}")


def prepare_command_context(
    args: argparse.Namespace, *, need_login: bool, require_workspace: bool = True
) -> CommandContext:
    config_path = (
        Path(args.config).expanduser() if getattr(args, "config", None) else default_config_path()
    )
    config = load_user_config(config_path)
    workspace_hint = Path(
        _as_str(getattr(args, "workspace", None)) or DEFAULT_WORKSPACE_ROOT
    )
    skillenv_path = (
        Path(args.skillenv).expanduser()
        if getattr(args, "skillenv", None)
        else discover_skillenv(workspace_hint)
    )
    skillenv = load_skillenv(skillenv_path)
    workspace_dir, task_id, workspace_source, reuse_items = resolve_workspace(args, config)
    workspace = TaskWorkspace(workspace_dir, task_id)
    credentials: Optional[Credentials] = None
    if need_login:
        credentials = resolve_credentials(args, config, skillenv)
        reuse_items = [*reuse_items, *credentials.reuse_items]
    apply_reuse_gate(args, reuse_items)
    if require_workspace and not workspace.exists():
        raise GateError(
            f"任务工作区不存在或缺少 context.json：{workspace.context_path}；"
            f"请先运行 init --task-id {task_id} 接入任务"
        )
    if skillenv_path is not None and skillenv:
        bullet(f".skillenv：{skillenv_path}")
    bullet(f"用户配置文件：{config_path}")
    bullet(f"工作区：{workspace.dir}（来源：{workspace_source}）")
    return CommandContext(
        args=args,
        config_path=config_path,
        config=config,
        skillenv=skillenv,
        workspace_root=workspace.dir,
        task_id=task_id,
        workspace=workspace,
        workspace_source=workspace_source,
        credentials=credentials,
    )


def connect(ctx: CommandContext, *, remember: bool = True) -> LoginStatus:
    """登录禅道（统一 v2 API；仅 v2 login 404 时回退 v1 /tokens），报告能力与告警。"""
    credentials = ctx.credentials
    if credentials is None:
        raise ConfigError("内部错误：当前命令未解析凭据")
    timeout = int(getattr(ctx.args, "timeout", None) or HTTP_TIMEOUT_DEFAULT)
    bullet(f"禅道地址：{credentials.base_url}（来源：{credentials.base_url_source}）")
    bullet(f"账号：{credentials.username}（来源：{credentials.credential_source}）")
    session = ZenTaoSession(credentials.base_url, timeout=timeout)
    status = session.login(credentials.username, credentials.password)
    ctx.session = session
    ctx.login = status
    mode_label = "v2 API" if status.login_mode == "v2" else "v1 /tokens 兜底（v2 登录不可用）"
    bullet(f"登录成功：{status.display_name}（模式：{mode_label}）")
    for item in status.warnings:
        warn(item)
    bullet(
        "能力（统一 v2 API）：读任务/需求=可用；创建前置任务/开始/完成=可用；"
        "附件上传=可用（失败严格停止）；独立备注=可用（失败降级并入 finish comment）"
    )
    if remember:
        try:
            ctx.config = remember_session(
                ctx.config_path,
                ctx.config,
                credentials=credentials,
                task_id=ctx.task_id,
                workspace_root=ctx.workspace_root,
                remember_password=not bool(getattr(ctx.args, "no_remember", False)),
            )
        except OSError as exc:  # pragma: no cover
            warn(f"用户配置保存失败（不影响本次操作）：{exc}")
    return status


def _login_payload(status: LoginStatus) -> dict:
    return status.to_json()


# ---------------------------------------------------------------------------
# init：接入禅道任务
# ---------------------------------------------------------------------------


def _story_id_of(task: dict) -> Optional[int]:
    story = task.get("story")
    if isinstance(story, dict):
        story = story.get("id")
    return _opt_int(story, "需求 id")


def _execution_id_of(task: dict) -> Optional[int]:
    execution = task.get("execution")
    if isinstance(execution, dict):
        execution = execution.get("id")
    return _opt_int(execution, "执行 id")


def _project_id_of(task: dict) -> Optional[int]:
    project = task.get("project")
    if isinstance(project, dict):
        project = project.get("id")
    return _opt_int(project, "项目 id")


def _task_snapshot(task: dict) -> dict:
    return {
        "id": task.get("id"),
        "name": _as_str(task.get("name")),
        "status": _as_str(task.get("status")),
        "story": _story_id_of(task),
        "execution": _execution_id_of(task),
        "project": _project_id_of(task),
        "estimate": task.get("estimate"),
        "left": task.get("left"),
        "consumed": task.get("consumed"),
        "assignedTo": account_of(task.get("assignedTo")),
        "openedBy": account_of(task.get("openedBy")),
        "estStarted": _date_or_none(task.get("estStarted")),
        "deadline": _date_or_none(task.get("deadline")),
        "module": task.get("module"),
        "pri": task.get("pri"),
    }


def _story_snapshot(story: Optional[dict]) -> dict:
    if not story:
        return {}
    return {
        "id": story.get("id"),
        "title": _as_str(story.get("title") or story.get("name")),
        "status": _as_str(story.get("status")),
        "openedBy": account_of(story.get("openedBy")),
    }


def _resolve_creator(task: dict, story: Optional[dict]) -> Tuple[dict, List[str]]:
    warnings: List[str] = []
    requirement_creator = account_of((story or {}).get("openedBy"))
    task_creator = account_of(task.get("openedBy"))
    if requirement_creator:
        resolved = requirement_creator
        source = "requirements_creator"
    elif task_creator:
        resolved = task_creator
        source = "task_creator"
        warnings.append("未取得需求创建人，收尾将指派回任务创建人（兜底规则）")
    else:
        resolved = ""
        source = ""
        warnings.append("任务的 openedBy 与需求创建人均为空：收尾时需用 --assign-back 显式指定指派账号")
    if story is None and _story_id_of(task):
        warnings.append("任务关联了需求但读取失败（或无权限），按“无需求创建人”处理")
    return (
        {
            "requirementCreator": requirement_creator,
            "taskCreator": task_creator,
            "resolvedAssignee": resolved,
            "source": source,
        },
        warnings,
    )


def cmd_init(args: argparse.Namespace) -> int:
    ctx = prepare_command_context(args, need_login=True, require_workspace=False)
    status = connect(ctx, remember=True)
    assert ctx.session is not None
    task_id = _as_int(ctx.task_id, "--task-id")
    try:
        task = ctx.session.get_task(task_id)
    except ZenTaoError as exc:
        if exc.status in (403, 404):
            raise ZenTaoError(
                f"禅道任务 id={task_id} 不存在或当前账号不可见（HTTP {exc.status}）："
                "请核对任务 id 与账号权限",
                status=exc.status,
            ) from exc
        raise
    task_status = _as_str(task.get("status"))
    if task_status not in INIT_ALLOWED_TASK_STATUS:
        raise StateConflictError(
            f"禅道任务 {task_id} 当前状态为 {task_status or '未知'}，不允许接入"
            f"（允许：{'、'.join(sorted(INIT_ALLOWED_TASK_STATUS))}）。"
            "已完成/已关闭/已取消的任务无需再次接入；如确需重新接入请先与用户确认"
        )
    story_id = _story_id_of(task)
    story = ctx.session.get_story(story_id) if story_id else None
    creator, creator_warnings = _resolve_creator(task, story)

    ctx.workspace.ensure()
    existing: Optional[dict] = None
    if ctx.workspace.exists():
        existing = ctx.workspace.load()
    if existing is not None and bool(getattr(args, "force", False)):
        if not bool(getattr(args, "confirm", False)):
            raise ConfigError("--force 会重置本地状态机，必须先向用户确认后加 --confirm")
        context = default_context(ctx.task_id)
        context["prepare"] = existing.get("prepare") or {}
        context["prerequisites"] = existing.get("prerequisites") or []
        context["warnings"] = [
            *(existing.get("warnings") or []),
            "已按 --force --confirm 重置本地状态机（历史 prepare/前置任务记录保留）",
        ]
    elif existing is not None:
        context = existing
        _append_warning(context, "工作区已存在：本次已刷新任务/需求快照，本地状态机保持不变")
    else:
        context = default_context(ctx.task_id)
        context["timestamps"] = {"initializedAt": now_str()}
    context["taskId"] = ctx.task_id
    context["taskSnapshot"] = _task_snapshot(task)
    context["taskUrl"] = task_link(ctx.credentials.base_url if ctx.credentials else "", task_id)
    context["storySnapshot"] = _story_snapshot(story)
    context["creator"] = creator
    for message in creator_warnings:
        _append_warning(context, message)
    context.setdefault("timestamps", {})["initializedAt"] = context["timestamps"].get(
        "initializedAt"
    ) or now_str()
    context["timestamps"]["updatedAt"] = now_str()
    if _context_status(context) not in STATE_LABELS:
        context["status"] = "initialized"
    ctx.workspace.save(context)

    warnings = list(context.get("warnings") or [])
    if args.json:
        emit_json(
            {
                "ok": True,
                "taskId": task_id,
                "task": context["taskSnapshot"],
                "taskUrl": context["taskUrl"],
                "story": context["storySnapshot"],
                "creator": creator,
                "status": context["status"],
                "workspace": str(ctx.workspace.dir),
                "contextPath": str(ctx.workspace.context_path),
                "login": _login_payload(status),
                "warnings": warnings,
            }
        )
        return 0
    step(1, 3, "任务与需求")
    bullet(f"任务：#{task_id} {context['taskSnapshot'].get('name')}（状态：{task_status}）")
    bullet(f"任务链接：{context['taskUrl']}")
    if story:
        bullet(
            f"需求：#{story.get('id')} {_as_str(story.get('title'))}"
            f"（创建人：{creator['requirementCreator'] or '未知'}）"
        )
    else:
        bullet("需求：无（任务未关联需求或需求不可见）")
    bullet(f"任务创建人：{creator['taskCreator'] or '未知'}")
    bullet(f"建议收尾指派：{creator['resolvedAssignee'] or '未确定（收尾时需 --assign-back）'}"
           f"（来源：{creator['source'] or '无'}）")
    step(2, 3, "本地工作区")
    bullet(f"工作区：{ctx.workspace.dir}")
    bullet(f"context：{ctx.workspace.context_path}（状态：{context['status']}）")
    step(3, 3, "登录与能力")
    bullet(f"登录模式：{'v2 API' if status.login_mode == 'v2' else 'v1 /tokens 兜底（v2 登录不可用）'}")
    for item in status.warnings:
        warn(item)
    for item in warnings:
        warn(item)
    say("")
    say(f"下一步：为任务补充研发环境与 Git 仓库信息 → prepare --task-id {task_id} --repo-path <仓库路径> ...")
    return 0


# ---------------------------------------------------------------------------
# prepare：环境与仓库信息
# ---------------------------------------------------------------------------


def cmd_prepare(args: argparse.Namespace) -> int:
    ctx = prepare_command_context(args, need_login=False)
    context = ctx.workspace.load()
    current = _context_status(context)
    if current not in ("initialized", "prepared"):
        raise GateError(
            f"prepare 要求当前状态为 initialized（或 prepared 允许重跑），实际是 {current}"
            f"（{STATE_LABELS.get(current, current)}）；请勿跳阶段"
        )
    if current == "prepared":
        warn("检测到 prepare 重跑：将覆盖本地的环境/仓库事实（不影响禅道）")

    repo_path = Path(_as_str(args.repo_path)).expanduser()
    repo_root = resolve_git_repo(repo_path)
    facts = collect_git_facts(repo_root)
    warnings: List[str] = []
    if not facts["clean"]:
        if bool(getattr(args, "allow_dirty", False)):
            warnings.append("Git 工作树存在未提交修改（已按 --allow-dirty 记录用户确认）")
        else:
            warnings.append(
                "Git 工作树存在未提交修改（仅告警；如确认可接受请加 --allow-dirty 并在报告中说明）"
            )
    repo_url = _as_str(getattr(args, "repo_url", None)) or facts["repoUrl"]
    if not repo_url:
        warnings.append("Git 仓库没有 origin remote，且未提供 --repo-url：附件/报告中仓库地址为空")
    branch = _as_str(getattr(args, "branch", None)) or facts["branch"]
    build_cmd = _as_str(getattr(args, "build_cmd", None))
    test_cmd = _as_str(getattr(args, "test_cmd", None))
    runtime = _as_str(getattr(args, "runtime", None))
    env_notes = [item for item in (args.env_note or []) if _as_str(item)]
    docs_dirs: List[str] = []
    raw_docs_dirs = list(args.docs_dir or [])
    if not raw_docs_dirs:
        raw_docs_dirs = [str(ctx.workspace.path("documents"))]
    for item in raw_docs_dirs:
        directory = Path(_as_str(item)).expanduser()
        if not directory.is_dir():
            raise GateError(f"--docs-dir 指向的目录不存在：{directory}")
        docs_dirs.append(str(directory))
    tools = detect_toolchain() if bool(getattr(args, "detect_tools", True)) else []
    if tools:
        missing = [item["tool"] for item in tools if not item["available"]]
        if missing:
            warnings.append(f"本机未检测到以下工具（仅记录，不影响本地阶段）：{'、'.join(missing)}")

    prepare = {
        "repoPath": str(repo_root),
        "repoUrl": repo_url,
        "branch": branch,
        "baseCommit": facts["head"],
        "clean": facts["clean"],
        "dirtyFiles": facts["dirtyFiles"],
        "allowDirty": bool(getattr(args, "allow_dirty", False)),
        "buildCmd": build_cmd,
        "testCmd": test_cmd,
        "runtime": runtime,
        "envNotes": env_notes,
        "docsDirs": docs_dirs,
        "toolchain": tools,
        "detectedAt": now_str(),
    }
    context["prepare"] = prepare
    for message in warnings:
        _append_warning(context, message)
    _set_status(context, "prepared", "preparedAt")
    ctx.workspace.save(context)

    facts_payload = {
        "taskId": ctx.task_id,
        "task": context.get("taskSnapshot") or {},
        "story": context.get("storySnapshot") or {},
        "repo": {
            "path": prepare["repoPath"],
            "url": prepare["repoUrl"],
            "branch": prepare["branch"],
            "head": prepare["baseCommit"],
            "clean": prepare["clean"],
            "dirtyFiles": prepare["dirtyFiles"],
        },
        "commands": {"build": build_cmd, "test": test_cmd, "runtime": runtime},
        "envNotes": env_notes,
        "docs": {"dirs": docs_dirs},
        "toolchain": tools,
        "status": context["status"],
        "contextPath": str(ctx.workspace.context_path),
        "warnings": warnings,
    }
    if args.json:
        emit_json({"ok": True, **facts_payload})
        return 0
    step(1, 3, "仓库事实（只读 Git）")
    bullet(f"仓库：{prepare['repoPath']}")
    bullet(f"远端：{prepare['repoUrl'] or '（缺失）'}")
    bullet(f"分支/HEAD：{prepare['branch'] or '未知'} / {(prepare['baseCommit'] or '未知')[:12]}")
    dirty_text = (
        "clean"
        if prepare["clean"]
        else "dirty（{} 个文件）".format(len(prepare["dirtyFiles"]))
    )
    bullet(f"工作树：{dirty_text}")
    step(2, 3, "环境信息")
    bullet(f"运行时：{runtime or '未填写'}")
    bullet(f"构建命令：{build_cmd or '未填写'}")
    bullet(f"测试命令：{test_cmd or '未填写'}")
    bullet(f"文档目录：{'；'.join(docs_dirs)}")
    if tools:
        available = "、".join(f"{item['tool']}" for item in tools if item["available"]) or "无"
        bullet(f"工具链（可用）：{available}")
    for item in warnings:
        warn(item)
    step(3, 3, "下一步")
    bullet("请让 agent 基于以上事实撰写可行性分析文档（feasibility.md），然后运行：")
    bullet("feasibility --report-file <workspace>/feasibility.md --conclusion feasible|infeasible", indent="    ")
    bullet(f"本地状态：{context['status']}")
    return 0


# ---------------------------------------------------------------------------
# prereq：创建前置任务
# ---------------------------------------------------------------------------


def _compose_prereq_desc(
    *,
    current_task_id: int,
    current_task_name: str,
    current_task_url: str,
    requirement_text: str,
    deadline: str,
    assignee: str,
) -> str:
    lines = [
        "<h3>前置任务说明</h3>",
        "<ul>",
        f"<li>被阻塞任务：#{current_task_id} {html_lib.escape(current_task_name, quote=False)}</li>",
        f"<li>被阻塞任务链接：{html_lib.escape(current_task_url, quote=False)}</li>",
        f"<li>指派给：{html_lib.escape(assignee, quote=False)}</li>",
        f"<li>期望完成时限：{html_lib.escape(deadline, quote=False)}</li>",
        "</ul>",
        "<h3>所需前置条件</h3>",
        f"<p>{html_lib.escape(requirement_text, quote=False)}</p>",
        "<h3>期望交付物</h3>",
        "<p>完成上述前置条件并提供可验证结果（文档/接口/数据/环境/权限等）；"
        "完成后请在本任务备注说明，以便被阻塞任务继续。</p>",
        "<h3>说明</h3>",
        "<p>本任务由 zentao-dev-task-flow 创建，用于解除被阻塞任务的依赖；"
        "状态与指派保持独立，不自动开始。</p>",
    ]
    return "\n".join(lines)


def cmd_prereq(args: argparse.Namespace) -> int:
    ctx = prepare_command_context(args, need_login=True)
    context = ctx.workspace.load()
    current_status = _context_status(context)
    if current_status == "finished":
        raise StateConflictError("当前任务已完成收尾，不能创建前置任务")
    dry_run = bool(getattr(args, "dry_run", False))
    status = connect(ctx, remember=not dry_run)
    assert ctx.session is not None
    task_id = _as_int(ctx.task_id, "--task-id")
    task = ctx.session.get_task(task_id)
    task_snapshot = _task_snapshot(task)
    task_name = _as_str(task_snapshot.get("name")) or f"任务 {task_id}"
    assignee = _as_str(args.assignee)
    if not assignee:
        raise ConfigError("缺少 --assignee：请向用户确认前置任务指派给谁（一般是用户名拼音/禅道 account）")
    users = ctx.session.list_users()
    pending_warnings: List[str] = []
    for message in ctx.session.warnings:
        if message not in pending_warnings:
            pending_warnings.append(message)
            warn(message)
    if users:
        accounts = {account_of(item).lower() for item in users}
        if assignee.lower() not in accounts:
            raise ConfigError(
                f"指派账号不存在：{assignee}（用户列表中没有该 account）；"
                "请让用户确认账号后重试"
            )
    else:
        pending_warnings.append(
            "无法拉取禅道用户列表：跳过指派账号校验，请让用户确认该账号在禅道中存在"
        )

    task_type = _as_str(getattr(args, "type", None)) or DEFAULT_TASK_TYPE
    if task_type not in TASK_TYPES:
        raise ConfigError(f"未知的任务类型：{task_type!r}（可选：{', '.join(TASK_TYPES)}）")
    priority_label, priority = parse_priority(getattr(args, "priority", None) or DEFAULT_PRIORITY)
    estimate = _as_float(getattr(args, "estimate", None), "--estimate")
    default_start = _date_or_none(task_snapshot.get("estStarted"))
    default_deadline = _date_or_none(task_snapshot.get("deadline"))
    warnings: List[str] = list(pending_warnings)
    if not default_start or not default_deadline:
        default_start = default_start or today_str()
        default_deadline = default_deadline or (date.today() + timedelta(days=30)).isoformat()
        warnings.append("当前任务起止日期缺失，前置任务默认使用今天与 30 天后；请用户确认")
    est_started = (
        parse_date_text(args.est_started, "--est-started")
        if _as_str(getattr(args, "est_started", None))
        else default_start
    )
    deadline = (
        parse_date_text(args.deadline, "--deadline")
        if _as_str(getattr(args, "deadline", None))
        else default_deadline
    )
    if deadline < est_started:
        raise ConfigError(f"--deadline 不能早于 --est-started（{est_started} > {deadline}）")
    name = _as_str(getattr(args, "name", None)) or f"[前置] {task_name}"
    requirement_text = ""
    desc_file = _as_str(getattr(args, "desc_file", None))
    if desc_file:
        desc_path = Path(desc_file).expanduser()
        requirement_text = _read_text_file(desc_path, "--desc-file").strip()
    if not requirement_text:
        requirement_text = f"解除任务 #{task_id}「{task_name}」的阻塞依赖（由用户/agent 确认的前置条件）"
    story = _opt_int(getattr(args, "story", None), "--story") or _opt_int(
        task_snapshot.get("story"), "任务需求 id"
    )
    module = getattr(args, "module", None)
    if module is None:
        module = task_snapshot.get("module")
    execution_id = _opt_int(task_snapshot.get("execution"), "任务执行 id")
    if execution_id is None:
        raise ConfigError(
            "任务详情缺少 execution（执行 id），无法创建前置任务；请让用户确认任务所属执行"
        )
    payload: Dict[str, Any] = {
        "name": name,
        "executionID": execution_id,
        "assignedTo": assignee,
        "type": task_type,
        "estStarted": est_started,
        "deadline": deadline,
        "pri": priority,
        "desc": _compose_prereq_desc(
            current_task_id=task_id,
            current_task_name=task_name,
            current_task_url=task_link(
                ctx.credentials.base_url if ctx.credentials else "", task_id
            ),
            requirement_text=requirement_text,
            deadline=deadline,
            assignee=assignee,
        ),
    }
    if estimate is not None:
        payload["estimate"] = estimate
    if story:
        payload["story"] = story
    if module not in (None, ""):
        payload["module"] = module
    notify = bool(getattr(args, "notify", True))
    notify_skip_reason = ""
    plan_payload = {
        "taskId": task_id,
        "taskName": task_name,
        "executionId": execution_id,
        "notify": notify,
        "payload": payload,
        "warnings": warnings,
    }

    if bool(getattr(args, "dry_run", False)):
        if args.json:
            emit_json({"ok": True, "mode": "dry-run", **plan_payload})
            return 0
        step(1, 3, "前置任务计划（dry-run）")
        bullet(f"被阻塞任务：#{task_id} {task_name}")
        bullet(f"执行 id：{execution_id}")
        bullet(f"前置任务名称：{name}")
        bullet(f"指派给：{assignee}；类型：{task_type}；优先级：{priority_label}（pri={priority}）")
        bullet(f"起止：{est_started} ~ {deadline}")
        step(2, 3, "请求体")
        bullet(json.dumps(payload, ensure_ascii=False))
        step(3, 3, "dry-run")
        for item in warnings:
            warn(item)
        say("")
        say("[dry-run] 未创建任何禅道任务、未修改 context；用户确认后加 --confirm 执行。")
        return 0
    if not bool(getattr(args, "confirm", False)):
        raise ConfigError(
            "创建前置任务会写入禅道：请把上面的计划展示给用户，取得明确同意后加 --confirm 重试"
        )

    created = ctx.session.create_task(payload)
    created_id = _opt_int(created.get("id"), "前置任务 id")
    if created_id is None:
        raise ZenTaoError(
            "禅道创建前置任务成功但未返回 id：" + json.dumps(created, ensure_ascii=False)[:300]
        )
    created_url = task_link(ctx.credentials.base_url if ctx.credentials else "", created_id)
    notified = False
    notify_error = ""
    if notify:
        html = (
            f"<h3>已创建前置任务</h3><p>为解除任务 #{task_id}「{html_lib.escape(task_name, quote=False)}」"
            f"的阻塞，已创建前置任务 <a href=\"{html_lib.escape(created_url, quote=False)}\">"
            f"#{created_id} {html_lib.escape(name, quote=False)}</a>，指派给 "
            f"{html_lib.escape(assignee, quote=False)}。</p>"
        )
        try:
            ctx.session.comment_task(task_id, html)
            notified = True
        except FlowError as exc:
            notify_error = exc.message
            warnings.append(f"在当前任务下留言通知失败（不影响前置任务创建）：{notify_error}")
    prereq_record = {
        "taskId": created_id,
        "name": name,
        "assignee": assignee,
        "url": created_url,
        "createdAt": now_str(),
        "notified": notified,
        "desc": payload["desc"],
    }
    context.setdefault("prerequisites", []).append(prereq_record)
    context.setdefault("timestamps", {})["updatedAt"] = now_str()
    for item in warnings:
        _append_warning(context, item)
    ctx.workspace.save(context)

    if args.json:
        emit_json(
            {
                "ok": True,
                "mode": "publish",
                **plan_payload,
                "prerequisite": prereq_record,
                "contextPath": str(ctx.workspace.context_path),
            }
        )
        return 0
    step(1, 2, "前置任务已创建")
    bullet(f"前置任务：#{created_id} {name}")
    bullet(f"链接：{created_url}")
    bullet(f"指派给：{assignee}；起止：{est_started} ~ {deadline}")
    bullet(f"当前任务留言通知：{'已提交' if notified else ('跳过' if not notify else '失败')}")
    if notify_skip_reason:
        bullet(f"通知跳过原因：{notify_skip_reason}")
    step(2, 2, "context 更新")
    bullet(f"已记录 prerequisites（{len(context.get('prerequisites') or [])} 条）：{ctx.workspace.context_path}")
    say("")
    say("请让用户跟进前置任务进展；完成后回到当前任务继续 prepare/feasibility/plan。")
    return 0


# ---------------------------------------------------------------------------
# feasibility / plan / approve-plan（本地阶段推进）
# ---------------------------------------------------------------------------


def cmd_feasibility(args: argparse.Namespace) -> int:
    ctx = prepare_command_context(args, need_login=False)
    context = ctx.workspace.load()
    _require_status(context, ("prepared", "feasibility_done"), "feasibility")
    report_path = Path(_as_str(args.report_file)).expanduser()
    report_text = _read_text_file(report_path, "--report-file")
    conclusion = _as_str(args.conclusion)
    blockers = [item for item in (args.blocker or []) if _as_str(item)]
    context["feasibility"] = {
        "conclusion": conclusion,
        "reportFile": str(report_path),
        "reportSummary": _excerpt(report_text, 800),
        "blockers": blockers,
        "createdAt": now_str(),
    }
    # infeasible 不再是独立状态：状态保持 feasibility_done，由 conclusion 控制 plan 门禁；
    # 允许在 feasibility_done 下重跑 feasibility 刷新结论（infeasible→feasible 可恢复）。
    _set_status(context, "feasibility_done", "feasibilityAt")
    if conclusion == "infeasible":
        message = (
            "可行性结论：不可行。请把 blockers 展示给用户，按需创建前置任务（prereq）"
            "或停止当前任务；不要继续 plan/start。修正后可重跑 feasibility 刷新结论。"
        )
    else:
        message = "可行性结论：可行。下一步请撰写开发计划（dev-plan.md）并运行 plan。"
    ctx.workspace.save(context)
    payload = {
        "ok": True,
        "taskId": ctx.task_id,
        "status": context["status"],
        "conclusion": conclusion,
        "reportFile": str(report_path),
        "blockers": blockers,
        "contextPath": str(ctx.workspace.context_path),
        "nextStep": message,
    }
    if args.json:
        emit_json(payload)
        return 0
    step(1, 2, "可行性分析记录")
    bullet(f"报告文件：{report_path}")
    bullet(f"结论：{conclusion}")
    if blockers:
        for item in blockers:
            bullet(f"阻塞项：{item}")
    bullet(f"本地状态：{context['status']}（{STATE_LABELS.get(context['status'], '')}）")
    step(2, 2, "下一步")
    say(message)
    return 0


def _count_plan_steps(text: str) -> Tuple[int, int]:
    step_count = 0
    risk_count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^(?:\d+[.)]|[-*+])\s+", stripped):
            step_count += 1
        if "风险" in stripped or "rollback" in stripped.lower() or "回滚" in stripped:
            risk_count += 1
    return step_count, risk_count


def cmd_plan(args: argparse.Namespace) -> int:
    ctx = prepare_command_context(args, need_login=False)
    context = ctx.workspace.load()
    current = _context_status(context)
    if current not in ("feasibility_done", "plan_done"):
        approved_hint = ""
        if current in ("plan_approved", "developing", "accepted", "finished") or bool(
            (context.get("plan") or {}).get("approved")
        ):
            approved_hint = (
                "；计划已获用户批准，不能在已批准/执行阶段直接改写计划，"
                "如确需修改请与用户确认后重新走 plan → approve-plan --confirm（或 init --force 重置）"
            )
        raise GateError(
            f"plan 要求当前状态为 feasibility_done（首次）或 plan_done（重跑），实际是 "
            f"{current}（{STATE_LABELS.get(current, current)}）{approved_hint}"
        )
    feasibility = context.get("feasibility") or {}
    if _as_str(feasibility.get("conclusion")) != "feasible":
        raise GateError(
            f"可行性结论为 {feasibility.get('conclusion') or '未知'}，不允许进入开发计划阶段；"
            "请先处理阻塞项、重跑 feasibility 修正结论，或创建前置任务"
        )
    if current == "plan_done":
        warn("检测到 plan 重跑：将刷新开发计划记录（需重新提交用户审核；已批准计划不可在此阶段改写）")
    plan_path = Path(_as_str(args.plan_file)).expanduser()
    plan_text = _read_text_file(plan_path, "--plan-file")
    step_count, risk_count = _count_plan_steps(plan_text)
    context["plan"] = {
        "file": str(plan_path),
        "approved": False,
        "stepCount": step_count,
        "riskCount": risk_count,
        "summary": _excerpt(plan_text, 800),
        "createdAt": now_str(),
    }
    _set_status(context, "plan_done", "planAt")
    ctx.workspace.save(context)
    payload = {
        "ok": True,
        "taskId": ctx.task_id,
        "status": context["status"],
        "planFile": str(plan_path),
        "stepCount": step_count,
        "riskCount": risk_count,
        "contextPath": str(ctx.workspace.context_path),
        "nextStep": "把 dev-plan.md 完整展示给用户审核；用户明确同意后运行 approve-plan --confirm",
    }
    if args.json:
        emit_json(payload)
        return 0
    step(1, 2, "开发计划已记录")
    bullet(f"计划文件：{plan_path}")
    bullet(f"步骤数：{step_count}；风险相关条目：{risk_count}")
    bullet(f"本地状态：{context['status']}（{STATE_LABELS.get(context['status'], '')}）")
    step(2, 2, "下一步")
    say("请把开发计划完整展示给用户审核（目标/范围/技术方案/改动文件/步骤/测试验证/风险回滚/工时）。")
    say("用户明确同意后运行：approve-plan --confirm")
    return 0


def cmd_approve_plan(args: argparse.Namespace) -> int:
    ctx = prepare_command_context(args, need_login=False)
    context = ctx.workspace.load()
    _require_status(context, ("plan_done",), "approve-plan")
    if not bool(getattr(args, "confirm", False)):
        raise ConfigError(
            "用户批准开发计划是执行开发的唯一前置门槛：请先把 dev-plan.md 展示给用户，"
            "取得明确同意后加 --confirm 重试"
        )
    plan = context.setdefault("plan", {})
    plan["approved"] = True
    plan["approvedAt"] = now_str()
    _set_status(context, "plan_approved", "planApprovedAt")
    ctx.workspace.save(context)
    payload = {
        "ok": True,
        "taskId": ctx.task_id,
        "status": context["status"],
        "planFile": plan.get("file", ""),
        "approvedAt": plan["approvedAt"],
        "contextPath": str(ctx.workspace.context_path),
        "nextStep": f"运行 start --task-id {ctx.task_id} 开始开发（同步禅道任务状态）",
    }
    if args.json:
        emit_json(payload)
        return 0
    step(1, 1, "计划已获用户批准")
    bullet(f"计划：{plan.get('file', '')}")
    bullet(f"本地状态：{context['status']}（{STATE_LABELS.get(context['status'], '')}）")
    say("")
    say("下一步：运行 start 开始开发，并让 agent 按计划编码/自测，把进展记录到 summary.md。")
    return 0


# ---------------------------------------------------------------------------
# start：开始开发（同步禅道任务状态）
# ---------------------------------------------------------------------------


def cmd_start(args: argparse.Namespace) -> int:
    ctx = prepare_command_context(args, need_login=True)
    context = ctx.workspace.load()
    _require_status(context, ("plan_approved",), "start")
    status = connect(ctx, remember=True)
    assert ctx.session is not None
    task_id = _as_int(ctx.task_id, "--task-id")
    task = ctx.session.get_task(task_id)
    task_status = _as_str(task.get("status"))
    if task_status not in START_ALLOWED_TASK_STATUS:
        raise StateConflictError(
            f"禅道任务 {task_id} 当前状态为 {task_status or '未知'}，不允许开始开发"
            "（允许：wait / doing）；done/closed/cancel 等状态请与用户确认处理方式"
        )
    warnings: List[str] = []
    no_sync = bool(getattr(args, "no_sync_status", False))
    payload: Dict[str, Any] = {}
    action = "sync"
    if no_sync:
        action = "local-only"
        warnings.append("按 --no-sync-status 仅记录本地开始时间，不修改禅道任务状态")
    elif task_status == "doing":
        action = "already-doing"
        warnings.append("禅道任务已处于 doing（开发中）：跳过 start 接口，仅推进本地状态")
    else:
        estimate = _as_float(task.get("estimate"), "任务预计工时")
        left_value = _as_float(task.get("left"), "任务剩余工时")
        if left_value is not None and left_value > 0:
            left = left_value
        elif estimate is not None and estimate > 0:
            left = estimate
        else:
            left = 1.0
        payload = {
            "realStarted": _normalize_datetime(now_str(), "realStarted"),
            "consumed": 0,
            "left": left,
            "comment": "已确认开发计划，开始开发（计划：dev-plan.md）",
            "assignedTo": account_of(task.get("assignedTo"))
            or (ctx.credentials.username if ctx.credentials else ""),
        }
        ctx.session.start_task(task_id, payload)
    context["startedAt"] = now_str()
    context["start"] = {
        "action": action,
        "taskStatusBefore": task_status,
        "startPayload": payload,
        "noSyncStatus": no_sync,
        "startedAt": context["startedAt"],
    }
    for message in warnings:
        _append_warning(context, message)
    _set_status(context, "developing", "startedAt")
    ctx.workspace.save(context)
    prepare = context.get("prepare") or {}
    payload_json = {
        "ok": True,
        "taskId": task_id,
        "status": context["status"],
        "action": action,
        "taskStatusBefore": task_status,
        "startPayload": payload,
        "workspace": str(ctx.workspace.dir),
        "devNotes": {
            "repoPath": prepare.get("repoPath", ""),
            "branch": prepare.get("branch", ""),
            "planFile": (context.get("plan") or {}).get("file", ""),
            "buildCmd": prepare.get("buildCmd", ""),
            "testCmd": prepare.get("testCmd", ""),
            "summaryFile": str(ctx.workspace.path("summary.md")),
        },
        "login": _login_payload(status),
        "warnings": warnings,
    }
    if args.json:
        emit_json(payload_json)
        return 0
    step(1, 2, "开发状态")
    if action == "sync":
        bullet(f"已调用 start 接口开始任务（left={payload.get('left')}）")
    elif action == "already-doing":
        bullet("禅道任务已处于开发中：未重复调用 start")
    else:
        bullet("按 --no-sync-status 仅记录本地开始（未改禅道状态）")
    bullet(f"本地状态：{context['status']}（{STATE_LABELS.get(context['status'], '')}）")
    step(2, 2, "开发须知")
    bullet(f"仓库：{prepare.get('repoPath') or '（未在 prepare 记录）'}")
    bullet(f"分支：{prepare.get('branch') or '未知'}")
    bullet(f"计划：{payload_json['devNotes']['planFile'] or '（未记录）'}")
    bullet(f"构建命令：{prepare.get('buildCmd') or '未填写'}")
    bullet(f"测试命令：{prepare.get('testCmd') or '未填写'}")
    bullet(f"开发过程总结（收尾必需）：{ctx.workspace.path('summary.md')}")
    for item in warnings:
        warn(item)
    say("")
    say("让 agent 按计划实施开发与自测，并把开发过程/验证结果写入 summary.md；完成后进入用户验收。")
    return 0


# ---------------------------------------------------------------------------
# accept：用户验收（仅本地记录）
# ---------------------------------------------------------------------------


def cmd_accept(args: argparse.Namespace) -> int:
    ctx = prepare_command_context(args, need_login=False)
    context = ctx.workspace.load()
    _require_status(context, ("developing",), "accept")
    if bool(getattr(args, "accept_pass", False)):
        evidence_raw = _as_str(getattr(args, "evidence_file", None))
        if not evidence_raw:
            raise ConfigError(
                "accept --pass 必须提供 --evidence-file <md>：请让用户给出验收证据文件"
                "（测试记录/截图说明等）；未提供证据不允许记录验收通过"
            )
        evidence_path = Path(evidence_raw).expanduser()
        if not evidence_path.is_file():
            raise ConfigError(f"--evidence-file 指向的文件不存在：{evidence_path}")
        try:
            evidence_text = evidence_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"--evidence-file 不是 UTF-8 文本：{evidence_path}（{exc}）") from exc
        if not evidence_text.strip():
            raise ConfigError(f"--evidence-file 内容为空：{evidence_path}")
        notes = _as_str(getattr(args, "notes", None))
        acceptance_path = ctx.workspace.path("acceptance.md")
        content = "\n".join(
            [
                "# 用户验收记录",
                "",
                f"- 任务：#{ctx.task_id}",
                "- 结论：通过",
                f"- 验收时间：{now_str()}",
                f"- 证据文件：{evidence_path}",
                f"- 备注：{notes or '无'}",
                "",
                "## 验收证据内容",
                "",
                evidence_text.rstrip(),
                "",
            ]
        )
        acceptance_path.write_text(content, encoding="utf-8")
        context["acceptance"] = {
            "passed": True,
            "evidenceFile": str(evidence_path),
            "notes": notes,
            "acceptedAt": now_str(),
        }
        _set_status(context, "accepted", "acceptedAt")
        ctx.workspace.save(context)
        payload = {
            "ok": True,
            "taskId": ctx.task_id,
            "passed": True,
            "status": context["status"],
            "evidenceFile": str(evidence_path),
            "acceptanceFile": str(acceptance_path),
            "contextPath": str(ctx.workspace.context_path),
            "nextStep": f"运行 finish --task-id {ctx.task_id} --dry-run 生成收尾计划",
        }
        if args.json:
            emit_json(payload)
            return 0
        step(1, 2, "用户验收：通过")
        bullet(f"证据文件：{evidence_path}")
        bullet(f"验收记录：{acceptance_path}")
        bullet(f"本地状态：{context['status']}（{STATE_LABELS.get(context['status'], '')}）")
        step(2, 2, "下一步")
        say("下一步：运行 finish --dry-run 展示收尾计划，用户确认后加 --confirm 执行收尾。")
        return 0

    reason = _as_str(getattr(args, "reason", None))
    if not reason:
        raise ConfigError("accept --fail 需要 --reason：请让用户说明未通过原因")
    notes = _as_str(getattr(args, "notes", None))
    acceptance = context.setdefault("acceptance", {})
    attempts = acceptance.setdefault("attempts", [])
    attempts.append({"passed": False, "reason": reason, "notes": notes, "at": now_str()})
    acceptance["passed"] = False
    context.setdefault("timestamps", {})["updatedAt"] = now_str()
    ctx.workspace.save(context)
    payload = {
        "ok": True,
        "taskId": ctx.task_id,
        "passed": False,
        "status": context["status"],
        "reason": reason,
        "contextPath": str(ctx.workspace.context_path),
        "nextStep": "修复后重新验收（accept --pass --evidence-file ...）；未通过不允许 finish",
    }
    if args.json:
        emit_json(payload)
        return 0
    step(1, 2, "用户验收：未通过")
    bullet(f"原因：{reason}")
    if notes:
        bullet(f"备注：{notes}")
    bullet(f"本地状态保持：{context['status']}（{STATE_LABELS.get(context['status'], '')}）")
    step(2, 2, "下一步")
    say("请让 agent 修复问题后再次验收（accept --pass --evidence-file ...）；验收未通过不允许 finish。")
    return 0


# ---------------------------------------------------------------------------
# status：状态总览（只读）
# ---------------------------------------------------------------------------

NEXT_STEP_BY_STATUS = {
    "initialized": "prepare：补充研发环境与 Git 仓库信息",
    "prepared": "feasibility：撰写 feasibility.md 并记录可行性结论",
    "feasibility_done": "plan：撰写 dev-plan.md 并记录开发计划",
    "plan_done": "approve-plan --confirm：把计划给用户审核并取得批准",
    "plan_approved": "start：开始开发（同步禅道任务状态）",
    "developing": "accept --pass/--fail：用户验收（通过前不能 finish）",
    "accepted": "finish --dry-run → --confirm：附件/备注/完成/指派回收尾",
    "finished": "已完成收尾；如需重新收尾请与用户确认后 --force 重新 init",
}


def cmd_status(args: argparse.Namespace) -> int:
    ctx = prepare_command_context(args, need_login=True, require_workspace=False)
    context = ctx.workspace.load()
    status = connect(ctx, remember=False)
    assert ctx.session is not None
    task_id = _as_int(ctx.task_id, "--task-id")
    task = ctx.session.get_task(task_id)
    files = task.get("files") if isinstance(task.get("files"), list) else []
    actions = task.get("actions") if isinstance(task.get("actions"), list) else []
    current = _context_status(context)
    next_step = NEXT_STEP_BY_STATUS.get(current, "请检查 context.json 状态")
    if (
        current == "feasibility_done"
        and _as_str((context.get("feasibility") or {}).get("conclusion")) == "infeasible"
    ):
        next_step = (
            "可行性结论为 infeasible：请重跑 feasibility 修正结论，"
            "或按 blockers 创建前置任务（prereq）后再继续"
        )
    payload = {
        "ok": True,
        "taskId": task_id,
        "workspace": str(ctx.workspace.dir),
        "contextPath": str(ctx.workspace.context_path),
        "state": {
            "status": current,
            "label": STATE_LABELS.get(current, current),
            "stageIndex": STATE_INDEX.get(current, -1),
            "timestamps": context.get("timestamps") or {},
        },
        "task": {
            "id": task.get("id"),
            "name": _as_str(task.get("name")),
            "status": _as_str(task.get("status")),
            "assignedTo": account_of(task.get("assignedTo")),
            "openedBy": account_of(task.get("openedBy")),
            "filesCount": len(files),
            "actionsCount": len(actions),
            "url": task_link(ctx.credentials.base_url if ctx.credentials else "", task_id),
        },
        "creator": context.get("creator") or {},
        "prepare": context.get("prepare") or {},
        "prerequisites": context.get("prerequisites") or [],
        "feasibility": context.get("feasibility") or {},
        "plan": context.get("plan") or {},
        "acceptance": {
            key: value
            for key, value in (context.get("acceptance") or {}).items()
            if key != "attempts"
        },
        "delivery": context.get("delivery") or {},
        "login": _login_payload(status),
        "nextStep": next_step,
        "warnings": context.get("warnings") or [],
    }
    if args.json:
        emit_json(payload)
        return 0
    step(1, 3, "本地状态机")
    bullet(
        f"状态：{current}（{STATE_LABELS.get(current, current)}）"
        f"；阶段 {STATE_INDEX.get(current, -1) + 1}/{len(STATE_ORDER)}"
    )
    bullet(f"工作区：{ctx.workspace.dir}")
    timestamps = context.get("timestamps") or {}
    if timestamps:
        bullet("时间线：" + "；".join(f"{key}={value}" for key, value in timestamps.items()))
    step(2, 3, "禅道任务")
    bullet(
        f"任务：#{task_id} {_as_str(task.get('name'))}（状态：{_as_str(task.get('status'))}）"
    )
    bullet(
        f"指派：{account_of(task.get('assignedTo')) or '未知'}；"
        f"附件 {len(files)} 个；actions {len(actions)} 条"
    )
    bullet(f"链接：{payload['task']['url']}")
    bullet(f"登录模式：{'v2 API' if status.login_mode == 'v2' else 'v1 /tokens 兜底'}")
    step(3, 3, "下一步")
    say(f"下一步：{payload['nextStep']}")
    if payload["prerequisites"]:
        bullet(f"前置任务：{len(payload['prerequisites'])} 条（context.prerequisites）")
    for item in payload["warnings"]:
        warn(item)
    return 0


# ---------------------------------------------------------------------------
# config-show / config-forget
# ---------------------------------------------------------------------------


def _config_path_from_args(args: argparse.Namespace) -> Path:
    return (
        Path(args.config).expanduser()
        if getattr(args, "config", None)
        else default_config_path()
    )


def cmd_config_show(args: argparse.Namespace) -> int:
    config_path = _config_path_from_args(args)
    config = load_user_config(config_path)
    payload = {
        "configPath": str(config_path),
        "exists": bool(config),
        "baseUrl": _as_str(config.get("baseUrl")),
        "username": _as_str(config.get("username")),
        "passwordSaved": bool(config.get("password")),
        "passwordScheme": (
            _as_str(config.get("password", {}).get("scheme"))
            if isinstance(config.get("password"), dict)
            else ("legacy" if config.get("password") else "")
        ),
        "passwordDescription": password_saved_description(config.get("password")),
        "lastTaskId": _as_str(config.get("lastTaskId")),
        "lastWorkspace": _as_str(config.get("lastWorkspace")),
        "updatedAt": _as_str(config.get("updatedAt")),
        "notes": "复用任何已保存值前必须先向用户复述并取得确认，再加 --reuse。",
    }
    if args.json:
        emit_json(payload)
        return 0
    say(f"配置文件：{config_path}")
    if not config:
        say("尚未保存任何禅道配置。")
        return 0
    say(f"禅道地址：{payload['baseUrl'] or '未保存'}")
    say(f"用户名：{payload['username'] or '未保存'}")
    say(f"密码：{payload['passwordDescription']}")
    say(f"上次任务 id：{payload['lastTaskId'] or '未保存'}")
    say(f"上次工作区：{payload['lastWorkspace'] or '未保存'}")
    say(f"更新时间：{payload['updatedAt'] or '未记录'}")
    say("")
    say("复用已保存的地址/账号/任务/工作区前，必须先向用户确认；确认后使用 --reuse。")
    return 0


def cmd_config_forget(args: argparse.Namespace) -> int:
    config_path = _config_path_from_args(args)
    if not config_path.is_file():
        if args.json:
            emit_json({"ok": True, "configPath": str(config_path), "deleted": False})
        else:
            say(f"没有需要清除的配置文件：{config_path}")
        return 0
    if not bool(getattr(args, "yes", False)):
        raise ConfigError(
            f"清除会删除已保存的禅道账号密码与上次任务记录：{config_path}\n确认删除请加 --yes"
        )
    config_path.unlink()
    if args.json:
        emit_json({"ok": True, "configPath": str(config_path), "deleted": True})
    else:
        say(f"已删除配置文件：{config_path}")
    return 0


# ---------------------------------------------------------------------------
# finish：收尾（zip 附件 + 独立备注 + 完成任务 + 指派回需求创建人）
# ---------------------------------------------------------------------------

SUMMARY_COMMENT_MARKER = "开发过程总结"


def _task_files(task: dict) -> List[dict]:
    files = task.get("files")
    if not isinstance(files, list):
        return []
    return [item for item in files if isinstance(item, dict)]


def _task_actions(task: dict) -> List[dict]:
    actions = task.get("actions")
    if not isinstance(actions, list):
        return []
    return [item for item in actions if isinstance(item, dict)]


def _match_attachment_file(files: Sequence[dict], zip_name: str) -> Optional[dict]:
    for item in files:
        for key in ("title", "filename", "name", "url"):
            value = _as_str(item.get(key))
            if value and (value == zip_name or zip_name in value or Path(value).name == zip_name):
                return item
    return None


def _normalize_datetime(value: Any, label: str) -> str:
    text = _as_str(value)
    if not text:
        return now_str()
    # 容忍 now_str() 产生的时区后缀（如 " +0800"）、UTC "Z" 与毫秒
    text = re.sub(r"[+-]\d{2}:?\d{2}$", "", text).strip()
    text = re.sub(r"Z$", "", text).strip()
    text = re.sub(r"\.\d{1,6}$", "", text)
    if _DATE_RE.match(text[:10]) and len(text) == 10:
        return text + " 00:00:00"
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", text):
        return text + ":00"
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", text):
        return text
    raise ConfigError(f"{label} 时间格式不正确（应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS）：{text}")


def _finish_preview(
    *,
    ctx: CommandContext,
    context: dict,
    zip_name: str,
    doc_dirs: Sequence[Path],
    extra_files: Sequence[Path],
) -> Dict[str, Any]:
    entries, warnings = collect_delivery_entries(
        ctx.workspace, doc_dirs=doc_dirs, extra_files=extra_files
    )
    total = 0
    files: List[dict] = []
    for arcname, source in entries:
        size = source.stat().st_size if source.is_file() else 0
        total += size
        files.append({"name": arcname, "size": size})
    return {
        "zipName": zip_name,
        "entries": files,
        "totalSize": total,
        "warnings": warnings,
    }


def _finish_report_payload(
    *,
    mode: str,
    ok: bool,
    ctx: CommandContext,
    task: dict,
    context: dict,
    assign_back: str,
    assign_back_source: str,
    consumed: float,
    real_started: str,
    finished_date: str,
    delivery: dict,
    stages: dict,
    warnings: Sequence[str],
    login: Optional[LoginStatus],
    error: Optional[FlowError] = None,
) -> dict:
    base_url = ctx.credentials.base_url if ctx.credentials else ""
    payload = {
        "mode": mode,
        "ok": ok,
        "taskId": ctx.task_id,
        "task": {
            "id": task.get("id"),
            "name": _as_str(task.get("name")),
            "status": _as_str(delivery.get("finalTaskStatus")) or _as_str(task.get("status")),
            "url": task_link(base_url, task.get("id")),
        },
        "assignBack": assign_back,
        "assignBackSource": assign_back_source,
        "consumed": consumed,
        "realStarted": real_started,
        "finishedDate": finished_date,
        "workspace": str(ctx.workspace.dir),
        "contextPath": str(ctx.workspace.context_path),
        "delivery": delivery,
        "stages": stages,
        "warnings": list(warnings),
    }
    if login is not None:
        payload["login"] = _login_payload(login)
    if error is not None:
        payload["error"] = {
            "exitCode": error.exit_code,
            "message": error.message,
            "detail": error.detail,
        }
    return payload


def cmd_finish(args: argparse.Namespace) -> int:
    ctx = prepare_command_context(args, need_login=True)
    context = ctx.workspace.load()
    current = _context_status(context)
    if current not in ("developing", "accepted"):
        raise GateError(
            f"finish 要求当前状态为 developing 或 accepted，实际是 {current}"
            f"（{STATE_LABELS.get(current, current)}）；请先完成开发与用户验收"
        )
    acceptance = context.get("acceptance") or {}
    if not bool(acceptance.get("passed")):
        raise ConfigError(
            "用户验收尚未通过：finish 需要先运行 accept --pass --evidence-file <证据文件>；"
            "禁止跳过验收门槛"
        )
    summary_path = ctx.workspace.path("summary.md")
    summary_text = _read_text_file(summary_path, "summary.md（开发过程总结，agent 必须已写入）")
    dry_run = bool(getattr(args, "dry_run", False))
    status = connect(ctx, remember=not dry_run)
    assert ctx.session is not None
    task_id = _as_int(ctx.task_id, "--task-id")
    task = ctx.session.get_task(task_id)
    task_status = _as_str(task.get("status"))
    if task_status in FINISH_FORBIDDEN_TASK_STATUS:
        raise StateConflictError(
            f"禅道任务 {task_id} 当前状态为 {task_status}，不允许收尾"
            "（done/closed/cancel 已是终态）。请回读任务状态并把下一步建议转告用户"
        )

    creator = context.get("creator") or {}
    explicit_assign = _as_str(getattr(args, "assign_back", None))
    if explicit_assign:
        assign_back = explicit_assign
        assign_back_source = "参数 --assign-back"
    elif _as_str(creator.get("resolvedAssignee")):
        assign_back = _as_str(creator.get("resolvedAssignee"))
        assign_back_source = _as_str(creator.get("source")) or "creator.resolvedAssignee"
    elif account_of(task.get("openedBy")):
        assign_back = account_of(task.get("openedBy"))
        assign_back_source = "task_openedBy（兜底）"
    else:
        raise ConfigError(
            "无法确定收尾指派账号：需求创建人与任务创建人均缺失；"
            "请让用户指定 --assign-back <account>（通常为需求创建人）"
        )

    warnings: List[str] = []
    estimate = _as_float(task.get("estimate"), "任务预计工时")
    consumed = _as_float(getattr(args, "consumed", None), "--consumed")
    if consumed is None:
        if estimate is not None and estimate > 0:
            consumed = estimate
        else:
            consumed = 1.0
            warnings.append("任务预计工时缺失/为 0：收尾消耗工时默认 1.0 小时，请用户确认")
    if consumed <= 0:
        consumed = 1.0
        warnings.append("currentConsumed 必须大于 0：已按 1.0 小时处理")

    started_at_default = _as_str(context.get("startedAt")) or _as_str(task.get("realStarted"))
    real_started = (
        _normalize_datetime(getattr(args, "real_started", None), "--real-started")
        if _as_str(getattr(args, "real_started", None))
        else _normalize_datetime(started_at_default or now_str(), "realStarted")
    )
    finished_date = (
        _normalize_datetime(getattr(args, "finished_date", None), "--finished-date")
        if _as_str(getattr(args, "finished_date", None))
        else _normalize_datetime(now_str(), "finishedDate")
    )
    zip_name = _as_str(getattr(args, "zip_name", None)) or (
        f"{task_id}-delivery-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    )
    if not zip_name.lower().endswith(".zip"):
        zip_name += ".zip"
    prepare = context.get("prepare") or {}
    repo_raw = _as_str(prepare.get("repoPath"))
    if not repo_raw:
        raise GateError("context.prepare 未记录仓库路径：请先运行 prepare 收集 Git 仓库信息")
    repo_path = Path(repo_raw).expanduser()
    doc_dirs: List[Path] = []
    for item in list(getattr(args, "doc_dir", None) or []):
        directory = Path(_as_str(item)).expanduser()
        if not directory.is_dir():
            raise GateError(f"--doc-dir 指向的目录不存在：{directory}")
        doc_dirs.append(directory)
    for item in _as_str_list((context.get("prepare") or {}).get("docsDirs")):
        directory = Path(item).expanduser()
        if directory.is_dir() and directory not in doc_dirs:
            doc_dirs.append(directory)
    extra_files = [Path(_as_str(item)).expanduser() for item in (getattr(args, "extra_file", None) or [])]
    no_attach = bool(getattr(args, "no_attach", False))
    now = now_str()

    # ---- 收尾计划（dry-run / 未确认前展示）----
    preview = _finish_preview(
        ctx=ctx, context=context, zip_name=zip_name, doc_dirs=doc_dirs, extra_files=extra_files
    )
    warnings.extend(preview["warnings"])
    summary_html = markdown_to_html(summary_text)
    comment_html = (
        f"<h3>{SUMMARY_COMMENT_MARKER}（任务 #{task_id}）</h3>\n{summary_html}"
    )
    short_comment = (
        f"开发完成并收尾：任务 #{task_id}「{_as_str(task.get('name'))}」。"
        f"附件：{zip_name}；开发过程总结见任务备注。"
    )
    finish_comment_plan = short_comment
    finish_payload_plan = {
        "currentConsumed": consumed,
        "realStarted": real_started,
        "finishedDate": finished_date,
        "assignedTo": assign_back,
        "comment": finish_comment_plan,
    }
    plan_payload = {
        "taskId": task_id,
        "taskName": _as_str(task.get("name")),
        "taskStatus": task_status,
        "taskUrl": task_link(ctx.credentials.base_url if ctx.credentials else "", task_id),
        "assignBack": assign_back,
        "assignBackSource": assign_back_source,
        "consumed": consumed,
        "realStarted": real_started,
        "finishedDate": finished_date,
        "zipName": zip_name,
        "zipPreview": {
            "entries": preview["entries"],
            "totalSize": preview["totalSize"],
            "humanSize": human_size(preview["totalSize"]),
        },
        "noAttach": no_attach,
        "apiMode": "v2",
        "commentMode": "独立备注（v2 /actions/comment；失败降级并入 finish comment）",
        "commentPreview": _excerpt(markdown_to_html(summary_text), 300),
        "finishPayload": finish_payload_plan,
        "warnings": warnings,
    }

    if bool(getattr(args, "dry_run", False)):
        if args.json:
            emit_json({"ok": True, "mode": "dry-run", **plan_payload})
            return 0
        step(1, 4, "收尾计划（dry-run）")
        bullet(f"任务：#{task_id} {plan_payload['taskName']}（禅道状态：{task_status}）")
        bullet(
            f"附件 zip：{zip_name}（{human_size(preview['totalSize'])}，"
            f"{len(preview['entries'])} 个文件）"
        )
        for item in preview["entries"]:
            bullet(f"{item['name']}（{human_size(item['size'])}）", indent="    ")
        bullet(f"上传方式：{'--no-attach（不自动上传，需用户手动上传）' if no_attach else 'v2 /files multipart 附件上传'}")
        bullet(f"备注方式：{plan_payload['commentMode']}")
        bullet(f"指派回：{assign_back}（来源：{assign_back_source}）")
        bullet(f"消耗工时：{consumed}；realStarted：{real_started}；finishedDate：{finished_date}")
        step(2, 4, "finish 请求体（不含 summary 全文）")
        bullet(json.dumps({**finish_payload_plan, "comment": _excerpt(finish_comment_plan, 200)}, ensure_ascii=False))
        step(3, 4, "能力与告警")
        for item in warnings:
            warn(item)
        step(4, 4, "dry-run")
        say("")
        say("[dry-run] 未生成 zip、未写禅道、未修改 context；用户确认后加 --confirm 执行收尾。")
        return 0
    if not bool(getattr(args, "confirm", False)):
        raise ConfigError(
            "收尾会生成 zip 并写入禅道（附件/备注/完成任务/指派）：请先把收尾计划展示给用户，"
            "取得明确同意后加 --confirm 重试"
        )

    # ---- 正式执行（严格顺序；失败停止并如实报告阶段）----
    stages = {
        "zipGenerated": False,
        "attachmentUploaded": False,
        "attachmentSkipped": False,
        "commentAdded": False,
        "commentDegraded": False,
        "finishExecuted": False,
    }
    delivery: Dict[str, Any] = {}
    degraded: List[str] = []
    try:
        # 1) 构建 zip
        build = build_delivery_zip(
            workspace=ctx.workspace,
            context=context,
            repo_path=repo_path,
            doc_dirs=doc_dirs,
            extra_files=extra_files,
            zip_name=zip_name,
        )
        warnings.extend(build.get("warnings") or [])
        zip_path = Path(build["zipFile"])
        delivery.update(
            {
                "zipFile": str(zip_path),
                "zipName": zip_name,
                "zipSize": build["zipSize"],
                "zipSha256": build["zipSha256"],
                "zipEntries": build.get("entries") or [],
            }
        )
        stages["zipGenerated"] = True
        bullet(
            f"[1/5] zip 已生成：{zip_path}（{human_size(build['zipSize'])}，"
            f"{len(build.get('entries') or [])} 个文件）"
        )

        # 2) 上传附件（幂等：同名已存在则复用）
        task_now = ctx.session.get_task(task_id)
        existing_file = _match_attachment_file(_task_files(task_now), zip_name)
        if existing_file is not None:
            delivery["attachmentFileId"] = existing_file.get("id")
            delivery["attachmentUrl"] = _as_str(existing_file.get("url"))
            stages["attachmentUploaded"] = True
            stages["attachmentSkipped"] = True
            warnings.append(f"禅道已存在同名附件（{zip_name}）：按幂等规则复用，不重复上传")
            bullet(f"[2/5] 附件已存在：复用 id={existing_file.get('id')}（不重复上传）")
        elif no_attach:
            stages["attachmentSkipped"] = True
            degraded.append("附件未自动上传（用户已同意 --no-attach，需手动上传 zip）")
            warnings.append("按 --no-attach 未自动上传附件：请让用户手动上传后再确认收尾结果")
            bullet("[2/5] 按 --no-attach 跳过附件上传（用户已同意/需手动上传）")
        else:
            upload_response = ctx.session.upload_task_file(task_id, zip_path)
            refreshed = ctx.session.get_task(task_id)
            matched = _match_attachment_file(_task_files(refreshed), zip_name)
            if matched is None:
                raise ZenTaoError(
                    "v2 附件上传接口已返回，但回读任务 files[] 未找到该附件；"
                    "请到禅道确认（必要时手动上传）后加 --no-attach 重跑 finish"
                )
            delivery["attachmentFileId"] = matched.get("id")
            delivery["attachmentUrl"] = _as_str(matched.get("url"))
            delivery["uploadResponse"] = upload_response
            stages["attachmentUploaded"] = True
            bullet(f"[2/5] 附件已上传：id={matched.get('id')} {_as_str(matched.get('url'))}")

        # 3) 独立备注（v2 /actions/comment；回读 actions 校验；失败降级并入 finish comment）
        before_actions = len(_task_actions(task_now))
        try:
            comment_response = ctx.session.comment_task(task_id, comment_html)
            refreshed = ctx.session.get_task(task_id)
            actions = _task_actions(refreshed)
            matched_action = None
            for item in actions:
                comment_text = _as_str(
                    item.get("comment") or item.get("actioncomment") or item.get("extra")
                )
                if SUMMARY_COMMENT_MARKER in comment_text:
                    matched_action = item
            if matched_action is None:
                raise ZenTaoError(
                    "v2 独立备注提交接口已返回，但回读任务 actions[] 未找到总结备注；"
                    "请到禅道确认（必要时手动粘贴 summary）"
                )
            action_id = matched_action.get("id") or comment_response.get("id")
            delivery["commentActionId"] = action_id
            stages["commentAdded"] = True
            bullet(
                f"[3/5] 独立备注已提交并回读校验：action id={action_id}"
                f"（tasks actions {before_actions}→{len(actions)}）"
            )
        except FlowError as exc:
            stages["commentDegraded"] = True
            degraded.append(f"v2 独立备注提交失败，summary 已并入 finish comment：{exc.message}")
            warnings.append(degraded[-1])
            warn(degraded[-1])

        # 4) finish（完成任务 + 指派回）
        finish_payload: Dict[str, Any] = {
            "currentConsumed": consumed,
            "realStarted": real_started,
            "finishedDate": finished_date,
            "assignedTo": assign_back,
            "comment": short_comment if stages["commentAdded"] else comment_html,
        }
        ctx.session.finish_task(task_id, finish_payload)
        stages["finishExecuted"] = True
        final_task = ctx.session.get_task(task_id)
        final_status = _as_str(final_task.get("status"))
        if final_status != "done":
            raise ZenTaoError(
                f"finish 接口已返回，但回读任务状态为 {final_status or '未知'}（期望 done）；"
                "请到禅道核对任务状态，勿重复收尾"
            )
        final_assignee = account_of(final_task.get("assignedTo"))
        if final_assignee and final_assignee.lower() != assign_back.lower():
            raise ZenTaoError(
                f"finish 后回读任务指派为 {final_assignee}（期望 {assign_back}），"
                "指派未生效；请到禅道手动处理（不再单独调用指派接口）"
            )
        final_files = _task_files(final_task)
        if stages["attachmentUploaded"] and _match_attachment_file(final_files, zip_name) is None:
            raise ZenTaoError(
                "finish 后回读任务 files[] 未找到附件；请到禅道核对附件是否已上传"
            )
        final_actions = _task_actions(final_task)
        if not any(_as_str(item.get("action")) == "finished" for item in final_actions):
            raise ZenTaoError(
                "finish 后回读任务 actions[] 未找到 finished 动作；请到禅道核对任务状态流转"
            )
        if stages["commentAdded"] and not any(
            _as_str(item.get("action")) == "commented" for item in final_actions
        ):
            raise ZenTaoError(
                "finish 后回读任务 actions[] 未找到独立备注（commented）动作；请到禅道核对"
            )
        delivery["finalActions"] = {
            "finished": True,
            "commented": any(
                _as_str(item.get("action")) == "commented" for item in final_actions
            ),
        }
        delivery["finalTaskStatus"] = final_status
        delivery["finalAssignedTo"] = final_assignee
        delivery["finishedAt"] = now_str()
        bullet(
            f"[4/5] finish 已执行并回读校验：status={final_status}；assignedTo={final_assignee or '未返回'}"
        )
    except FlowError as exc:
        for item in warnings:
            _append_warning(context, item)
        delivery.update(
            {
                "stages": stages,
                "degraded": degraded,
                "lastError": {"exitCode": exc.exit_code, "message": exc.message},
                "failedAt": now_str(),
            }
        )
        context["delivery"] = delivery
        context.setdefault("timestamps", {})["updatedAt"] = now_str()
        try:
            ctx.workspace.save(context)
        except OSError:
            pass
        if args.json:
            emit_json(
                _finish_report_payload(
                    mode="publish",
                    ok=False,
                    ctx=ctx,
                    task=task,
                    context=context,
                    assign_back=assign_back,
                    assign_back_source=assign_back_source,
                    consumed=consumed,
                    real_started=real_started,
                    finished_date=finished_date,
                    delivery=delivery,
                    stages=stages,
                    warnings=warnings,
                    login=status,
                    error=exc,
                )
            )
        CONSOLE.error("")
        CONSOLE.error(f"收尾中断：{exc.message}")
        CONSOLE.error(
            "已完成阶段："
            + "；".join(
                f"{name}={('是' if done else '否')}"
                for name, done in (
                    ("zip 已生成", stages["zipGenerated"]),
                    ("附件已上传/复用", stages["attachmentUploaded"]),
                    ("独立备注已提交", stages["commentAdded"]),
                    ("finish 已执行", stages["finishExecuted"]),
                )
            )
        )
        CONSOLE.error(
            "未声称完成：请在修复问题后重跑 finish（zip/同名附件幂等，不会重复上传）；"
            "如附件已手动上传，可加 --no-attach 重试。"
        )
        raise

    # 5) 更新 context + 报告
    delivery.update(
        {
            "stages": stages,
            "degraded": degraded,
            "finishPayload": {
                key: value for key, value in {
                    "currentConsumed": consumed,
                    "realStarted": real_started,
                    "finishedDate": finished_date,
                    "assignedTo": assign_back,
                }.items()
            },
            "commentMode": "independent" if stages["commentAdded"] else "degraded_finish_comment",
        }
    )
    context["delivery"] = delivery
    for item in warnings:
        _append_warning(context, item)
    _set_status(context, "finished", "finishedAt")
    ctx.workspace.save(context)
    bullet(f"[5/5] context 已更新：{ctx.workspace.context_path}（status=finished）")

    report = _finish_report_payload(
        mode="publish",
        ok=True,
        ctx=ctx,
        task=task,
        context=context,
        assign_back=assign_back,
        assign_back_source=assign_back_source,
        consumed=consumed,
        real_started=real_started,
        finished_date=finished_date,
        delivery=delivery,
        stages=stages,
        warnings=warnings,
        login=status,
    )
    if args.json:
        emit_json(report)
        return 0
    step(6, 6, "收尾完成报告")
    bullet(f"任务：#{task_id} {_as_str(task.get('name'))}（状态：{delivery.get('finalTaskStatus', 'done')}）")
    bullet(f"任务链接：{task_link(ctx.credentials.base_url if ctx.credentials else '', task_id)}")
    bullet(
        f"附件：{zip_name}（{human_size(int(delivery.get('zipSize') or 0))}，"
        f"id={delivery.get('attachmentFileId') or '未上传'}）"
    )
    if stages["attachmentSkipped"] and no_attach:
        bullet("附件上传：未自动上传（用户已同意 --no-attach；需手动上传 zip）")
    bullet(
        "独立备注："
        + (
            f"已提交（action id={delivery.get('commentActionId')}）"
            if stages["commentAdded"]
            else "已降级（summary 并入 finish comment）"
        )
    )
    bullet(f"指派回：{assign_back}（来源：{assign_back_source}）")
    bullet(f"消耗工时：{consumed}；realStarted：{real_started}；finishedDate：{finished_date}")
    bullet(f"zip sha256：{delivery.get('zipSha256')}")
    if degraded:
        for item in degraded:
            warn(item)
    for item in warnings:
        warn(item)
    say("")
    say("收尾完成：任务已完成并指派回需求创建人（或任务创建人兜底）。")
    return 0



# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_credential_args(parser: argparse.ArgumentParser, *, no_remember: bool = False) -> None:
    parser.add_argument(
        "--config",
        help="用户级禅道配置文件路径（默认 %%USERPROFILE%%\\.dev-skills\\zentao-dev-task-flow\\zentao.json）",
    )
    parser.add_argument("--base-url", help=f"禅道地址（默认 {DEFAULT_BASE_URL}）")
    parser.add_argument("--username", help="禅道用户名（首次使用由用户提供）")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="从 stdin 读取密码（避免进入命令历史；也可用环境变量 ZENTAO_PASSWORD）",
    )
    parser.add_argument(
        "--reuse",
        action="store_true",
        help="复用上次保存的配置（地址/账号密码/上次任务/工作区）：必须先向用户确认",
    )
    if no_remember:
        parser.add_argument(
            "--no-remember", action="store_true", help="本次不记住密码（其他非敏感配置仍保存）"
        )
    parser.add_argument(
        "--timeout", type=int, default=HTTP_TIMEOUT_DEFAULT, help=f"HTTP 超时秒数（默认 {HTTP_TIMEOUT_DEFAULT}）"
    )
    parser.add_argument("--skillenv", help=".skillenv 路径（默认自动发现）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（stdout 只输出一个 JSON 对象）")


def _add_workspace_args(
    parser: argparse.ArgumentParser, *, required_task_id: bool = False
) -> None:
    parser.add_argument(
        "--task-id",
        required=required_task_id,
        help="禅道任务 id（工作区定位；默认工作区 .zentao-dev-task/<task-id>）",
    )
    parser.add_argument(
        "--workspace",
        help="任务工作区目录（默认 .zentao-dev-task/<task-id>；与 --task-id 二选一即可）",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zentao_dev_task.py",
        description=(
            "禅道研发任务执行与收尾：init 接入 → prepare 环境/仓库 → feasibility 可行性 → "
            "plan/approve-plan → start 开发 → accept 用户验收 → finish 附件/备注/完成/指派回；"
            "prereq 创建前置任务。所有禅道写操作要求 dry-run 计划 + 用户确认（--confirm）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python zentao_dev_task.py init --task-id 123 --username admin --password-stdin\n"
            "  python zentao_dev_task.py status --task-id 123\n"
            "  python zentao_dev_task.py prereq --assignee zhangsan --dry-run\n"
            "  python zentao_dev_task.py prepare --repo-path D:/work/repo --branch main\n"
            "  python zentao_dev_task.py feasibility --report-file feasibility.md --conclusion feasible\n"
            "  python zentao_dev_task.py plan --plan-file dev-plan.md\n"
            "  python zentao_dev_task.py approve-plan --confirm\n"
            "  python zentao_dev_task.py start\n"
            "  python zentao_dev_task.py accept --pass --evidence-file acceptance-evidence.md\n"
            "  python zentao_dev_task.py finish --dry-run\n"
            "  python zentao_dev_task.py finish --confirm --reuse\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {SCRIPT_VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ---- init ----
    init = subparsers.add_parser("init", help="接入禅道任务并创建本地工作区/context（只读禅道）")
    _add_credential_args(init, no_remember=True)
    _add_workspace_args(init, required_task_id=True)
    init.add_argument("--force", action="store_true", help="重置本地状态机（必须配合 --confirm）")
    init.add_argument("--confirm", action="store_true", help="确认执行 --force 重置")
    init.set_defaults(func=cmd_init)

    # ---- prepare ----
    prepare = subparsers.add_parser("prepare", help="收集并校验研发环境与 Git 仓库事实（不写禅道）")
    _add_credential_args(prepare)
    _add_workspace_args(prepare)
    prepare.add_argument("--repo-path", required=True, help="本地 Git 仓库路径")
    prepare.add_argument("--repo-url", help="仓库地址（默认取 origin remote）")
    prepare.add_argument("--branch", help="分支（默认当前分支）")
    prepare.add_argument("--build-cmd", help="构建命令")
    prepare.add_argument("--test-cmd", help="测试命令")
    prepare.add_argument("--runtime", help='运行时环境，如 "JDK 21 + Maven 3.9 + MySQL 8"')
    prepare.add_argument("--env-note", action="append", default=[], help="环境说明（可重复）")
    prepare.add_argument(
        "--docs-dir", action="append", default=[], help="过程文档目录（可重复；默认 workspace/documents）"
    )
    prepare.add_argument("--allow-dirty", action="store_true", help="记录并接受 dirty 工作树")
    prepare.add_argument(
        "--detect-tools",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="探测本机工具链版本（默认开启；--no-detect-tools 关闭）",
    )
    prepare.set_defaults(func=cmd_prepare)

    # ---- prereq ----
    prereq = subparsers.add_parser("prereq", help="创建前置任务并指派协同人员（dry-run → --confirm）")
    _add_credential_args(prereq, no_remember=True)
    _add_workspace_args(prereq)
    prereq.add_argument("--assignee", required=True, help="前置任务指派账号（必填）")
    prereq.add_argument("--name", help="前置任务名称（默认 [前置] <当前任务名>）")
    prereq.add_argument("--desc-file", help="前置条件说明文件（默认自动生成）")
    prereq.add_argument("--type", help=f"任务类型（默认 {DEFAULT_TASK_TYPE}）")
    prereq.add_argument("--priority", help=f"优先级 P0~P3（默认 {DEFAULT_PRIORITY}）")
    prereq.add_argument("--estimate", type=float, help="预计工时")
    prereq.add_argument("--est-started", help="预计开始日期 YYYY-MM-DD（默认沿用当前任务）")
    prereq.add_argument("--deadline", help="截止日期 YYYY-MM-DD（默认沿用当前任务）")
    prereq.add_argument("--story", help="关联需求 id（默认沿用当前任务）")
    prereq.add_argument("--module", help="模块 id（默认沿用当前任务）")
    prereq.add_argument(
        "--notify",
        dest="notify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="创建后使用 v2 comment 在当前任务下留言通知（失败降级告警；默认开启，--no-notify 关闭）",
    )
    prereq_mode = prereq.add_mutually_exclusive_group()
    prereq_mode.add_argument("--dry-run", action="store_true", help="只展示计划，不写禅道")
    prereq_mode.add_argument("--confirm", action="store_true", help="用户已确认计划（真实创建的必要条件）")
    prereq.set_defaults(func=cmd_prereq)

    # ---- feasibility ----
    feasibility = subparsers.add_parser("feasibility", help="记录可行性分析结论（仅本地）")
    _add_credential_args(feasibility)
    _add_workspace_args(feasibility)
    feasibility.add_argument("--report-file", required=True, help="可行性分析 Markdown 文件（agent 撰写）")
    feasibility.add_argument(
        "--conclusion", required=True, choices=("feasible", "infeasible"), help="可行性结论"
    )
    feasibility.add_argument("--blocker", action="append", default=[], help="阻塞项（可重复）")
    feasibility.set_defaults(func=cmd_feasibility)

    # ---- plan ----
    plan = subparsers.add_parser("plan", help="记录开发计划（仅本地）")
    _add_credential_args(plan)
    _add_workspace_args(plan)
    plan.add_argument("--plan-file", required=True, help="开发计划 Markdown 文件（agent 撰写）")
    plan.set_defaults(func=cmd_plan)

    # ---- approve-plan ----
    approve = subparsers.add_parser("approve-plan", help="用户批准开发计划（唯一执行门槛，需 --confirm）")
    _add_credential_args(approve)
    _add_workspace_args(approve)
    approve.add_argument("--confirm", action="store_true", help="用户已明确审核通过开发计划")
    approve.set_defaults(func=cmd_approve_plan)

    # ---- start ----
    start = subparsers.add_parser("start", help="开始开发并同步禅道任务状态（wait → doing）")
    _add_credential_args(start, no_remember=True)
    _add_workspace_args(start)
    start.add_argument("--no-sync-status", action="store_true", help="只记录本地开始，不修改禅道状态")
    start.set_defaults(func=cmd_start)

    # ---- accept ----
    accept = subparsers.add_parser("accept", help="记录用户验收结论（--pass 需证据文件 / --fail 需原因）")
    _add_credential_args(accept)
    _add_workspace_args(accept)
    accept_result = accept.add_mutually_exclusive_group(required=True)
    accept_result.add_argument("--pass", dest="accept_pass", action="store_true", help="验收通过（必须提供 --evidence-file）")
    accept_result.add_argument("--fail", dest="fail_flag", action="store_true", help="验收未通过（必须提供 --reason）")
    accept.add_argument("--evidence-file", help="验收证据 Markdown 文件（--pass 必填，存在且非空）")
    accept.add_argument("--notes", help="验收备注")
    accept.add_argument("--reason", help="未通过原因（--fail 必填）")
    accept.set_defaults(func=cmd_accept)

    # ---- finish ----
    finish = subparsers.add_parser("finish", help="收尾：zip 附件 + 独立备注 + 完成任务 + 指派回需求创建人")
    _add_credential_args(finish, no_remember=True)
    _add_workspace_args(finish)
    finish.add_argument("--consumed", type=float, help="实际消耗工时（缺省按任务 estimate，否则 1.0 并告警）")
    finish.add_argument("--real-started", help="实际开始时间（默认 context.startedAt / 任务 realStarted / 现在）")
    finish.add_argument("--finished-date", help="完成时间（默认现在）")
    finish.add_argument("--assign-back", help="收尾指派回账号（默认需求创建人 → 任务创建人兜底）")
    finish.add_argument("--doc-dir", action="append", default=[], help="附加文档目录（可重复）")
    finish.add_argument("--extra-file", action="append", default=[], help="附加文件（可重复）")
    finish.add_argument("--zip-name", help="zip 文件名（默认 <taskId>-delivery-<时间戳>.zip）")
    finish.add_argument("--no-attach", action="store_true", help="不自动上传附件（用户已同意/手动上传）")
    finish_mode = finish.add_mutually_exclusive_group()
    finish_mode.add_argument("--dry-run", action="store_true", help="只展示收尾计划，不生成 zip、不写禅道")
    finish_mode.add_argument("--confirm", action="store_true", help="用户已确认收尾计划（真实收尾的必要条件）")
    finish.set_defaults(func=cmd_finish)

    # ---- status ----
    status = subparsers.add_parser("status", help="查看本地阶段、禅道任务状态与下一步建议（只读）")
    _add_credential_args(status)
    _add_workspace_args(status)
    status.set_defaults(func=cmd_status)

    # ---- config-show / config-forget ----
    show = subparsers.add_parser("config-show", help="查看已保存配置（密码只显示保存方式，不回显）")
    show.add_argument("--config", help="用户级禅道配置文件路径")
    show.add_argument("--json", action="store_true", help="输出 JSON")
    show.set_defaults(func=cmd_config_show)

    forget = subparsers.add_parser("config-forget", help="删除已保存的禅道配置（含密码）")
    forget.add_argument("--config", help="用户级禅道配置文件路径")
    forget.add_argument("--yes", action="store_true", help="确认删除")
    forget.add_argument("--json", action="store_true", help="输出 JSON")
    forget.set_defaults(func=cmd_config_forget)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    CONSOLE.set_json_mode(bool(getattr(args, "json", False)))
    debug = os.environ.get(DEBUG_ENV) == "1"
    try:
        return int(args.func(args))
    except FlowError as exc:
        if CONSOLE.json_mode and not CONSOLE.json_emitted:
            CONSOLE.emit_json(
                {
                    "ok": False,
                    "error": {
                        "exitCode": exc.exit_code,
                        "message": exc.message,
                        "detail": exc.detail,
                    },
                }
            )
        CONSOLE.error("")
        CONSOLE.error(f"错误：{exc.message}")
        if exc.detail:
            CONSOLE.error(f"位置：{exc.detail}")
        if exc.hint:
            CONSOLE.error("")
            CONSOLE.error(f"提示：{exc.hint}")
        if debug:
            raise
        return exc.exit_code
    except KeyboardInterrupt:
        CONSOLE.error("\n已中断：未完成的禅道操作请按上面的步骤输出判断。")
        return 1
    except Exception as exc:  # noqa: BLE001
        CONSOLE.error(f"错误：未预期异常 {type(exc).__name__}: {exc}")
        if debug:
            raise
        CONSOLE.error(
            f"提示：设置环境变量 {DEBUG_ENV}=1 可查看堆栈；提交问题时请附脱敏日志。"
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
