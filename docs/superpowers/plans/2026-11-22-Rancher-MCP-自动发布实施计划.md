# Rancher MCP 自动发布实施计划

**目标** 在前后端 Harbor 发布后自动更新 Rancher，并取得可审计的滚动发布回执。

**架构** Windows 工单启动器调度现有发布执行器，执行器调用共享的 MCP 客户端。SQLite 账本校验配置快照、目标、镜像摘要和新 Pod 证据。

**技术栈** Python、MCP Streamable HTTP、Harbor、Kubernetes Deployment。

参见[设计](../specs/2026-11-22-Rancher-MCP-自动发布设计.md)。

- [x] 在 `.agents/skills/zentao-autodelivery/scripts/rancher_mcp.py` 实现协议、预检、目标核对、执行和滚动读回。覆盖目标漂移、Pod 未就绪和写回中断后的恢复。
- [x] 接入 `frontend_release.py` 与 `backend_deploy.py`，校验账本回执，更新联调和 Playwright 的版本探针。
- [x] 对两个工作负载使用已在运行镜像的等价摘要验证写权限。Smart-Geo 新 Pod `smart-geo-new-64bf8c5c8b-txk2m` 和前端新 Pod `cnooced-suite-web-5d688bc8bd-lnczm` 均为 Ready。从 Rancher 独立读回两个 Pod 的 `imageID`，与 Harbor 摘要一致。Smart-Geo 直连健康接口返回 `UP`。原始写入回执保存在本机 `.local/zentao-delivery/mcp-write-check/`。
- [x] 验证项目配置。完整工作流回归 183 项通过，MCP、前后端发布、账本和联调相关的 57 项定向测试通过。用实际集群再次只读核对两个目标的模板和 Ready Pod。
