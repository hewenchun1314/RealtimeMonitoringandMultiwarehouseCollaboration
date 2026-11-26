# 后端服务验收与联调交接

仅处理账本选中后端的工单。项目通过 `workspaces.backend` 指定后端工作区，`development.backend_bundle` 包含 `name` 和按配置键组织的 `services`。每条服务设置相对于工作区的 `repository`、目标 `branch`、`build_command`、`test_command`、`acceptance_command`，以及 `start_command` 或已有服务的 `service_url`。运行配置只登记项目认可的入口和凭据引用。

1. 后端开发 Agent 在 `backend.plan` 提交涉及的服务键。主会话核验目标分支和工作树后，开发 Agent 在该 worktree 实现并构建，运行服务单测，逐服务记录命令、结果、候选 SHA。跨端工单在本单证据目录编写 `round-<n>/backend/handoff.md`，列出关联任务、接口约定、版本、启动/连接方式、测试数据准备、实际成功请求及响应、错误场景和已确认的变化。
2. 后端测试 Agent 独立审查候选改动，按服务实际入口执行 `acceptance_command` 并读回证据。用版本接口、受控启动工作树或等价方式确认测试对象。后台任务记录输入、执行结果与持久化状态。复用已有服务时保留用户原进程。本次启动的进程由阶段运行器管理。
3. 主会话逐仓核对候选版本、目标分支和合回结果。多仓部分成功时记录各仓实际提交，重试先读回。全部计划仓库合回成功后，由 Windows 工单启动器执行已配置的推送和部署。合回最终失败或仅部分成功时，工单以「阻断」收尾。
4. 前后端工单额外执行 `handoff`。核对所有必需后端任务、审查、验收、合回提交、可调用版本。提交 `evidence.document`（实际账本同级 `items/<marker>/round-<n>/backend/handoff.md` 绝对路径）、`backend_versions`（服务键到已合回提交）、`environment`（环境地址或可定位的本地实例）、`verification`（调用及版本核验事实）。后端候选与合回内容有差异时，独立复验受影响行为。
5. 账本交接通过后，主会话把前端任务、联调文档和版本交给前端开发 Agent。后端新增修复进入联调时更新文档与实际版本，再验证受影响的前端行为。对于旧环境、失效接口或缺少必需仓库等情况，记录具体依赖和恢复依据。

## 后端自动部署

按服务在 `development.backend_bundle.services.<服务键>.deployment` 配置 `enabled`、所选 `target` 和 `targets`。目标登记 Harbor 仓库、平台、集群、命名空间、工作负载、容器和业务网关。Smart-Geo 的 `deployment.build_dependencies` 用经验证的 Git SHA 固定 `tet-common-java`。

工单启动器从账本接手于 `backend.deploy` 的工单，核对当前授权和领取快照，用合回 SHA 构建并推送 Harbor。按镜像标签和工作负载互斥，镜像标签冲突时保留证据。

- 当前 Smart-Geo 目标用 `mcp_url` 更新 Rancher。发布脚本验证 Harbor 镜像的提交标签和摘要，调用 MCP 等待新 Pod 就绪并核对 Deployment 模板的镜像引用。直连 `health_url` 还须报告服务健康。
- kubeconfig 路线填写 `kube_context` 和 `verification_path`，核对实际 Pod 镜像 ID 和 HTTP 版本字段，默认读取 `git.commit.id`。需要认证的版本探针使用 HTTPS 和可选 `http_credential_ref`、`http_username`。

部署失败登记「待验证」。镜像已推送或 Deployment 已更新时，原尝试证据供重试对账。

跨端启用部署时，`handoff` 在已有字段之外提交发布回执中的 `deployment` 服务映射及共同 `gateway_url`，前端联合验收从该网关重验完整后端版本。有关已安装的发布能力和实施上下文见[部署设计](../../../../docs/superpowers/plans/2026-11-22-禅道后端自动部署需求设计与实施计划.md)。

## 上游依赖与修改权限

项目可在 `backend_dependencies` 登记业务后端按需调用的上游服务。`source_path` 相对于项目配置文件，`api_source` 相对于该依赖目录，可指向接口源码、OpenAPI 文件或契约目录。`used_by` 填消费方服务键，可以引用业务后端或另一个可写上游。配置随新工单领取快照保存。

`access` 支持以下两种取值。

- `read_only` 是缺省值。依赖源码与 Git 数据供查阅。需要修改该依赖时，在工单里记录受影响的任务和具体接口。
- `writable` 允许将依赖键加入后端计划的 `services`。在依赖的 `delivery` 下登记 `branch`、`build_command`、`test_command`、`acceptance_command`，以及 `start_command` 或 `service_url`。仓库位置取自 `source_path`。选中后使用依赖自己的目标分支和工单工作树，沿正常后端流程实现、验证和合回。当前配置和领取快照均须允许写入。当前配置撤销授权后，账本会拒绝准备或核验该依赖的工作树，暂停的工单续跑也会重新应用只读保护。

后端设计和计划回执提供候选依赖位置及访问模式。选定业务服务后的后端回执按计划服务过滤这些位置。主会话把 `backend_dependencies` 原样交给负责当前后端阶段的 Agent。Agent 先定位本次需求涉及的业务服务 Controller、Service 和已有客户端调用。业务服务本地实现能完成需求时直接沿本地链路设计和验证。只有涉及实际的上游调用或明确需要上游能力时，才查相应契约，在计划和验收证据里记下消费方调用点、上游接口及实际核验结果。工单需要修改可写依赖时才把它列入计划。

上游连接地址仍由业务后端环境配置提供。前端使用业务后端提供的接口，交接文档给出前端可调用的业务契约和版本。
