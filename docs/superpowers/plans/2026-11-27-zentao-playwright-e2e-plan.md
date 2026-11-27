# 禅道合回后 Playwright Test E2E 实施计划

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking. **只有用户明确批准设计和计划后才可执行；本次所有实施步骤均未执行。**

**Goal:** 将禅道需求与 Bug 的正式验收接入 Playwright Test 和统一回执，确保先合回、按实际合回 SHA 验收、失败可定位且执行有截止。

**Architecture:** SQLite 账本及 worker 持有工单与尝试；宿主控制固定版本环境、Windows Job 和截止时间；Playwright Test 执行页面用例，后台适配器执行 API 用例。逐用例结果持久化后，由统一校验器登记正式事实，原同步器办理禅道回写。

**Tech Stack:** Python 标准库与 unittest、SQLite、PowerShell、Windows Job Object、Node 24、独立锁定的 `@playwright/test 1.60.0`、TypeScript fixtures/Reporter、Git、既有构建发布及禅道接口。

设计依据：[完整设计](E:/RealtimeMonitoringandMultiwarehouseCollaboration/docs/superpowers/specs/2026-11-27-zentao-playwright-e2e-design.md)。

状态：待用户审核。日期：2026-11-27。当前观察基线为 `main@ed73174ce5602907eacf770256cf61a930029ea2`；实施开始时重新冻结基线。

## 执行约束与任务依赖

- 本轮只写两份文档。下列命令、测试、安装、提交、发布均属于批准后的实施步骤。
- 当前其他修改由其所有者继续维护。本计划不授权 reset、stash、覆盖、提交无关改动或将旧分支整段恢复。
- 在受控开发工作树实施；每个任务只提交自己的文件，中文说明问题、行为与验证。提交通过原 hooks；不使用 `git add .`、跳过 hooks 或强推。文档交付本身不包含 commit/push。
- 小步提交仅是未来实施安排；对外推送、部署和原单操作遵守项目现有授权，不以测试计划扩大权限。
- 每个任务按“行为测试失败 → 最小实现 → 对应测试通过 → 差异复核 → 本任务提交”推进，不以缺少模块的 ImportError 代替唯一的行为红灯。
- Task 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11。先建立工具包和认证夹具，再执行依赖它们的资源/结果测试。
- 每个 Task 下的子步骤为实际勾选单位。失败或范围变化先更新设计和本计划，不边执行边隐式扩大合同。

### 统一命令环境

以下命令均从批准后隔离工作树的仓库根目录执行，使用该工作树路径，不硬编码成当前正在被其他任务修改的 main。

文件清单中的缩写根固定如下：`scripts/`、`tests/` 和 `e2e/` 分别指 `.agents/skills/zentao-autodelivery/scripts/`、`.agents/skills/zentao-autodelivery/tests/` 和 `.agents/skills/zentao-autodelivery/e2e/`；不会在仓库根另建同名目录。命令里的路径已经按仓库根写出。

```powershell
$e2eRepo = (git rev-parse --show-toplevel).Trim()
$e2eScripts = Join-Path $e2eRepo '.agents/skills/zentao-autodelivery/scripts'
$e2eTests = Join-Path $e2eRepo '.agents/skills/zentao-autodelivery/tests'
$e2eTool = Join-Path $e2eRepo '.agents/skills/zentao-autodelivery/e2e'
$env:PYTHONPATH = $e2eScripts
```

Python 回归使用 `python -m unittest discover -s $e2eTests -p '具体测试文件.py' -v`。浏览器依赖只在已批准的工具准备步骤安装一次；正式工单不使用会临时下载包的 `npx ...@latest`。

## Chunk 1: 基线、协议与合回后路由

### Task 0: 冻结实施基线和现有改动边界

**文件与依据：**

- 读：[项目配置](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/zentao-project.json)、[当前阶段路由](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/skills/zentao-autodelivery/scripts/delivery_scope.py)。
- 读：当前 main 未提交修改、`codex/zentao-post-merge-e2e` 的相关提交及测试，比较后选取已验证能力。
- 新建实施证据：`.local/zentao-delivery/verification/playwright-e2e-migration/baseline.json`，不提交运行证据。

