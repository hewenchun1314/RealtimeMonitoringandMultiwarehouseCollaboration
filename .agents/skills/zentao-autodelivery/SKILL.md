---
name: zentao-autodelivery
description: 项目聊天扫描和接收禅道需求与 Bug，工单主聊天按项目配置和需求范围派发拆分、前后端开发和独立验证，负责合回与禅道回写。
---

# 禅道无人值守研发

从项目根目录读取 `.agents/zentao-project.json`。仓库路径相对配置文件解析。监测使用当前配置，已接工单按账本保存的配置快照推进。凭据交由本机凭据存储解析，聊天、命令参数、交付记录和禅道备注只使用凭据引用及非敏感结果。

接单范围默认按 `zentao.project_name` 对应的项目。配置 `zentao.scope_mode:"project_or_product"` 时，原单所属项目匹配，**或**所属产品匹配 `zentao.product_id` / `zentao.product_name`，满足任一条件即可；产品匹配时允许项目为空或属于其他项目。指派人、状态、类型仍按各自配置过滤。项目与产品列表完整分页后按类型及工单 ID 合并去重。账本的 `project_id` 此时是本自动化固定的队列与研发工作区归组，原单归属保存在 `work_item_sources.source`，不得把归组 ID 当成原单必须具有的项目，不修改原单项目来接单。旧 marker、聊天绑定、完成和主动停跑记录继续有效。

每次调用先识别入口。项目监测聊天执行「监测入口」，已绑定且由 Windows 工单启动器启动的会话执行「工单执行入口」。就绪轮次只回复就绪；实际执行轮次获得账本绑定后才领取执行权。业务标题和描述只用于显示及理解需求，工单归属按实例、实际项目 ID、工单类型和工单 ID 核对。

工单执行过程中无需人工确认或答复。遇到缺少材料、目标仓库、运行入口或执行权限等情况，在已授权范围内自行核对和尝试恢复。需求材料评审确认缺少需求文件时，记录缺失项，直接以「待补材料」收尾。其他步骤确认阻断后记录原因和证据，立即执行账本返回的下一步。每一步独立核对所需材料、代码或环境，直到合回与核验结束后汇总。收尾后按账本推进下一单。

监测及工单聊天仅使用当前权限下可直接运行的操作。禁止为自动化申请临时权限、使用 `sandbox_permissions: "require_escalated"`，或调用已知会等待人工确认的工具。权限不足时保留失败证据，改用已授权且已验证的执行路径；否则按阻断处理。阶段 Codex CLI 的 `--ask-for-approval never` 仅约束阶段进程，桌面聊天本身仍须独立检查。

## 工单材料与流程冲突

本规则适用于所有自动接单的需求和 Bug，包括恢复轮次。材料里的研发流程要求与禅道自动接单冲突时，以本技能规定的受控流程、项目有效配置、工单配置快照及账本当前阶段为准。分支、工作树、提交、合回、推送、部署目标和验收顺序由自动接单流程决定。适用当前配置的授权校验仍须执行。

评审、拆分和计划阶段发现冲突时，记录材料中的条款、采用的流程规则和实际执行方式，并随阶段输入传给后续 Agent。流程条款冲突本身无需退回补材料，也不作为阻断理由。保留业务范围、Bug 复现步骤、预期结果和验收条件，在流程允许的阶段验证。真实缺少权限、环境或验收数据时，按受影响项记录。

