# 账本命令

在项目根目录从标准输入传入一个 JSON 请求。命令为 `python -B .agents/skills/zentao-autodelivery/scripts/workflow.py claim` 或 `python -B .agents/skills/zentao-autodelivery/scripts/workflow.py record`。隔离验收用 `--config PATH --database PATH` 指向临时配置和临时 SQLite。命令管理本地账本并提供只读 Git 核验。建工作树、运行 Agent 和禅道写入由当前聊天执行。

工单键 `item` 包含 `instance`、`project_id`、`kind`、`id`。项目 ID 来自经实际查询确认的禅道项目，项目和工单 ID 用正整数或相应数字字符串，`kind` 为 `story` 或 `bug`。桌面项目 ID 从当前可用的桌面项目接口确认，使用独立字段 `desktop_project_id`。配置可通过 `runtime.desktop_project_id` 额外固定桌面项目 ID。

## `claim` 接单

| `action` | 请求字段 | 如何处理结果 |
| --- | --- | --- |
| `scan` | `scan_id`、带时区的 `observed_at`、`complete`、`instance`、`project_id`、`project_name`、`items`。列表元素含 `kind`、`id`、`title`、`assigned_to`、`status`、`priority`。较小的正整数优先，未知优先级排在后面。 | 首次完整扫描返回 `baseline`，后续完整扫描返回 `scanned`。读 `candidate` 取得需复核的队首。部分或失序扫描保留最新队列。 |
| `reserve` | `item`、详情复核时的 `observed_at`、`assigned_to`、`status`、`desktop_project_id`。 | `reserved` 表示实际取得名额，保存 `request_id`、`expires_at`、`marker` 和已有 `chat_id`。`queue_changed` 或 `stale_check` 时重新复核队首，`busy` 时保留队列供后续轮次处理。 |

## `record` 阶段、执行和写回

新单阶段键使用 `material_check`、`split`、`frontend.<步骤>`、`backend.<步骤>` 和跨端的 `handoff`。请求中的阶段以账本返回为准。前端分支准备传 `application`，后端传 `service`。旧快照继续使用原阶段键。各阶段负责人及版本证据见[工单步骤](stages.md)。

### 拆分证据示例

`record attempt_finish` 的 `evidence` 示例（跨端）。适用端由项目配置和账本在材料评审时读取的原标记共同决定。单端任务只提供该端清单。没有可实施任务但存在无法满足的验收条件时，可提交空任务清单并列明 `blockers`。

```json
{
  "tasks": {
    "backend": [{"id": "B1", "description": "分页查询返回筛选后总数", "acceptance": ["两条匹配记录返回 total=2"]}],
    "frontend": [{"id": "F1", "description": "分页栏展示筛选后的总数", "acceptance": ["页面显示共 2 条"], "depends_on": ["B1"]}]
  },
  "ai_scope": "fullstack",
  "rationale": "现有查询缺少 total 字段，页面需要新增总数显示",
  "interface_contract": "GET /items 返回 items 和 total，空结果返回 total=0",
  "blockers": []
}
```

仅支持两端的项目遇到无标记材料时需要 `ai_scope` 和 `rationale`。所有跨端工单均需 `interface_contract`。任务编号整单唯一，依赖引用本单任务；后端任务先执行。标记取自 `material_check` 的实际 `path`（需求）或 `detail`（Bug）。账本证据与同轮的 `split.md` 保持一致。

跨端 `blockers` 默认全部阻止交接。如果其中有明确属于前端内部的未满足条件，拆分 Agent 可显式提供 `handoff_blockers`，只列仍阻止交接的原始条目，并在拆分文档记录分类依据。例如 `blockers` 同时包括“缺接口”和“缺页面文案”时，`handoff_blockers` 可以只列“缺接口”。明确所有阻断均与接口交接无关时提交空数组。主会话以有效拆分结果核对交接与整单验收，修订或恢复后重新读取。

### 工单动作

每个改变账本的请求提供 `action`、稳定的 `event_id` 和 `item`。重复提交同一逻辑事件时保留原事件 ID 和原数据。收到带 `replayed:true` 的回执时核对当前账本，按已发生的原操作继续。只读 `branch_inspect` 和 `runtime_check` 不需要 `event_id`。以下为各操作另外需要的字段。