- [ ] 核对用户已批准的设计/计划版本，记录文档摘要、HEAD、分支、工作树与暂存区清单及文件哈希。
- [ ] 列出并确认接入点：after-integration 路由、受控 Git/WIP 保存、精确检出、共享占用、宿主 E2E 派发；标注当前缺少与已有 WIP 的重叠文件。
- [ ] 为重叠改动逐项核对意图，准备独立开发工作树；通过原受控 Git 入口办理，保留用户原内容和暂存层次。
- [ ] 记录 Node/Playwright/浏览器/依赖锁/启动拓扑。系统 Chrome 只作为已发现候选；正式浏览器安装属于 Task 5，不在此隐式启动。
- [ ] 对已选取的 Python/Node 基线测试运行一次，保存真实命令、退出码和文件副作用；既有失败单列，不修改无关功能。

**验收：** 基线清单完整、与用户 WIP 无混淆、没有意外文件变化。此前 399 项通过证据不能直接当作当前 main 的测试结果。

**提交：** 本任务仅准备隔离和证据；若需要解决前置代码冲突，另列最小修订及对应测试后提交，不混入后续功能。

### Task 1: 建立引擎无关的合同和回执校验

**文件：**

- 新增：`.agents/skills/zentao-autodelivery/scripts/e2e_contract.py`。
- 新增：`.agents/skills/zentao-autodelivery/scripts/e2e_results.py`。
- 新增：`.agents/skills/zentao-autodelivery/tests/test_e2e_contract.py`。
- 修改：[route_evidence.py](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/skills/zentao-autodelivery/scripts/route_evidence.py)；旧校验入口保留历史适配。

**接口边界：**

```python
def normalize_e2e_contract(config_snapshot): ...
def effective_versions(history, side, *, mode, contract): ...
def validate_request(request, job): ...
def validate_receipt(receipt, expected): ...
def summarize_cases(plan, events, *, host_outcome): ...
```

`expected` 必须由活动尝试、请求摘要、冻结计划、环境清单和宿主事实构造，不能直接从待校验回执复制。正式 `effective_versions` 只读 integration 成功回执。

浏览器阶段 engine为 `playwright-test`，后台阶段为 `backend-api`，由合同与当前阶段推导。版本证明按 `frontend-http`、`backend-local`、`backend-deployed` 分别校验；后端不要求frame，前端不能用后台健康检查代替浏览器资源证明。

- [ ] 写行为测试：候选 SHA 冒充合回 SHA、错误 attempt/round/mode/engine、路径越界、错误计划摘要均被拒绝；后台engine/证明类型正确，读历史合同不篡改记录。
- [ ] 写结果完整性测试：全部 skipped、零用例、缺少必需 case、错误阶段、重复 case 均不能 passed；次要异常不删除业务首因。
- [ ] 运行 `python -m unittest discover -s $e2eTests -p 'test_e2e_contract.py' -v`，确认上述行为在旧实现下失败。
- [ ] 实现合同2、统一evidence.e2e_receipt、版本来源和身份校验；按设计10.1登记配置键与有效值，保留合同1历史回执只读适配，不直接放宽runner字符串白名单。
- [ ] 重跑同一命令及 `test_route_evidence.py`，预期全部通过、没有接受缺失证据的捷径。
- [ ] 复核本任务差异并单独提交，建议说明：`建立禅道正式验收的统一合同和版本回执校验`。

**关键断言示例：**

```python
history = {
    'frontend.coding': {'evidence': {'versions': {'app': 'a' * 40}}},
    'frontend.integration': {
        'outcome': 'success',
        'evidence': {'merged_versions': {'app': 'b' * 40}},
    },
}
self.assertEqual(
    effective_versions(history, 'frontend', mode='formal', contract=2),
    {'app': 'b' * 40},
)
```

### Task 2: 调整新合同路由，并保留脏主工作区合回

**文件：**

- 修改：[delivery_scope.py](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/skills/zentao-autodelivery/scripts/delivery_scope.py)、[ledger.py](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/skills/zentao-autodelivery/scripts/ledger.py)、[route_evidence.py](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/skills/zentao-autodelivery/scripts/route_evidence.py)。
- 修改：[development_git.py](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/skills/zentao-autodelivery/scripts/development_git.py)、[development_git_effects.py](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/skills/zentao-autodelivery/scripts/development_git_effects.py)、[development_git_runner.py](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/skills/zentao-autodelivery/scripts/development_git_runner.py)。
- 测试：`test_route_execution.py`、`test_route_evidence.py`、`test_development_git.py`；新增 `test_e2e_routes.py`、`test_e2e_dirty_merge.py`。

