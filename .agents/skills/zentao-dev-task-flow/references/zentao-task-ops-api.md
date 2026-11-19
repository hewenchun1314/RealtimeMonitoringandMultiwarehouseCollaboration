# 禅道研发任务执行/收尾接口参考（API v2）

本文件是 `zentao-dev-task-flow` skill（`scripts/zentao_dev_task.py`）所依赖的禅道接口合同，供实现、排障与版本升级时复验。**改脚本或升级禅道前，先按第 6 节运行期验证清单逐项验证，并同步更新本文件。**

- 目标实例：禅道 **22.6**，默认地址 `http://192.168.110.25:30783`（无尾斜杠规范）。
- 统一使用 **API v2**：基址 `{baseUrl}/api.php/v2`，请求头 `Token: <token>`；v1（`/api.php/v1`）仅用于登录兜底与用户列表兜底。
- 本 skill 只涉及：登录、任务详情、需求详情、用户列表、创建前置任务、开始任务、完成任务、上传附件、提交备注。**不使用**任务删除/关闭/评审/依赖关系等接口（22.6 无任务依赖 REST API）。
- 所有写操作前必须有 dry-run 计划 + 用户明确同意（`--confirm`）；本文档不替代 SKILL.md 的流程与门槛说明。

## 1. 版本、基址与总体约定

| 项 | 约定 |
| --- | --- |
| API 版本 | v2（`/api.php/v2`）；v1 仅登录兜底与用户列表兜底 |
| 传输 | HTTPS 优先（本环境为 HTTP 内网）；请求体 `Content-Type: application/json`（上传为 multipart/form-data） |
| 成功 | HTTP 200（写接口常见返回 `{"status":"success"}` 或 `{"status":"success","id":N}`） |
| 业务失败 | **HTTP 200 + `{"status":"fail","message": ...}`**，message 为 string / object / array（与 HTTP 4xx/5xx 区分处理） |
| HTTP 错误 | 401 未认证；403 `{"error":"Access not allowed"}`（无权限，部分 v2 路由未认证时也返回 403）；404 `{"error":"not found"}` |
| 认证 | 请求头 `Token: <token>`（v2 token 也可用于 v1 用户列表兜底，源码同一 session 机制） |
| 时间 | 开始/完成等写接口的时间用**服务器本地时间** `YYYY-MM-DD HH:MM:SS` |
| 分页（v2） | `recPerPage`（≤1000）、`pageID`；v1 兜底用 `limit`/`page` |

请求路径中的 `{taskID}`、`{storyID}` 为数字 id；响应中 id 可能是数字或字符串，解析时统一转换为整数。**不要按错误文案分支逻辑**，只把 message/reason 展示给用户。

## 2. 认证与登录

### 2.1 v2 登录（首选）

```http
POST /api.php/v2/users/login
Content-Type: application/json

{"account": "<账号>", "password": "<密码>"}
```

**成功：HTTP 200**

```json
{
  "status": "success",
  "token": "<token>",
  "user": {"id": 3, "account": "<账号>", "realname": "<姓名>"}
}
```

**失败：HTTP 200（业务失败）**

```json
{"status": "failed", "reason": "登录失败，请检查您的用户名或密码"}
```

- 失败**不要自动重试**（连续失败会锁定账号，防锁号）；把 `reason` 如实展示给用户。
- 每个 CLI 命令独立登录一次，不跨命令复用进程内会话；可复用保存的密码，但必须遵守 SKILL.md 的 `--reuse` 复用确认规则。

### 2.2 v1 登录（兜底，仅在 v2 login 404/不可用时）

```http
POST /api.php/v1/tokens
Content-Type: application/json

{"account": "<账号>", "password": "<密码>"}
```

成功 **201** `{"token":"<token>"}`；失败 **400** `{"error": "..."}`。取得 token 后同样用 `Token: <token>` 请求头访问 v2 API（源码同一 session 机制）。

### 2.3 请求认证

```http
GET /api.php/v2/tasks/1234
Token: <token>
```

- token 等同密码：不打印、不写日志、不写 `context.json`、不进 zip/备注/仓库。
- token 失效返回 401（**部分 v2 路由未认证时返回 403**，见 3.2），重新登录一次即可；仍失败则报错（不循环重试）。

