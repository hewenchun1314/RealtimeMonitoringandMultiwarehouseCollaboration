#!/usr/bin/env python3
"""禅道「指派给我」工作看板采集引擎。

拉取指派给指定用户的需求 / 研发需求 / 任务 / Bug，生成可过滤的静态看板，并标出新增项。
纯标准库实现，不依赖任何第三方包，可独立运行（不需要本 skill 的其他文件）。

用法：
    python zentao_my_dashboard.py             # 拉取 + 生成看板
    python zentao_my_dashboard.py --json      # 额外输出一行结构化 JSON（供调度器/agent 读取）
    python zentao_my_dashboard.py --dry-run   # 只登录并统计，不写文件、不动 state.json

同目录文件：
    config.json      配置（本脚本唯一输入）
    dashboard.html   看板产物
    state.json       已见条目 ID，用于判断新增
    run.log          运行日志
"""
import argparse
import html
import json
import os
import urllib.error
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CONF_FILE = os.path.join(HERE, "config.json")
STATE_FILE = os.path.join(HERE, "state.json")
OUT_FILE = os.path.join(HERE, "dashboard.html")
LOG_FILE = os.path.join(HERE, "run.log")

TASK_STATUS = {
    "wait": "未开始", "doing": "进行中", "done": "已完成", "closed": "已关闭",
    "cancel": "已取消", "pause": "已暂停", "": "—",
}
STORY_STATUS = {
    "draft": "草稿", "reviewing": "待评审", "active": "激活", "closed": "已关闭",
    "changed": "已变更", "": "—",
}
STORY_STAGE = {
    "wait": "未开始", "planned": "已计划", "projected": "已立项", "developing": "研发中",
    "testing": "测试中", "verified": "已验收", "released": "已发布", "developed": "已研发",
    "": "—",
}
BUG_STATUS = {"active": "激活", "resolved": "已解决", "closed": "已关闭", "": "—"}
PRI_LABEL = {"1": "1 紧急", "2": "2 高", "3": "3 中", "4": "4 低"}
SEV_LABEL = {"1": "1 致命", "2": "2 严重", "3": "3 一般", "4": "4 轻微"}

ALL_CATS = [("requirement", "需求"), ("story", "研发需求"), ("task", "任务"), ("bug", "Bug")]
CAT_LABEL = dict(ALL_CATS)
NO_PROJECT = "未归属项目"


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # 只读目录等场景下不因日志写失败而中断


def load_conf():
    if not os.path.exists(CONF_FILE):
        raise SystemExit(f"找不到配置文件：{CONF_FILE}（可参考 config.example.json）")
    with open(CONF_FILE, "r", encoding="utf-8") as f:
        conf = json.load(f)
    for key in ("base_url", "account", "password"):
        if not conf.get(key):
            raise SystemExit(f"config.json 缺少必填项：{key}")
    conf.setdefault("assignee", conf["account"])
    conf.setdefault("api_version", "auto")
    conf.setdefault("categories", [k for k, _ in ALL_CATS])
    return conf


def _json(raw):
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", 0, "0"):
            return v
    return ""


def account_of(value):
    """取指派人账号。v1 返回对象 {id, account, realname}，v2 返回字符串，两种都兼容。"""
    if isinstance(value, dict):
        return value.get("account") or ""
    return value or ""


class Zt:
    """禅道 API 客户端。自动在 v2 / v1 之间选择可用的接口前缀。

    v2（禅道 18+）：/api.php/v2，支持 /my/* 直接取「我的」数据
    v1（禅道 12~17）：/api.php/v1，无 /my/*，只能按项目/迭代枚举后过滤
    """

    def __init__(self, base, account, password, api_version="auto"):
        self.base = base.rstrip("/")
        self.account = account
        self.password = password
        self.want = (api_version or "auto").lower()
        self.prefix = ""
        self.token = None
        self.version = ""

    # ---------- 登录 ----------
    def login(self):
        order = [("v2", "/api.php/v2"), ("v1", "/api.php/v1")]
        if self.want in ("v1", "v2"):
            order = [o for o in order if o[0] == self.want]

        errors = []
        for ver, prefix in order:
            path = f"{prefix}/users/login" if ver == "v2" else f"{prefix}/tokens"
            st, raw = self._raw_abs(path, {"account": self.account, "password": self.password})
            data = _json(raw) or {}
            # v1 /tokens 成功时返回 201，v2 /users/login 返回 200，统一按 2xx 判断
            if 200 <= st < 300 and data.get("token"):
                self.version = ver
                self.prefix = prefix
                self.token = data["token"]
                return data.get("user", {})
            errors.append(f"{ver} 登录失败（HTTP {st}）：{raw[:120]}")
        raise RuntimeError("禅道登录失败，请检查 base_url / account / password。\n  " + "\n  ".join(errors))

    # ---------- 请求 ----------
    def _raw_abs(self, path, body=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method or ("POST" if data else "GET"))
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return 0, str(e)

    def get(self, rel_path):
        """rel_path 以 / 开头，自动补接口前缀。"""
        return self._raw_abs(self.prefix + rel_path)

    def has_my_api(self):
        return self.version == "v2"


