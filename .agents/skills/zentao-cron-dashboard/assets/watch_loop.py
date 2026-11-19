#!/usr/bin/env python3
"""无需系统计划任务时的常驻兜底：按固定间隔反复采集，把新增条目写进 notify.log。

Windows 上 schtasks 可能被安全策略禁用、Linux 上可能没有 crontab 权限时用这个。

用法：
    python watch_loop.py --interval 60          # 每 60 分钟跑一次，常驻
    python watch_loop.py --interval 60 --once   # 只跑一次（相当于手动执行）
    python watch_loop.py --interval 30 --exit-on-error   # 首次失败即退出（便于外部守护）

新增条目会同时打到 stdout 和 notify.log；dashboard.html 由引擎自己覆盖更新。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "zentao_my_dashboard.py")
NOTIFY = os.path.join(HERE, "notify.log")


def stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def emit(line):
    print(line, flush=True)
    try:
        with open(NOTIFY, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run_once():
    proc = subprocess.run([sys.executable, ENGINE, "--json"],
                          cwd=HERE, capture_output=True, text=True, encoding="utf-8")
    out = proc.stdout or ""
    data = None
    for line in reversed(out.strip().splitlines()):
        if line.strip().startswith("{"):
            try:
                data = json.loads(line.strip())
                break
            except json.JSONDecodeError:
                continue
    if proc.returncode != 0 or not data or not data.get("ok"):
        emit(f"[{stamp()}] 采集失败：{(out + (proc.stderr or '')).strip()[-400:]}")
        return False

    counts = "，".join(f"{k} {v}" for k, v in (data.get("counts") or {}).items())
    if data.get("new_count"):
        emit(f"[{stamp()}] 新增 {data['new_count']} 条（当前共 {data.get('total')} 条：{counts}）")
        for n in data["new"]:
            place = n["project"] + (f" / {n['execution']}" if n.get("execution") else "")
            emit(f"    [{n['kind']}] #{n['id']} {n['title']}（{place}，{n['status']}，"
                 f"截止 {n.get('deadline') or '—'}）{n.get('url','')}")
    else:
        emit(f"[{stamp()}] 无新增（当前共 {data.get('total')} 条：{counts}）")
    for w in data.get("warnings") or []:
        emit(f"    告警：{w}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60, help="轮询间隔（分钟，最小 1）")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--exit-on-error", action="store_true")
    args = ap.parse_args()

    interval = max(1, args.interval) * 60
    while True:
        ok = run_once()
        if args.once:
            return 0 if ok else 1
        if not ok and args.exit_on_error:
            return 1
        time.sleep(interval if ok else min(300, interval))


if __name__ == "__main__":
    raise SystemExit(main())
