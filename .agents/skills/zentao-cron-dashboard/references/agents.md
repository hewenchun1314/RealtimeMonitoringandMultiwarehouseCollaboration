# 在各家 agent / 系统里创建定时任务

采集引擎本身与 agent 无关（纯 Python + config.json）。差别只在**谁来定时触发**、**新增怎么提示用户**。

先确定三件事，再选方案：

| 问题 | 说明 |
| --- | --- |
| 宿主有没有原生定时能力 | WorkBuddy / CodeBuddy 有自动化工具；Claude Code、Codex、Cursor 没有内置 cron |
| 通知要发到哪里 | 宿主的会话消息最自然；没有宿主就用文件/桌面通知/邮件 |
| `state.json` 能不能持续保留 | **必须**。它丢了等于把现有条目全当新增，会误报 |

## 方案对照

| 环境 | 定时方式 | 提示方式 | 备注 |
| --- | --- | --- | --- |
| WorkBuddy / CodeBuddy | `automation_update` 工具（原生） | 自动化运行结果直接进会话 | 首选 |
| Claude Code | 系统计划任务 + `claude -p` | 会话输出 / 文件 | 无原生 cron |
| Codex CLI | 系统计划任务 + `codex exec` | 终端输出 / 文件 | 无原生 cron |
| Cursor / 其他 IDE agent | 系统计划任务（agent 不参与） | 文件 + 看板本身 | 只跑 Python 即可 |
| 无 agent 的机器 | Windows 计划任务 / crontab / systemd timer | notify.log + 看板 | 不依赖大模型 |
| 团队共享 | GitHub Actions / Jenkins 定时 | 提交产物 / 通知插件 | 需持久化 state.json |

---

## 1. WorkBuddy / CodeBuddy（首选）

用 `automation_update` 工具建一条 recurring 自动化。要点：

- `scheduleType="recurring"`，`rrule` 用 RFC 5545；`cwds` 设为看板目录（脚本的 cwd）
- prompt 必须自包含：新会话看不到当前对话，路径、命令、汇报格式都要写清楚
- 别让 agent 改 `state.json` / `config.json`

示例参数（把路径换成实际目录）：

```json
{
  "mode": "create",
  "name": "禅道我的工作看板（每工作小时拉取）",
  "scheduleType": "recurring",
  "rrule": "FREQ=HOURLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR",
  "status": "ACTIVE",
  "cwds": "C:/path/to/zentao-dashboard"
}
```

prompt 模板：

```text
从禅道拉取指派给 <账号> 的需求 / 研发需求 / 任务 / Bug，生成工作看板并报告新增。

1. 在目录 <看板目录> 下执行：python zentao_my_dashboard.py --json
2. 解析最后一行 JSON（字段：counts、total、new_count、by_project、new[]、warnings、dashboard）：
   - new_count > 0：以「新增 N 条」开头，逐条列出 类型、ID、标题、所属项目 / 迭代、状态、截止、链接
   - new_count = 0：一句话说明「本次无新增，当前共 X 条（需求 a / 研发需求 b / 任务 c / Bug d）」，并给出按项目分布
3. warnings 非空时单独列出；登录/连接失败时明确说「禅道连接失败」+ 原文，并提示排查网络、账号密码
4. 不要修改 state.json / config.json；汇报要简短，不要贴脚本日志全文
```

### rrule 写法（满足 schema 要求）

| 需求 | rrule |
| --- | --- |
| 工作日每小时 | `FREQ=HOURLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR` |
| 每 2 小时 | `FREQ=HOURLY;INTERVAL=2` |
| 每天 09:00 | `FREQ=DAILY;BYHOUR=9;BYMINUTE=0` |
| 工作日 09:00 和 18:00 | `FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0;BYDAY=MO,TU,WE,TH,FR` |
| 每周一 09:30 | `FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=30` |
| 每月 1 号和 15 号 09:00 | `FREQ=MONTHLY;BYMONTHDAY=1,15;BYHOUR=9;BYMINUTE=0` |

`FREQ=HOURLY` 只写 `INTERVAL`（可选 `BYDAY`），不要加 `BYHOUR`/`BYMINUTE`；`DAILY`/`WEEKLY`/`MONTHLY` 必须带 `BYHOUR`+`BYMINUTE`。

---

## 2. Claude Code

没有内置 cron。两种做法：

**a) 系统计划任务调 headless 模式**

```bash
# crontab -e（Linux/macOS）：工作日 9-19 点每小时
0 9-19 * * 1-5 cd /path/to/zentao-dashboard && claude -p "执行 python zentao_my_dashboard.py --json，按新增条目汇报，详见 README" >> claude-run.log 2>&1
```