| `action` | 请求字段及用法 |
| --- | --- |
| `runtime_check` | 只需实际桌面 `chat_id`。读取该聊天最近一轮 Codex 本机运行记录的审批策略，只输出策略和检查状态。仅 `runtime_unattended` 可派发。其他状态为 `runtime_requires_approval` 或 `runtime_unverified`。配置文件默认值及父聊天的策略不能用作该聊天的证明。 |
| `bind` | `request_id`、实际 `chat_id`、读回的 `desktop_project_id`。确认绑定和名额后，再向绑定的聊天发启动指令。处理待核对建聊时先绑定，再重新领取名额。 |
| `start` | `request_id`、当前 `chat_id`。只有工单聊天在获得启动指令后调用。启用 `runtime.require_unattended_approval` 时账本再次核对该聊天策略。若返回 `dispatch_runtime_blocked`，工单仍绑定原聊天，预占名额已释放。 |
| `defer_dispatch` | `request_id`。建聊结果不明且尚未启动业务时，登记待核对。已绑定但未开始执行且检查未通过时传入 `reason:"approval_policy"`，账本复核后释放预占名额并保留原聊天。等待策略或运行环境变化后，用新的派发请求续接。 |
| `expire_dispatch` | `request_id`。核对未领取执行权的过期派发。 |
| `resolve_dispatch` | `request_id`、`not_created:true` 和非空 `evidence`。有明确证据证明旧请求未创建聊天时，用于释放待建状态。 |
| `attempt_start` | `request_id`、`chat_id`、`stage`。保存返回的 `attempt_id`、`started_at`、`phase_started_at`、`deadline_at`、`retry_number` 和 `phase`。新单的 `phase` 为 `verify` 或返工的实际分段名。旧单沿用 `repair_coding` 等原分段。按回执的角色和截止时间执行，`attempt_active` 表示原尝试仍存在。 |
| `branch_inspect` | `request_id`、`chat_id`、有效分支核验 `attempt_id`。可传 `worktrees`（应用配置键对应的工作树绝对路径）。首次调用解析分支，准备好工作树后再次调用验证真实提交和工单分支。参照 [分支核验与工作树](branches.md) 处理每项状态。 |
| `branch_prepare` | `request_id`、`chat_id`、有效分支核验 `attempt_id`、目标 `application`（前端）或 `service`（后端），以及 `desktop_reconciliation` 对象，含 `status`（`no_receipt`、`failed` 或 `unavailable`）和非空 `evidence`。对账后按固定路径创建或恢复核验过的 Git worktree，以实际路径继续。此操作不改变 SQLite 阶段。 |
| `repair_advance` | `request_id`、`chat_id`、`attempt_id`、当前 `phase`、`summary` 和本分段的成功 `evidence`。获得新的 `repair_advanced` 后，使用同一 `attempt_id` 执行返回的下一分段及其新截止时间。重放返回 `phase_already_advanced`，需对账当前分段。 |
| `attempt_finish` | `request_id`、`chat_id`、`attempt_id`、当前 `phase`、`outcome`、`summary`、`evidence`。`outcome` 为 `success`、`failed` 或 `timed_out`。Code Review 或 E2E 有确需修复的代码缺陷时，失败结果提供 `repair_required:true`。环境问题按普通失败处理。分段失败也由本操作记录，超时须提供 `stop_confirmed:true` 和 `stop_evidence`。确定无法重试时提供 `retryable:false`，可用 `completion` 指定「待补材料」「待验证」或「阻断」。`disposition:next` 时即使本步失败也立即执行 `next_stage`。`retry` 续跑本步，收到 `close` 或 `blocked` 后执行收尾。需求的 `material_check` 确认缺少需求文件时，提交 `outcome:"failed"` 和 `evidence.material_file_status:"missing"` 并附核对依据。账本跳过重试，返回 `blocked`，后续步骤记为跳过，结论为「待补材料」。 |
| `complete` | `request_id`、`chat_id`。有阻断时提供本地 `blockers_path`，有协作者可访问的位置时再提供 `blockers_location`。以账本返回的结论和步骤结果为准。 |
| `stop` | 用户明确要求停跑时使用。传 `request_id`、`chat_id`、`stop_evidence`，证据须包含实际聊天状态 `chat_status:"idle"` 和 `owned_processes_active_after_stop:0`。有活动尝试时先登记原尝试终态。停跑保留绑定和已有步骤，成绩记为「主动停跑」，释放名额并入队写回，不进入自动恢复。 |
| `recover` | `recovery_id`、与阻断直接相关的 `evidence`。同一恢复证据用同一标识。从流程顺序中首个仍失败的步骤恢复，沿原聊天继续。 |
| `writeback_claim` | `operation_id`、`owner`。保存 `lease_id`、`expires_at`、`reconcile_required` 及 `entry`。到期失去发起请求的权利。 |
| `writeback_reconcile` | `operation_id`、`lease_id`、`prior_ended:true`、`missing:true`。接管后确认旧请求已终止并读回确认缺失时提交。获得 `ready_to_send` 后，才能申请补发。 |
| `writeback_send` | `operation_id`、`lease_id`。获得新的 `send_authorized` 后允许执行一次对应的禅道写入请求。同一事件重放或已有进行中的发送授权时返回 `already_authorized`，进入回读对账。领取到期后的新执行者按 `writeback_reconcile` 完成核对后再申请授权。 |
| `writeback_confirm` | `operation_id`、`lease_id`、实际读回的 `receipt`。已存在时提交回执，无需再次发送。 |

