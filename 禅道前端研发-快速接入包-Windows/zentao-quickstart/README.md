# 禅道前端研发 · Windows 快速接入

把一条指派给项目账号的禅道需求，依次送入材料评审、设计、计划、分支核验、开发、单测、代码审查、浏览器验收、合回和禅道回写。每个项目用一个独立工作区，默认一次处理一单。

## 1　解压后打开这个文件夹

将 ZIP 完整解压到长期使用的位置。在 Codex 桌面端把**同时含有 README.md、AGENTS.md、quickstart.py 和 .agents 的文件夹**打开为项目。以下命令都在这个文件夹的 PowerShell 里执行。

- 使用 Windows、已登录的 Codex 桌面端（含 App Server CLI）、Python 3.11 或更高版本和 Git。接入脚本只用 Python 标准库。
- 克隆需求仓库和前端仓库，按前端仓库自己的说明安装 Node 和依赖。需要子仓时一并检出并取得目标分支。本机应能访问禅道、Git 和测试环境。
- 确认项目聊天能使用桌面项目、自动化和浏览器工具。E2E 也支持项目已安装的 Playwright 及其浏览器。工单启动器使用当前用户已有的 Codex 登录。

## 2　只改一份配置

用编辑器打开 [.agents/zentao-project.json](.agents/zentao-project.json)。这就是可直接编辑的模板。首次接入保留 `monitor.enabled = false`。账号区分大小写。

| 配置位置 | 填什么 |
| --- | --- |
| `zentao` | 填可访问的 `base_url`（含实际部署前缀）、禅道项目全名和 `assigned_to_accounts` 接单账号列表。模板接收 `reviewing`、`active` 状态的需求。 |
| `workspaces` | 填需求仓库和前端仓库目录。推荐 Windows 绝对路径，例如 `D:/src/specs` 和 `D:/src/web`。相对路径从 **.agents 所在目录内**解析，即 `../frontend-repo` 指解压根目录下的 `frontend-repo`。 |
| `development.frontend_bundle.applications` | 单仓项目可用一项，例如 `"web": {"repository": ".", "branch": "main"}`。换成实际应用键和合回分支。多仓项目每个独立 Git 仓库一项，路径相对于 `workspaces.frontend`，例如 `apps/report`。 |
| `e2e` | 按实际项目填写启动命令、测试前端 URL、业务入口路径。需登录则填登录路径和测试用户名，公开入口可设 `login` 为 `null`。项目需要选择工区时，将 `default_work_area` 改为 `{"id":"实际ID","name":"实际名称"}`。 |
| `runtime` | 保留模板即可。监测聊天从桌面核对当前工作区对应的项目 ID。若需固定绑定，可请 Codex 核对并填写 `runtime.desktop_project_id`。 |

保留两个 `credential_ref` 的模板值。需求接单账号与禅道登录账号可以不同，登录账号需要能读取目标项目和目标工单。记录备注、上传附件还需要相应权限。模板范围为纯前端需求，Bug 接收默认关闭。

## 3　录入本机凭据并自检

在**自己的 PowerShell 窗口**逐行执行。第一条会提示输入禅道登录账号、密码，以及配置中需要的 E2E 密码。密码输入时不会显示。

```powershell
python -B quickstart.py credentials
python -B quickstart.py check
python -B quickstart.py login-test
python -B .agents/skills/zentao-autodelivery/scripts/worker.py probe
```

依次看到“凭据已保存”“本机配置检查通过”“禅道登录成功”和 `probe_passed` 即可进行项目联调。凭据以 Windows 当前用户的 DPAPI 加密，保存在本工作区的 `.local/credentials/`。项目名、服务地址、E2E 账号或 Windows 用户变动后重新执行 `credentials`。CLI 探针只运行一条本地项目文件检查，回执位于 `.local/zentao-delivery/worker-probes/`。

## 4　在新项目建一个固定监测聊天

在上述 Codex 项目里新建聊天。先粘贴以下联调指令，保持监测关闭。

> 读取本项目 AGENTS.md 和 zentao-autodelivery 技能。请做接入联调。核对当前聊天所属桌面项目和工作目录，并用 record runtime_check 确认实际策略为 runtime_unattended。用 quickstart.zen_client(load_config()) 登录，仅通过只读接口核对禅道项目名称唯一匹配、实际项目 ID、接单账号和需求分页。核对需求仓库、目标分支及本机运行器的超时停止证据；用测试账号打开配置的 E2E 页面并核对实际入口。在本轮保持监测关闭，不领取工单、不修改禅道。逐项给出结果及尚待验证项。

联调通过、项目负责人准备好启用后，在**同一聊天**粘贴以下启用指令。让 Codex 创建并读回桌面定时任务；配置文件中的间隔本身不会创建任务。

> 我授权启用当前项目的前端需求交付流程。请核对本聊天绑定、授权范围和已有定时任务，将 monitor.enabled 设为 true。先执行一次监测，确认首次完整扫描返回 baseline。为本聊天建立或更新唯一的定时任务，按 monitor.interval_minutes 执行；每轮从项目根目录读取 zentao-autodelivery 技能，按监测入口对账、扫描、派发和续接。只处理配置范围内的项目、账号和状态，按技能核验范围执行代码合回和原需求备注、附件回写。最后报告定时任务状态、实际间隔、项目 ID 和基线回执。

监测频率默认 3 分钟。若桌面环境无法运行该频率，核对实际可用频率并同步调整配置和定时任务。首次启用时保持桌面环境可运行。

## 5　用一条小需求验收

1. **看到 baseline 后再提交测试需求。** 首轮已经看到的历史需求会登记为基线。选一项有明确验收条件的小改动，例如调整测试页面的一条提示文案。
2. 在需求仓库准备 `requirements/ZT-123/intent.md`，写明涉及页面、当前文案、期望文案和验证方式。把示例编号替换为实际材料目录，在禅道需求正文写明 `ZT-123/`，选对项目、指派账号和状态。
3. 打开解压根目录下自动生成的 `.local/zentao-delivery/status.html`。核对工单 ID、队列、当前阶段和最后结论。验收时核对目标分支的合回结果、待验版本的 E2E 证据和原需求上的最终备注。测试涉及附件时，再核对附件关联和下载内容。新实例的备注和附件能力以该实例的实际回读为准。
4. 成功后保留这个监测聊天和工作区。需要暂停新单时关闭 `monitor.enabled`；完整停机前让在途单收尾、核对待回写项，再暂停定时任务。

| 遇到的现象 | 优先检查 |
| --- | --- |
| `repository_missing` 或 `target_ref_missing` | 核对独立 Git 仓根目录和分支拼写，并从目标远程取得对应分支。 |
| `worker_error` 或 `probe_failed` | 核对桌面端登录，检查 `CODEX_HOME`（默认用户目录下的 `.codex`）内是否有 `packages/app-server-daemon/current/bin/codex.exe`，并检查探针回执。 |
| `runtime_requires_approval` 或 `runtime_unverified` | 在项目允许的无人值守运行环境重新核对监测聊天的实际策略和会话记录。 |
| 禅道登录成功但没有新单 | 核对首次基线、定时任务、分页权限、项目归属、指派账号大小写和状态。 |
| 待补材料、待验证或待回写 | 从状态页和工单证据定位缺少的材料、测试入口、权限或回写回执。 |

**同一项目换电脑**时，先在旧机暂停接新单，核对在途工单及待回写项并交接，再启用新机。每个新项目都从干净的 ZIP 解压；转发时使用原始 ZIP。运行后的目录含本机凭据、工单账本和研发证据。
