# E2E 环境准备与取证

登录、工区、测试数据选择及原单写法见[前端 E2E 操作手册](../../../../docs/前端E2E操作手册.md)。

纯前端及跨端工单在 `frontend.e2e` 运行本节。跨端先读取主会话交付的 `backend/handoff.md` 和服务合回版本，实际验证前端候选与该版本的业务交互。记录 `versions`、`backend_versions`、环境和请求/页面证据。旧环境只可作诊断。纯后端工单的 `backend.acceptance` 使用[服务验收入口](backend.md)。

工单配置快照的 `e2e.enabled` 为 `false` 时，账本省略 `frontend.e2e`，直接推进至前端合回，并在收尾注明页面验收已跳过。缺省为开启。

`e2e.after_integration` 启用后，新工单采用[合回后正式验收](#合回后正式验收)。从工单配置快照读取 `e2e` 设置及前端仓库。基座准备、登录、选工区和场景验证共同使用 E2E 尝试的时限，当前配置为 90 分钟。

预检和正式验收统一使用 AgentLimb。`e2e.agentlimb.intake_gate` 为 `false` 时，浏览器诊断不作为接单条件。正式验收仍需核对候选并执行本单用例。

## 合回后正式验收

1. 本单从前端合回前持有共享交付占用，其他共用主工作区、目标分支或端口的工单在进入合回前等待。本单完结后启动器续接等待单。
2. 新领取的工单编写一份 JSON 页面场景，提供 `entry_path` 和 `cases` 或 `steps`。需要选工区或展开菜单时，可填 `work_area` 和 `checks`。
3. 在回执给出的 `frontend/e2e-<attempt_id>/host-request.json` 只登记 `attempt_id` 和 `scenario`（相对工单目录的路径）。写入后结束本轮，宿主核对尝试、停止回执和交付占用。
4. 宿主从已合回的提交创建隔离目录并启动前端。浏览器复用配对的测试登录。宿主在本次各候选应用的 JavaScript 和 CSS 响应上加一次性标识，验收从浏览器已加载的响应中核对每个候选应用。缺少应用或发现新旧混用时记录环境受阻。结束时重新核对源和运行目录的提交，回收本次进程后清理临时源码。端口冲突会记录在回执中。
5. 主会话恢复后读本次运行目录的 `agentlimb.json`，按实际用例结果登记 `evidence.agentlimb_receipt`、`versions`、验收证据和逐任务结果。定位为功能问题时登记 `repair_required:true`，由对应开发 Agent 修复。前端修复使用 `main_frontend_repair` 返回的主工作区、目标 SHA 和受保护路径。用 `candidate_commit` 提交本单的字面文件，随后单测和复审生效。测试 Agent 用更新后的目标版本重跑原业务用例。达到时限或重试上限时登记实际结果及未执行原因。
6. 全部用例有最终结果且返工、复测结束后，继续已配置的前端推送和部署。E2E 最终失败或受阻时分别记录验收、推送和部署的实际结果。正式宿主请求已发出时，先等本次宿主回执与进程停止确认。宿主请求前已确认环境阻断时，登记 `evidence.e2e_blocker`，包含 `stage`、实际 `reason` 和 `cases:"not-run"`。当前结论由最终有效版本的验收回执决定，历史失败保留。

### 宿主请求和场景

宿主按项目配置的 `e2e.startup_routes` 选择服务。交会图启动平台入口、登录服务、工作台、`tet-smart-log` 和提供交会图绘图组件的 `micro-pure-model-page`（30044）。如果用例还会访问未配置的页面，或本单包含其他候选应用，宿主使用项目的完整启动命令。

在阶段回执给出的目录创建 UTF-8 编码的 `host-request.json`。

```json
{
  "attempt_id": "<当前尝试编号>",
  "scenario": "frontend/e2e-scenario.json"
}
```

工单快照没有 `e2e_attestation: "launch"` 时，沿用原 `attempt_id/profile/scenario/base_url` 请求格式和 `profile.candidate_resources` 中的 `app/url/sha256`。

自动核对版本的工单在宿主系统的临时目录检出源码，目录名按验收尝试生成。这样多层 npm 启动命令的工作路径与工单日志目录深度无关。启动日志和目录归属记录保存在本次工单目录，受控进程全部退出后由宿主核对归属并清理源码及 Git 临时目录。

场景文件是固定的 JSON 动作表。单场景在 `steps` 中登记动作，多场景在 `cases` 下逐项登记唯一的 `name` 和 `steps`，每项从 `navigate` 起步，使用相同基座 origin 下的相对路径。每项至少有一个业务断言，脚本按场景依次执行并记录结果。例如：

```json
{
  "entry_path": "/aggregation-platform/shell-hub/data-overview",
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
- 虚拟列表只渲染当前可见的一段。查找列表项时可给 `wait-for` 指定 `scroll_container`（列表滚动区域的 CSS 选择器），执行器逐屏向下查找至该步超时。
- 宿主停止后生成 `agentlimb.json`，其中 `scenario_cases` 按计划列出每项的 `name` 和 `status`，以及适用的 `check` 或受阻阶段。状态包括 `passed`、`failed` 和 `blocked`。主聊天按原尝试登记结果。

## AgentLimb 浏览器

在实际运行工单的账号下安装 [AgentLimb MCP 主机和 Chrome 扩展](https://github.com/hooosberg/AgentLimb#quick-start)。可沿用已登录业务平台的 Chrome 配置。在扩展侧栏查看配置 ID 或设置浏览器名称，与 `e2e.agentlimb.browser` 对应。扩展需要首次授权时，在侧栏点击「复制连接提示词」，并在配对窗口内运行本项目 `worker.py e2e-preflight`，配对的固定客户端是 `ZenTao E2E`。默认主机为该用户目录下的 `.agentlimb/host/mcp-server.mjs`。宿主可用 `ZENTAO_AGENTLIMB_HOST` 和 `ZENTAO_AGENTLIMB_BROWSER` 指定主机文件和测试浏览器 ID。执行器仅使用唯一匹配的配置，在自建标签页内操作，完成后关闭该标签页。不同工单的浏览器执行串行。

从项目根目录用 `worker.py e2e-preflight` 诊断连接与登录。预检地址已有本地服务时直接复用，端口空闲时从 `e2e.workspace` 执行 `e2e.start_command`，等待入口和配置的必要端口就绪。登录和选工区遵循公共手册。预检在 `data-overview` 检查「数据查询」和「多井柱状图」导航可见。阶段运行器回收本次启动的进程，原有开发服务继续运行。

页面控件在后台标签页等待超时后，若标签页仍处于隐藏状态，执行器将该测试标签页切到前台，重试同一控件检查一次。当测试结束且测试标签页仍在前台时，恢复此前的标签页。预检通过回执以 `foreground_retry: true` 标明实际使用了前台重试。

`--profile <JSON路径>` 可额外诊断指定业务页面，也可在正式验收中提供业务场景的测试档案。档案可以指定 `entry_path`、`work_area` 和 `checks`，每条检查包含 `name`、用于精确匹配的 `text`，以及可选的 `action`。`visible` 检查页面元素可见，`expand` 展开树节点，`click` 用于菜单、案例、页签、井或曲线等导航和选择。计算、归档和删除所需的专用数据由正式用例按其权限准备、核验和恢复。全程由工单执行并留证。

跨端工单若有自动部署结果，执行器按当前联调交接的网关启动候选前端，并在 AgentLimb 执行前后核对后端发布证据。当前 Rancher 直连配置会回读 Deployment、当前 ReplicaSet 的就绪 Pod 镜像摘要和服务健康状态。配置了网关版本接口的其他目标按该接口回读提交号。验收证据从实测回执填入 `gateway_url`（取 `frontend.gateway`）及 `backend_deployment`。

## 登录和工区

登录、工区选择、测试数据准备及原单写法统一见[前端 E2E 公共操作手册](../../../../docs/前端E2E操作手册.md)。执行器从工单配置快照读取入口，用例从禅道原单描述生成。

## 验收结果

按 `plan.md` 逐项运行。只有核实候选资源后出现的业务断言失败才按 `repair_required:true` 交回对应开发 Agent。连接、登录、候选启动、版本不符及测试数据问题按环境阻断或普通重试登记。保留单测和审查证据，E2E 结论由账本决定后续阶段。