Windows 用计划任务指向同样的命令（`claude.exe` 换成绝对路径）。

**b) 干脆不用 agent**：直接 `python zentao_my_dashboard.py --json`，看板自己更新，`notify.log` 记录新增。省 token，也最稳。

---

## 3. Codex CLI

同 Claude Code 思路，把 `claude -p` 换成 `codex exec`：

```bash
0 9-19 * * 1-5 cd /path/to/zentao-dashboard && codex exec "运行 python zentao_my_dashboard.py --json 并汇总新增条目" >> codex-run.log 2>&1
```

---

## 4. Windows 计划任务（不依赖 agent）

先在本机确认 `schtasks` 是否被安全策略拦截（部分环境会直接拒绝执行）。

```bat
REM 每小时第 5 分钟跑，写日志
schtasks /create /tn "ZenTaoDashboard" /sc hourly /mo 1 /st 09:05 ^
  /tr "cmd /c cd /d C:\path\to\zentao-dashboard && python zentao_my_dashboard.py --json >> run.log 2>&1" /f
```

查看 / 删除：

```bat
schtasks /query /tn "ZenTaoDashboard"
schtasks /delete /tn "ZenTaoDashboard" /f
```

**schtasks 被禁用时**：用常驻兜底脚本，登录时启动即可

```bat
REM 启动文件夹放一个 .vbs，静默常驻
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\path\to\zentao-dashboard"
sh.Run "pythonw.exe watch_loop.py --interval 60", 0, False
```

`watch_loop.py` 每 N 分钟跑一次采集，新增条目写进 `notify.log` 并打印；采集失败会退避重试。

---

## 5. Linux · cron

```bash
# 工作日 9-19 点每小时
0 9-19 * * 1-5 cd /path/to/zentao-dashboard && /usr/bin/python3 zentao_my_dashboard.py --json >> run.log 2>&1
```

crontab 里 `%` 要转义；用户级 crontab 找不到 python 时写绝对路径。

## 6. Linux · systemd timer

`~/.config/systemd/user/zentao-dashboard.service`

```ini
[Unit]
Description=ZenTao my-work dashboard

[Service]
Type=oneshot
WorkingDirectory=%h/zentao-dashboard
ExecStart=/usr/bin/python3 %h/zentao-dashboard/zentao_my_dashboard.py --json
StandardOutput=append:%h/zentao-dashboard/run.log
```

`~/.config/systemd/user/zentao-dashboard.timer`

```ini
[Unit]
Description=Run ZenTao dashboard hourly on workdays

[Timer]
OnCalendar=Mon..Fri *-*-* 09..19:05:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now zentao-dashboard.timer
systemctl --user list-timers | grep zentao
```

---

## 7. CI（GitHub Actions 示例）

```yaml
name: zentao-dashboard
on:
  schedule:
    - cron: "5 1-10 * * 1-5"   # UTC，对应北京时间 9-18 点每小时
  workflow_dispatch:

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/cache@v4        # state.json 必须跨次保留，否则全是「新增」
        with:
          path: zentao-dashboard/state.json
          key: zentao-state-${{ github.run_id }}
          restore-keys: zentao-state-
      - run: |
          cd zentao-dashboard
          echo "${{ secrets.ZENTAO_CONFIG }}" > config.json
          python zentao_my_dashboard.py --json | tee result.json
      - uses: actions/upload-artifact@v4
        with:
          name: dashboard
          path: |
            zentao-dashboard/dashboard.html
            zentao-dashboard/result.json
```

密钥放仓库 Secrets，不要提交 `config.json`。

---

## 通知方式参考

| 方式 | 落地 |
| --- | --- |
| 会话消息 | 宿主自动化（WorkBuddy/CodeBuddy）最省事 |
| 文件 | 引擎的 `--json` 末行 JSON；`watch_loop.py` 的 `notify.log` |
| 桌面通知（Windows） | PowerShell BurntToast 模块，或 `msg %username% "新增 N 条"` |
| 邮件 | 宿主有邮件连接器时，让 agent 把 `new[]` 汇总后发信 |
| IM | 同上有 IM 连接器时转发；不要再造轮子 |

## 通用注意事项

- 定时任务只读 `config.json`；**不要**让任务去改 `state.json`，否则新增判定失效。
- 想「重来一次、把现有全算新增」：删除 `state.json` 后再跑。
- 看板是静态文件，直接覆盖；如需历史版本，自己按日期另存。
- 采集失败要显式报错，别静默跳过——否则用户会以为「没有新条目」。