def build_lookups(zt):
    """建 id -> 名称 映射。禅道对象只回传 project / execution 的 ID，名称要自己查。"""
    projects, executions, products = {}, {}, {}
    warn = []

    st, raw = zt.get("/projects?recPerPage=200")
    plist = (_json(raw) or {}).get("projects")
    if st != 200 or not isinstance(plist, list):
        warn.append(f"项目列表拉取异常（HTTP {st}）")
        plist = []
    for p in plist:
        projects[str(p.get("id"))] = p.get("name") or f"项目#{p.get('id')}"

    for pid in list(projects.keys()):
        for suffix in ("?recPerPage=200&status=all", "?recPerPage=200"):
            st, raw = zt.get(f"/projects/{pid}/executions{suffix}")
            rows = (_json(raw) or {}).get("executions") or []
            for e in rows:
                executions[str(e.get("id"))] = {
                    "name": e.get("name") or f"迭代#{e.get('id')}",
                    "pid": str(e.get("project") or pid),
                }
            if rows:
                break

    st, raw = zt.get("/executions?recPerPage=200&status=all")
    for e in (_json(raw) or {}).get("executions") or []:
        executions.setdefault(str(e.get("id")), {
            "name": e.get("name") or f"迭代#{e.get('id')}",
            "pid": str(e.get("project") or ""),
        })

    st, raw = zt.get("/products?recPerPage=200")
    for p in (_json(raw) or {}).get("products") or []:
        products[str(p.get("id"))] = p.get("name") or f"产品#{p.get('id')}"

    return projects, executions, products, warn


def resolve_place(item, projects, executions):
    """返回 (项目ID, 项目名, 迭代名)。"""
    pid = str(first(item, "project") or "")
    eid = str(first(item, "execution") or "")
    if (not pid or pid == "0") and eid in executions:
        pid = executions[eid]["pid"]
    pname = projects.get(pid) if pid and pid != "0" else ""
    ename = executions[eid]["name"] if eid in executions else (f"迭代#{eid}" if eid and eid != "0" else "")
    if not pid or pid == "0":
        return "0", NO_PROJECT, ename
    return pid, pname or f"项目#{pid}", ename


