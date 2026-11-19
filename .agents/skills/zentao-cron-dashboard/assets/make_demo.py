"""用样例数据渲染一份 demo.html，仅用于检查看板样式与项目过滤，不影响 dashboard.html。

不读 config.json、不连禅道，可在初始化后随时跑：
    python make_demo.py
"""
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("zd", os.path.join(HERE, "zentao_my_dashboard.py"))
zd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zd)

BASE = "http://zentao.example.com"
P1, P1N = "1", "示例项目 A"
P2, P2N = "4", "示例项目 B"


def mk(kind, iid, title, status, pri, proj, execu="", deadline="", extra="", sev=None,
       updated="2026-01-01 11:20"):
    pid, pname = proj
    return {
        "kind": kind, "id": str(iid), "title": title, "status": status,
        "pri": zd.PRI_LABEL.get(str(pri), str(pri)), "pri_raw": pri,
        "severity": zd.SEV_LABEL.get(str(sev), "") if sev else "",
        "deadline": deadline, "pid": pid, "project": pname, "execution": execu,
        "extra": extra, "assignedTo": "demo", "updated": updated,
        "url": f"{BASE}/index.php?m={kind}&f=view&{kind}ID={iid}",
    }


items = [
    mk("requirement", 12, "示例需求：导出支持标准格式", "激活 / 研发中", 1, (P1, P1N), "R3 迭代", "2026-02-10"),
    mk("requirement", 15, "示例需求：参数模板可继承", "待评审", 2, (P1, P1N), "", "2026-02-15"),
    mk("requirement", 21, "示例需求：统一鉴权改造", "激活", 2, (P2, P2N), "", "2026-02-20"),
    mk("story", 28, "示例研发需求：版本回滚", "激活 / 研发中", 2, (P1, P1N), "R3 迭代", "2026-02-12", extra="预计 16h"),
    mk("story", 31, "示例研发需求：批量导入进度", "激活 / 已计划", 3, (P2, P2N), "Sprint 7", "2026-02-18", extra="预计 8h"),
    mk("task", 104, "示例任务：抽离公共算法", "进行中", 1, (P1, P1N), "R3 迭代", "2026-02-11",
       extra="工时 预计8 / 已耗5 / 剩余3"),
    mk("task", 107, "示例任务：评审意见整理", "未开始", 2, (P1, P1N), "R3 迭代", "2026-02-13",
       extra="工时 预计4 / 已耗0 / 剩余4"),
    mk("task", 110, "示例任务：环境配置排查", "进行中", 2, (P2, P2N), "Sprint 7", "2026-02-14",
       extra="工时 预计6 / 已耗2 / 剩余4"),
    mk("bug", 88, "示例 Bug：单位未换算", "激活", 1, (P1, P1N), "R3 迭代", "2026-02-11",
       extra="影响版本 trunk", sev=2),
    mk("bug", 91, "示例 Bug：浏览器下样式错位", "已解决", 3, (P1, P1N), "R3 迭代", "2026-02-16",
       extra="影响版本 2.3.0", sev=3),
    mk("bug", 95, "示例 Bug：分页参数超限报错", "激活", 2, (P2, P2N), "Sprint 7", "2026-02-17",
       extra="影响版本 1.8.2", sev=1),
]

new = [items[0], items[5], items[8], items[10]]
cats = [(k, label) for k, label in zd.ALL_CATS]
meta = {"user": "示例用户（demo）", "base": BASE, "api": "v2", "time": "2026-01-01 12:00", "runs": 1}
out = os.path.join(HERE, "demo.html")
with open(out, "w", encoding="utf-8") as f:
    f.write(zd.render(items, new, [], meta, cats))
print("demo written:", out, "| items:", len(items), "| new:", len(new))
