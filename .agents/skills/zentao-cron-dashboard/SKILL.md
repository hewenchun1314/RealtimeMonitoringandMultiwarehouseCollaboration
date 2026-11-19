---
name: zentao-cron-dashboard
description: 为禅道（ZenTao）创建「指派给我」的定时工作看板：拉取需求/研发需求/任务/Bug，生成支持按项目过滤、标注所属项目与迭代的静态看板，并在宿主 agent（WorkBuddy/CodeBuddy 自动化、Claude Code、Codex、系统计划任务等）里创建定时任务、提示新增条目。当用户说"定时从禅道拉取我的任务/需求/Bug 生成看板"、"做个禅道工作台/日报定时任务"、"禅道有新单子提醒我"、"把禅道指派给我的东西汇总成页面"时使用。
---

# 禅道定时工作看板

给任意禅道环境落地一套「定时采集 → 生成看板 → 新增提醒」的能力。采集引擎与 agent 无关，宿主只负责**定时触发**和**把新增讲给用户**。

## 适用范围

- 目标：把指派给某个账号的需求（UR）/ 研发需求（SR）/ 任务 / Bug 汇总成一个本地 HTML 看板，支持按项目过滤，每条标明所属项目与迭代。
- 支持禅道 v2（18+）与 v1（12~17）；v2 优先用 `/my/*`，v1 走项目/迭代枚举。
- 不写入禅道任何数据，纯只读采集。
- 与具体禅道地址、账号、项目无关：全部来自用户输入与 `config.json`。

## 一、输入

| 项 | 必填 | 说明 |
| --- | --- | --- |
| 禅道地址 | 是 | 如 `http://192.168.1.10:8080`，带端口，不带 `/api.php` |
| 账号 / 密码 | 是 | 用于登录拿 token；密码只落本地 `config.json`，**不进日志、不进对话正文** |
| 被指派人账号 | 否 | 默认同账号；注意禅道按**账号**匹配，不是姓名 |
| 看板输出目录 | 否 | 默认当前目录下的 `zentao-dashboard/` |
| 定时频率 | 否 | 默认工作日每小时；其他见 `references/agents.md` 的 rrule 表 |
| 采集类别 | 否 | 默认四类全收；只要任务就传 `task` |

缺项就问，**不要猜禅道地址或账号**。密码建议让用户用环境变量传（`ZENTAO_PASSWORD`），避免留在 shell history。

## 二、执行流程

设 `<skill>` 为本 skill 目录。

### 1. 初始化 + 连接自检 + 首版看板

```bash
python "<skill>/scripts/init_workspace.py" \
  --base-url "http://<host>:<port>" \
  --account "<账号>" \
  --password "<密码或从环境变量读取>" \
  --assignee "<被指派人，可省略>" \
  --out-dir "<看板目录>" \
  --json
```

脚本会：复制模板 → 写 `config.json`（unix 下 0600）→ `--dry-run` 验证登录（失败立即停，不写 `state.json`）→ 正式采集生成 `dashboard.html`，最后打印一行 JSON（`ok/out_dir/api/total/counts/warnings/dashboard`）。

**首跑即基线**：第一次运行把所有条目都记为「已见」，不会报一堆新增，这是预期行为。

### 2. 确认产物

- `dashboard.html`：看板。顶部有项目过滤下拉，表格列为 `ID | 标题 | 项目 | 迭代 | 状态 | 优先级 | 截止 | 备注 | 更新`
- 想把看板效果先给用户看：`python make_demo.py` 生成 `demo.html`（假数据样张）
- 想确认前端过滤逻辑没坏：`node test_filter.js`（解析 `demo.html` 真实结构跑页面脚本，全 PASS 才算通过）

### 3. 检查采集健康度

看脚本输出的 `warnings` 和 `total`：

