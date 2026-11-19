#!/usr/bin/env python3
"""采集引擎的离线单测：不连禅道，验证字段归一化、项目/迭代解析、状态与优先级映射。

改模板后先跑这个，再跑 test_filter.js：
    python test_engine.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("zd", os.path.join(HERE, "zentao_my_dashboard.py"))
zd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zd)

BASE = "http://zt.example.com"
PROJECTS = {"1": "项目甲", "4": "项目乙"}
EXECS = {"2": {"name": "R2 迭代", "pid": "1"}, "9": {"name": "Sprint 7", "pid": "4"}}

failed = []


def check(label, cond, got=None):
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}  -> {got!r}")
        failed.append(label)


# ---- 任务：有项目有迭代 ----
t = zd.normalize("task", {"id": 104, "name": "抽公共算法", "project": 1, "execution": 2,
                          "status": "doing", "pri": 2, "estimate": "8.00",
                          "consumed": "5.00", "left": "3.00", "deadline": "2026-10-27",
                          "lastEditedDate": "2026-10-26 13:40:00"}, BASE, PROJECTS, EXECS)
check("任务-项目名", t["project"] == "项目甲", t["project"])
check("任务-迭代名", t["execution"] == "R2 迭代", t["execution"])
check("任务-状态中文", t["status"] == "进行中", t["status"])
check("任务-优先级标签", t["pri"] == "2 高", t["pri"])
check("任务-链接", t["url"].endswith("m=task&f=view&taskID=104"), t["url"])
check("任务-工时备注", "预计8.00" in t["extra"], t["extra"])

# ---- 任务：只有迭代、没有 project 字段（部分版本如此），项目应由迭代反推 ----
t2 = zd.normalize("task", {"id": 7, "name": "x", "execution": 9, "status": "wait", "pri": 3},
                  BASE, PROJECTS, EXECS)
check("任务-项目由迭代反推", t2["project"] == "项目乙", t2["project"])
check("任务-无截止显示空", t2["deadline"] == "", t2["deadline"])

# ---- 研发需求：无迭代 ----
s = zd.normalize("story", {"id": 65, "title": "到期提醒", "project": 1, "status": "active",
                           "stage": "developing", "pri": 1, "estimate": "0.00"},
                 BASE, PROJECTS, EXECS)
check("需求-迭代为空", s["execution"] == "", s["execution"])
check("需求-状态拼阶段", s["status"] == "激活 / 研发中", s["status"])
check("需求-项目名", s["project"] == "项目甲", s["project"])

# ---- Bug：严重程度 + 已解决 ----
b = zd.normalize("bug", {"id": 88, "title": "单位未换算", "project": 4, "execution": 9,
                         "status": "resolved", "pri": 1, "severity": 2,
                         "openedBuild": "trunk"}, BASE, PROJECTS, EXECS)
check("Bug-严重程度", b["severity"] == "2 严重", b["severity"])
check("Bug-状态中文", b["status"] == "已解决", b["status"])
check("Bug-备注含版本", "trunk" in b["extra"], b["extra"])

# ---- 需求（UR）：标题字段为 title，项目缺失 ----
r = zd.normalize("requirement", {"id": 12, "title": "统一鉴权", "status": "active", "pri": 2},
                 BASE, PROJECTS, EXECS)
check("需求-未归属项目", r["project"] == zd.NO_PROJECT, r["project"])
check("需求-pid 归零", r["pid"] == "0", r["pid"])
check("需求-链接", "requirementID=12" in r["url"], r["url"])

# ---- 未知状态不崩，原样透出 ----
u = zd.normalize("task", {"id": 9, "name": "y", "project": 1, "status": "weird"}, BASE, PROJECTS, EXECS)
check("未知状态透出", u["status"] == "weird", u["status"])

# ---- 未知迭代 ID 有兜底文案，不抛异常 ----
v = zd.normalize("task", {"id": 10, "name": "z", "project": 1, "execution": 999}, BASE, PROJECTS, EXECS)
check("未知迭代兜底", v["execution"] == "迭代#999", v["execution"])

# ---- 排序键：优先级数字，缺失/非法排最后 ----
check("优先级解析", zd.normalize("task", {"id": 11, "project": 1, "pri": "2"}, BASE, PROJECTS, EXECS)["pri_raw"] == 2)
check("非法优先级排后", zd.normalize("task", {"id": 12, "project": 1, "pri": "x"}, BASE, PROJECTS, EXECS)["pri_raw"] == 99)

print()
if failed:
    print(f"{len(failed)} 项失败：{failed}")
    sys.exit(1)
print("全部通过")
