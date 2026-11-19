#!/usr/bin/env python3
"""初始化一个禅道工作看板工作区，并做连接自检。

做四件事：
  1. 把采集引擎模板复制到目标目录；
  2. 写入 config.json（含明文密码，unix 下自动 chmod 600）；
  3. 跑一次 dry-run 验证登录，失败立即报错（不写 state.json）；
  4. 跑一次正式采集，生成首版 dashboard.html。

用法：
    python init_workspace.py --base-url http://zentao.example.com:8080 \
        --account alice --password '******' [--assignee alice] \
        [--out-dir ./zentao-dashboard] [--api-version auto] \
        [--categories requirement,story,task,bug] [--force] [--skip-fetch] [--json]

注意：密码不会打印、不写入日志；如不希望密码出现在 shell history，改用环境变量
      ZENTAO_PASSWORD 传入（优先于 --password）。
"""
import argparse
import json
import os
import shutil
import stat
import subprocess
import sys

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(SKILL_DIR, "assets")
ENGINE = "zentao_my_dashboard.py"
COPY_FILES = [ENGINE, "make_demo.py", "test_filter.js", "test_engine.py", "watch_loop.py"]


def log(msg):
    print(msg, flush=True)


def run_engine(out_dir, args):
    cmd = [sys.executable, os.path.join(out_dir, ENGINE)] + args
    proc = subprocess.run(cmd, cwd=out_dir, capture_output=True, text=True, encoding="utf-8")
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


def last_json(text):
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def main():
    ap = argparse.ArgumentParser(description="初始化禅道工作看板工作区")
    ap.add_argument("--base-url", required=True, help="禅道地址，如 http://192.168.1.10:8080")
    ap.add_argument("--account", required=True)
    ap.add_argument("--password", default=os.environ.get("ZENTAO_PASSWORD", ""),
                    help="禅道密码；可用环境变量 ZENTAO_PASSWORD 代替，避免出现在 shell history")
    ap.add_argument("--assignee", default="", help="被指派人账号，默认与 --account 相同")
    ap.add_argument("--out-dir", default=os.path.join(os.getcwd(), "zentao-dashboard"))
    ap.add_argument("--api-version", default="auto", choices=["auto", "v1", "v2"])
    ap.add_argument("--categories", default="requirement,story,task,bug",
                    help="要收集的类别，逗号分隔：requirement,story,task,bug")
    ap.add_argument("--force", action="store_true", help="目标目录已存在时覆盖 config.json 与脚本")
    ap.add_argument("--skip-fetch", action="store_true", help="只初始化与自检，不生成看板")
    ap.add_argument("--json", action="store_true", help="最后输出一行结构化 JSON")
    args = ap.parse_args()

    if not args.password:
        raise SystemExit("缺少密码：传 --password 或设置环境变量 ZENTAO_PASSWORD")

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    config_path = os.path.join(out_dir, "config.json")
    if os.path.exists(config_path) and not args.force:
        raise SystemExit(f"{config_path} 已存在。确认要覆盖请加 --force")

    # 1. 复制模板
    copied = []
    for name in COPY_FILES:
        src = os.path.join(ASSETS, name)
        if not os.path.exists(src):
            raise SystemExit(f"模板缺失：{src}")
        dst = os.path.join(out_dir, name)
        if os.path.exists(dst) and args.force:
            shutil.copy2(src, dst)
            copied.append(name)
        elif not os.path.exists(dst):
            shutil.copy2(src, dst)
            copied.append(name)
    example = os.path.join(out_dir, "config.example.json")
    if not os.path.exists(example):
        shutil.copy2(os.path.join(ASSETS, "config.example.json"), example)

    # 2. 写配置
    cats = [c.strip() for c in args.categories.split(",") if c.strip()]
    valid = {"requirement", "story", "task", "bug"}
    bad = [c for c in cats if c not in valid]
    if bad:
        raise SystemExit(f"--categories 含未知类别：{bad}（可选 {sorted(valid)}）")
    conf = {
        "base_url": args.base_url.rstrip("/"),
        "account": args.account,
        "password": args.password,
        "assignee": args.assignee or args.account,
        "api_version": args.api_version,
        "categories": cats,
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(config_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600，非 Windows 生效
    except OSError:
        pass
    log(f"[1/4] 模板已就绪：{out_dir}（{len(copied)} 个文件）")
    log(f"[2/4] 配置已写入 config.json（assignee={conf['assignee']}，类别={','.join(cats)}）")

    # 3. dry-run 自检
    code, out, err = run_engine(out_dir, ["--dry-run", "--json"])
    info = last_json(out)
    if code != 0 or not info or not info.get("ok"):
        log("[3/4] 连接自检失败")
        detail = (out or "") + (err or "")
        for line in detail.strip().splitlines()[-6:]:
            log("      " + line)
        if args.json:
            print(json.dumps({"ok": False, "stage": "dry-run", "error": detail[-800:]}, ensure_ascii=False))
        raise SystemExit(1)
    log(f"[3/4] 连接自检通过（API {info.get('api')}）：共 {info.get('total')} 条，"
        + "，".join(f"{k} {v}" for k, v in (info.get("counts") or {}).items()))
    for w in info.get("warnings") or []:
        log("      告警：" + w)

    # 4. 正式采集
    result = None
    if args.skip_fetch:
        log("[4/4] 已跳过首次采集（--skip-fetch）")
    else:
        code, out, err = run_engine(out_dir, ["--json"])
        result = last_json(out)
        if code != 0 or not result or not result.get("ok"):
            log("[4/4] 首次采集失败")
            for line in ((out or "") + (err or "")).strip().splitlines()[-6:]:
                log("      " + line)
            if args.json:
                print(json.dumps({"ok": False, "stage": "fetch"}, ensure_ascii=False))
            raise SystemExit(1)
        log(f"[4/4] 首版看板已生成：{result.get('dashboard')}"
            f"（共 {result.get('total')} 条，初始 {result.get('new_count')} 条记为基线）")

    log("")
    log("下一步：创建定时任务（不同 agent 的目标方式见 references/agents.md）")
    log(f"  手动跑：cd \"{out_dir}\" && python {ENGINE} --json")
    log("  看样式：python make_demo.py 生成 demo.html")
    log("  防回归：node test_filter.js")

    if args.json:
        print(json.dumps({
            "ok": True,
            "out_dir": out_dir,
            "engine": os.path.join(out_dir, ENGINE),
            "config": config_path,
            "dashboard": (result or {}).get("dashboard"),
            "api": (result or info).get("api"),
            "total": (result or info).get("total"),
            "counts": (result or info).get("counts"),
            "warnings": (result or info).get("warnings") or [],
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
