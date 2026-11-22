# Windows 工单启动器

领取后主会话读取该单快照与当前有效尝试。新单先检查材料、拆分任务，再执行账本选中的端；后端与前端各由开发、测试 Agent 交付证据，主会话负责两端分支核验和合回。跨端依赖与恢复细节见[步骤指引](stages.md)。沿原工单聊天续接时按账本阶段派工。

从项目根目录运行。启动器使用本机已安装并登录的 Codex App Server 发行版 CLI。每次 CLI 开工或恢复都显式传入 `--ask-for-approval never`。启动器持有工单锁，阶段运行器保留 stdout、stderr、终态和 Windows 进程清理证据。

Windows MXC 在可写工作区内仍会保护 Git 元数据。对于已获准执行本地工单分支、提交和合回的服务或应用，用 `runtime.worker_git_write_repositories` 按 `backend:服务键`、`frontend:应用键` 显式登记。启动器核对当前配置和领取快照中解析到的仓库路径一致，再只授予该仓库自己的 `.git` 或对应子模块元数据目录。该规则不扩大只读依赖或父仓库其他 Git 路径的权限。

配置了 `backend_dependencies` 时，启动器为该工单 CLI 启用 Windows MXC 权限配置。只有当前项目配置和工单快照均将同一依赖路径设为 `writable` 时，启动器才会为其授予写权限。项目、已配置的前后端工作区、Codex 运行目录及临时目录提供正常的写入空间，`runtime.worker_cache_paths` 另外登记工作区外的构建缓存目录，支持 `~` 和系统环境变量。阶段 CLI 由本工单入口在同一受限进程树内启动。新增依赖后可以先跑 `probe` 确认配置可加载。MXC 无法启动时按现有权限阻断规则处理。

- `python -B .agents/skills/zentao-autodelivery/scripts/worker.py probe` 运行一次只读的实机探针。它要求 CLI 实际调用 shell 检查项目文件，根据工具输出和进程回执验证通过。不接单、不访问禅道。
- `python -B .agents/skills/zentao-autodelivery/scripts/worker.py e2e-preflight` 执行默认页面预检。已有本地前端时复用，端口空闲时按项目启动命令拉起服务，再用独立 Playwright 浏览器核验登录、工区及页面控件。退出码 `0` 且回执为 `e2e_preflight_passed` 表示通过。命令不领取工单。`--profile <JSON路径>` 可用于单独诊断指定业务页面。 [E2E 阶段指引](e2e.md#独立-playwright-浏览器)
- `python -B .agents/skills/zentao-autodelivery/scripts/worker.py prepare --marker <marker>` 仅用于已预占且未绑定的工单。以 `worker_ready` 返回的实际聊天 ID 为准。核对该聊天工作目录和实际桌面项目，尚未归组时用桌面项目工具挂载并读回，随后登记 `record bind`。遇到不明回执，先按账本待核对派发流程处理，再使用原有证据；只有确认没有创建聊天且该进程已停止，才允许新的派发请求重新准备。
- `python -B .agents/skills/zentao-autodelivery/scripts/worker.py run --marker <marker>` 在已绑定工单的原有会话里开始或续接。工单 Agent 用 `record runtime_check` 核对本轮实际策略，用 `record start` 领取执行权。已有活动尝试的情况下，先核对对应进程和账本，禁止重复启动。命令运行期间持续等待其回执；其他轮次遇到 `worker_busy` 时仅对账。回执缺失时根据受控进程和账本确认是否可恢复。

所有正式工单操作均由此入口启动。桌面聊天可以用来查看记录和核对状态。桌面发送消息工具不能覆盖工单轮次的实际审批策略。

当阶段进入 `backend.deploy`（含 E2E 返工分段）或 `frontend.deploy`，启动器在工单 Agent 轮次停止后驱动对应的独立发布执行器。发布执行器自行认领账本尝试、启动受控发布脚本并提交结果。已核实的结束证据可以续接原尝试，缺少停止证据时保留占用并报告阻断。发布结果入账后，启动器从原会话继续联调或收尾。

## 拆分任务同步

已启用任务同步的新 Story 在 `run` 期间，由启动器同时监管研发 CLI 和拆分 Agent 的 Task 同步进程。同步进程核对固定会话、工单执行状态和会话无人值守权限，使用独立 Windows Job Object。研发 CLI 返回后，启动器停止同步进程，核实其进程树退出；回执中的 `task_sync_receipt` 指向本轮证明文件。若外部请求尚未核实，操作留待后续对账。

每轮正式监测在原聊天执行：

```powershell
python -B .agents/skills/zentao-autodelivery/scripts/worker.py sync-tasks
```

命令从当前会话的 `CODEX_THREAD_ID` 核对实际权限，并按本项目最近确认的禅道实例及项目身份筛选待办，包括已收尾工单。一个批次最多处理 32 项，截止时间为 3 分钟。可在命令返回的回执目录查看执行结果。工单同步和监测补办共用逐项租约，允许同时推进各自有权领取的操作。
