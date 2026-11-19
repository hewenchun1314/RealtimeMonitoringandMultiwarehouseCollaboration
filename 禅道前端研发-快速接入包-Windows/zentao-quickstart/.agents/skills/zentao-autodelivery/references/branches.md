# 分支核验与工作树

使用工单配置快照中的 `development.frontend_bundle` 和 `workspaces.frontend`。应用键与构建清单一致，严格区分大小写。`repository` 是相对前端工作区的仓库路径，`branch` 是本单的基线及合回目标。计划只列需要提交代码的应用。仅在浏览器中加载的依赖按 E2E 指引核对。

## 计划完成后

1. 开发 Agent 在 `plan.md` 明确受影响的应用，并在计划成功的 `attempt_finish` 中提供 `evidence.applications`。例如只修改菜单时可用 `["shell-hub"]`。具体应用有查证困难时，先核对代码入口、构建清单和现有仓库说明。仍无法确定范围时，以计划不可重试失败登记实际证据。
2. 工单主聊天启动账本返回的 `branch_check`，核对本机工具支持在本次尝试内完成或停止，使用返回的尝试编号和截止时间。运行 `record branch_inspect` 读取每个应用的实际 Git 根目录、当前分支及目标提交。检查严格匹配目标 refs，优先本地同名分支，然后查已有 `origin` 跟踪分支。
3. 返回 `target_ref_missing` 时，先核对该仓库是否配置了 `origin`，通过已有凭据以非交互方式运行带时限的 `git ls-remote --exit-code --heads origin refs/heads/<branch>`。核实远端确有目标后，只抓取这一条分支到 `refs/remotes/origin/<branch>`，再运行 `branch_inspect`。对 fetch 和远端查询设置工具超时，继续沿用本次尝试的剩余时间。
4. 所有目标都解析后，逐仓调用桌面的 `create_worktree`，传入实际 Git 根目录和锁定的 `base_commit`，保留操作编号并有界等待。成功时只在返回的工作树中建立工单分支。回执缺失时核对桌面附件和源仓库的 Git worktree 清单，优先复用能归属本单的已有结果。确认失败或仍无回执且旧请求只可能创建独立的 detached worktree 时，保留对账证据，调用 `record branch_prepare` 在账本目录创建固定路径的 Git worktree，读回后可尝试 `attach_worktree`。同一工单轮次重复调用会核对复用原路径。桌面注册未返回时，使用核验过的实际路径继续开发并记录注册状态。迟到的桌面回执只用于对账。
5. 准备好所有工作树后，向 `branch_inspect` 传 `worktrees`，键为应用，值为返回的绝对工作树路径。每项应为 `verified`。把返回的 `repositories` 整体写入 `attempt_finish.evidence.repositories`。账本在进入编码前重新核对目标应用、仓库、工单分支和基线。启动编码或返工前读回该路径当前仍在已登记的工单分支，再把工作树交给 Agent，并记录后续候选提交。运行已启动服务的实际版本以及合回目标按现有 E2E 和合回要求核对。

## 阻断与继续

Windows 工单启动器中的 CLI 若没有桌面建树工具，先核对现有 Git worktree 和本工单证据，通过 `record branch_prepare` 使用已经核验的固定路径，传入 `desktop_reconciliation.status:"unavailable"` 及实际证据。续接时优先复用已登记的工作树。

已证实应用映射缺失或无效、缺少实际 Git 仓库或本地及远端均无目标分支时，按不可重试失败记录应用、目标分支、路径和确认依据。仓库暂不可读、远端暂不可达或工具暂时异常时，按首次失败后最多三次重试处理。每次重试沿用账本中的工单和目标配置，先读回已有工作树再继续。

建树回执超时时，核对原操作是否已停止，或是否已限定在本工单独立的 detached worktree 内且没有任何编码 Agent 启动。能证明隔离时保存证据，按超时处理。迟到的工作树回执只进入原尝试对账。复验得到新的结论时使用新的完成事件编号。

用完重试或确认不可重试时，在 `blockers.md` 列出受阻应用和步骤、尝试结果及依赖关系。执行 `record complete`，由现有流程回写逐步结果、总结和附件，继续处理下一单。待写回项可按现有规则补发。整个过程按账本和工具回执推进，无需人工答复。
