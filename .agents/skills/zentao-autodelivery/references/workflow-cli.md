# 账本命令

在项目根目录从标准输入传入一个 JSON 请求。命令为 `python -B .agents/skills/zentao-autodelivery/scripts/workflow.py claim` 或 `python -B .agents/skills/zentao-autodelivery/scripts/workflow.py record`。隔离验收用 `--config PATH --database PATH` 指向临时配置和临时 SQLite。命令管理本地账本并提供只读 Git 核验。建工作树、运行 Agent 和禅道写入由当前聊天执行。

工单键 `item` 包含 `instance`、`project_id`、`kind`、`id`。项目 ID 来自经实际查询确认的禅道项目，项目和工单 ID 用正整数或相应数字字符串，`kind` 为 `story` 或 `bug`。桌面项目 ID 从当前可用的桌面项目接口确认，使用独立字段 `desktop_project_id`。配置可通过 `runtime.desktop_project_id` 额外固定桌面项目 ID。

## `claim` 接单

| `action` | 请求字段 | 如何处理结果 |
| --- | --- | --- |
| `scan` | `scan_id`、带时区的 `observed_at`、`complete`、`instance`、`project_id`、`project_name`、`items`。列表元素含 `kind`、`id`、`title`、`assigned_to`、`status`、`priority`。较小的正整数优先，未知优先级排在后面。 | 首次完整扫描返回 `baseline`，后续完整扫描返回 `scanned`。读 `candidate` 取得需复核的队首。部分或失序扫描保留最新队列。 |
| `reserve` | `item`、详情复核时的 `observed_at`、`assigned_to`、`status`、`desktop_project_id`。 | `reserved` 表示实际取得名额，保存 `request_id`、`expires_at`、`marker` 和已有 `chat_id`。`queue_changed` 或 `stale_check` 时重新复核队首，`busy` 时保留队列供后续轮次处理。 |

## `record` 阶段、执行和写回

每个改变账本的请求提供 `action`、稳定的 `event_id` 和 `item`。重复提交同一逻辑事件时保留原事件 ID 和原数据。收到带 `replayed:true` 的回执时核对当前账本，按已发生的原操作继续。只读 `branch_inspect` 和 `runtime_check` 不需要 `event_id`。以下为各操作另外需要的字段。