- [ ] 写纯前端、纯后端、跨端Story/Bug路由红灯：合同2 integration必须在正式e2e/acceptance前；完整覆盖设计5.2开关真值表，前端总开关同时控制两类页面阶段，后台独立，禁用不能形成页面通过。
- [ ] 写合回前置红灯：有 coding/单测/审查即可进入 integration，不等待尚未发生的正式验收；验收缺少成功合回时拒绝运行。
- [ ] 用临时仓库覆盖仅unstaged、仅staged、两层同时存在、无关未跟踪文件、快进/分叉/已包含、提交冲突与WIP重放冲突。无关文件核对原始字节/index blob；冲突文件先归档原版，再核对明确的逐层解决结果、HEAD及无关改动未提交。
- [ ] 分别运行 `test_e2e_routes.py`、`test_e2e_dirty_merge.py`，确认旧顺序/脏树拒绝行为可被测试捕获。
- [ ] 实现新合同路由、阶段中文名、后端 acceptance 重排及条件化合回校验；同时登记stage_role、时限键、宿主阶段白名单、恢复定位和允许返工阶段。按 Task 0 差异清单逐项接入安全 WIP 处理，禁止恢复整套旧并行调度。
- [ ] 重跑上面两个新模块和 `test_development_git.py`、`test_route_execution.py`、`test_route_evidence.py`，预期全部通过，历史路线解析保持原含义。
- [ ] 单独提交，建议说明：`固定禅道合回后验收顺序并保留主工作区本地改动`。

**顺序断言：**

```python
self.assertLess(stages.index('frontend.integration'), stages.index('frontend.e2e'))
self.assertLess(stages.index('backend.integration'), stages.index('backend.acceptance'))
```

这些断言只对新合同生效；旧回执仍按原路线解释，但不能替代新合同的正式通过。

## Chunk 2: 固定版本环境、宿主截止与认证

### Task 3: 准备可证明版本的隔离环境

**文件：**

- 新增：`scripts/merged_e2e_sources.py`、`scripts/e2e_environment.py`、`tests/test_e2e_environment.py`。
- 新增：`tests/test_merged_e2e_inputs.py`，复用并调整历史分支中已经验证的真实检出测试。
- 复用现有启动配置与后台部署版本检查；不修改业务仓的启动命令来临时隐藏异常。

- [ ] 写红灯：主工作区含 WIP 或发生变化时，正式运行仍只包含登记的合回版本；错误分支/缺 SHA/不一致依赖缓存被拒绝。
- [ ] 写 Windows 长路径与清理红灯：clone 前 longpaths 生效、短 Git 元数据、伪 owner/联接越界/中断残留均有准确处理。
- [ ] 运行 `test_e2e_environment.py` 和 `test_merged_e2e_inputs.py`，确认能捕获原长路径和混合版本问题。
- [ ] 实现规范化隔离路径、独立所有权、精确检出和依赖清单；运行前后核对源文件，源码与 Git 元数据可独立恢复清理。
- [ ] 实现 `committed-dev` 运行清单与目标响应清单：启动本次服务、核对 PID/端口/源根、在导航前生成预期摘要。支持已有 `committed-build` 清单，不强制重写全部微前端构建。
- [ ] 定义无部署后台的交接配方：合回SHA、产物/依赖摘要、启动命令、配置引用及数据恢复；在前端Job中重启同一版本，不把已停止PID传给联合测试。增加该生命周期测试。
- [ ] 重跑上述测试，并在实际 Windows 路径和完整配置基座做一次受控装配验证，预期源码/依赖归属明确、自有进程停止后清理正确。
- [ ] 单独提交，建议说明：`按合回版本准备可恢复的禅道验收环境`。

**必需产物：** runtime manifest、resource manifest、锁文件和配置摘要、源码和元数据 owner 记录、服务监听所有者、准备失败原因。不得用一次 HTTP 200 替代版本清单。

### Task 4: 接入宿主派发、预算和强制结束后的结果恢复

**文件：**

- 新增：`scripts/frontend_e2e_runner.py`、`scripts/e2e_host.py`、`tests/test_frontend_e2e_runner.py`、`tests/test_e2e_host.py`。
- 新增：`scripts/e2e_budget.py`、`scripts/e2e_delivery.py`、`tests/test_e2e_budget.py`、`tests/test_e2e_delivery.py`。
- 修改：[worker.py](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/skills/zentao-autodelivery/scripts/worker.py)。
- 修改：[ledger.py](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/skills/zentao-autodelivery/scripts/ledger.py)、[execution.py](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/skills/zentao-autodelivery/scripts/execution.py)、[deploy_control.py](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/skills/zentao-autodelivery/scripts/deploy_control.py)，分别增加持久预算/占用表并调整创建尝试、推进正式分段及释放点。
- 复用：[stage_runner.py](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/skills/zentao-autodelivery/scripts/stage_runner.py)，确认现有 WIP 后再决定是否有最小补丁。

