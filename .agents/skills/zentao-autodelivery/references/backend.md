# 后端服务验收与联调交接

仅处理账本选中后端的工单。项目通过 `workspaces.backend` 指定后端工作区，`development.backend_bundle` 包含 `name` 和按配置键组织的 `services`。每条服务设置相对于工作区的 `repository`、目标 `branch`、`build_command`、`test_command`、`acceptance_command`，以及 `start_command` 或已有服务的 `service_url`。运行配置只登记项目认可的入口和凭据引用。当前项目启用纯前端，接入时再登记真实服务。

1. 后端开发 Agent 在 `backend.plan` 提交涉及的服务键。主会话核验目标分支和工作树后，开发 Agent 在该 worktree 实现并构建，运行服务单测，逐服务记录命令、结果、候选 SHA。跨端工单在本单证据目录编写 `round-<n>/backend/handoff.md`，列出关联任务、接口约定、版本、启动/连接方式、测试数据准备、实际成功请求及响应、错误场景和已确认的变化。
2. 后端测试 Agent 独立审查候选改动，按服务实际入口执行 `acceptance_command` 并读回证据。用版本接口、受控启动工作树或等价方式确认测试对象。后台任务记录输入、执行结果与持久化状态。复用已有服务时保留用户原进程。本次启动的进程由阶段运行器管理。
3. 主会话逐仓核对候选版本、目标分支和合回结果。多仓部分成功时记录各仓实际提交，重试先读回。纯后端工单在本端合回后收尾。
4. 前后端工单额外执行 `handoff`。核对所有必需后端任务、审查、验收、合回提交、可调用版本。提交 `evidence.document`（实际账本同级 `items/<marker>/round-<n>/backend/handoff.md` 绝对路径）、`backend_versions`（服务键到已合回提交）、`environment`（环境地址或可定位的本地实例）、`verification`（调用及版本核验事实）。后端候选与合回内容有差异时，独立复验受影响行为。
5. 账本交接通过后，主会话把前端任务、联调文档和版本交给前端开发 Agent。后端新增修复进入联调时更新文档与实际版本，再验证受影响的前端行为。对于旧环境、失效接口或缺少必需仓库等情况，记录具体依赖和恢复依据。