| `action` | 请求字段及用法 |
| --- | --- |
| `runtime_check` | 只需实际桌面 `chat_id`。读取该聊天最近一轮 Codex 本机运行记录的审批策略，只输出策略和检查状态。仅 `runtime_unattended` 可派发。其他状态为 `runtime_requires_approval` 或 `runtime_unverified`。配置文件默认值及父聊天的策略不能用作该聊天的证明。 |
| `bind` | `request_id`、实际 `chat_id`、读回的 `desktop_project_id`。确认绑定和名额后，再向绑定的聊天发启动指令。处理待核对建聊时先绑定，再重新领取名额。 |
| `start` | `request_id`、当前 `chat_id`。只有工单聊天在获得启动指令后调用。启用 `runtime.require_unattended_approval` 时账本再次核对该聊天策略。若返回 `dispatch_runtime_blocked`，工单仍绑定原聊天，预占名额已释放。 |
| `defer_dispatch` | `request_id`。建聊结果不明且尚未启动业务时，登记待核对。已绑定但未开始执行且检查未通过时传入 `reason:"approval_policy"`，账本复核后释放预占名额并保留原聊天。等待策略或运行环境变化后，用新的派发请求续接。 |
| `expire_dispatch` | `request_id`。核对未领取执行权的过期派发。 |
| `resolve_dispatch` | `request_id`、`not_created:true` 和非空 `evidence`。有明确证据证明旧请求未创建聊天时，用于释放待建状态。 |
| `attempt_start` | `request_id`、`chat_id`、`stage`。保存返回的 `attempt_id`、`started_at`、`phase_started_at`、`deadline_at`、`retry_number` 和 `phase`。`phase` 为 `verify`、`repair_coding`、`repair_unit_test` 或 `repair_review`，以账本返回的分段和截止时间为准。`attempt_active` 表示原尝试仍存在，需核对进度。 |
| `branch_inspect` | `request_id`、`chat_id`、有效分支核验 `attempt_id`。可传 `worktrees`（应用配置键对应的工作树绝对路径）。首次调用解析分支，准备好工作树后再次调用验证真实提交和工单分支。参照 [分支核验与工作树](branches.md) 处理每项状态。 |
| `branch_prepare` | `request_id`、`chat_id`、有效分支核验 `attempt_id`、目标 `application`、`desktop_reconciliation` 对象，含 `status`（`no_receipt`、`failed` 或 `unavailable`）和非空 `evidence`。对账后按固定路径创建或恢复核验过的 Git worktree，以实际路径继续。此操作不改变 SQLite 阶段。 |
| `repair_advance` | `request_id`、`chat_id`、`attempt_id`、当前 `phase`、`summary` 和本分段的成功 `evidence`。获得新的 `repair_advanced` 后，使用同一 `attempt_id` 执行返回的下一分段及其新截止时间。重放返回 `phase_already_advanced`，需对账当前分段。 |
| `attempt_finish` | `request_id`、`chat_id`、`attempt_id`、当前 `phase`、`outcome`、`summary`、`evidence`。`outcome` 为 `success`、`failed` 或 `timed_out`。Code Review 或 E2E 有确需修复的代码缺陷时，失败结果提供 `repair_required:true`。环境问题按普通失败处理。分段失败也由本操作记录，超时须提供 `stop_confirmed:true` 和 `stop_evidence`。确定无法重试时提供 `retryable:false`，可用 `completion` 指定「待补材料」「待验证」或「阻断」。 |
| `complete` | `request_id`、`chat_id`。有阻断时提供本地 `blockers_path`，有协作者可访问的位置时再提供 `blockers_location`。以账本返回的结论和步骤结果为准。 |
| `stop` | 用户明确要求停跑时使用。传 `request_id`、`chat_id`、`stop_evidence`，证据须包含实际聊天状态 `chat_status:"idle"` 和 `owned_processes_active_after_stop:0`。有活动尝试时先登记原尝试终态。停跑保留绑定和已有步骤，成绩记为「主动停跑」，释放名额并入队写回，不进入自动恢复。 |
| `recover` | `recovery_id`、与阻断直接相关的 `evidence`。同一恢复证据用同一标识。 |
| `writeback_claim` | `operation_id`、`owner`。保存 `lease_id`、`expires_at`、`reconcile_required` 及 `entry`。到期失去发起请求的权利。 |
| `writeback_reconcile` | `operation_id`、`lease_id`、`prior_ended:true`、`missing:true`。接管后确认旧请求已终止并读回确认缺失时提交。获得 `ready_to_send` 后，才能申请补发。 |
| `writeback_send` | `operation_id`、`lease_id`。获得新的 `send_authorized` 后允许执行一次对应的禅道写入请求。同一事件重放或已有进行中的发送授权时返回 `already_authorized`，进入回读对账。领取到期后的新执行者按 `writeback_reconcile` 完成核对后再申请授权。 |
| `writeback_confirm` | `operation_id`、`lease_id`、实际读回的 `receipt`。已存在时提交回执，无需再次发送。 |

配置了前端分支映射时，计划成功需要非空且不重复的 `evidence.applications`。核验成功需要把 `branch_inspect` 返回的全部 `repositories` 保存到 `evidence.repositories`，以账本接受的回执推进编码。未通过工作树校验时保留当前尝试，核对原因后继续或登记失败。

只读核对使用 `sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)`，其中 `path` 是账本的绝对 `Path`。查询未初始化账本时报告尚无数据。`work_items` 通过 `instance`、`project_id`、`kind`、`item_id` 定位。以该行的 `seq` 对应 `dispatches`、`attempts`、`writebacks` 的 `item_seq` 查派发、各步骤尝试和待写回。`work_items.config_snapshot` 保存已接单的配置，`request_id` 和 `stage` 记录当前派发和步骤。

账本记录里只保存对象标识、非敏感证据和回执。禅道密码、登录会话和原始认证响应禁止进入命令请求或 SQLite。
