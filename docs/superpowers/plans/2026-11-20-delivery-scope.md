# 禅道工单分端交付改造计划

2026-11-20 · 配套[流程设计](../specs/2026-11-20-delivery-scope.md)

**目标**　在保留当前项目纯前端默认值的前提下，让新工单按配置、需求标记、AI 研判的优先级运行纯前端、纯后端或前后端流程。

**实现方式**　在现有 Python、SQLite、Git worktree 和 Windows 阶段运行器上扩展 `claim` / `record`。配置快照约束可选范围，工单账本保存实际路线、执行阶段和交接证据。以同一主工单会话负责两端合回。以下复选框供实施时勾选；可按 executing-plans 技能逐项执行，有独立任务时可按 subagent-driven-development 技能分工。

## Chunk 1：从工单领取到最终合回

### 实施与验证记录

当前项目保留 `["frontend_only"]`。改造前 36 项回归通过，改造后 65 项测试全部通过。验收使用合成工单、临时 SQLite、独立 Git 仓库、本地 HTTP 服务和无界面 Chrome。演练直接调用生产账本的接单和记录入口，由测试用例执行各角色的动作。逐阶段核对工作树、候选与合回提交、服务响应、页面显示及原工单待回写记录。

| 演练 | 结果 |
|---|---|
| 纯前端项目分别接收未标记、标记“前后端”的材料 | 两单各 10 个阶段，均只运行前端并合回 |
| 纯后端项目接收标记“纯前端”的材料 | 10 个阶段，无需前端工作区，服务验收并合回 |
| 前后端项目接收标记“前后端”的材料 | 19 个阶段，后端合回与交接后才启动前端；浏览器从前端候选页面调用本次后端服务 |
| 跨端交接与返工 | 验收失败、版本不符、缺少本单联调文档均阻止交接。返工后重新核验有效版本与验收任务 |
| 后端两仓部分合回 | 保留已合回仓版本，拒绝错误回执；恢复时仅合回剩余仓库，再完成工单 |
| 升级与回退 | 用改造前的执行器领取旧单，新执行器从原 `plan` 续接到收尾；在测试库暂停接单、停止 V2 工单后切回旧执行器，待回写记录保留 |

从项目根目录运行下列命令可复验。浏览器路径按本机安装位置填写。JSON 回执含每张成功工单的阶段序列、合回 SHA 和实际验收结果。

```powershell
$env:ZENTAO_DRILL_BROWSER = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
$env:ZENTAO_DRILL_REPORT_DIR = '.local/verification/delivery-scope'
python -B -m unittest discover -s .agents/skills/zentao-autodelivery/tests -v
```

### 准备与定位

以下 `scripts/`、`tests/`、`references/` 均相对于 `.agents/skills/zentao-autodelivery/`。命令从项目根目录用 PowerShell 执行。步骤 1–6 使用临时配置、临时 SQLite 和测试仓库；真实后端接入单列在步骤 7。

| 现有位置 | 改造切入点 |
|---|---|
| `scripts/claim.py::reserve`、`scripts/ledger.py::validate_config` | 首次领取保存配置快照；增加项目模式、后端配置校验与版本分流 |
| `scripts/ledger.py::workflow_stages`、`scripts/execution.py` | 现行顺序固定，只有前端分支核验可选；阶段启动、结束、超时、返工、最终汇总、恢复均需识别实际路线 |
| `scripts/branch_check.py::branch_context`、`scripts/workflow.py::read_branch_attempt` | 当前按固定 `plan` 阶段、前端工作区及应用清单查 Git 证据；扩展到本端计划和本端仓库 |
| `scripts/status_board.py`、`scripts/worker.py`、技能指引 | 进度页、工单续接提示和 Agent 指引读取相同的端、阶段、角色和交接要求 |

- [x] 记录改造前基线。运行下方全量测试命令，保存用例数与失败项。只读检查当前项目配置为 `["frontend_only"]`。每项实施先加针对性失败用例，再实现并跑绿；形成可独立审查的提交。

```powershell
$suite = '.agents/skills/zentao-autodelivery/tests'
python -B -m unittest discover -s $suite -v
```

### 1. 配置、范围判定和标记读取

**文件**　新增 `scripts/delivery_scope.py`、`tests/test_delivery_scope.py`，修改 `scripts/ledger.py`。项目配置使用现有 `.agents/zentao-project.json` 的纯前端取值。