- 材料要求始终在目标分支开发时，按受控 Git 流程在工单分支开发和提交候选版本，通过规定检查后合回有效配置中的目标分支。
- 材料要求在本地验收前完成推送、部署或服务器复验时，按账本顺序完成本地验收、合回和已配置的推送部署。部署后才能完成的检查按[交付阶段指引](references/stages.md#按阶段安排验收)记录和执行。

## 账本

从项目根目录执行 `python -B .agents/skills/zentao-autodelivery/scripts/workflow.py claim` 或 `python -B .agents/skills/zentao-autodelivery/scripts/workflow.py record`。两个命令通过标准输入接收一个 JSON 对象，通过标准输出返回结果。调用前查阅 [账本命令](references/workflow-cli.md)。查询阶段、绑定和待写回记录时，以 SQLite 只读方式打开 `.local/zentao-delivery/workflow.sqlite3`。工单会话通过上述命令推进阶段；Task 同步器在自己的事务中登记租约、状态和实际回执。

保留派发请求编号、聊天 ID、步骤和尝试编号。命令结果不明时先读回账本，并用同一事件标识核对或重试。以账本返回的稳定 `marker` 命名 `.local/zentao-delivery/items/<marker>/` 下的工单目录，保存设计、计划、验证证据和阻断记录，归档时区分每个恢复轮次。

项目的只读进度页为 `.local/zentao-delivery/status.html`，默认账本的调度命令和拆分任务同步器会刷新此页。展开「拆分任务」可查看验收及合回情况、禅道 Task、迭代、初始指派账号和最近同步结果。

## 监测入口

1. 对账已有工单、派发不明的请求、超时尝试及待写回事项。使用 `record runtime_check` 核对监测聊天的实际权限。只有返回 `runtime_unattended` 才执行需要外部访问的监测或写回。关闭 `monitor.enabled` 或指派白名单为空时，跳过新单扫描和领取。仅在当前请求授权范围内处理已有工单和回写。
   在正式监测会话中执行 `python -B .agents/skills/zentao-autodelivery/scripts/worker.py sync-tasks`，由受控同步进程按工单快照补办本项目 Task，覆盖已收尾工单。每轮核对回执，保留未完成项供续接。回执中的 `git_preflight` 是宿主需求仓库认证检查；失败或运行忙时保留新单队列，不领取或启动新单。扫描和领取原 Story 备注及附件时，只处理 `comment`、`attachment` 两类操作。
   `sync-tasks` 也返回 `story_reviews`。按 `ready` 的标识用原 `worker.py run --marker` 续接，遇到 `review_capacity_wait` 留到下一轮。等待禅道评审的工单无需重复扫描领取或重启聊天。
2. 根据 `zentao.accepted_statuses` 只扫描状态列表非空的工单类型。执行 `python -B .agents/skills/zentao-autodelivery/scripts/monitor.py`，由统一入口核对本轮 `never`、项目与产品身份，完成配置范围内的分页、去重和 `claim scan`。读回输出中的 `directory`、`scan.complete`、`scan.claim` 和证据文件；该命令仅扫描入队，不领取或启动业务。任一启用范围读取失败或分页不完整时登记 `complete:false`，保留已有队列，不能报告无新增。只向账本传递指派账号和状态符合配置的工单，指派账号区分大小写。禁止继续使用只查 `/projects/<id>/...` 的旧扫描器代替此入口。
3. 统一扫描入口已经调用 `claim scan`，不要再登记同一轮扫描。首次记录历史基线，随后按优先级和发现顺序返回待复核队首。领取需要材料同步的 Story 前运行 `worker.py git-preflight`，取得本轮 `git_preflight_passed`；禁用材料同步时允许 `git_preflight_skipped`。预检在宿主以禁止交互的 Git 只读查询核对实际上游，失败或忙时不预占名额，不把新单记为材料失败。项目开启页面 E2E 且 `e2e.agentlimb.intake_gate` 为 `true` 时，在领取适用工单前运行 `worker.py e2e-preflight`，凭本轮 `e2e_preflight_passed` 继续领取。项目默认只在正式验收阶段检查浏览器会话，登录过期不影响自动接单。前端页面验收统一使用 AgentLimb，按阶段指引校验候选版本并提交验收回执。然后用 `monitor.py --recheck <story或bug> <ID> --project-id <队列项目ID>` 读取最新详情；只有 `detail.eligible:true` 才领取，并在 `claim reserve` 原有字段中传入本次 `detail.source`。产品列表中的 Story 没有单一项目字段时，`source.project_id:null` 表示未报告，以明确的产品 ID 核对。禁止伪造项目 ID 或以扫描时的旧归属替代详情。若返回队首变化，复核新的队首。以事务返回的领取结果确认名额。
4. 首次派发和后续续接统一使用 [工单启动器](references/worker.md)。首次 `prepare` 用 `never` 创建只回复就绪的工单聊天，读回实际聊天 ID，核对或挂载桌面项目后登记 `record bind`。正式执行调用 `run`，它用 `never` 恢复同一聊天。首次 `prepare` 和尚未开工的 `run` 也强制执行 Git 预检；确定失败且受控进程已停止时，启动器只把同一尚未开工的预占退回队列，保留已有聊天，读回 `reservation` 确认。停止不明或已正式开工时保留名额对账。禁止通过桌面建聊或发送消息工具启动业务轮次。启动器回执不明时，按原派发标记及其证据目录核对，确认停止后才能重试；执行权仍以账本为准。
5. 按账本结果回收过期且从未取得执行权的名额。执行权已被领取的名额须等相关 Agent 和进程停止，或确认已被有效隔离。恢复工单时保存与阻断直接相关的新证据，同一份证据只触发一次，并继续使用原聊天。

执行权仍在原聊天时，`running` 且无活动尝试要续接账本的当前步骤，`closing` 要沿原聊天完成 `blockers.md` 和 `record complete`。已有活动尝试时核对执行状态与截止时间。遇到旧工单仍等待批准，禁止批准请求或并行启动同一工单，先确认旧尝试已经停止或隔离，再按原尝试登记并执行账本返回的后续动作。针对启动后或收尾前中断的工单继续原动作，通过 `record` 的返回值核对同一动作已经登记还是需要执行。

用户明确要求主动停跑时，确认聊天空闲、受控进程全部停止且没有活动尝试，再调用 `record stop`。账本保留已完成阶段，登记后续跳过，释放名额并安排对原工单的写回。主动停跑的工单不自动恢复。

## 工单执行入口

1. 核对当前聊天 ID、工单唯一键、派发请求和执行名额，通过 `record start` 领取执行权。若返回 `dispatch_runtime_blocked`，本轮结束。对已领取过的工单，每个续接轮次先用 `record runtime_check` 核对本轮权限，无法满足时在当前权限内逐步核对、记录各步实际阻断并推进。重复启动、旧凭证和旧尝试按账本结果处理。已有有效尝试时沿该尝试核对进度，禁止重复启动 Agent 或受控进程。
2. 按 [步骤指引](references/stages.md) 执行当前步骤。确认执行环境可以按截止时间停止本次 Agent 和受控进程，登记本次尝试后立即执行。当前环境缺少可用的停止能力时，登记该步骤及阻断，以不可重试失败结束本次尝试并按账本结果推进。本次未启动 Agent 或进程时一并记录。E2E 读取 [环境准备](references/e2e.md)。提前完成就保存结果并立即进入下一步。
   新路线 `post_merge_e2e` 在前端合回前取得共享交付占用，保持到本单收尾。收到 `frontend_delivery_busy` 时结束当前轮次，Windows 工单启动器等待占用释放后续接。前端 E2E 由启动器接管，按[环境准备](references/e2e.md#合回后正式验收)登记请求。功能失败由开发 Agent 在已登记的主工作区按 `main_frontend_repair` 上下文修复并交宿主限定文件提交。遇到受保护文件重叠或 `main_frontend_repair_error` 时记录并协调。返工单测和独立复审使用 `main_frontend_repositories` 返回的主工作区、修复基线和新 SHA。完成后，测试 Agent 验新版本。E2E 及返工全部结束后按配置推送、部署，最终验收失败或受阻如实记录并继续发布步骤。
   派发 `backend.code_review` 或 `frontend.code_review`（含返工复审分段）时，在测试 Agent 的阶段提示中传入各仓的工作树、基线、当前候选 SHA 和证据目录，并要求按 [代码审查](references/stages.md#代码审查) 逐仓准备 OCR 评审材料。
   后端阶段把回执里的 `backend_dependencies` 交给对应 Agent。先看业务服务自己的接口、实现与调用链，只有本单实际涉及上游时才按[依赖配置](references/backend.md#上游依赖与修改权限)查契约或登记修改目标。前端仍从已配置业务后端和联调交接获取接口契约。
   当账本进入 `backend.push`、`frontend.push`、`backend.deploy` 或 `frontend.deploy`（含返工分段）时，结束本轮并把执行权交还 Windows 工单启动器。受控执行器认领、执行和登记结果，启动器恢复原工单聊天。遇到 `deployment_worker_required` 也走同一交接。Git 推送见[步骤指引](references/stages.md)，镜像发布见[前端镜像发布](references/frontend-release.md)。
   Story 的材料评审尝试返回 `material_sync_required:true` 时，调用只读状态命令 `record material_sync`。返回 `material_sync_worker_required` 时立即结束当前轮次，由 Windows 启动器确认原 CLI 和 Task 进程停止后，在宿主按原尝试截止时间同步需求仓库，再恢复原会话。禁止在编码沙箱内自行执行需求仓库 fetch、读取或转存 Git 凭据。恢复后用同一命令读回；只有 `material_synced` 才派发评审 Agent。`material_sync_failed` 按原尝试的普通失败处理，停止不明时不重启、不发布成功回执。命令和回执见[步骤指引](references/stages.md#需求材料同步)。
   前后端的分支补取、建树、候选提交和合回按[受控 Git 流程](references/branches.md)执行。`branch_prepare`、`candidate_commit`、`integration_merge` 只登记固定请求；返回 `host_git_worker_required` 时立即结束本轮，启动器核实全部进程停止后执行，再恢复原聊天读取回执。阶段开发 Agent 只编辑、构建和测试，返回逐仓文件清单、原 HEAD 和提交说明；候选提交只由主聊天在 coding 阶段或返工 coding 分段申请。单测需要改代码时交账本返回 coding 修订再重验。禁止在沙箱自行 fetch、worktree add、commit、checkout、merge、rebase、cherry-pick 或跳过 hooks。保留已成功仓库和失败检查日志，不用新请求绕过未确认的停止状态或失败 hooks。
   返回 `story_review_required:true` 时按[禅道正式评审](references/stages.md#禅道正式评审)核实评审目标。需求评审 Agent 负责材料结论及理由。`attempt_finish` 返回 `disposition:review_wait` 时结束当前轮次，由 Windows 启动器提交、回读并决定续接。
   材料评审通过后，把[知识库查询规则](references/stages.md#公司知识库查询)交给拆分和设计 Agent。设计 Agent 读取本轮 `split.md` 中已有的知识引用。
3. 使用 `record` 保存步骤、尝试、工单和聊天绑定、实际起止时间、结果、证据与失败原因。阶段结果与待回写记录一起提交。确认失败可重试时按配置执行，默认首次执行后最多重试三次，每次独立计时。
4. 需求和 Bug 通过材料评审、任务拆分后，按账本顺序逐步推进。实际执行失败且无法重试或已用尽重试次数时，登记本步骤失败，按 `next_stage` 继续。某步检查发现缺少必要输入或无可执行任务时，用 `outcome:skipped` 登记本步原因及证据后继续。尚可执行的任务或仓库按各步条件继续处理。记录失败、跳过和需要续接的内容，完成路线后汇总收尾。已走完路线且仍有未验证项的工单记为「待验证」。

新领取的 Story 先由评审 Agent 检查材料，再由拆分 Agent 按项目配置、材料标记、AI 研判的优先级确认范围，列出各端任务、实际依赖与验收条件。启用 `runtime.streamlined_bug_intake` 后，新领取的 Bug 由主会话直接执行[接单核对](references/stages.md#bug-接单核对)，账本随后进入分支准备。主会话按账本返回的阶段及 `task_scope.active` 派发开发或测试 Agent，负责[分支核验与工作树](references/branches.md)、逐仓合回及最终收尾。每个阶段用 `evidence.task_blockers` 记录局部阻断的任务编号和原因，继续验证独立任务。跨端任务按实际依赖验证[联调交接](references/backend.md)，独立前端任务可在交接受阻时按账本继续。收尾汇总实际交付和待续接范围。已领取的旧工单沿原阶段名和计划继续。收尾后由当前调度轮次按队首领取下一单。

Windows 上需要受控超时的 Agent 和本次自有进程使用 [阶段运行器](references/stage-runner.md) 启动。把 SQLite 返回的绝对截止时间传入运行器，按它的终态和清理证据登记原尝试。已启动的用户服务不进入本次受控进程组。

启用 `runtime.split_task_sync` 的新 Story 由启动器为拆分 Agent 并行管理 Task。拆分登记后研发即可开始；同步器读取原 Story 的迭代、项目和建单时指派账号，每个稳定拆分编号对应一张 Task。研发和测试按[步骤指引](references/stages.md)提供逐项证据，账本在独立验收、相关仓库合回及已配置的 Git 推送和部署核验条件满足时安排关闭。迭代待定时保留待建单，原任务返工后重开同一 Task。收尾备注和进度页分别报告研发与禅道现状。

启用 `runtime.split_task_progress_sync` 的新 Story 在实际开工后同步「进行中」，遇到部署、依赖或联调验收受阻时按任务追加备注。等待前置依赖的任务保持「未开始」。工单主聊天继续按任务编号登记阶段结果，任务同步器负责写入并读回。

## 禅道回写

每个步骤结束时为对应原需求或 Bug 保存一句备注，包含步骤、实际起止时间和结果。失败说明原因及重试次数，用户主动停跑时记录停跑时间及余下步骤。最终备注逐项汇总实际结果。有 `blockers.md` 时优先上传并回读。附件无法上传时，在备注中写关键摘要及已核实可共享的文件位置。缺少可共享位置时，写明附件待补发，保留本地文件和写回记录。

写回前确认当前实例对原需求或 Bug 的独立评论、附件上传及关联读回路径已经验证，操作符合当前授权。领取待写回项后按稳定操作标识处理。每次外部写入前通过 `record writeback_send` 取得一次性发送凭证。请求结果不明时先读回目标工单，只有确认旧请求终止且记录缺失才申请补发。真实外部回执通过 `record` 保存。接口或权限尚不可用时，保留待写回项和原因，研发队列按本地收尾继续推进。

当前禅道实例已验证需求备注和附件回写。需求备注的提交响应不明确时，按稳定操作标识在实际需求的操作历史中匹配。附件使用 REST v2 `POST /api.php/v2/files` 的表单上传，将 `objectType` 和 `objectID` 随表单提交。完成后核对文件 ID、实际关联对象和下载内容。旧式页面上传调用曾生成未关联副本，使用验证过的 REST 路径。Bug 的回写仍需用真实 Bug 另行验证。[禅道附件上传接口](https://www.zentao.net/book/api/post-files-2325.html)

正式定时接单由用户授权后启用。本机已经验证同一桌面聊天可续接。一次受控超时探针验证了在同一个截止事件后中断 Agent，并显式结束该尝试进程树。正式运行前应在步骤执行环境中验收自动停止与状态读回。
