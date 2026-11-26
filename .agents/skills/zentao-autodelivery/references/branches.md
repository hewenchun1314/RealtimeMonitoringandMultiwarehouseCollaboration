# 分支核验、候选提交与合回

前后端编辑、构建和测试留在 `never` 的 MXC 工单环境。认证、建树和调用 Git hooks 的固定操作交 Windows 宿主启动器。主聊天和阶段 Agent 禁止在沙箱自行 fetch、worktree add、commit、checkout、merge、rebase 或 cherry-pick，也不能用 `HUSKY=0`、`--no-verify` 或修改 hooks 配置绕过检查。

前端计划用 `evidence.applications`，后端用 `evidence.services` 登记本端配置键。当前配置与领取快照必须仍授权同一个仓库、分支。普通服务与应用在任一配置声明 `runtime.worker_git_write_repositories` 时，两者都必须包含对应 `frontend:<键>` 或 `backend:<键>`；删除或清空既有登记视为撤销。两者从未声明登记的旧配置沿用计划与仓库配置授权。

后端依赖沿既有独立授权：当前配置与快照都须为 `access:writable`，且 `source_path`、`delivery.branch` 等目标仍匹配，才可按依赖键列入计划。它不依赖只用于普通 bundle 仓库的上述登记；删除依赖或改为只读会撤销写权限。

跨轮恢复若仍保留有效的成功分支核验回执，提交和合回继续使用该回执登记的工单分支及工作树；不能仅因轮次增加另造分支或丢弃原候选。只有账本重新进入 branch_check 时才准备对应的新工作树。

## 准备工作树

1. 主聊天领取 `<端>.branch_check`（旧单为 `branch_check`），保存原尝试、分段和绝对截止时间。`branch_inspect` 仅作只读诊断，不能用缺少目标 ref 推断远端无分支。
2. 调用 `record branch_prepare`，传绑定、`attempt_id`、`phase` 和稳定 `event_id`。一次请求准备整个成功计划；`application` 或 `service` 仅核对计划键，批次仍覆盖该端全部目标。返回 `host_git_worker_required` 后立即结束本轮，让启动器确认 CLI、阶段子进程和 Task 同步器均已停止。
3. 宿主仅在目标 ref 本地不存在时，非交互抓取 `origin` 精确分支；锁定基线，在账本固定路径创建工单工作树，保留源仓库位置、当前分支和用户改动。原 hooks 与过滤器照常执行。仅可把源仓库已安装、被 Git 忽略的 `.husky/_` 生成运行文件复制到工单树，不覆盖跟踪脚本或运行自由安装命令；Agent 仍须准备原项目依赖。
4. 启动器恢复同一聊天。用原事件及相同数据读回，或 `host_git_status` 查询 `operation_id`。成功 `repositories` 是逐仓凭据；`branch_inspect` 再核对后整体提交 `attempt_finish.evidence.repositories`。部分失败保留已完成项，修复真实原因后新事件重试。

## 编码与候选提交

编码 Agent 只在核验工作树编辑、构建、测试，返回每仓 `expected_head`、字面相对文件清单 `files`、`message` 和验证证据，不自行提交。主聊天仅在 coding 或返工 coding 分段调用 `record candidate_commit`，以 `commits` 按仓库键批量请求。不接受自由路径、远端 URL 或宿主命令；拒绝 Git 元数据、越界路径和清单外已跟踪/暂存改动。

`host_git_worker_required` 时结束本轮；宿主按原截止时间提交并运行原 hooks。成功 `versions` 用作 coding 证据。`candidate_retest_required` 表示 hooks 改写候选或提交后检查失败：核对既有 SHA、清理并在沙箱重跑受影响检查，再以新事件、当前 `expected_head`、`files:[]` 和非空 `verification` 请求确认。失败原 post hook 必须在宿主实际重跑通过，不能靠文字跳过。仍需提交的 hook 改动先修复、测试，再请求新的 checked commit。相同事件只能读回原结果，不能换消息或文件清单。

unit_test 只能验证已登记候选。需要改代码或补测试时，以 failed 和 `repair_required:true` 登记，由账本返回同端 coding 修订再验证。普通单测得到 coding→verify 路线，已有返工中的单测回到前一个同端 coding。新候选重新执行单测、审查和后续验收，不沿用旧 SHA 证据。

## 合回与恢复

主聊天在 integration（含返工分段）调用 `record integration_merge` 并结束本轮。宿主从成功 coding 证据读取候选，核对干净工作树与配置目标，仅快进或确认已包含。源仓库不在目标分支时使用本单拥有的短固定路径临时工作树，保留源分支。目标在另一个用户工作树检出或源/合回树有跟踪及暂存改动时保留现场。分叉需要单独的受控历史处理路径，当前固定操作集不执行该处理；不能在沙箱自行 rebase/cherry-pick，也不能用普通 coding 提交声称解决祖先关系。

成功返回候选 `versions` 和实际 `merged_versions`；部分失败保留逐仓结果。`integration_retest_required` 先清理、复验实际 SHA，再以新事件和非空 `verification` 确认；失败原 post hook 会实际重跑。最终树与候选不同时，继续满足 `merged_verification` 和任务复验要求。

宿主按 canonical common Git directory 串行化源仓库与 linked worktree。停止不明时保留占用，不启动另一种操作或另一个工单；恢复重新查询原命名 Job Object。稳定效果日志、提交标识及逐仓结果用于恢复 stdout/回执丢失、提交已生效和批次部分完成，不能重复提交或删除用户工作树。稳定结果只属于原绑定、尝试、分段和截止时间，不能推进新尝试。真实失败按原阶段重试规则登记，保留具体仓库、hooks 诊断路径和恢复条件。

用户明确停跑的工单不自动恢复。确认原聊天空闲、受控进程全部停止并登记原活动尝试终态后，沿现有 `record stop` 规则收尾。