### 2.4 v2 通用路由能力（22.6 已确认）

22.6 的 **v2 通用路由**（`framework/api/router.class.php::routeV2`，约 **781-844 行**；属 generic 路由机制——按 `config/apiv2.php` 路由表把 REST 路径/方法映射到模块控制器方法，而非每个接口手写 entry）支持本 skill 需要的全部写操作：

| 能力 | 方法 + 路径 |
| --- | --- |
| 任务详情 | `GET /tasks/{taskID}` |
| 创建任务（前置任务） | `POST /tasks` |
| 开始任务 | `PUT /tasks/{taskID}/start` |
| 完成任务 | `PUT /tasks/{taskID}/finish` |
| 指派（不依赖） | `PUT /tasks/{taskID}/assignTo`（未文档化，本 skill 不依赖） |
| 备注（评论） | `POST /actions/comment?objectType=&objectID=` |
| 上传附件 | `POST /files`（v2 file 模块 multipart 特判：表单字段 `file`/`objectType`/`objectID`） |

上表是本次调研确认可用的路由；`routeV2` 属 generic 路由机制（非每接口手写 entry），升级/定制实例后需按第 6 节复验。

## 3. 错误语义

### 3.1 v2 业务失败（注意与 HTTP 码区分）

v2 接口的业务失败**常返回 HTTP 200**，靠响应体判断：

```json
{"status": "fail", "message": "任务状态不允许完成"}
{"status": "fail", "message": {"assignedTo": "用户不存在"}}
{"status": "fail", "message": ["缺少 currentConsumed"]}
```

- `message` 可能是 **string / object / array**：对象按 `key: value` 拼接、数组逐项转文本展示，**不按文案分支**。
- 判定：HTTP 2xx 且 `status` 为 `fail`/`failed` → 按接口失败处理（脚本退出 5）；**其它 2xx 且无 `status`（或 status 非 fail/failed）视为成功**，`message` 仅用于展示、不单独作为失败判据。
- 成功也可能带 `id`/`data`/`url` 等附加字段；以 `status == "success"` 为准。

### 3.2 HTTP 错误

| 状态码 | 典型响应 | 触发场景 |
| --- | --- | --- |
| 401 | `{"error":"Unauthorized"}` | 未带 token / token 失效 |
| 403 | `{"error":"Access not allowed"}` | 账号无当前模块/对象权限；**部分 v2 路由未认证时也返回 403**（实测 `PUT /api.php/v2/tasks/1/finish` 无 token 即返回 403） |
| 404 | `{"error":"not found"}` | 路径不存在、对象不存在 |

**401/403 均按登录/权限失败处理（脚本退出 5）**，不要按业务失败处理或重试；403 先重登一次确认 token，仍失败则让用户核对账号权限。

任务不存在时 v2 也可能返回 HTTP 200 `{"status":"fail","code":404,"message":"404 Not found"}`——与 404 同等按「不存在」处理。

## 4. 接口清单

| 编号 | 方法 | 路径 | 用途 | 成功响应 |
| --- | --- | --- | --- | --- |
| 4.1 | GET | `/tasks/{taskID}` | 任务详情（任务+actions） | `{status,task,actions}` |
| 4.2 | GET | `/stories/{storyID}` | 需求详情（需求创建人） | `{status,story,actions}` |
| 4.3 | GET | `/users?recPerPage=200&pageID=1` | 用户列表（校验指派人） | `{status,users,...}` |
| 4.4 | POST | `/tasks` | 创建前置任务 | `{status,id}` |
| 4.5 | PUT | `/tasks/{taskID}/start` | 开始任务 | `{status}` |
| 4.6 | PUT | `/tasks/{taskID}/finish` | 完成任务 | `{status}` |
| 4.7 | POST | `/files` | 上传任务附件（zip） | `{status,id,url}` |
| 4.8 | POST | `/actions/comment?objectType=task&objectID={taskID}` | 提交任务备注 | `{status,data}` |

### 4.1 任务详情

```http
GET /api.php/v2/tasks/1234
Token: <token>
```

响应 200：