def normalize(kind, item, base, projects, executions):
    iid = item.get("id") or 0
    pid, pname, ename = resolve_place(item, projects, executions)

    if kind == "task":
        title = item.get("name", "")
        status = TASK_STATUS.get(item.get("status", ""), item.get("status", ""))
        extra = f"工时 预计{item.get('estimate', '-')} / 已耗{item.get('consumed', '-')} / 剩余{item.get('left', '-')}"
        url = f"{base}/index.php?m=task&f=view&taskID={iid}"
        pri, sev = item.get("pri"), None
    elif kind == "bug":
        title = item.get("title", "")
        status = BUG_STATUS.get(item.get("status", ""), item.get("status", ""))
        extra = f"影响版本 {item.get('openedBuild', '-')}"
        url = f"{base}/index.php?m=bug&f=view&bugID={iid}"
        pri, sev = item.get("pri"), item.get("severity")
    elif kind == "story":
        title = item.get("title", "")
        status = STORY_STATUS.get(item.get("status", ""), item.get("status", ""))
        stage = STORY_STAGE.get(item.get("stage", ""), item.get("stage", ""))
        if stage and stage != "—":
            status = f"{status} / {stage}"
        extra = f"预计 {item.get('estimate', '-')}h"
        url = f"{base}/index.php?m=story&f=view&storyID={iid}"
        pri, sev = item.get("pri"), None
    else:  # requirement
        title = first(item, "title", "name")
        status = STORY_STATUS.get(item.get("status", ""), item.get("status", ""))
        extra = ""
        url = f"{base}/index.php?m=requirement&f=view&requirementID={iid}"
        pri, sev = item.get("pri"), None

    return {
        "kind": kind,
        "id": str(iid),
        "title": title,
        "status": status or "—",
        "pri": PRI_LABEL.get(str(pri), str(pri) if pri else "—"),
        "pri_raw": _int(pri),
        "severity": SEV_LABEL.get(str(sev), "") if sev else "",
        "deadline": item.get("deadline", "") or "",
        "pid": pid,
        "project": pname,
        "execution": ename or "",
        "extra": extra,
        "assignedTo": account_of(item.get("assignedTo")),
        "updated": first(item, "lastEditedDate", "editedDate", "openedDate", "assignedDate"),
        "url": url,
    }


def _int(v):
    try:
        return int(v)
    except Exception:  # noqa: BLE001
        return 99


def fetch_mine(zt, account, projects, executions, products, cats):
    """v2 优先用 /my/*；随后一律按项目/迭代/产品枚举兜底并合并去重。

    注意：/my/* 返回的需求常常没有 project 字段（产品维度而来），枚举路径才有。
    因此同一 ID 出现多次时按「信息更全」合并，而不是简单丢弃后者。
    """
    bucket, warn = {}, []

    def push(kind, row):
        n = normalize(kind, row, zt.base, projects, executions)
        if n["id"] in ("0", ""):
            return
        key = (kind, n["id"])
        old = bucket.get(key)
        if old is None:
            bucket[key] = n
            return
        if old["pid"] == "0" and n["pid"] != "0":
            old["pid"], old["project"] = n["pid"], n["project"]
        if not old["execution"] and n["execution"]:
            old["execution"] = n["execution"]
        for f in ("deadline", "status", "updated", "extra", "severity"):
            if not old.get(f) and n.get(f):
                old[f] = n[f]
        if not old["title"]:
            old["title"] = n["title"]

    if zt.has_my_api():
        for kind, path, key in [
            ("requirement", "/my/requirements?recPerPage=200", "requirements"),
            ("story", "/my/stories?recPerPage=200", "stories"),
            ("task", "/my/tasks?recPerPage=200", "tasks"),
            ("bug", "/my/bugs?recPerPage=200", "bugs"),
        ]:
            if kind not in cats:
                continue
            st, raw = zt.get(path)
            rows = (_json(raw) or {}).get(key)
            if st != 200 or not isinstance(rows, list):
                warn.append(f"{path} 返回异常（HTTP {st}），改用枚举兜底")
                continue
            for r in rows:
                push(kind, r)

    for pid in projects:
        if "story" in cats:
            st, raw = zt.get(f"/projects/{pid}/stories?recPerPage=200")
            for r in (_json(raw) or {}).get("stories") or []:
                if account_of(r.get("assignedTo")) == account:
                    push("story", r)
        if "bug" in cats:
            st, raw = zt.get(f"/projects/{pid}/bugs?recPerPage=200")
            for r in (_json(raw) or {}).get("bugs") or []:
                if account_of(r.get("assignedTo")) == account:
                    push("bug", r)
        if "task" in cats:
            st, raw = zt.get(f"/projects/{pid}/executions?recPerPage=200&status=all")
            for e in (_json(raw) or {}).get("executions") or []:
                st2, raw2 = zt.get(f"/executions/{e.get('id')}/tasks?browseType=all&recPerPage=300")
                for t in (_json(raw2) or {}).get("tasks") or []:
                    if account_of(t.get("assignedTo")) == account:
                        push("task", t)

    for prod in products:
        if "bug" in cats:
            st, raw = zt.get(f"/products/{prod}/bugs?recPerPage=200")
            for r in (_json(raw) or {}).get("bugs") or []:
                if account_of(r.get("assignedTo")) == account:
                    push("bug", r)

    return list(bucket.values()), warn


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            s.setdefault("seen", {})
            return s
        except Exception:  # noqa: BLE001
            pass
    return {"seen": {}, "runs": 0, "updated": ""}


