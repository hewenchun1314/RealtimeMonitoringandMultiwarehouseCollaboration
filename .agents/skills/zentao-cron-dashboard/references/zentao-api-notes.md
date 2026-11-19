# 禅道 REST API 速查（看板采集相关）

实测环境：禅道开源版 22.4（Docker）、企业部署 20.x/21.x 均适用。

## 1. 认证

| 版本 | 登录接口 | 成功状态码 | 说明 |
| --- | --- | --- | --- |
| v2（18+） | `POST /api.php/v2/users/login`，body `{"account","password"}` | **200** | 返回 `{"status":"success","token":"..."}` |
| v1（12~17） | `POST /api.php/v1/tokens`，同上 body | **201** | 返回 `{"token":"..."}`，**没有** `status` 字段 |

后续请求统一加 header：`token: <32 位 token>`（**不是** `Authorization`）。

- **别用 `status == 200` 判断登录成功**：v1 是 201，写成 `== 200` 会把 v1 判成失败。按 `200 <= status < 300` 判断。
- token 失效时返回 **401**，重新登录换新 token 即可。
- 没有 token 访问 API 一律 401/403。

## 2. 取「指派给我」的四种方式

| 方式 | 接口 | 适用 |
| --- | --- | --- |
| 直取（首选） | `GET /api.php/v2/my/tasks`、`/my/bugs`、`/my/stories`、`/my/requirements` | v2，一次拿全，最省请求 |
| 工作量概览 | `GET /api.php/v2/my/work` | 含 `todoCount`（各类计数）+ 列表，可做空库快速判断 |
| 枚举兜底 | `GET /projects` → `/projects/{id}/executions` → `/executions/{eid}/tasks?browseType=all` | 全版本可用；`/my/*` 缺失或异常时必须走这条 |
| 产品维度 | `GET /products` → `/products/{pid}/bugs` | 部分 Bug 只挂在产品上，项目维度会漏 |

**必须两路合并去重**：`/my/*` 在个别版本/权限下会返回空数组但 HTTP 200，不能只信它。
另外 `/my/stories` 返回的需求**常常不带 `project` 字段**（走产品维度），枚举路径才有；同一 ID 出现两次时要按「信息更全」合并，否则会被标成「未归属项目」。

## 3. 关键字段与坑

### 3.1 项目 / 迭代只有 ID

任务、需求、Bug 对象里 `project`、`execution`、`product` 都是**数字 ID**，没有 `projectName` / `executionName`。必须自建映射：

```
GET /projects?recPerPage=200                      -> {id: name}
GET /projects/{id}/executions?status=all          -> {id: {name, project}}
GET /executions?status=all                        -> 兜底
GET /products?recPerPage=200                      -> {id: name}
```

需求常常没有 `execution`（还没挂迭代），此时迭代列留空，不要瞎猜。

### 3.2 指派字段形态不一致（v1 / v2 都有坑）

| 场景 | 形态 | 后果 |
| --- | --- | --- |
| v2 需求/Bug/任务 | `"assignedTo": "fanqihui"`（字符串） | 直接比较即可 |
| **v1 需求/Bug** | `"assignedTo": {"id":1,"account":"admin","avatar":"","realname":"admin"}`（**对象**） | 用 `== "admin"` 比较**永远不相等**，必须取 `.account` |
| 任务被关闭后 | `"assignedTo": "closed"`（禅道用伪用户 `closed` 占位） | 「我的任务」天然看不到已关闭任务，这是禅道自身行为，不是漏采 |

采集端统一用 `account_of(value)` 取值：dict 取 `account`，字符串原样返回。

### 3.3 失败也返回 HTTP 200

| 版本 | 失败形态 |
| --- | --- |
| v2 | `{"status":"fail","message":{...}}` 或 `{"result":"fail","message":"..."}` |
| v1 | `{"result":"fail", ...}` |

判断成功要看 `status` / `result`，**不能只看 HTTP 码**。

### 3.4 列表默认过滤已关闭条目

- 任务：`GET /executions/{eid}/tasks` 默认不含 `closed`，要传 `browseType=all`
- 迭代：部分版本需要 `?status=all` 才会返回已关闭迭代
- v1 的分页参数是 `page` / `limit`，传 `recPerPage` 会被忽略（不报错，但拿不到预期条数）——大项目要按 `total` 判断是否分页没取全

### 3.5 权限相关

- `403 {"error":"Access not allowed"}`：该账号没有此模块权限（如 `/requirements`、`/user/tasks`）。**降级为告警**，不要中断整次采集。
- 某些实例 `/products` 返回 0 条（项目型产品的影子产品不进入列表），但 `/products/{id}` 可访问。别据此判定「没有产品」。

### 3.6 写操作注意（本 skill 只读，供扩展用）

- 创建需求 / Bug 时 `productID` 必须放在 **query**：`POST /api.php/v2/bugs?productID=1`，放 body 会报 `Missing required parameter: productID`
- 全生命周期模式创建需求要带 `reviewer: ["admin"]`，否则报「评审人不能为空」
- 创建 scrum 项目会自动建一个同名空产品，删除项目时容易漏

## 4. 请求量与性能

枚举模式请求数 ≈ `1 + 项目数 + 迭代数 + 产品数`。

- 项目数 > 50 或迭代 > 200 时，优先 `/my/*`；枚举可考虑只跑最近活跃项目
- `recPerPage` 用 200（禅道上限一般 1000，200 足够且稳）
- 单次采集建议超时 30s/请求，整体超过 3 分钟就应改为分页异步

## 5. 数据结构示例（v2）

```json
// GET /api.php/v2/my/tasks?recPerPage=2
{"status":"success",
 "tasks":[{"id":104,"name":"...","project":1,"execution":2,"product":1,
           "assignedTo":"fanqihui","status":"doing","pri":2,
           "estimate":"8.00","consumed":"5.00","left":"3.00",
           "deadline":"2026-10-27","lastEditedDate":"2026-10-26 13:40:00"}],
 "pager":{"recTotal":1,"recPerPage":2,"pageID":1}}
```

字段在不同版本可能缺失（如 `consumed`/`left` 在早期版本没有），采集端一律用 `.get()` 容错。

## 6. 自检清单

1. 登录返回 `token` 且长度 32
2. `GET /projects` 能列出项目
3. `GET /my/work` 的 `todoCount` 与实际业务量对得上（对不上说明权限或版本问题）
4. 任取一条任务，用 `projects[task.project]`、`executions[task.execution]` 能解析出名称
5. 失败信号（`status=fail`）能被识别，不误判为成功

## 7. 实测矩阵

| 环境 | 版本 | v2 | v1 | 备注 |
| --- | --- | --- | --- | --- |
| 本地 Docker（easysoft/zentao:22.4） | 22.4 | ✅ | ✅ | 两种模式均采到同样的 4 条研发需求；v1 登录返回 201 |
| 企业部署（NodePort 暴露） | 20.x+ | ✅ | 未测 | `/my/*` 可用；账号无数据时全程 0 条，属正常 |

`--api-version auto` 会先试 v2，失败再退 v1。已知某版本只支持 v1 时可直接 `--api-version v1`。