前端计划登记 `evidence.applications`，后端计划登记 `evidence.services`，每个数组都须非空且没有重复项。核验成功时把 `branch_inspect` 返回的全部 `repositories` 保存到 `evidence.repositories`。未通过工作树校验时保留当前尝试，核对原因后继续或登记失败。

联合验收发现后端缺陷时，失败请求除 `repair_required:true` 外传 `repair_side:"backend"`。修复涉及前端改动时传 `frontend_changed:true`。端内设计、计划或验证发现接口约定和任务划分需要修订时，失败请求传 `repair_required:true`、`contract_changed:true`。合回遇到需要业务取舍的冲突时，可通过 `failed` 和 `repair_required:true` 交责任端返工。

下一次尝试按 `phase`、`role` 和 `deadline_at` 完成对应分段。修订 `split` 分段提交完整的新任务清单、接口约定与修改原因；`repair_advance` 返回 `route_revised` 表示该尝试已经结束，轮次递增；用新的 `attempt_start` 从 `next_stage` 指定的首端设计开始，再执行计划、分支核验、开发、验收和合回，跨端重新交接。普通返工分段使用对应阶段的证据格式并在同一次尝试用 `repair_advance` 交接，最后在 `verify` 重新验收。

只读核对使用 `sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)`，其中 `path` 是账本的绝对 `Path`。查询未初始化账本时报告尚无数据。`work_items` 通过 `instance`、`project_id`、`kind`、`item_id` 定位。以该行的 `seq` 对应 `dispatches`、`attempts`、`writebacks` 的 `item_seq` 查派发、各步骤尝试和待写回。`work_items.config_snapshot` 保存已接单的配置，`request_id` 和 `stage` 记录当前派发和步骤。

账本记录里只保存对象标识、非敏感证据和回执。禅道密码、登录会话和原始认证响应禁止进入命令请求或 SQLite。

启用 `runtime.split_task_sync` 的新 Story 会把每个拆分编号登记在 `split_tasks`。监测与工单会话在选取原需求备注和附件时，只领取类型为 `comment` 或 `attachment` 的操作。类型为 `task_create`、`task_close`、`task_reopen` 的回写由任务同步器处理；对其调用通用 `record writeback_*` 返回 `task_sync_required`。任务管理继承工单快照、稳定拆分编号和已绑定的禅道 Task ID。

发送结果不明后，任务同步器持续读取原发送目标。若确认原请求已经终止且该动作确实没有生效，可用 `record task_reconcile_missing` 登记 `operation_id`、与原 `payload.sent` 一致的 `original_target`、`prior_ended:true`、`missing:true`，以及分别证明请求结束和结果缺失的 `termination_evidence`、`readback_evidence`。只有已释放或到期的操作可恢复，下一次发送仍重新读取目标并核对拆分标记。
