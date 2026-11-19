# Windows 工单启动器

从项目根目录运行。启动器使用本机已安装并登录的 Codex App Server 发行版 CLI。每次 CLI 开工或恢复都显式传入 `--ask-for-approval never`，沿用宿主已配置的沙箱权限。启动器持有工单锁，阶段运行器保留 stdout、stderr、终态和 Windows 进程清理证据。

- `python -B .agents/skills/zentao-autodelivery/scripts/worker.py probe` 运行一次只读的实机探针。它要求 CLI 实际调用 shell 检查项目文件，根据工具输出和进程回执验证通过。不接单、不访问禅道。
- `python -B .agents/skills/zentao-autodelivery/scripts/worker.py prepare --marker <marker>` 仅用于已预占且未绑定的工单。以 `worker_ready` 返回的实际聊天 ID 为准。核对该聊天工作目录和实际桌面项目，尚未归组时用桌面项目工具挂载并读回，随后登记 `record bind`。遇到不明回执，先按账本待核对派发流程处理，再使用原有证据；只有确认没有创建聊天且该进程已停止，才允许新的派发请求重新准备。
- `python -B .agents/skills/zentao-autodelivery/scripts/worker.py run --marker <marker>` 在已绑定工单的原有会话里开始或续接。工单 Agent 用 `record runtime_check` 核对本轮实际策略，用 `record start` 领取执行权。已有活动尝试的情况下，先核对对应进程和账本，禁止重复启动。命令运行期间持续等待其回执；其他轮次遇到 `worker_busy` 时仅对账。回执缺失时根据受控进程和账本确认是否可恢复。

所有正式工单操作均由此入口启动。桌面聊天可以用来查看记录和核对状态。桌面发送消息工具不能覆盖工单轮次的实际审批策略。
