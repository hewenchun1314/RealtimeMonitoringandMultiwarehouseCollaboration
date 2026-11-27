# 阶段执行与超时清理

主聊天用账本的 `attempt_id`、`phase` 和带时区的绝对 `deadline_at` 启动步骤 Agent。执行命令和仓库路径均由本次阶段及项目配置确定。在截止时间前返回的阶段立即登记证据并继续。下面的命令格式在三个宿主系统一致。

```sh
python -B .agents/skills/zentao-autodelivery/scripts/stage_runner.py --attempt-id <attempt_id> --phase <phase> --deadline-at <deadline_at> --cwd <工作树> --evidence-dir <证据目录> --stdin-file <阶段提示文件> -- <codex可执行文件> --ask-for-approval never exec --ephemeral --json --color never -
```

运行器监管本次启动的 CLI、服务及子进程。Windows 使用 Job Object，Linux 和 macOS 使用独立进程会话和监管进程。步骤命令与本单服务以前台方式启动，由运行器维持其生命周期。嵌套步骤使用同一会话中的独立进程组。Linux 监管进程可接管孤儿进程；macOS 核对会话、进程组及可追踪子树，自行脱离会话并由系统接管的后台守护程序超出监管范围。运行器在启动时登记所有者，在正常退出、超时、外部取消及恢复时核对停止证据。原本由用户运行的服务保留原有归属。

每次尝试在 `<证据目录>/<attempt_id>/<phase>/` 保存进程回执与标准输出。阶段成功还需核对实际候选版本及业务验证结果。超时时用原 `phase`、`outcome: timed_out`、`stop_confirmed` 和 `stop_evidence` 登记。无法确认本次进程退出时，先核对原尝试，等待可验证的结果。

开发 Agent 交付修改文件清单、原 HEAD、提交说明和验证证据，主会话再通过统一 Git 入口提交并读取回执。