- [x] 用表驱动测试覆盖设计文档中的六行规则；双端缺标记分别提供三种 AI 研判结果。另测空数组、重复值、无效组合、字段缺省、示例代码块、非法标记和冲突标记。
- [x] 实现模式校验与标记解析。解析时记录原始值及材料来源；用账本按项目模式和合法标记重算优先级。仅双端且缺标记时接收拆分 Agent 给出的范围与证据；证据不足按拆分阻断处理。
- [x] 纯前端保留现有 `workspaces.frontend` 与 `development.frontend_bundle.applications`。启用后端的项目登记 `workspaces.backend` 和 `development.backend_bundle.services`；每个服务键对应仓库相对路径、目标分支、构建、单测、启动或已有服务访问方式及功能验收入口。复用已有凭据引用机制。纯后端配置可省略前端工作区、前端 bundle 和浏览器 E2E 环境。
- [x] 运行 `python -B -m unittest discover -s $suite -p test_delivery_scope.py -v`。验收要求为当前项目零后端配置可通过，三种模式解析正确，双端缺标记时账本能区分“需研判”和“已判定”。

### 2. 新单按路线推进，旧单原地续接

**文件**　修改 `scripts/claim.py`、`scripts/ledger.py`、`scripts/execution.py`、`scripts/delivery_scope.py`、`tests/test_workflow.py`。

- [x] 用一份升级前已领取且停在 `plan` 的原格式快照、一份刚发现且尚未领取的工单建立迁移用例。首次新领取在快照里写内部流程版本 2；已有快照字节内容保持原样，无版本按旧流程读取。恢复旧单沿用第一次领取的版本。
- [x] V2 开始只确定 `material_check → split`。在 `split` 成功事务内保存判定来源、原标记、按端任务和验收清单、依赖及有序阶段；可在 SQLite 增设以工单和修订号为键的路线记录。将阶段结论、路线写入、下个阶段和待回写记录一次提交。相同 `event_id` 重放只能得到一次路线和回执。
- [x] 路线用唯一阶段键。后端为 `backend.design / plan / branch_check / coding / unit_test / code_review / acceptance / integration`（各项均带 `backend.` 前缀）；前端同样带 `frontend.` 前缀，使用 `e2e` 验收。跨端在后端合回和前端设计之间插入 `handoff`。新路线未选中的端不生成执行项；旧单继续识别原无前缀阶段。
- [x] 修改 `start`、`attempt_start`、`attempt_finish`、`phase_deadline`、`terminal_steps`、`complete`、`stop` 和 `recover`。缺需求文件沿用原收尾；拆分无有效结果且已终止时不启动开发。阶段时限从本单快照读取；建议 `split` 复用计划时限、`handoff` 复用合回时限、后端验收复用验收时限，允许项目显式覆盖。保留每次尝试的绝对截止时间。
- [x] 用真实 `workflow.run` 测试三条路线和中断续接。断言单端没有另一端阶段；旧单原阶段、重试次数与回写键均可读；切换项目模式后既有工单路线不漂移。运行 `python -B -m unittest discover -s $suite -p test_workflow.py -v`。

### 3. 端内计划、Git 分支和候选版本

**文件**　修改 `scripts/branch_check.py`、`scripts/workflow.py`、`scripts/execution.py`、`references/branches.md`、`tests/test_workflow.py`、`tests/test_cli.py`。

- [x] 计划证据按端读取应用键或服务键，以当前工单快照解析对应工作区。Git 工作树按工单、轮次、端和仓库区分。前后端同名配置键、同一轮次多仓库均能定位到正确的计划和基线。旧版 `applications` 入参与回执保持兼容。
- [x] 扩展 `branch_inspect`、`branch_prepare` 及阶段结束前的 Git 实地复核，保留工作区边界、实际仓库根、目标分支、基线和工作树检查。使两端成功的编码和合回证据关联已核验仓库；分支阶段失败后仍可登记阻断，禁止向未核验路径写代码。
- [x] 在两个临时 Git 仓库验证正常创建、幂等续接、分支错误、路径越界、漏仓及跨端证据误用。运行 workflow 与 CLI 两组测试，确认错误证据无法推进为成功。

### 4. 两端执行、合回和跨端交接条件

**文件**　修改 `scripts/execution.py`、`scripts/worker.py`、`references/stages.md`、`references/e2e.md`、`references/worker.md`、`SKILL.md` 和 `tests/test_worker.py`、`tests/test_workflow.py`。新增 `references/backend.md` 说明服务验收和联调交接。

