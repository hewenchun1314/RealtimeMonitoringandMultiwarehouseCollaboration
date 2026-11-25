# E2E 环境准备与取证

项目入口、业务菜单及操作示例见[前端 E2E 操作手册](../../../../docs/前端E2E操作手册.md)。

纯前端及跨端工单在 `frontend.e2e` 运行本节。跨端先读取主会话交付的 `backend/handoff.md` 和服务合回版本，实际验证前端候选与该版本的业务交互。记录 `versions`、`backend_versions`、环境和请求/页面证据。旧环境只可作诊断。纯后端工单的 `backend.acceptance` 使用[服务验收入口](backend.md)。

工单配置快照的 `e2e.enabled` 为 `false` 时，账本省略 `frontend.e2e`，直接推进至前端合回，并在收尾注明页面验收已跳过。缺省为开启。

E2E 在合回前执行。从工单配置快照读取 `e2e` 设置及当前前端开发仓库。基座准备、登录、选工区和场景验证共同使用本次 E2E 的单次最长耗时，当前配置为 90 分钟。

预检和正式验收统一使用 AgentLimb，旧工单快照仅沿用其中的页面和预检配置。`e2e.agentlimb.intake_gate` 为 `false` 时，浏览器诊断不作为接单条件。正式验收仍需核对候选并执行本单用例。

## AgentLimb 浏览器

在实际运行工单的 Windows 用户下安装 [AgentLimb MCP 主机和 Chrome 扩展](https://github.com/hooosberg/AgentLimb#quick-start)。可沿用已登录业务平台的 Chrome 配置。在扩展侧栏查看配置 ID 或设置浏览器名称，与 `e2e.agentlimb.browser` 对应。扩展需要首次授权时，在侧栏点击「复制连接提示词」，并在配对窗口内运行本项目 `worker.py e2e-preflight`，配对的固定客户端是 `ZenTao E2E`。默认主机为该用户目录下的 `.agentlimb/host/mcp-server.mjs`。宿主可用 `ZENTAO_AGENTLIMB_HOST` 和 `ZENTAO_AGENTLIMB_BROWSER` 指定主机文件和测试浏览器 ID。执行器仅使用唯一匹配的配置，在自建标签页内操作，完成后关闭该标签页。不同工单的浏览器执行串行。

从项目根目录用 `worker.py e2e-preflight` 诊断连接与登录。预检地址已有本地服务时直接复用，端口空闲时从 `e2e.workspace` 执行 `e2e.start_command`，等待入口和配置的必要端口就绪。AgentLimb 复用测试配置的会话。如果跳回配置的本地登录页且 Chrome 已为配置账号填入密码，执行器尝试提交一次。外部 SSO 或需扫码、验证码等交互时，记 `waiting-login`，用户完成登录后再试。E2E 执行器不读取 Windows 凭据管理器。登录后选工区，在 `data-overview` 检查「数据查询」和「多井柱状图」导航可见。阶段运行器回收本次启动的进程，原有开发服务继续运行。

页面控件在后台标签页等待超时后，若标签页仍处于隐藏状态，执行器将该测试标签页切到前台，重试同一控件检查一次。当测试结束且测试标签页仍在前台时，恢复此前的标签页。预检通过回执以 `foreground_retry: true` 标明实际使用了前台重试。

`--profile <JSON路径>` 可额外诊断指定业务页面，也可在正式验收中提供业务场景的测试档案。档案可以指定 `entry_path`、`work_area` 和 `checks`，每条检查包含 `name`、用于精确匹配的 `text`，以及可选的 `action`。`visible` 检查页面元素可见，`expand` 展开树节点，`click` 用于菜单、案例、页签、井或曲线等导航和选择。计算、归档和删除所需的专用数据由正式用例按其权限准备、核验和恢复。全程由工单执行并留证。

正式验收在本单 E2E 尝试中用阶段运行器执行 `python -B .agents/skills/zentao-autodelivery/scripts/agentlimb_e2e.py --marker <marker> --base-url <候选源> --profile <JSON路径> --scenario <CJS路径> --startup-log <日志路径>`。仅子应用有工单工作树时，加 `--candidate-workspace <装配基座路径>`，由受控启动命令从这个基座启动。基座和待验子应用端口须空闲。档案有 `checks`，并为每个改动应用提供至少一条 `candidate_resources`，每条包含应用键 `app`、应由该页面实际加载的本地资源绝对 `url` 和候选产物的 `sha256`。先从已核对的候选环境取得资源指纹，验收时比对页面资源记录和实际响应。场景脚本导出 `async ({ browser }) => { ... }`，可调用 `browser.evaluate(expression)`、`browser.navigate(url)`、`browser.click(selector)`、`browser.input(selector, value)` 和 `browser.wait(options)`。只读断言使用 `node:assert/strict`，附脱敏复现证据。工单通过或触发代码返工时，在 `evidence.agentlimb_receipt` 提供本次阶段的 AgentLimb JSON stdout 文件，账本核对工单、尝试 ID、候选版本和资源覆盖。

跨端工单若有自动部署结果，执行器按当前联调交接的网关启动候选前端，并在 AgentLimb 执行前后核对后端发布证据。MCP 路线读回 Deployment 模板摘要、Ready Pod 和服务健康，kubeconfig 路线读回网关 HTTP 版本。验收证据从实测回执填入 `gateway_url`（取 `frontend.gateway`）及 `backend_deployment`。

## 基座应用

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