- [ ] 写红灯：相同请求重入不重复启动、原尝试停止不明不启动、错误请求摘要/过期 deadline 被拒绝，新旧引擎共用资源锁。
- [ ] 写预算与异常红灯：提前子预算结束、账本有效截止、启动卡死、Reporter无最终回执、清理失败、宿主重启；首例完成保留，次例已开始未结束记error/interrupted或execution-timeout，第三例未开始才not-run。
- [ ] 写持久占用红灯：A首次integration前取得全部资源；A合同2验收后尚未push/部署复验时，B合同1或2的integration/push/deploy均不能插入同目标；阶段Job结束只释放阶段锁；A停止不明不释放，收尾且停止核验通过才释放。
- [ ] 运行 `test_frontend_e2e_runner.py`、`test_e2e_host.py` 和 `test_stage_runner.py`，确认现有入口的缺口。
- [ ] 实现活动尝试读取、固定 JSON 请求、宿主状态文件和恢复核对；子进程命令由配置和固定入口构造，不接受任意 shell 请求。
- [ ] 在ledger增加e2e_budgets：工单/轮次/正式阶段/受控修复分段/版本摘要唯一定位，保存首次开始与deadline。attempt_start及repair_advance进入正式分段时同事务固化有效trial.deadline_at；普通重试和重启不能重置预算。
- [ ] 实现宿主对同一deadline的消费、启动/测试/清理预算分配；Playwright globalTimeout不超过剩余预算减清理预留。内部预算提前耗尽使用failed外层结果及typed blocked/error原因，仅到达有效账本截止才登记timed_out。仅终止自有Job。
- [ ] 增加delivery_occupancies持久占用，资源稳定排序、同事务全取或不取；全部合同integration/push/deploy入口取得或核对同一占用，不能仅保护新合同；阶段结束不释放整单占用，停止不明保留或隔离，不以租约过期单独释放。
- [ ] 在本地故障夹具上重跑并核对真实停止证据；若 stage_runner 内部确有缺口，先增加相应行为测试再补最小修改。
- [ ] 单独提交，建议说明：`为禅道正式验收增加统一宿主截止和结果恢复`。

**预算核心行为：**

```python
# 在账本事务内：同一正式分段普通重试复用持久预算。
segment_budget = get_or_create_budget(segment_key, first_started_at, total_budget)
effective_deadline = min(original_phase_deadline, segment_budget.deadline_at)
trial.deadline_at = effective_deadline
# 宿主只消费已固化截止，不另外偷偷提前更改账本语义。
host_deadline = trial.deadline_at
browser_budget = min(browser_limit, host_deadline - now - cleanup_reserve)
if browser_budget <= 0:
    return blocked_result('budget-exhausted')
```

若上述预算不足发生在effective_deadline之前，账本可提前登记failed+预算阻断原因；不能提交会被旧规则拒绝的提前timed_out。将这组行为分别加入test_e2e_budget.py与test_e2e_host.py，并在本任务结束前运行。

### Task 5: 建立锁定的 Playwright Test 工具包和认证 fixture

**文件：**

- 新增 `.agents/skills/zentao-autodelivery/e2e/package.json`、`package-lock.json`、`playwright.config.ts`。
- 新增 `e2e/src/fixtures/auth.ts`、`e2e/src/fixtures/identity.ts`、`e2e/tests/auth.spec.ts`、`e2e/tests/fixtures/auth-server.mjs`。
- 新增 `scripts/playwright_e2e.py`，只作为统一宿主的固定调用入口。