- [x] 为评审、拆分和两端开发/测试填写阶段角色、输入来源、工单目录和证据要求。拆分写 `split.md`；各端各写 `design.md`、`plan.md`、测试证据；跨端后端额外写 `backend/handoff.md`。文件置于工单目录的对应端与轮次下。阶段运行器沿用原超时与停止能力。
- [x] 接入后端构建、单测和独立验收入口。后端测试 Agent 记录版本和真实调用或任务执行结果。合回统一交主会话，逐仓保存候选、目标分支、合回后实际提交及失败原因；重试先从 Git 读回上次操作的实际结果。
- [x] 实现可执行的 `handoff` 检查。账本核对所有必需后端任务、审查、服务验收、目标仓合回和联调文档证据；主会话核对从对应合回内容启动或能验证版本的环境，并提交调用证据。交接结果绑定后端版本集合及前端任务清单。条件齐全才返回 `frontend.design`；失败按时限重试或带依赖原因收尾。即使一个可安全合入的仓库已经合回，也要保留未通过的验收结论。
- [x] 模拟完整成功、后端测试失败但安全合回、部分合回、环境运行旧版本、仅文档缺失和交接回执丢失。失败场景前端启动次数应为零，回执重放只派发一次。纯后端完成后直接收尾。纯前端运行时后端配置读取、后端进程启动和交接调用次数均为零。

### 5. 返工、恢复、回写与进度显示

**文件**　修改 `scripts/execution.py`、`scripts/ledger.py`、`scripts/status_board.py`、`references/workflow-cli.md`、`references/stages.md`、`tests/test_workflow.py`、`tests/test_writeback.py`。复用 `scripts/writeback.py` 的发送、读回和去重机制。

- [x] 本端审查和验收沿用返工分段。跨端联调发现后端问题时，在同一轮联调重试下记录责任端，依次保存后端修复、单测、独立复验、主会话重新合回与重新交接，最后重跑前端联合验收。若涉及前端改动，也要跑受影响的前端单测和复审。每一分段记录实际执行角色、截止时间及证据；后端交接版本变化使旧联合验收失效。
- [x] 接口变化影响任务或验收条件时，用可重放的修订记录关联原任务与新任务；拆分 Agent 修订、主会话重新派发，明确需要重跑的端和阶段。使用修订或返工分段标识隔离已失效结果，约束在同一工单重试预算内。恢复从当前有效路线的第一个未满足依赖开始，先核对已合回的仓库。
- [x] 在任务清单中明确未满足的业务验收项。即使所有可执行代码均已合回，此类工单最终仍为阻断或待验证。仅汇总选中端，区分“按范围不适用”“因依赖跳过”“已合回但未验收”；状态页和禅道摘要展示范围来源、当前端与阶段、最终合回版本和验收结论。
- [x] 覆盖后端合回后中断、跨仓部分成功后恢复、重新交接、旧尝试迟到、主动停跑、同一事件重复提交、回写超时后读回等场景。断言历史回执可查、阶段互不覆盖，恢复不会重复合回或重复发送事实相同的备注。

### 6. 集成演练与当前项目启用

**文件**　更新 `docs/禅道无人值守研发入门.md`、`docs/禅道无人值守研发实施方案.md` 和 `docs/intent.md` 的操作说明；在 `.agents/skills/zentao-work-item-create/references/operations.md` 说明可选标记及三种写法。只对测试夹具提供双端配置示例。

- [x] 从 `claim → bind → start → record → complete` 演练三张合成工单及一张旧快照工单，通过临时数据库、Git 仓库和可控本地服务读回结果。前后端工单记录两端 SHA 与联调样例；运行时若旧环境或错误版本也通过，判为验收失败。
- [x] 再跑全量 `python -B -m unittest discover -s $suite -v`，并对照设计第 5 节验收。除绿色测试外，检查进度页、收尾摘要、逐仓合回回执和双端版本关联。测试运行使用临时回写目标。
- [x] 首次应用到当前项目时保留 `["frontend_only"]`。用当前配置副本处理一份标了“前后端”的材料和一份无标记材料，两单均应产生拆分与完整前端阶段，零后端活动。上线后新领取工单使用新路线，已领取工单继续使用旧路线。
- [x] 回退演练先暂停新领取、核对活动尝试与未发送回执。已有 V2 工单由支持其版本的执行器收尾或受控停跑；确认无活动 V2 工单后再恢复旧执行器。保留 SQLite 历史和原工单绑定，记录待回写项归属。

### 7. 未来接入首个真实后端

- [ ] 项目提供真实服务仓库和目标分支、构建与单测入口、运行环境及可验证版本的方法。在独立测试配置中将后端工作区和服务条目映射到这些实际值。
- [ ] 先跑纯后端小工单，核验从分支准备到服务验收、合回的实际版本；再跑一张涉及接口和页面的跨端工单，从联调文档独立启动或连接服务，验证两端版本和最终业务结果。演练通过后再按项目决定切换配置。当前项目交付以步骤 6 的纯前端验收为准。
