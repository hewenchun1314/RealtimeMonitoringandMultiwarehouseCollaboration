# 分支核验、候选提交与合回

前后端编辑、构建和测试留在 `never` 的受控工单环境。认证、建树和调用 Git hooks 的固定操作交跨平台 Git 入口。主聊天和阶段 Agent 禁止在沙箱自行 fetch、worktree add、commit、checkout、merge、rebase 或 cherry-pick，也不能用 `HUSKY=0`、`--no-verify` 或修改 hooks 配置绕过检查。

前端计划用 `evidence.applications`，后端用 `evidence.services` 登记本端配置键。当前配置与领取快照必须仍授权同一个仓库、分支。普通服务与应用在任一配置声明 `runtime.worker_git_write_repositories` 时，两者都必须包含对应 `frontend:<键>` 或 `backend:<键>`；删除或清空既有登记视为撤销。两者从未声明登记的旧配置沿用计划与仓库配置授权。

后端依赖沿既有独立授权：当前配置与快照都须为 `access:writable`，且 `source_path`、`delivery.branch` 等目标仍匹配，才可按依赖键列入计划。它不依赖只用于普通 bundle 仓库的上述登记；删除依赖或改为只读会撤销写权限。

跨轮恢复若仍保留有效的成功分支核验回执，提交和合回继续使用该回执登记的工单分支及工作树；不能仅因轮次增加另造分支或丢弃原候选。只有账本重新进入 branch_check 时才准备对应的新工作树。

## 准备工作树

1. 主聊天领取 `<端>.branch_check`（旧单为 `branch_check`），保存原尝试、分段和绝对截止时间。`branch_inspect` 仅作只读诊断，不能用缺少目标 ref 推断远端无分支。
2. 调用 `record branch_prepare`，传绑定、`attempt_id`、`phase` 和稳定 `event_id`。一次请求准备整个成功计划；`application` 或 `service` 仅核对计划键，批次仍覆盖该端全部目标。在本轮等待宿主回执，按实际状态继续。
3. 宿主仅在目标 ref 本地不存在时，非交互抓取 `origin` 精确分支；锁定基线，在账本固定路径创建工单工作树，保留源仓库位置、当前分支和用户改动。原 hooks 与过滤器照常执行。仅可把源仓库已安装、被 Git 忽略的 `.husky/_` 生成运行文件复制到工单树，不覆盖跟踪脚本或运行自由安装命令；Agent 仍须准备原项目依赖。
4. 主聊天当场读取回执；中断时用原事件及相同数据读回，或 `host_git_status` 查询 `operation_id`。成功 `repositories` 是逐仓凭据；`branch_inspect` 再核对后整体提交 `attempt_finish.evidence.repositories`。部分失败保留已完成项，修复真实原因后新事件重试。

## 编码与候选提交

编码 Agent 只在核验工作树编辑、构建、测试，返回每仓 `expected_head`、字面相对文件清单 `files`、`message` 和验证证据，不自行提交。主聊天仅在 coding 或返工 coding 分段调用 `record candidate_commit`，以 `commits` 按仓库键批量请求。不接受自由路径、远端 URL 或宿主命令；拒绝 Git 元数据、越界路径和清单外已跟踪/暂存改动。

宿主按原截止时间提交并运行仓库 hooks，主聊天等待本次回执。成功 `versions` 用作 coding 证据。`candidate_retest_required` 表示 hooks 改写候选或提交后检查失败：核对既有 SHA、清理并在沙箱重跑受影响检查，再以新事件、当前 `expected_head`、`files:[]` 和非空 `verification` 请求确认。失败原 post hook 必须在宿主实际重跑通过，不能靠文字跳过。仍需提交的 hook 改动先修复、测试，再请求新的 checked commit。相同事件只能读回原结果，不能换消息或文件清单。

unit_test 只能验证已登记候选。需要改代码或补测试时，以 failed 和 `repair_required:true` 登记，由账本返回同端 coding 修订再验证。普通单测得到 coding→verify 路线，已有返工中的单测回到前一个同端 coding。新候选重新执行单测、审查和后续验收，不沿用旧 SHA 证据。

## 合回与恢复

主聊天在 integration（含返工分段）调用 `record integration_merge` 并等待回执。宿主核对已验证候选、配置目标和工作树归属，对已包含、可快进及历史分叉的候选分别核验。需求和 Bug 的目标主工作区允许存在已跟踪或暂存改动。可快进且路径互不重叠时，宿主记录本地暂存区和工作区指纹，原地快进后重新核验。需要协调本地改动时，宿主在移动目标前保存快照并计算暂存和未暂存两层在目标版本上的结果，执行原 Git hooks，恢复两层改动并核验。源仓库检出其他分支且有编辑时，在本单拥有的临时工作树处理目标分支。遇到其他用户工作树占用目标或已存在的未完成 Git 操作时，保留现场并记录具体原因。

单仓返回 `integration_conflict` 时，主聊天在当前合回阶段根据回执的冲突层、文件、各方内容及工单验收条件组织协调。开发 Agent 在工单目录准备逐文件解决结果，核对功能语义并记录验证依据。以新 `event_id` 再申请 `integration_merge`，在 `resolutions` 下按仓库键传入 `conflict_id`、覆盖本次全部冲突路径的 `files`、非空 `verification`。每个文件可提交完整 UTF-8 文本，或在明确核对内容后传入 `{"take":"base"}`、`{"take":"upstream"}`、`{"take":"local"}`、`{"take":"delete"}`。层次及各方身份以回执为准。后续层再次冲突时读回新证据、为新标识提交解决结果。宿主重算指纹，核对目标、候选和本地快照后才使用匹配决定。历史冲突形成新的合回树时，对实际交付版本复验；工作区重放解决仅恢复本地改动。无法确定意图时记录具体冲突与两方内容。

成功返回候选 `versions` 和逐仓已核验的 `merged_versions`。`integration_retest_required` 回执中的 `merged` 用于定位复验版本。复核实际 SHA、用户本地改动的恢复结果和原 hooks 后，以新事件和非空 `verification` 续接。最终树与候选不同时，继续满足 `merged_verification` 和任务复验要求。中断后须按宿主日志和原工作区身份恢复。目标前移前中断且原工作区已恢复时，宿主返回 `integration_retry_required`，主聊天立即以新事件续接合回。存在未完成的暂存区或工作区恢复时，该仓持续由原工单占用。

宿主按 canonical common Git directory 串行化源仓库与 linked worktree。停止不明时保留占用，不启动另一种操作或另一个工单；恢复时核对原运行的停止回执；若回执中断，Windows 查询原命名 Job Object，Linux 和 macOS 核对监管进程留下的退出证明及原进程会话或进程组的成员。稳定效果日志、提交标识及逐仓结果用于恢复 stdout/回执丢失、提交已生效和批次部分完成，不能重复提交或删除用户工作树。稳定结果只属于原绑定、尝试、分段和截止时间，不能推进新尝试。真实失败按原阶段重试规则登记，保留具体仓库、hooks 诊断路径和恢复条件。

用户明确停跑的工单不自动恢复。确认原聊天空闲、受控进程全部停止并登记原活动尝试终态后，沿现有 `record stop` 规则收尾。
