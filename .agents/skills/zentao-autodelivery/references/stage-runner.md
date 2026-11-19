# Windows 阶段执行与超时证据

工单主聊天从 `record attempt_start` 取得 `attempt_id`、当前 `phase` 和带时区的绝对 `deadline_at`，用该截止时间启动本次执行。步骤 Agent 用已登录的 Codex CLI 在实际工作树中运行，由 `stage_runner.py` 持有该次执行的 Windows Job Object。对同一次尝试续接时，先读证据目录。重复使用相同尝试和分段标识会拒绝启动。

```powershell
python -B .agents/skills/zentao-autodelivery/scripts/stage_runner.py `
  --attempt-id $attemptId --phase $phase --deadline-at $deadlineAt `
  --cwd $workspace --evidence-dir $evidenceDir --stdin-file $promptFile `
  -- $codexExe --ask-for-approval never exec --ephemeral --json --color never -
```

`$codexExe` 使用实际的 Codex 可执行文件。项目受控进程也可以作为阶段命令的后代启动。将工单范围、允许修改的路径和所需证据传入阶段提示，禅道文字作为需求资料读取。外部已运行的服务留在原进程中，由 E2E 按项目约定复用。

从工单派发实际使用的执行环境验证 CLI 能直接初始化、运行和收尾。父聊天的沙箱在启动子进程前生效。若 CLI 状态目录、网络或工作树权限不足，记录原始错误与停止证据，使用同一授权范围内已验证的入口恢复。禁止因这类错误向用户申请提升 shell 权限。

监督器只在启动器加入 Job 后才开闸。达到截止时间时直接终止该 Job，并检查所属活动进程数为零后给出 `stop_confirmed:true`。即使监督器异常退出，Job 句柄关闭也会终止其所属进程。作业只覆盖本次启动的本地 Codex CLI 及后代，证据中的 Codex 事件仅记录可观察到的本地会话状态。正常退出时会收尾本次遗留的后台进程。

每次分段在 `<evidenceDir>/<attempt_id>/<phase>/` 保存 `launch.json`、`result.json`、`stdout.log` 和 `stderr.log`。工单主聊天从 `result.json` 读取结果。超时且停止已确认时，把原 `phase`、`outcome: "timed_out"`、`stop_confirmed`、`stop_evidence` 和收据路径交给 `record attempt_finish`。其他退出仍须核对工作版本、测试结果和阶段证据后才登记成功。启动器未返回、收据缺失或停止未确认时按原尝试对账，收到充分停止证据前不启动下一次。