```json
{
  "status": "success",
  "task": {
    "id": 1234,
    "name": "[训练集管理] TASK-01 基础框架",
    "status": "wait",
    "story": 88,
    "execution": 34,
    "project": 12,
    "module": 0,
    "type": "devel",
    "pri": 3,
    "estimate": 8,
    "consumed": 0,
    "left": 8,
    "estStarted": "2026-11-01",
    "deadline": "2026-11-30",
    "realStarted": null,
    "openedBy": "<任务创建人>",
    "assignedTo": "<指派人>",
    "finishedBy": "",
    "finishedDate": null,
    "storyTitle": "<需求标题>",
    "storyDeleted": "0",
    "files": {
      "77": {"id": 77, "title": "1234-delivery-20261130.zip", "url": "/file-read-77.zip"}
    },
    "actions": [
      {"id": 901, "action": "commented", "comment": "<p>...</p>", "actor": "<账号>", "date": "2026-11-28 10:00:00"}
    ]
  },
  "actions": [
    {"id": 900, "action": "started", "actor": "<账号>", "date": "2026-11-02 09:30:00"}
  ]
}
```

- `task.openedBy`/`assignedTo`/`finishedBy` 是**字符串账号**（v2），直接使用。
- `task.files` 是**以 file id 为 key 的 map**，回读时先归一化为列表再匹配附件 id/title。
- 顶层 `actions` 与 `task.actions` 均可能出现：回读备注时**优先使用顶层 `actions`（存在时覆盖 `task.actions`），缺失时回退 `task.actions`**；按内容摘要匹配最新一条。
- `task.story` 为 0/空表示未关联需求；`storyDeleted=1` 表示需求已删除，不得按需求创建人指派。

### 4.2 需求详情

```http
GET /api.php/v2/stories/88
Token: <token>
```

响应 200（节选）：

```json
{
  "status": "success",
  "story": {
    "id": 88,
    "title": "<需求标题>",
    "status": "active",
    "deleted": "0",
    "openedBy": "<需求创建人>"
  },
  "actions": []
}
```

- 需求创建人 = `story.openedBy` 字符串账号；`deleted=1` 或响应无 `story.id` 视为不存在（记录为空，收尾回退任务创建人）。
- 需求不可见/接口失败时容错，不得因此中断 init；以用户确认为准。

### 4.3 用户列表（校验指派人）

```http
GET /api.php/v2/users?recPerPage=200&pageID=1
Token: <token>
```

响应 200（v2）：

```json
{
  "status": "success",
  "users": [
    {"id": 2, "account": "<账号>", "realname": "<姓名>"},
    {"id": 5, "account": "<姓名拼音>", "realname": "<姓名>"}
  ],
  "pager": {"recTotal": 2, "recPerPage": 200, "pageID": 1}
}
```

- 用于 `prereq`/`finish --assign-back` 校验账号存在。
- **v2 `/users` 可能 403/无权限**：失败时用 v1 兜底：

```http
GET /api.php/v1/users?limit=200&page=1
Token: <token>
```

```json
{"page": 1, "total": 3, "limit": 200, "users": [{"account": "<账号>", "realname": "<姓名>"}]}
```

- v1 也不要传 `full=1`（需要 company-browse 权限）。
- 两路都失败 → 降级为告警、不阻塞（由用户确认指派账号）。

### 4.4 创建前置任务

