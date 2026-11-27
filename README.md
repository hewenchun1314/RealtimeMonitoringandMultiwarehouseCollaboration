# 海能发地质工程一体化 · 禅道无人值守交付

监测进程扫描需求和 Bug，按优先级排队，为每张新单建立一个 Codex 会话。工单依次完成材料核对、拆分、开发、独立验证、合回及配置的推送和部署。阶段结果、收尾结论和附件通过持久队列回写原单。处于评审中且材料齐全的需求可直接开工。

## 启动

从仓库根目录运行。需要 Python 3.11+、Git、已登录的 Codex CLI，以及工单所涉及的仓库和工具。前端验收需要本机已配对且已登录的 AgentLimb 浏览器，发布需要可访问的镜像仓库和部署环境。

1. 核对 [.agents/zentao-project.json](.agents/zentao-project.json) 的实例、项目/产品、接单账号、仓库和目标环境。实例地址应能从当前宿主访问。
2. 在 `.local/zentao-credentials.json` 写入 `{"base_url":"与项目配置相同的地址","username":"禅道账号","password":"禅道密码"}`。此本地文件由当前宿主读取。启用钉钉时，可运行 `python .agents/skills/zentao-autodelivery/scripts/dingtalk_notify.py configure`，凭据保存在 `.local/dingtalk-credentials.json`。
3. 需要诊断 Codex 环境时运行 `python .agents/skills/zentao-autodelivery/scripts/worker.py probe`。可通过项目配置 `runtime.codex_cli` 指定可执行文件。
4. 运行以下命令启动持续监测。一次扫描、派单及回写使用 `--once`。仅扫描入队可省略参数。持续监测可在 Codex 桌面关闭后运行。

```sh
python .agents/skills/zentao-autodelivery/scripts/monitor.py --watch
```

首次完整扫描将已有符合范围的单登记为基线。以后新发现的单才进入队列。每张单由原会话续接，启动、绑定、阶段尝试和外部写入都有可核对的记录。监测间隔由 `monitor.interval_minutes` 配置，执行中的工单和待回写事项在扫描间隔内也会被核对。单次模式派出独立的工单进程，后续补发和调度由下次监测处理。

## 提交工单

- 需求材料放在需求仓库的 `requirements/` 下。在该需求目录的 `intent.md` 中写清目标和验收条件，在禅道原需求的描述中写明材料目录。
- Bug 在原单写明复现步骤、实际结果和预期结果。当前项目接收指派给 `DOt-hwc` 的 `active` Bug，以及 `reviewing` 或 `active` 需求。
- 原单备注可查看阶段结果。进度页可查看本地拆分项、已合回版本、阻断原因和待回写数。

## 运行记录

新账本位于 `.local/zentao-delivery-v2/workflow.sqlite3`，进度页为同目录下的 `status.html`。每单的证据和派发日志保存在 `items/<marker>/`。新入口从空账本建基线，历史目录可独立保留。拆分项只在本地登记。禅道账号、钉钉密钥和部署验证凭据保留在本地配置文件中。

同一项目的一组监测和执行进程使用同一台宿主及本地文件系统，便于恢复原 Codex 会话、仓库状态和进程。SQLite 领取事务与按工单、仓库和浏览器资源的锁允许独立工单并发。结果不明时先核对原版本、操作标识和进程结果，再决定是否续办。

## 验证

Windows、macOS 和 Linux 使用相同的离线验收入口，需要 Node.js、npm、Python 和 Git。将以下命令中的 `python` 替换为本机 Python 3.11+ 的命令即可。测试使用临时账本、测试仓库、模拟禅道和发布回执。

```sh
python .agents/skills/zentao-autodelivery/scripts/verify.py
```

实机联调使用测试项目及独立配置。在目标宿主建立首次基线，提交新的 reviewing 需求和 active Bug，观察重复扫描、会话恢复、Git 与页面验收，再比对测试集群运行版本、原单备注和附件。资格、工作目录和服务地址均取自该测试环境配置。

运行与接口细节见[工单执行说明](.agents/skills/zentao-autodelivery/SKILL.md)、[账本命令](.agents/skills/zentao-autodelivery/references/workflow-cli.md)、[前端验收](.agents/skills/zentao-autodelivery/references/e2e.md)和[后端验收与发布](.agents/skills/zentao-autodelivery/references/backend.md)。

使用 Codex 定时接单时，把任务绑定到本机的项目工作区，任务提示词见[禅道定时接单指令](docs/禅道定时接单指令.md)。
