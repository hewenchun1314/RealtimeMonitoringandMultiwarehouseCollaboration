# 工单步骤和交付证据

使用工单配置快照中的步骤时限。每个时限都是单次尝试的最长耗时，从实际开始时起算，提前完成立即推进。记录每次尝试的开始、截止、结束和结论。失败后最多重试三次，重启沿用原尝试、原次数和原工单聊天。

## 按阶段安排验收

需求和 Bug 的材料评审、任务拆分和实施计划遵循[工单材料与流程冲突规则](../SKILL.md#工单材料与流程冲突)。主会话在派发阶段 Agent 时传递已核对的流程决定。设计或计划引用旧材料时，按同一规则处理其中的冲突。

拆分时把本地可验证的开发成果放入端内验收。镜像构建、推送、滚动更新和部署后业务复验属于后续阶段的交付检查，不列为本地验收必须通过的独立开发任务，也不作为本地验收的前置条件。计划明确各项检查的阶段和所需证据。部署后的业务复验以实际运行版本为准，记录真实业务结果。缺少对应环节、环境或数据时，在工单收尾中如实记录待验证事项。

开发和本端全部计划仓库合回成功后，继续执行已配置且获授权的推送和部署。单测、后端服务验收和前端页面 E2E 的失败按原结果入账，相关验收项保留待验证状态。

合回最终失败、超时或仅部分成功时，工单以「阻断」收尾，后续推送和部署标记为跳过。保留已合回仓库的实际 SHA 和未合回仓库的原因，供恢复时核对。

## 新领取的工单

当 Story 领取快照启用 `runtime.split_task_sync`，拆分 Agent 按任务编号同步禅道 Task。相同拆分项跨修订保留编号，新增独立要求另用新编号。每端沿正常研发路径提交以下补充证据。

| 阶段 | 逐项证据 |
|---|---|
| 拆分 | `tasks.frontend`、`tasks.backend` 保留现有编号、描述、依赖及验收条件。跨端工单中需要页面联合验收才能交付的后端任务加 `"joint_acceptance": true`。 |
| 前端或后端计划 | 在原有 `applications` 或 `services` 之外，提供 `"task_repositories":{"F1":["app"],"F2":["app","shell"]}`。每项覆盖实际相关的全部本端仓库；跨端前端计划还列出需联合验收的后端任务对应的前端仓库。 |
| 服务验收或页面 E2E | 在原版本和 `verified_tasks` 字段之外，提供 `"task_results":{"F1":{"outcome":"passed","verification":"候选版本上的具体用例、观测结果或证据路径"}}`。结果为 `passed`、`failed` 或 `pending`。整步成功须覆盖本端所有任务；部分失败时分别记录能够独立核实的结果和受验版本。跨端页面验收同时提供实际交接的 `backend_versions`。需要联合验收的后端项也写入 `task_results`；联调发现已关闭的后端原项有缺陷时，在该回执中明确给出原任务编号和失败证据。 |
| 逐仓合回 | 保留 `versions`、`merged_versions` 及现有最终内容复验要求。目标树有变化时，针对受影响任务补充 `"merged_task_verification":{"F1":"最终合回版本的用例及实际结果"}`。部分仓库成功时，失败回执也要列出已经实地读回的合回版本。 |

工单回执由本地账本分别核对任务对应的候选、验收结果和目标分支。拆分 Agent 根据有效证据办理关闭、返工重开并核实禅道实际状态。

从本单 `attempt_start` 回执取实际步骤、角色和时限。证据保存在账本所在目录的 `items/<marker>/round-<run_round>/` 下。主会话负责派发、两端的分支核验与合回、交接和收尾。每个被选中的端由本端开发 Agent 负责设计到单测，由本端测试 Agent 独立审查与验收。纯后端工单只读取后端入口，纯前端工单只读取前端入口。

| 阶段 | 负责人 | 操作和成功证据 |
| --- | --- | --- |
| `material_check` | 主会话、评审 Agent | Story 先按[需求材料同步](#需求材料同步)同步仓库，再核对实际目录并提交 `evidence.path`，例如 `requirements/ZT-123/intent.md`，相对需求工作区。Bug 提交原单复现详情 `evidence.detail`。账本解析材料正文并保留摘要和原标记。需求文件确实缺失时以 `failed` 和 `material_file_status:"missing"` 提交。 |
| `split` | 拆分 Agent | 项目单端设置决定实际范围。项目支持两端时优先用文档标记，缺标记再根据代码、接口与验收条件研判；双端配置中标记非法或矛盾时记录歧义。写 `split.md` 和结构化任务证据，格式见[账本命令](workflow-cli.md)。将超出范围的验收条件写入 `blockers`。 |
| `<端>.design`、`<端>.plan` | 本端开发 Agent | 按拆分清单在本轮的 `frontend/` 或 `backend/` 写 `design.md`、`plan.md`。设计附文件证据，计划登记非空且唯一的应用键数组 `applications` 或服务键数组 `services`。 |
| `<端>.branch_check` | 主会话 | 按[分支核验](branches.md)准备并实地核对每个目标仓库的 worktree，提交完整 `repositories`。 |
| `<端>.coding` | 本端开发 Agent、主会话 | 开发 Agent 在已核验工作树编辑、构建和测试，返回逐仓文件清单、原 HEAD、提交说明及验证证据。主会话通过 [candidate_commit](branches.md#编码与候选提交) 交宿主运行原 hooks，恢复后把成功 `versions` 登记为候选。后端还运行配置的构建命令，跨端时编写联调文档。 |
| `<端>.unit_test` | 本端开发 Agent | 对有效 coding 的同一 `versions` 跑测试，以 `verification` 保存实际命令、结果和证据；不得自行提交。需要代码或生成文件修订时，失败结果传 `repair_required:true`，回到 coding 后重跑单测。 |
| `<端>.code_review` | 本端测试 Agent | 按[代码审查](#代码审查)逐仓准备 OCR 文件清单和评审规则，审查本端计划及候选改动。提交 `versions` 和 `verification`，记录位置、结论和严重度。 |
| `backend.acceptance`、`frontend.e2e` | 本端测试 Agent | 按[后端服务验收](backend.md)或[前端页面验收](e2e.md)实际运行用例。提交 `versions`、`verification` 和逐项验证的本端任务编号数组 `verified_tasks`，覆盖当前有效清单。前端通过或代码返工须提交本轮 `agentlimb_receipt`。联合验收加 `backend_versions`，核对已交接服务版本。 |
| `<端>.integration` | 主会话、宿主执行器 | 通过 [integration_merge](branches.md#合回与恢复) 请求受控合回，结束本轮交宿主执行。目标前进时，在宿主回执指定的独立工作树复验整合 SHA，通过后提交 `verified_integration_candidates` 和 `verification`。恢复后保存候选 `versions` 及实际 `merged_versions`。最终树不同还需 `merged_verification`。冲突按受阻任务登记，保留其他仓库的实际结果。 |
| `backend.push`、`frontend.push` | Windows 工单启动器和 Git 推送执行器 | 工单快照启用 `runtime.git_push_enabled` 时，开发和全部计划仓库合回成功后逐仓推送合回 SHA。核对快照、当前配置和分支核验回执，向配置分支普通推送并读回远端。已包含该 SHA 时登记已有远端版本。遇到分叉、权限或目标错误时记录该仓失败，保留其他仓结果。 |
| `backend.deploy` | Windows 工单启动器和发布执行器 | 开发、合回和已启用的 Git 推送成功后，按快照和当前授权选择实际服务的部署目标。用已合回版本打包并推送，按摘要更新 Deployment，核验 Pod 与网关版本后自动登记回执。工单主会话在此阶段交还执行权。详见[后端部署](backend.md#后端自动部署)。 |
| `frontend.deploy` | Windows 工单启动器和前端部署 Agent | 核验前端 Git 远端已包含合回 SHA，核对本地停服，从固定 SHA 打包并读回 Harbor 摘要。配置 Rancher 时继续更新并核验新 Ready Pod。详见[前端镜像发布](frontend-release.md)。 |
| `handoff` | 主会话 | 仅跨端运行。后端合回后读取联调文档、独立验收和实际环境版本，提交[交接证据](backend.md)。交接失败后记录依赖交接的任务，按账本继续独立的前端任务。 |

端内返工依次完成修复、单测、复审或重验。联合验收发现后端缺陷时注明责任端，按同一尝试中账本返回的分段修复并再次交接，使用新版本跑受影响的联合验收。涉及任务或接口约定变化时，由拆分 Agent 更新任务并说明依据。主会话收尾以有效版本、实际验收和所有必需任务为准；安全合回成功的事实与未解决的验收项分别记账。

编码、单测、审查和验收登记成功时，账本会读回工单工作树的 HEAD、已跟踪文件和暂存区。单测发现需要修订时，普通单测失败的 `repair_required:true` 生成同端 coding→verify 序列；返工中单测失败则回到紧邻的同端 coding，保留此前有效历史。新 coding 回执使旧版本的下游证据失效，单测、审查和验收按新 SHA 重跑。跨轮恢复先复核保留的历史。hooks 自动改文件、提交后检查失败或回执丢失按[候选恢复](branches.md#编码与候选提交)处理，不能直接登记旧版本成功。主工作区中用户已有的改动须保留。

## 代码审查

Windows 工单运行账号使用固定的 Open Code Review v1.14.2。默认路径为 `%LOCALAPPDATA%\Programs\ocr\1.14.2\ocr.exe`。安装时从 [GitHub Release](https://github.com/alibaba/open-code-review/releases/tag/v1.14.2) 取对应架构的二进制并对照发布摘要核验 SHA-256。在工单环境执行该二进制的 `version` 检查可用性。委托模式复用阶段 Codex 的模型。

1. 主会话按当前有效任务和阶段派发测试 Agent。初审、代码审查失败后的 `verify` 复审，以及其他阶段返工进入的 `<端>.code_review` 分段，都使用这个流程。按仓库键读回本端 `branch_check` 的 `repositories` 和有效的 `coding` 候选 `versions`。返工读取新候选，评审前核对对应单测回执和工作树版本。
2. 测试 Agent 在现有受控阶段内逐仓运行以下命令。`$ocrScript` 是项目根目录下 `.agents/skills/zentao-autodelivery/scripts/ocr_review.py` 的绝对路径。`$worktree` 和 `$baseSha` 分别取仓库核验回执的 `worktree`、`base_commit`。`$ocrEvidence` 是本次工单轮次内按尝试、分段、仓库键和候选 SHA 区分的 JSON 路径，位于工作树外。

   ```powershell
   python -B $ocrScript --repo $worktree --base $baseSha --candidate $candidateSha --output $ocrEvidence
   ```

3. 报告为 `prepared` 时，按 `preview.reviewable_files` 和 `rules` 审查固定提交区间，逐项注明已审查或跳过原因。看 `preview.excluded_files` 的排除理由，对确实影响本单的接口、测试和配置改动补审，遵守敏感路径保护。结合本单计划、拆分任务和验收条件确认发现及严重度。OCR 目录权限、命令失败、超时或输出异常时记录实际原因，在本阶段剩余时间内用原独立审查方式核对候选版本。
4. 逐仓保存文件覆盖、补审范围、缺陷位置、受影响任务及结论，回执登记实际审查的 `versions` 和引用证据的 `verification`。确认确需改代码的严重问题按现有 `repair_required:true` 流程交开发 Agent 修复并复审。OCR 报告保存在工单证据目录，禅道回写使用审查结论及受影响任务摘要。

## 公司知识库查询

材料评审使用工单材料。材料评审通过后，拆分 Agent 在现有能力、应用归属或跨应用依赖需要确认时查询公司 MCP `company-knowledge`。按功能名、业务对象或页面入口调用 `search_knowledge`，用命中条目的 `itemId` 和 `versionId` 调用 `get_knowledge`。在 `split.md` 记录引用、发布状态和它影响的拆分决定。研发范围仍按项目配置和工单材料确定。

设计 Agent 先读 `split.md` 的引用。需要补充共享组件、接口或上游依赖的证据时，再查原文或用 `traverse_knowledge` 查关系。在实际目标仓库验证源码或接口，在 `design.md` 记录采用的结论和核验位置。独立验收沿用已记录的引用，并以候选版本上的实际验证为准。

无命中、鉴权失败或调用超时时，在当前阶段时限内记下原因，继续检查工单材料、源码和实际接口。只有关键条件仍无法核实才按现有规则记录受影响任务。使用与当前任务相关的最少查询信息，交付文档只保存所用条目的引用和结论。API Key 由启动环境通过 `KNOWLEDGE_HUB_API_KEY` 提供。

## 上线前已领取的工单

快照无新流程版本的工单继续使用下表中的原阶段名、超时和回写键。

| 步骤 | 负责人 | 单次最长耗时 | 输入和交付 |
| --- | --- | ---: | --- |
| `material_check` | 评审 Agent | 10 分钟 | 需求按工单登记的相对材料路径核对需求仓库中的对应目录和 `intent.md`，找到即通过，结果附实际路径。Bug 核对原工单详情和复现材料后交开发。记录缺失项或读取错误。 |
| `design` | 开发 Agent | 45 分钟 | 阅读需求材料或 Bug 复现信息，写 `design.md`，记录方案与影响范围。发现必须改后端时，记录超出 `frontend_only` 的原因。 |
| `plan` | 开发 Agent | 45 分钟 | 写 `plan.md`，明确实施顺序、单测安排、验收条件和本单要改动的应用配置键。 |
| `branch_check` | 工单主聊天 | 15 分钟 | 核对本单涉及的 Git 仓库和目标分支，从目标分支提交创建或续接工单 worktree，读回分支和基线提交。 |
| `coding` | 开发 Agent | 120 分钟 | 使用分支核验确认的工单 worktree 实施，记录实际仓库、目标分支、worktree 和待验版本。操作账本时仍显式指定项目根目录为工作目录。原配置快照无分支阶段的旧单沿用已登记工作区及原步骤。 |
| `unit_test` | 开发 Agent | 45 分钟 | 编写或更新相关单测，运行后保存实际命令、结果和待验版本。 |
| `code_review` | 测试 Agent | 45 分钟 | 审查本次代码改动，记录版本、结论及问题位置和严重程度。严重问题交原开发 Agent 修复并复审；普通问题记录后继续。 |
| `e2e` | 测试 Agent | 90 分钟 | 从实际应用入口验证计划中的验收场景，记录版本、启动工作区、工区、结果和证据。严重问题修复后，复审相关改动并重跑受影响场景。 |
| `integration` 合回与核验 | 工单主聊天 | 45 分钟 | 逐仓核对本单候选提交、已登记的目标分支及用户已有改动。即使上游审查或验证失败，仍将可安全合回的候选代码合回，并读回实际提交。缺少候选、目标不明或冲突无法安全解决时，记录对应仓库的具体原因，继续处理其余仓库。合回结果不明时，先检查实际 Git 版本和状态再决定是否重试。合回最终受阻时，按「阻断」收尾，后续步骤记为跳过。 |

材料路径必须落在配置需求仓库内。标准需求正文只登记目录标识，例如 `ZT-123/`，此时检查仓库中的 `requirements/ZT-123/intent.md`。需求评审确认未提供需求文件或登记目录缺少 `intent.md` 时，在 evidence 中记录 `material_file_status:"missing"` 及核对依据，以 failed 提交。账本直接以「待补材料」收尾。若仓库暂不可读、路径待核实或权限不足，记录具体错误，按普通失败处理。Bug 评审沿用复现材料规则。已知材料不足、必须修改后端或存在其他明确无法继续的情况，登记本步骤的证据和结果，进入下一步。

材料可用时先完成能独立开展的 `design` 和 `plan`。目标子仓、Git 基线或待验服务暂缺时，在文档标记待查证的源码路径、测试命令和版本，再在分支核验、编码或 E2E 记录各自的实际阻断。已运行服务上的可执行场景可以独立补验，证据注明实际环境和覆盖范围。当前步骤确实缺少必要输入时，以实际原因登记该步失败，然后进入下一步。

设计、计划、开发、测试、审查、联调、合回及部署均按账本的任务范围推进。按任务编号记录阻断、受影响的验收项、证据和恢复条件。验收保留实际通过、失败和待验证的独立场景。合回前确认候选提交只包含本次可交付范围，含受阻变更的候选须隔离并复验。账本返回下一阶段时继续执行，进入收尾时汇报部分交付及待续接任务。

核心验收失败、原 Bug 仍能复现、已有功能明显回归，视为严重问题。Code Review 或 E2E 失败且确需改代码时，通过 `repair_required:true` 登记返工。下一次尝试按账本返回分段处理，依次由开发 Agent 修复并运行相关单测，E2E 返工额外由测试 Agent 复审，最后回到当前步骤验证。环境失败则按普通重试处理。

整轮返工占用当前 Code Review 或 E2E 的一次重试。每个分段成功后，以 `repair_advance` 保存证据、继续同一次尝试，并使用下一分段返回的时限。最终验证使用 `attempt_finish`，任一分段失败或超时也通过 `attempt_finish` 结束当前尝试。将修复后的候选提交和各项验证结果一并交给合回步骤，最终汇总保留未解决的问题。

## 需求材料同步

项目启用 `runtime.sync_requirement_before_review` 时，Story 的每次材料评审尝试（含重试和重新评审）先同步。领取快照和当前配置须指向同一个独立 Git 需求仓库。仓库沿用当前分支已有的远端上游，只允许快进，Git 遇到会覆盖的本地编辑时会拒绝更新。

主会话调用 `record material_sync`，提供 `item`、原 `request_id`、`chat_id` 和 `attempt_id`。该命令仅查状态，不在沙箱中执行 Git。返回 `material_sync_worker_required` 时立即结束当前 CLI 轮次，把执行权交还 Windows 工单启动器，不另起聊天、不请求权限、不尝试在沙箱读取或转存 Git 凭据。

启动器确认原 CLI 和 Task 同步进程已停止，再以同一尝试的绝对截止时间和 Windows Job Object 在宿主执行固定的需求仓库同步。凭据仍由宿主 Git Credential Manager 解析，不进入提示、参数、账本或交付文件。执行器只能使用已核对相同的当前配置和快照仓库及其已有上游，不接受自由仓库或命令。子进程先计算结果；外层确认进程全部停止、原绑定仍有效且 HEAD/分支/上游与结果一致后，才原子写入同步成功回执和本次终态。

宿主成功或确定失败后恢复同一聊天，再次查询原尝试的 `record material_sync`。取得 `material_synced` 后，把回执里的提交交给评审 Agent，用原分段 `verify` 继续。`material_sync_failed` 使用原尝试登记实际失败和运行回执，再按账本重试；不能以认证故障推断缺材料。停止不明时保留原名额和证据，不重复同步、不发布成功回执。启动器恢复先对账稳定目录及命名 Job Object。评审开始前的认证预检由 `worker.py git-preflight` 在宿主完成；页面 E2E 关闭时同样执行。

账本取得该尝试的同步成功回执后才接受评审成功或「待补材料」。评审成功时账本核对本地 `intent.md` 与该提交一致。只读核验显式使用需求仓库的 Git 元数据与工作树路径，不用 `git -C` 或切换进只读目录；文件哈希保持相对 `--path` 的清理规则，并用绝对文件路径读取材料。普通 `.git` 目录和关联 worktree 的 `.git` 指针均按固定路径解析。若已知材料目录但文件缺失，随 `material_file_status:"missing"` 提供预期 `evidence.path`，账本在同步提交中核对该路径。


## 禅道正式评审

启用 `runtime.story_review_sync` 的新 Story 领取后，`material_check` 的 `attempt_start` 返回 `story_review_required:true`。在本次尝试开始后、派发需求评审 Agent 前，从项目根目录执行 `python -B .agents/skills/zentao-autodelivery/scripts/worker.py review-target --marker <marker>`。取得 `review_target_verified` 回执后，把禅道版本、轮次和材料 Git 提交交给评审 Agent。启动器将目标写入当前尝试的账本。目标读取失败按执行故障登记及重试。

评审 Agent 提交 `attempt_finish` 的材料结论，沿用 `evidence.path`。材料通过时提交 `success`，系统生成「通过」及材料路径和版本。明确缺少文件时提交 `failed` 和 `evidence.material_file_status:"missing"`。文件可读但有已核实的关键缺项时提交 `failed`、实际 `evidence.path` 和 `evidence.material_review_status:"needs_clarification"`。失败的 `summary` 写明具体缺项、影响和需要补什么，系统将它作为「有待明确」的评审备注。同步、权限或超时等执行故障按普通失败处理。

`disposition:review_wait` 表示已将正式评审加入同一事务的回写待办，结束当前工单轮次。Windows 启动器确认工单轮次及其受控进程停止后，释放研发名额，再用原 Story 的禅道 22.6 正式评审表单提交。原生提交按 Story ID 执行，启动器在提交前核对版本、轮次和账号，提交后读回实际评审历史。已核实的通过意见等到 Story 激活再进入拆分。「有待明确」读回后按「待补材料」收尾。材料通过且评审前已激活的 Story 沿用禅道状态。暂时无法读回的提交保留待对账，出现版本或轮次变更则收尾为「阻断」，由原工单重新评审。监测轮次持续核对并用原会话续接。

提交结果不明且后续一直没有回执时，确认上一次请求已结束后，可运行 `python -B .agents/skills/zentao-autodelivery/scripts/worker.py review-reconcile --marker <marker> --prior-ended`。程序重新检查禅道历史与本轮待评审资格，并在原写入租约结束后恢复这条待办。监测轮次负责下一次提交。

## 超时和受控停止

1. 开始每次尝试前，在对应工具中确认能够按时限停止执行。登记实际 Agent 或工具会话及其启动的受控进程归属。进程身份至少核对 PID、启动时间和所属尝试，保留启动工作区或等效证据。
2. 到达截止时间后停止本次执行。中断 Agent 或返回工具超时只说明该层停止请求，另核对本次启动的进程树。必要时使用已验证的受控进程停止手段，只结束归属于本次尝试的进程，保留用户原有服务。记录 Agent 状态、进程退出或有效隔离的核验结果。
3. 确认停止或有效隔离后才允许重试或释放执行名额。执行环境缺少可靠停止能力时记录阻断证据。停止与隔离均未获证实时保留名额并继续对账，待写回等独立事项可以继续推进。

不得将时间预算、Agent 中断回执、shell 会话退出和进程树退出混作同一份证据。保存结果前核对本次尝试仍有效。迟到的结果只关联原尝试，避免推进当前步骤。