```http
POST /api.php/v2/tasks
Token: <token>
Content-Type: application/json

{
  "name": "[前置] TASK-01 基础框架",
  "executionID": 34,
  "type": "devel",
  "assignedTo": "<协同人员账号>",
  "estStarted": "2026-11-01",
  "deadline": "2026-11-30",
  "pri": 3,
  "estimate": 8,
  "story": 88,
  "module": 0,
  "desc": "<h3>前置任务</h3><p>被阻塞任务：TASK-01 ...</p>"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | string | **是** | 默认 `[前置] <当前任务名>` |
| `executionID` | int | **是** | 当前任务所属执行（前置任务创建在同一执行下） |
| `type` | string | 常用 | 默认 `devel` |
| `assignedTo` | string | 常用 | 协同人员账号（account） |
| `estStarted` / `deadline` | date | 常用 | 默认当前任务起止；缺失用今天 / 今天+30 天并告警 |
| `pri` | int | 否 | 1~4（P0~P3 映射 1~4，默认 P2=3） |
| `estimate` | float | 否 | 预计工时 |
| `desc` | string | 否 | HTML；**必须写明被阻塞任务 id/名称/链接、所需前置条件、期望交付物、时限** |
| `story` / `module` | int | 否 | 默认沿用当前任务 |

**成功：HTTP 200** `{"status":"success","id":1236}`。

**失败**：HTTP 200 `{"status":"fail","message":...}` 或 HTTP 4xx/5xx（权限/参数）。创建成功后不自动开始前置任务、不指派给当前人。

### 4.5 开始任务

```http
PUT /api.php/v2/tasks/1234/start
Token: <token>
Content-Type: application/json