- [ ] 明确工具包使用精确版本 `@playwright/test:1.60.0`，固定 Node/浏览器安装来源和缓存；写缺依赖/缺浏览器/版本漂移的预检测试。
- [ ] 先生成并核验锁文件，再在已批准的准备步骤运行 `npm ci --prefix $e2eTool`；运行 `node (Join-Path $e2eTool 'node_modules/@playwright/test/cli.js') install chromium` 到配置的受控缓存，记录版本和结果。此步骤不在每张工单重复。
- [ ] 写认证夹具红灯：有效状态、过期后恢复、账号不符、sessionStorage、无法自动完成认证，均有界结束且分类正确。
- [ ] 实现专用 context、受控凭据引用和状态更新；校验当前账号与工区，状态文件排除 Git/附件。认证刷新最多一次，之后 blocked。
- [ ] 运行 `npm --prefix $e2eTool run test:auth`，只访问本地认证夹具；预期全部用例通过且没有登录状态写入仓库。
- [ ] 再以诊断模式验证真实测试账号的登录/过期路径，不向真实业务数据写入；若无法无人值守，明确阻断灰度，不复用用户日常 profile 隐式兜底。
- [ ] 单独提交，建议说明：`接入独立 Playwright Test 浏览器与受控认证`。

**配置初值，按宿主预算覆盖：**

```typescript
import { defineConfig } from '@playwright/test';

export default defineConfig({
  workers: 1,
  retries: 0,
  timeout: 120_000,
  globalTimeout: 20 * 60_000,
  expect: { timeout: 10_000 },
  use: {
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
});
```

正式配置从受校验的运行清单读取更早的预算和输出目录。上面没有配置 webServer：服务生命周期归宿主，浏览器归 Playwright，避免重复启动和相互清理。

package脚本必须显式定义，不能只在计划里引用：

```json
{
  "type": "module",
  "scripts": {
    "test:auth": "playwright test --project=harness auth.spec.ts",
    "test:resources": "playwright test --project=harness resources.spec.ts",
    "test:results": "playwright test --project=harness results.spec.ts",
    "test:harness": "playwright test --project=harness",
    "test:formal": "playwright test --project=formal"
  },
  "devDependencies": { "@playwright/test": "1.60.0" }
}
```

Playwright配置包含两个独立project：`harness`的testDir为`./tests`、只发现本地夹具spec；`formal`的testDir为`./src/specs`、只发现`work-item.spec.ts`。harness导入过程不得读取真实项目认证或业务manifest；formal缺少宿主核验的mode/manifest时在访问网络前拒绝运行。所有本地auth/resource夹具均由测试fixture创建并在自有Job内停止，不隐式访问真实禅道。

## Chunk 3: 实际响应证明、业务用例和账本接入

### Task 6: 采集实际加载的资源，移除 Performance 唯一门禁

**文件：**

- 新增 `e2e/src/fixtures/resources.ts`、`e2e/src/runtime/resource-identity.ts`。
- 新增 `e2e/tests/resources.spec.ts`、`e2e/tests/fixtures/resource-server.mjs`。
- 修改统一回执校验以接收可信资源清单和浏览器观测，不修改 AgentLimb 历史结果。

- [ ] 建立本地 HTTP 故障夹具：251/1000 以上资源、同源/跨源 iframe、懒加载、查询参数、缓存/304、Service Worker、错误版本和正文读取失败。
- [ ] 写红灯：旧版本加载后新 GET正确仍不能通过；查询语义不同不能合并；同一目标多摘要不能忽略；缺少关键模块必须说明原因。
- [ ] 运行 `npm --prefix $e2eTool run test:resources`，确认测试不是使用 mock 的单条 performance 记录。
- [ ] 在创建 page 前注册 context 级响应监听；只采集计划中的关键资源正文，计算摘要并持续保存原始 URL/frame/来源/状态；异步任务在检查前有界等待完成。
- [ ] 实现规范化资源标识及静态/开发清单比对；语义参数保持，允许声明的时间戳参数仍受实际摘要约束。相关模块被场景触发并就绪后再做断言前检查。
- [ ] 重跑上述命令；预期资源数量与跨 frame 不造成错误缺失，旧响应和不明响应不能通过，错误原因可分别定位。
- [ ] 单独提交，建议说明：`以浏览器实际响应验证禅道验收运行版本`。

**采集生命周期示意：**

```typescript
const pending = new Set<Promise<void>>();
const collectorErrors: string[] = [];
context.on('response', response => {
  if (!manifest.matches(response.url())) return;
  const task = captureActualResponse(response)
    .then(record => journal.append(record))
    .catch(error => { collectorErrors.push(sanitizeCaptureError(error)); })
    .finally(() => pending.delete(task));
  pending.add(task);
});
// 该监听在 context.newPage 和首次导航前安装。
// 版本检查有界等待已声明目标，collectorErrors非空不能宣称证明完整。
```

不能通过扩大 `setResourceTimingBufferSize` 来代替这项测试，也不能把所有网络正文无限保存在内存中。