def diff_new(items, state):
    seen = state["seen"]
    return [it for it in items if it["id"] not in set(seen.get(it["kind"], []))]


def save_state(items, state):
    seen = {}
    for it in items:
        seen.setdefault(it["kind"], []).append(it["id"])
    state["seen"] = seen
    state["runs"] = int(state.get("runs", 0)) + 1
    state["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def render(items, new, warn, meta, cats):
    new_ids = {(n["kind"], n["id"]) for n in new}

    counts = {}
    for i in items:
        counts[(i["pid"], i["project"])] = counts.get((i["pid"], i["project"]), 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (kv[0][0] == "0", -kv[1], kv[0][1]))
    options = f'<option value="">全部项目（{len(items)}）</option>'
    for (pid, pname), c in ordered:
        options += f'<option value="{html.escape(pid)}">{html.escape(pname)}（{c}）</option>'

    stats = "".join(
        f'<div class="stat"><div class="stat-num">{len([i for i in items if i["kind"] == k])}</div>'
        f'<div class="stat-label">{label}</div></div>'
        for k, label in cats
    )

    def row_html(i):
        is_new = (i["kind"], i["id"]) in new_ids
        badge = '<span class="badge">NEW</span>' if is_new else ""
        sev = f'<span class="sev">{html.escape(i["severity"])}</span>' if i["severity"] else ""
        exec_cell = (f'<span class="pill alt">{html.escape(i["execution"])}</span>'
                     if i["execution"] else '<span class="dim">—</span>')
        return f"""<tr class="{'new' if is_new else ''}" data-pid="{html.escape(i['pid'])}">
      <td class="mono">{html.escape(i["id"])}</td>
      <td class="title"><a href="{html.escape(i["url"])}" target="_blank">{html.escape(i["title"])}</a>{badge}</td>
      <td class="dim">{html.escape(i["project"])}</td>
      <td>{exec_cell}</td>
      <td><span class="pill">{html.escape(i["status"])}</span></td>
      <td class="nowrap">{html.escape(i["pri"])}{sev}</td>
      <td class="nowrap">{html.escape(i["deadline"]) if i["deadline"] else "—"}</td>
      <td class="dim">{html.escape(i["extra"])}</td>
      <td class="dim mono nowrap">{html.escape(str(i["updated"])[:16])}</td>
    </tr>"""

    tables = ""
    for kind, label in cats:
        rows = [i for i in items if i["kind"] == kind]
        if not rows:
            continue
        rows.sort(key=lambda x: (x["pri_raw"], x["deadline"] or "9999"))
        body = "\n".join(row_html(r) for r in rows)
        tables += f"""
  <section class="card group">
    <h2>{label} <span class="count">{len(rows)}</span></h2>
    <div class="tablewrap"><table>
      <thead><tr><th>ID</th><th>标题</th><th>项目</th><th>迭代</th><th>状态</th><th>优先级</th><th>截止</th><th>备注</th><th>更新</th></tr></thead>
      <tbody>{body}</tbody>
    </table></div>
  </section>"""

    if not items:
        tables = """
  <section class="card empty">
    <h2>暂无可展示条目</h2>
    <p>当前账号在禅道里没有被指派的需求 / 研发需求 / 任务 / Bug。</p>
    <p class="dim">可能原因：项目还没有迭代与任务、产品下还没有需求或 Bug，或这些条目指派给了其他人。
    数据出现后，下一次定时拉取会自动填充本看板，并按项目归类。</p>
  </section>"""

    new_block = ""
    if new:
        lis = "".join(
            f'<li data-pid="{html.escape(n["pid"])}"><a href="{html.escape(n["url"])}" target="_blank">'
            f'[{CAT_LABEL[n["kind"]]}] {html.escape(n["title"])}</a>'
            f'<span class="dim"> · {html.escape(n["project"])}'
            + (f' / {html.escape(n["execution"])}' if n["execution"] else "")
            + f' · {html.escape(n["status"])} · 截止 {html.escape(n["deadline"] or "—")}</span></li>'
            for n in new
        )
        new_block = f'<section class="card newcard"><h2>本次新增 {len(new)} 条</h2><ul>{lis}</ul></section>'

    warn_block = ""
    if warn:
        warn_block = '<section class="card warn"><h2>采集告警</h2><ul>' + "".join(
            f"<li>{html.escape(w)}</li>" for w in warn) + "</ul></section>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>我的禅道工作看板</title>
<style>
  :root {{
    --bg:#f6f7f9; --card:#ffffff; --line:#e6e8ec; --text:#1f2329; --dim:#8a9099;
    --accent:#2f6feb; --new:#e5484d; --newbg:#fff5f5;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
         font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
  .wrap {{ max-width:1320px; margin:0 auto; padding:28px 20px 60px; }}
  header {{ display:flex; align-items:flex-end; justify-content:space-between; margin-bottom:18px; flex-wrap:wrap; gap:12px; }}
  h1 {{ margin:0; font-size:22px; font-weight:650; letter-spacing:.3px; }}
  .sub {{ color:var(--dim); font-size:12.5px; }}
  .stats {{ display:flex; gap:12px; margin:16px 0 14px; flex-wrap:wrap; }}
  .stat {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 20px; min-width:104px; }}
  .stat-num {{ font-size:23px; font-weight:680; }}
  .stat-label {{ color:var(--dim); font-size:12.5px; }}
  .toolbar {{ display:flex; align-items:center; gap:10px; margin:0 0 16px;
              background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 14px; flex-wrap:wrap; }}
  .toolbar label {{ color:var(--dim); font-size:13px; }}
  select {{ font:13px/1.5 inherit; padding:5px 10px; border:1px solid var(--line);
            border-radius:8px; background:#fff; color:var(--text); min-width:220px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 18px; margin-bottom:16px; }}
  .card h2 {{ font-size:15px; margin:0 0 12px; font-weight:640; display:flex; align-items:center; gap:8px; }}
  .count {{ background:#eef1f6; color:#5b6472; border-radius:20px; padding:1px 9px; font-size:12px; font-weight:500; }}
  .tablewrap {{ overflow-x:auto; }}
  table {{ width:100%; border-collapse:collapse; }}
  th {{ text-align:left; font-weight:560; color:var(--dim); font-size:12.5px; padding:6px 8px;
        border-bottom:1px solid var(--line); white-space:nowrap; }}
  td {{ padding:9px 8px; border-bottom:1px solid #f1f2f5; vertical-align:top; }}
  tr:last-child td {{ border-bottom:none; }}
  a {{ color:var(--accent); text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  .mono {{ font-family:"Cascadia Mono",Consolas,monospace; font-size:12.5px; color:#5b6472; }}
  .dim {{ color:var(--dim); font-size:12.5px; }}
  .nowrap {{ white-space:nowrap; }}
  .pill {{ background:#eef1f6; color:#445; border-radius:6px; padding:1px 8px; font-size:12px; white-space:nowrap; }}
  .pill.alt {{ background:#eef4ff; color:#2f5fbf; }}
  .sev {{ margin-left:6px; color:var(--new); font-size:12px; }}
  .badge {{ background:var(--new); color:#fff; font-size:10.5px; border-radius:4px; padding:1px 5px;
            margin-left:8px; vertical-align:middle; letter-spacing:.5px; }}
  tr.new {{ background:var(--newbg); }}
  .newcard {{ border-color:#f5c6c8; background:var(--newbg); }}
  .newcard ul, .warn ul {{ margin:0; padding-left:20px; }}
  .newcard li {{ margin:5px 0; }}
  .warn {{ border-color:#f0d9b5; background:#fffaf0; }}
  .empty p {{ margin:6px 0; }}
  footer {{ color:var(--dim); font-size:12px; margin-top:26px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>我的禅道工作看板</h1>
      <div class="sub">{html.escape(meta["user"])} · {html.escape(meta["base"])} · API {html.escape(meta["api"])}</div>
    </div>
    <div class="sub">生成于 {meta["time"]} · 第 {meta["runs"]} 次拉取 · 共 {len(items)} 条</div>
  </header>
  <div class="stats">{stats}</div>
  <div class="toolbar">
    <label for="pf">按项目过滤</label>
    <select id="pf">{options}</select>
    <span class="sub" id="pinfo"></span>
  </div>
  {new_block}
  {warn_block}
  {tables}
  <footer>数据来源：禅道 REST API（/my/* 优先，项目/迭代/产品枚举兜底）。已见条目记录在 state.json，新增项以红色 NEW 标记。</footer>
</div>
<script>
(function () {{
  var sel = document.getElementById('pf');
  var info = document.getElementById('pinfo');
  if (!sel) return;
  var sections = Array.prototype.slice.call(document.querySelectorAll('section.group'));

  function apply() {{
    var want = sel.value;
    var shownRows = 0;
    Array.prototype.forEach.call(document.querySelectorAll('[data-pid]'), function (el) {{
      var ok = !want || el.getAttribute('data-pid') === want;
      el.style.display = ok ? '' : 'none';
      if (ok && el.tagName === 'TR') shownRows++;
    }});
    sections.forEach(function (sec) {{
      var vis = 0;
      Array.prototype.forEach.call(sec.querySelectorAll('tbody tr'), function (tr) {{
        if (tr.style.display !== 'none') vis++;
      }});
      var c = sec.querySelector('.count');
      if (c) c.textContent = vis;
      sec.style.display = vis ? '' : 'none';
    }});
    if (info) info.textContent = want ? ('已筛选 ' + shownRows + ' 条') : '';
  }}

  sel.addEventListener('change', apply);
  apply();
}})();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="禅道「指派给我」工作看板")
    ap.add_argument("--json", action="store_true", help="输出一行结构化 JSON 结果")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写文件、不动 state.json")
    args = ap.parse_args()

    conf = load_conf()
    cats = [(k, label) for k, label in ALL_CATS if k in conf["categories"]]

    zt = Zt(conf["base_url"], conf["account"], conf["password"], conf["api_version"])
    user = zt.login()
    log(f"登录成功：{user.get('realname') or conf['account']}（{conf['base_url']}，API {zt.version}）")

    projects, executions, products, lw = build_lookups(zt)
    log(f"项目 {len(projects)} 个，迭代 {len(executions)} 个，产品 {len(products)} 个")

    items, warn = fetch_mine(zt, conf["assignee"], projects, executions, products, [k for k, _ in cats])
    warn = lw + warn
    for w in warn:
        log("告警：" + w)

    counts = {label: len([i for i in items if i["kind"] == k]) for k, label in cats}
    by_project = {}
    for i in items:
        by_project[i["project"]] = by_project.get(i["project"], 0) + 1

    if args.dry_run:
        log("dry-run：" + "，".join(f"{k} {v}" for k, v in counts.items()) + f"；共 {len(items)} 条")
        if args.json:
            print(json.dumps({"ok": True, "dry_run": True, "api": zt.version, "counts": counts,
                              "total": len(items), "by_project": by_project, "warnings": warn},
                             ensure_ascii=False))
        return

    state = load_state()
    new = diff_new(items, state)
    meta = {
        "user": f"{user.get('realname', '')}（{conf['account']}）",
        "base": conf["base_url"],
        "api": zt.version,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "runs": int(state.get("runs", 0)) + 1,
    }
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(render(items, new, warn, meta, cats))
    save_state(items, state)

    log("拉取完成：" + "，".join(f"{k} {v}" for k, v in counts.items())
        + f"；新增 {len(new)} 条；涉及项目 {len(by_project)} 个")
    for n in new:
        place = n["project"] + (f" / {n['execution']}" if n["execution"] else "")
        log(f"  新增 [{CAT_LABEL[n['kind']]}] #{n['id']} {n['title']}（{place}，{n['status']}，截止 {n['deadline'] or '—'}）")
    log(f"看板：{OUT_FILE}")

    if args.json:
        print(json.dumps({
            "ok": True,
            "time": meta["time"],
            "api": zt.version,
            "counts": counts,
            "total": len(items),
            "new_count": len(new),
            "by_project": by_project,
            "new": [{"kind": CAT_LABEL[n["kind"]], "id": n["id"], "title": n["title"],
                     "project": n["project"], "execution": n["execution"],
                     "status": n["status"], "deadline": n["deadline"], "url": n["url"]} for n in new],
            "warnings": warn,
            "dashboard": OUT_FILE,
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
