# 前端镜像发布

启用后，纯前端和前后端工单都在前端验证及合回成功后进入 `frontend.deploy`。主会话读回实际合回 SHA 后交还 Windows 工单启动器。前端部署 Agent 在受控进程组逐仓核对及推送，确认本地前端退出，再构建和发布到 Harbor。

1. 使用本单快照的应用键、目标分支及合回 SHA。逐仓核对 `origin` 有唯一且一致的读取与推送端点。核对远端分支包含本单提交，必要时普通推送并重读远端。出现分叉、权限失败或目标不明时，保留已确认回执并结束当前尝试。
2. 阶段运行器收尾本单启动的前端进程。发布脚本检查配置的本地端口。如果还有监听，回执日志列出占用端口和 PID，等待所属进程退出再续跑。
3. 在隔离目录检出实际 SHA，调用本机 `customized-release/bundle-build.cjs`。用 `zentao-ai-development` 配置构建本单应用，局部替换显式指定的基础镜像。自动标签为工单和输入版本的指纹加 `-zentao-ai-development`。
4. 推送前持久化标签和镜像配置摘要。Harbor 读回确认配置摘要、镜像平台、镜像摘要。本单已有匹配回执时先读回并复用，避免重复打包或推送。发布阶段结束时汇总源码 SHA 和镜像摘要。

## 启用配置

在 `.agents/zentao-project.json` 的 `development.frontend_bundle.deployment` 设置 `image_repository`、`platform`、`local_ports` 与 `base_image`，核实后将 `enabled` 改为 `true`。基础镜像填写可信的 `仓库@sha256:…`。每次准备局部发布时，核对底图是否包含预期的其他应用版本。默认 `image_mode=build`，续跑需要限定使用已有发布回执时可改为 `reuse`。前端阶段时限可由 `development.step_timeout_minutes["frontend.deploy"]` 配置。

部署主机需要当前工作区的 `aggregation-platform-4`、本机 `customized-release` 及其禅道分支映射、Git 凭据、Node.js、Python、Docker 和 Harbor 凭据。工单的逐仓、镜像和停止证据保存在 `items/<marker>/frontend-releases/`。