### Task 7: 固定业务模块、逐用例日志和 Reporter

**文件：**

- 新增 `e2e/src/specs/work-item.spec.ts`、`e2e/src/cases/registry.ts`、`e2e/src/cases/curve-validation.ts`。
- 新增 `e2e/src/reporters/ledger.ts`、`e2e/src/runtime/journal.ts`、`e2e/tests/results.spec.ts`。
- 修改 `scripts/e2e_results.py`，补齐强杀恢复与次要异常合并。

- [ ] 写红灯：AC/Task 映射缺失、计划 case 缺失、全 skipped、零测试、重复结果、业务失败后版本复查/清理又失败，均保留真实状态。
- [ ] 写浏览器用例注册测试：每个 case 有稳定 ID 和 required_phases，fixture前置失败与业务断言失败可以区分；通用 TimeoutError 不决定责任端。
- [ ] 运行 `npm --prefix $e2eTool run test:results` 及 `test_e2e_contract.py`，确认原末行 stdout 方案无法满足结果恢复。
- [ ] 实现按冻结计划注册 test、test.step 标注动作和预期；固定模块选择器优先 test id/角色，iframe 显式处理，复杂图表用经审查的业务断言。
- [ ] 实现同步追加的case开始/结束日志、原子结果快照、Reporter完整回执和证据索引；区分已开始中断与未开始，保留采集失败；屏蔽认证内容，不在硬杀时伪造完整Trace。
- [ ] 运行上述命令及宿主硬杀夹具，预期已完成结果不丢失，未完成部分有明确状态，退出码不覆盖业务结果。
- [ ] 单独提交，建议说明：`保存禅道验收逐项结果和首次失败证据`。

**结果合并示例：**

```json
{
  "case_id": "curve-detail-toolbox",
  "status": "failed",
  "failure_kind": "business",
  "primary_error": "restore icon remains visible",
  "secondary_errors": [
    {"kind": "cleanup", "reason": "temporary data restore failed"}
  ]
}
```

该示例只展示一个 case；正式回执必须包含完整身份、版本、资源和宿主事实。

### Task 8: 接回账本、返工、任务状态和统一重试

**文件：**

- 修改：[execution.py](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/skills/zentao-autodelivery/scripts/execution.py)、[route_execution.py](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/skills/zentao-autodelivery/scripts/route_execution.py)、[task_evidence.py](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/skills/zentao-autodelivery/scripts/task_evidence.py)、[task_sync.py](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/skills/zentao-autodelivery/scripts/task_sync.py)、[deploy_control.py](E:/RealtimeMonitoringandMultiwarehouseCollaboration/.agents/skills/zentao-autodelivery/scripts/deploy_control.py)。
- 新增 `tests/test_e2e_ledger.py`；扩展 `test_route_execution.py`、`test_route_evidence.py`。

- [ ] 写红灯：业务失败进入责任端修复，环境 blocked/error不生成代码返工；回执未核验、超时停止不明或未覆盖必需任务不能完成。
- [ ] 按设计5.3表逐条写修复序列红灯：本地同端、部署后同端、联合后台、部署后联合后台、integration冲突。断言新合回SHA和部署摘要被使用，阶段间无循环，后台更新使依赖前端与部署证据失效。
- [ ] 写重试红灯：账本单一计数、同因不变 retryable=false、主动停跑不恢复；明确 transient 恢复证据才允许普通重试。
- [ ] 运行 `test_e2e_ledger.py` 与原 route 模块，确认旧 AgentLimb 硬编码和旧返工顺序被测试覆盖。
- [ ] 实现统一回执消费、状态分类、逐任务结果、设计5.3明确返工序列和新合回版本登记；新增deployed_e2e进入允许返工、stage_role、计时与恢复映射。
- [ ] 核对execution和deploy_control的各释放点：阶段终结不释放合同2整单交付占用，最终本地收尾/有效隔离才释放。失败后的普通推送/部署按有效配置独立记账，不把终态失败变成success。
- [ ] 重跑上述测试，并用临时 SQLite 完整走一张前端 Bug、一张 Story 和跨端返工；预期无正式验收在合回前执行，未通过 Task 不关闭。
- [ ] 单独提交，建议说明：`将合回后验收结果接入返工与禅道任务状态`。

## Chunk 4: 后端、部署、恢复与灰度验收

### Task 9: 后端合回验收及部署后业务复验

**文件：**

