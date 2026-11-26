# E2E 环境准备与取证

项目入口、业务菜单及操作示例见[前端 E2E 操作手册](../../../../docs/前端E2E操作手册.md)。

纯前端及跨端工单在 `frontend.e2e` 运行本节。跨端先读取主会话交付的 `backend/handoff.md` 和服务合回版本，实际验证前端候选与该版本的业务交互。记录 `versions`、`backend_versions`、环境和请求/页面证据。旧环境只可作诊断。纯后端工单的 `backend.acceptance` 使用[服务验收入口](backend.md)。

工单配置快照的 `e2e.enabled` 为 `false` 时，账本省略 `frontend.e2e`，直接推进至前端合回，并在收尾注明页面验收已跳过。缺省为开启。

`e2e.after_integration` 启用后，新工单采用[合回后正式验收](#合回后正式验收)。旧路线快照按原阶段执行。从工单配置快照读取 `e2e` 设置及前端仓库。基座准备、登录、选工区和场景验证共同使用 E2E 尝试的时限，当前配置为 90 分钟。

预检和正式验收统一使用 AgentLimb，旧工单快照仅沿用其中的页面和预检配置。`e2e.agentlimb.intake_gate` 为 `false` 时，浏览器诊断不作为接单条件。正式验收仍需核对候选并执行本单用例。

## 合回后正式验收

1. 本单从前端合回前持有共享交付占用，其他共用主工作区、目标分支或端口的工单在进入合回前等待。本单完结后启动器续接等待单。
2. 前端合回后，准备本单用例和验收档案。档案 `candidate_resources` 逐应用登记 `app`、实际页面加载的本地资源 `url` 和可信的 `sha256`。预期指纹从已核对的合回版本生成。优先选择本次变更涉及的模块或包含该模块的产物，写明生成依据、应用和 SHA。
3. 在本单证据根目录下的 `frontend/e2e-<attempt_id>/host-request.json` 写入 `attempt_id`、`profile`、`scenario` 和 `base_url`。前两份文件都使用 JSON 文件，`profile` 与 `scenario` 写相对本单证据根目录的路径。`base_url` 使用配置的本地前端源。完成请求后结束本轮，Windows 启动器核对尝试、进程停止与占用，调用宿主执行器。
4. 宿主从主工作区核对每个本单应用的目标分支和合回 SHA，同时记录基座和已安装的其他配置应用当前版本。运行文件与登记版本一致时，已有服务先检查浏览器实际加载的资源，版本可证实时复用。主工作区有已暂存或未暂存改动、存在未忽略的新文件、索引带有 `assume-unchanged` 或 `skip-worktree` 标记，或当前检出的版本与目标不同，宿主在本次尝试目录检出各仓登记的 SHA，并连接已有的 `node_modules`。Windows 使用目录联接。宿主从主工作区启动配置命令，npm 以 `--prefix` 运行这一组精确版本的包和嵌套子应用，其他配置命令在精确版本目录运行。按 `e2e.startup_ports` 回收冲突监听后再启动，避免复用混合版本的服务。执行器复用已配对的 Chrome 登录，核对页面实际加载资源和验收结束时的目标分支及运行文件。使用主目录运行期间出现本地修改，且登记版本仍有效时，宿主在本次尝试的原时限内改用精确版本重跑一次场景。回执记录复跑原因和最终用例结果。目标分支已移动时按版本变化登记。宿主确认进程已停止后清理本次源码副本。
5. 主会话恢复后读本次运行目录的 `agentlimb.json`，按实际用例结果登记 `evidence.agentlimb_receipt`、`versions`、验收证据和逐任务结果。定位为功能问题时登记 `repair_required:true`，由对应开发 Agent 修复。前端修复使用 `main_frontend_repair` 返回的主工作区、目标 SHA 和受保护路径。用 `candidate_commit` 提交本单的字面文件，随后单测和复审生效。测试 Agent 用更新后的目标版本重跑原业务用例。达到时限或重试上限时登记实际结果及未执行原因。
6. 全部用例有最终结果且返工、复测结束后，继续已配置的前端推送和部署。E2E 最终失败或受阻时分别记录验收、推送和部署的实际结果。正式宿主请求已发出时，先等本次宿主回执与进程停止确认。宿主请求前已确认环境阻断时，登记 `evidence.e2e_blocker`，包含 `stage`、实际 `reason` 和 `cases:"not-run"`。当前结论由最终有效版本的验收回执决定，历史失败保留。

### 宿主请求和场景

在阶段回执给出的目录创建 UTF-8 编码的 `host-request.json`，传入当前尝试编号、工单目录下的两个 JSON 文件和配置的本地基座 origin。例如：

```json
{
  "attempt_id": "<当前尝试编号>",
  "profile": "frontend/e2e-profile.json",
  "scenario": "frontend/e2e-scenario.json",
  "base_url": "http://localhost:5000"
}
```

场景文件是固定的 JSON 动作表。单场景使用 `{"steps":[...]}`，多场景在 `cases` 下逐项登记唯一的 `name` 和 `steps`，每项从 `navigate` 起步，使用相同基座 origin 下的相对路径。每项至少有一个业务断言，脚本按场景依次执行并记录结果。例如：

```json
{
  "cases": [
    {
      "name": "default-empty-state",
      "steps": [
        { "action": "navigate", "path": "/aggregation-platform/shell-hub/data-overview" },
        { "action": "wait-for", "selector": "[data-test=chart-panel]" },
        { "action": "assert-count", "selector": "[data-test=select-well-prompt]", "count": 1 }
      ]
    }
  ]
}
```

- 动作 `navigate` 使用 `path`，`click` 使用 `selector`，`input` 使用 `selector` 和字符串 `value`。
- 等待与断言使用 CSS `selector`。支持 `wait-for`、`assert-visible`、`assert-hidden`、`assert-text` 和 `assert-count`。`assert-text` 增加 `text` 精确匹配文字，`assert-count` 增加非负整数 `count`。等待与断言可带 `timeout_ms`，范围 0 到 30000，默认 10000。步骤可用简短的小写英文标识 `name` 便于定位。
- `wait-for` 用于测试数据、导航或前置控件，超时登记环境受阻。预期业务行为用 `assert-*` 核对。提交失败回执前先核对实际版本和场景列表，已验证版本上的业务断言失败才交给开发 Agent。
- 宿主停止后生成 `agentlimb.json`，其中 `scenario_cases` 按计划列出每项的 `name` 和 `status`，以及适用的 `check` 或受阻阶段。状态包括 `passed`、`failed` 和 `blocked`。主聊天按原尝试登记结果。

## AgentLimb 浏览器

在实际运行工单的 Windows 用户下安装 [AgentLimb MCP 主机和 Chrome 扩展](https://github.com/hooosberg/AgentLimb#quick-start)。可沿用已登录业务平台的 Chrome 配置。在扩展侧栏查看配置 ID 或设置浏览器名称，与 `e2e.agentlimb.browser` 对应。扩展需要首次授权时，在侧栏点击「复制连接提示词」，并在配对窗口内运行本项目 `worker.py e2e-preflight`，配对的固定客户端是 `ZenTao E2E`。默认主机为该用户目录下的 `.agentlimb/host/mcp-server.mjs`。宿主可用 `ZENTAO_AGENTLIMB_HOST` 和 `ZENTAO_AGENTLIMB_BROWSER` 指定主机文件和测试浏览器 ID。执行器仅使用唯一匹配的配置，在自建标签页内操作，完成后关闭该标签页。不同工单的浏览器执行串行。

从项目根目录用 `worker.py e2e-preflight` 诊断连接与登录。预检地址已有本地服务时直接复用，端口空闲时从 `e2e.workspace` 执行 `e2e.start_command`，等待入口和配置的必要端口就绪。AgentLimb 复用测试配置的会话。如果跳回配置的本地登录页且 Chrome 已为配置账号填入密码，执行器尝试提交一次。外部 SSO 或需扫码、验证码等交互时，记 `waiting-login`，用户完成登录后再试。E2E 执行器不读取 Windows 凭据管理器。登录后选工区，在 `data-overview` 检查「数据查询」和「多井柱状图」导航可见。阶段运行器回收本次启动的进程，原有开发服务继续运行。

页面控件在后台标签页等待超时后，若标签页仍处于隐藏状态，执行器将该测试标签页切到前台，重试同一控件检查一次。当测试结束且测试标签页仍在前台时，恢复此前的标签页。预检通过回执以 `foreground_retry: true` 标明实际使用了前台重试。

`--profile <JSON路径>` 可额外诊断指定业务页面，也可在正式验收中提供业务场景的测试档案。档案可以指定 `entry_path`、`work_area` 和 `checks`，每条检查包含 `name`、用于精确匹配的 `text`，以及可选的 `action`。`visible` 检查页面元素可见，`expand` 展开树节点，`click` 用于菜单、案例、页签、井或曲线等导航和选择。计算、归档和删除所需的专用数据由正式用例按其权限准备、核验和恢复。全程由工单执行并留证。

旧路线的合回前正式验收在本单 E2E 尝试中用阶段运行器执行 `python -B .agents/skills/zentao-autodelivery/scripts/agentlimb_e2e.py --marker <marker> --base-url <候选源> --profile <JSON路径> --scenario <CJS路径> --startup-log <日志路径>`。仅子应用有工单工作树时，加 `--candidate-workspace <装配基座路径>`，由受控启动命令从这个基座启动。基座和待验子应用端口须空闲。档案有 `checks`，并为每个改动应用提供至少一条 `candidate_resources`，每条包含应用键 `app`、应由该页面实际加载的本地资源绝对 `url` 和候选产物的 `sha256`。先从已核对的候选环境取得资源指纹，验收时比对页面资源记录和实际响应。场景脚本导出 `async ({ browser }) => { ... }`，可调用 `browser.evaluate(expression)`、`browser.navigate(url)`、`browser.click(selector)`、`browser.input(selector, value)` 和 `browser.wait(options)`。只读断言使用 `node:assert/strict`，附脱敏复现证据。工单通过或触发代码返工时，在 `evidence.agentlimb_receipt` 提供本次阶段的 AgentLimb JSON stdout 文件，账本核对工单、尝试 ID、候选版本和资源覆盖。

跨端工单若有自动部署结果，执行器按当前联调交接的网关启动候选前端，并在 AgentLimb 执行前后核对后端发布证据。MCP 路线读回 Deployment 模板摘要、Ready Pod 和服务健康，kubeconfig 路线读回网关 HTTP 版本。验收证据从实测回执填入 `gateway_url`（取 `frontend.gateway`）及 `backend_deployment`。

## 基座应用的旧路线说明

1. 根据 `e2e.ready_url`、已打开的业务页面或启动设置识别前端地址。服务已启动时直接复用。优先查看已登录的浏览器页面及应用内导航。需要额外做 HTTP 探测时，页面请求显式使用 `Accept: text/html`，例如 `Invoke-WebRequest -Uri $browserEntry -Headers @{ Accept = 'text/html' } -TimeoutSec 8 -SkipHttpErrorCheck`。缺少 `Accept` 或按 JSON 请求的深层路由可能返回 `404`，应以实际浏览器访问和页面功能作准备结论。HTML 返回 `200` 仅证明前端入口可访问。
2. 确认服务尚未启动时，才从本单 worktree 对应的基座目录执行 `e2e.start_command`。本次启动的服务纳入当前尝试的受控进程登记。`ready_url` 为空时，从本次启动输出或现有浏览器页面确认前端入口。网关参数用于启动配置。
3. 记录复用服务的实际工作区和能核验的代码版本。涉及子应用时，核对基座加载的子应用。运行中的版本和候选版本无法证实一致时，仍执行可做的现有页面验证，单独记录结果，并注明候选版本仍待验证。交给合回步骤时，分别标明本单候选提交、已验证场景和待验证项。
4. 入口或场景无法执行时记录阻断，继续其余可执行的场景、报告和回写。保留复用的会话、工区、服务及用户已有待保存操作。超时按 [受控停止](stages.md#超时和受控停止) 处理本次自有资源。

## 登录和工区

1. 从实际基座地址解析 `e2e.login.path` 和 `e2e.platform_entry_path`。在已配对的指定 Chrome 配置中复用会话或允许 Chrome 填充测试账号，认证需人工操作时留 `waiting-login` 回执。
2. 有已选工区时直接沿用，包括登录后平台自动恢复的工区。当前会话没有工区时，在平台首页按 `e2e.default_work_area.id` 选择配置的默认工区，确认选中生效后再进入菜单。已有工区即使与默认配置不同也以实际当前工区为准。
3. 因地址、登录或工区准备受阻时，记录实际失败的环节和必要证据。重试从环境检查开始，逐项复用已经就绪且仍符合版本及会话要求的部分。

## 验收结果

按 `plan.md` 逐项运行。只有核实候选资源后出现的业务断言失败才按 `repair_required:true` 交回对应开发 Agent。连接、登录、候选启动、版本不符及测试数据问题按环境阻断或普通重试登记。保留单测和审查证据，E2E 结论由账本决定后续阶段。
