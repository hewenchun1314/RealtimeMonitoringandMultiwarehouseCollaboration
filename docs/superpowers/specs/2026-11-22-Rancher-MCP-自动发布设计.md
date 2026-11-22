# Rancher MCP 自动发布设计

前端和 Smart-Geo 共用 Rancher MCP 更新 Deployment。各端沿用本地构建和 Harbor 推送。发布执行器从项目配置取固定的集群、命名空间、工作负载、容器和 MCP 地址，调用时核对 MCP 自检报告连接的集群。推送后只传 Harbor 核实过的不可变镜像引用。

前端局部替换以 Deployment 当前引用的 Harbor 镜像为底图，镜像摘要参与标签计算，保存底图和原引用供重试复用。更新前通过 `get_deployment` 保存已有 Pod 名和副本数，使用 `imageUpdate(apply=false)` 核对容器、原镜像和镜像拉取 Secret。随后调用 `apply=true`，轮询新一代 Pod 全部就绪，最后再读回 Deployment 镜像引用。更新前写入进度，重试发现模板已经指向目标时继续读回与等待。

账本按目标互斥，并在更新前重读原图。MCP 当前工具没有原镜像的原子比较参数，外部并发修改由写回结果和最终读回识别。当前 MCP Pod 查询提供 Pod 名和 Ready 状态，自动回执记录这些事实及模板摘要。真实试验另从 Rancher 只读取得新 Pod 的 `imageID`。

MCP 发布用 Harbor 镜像的 Git SHA 标签和模板摘要证明发布版本。Smart-Geo 同时检查直连健康接口。后端交接把该版本绑定到联调地址，联合验收前后重读目标镜像与 Pod，并运行本单接口和页面场景。现有 kubectl 存量路线继续使用 Pod 镜像 ID 和 HTTP 版本校验。

真实写权限通过同一镜像标签切换到 Harbor 核实过的等价摘要验证。逐个目标操作，核对旧镜像和当前健康状态，等待本次 rollout 完成后才验证下一目标。两个试验均返回 `dryRun=false` 与实际镜像变更。