- 新增 `scripts/backend_e2e_runner.py`、`scripts/backend_e2e_cases.py`、`tests/test_backend_e2e_runner.py`、`tests/test_deployed_e2e.py`。
- 修改 `delivery_scope.py`、`ledger.py`、`worker.py`、`task_evidence.py` 的阶段登记与调度。
- 复用既有后台验收命令、部署回执与前端发布清单；补齐部署 origin 白名单解析。

- [ ] 写红灯：后台acceptance只能验证成功合回版本，health/swagger/version命令通过不能代替业务case；运行服务/产物身份无法证明时不接受exit0；backend-api证明不要求frame，跨端gateway/后台SHA不符被阻断。
- [ ] 写无部署跨端生命周期红灯：后台验收结束Job停止；前端接收启动配方并在自己的Job重启同SHA后台；不可访问的发布后后台组合明确blocked，不能交接已停PID。
- [ ] 写部署复验红灯：Pod Ready但业务错误、镜像摘要/目标不符、多副本混合版本、远端入口未配置、部署失败和未启用部署分别正确分类。
- [ ] 运行 `test_backend_e2e_runner.py` 与 `test_deployed_e2e.py`，确认原阶段体系缺少部署业务复验时会失败。
- [ ] 实现后台运行清单和固定命令适配：合回SHA构建产物，记录摘要和启动配方，本次Job启动并核对PID/端口/版本。现有健康命令仅作前置检查；backend_e2e_cases.py负责与AC映射的业务输入、结果、回读和恢复。
- [ ] 实现无部署后台交接配方的校验与前端宿主重启，使用原合回版本和新Job所有权；有部署后台按实际digest/网关交接。
- [ ] 实现 frontend/backend.deployed_e2e：从配置选择允许 origin，绑定实际目标与 digest，复用业务模块的 deployed 用例，不拿本地摘要直接比生产构建响应。
- [ ] 重跑以上模块、`test_backend_deploy_runner.py`、`test_backend_deploy.py`、`test_deploy_control.py`；在测试环境核对完整读写回读和数据恢复，不新增实际部署目标。
- [ ] 单独提交，建议说明：`增加合回后台验收与部署后的真实业务复验`。

### Task 10: 历史快照迁移、回写故障与安全回退

**文件：**

- 新增 `tests/test_e2e_migration.py`、`tests/test_e2e_writeback.py`。
- 修改合同解析、worker、阶段映射和回执消费；复用现有 writeback/同步器协议。
- 测试：`test_writeback.py`、`test_route_drills.py`、`test_git_delivery.py`。

- [ ] 写红灯：旧快照活动中不热切换，历史 completed保留、主动停止不复活、候选通过不能冒充新合回验收；新旧引擎不并发争抢同一资源。
- [ ] 增加跨合同升级回归：A合同2验收后持有交付占用，B合同1的integration/push/deploy均被阻止；检测到已加载旧代码进程时不能开启新正式派发，排空并核实停止后才可开启。
- [ ] 写故障注入：测试禅道接口实际记录写入后丢失响应、附件错关联/下载摘要不符、推送成功但回执未登记、worker重启。
- [ ] 运行 `test_e2e_migration.py`、`test_e2e_writeback.py`，确认无重复外部动作或验收状态升格。
- [ ] 实现明确迁移事件：停止核验、原聊天/原单绑定、路由重算、先合回后新正式尝试；历史回执只读兼容，诊断结果不转正。既有合同的共享写入口遵守新占用，不改变其历史业务阶段含义。
- [ ] 实现灰度/回退开关e2e.formal_dispatch_enabled：先具备双合同读取及跨合同占用，再按原截止排空并核实停止不能遵守新占用的旧代码进程，最后才允许新合同派发。回退保留新路由/读取能力，不通过关闭e2e来假装通过。
- [ ] 重跑上述模块及原 writeback/drill/Git delivery测试；专用测试禅道联调须事先在既有授权中核对测试项目，缺少可写测试环境时记录外部验收未完成，不向生产原单制造验证数据。
- [ ] 单独提交，建议说明：`兼容历史验收记录并核验恢复回写的幂等性`。

### Task 11: Bug11 对照、全链路验证与新工单灰度

**文件：**

- 修改 `e2e/src/cases/curve-validation.ts`，补真实业务断言和试点输入校验；Task 7 已建立该模块。
- 必要时最小修改业务仓的只读图表测试适配点，并在该业务仓独立审查/提交。
- 修改项目配置、autodelivery SKILL 和 E2E/阶段/worker/分支/后台/发布指引、前端操作手册；审批后记录最终选定配置。