- `total = 0`：**先别急着说成功**。核对 `/my/work` 的 `todoCount` 是否也是 0；若界面里有、这里没有，多半是权限或指派账号不匹配，如实告诉用户原因（见 `references/zentao-api-notes.md` 第 3 节）
- 出现 `403 Access not allowed`：该账号没模块权限，属正常降级，说明清楚即可
- 出现 `401`：token 或账号密码问题

### 4. 创建定时任务

按宿主能力选方案，完整对照和命令见 **`references/agents.md`**：

- **WorkBuddy / CodeBuddy**：用 `automation_update` 工具建 recurring 自动化，`cwds` 指向看板目录
- **Claude Code / Codex CLI**：系统计划任务 + `claude -p` / `codex exec`
- **Cursor 等无调度能力**：系统计划任务直接跑 python（不经过 agent，最省）
- **schtasks 被安全策略禁用**：用 `watch_loop.py` 常驻兜底

自动化 prompt 必须自包含（新会话看不到当前对话）：写明看板目录、执行命令、汇报格式、禁止改 `state.json`。

### 5. 汇报

给用户四件事：

1. 看板路径（可预览就顺手打开）
2. 当前条数与分类分布，以及涉及几个项目
3. 定时任务的实际频率 + 新增怎么提示
4. 若数据为 0 或存在告警，**如实说明原因和排查方向**，不要粉饰

## 三、定时任务的标准 prompt

```
从禅道拉取指派给 <账号> 的需求 / 研发需求 / 任务 / Bug，生成工作看板并报告新增。

1. 在目录 <看板目录> 下执行：python zentao_my_dashboard.py --json
2. 解析最后一行 JSON（counts、total、new_count、by_project、new[]、warnings、dashboard）：
   - new_count > 0：以「新增 N 条」开头，逐条列 类型、ID、标题、所属项目 / 迭代、状态、截止、链接
   - new_count = 0：一句话「本次无新增，当前共 X 条（需求 a / 研发需求 b / 任务 c / Bug d）」，并给按项目分布
3. warnings 非空单独列出；登录/连接失败要明确说「禅道连接失败」+ 原文与排查建议
4. 不要修改 state.json / config.json；汇报简短，不贴日志全文
```

## 四、安全与边界

- 密码只写本地 `config.json`；不回显、不写日志、不进自动化 prompt、不提交版本库（提醒用户把 `config.json` 加进 `.gitignore`）
- 采集只读，绝不对禅道发起写请求
- `state.json` 是新增判定的唯一依据：**不要**让定时任务或 agent 去清空/改写它；用户想「全量重报」时才删
- 不要因为 `total = 0` 就断言「任务已完成」——先区分「真的没有」和「看不到」

## 五、验收自检

- [ ] `init_workspace.py --json` 返回 `ok: true`，且 `api` 与实际版本一致
- [ ] `dashboard.html` 能打开，项目下拉有选项（有数据时）
- [ ] 每条记录都能解析出项目名；有迭代的显示迭代名，没有的显示 `—`
- [ ] `node test_filter.js` 全 PASS（改过模板才需要）
- [ ] 定时任务已创建，`cwds` 正确，频率符合用户要求
- [ ] 第二次运行 `new_count = 0`（证明基线生效，不会重复报新）

## 六、目录

```
zentao-cron-dashboard/
├── SKILL.md
├── scripts/init_workspace.py          初始化 + 自检 + 首版看板
├── assets/
│   ├── zentao_my_dashboard.py         采集引擎（纯标准库，可独立运行）
│   ├── watch_loop.py                  无常驻调度时的兜底轮询
│   ├── make_demo.py                   假数据样张
│   ├── test_filter.js                 过滤逻辑单测（Node）
│   └── config.example.json            配置模板
└── references/
    ├── agents.md                      各 agent / 系统建定时任务的方法
    └── zentao-api-notes.md            接口、字段、坑
```

引擎用法本身不看 SKILL.md 也能跑：

```bash
python zentao_my_dashboard.py            # 采集 + 生成看板
python zentao_my_dashboard.py --json     # 附一行结构化结果
python zentao_my_dashboard.py --dry-run  # 只统计，不改 state.json
```
