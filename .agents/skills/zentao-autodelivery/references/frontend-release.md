# 前端镜像发布

启用后，纯前端和前后端工单都在前端开发、全部计划仓库合回及已配置的 Git 推送成功后执行 `frontend.deploy`。测试失败保留在账本和收尾记录中，发布继续执行。前端部署 Agent 在受控进程组逐仓核对远端，确认本地前端退出，再构建和发布。

1. 使用本单快照的全部计划应用、目标分支及合回 SHA。已有 `frontend.push` 阶段的工单须先取得整包范围的 Git 回执，部署时逐仓核对远端仍包含本单提交。旧快照沿用本阶段的普通推送及读回操作。两种流程均要求 `origin` 的读取与推送端点唯一且一致。
2. 阶段运行器收尾本单启动的前端进程。发布脚本检查配置的本地端口。如果还有监听，回执日志列出占用端口和 PID，等待所属进程退出再续跑。
3. 在隔离目录检出实际 SHA，调用本机 `customized-release/bundle-build.cjs`。用 `zentao-ai-development` 配置构建本单应用。配置 Rancher 时，以目标当前模板的 Harbor 镜像摘要为底图做局部替换。自动标签为工单和输入版本的指纹加 `-zentao-ai-development`。
4. 推送前持久化标签和镜像配置摘要。Harbor 读回确认配置摘要、镜像平台、镜像摘要。本单已有匹配回执时先读回并复用。
5. 配置 Rancher 时，通过 MCP 预检并更新本单镜像摘要，核对模板和新 Ready Pod 后才结束发布。底图的原镜像在打包后被替换时暂停更新，原发布记录用于对账。

## 启用配置

在 `.agents/zentao-project.json` 的 `development.frontend_bundle.deployment` 设置 `image_repository`、`platform`、`local_ports` 与 `base_image`。基础镜像填写可信的 `仓库@sha256:…`，配置 `rancher` 时该值用于只读查询模板原镜像。当前目标为 `internalrd/cnooced-suite-web`。默认 `image_mode=build`，续跑需要限定使用已有发布回执时可改为 `reuse`。前端阶段时限可由 `development.step_timeout_minutes["frontend.deploy"]` 配置。

部署主机需要当前工作区的 `aggregation-platform-4`、本机 `customized-release` 及其禅道分支映射、Git 凭据、Node.js、Python、Docker 和 Harbor 凭据。工单的逐仓、镜像、停止及 Rancher 证据保存在 `items/<marker>/frontend-releases/`。