- [ ] 核对设计V01–V16的自动化覆盖及证据路径，包含跨阶段持久占用，尚未验证的项不能被“单测全绿”替代。
- [ ] 准备 Bug11 独立诊断输入，明确工区/井/数据集 ID、旧与修复提交、账号和恢复策略；缺输入即 blocked，不随意挑选可写生产记录。
- [ ] 核对旧/新版本使用兼容的同一观测方式；新增只读适配点不能使旧版因“适配点不存在”而失败，必要时使用两版共用、不改业务行为的外部观测层。
- [ ] 运行旧版/修复版同场景：四个图表直接图标断言、保留操作回归、切换数据/刷新重开；有效旧版红灯必须落在目标图标/业务行为上，认证/数据/适配点/版本证明失败不算复现成功。不得自动重启Bug11原工单。
- [ ] 分别执行真实 Windows 登录过期、1000+资源、错误版本、断网、超时强杀和异常恢复，保存首因、逐项结果与停止证据；验证重试不重置时限。
- [ ] 对专用测试 Story、Bug、纯后端和跨端路线跑端到端，核对最后 SHA、远端包含关系、实际部署摘要、业务回读、Task 状态与原单备注/附件。未授权或缺少环境的项目单列，阻止默认切换。
- [ ] 运行一次全量受影响 Python 测试、原 Node测试及本地 Playwright框架夹具；检查源码副作用和 `git diff --check`。只有出现新修改、失败或未决问题才扩大重复测试。
- [ ] 按明确灰度配置只让新接工单使用合同 2，观察有效样例；最终默认切换必须满足设计第 14 节全部必需条件。发布记录写中文变更范围、真实验证与残余限制。

**命令与预期：**

```powershell
python -m unittest discover -s $e2eTests -v
node --test .agents/skills/zentao-autodelivery/tests/test_agentlimb_e2e.cjs
npm --prefix $e2eTool run test:harness
git diff --check
```

`test:harness` 只访问本地夹具，不能隐式连真实禅道或业务数据。真实试点由受控宿主读取冻结请求运行，模式明确为 diagnostic；正式测试工单使用 formal，两者回执不可混用。

预期：测试结果与冻结计划完整对应、无源码副作用、无自有存活进程；旧业务版本按预期失败不能被汇总为框架回归失败，框架本身测试必须通过。

**提交与交付：** 分开提交框架、业务样例/适配点、最终配置和指引，分别说明各自验证。代码推送与发布按用户批准后的既有授权执行；不将无关 WIP 混入交付。

## 需求与任务追溯

| 设计要求 | 实施任务 | 退出证据 |
| --- | --- | --- |
| 合回后正式验收、脏主区合回 | 0、1、2、8 | V01–V03、路由/合回及返工回归。 |
| 固定合回版本、长路径、依赖与所有权 | 3、4 | V04–V05、完整装配与清理事实。 |
| 实际浏览器响应证明 | 6 | V05–V06、真实 HTTP/浏览器资源夹具。 |
| 认证、真实数据与直接业务断言 | 5、7、11 | V07、V15、旧/新版本对照及恢复。 |
| 首因、完整用例、超时和恢复 | 1、4、7、8、10 | V08–V10、Journal及宿主停止核验。 |
| 跨阶段共享交付占用 | 4、8、10 | V16、A交付中B同目标合回等待及停止/收尾释放。 |
| 后台交接、部署后复验 | 8、9、11 | V11–V12、实际目标/版本/业务回读。 |
| 禅道回写、幂等性与旧记录 | 8、10、11 | V13–V14、测试原单历史/附件读回。 |

## 评审、回退与用户批准点

文档阶段先完成独立设计与计划审查，修订内部矛盾后交用户审核。独立审查通过只表示文档可实施，不代表用户已批准修改。

实施阶段每个 Chunk 验证退出条件：Chunk 1 合同/顺序成立；Chunk 2 环境和停止可控；Chunk 3 真实用例和账本闭环成立；Chunk 4 才允许评估默认灰度。任何层面不能通过时保持未通过或待验证，修复该层，不自动换工具或吞掉必需用例。

用户当前需要审核：设计第 1 节决策、两端合回后验收顺序、部署复验要求、失败后的独立交付语义，以及本计划的改动范围与验证门槛。批准前不执行上述任何实施任务。