{
  "realStarted": "2026-11-02 09:30:00",
  "consumed": 0,
  "left": 8,
  "comment": "已确认开发计划，开始开发（计划：dev-plan.md）"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `realStarted` | datetime | 否 | 默认 now（脚本显式传当前本地时间） |
| `consumed` | float | 否 | 已消耗工时，开始时通常 0 |
| `left` | float | 否 | 剩余工时（脚本取 `task.left>0 ? left : (estimate>0 ? estimate : 1)`） |
| `comment` | string | 否 | 开始备注 |
| `assignedTo` | string | 否 | 指派账号（**脚本始终传当前指派人**；任务无指派人时回退登录账号） |

**成功：HTTP 200** `{"status":"success"}`（任务状态变为 `doing`）。

- **仅 `wait` 状态可开始**；已是 `doing` 视为已在开发中（脚本跳过 start 并记录，不重复调用）；其它状态按 SKILL 退出 7。
- 失败可能为 HTTP 200 `{"status":"fail","message":...}`（状态/权限/工作流限制）。

### 4.6 完成任务（finish，收尾核心）

```http
PUT /api.php/v2/tasks/1234/finish
Token: <token>
Content-Type: application/json

{
  "currentConsumed": 8,
  "realStarted": "2026-11-02 09:30:00",
  "finishedDate": "2026-11-28 17:00:00",
  "assignedTo": "<需求创建人账号>",
  "comment": "<p>研发完成，详见任务附件 zip 与开发过程总结。</p>"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `currentConsumed` | float | **是** | 本次完成消耗工时；任务旧 `consumed==0` 时必须 >0 |
| `realStarted` | datetime | **是** | 实际开始时间（本地时间 `YYYY-MM-DD HH:MM:SS`） |
| `finishedDate` | datetime | **是** | 完成时间（本地时间，脚本默认 now） |
| `assignedTo` | string | 否 | **完成后指派人**；不传时服务端默认 = 任务创建人 `task.openedBy`；脚本必须显式传「需求创建人优先、任务创建人兜底」 |
| `comment` | string | 否 | 完成备注（HTML）；独立备注失败降级时在此放 summary 内容 |

**成功：HTTP 200** `{"status":"success"}`；回读任务应为 `status=done`、`assignedTo=目标账号`。

- **finish 的默认指派规则**：未显式传 `assignedTo` 时服务端默认指派给任务创建人（22.6 源码 `module/task/zen.php` finish 默认值）。因此「指派回需求创建人」必须显式传 `assignedTo`，不能依赖默认值。
- **comment 会同时进入任务的 effort/备注记录**（完成动作），这样即使独立备注接口失败，summary 内容也不会丢。
- 失败：HTTP 200 `{"status":"fail","message":...}`（状态/工作流/权限）或 4xx/5xx；脚本先回读任务状态，已完成/已关闭时按 SKILL 退出 7。
- finish 后必须回读校验；若回读指派不一致，如实报错并提示用户在任务页面核对，**不再单独调用指派接口**（已完成任务再指派可能被拒），也不重复 finish。

### 4.7 上传任务附件（v2，zip）

```http
POST /api.php/v2/files
Token: <token>
Content-Type: multipart/form-data; boundary=...

--boundary
Content-Disposition: form-data; name="file"; filename="1234-delivery-20261130.zip"
Content-Type: application/zip

<zip bytes>
--boundary
Content-Disposition: form-data; name="objectType"

task
--boundary
Content-Disposition: form-data; name="objectID"

1234
--boundary--
```

| 字段 | 位置 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file` | multipart 文件字段 | 是 | zip 文件，字段名固定 `file` |
| `objectType` | multipart 表单字段 | 是 | 固定 `task`（**必须放表单字段，不能只放 query**） |
| `objectID` | multipart 表单字段 | 是 | 任务 id（必须放表单字段） |

**成功：HTTP 200**

```json
{"status": "success", "id": 77, "url": "/file-read-77.zip"}
```

- 文件立即以 `objectType=task`、`objectID=<taskID>` 关联到任务；随后 `GET /tasks/{id}` 回读 `task.files`（map）校验出现该 id/title。
- 上传失败（HTTP 200 `status:fail` 或 4xx/5xx）且未 `--no-attach` → **严格停止**（SKILL 退出 5），报告阶段；可重试或用户同意后手动上传 + `--no-attach` 重跑。
- 允许扩展名由实例配置决定（默认含 `zip`）；大小受 php.ini `upload_max_filesize`/`post_max_size` 限制。
- 幂等：同名 zip 已在 `task.files` 中则复用，不重复上传。

### 4.8 独立备注（v2，任务评论）

```http
POST /api.php/v2/actions/comment?objectType=task&objectID=1234
Token: <token>
Content-Type: application/json

{"comment": "<p>开发过程总结（HTML）...</p>"}
```

| 参数 | 位置 | 必填 | 说明 |
| --- | --- | --- | --- |
| `objectType` | query | 是 | 固定 `task` |
| `objectID` | query | 是 | 任务 id |
| `comment` | JSON body | 是 | 备注内容（HTML）；**空 comment 不会创建 action** |

**成功：HTTP 200** `{"status":"success","data":<actionID>}`，创建 `commented` action。

- 回读任务 `actions`（**优先顶层，缺失时回退 `task.actions`**）校验出现对应 comment；匹配成功记录 action id。
- **失败降级**：接口失败时把 summary 内容并入 finish 的 `comment`（`context.degraded` 记录），继续收尾，不停止。

## 5. 字段与语义

### 5.1 `openedBy` / `assignedTo` 形态

v2 的任务/需求详情中 `openedBy`、`assignedTo`、`finishedBy` 均为**字符串账号**（不是对象）；空值为 `""`/`null`。写接口的 `assignedTo` 也必须是字符串 account。统一 `account_of(v)`：`null → ""`，否则去空白字符串。

### 5.2 `files` map 与附件回读

- v2 任务详情 `task.files` 是 **map**：`{"<fileID>": {"id","title","url",...}}`，需归一化为列表。
- 上传附件成功后以 `file.id` 与 `title`（zip 文件名）双重匹配回读。
- `objectType=task` + `objectID=<taskID>` 决定附件归属；其它 objectType 不用于本 skill。

### 5.3 `actions` 与备注回读

- `actions` 元素结构：`{id, action, comment, actor, date}`；备注对应 `action` 含 `commented`/`Comment` 语义。
- 任务详情响应可能同时有顶层 `actions` 与 `task.actions`：**优先使用顶层 `actions`（存在时覆盖 `task.actions`），缺失时回退 `task.actions`**，按内容摘要匹配最新一条。
- 完成动作（finish）也会产生 action，其中包含 finish 的 comment（降级 summary 的校验依据）。
- finish 后回读校验：`actions` 必须含本次完成动作 `finished`；独立备注成功时还必须含 `commented` 动作（缺任一视为收尾不一致，按失败报告，不重复收尾）。

### 5.4 任务状态与操作前置状态

| 任务状态 | 含义 | 本 skill 允许的操作 |
| --- | --- | --- |
| `wait` | 未开始 | `start` 可开始；`finish` 不允许（未验收） |
| `doing` | 进行中 | `start` 视为已在开发中（跳过）；`accept`；`finish`（须验收通过） |
| `pause` | 已暂停 | `init` 允许接入；`start` 不允许（退出 7，先恢复任务） |
| `done` | 已完成 | 不可再收尾（`init`/`finish` 退出 7） |
| `closed` | 已关闭 | 不可操作（退出 7） |
| `cancel` | 已取消 | 不可操作（退出 7） |

前置约束：`start` 仅 `wait` 可调用开始接口（`doing` 跳过）；`accept` 要求本地状态 `developing`；`finish` 要求本地状态 ∈ {`accepted`,`developing`} 且 `acceptance.passed=true`，并回读任务状态不在 `done/closed/cancel`。

### 5.5 日期与时间

- 写接口 `realStarted`/`finishedDate`：服务器本地时间 `YYYY-MM-DD HH:MM:SS`。
- 任务 `estStarted`/`deadline`：`YYYY-MM-DD`；响应可能返回本地时间格式或 `null`/`0000-00-00`（历史数据），解析时归一化。
- `prepare` 记录的 Git HEAD/分支与 `start` 的 startedAt 用于 finish 的 `realStarted` 兜底：`context.startedAt → task.realStarted → now`。
- 跨时区团队以实例时区为准，不自行换算日期边界。

### 5.6 任务 URL

```text
{baseUrl}/task-view-{taskID}.html
```

示例：`http://192.168.110.25:30783/task-view-1234.html`。前置任务链接、完成报告均使用该格式。

### 5.7 附件允许扩展名与大小

- 允许扩展名来自实例配置 `$config->file->allowed`（默认包含 `zip`）；上传前不要改扩展名伪装类型。
- 大小受 php.ini `upload_max_filesize`/`post_max_size` 限制；超限时上传失败，按 4.7 严格停止/手动上传处理。

### 5.8 任务依赖（无 API 的结论）

22.6 数据库存在 `zt_relationoftasks` 表，但**没有公开的任务依赖 REST API**（v2 `/custom/relations` 在开源版无对应控制器）。因此前置关系通过以下方式表达，不写数据库：

1. 在当前任务所属执行下创建前置任务，指派协同人员；
2. 前置任务 `desc` 写明被阻塞任务 id/名称/链接、所需前置条件、期望交付物、时限；
3. 可选在当前任务下写一条 comment（`/actions/comment`）通知前置任务链接（失败降级告警，不阻塞）。

### 5.9 分页差异

- v2：`recPerPage`（≤1000）、`pageID`；v1 兜底：`limit`、`page`；响应分页字段分别为 `pager`（v2）与 `page/total/limit`（v1）。
- 用户列表用 200 条/页即可覆盖普通团队；超限按分页取全并按 account 去重。

## 6. 已知风险与运行期验证清单

| 项 | 风险 / 现状 | 运行期验证方法 |
| --- | --- | --- |
| v2 通用路由覆盖 | `finish/start/files/actions-comment` 依赖 22.6 `routeV2` 通用路由；升级/定制后可能变化 | 用低价值测试任务走一遍 start/finish/上传/评论；失败时看 HTTP 与 message |
| v2 登录失败形态 | 业务失败可能 HTTP 200 `status:failed`（而非 4xx） | 故意错密码执行 `init`，确认按退出 5 处理且不重试 |
| v1 兜底登录 | 仅 v2 login 404/不可用时才用 v1 tokens；两套 token 的 session 兼容性需实测 | 断开 v2 login 路由或看日志，确认兜底生效且用户列表可读 |
| 用户列表权限 | v2 `/users` 可能 403；v1 兜底也可能 403 | 用普通账号执行 `prereq`，确认降级告警而非失败 |
| 创建任务必填 | v2 `POST /tasks` 必填 `name,executionID`；实例工作流可能要求更多字段 | 创建一次前置任务，检查 200 success + id |
| 开始/完成状态限制 | `start` 仅 wait；`finish` 受工作流/权限限制 | 分别用 wait/doing 任务验证；失败按 message 处理 |
| 完成默认指派 | 不传 `assignedTo` 时默认任务创建人；显式传需求创建人后才生效 | finish 后回读 `assignedTo` 是否等于目标账号 |
| finish comment 入 effort | comment 会进入完成动作/effort 记录 | 完成任务后查看 actions 与 effort 是否含 comment |
| 附件上传 | 字段必须 multipart 表单（不能只 query）；大小/扩展名受限 | 上传后回读 `task.files`；超限时确认失败路径 |
| 备注为空 | 空 comment 不创建 action | 提交空备注确认无 action，脚本不误判成功 |
| files 为 map | v2 `task.files` 是 map，需归一化 | 回读后打印归一化列表确认 |
| 时间格式 | 写接口用服务器本地时间；跨时区显示差异 | 对比禅道页面显示时间与请求值 |
| 任务状态冲突 | 已完成/已关闭任务、并发操作 | 命令回读状态；SKILL 退出 7 |
| 重复上传/重复备注 | 重跑 finish 可能重复 | 同名 zip 复用 + 备注前回读；重跑前先 `status` |
| 真实写操作未实测 | 本文档基于 22.6 源码 routeV2/OpenAPI/官方手册/无凭据探测；上传、评论、finish 的实际写入未用真实账号验证 | 首次真实运行按本清单逐项验证，失败按第 7 节安全边界报告 |

## 7. 安全要求

- 密码只经 `--password-stdin` 或当次进程环境变量 `ZENTAO_PASSWORD`；**不写进命令行**（脚本不提供 `--password`）。
- 密码/token：不回显、不写日志、不写报告、不写 `context.json`、不进 zip、不进备注、不进 Git。
- zip 生成与备注内容不得包含凭据；`context.json` 永不打包。
- 最小权限：任务可见 + 可在当前执行创建任务 + 可开始/完成任务；无用户列表权限只降级告警。
- 只调用本文档列出的接口；**不删除任何禅道数据**（任务/附件/评论），不做任务关闭/评审/状态回退。
- dry-run 必须零写（不建 zip、不上传、不评论、不改任务状态、不写配置/context）；失败后重跑先回读状态，避免重复写。
- 附件上传失败不得静默继续；必须按 SKILL 严格停止并报告阶段（除非用户明确同意 `--no-attach`）。

## 8. 证据来源

本文件按以下来源整理与交叉核对（按可信度排序）：

1. **禅道 22.6 源码**（tag `zentaopms_22.6_20260917`，GitHub `easysoft/zentaopms`；与 main 同日同内容）：
   - `config/apiv2.php`（v2 路由表）
   - `framework/api/router.class.php::routeV2`（**约 781-844 行**；v2 **generic 通用路由**：按 `config/apiv2.php` 的路由表把 REST 路径/方法映射到模块控制器方法。支持 `GET /tasks/{id}`、`POST /tasks`、`PUT /tasks/{id}/start`、`PUT /tasks/{id}/finish`、`PUT /tasks/{id}/assignTo`、`POST /actions/comment`、`POST /files` 特判）
   - `framework/api/control.class.php`（v2 控制器/响应与业务失败形态）
   - `module/task/*`（任务详情/开始/完成字段与 finish 默认指派）
   - `module/file/control.php`（v2 `POST /files` 由 file 模块做 multipart 特判：字段 `file`/`objectType`/`objectID`）
   - `module/action/control.php`（`actions/comment` 创建 commented action）
   - 任务依赖表 `zt_relationoftasks` 存在但无公开 REST 维护接口
2. **官方 OpenAPI**：`easysoft/zentao-api` 仓库 `data/zentao-openapi.json`（v2 接口定义交叉核对）。
3. **官方 API 手册 / 二次开发手册**：v2 登录、任务接口、文件与评论接口的字段语义。
4. **目标实例无凭据探测**：`GET /?mode=getconfig` 返回 `version=22.6`；v1 无 token 访问返回 401 JSON，确认 API 已启用且错误体为 JSON；无 token 调用部分 v2 路由（如 `PUT /api.php/v2/tasks/1/finish`）返回 **403 `Access not allowed`**。

**证据等级说明**：以上为源码/OpenAPI/官方手册/无凭据探测结论，**未用真实账号对写接口做端到端实测**（尤其附件上传、评论、finish 的指派效果）；首次真实运行前必须按第 6 节逐项验证。若目标实例版本/发行版不同（企业版/旗舰版），完成权限、工作流字段、附件行为可能不同，以实测为准并更新本文件。
