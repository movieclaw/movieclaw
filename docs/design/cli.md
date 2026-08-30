# movieclaw CLI 化架构设计（Agent-first · OpenAPI 驱动）

> 目标与三条硬要求（来自产品）：
>
> 1. **CLI 的目标用户是 Agent**——给模型提供模型友好的工具，人类可用是副产品。
> 2. **每个新 API 自动获得 CLI 能力**——不允许「加一个接口就要手写一个命令」，
>    可用工具必须能动态发现。
> 3. **每个命令自带 help（描述 + 示例）供探索发现**——尽可能复用 OpenAPI 文档，
>    不另外维护第二份说明。
>
> 结论先行：**以 OpenAPI spec 为唯一事实源——构建期导出、随包内置基线，
> 运行时按 hash 偏斜刷新——CLI 启动时由 spec 动态生成命令树与 help；
> 再叠一层很薄的「精选命令」处理跨接口工作流。**
> 后端已具备关键条件：129 个端点全部带中文 `summary`、参数带 `description`、
> 请求/响应全部是 Pydantic schema——原材料已经在代码里，缺的只是暴露与约定。

---

## 0. 前提假设

1. **CLI 是远程薄客户端**：只调 `/api/v1`，业务逻辑全在服务端。
2. **首要运行形态**：movieclaw 自带 AI 助手（movieclaw_agent）在隔离工作区执行
   bash——CLI 会被放进这个工作区，成为 Agent 操作产品本身的工具集；其次才是
   用户在自己终端远程使用。因此**非交互（non-TTY）是主形态，不是降级形态**。
3. 命令名唯一且只有一个：**`mclaw`**，不注册别名。否决 `mc`——与
   Midnight Commander、MinIO Client 撞名，目标用户（NAS 自部署人群）恰是
   这两个工具的高密度人群，PATH 撞名是事故；`mclaw` 无任何已知撞名，对模型
   是独特 token，不会与已有工具的用法产生世界知识混淆。help/示例/
   x-cli-examples 统一用 `mclaw`；包名、环境变量前缀（`MOVIECLAW_*`）、
   配置目录（`~/.config/movieclaw/`）保留产品全名（命令短、产品标识全，
   同 gcloud 之于 Google Cloud）。Python 实现（同栈同仓，见 §9）。
4. 现状硬约束（盘点结论）：认证只有 Cookie 会话（无通用 API Token）；
   2 个 SSE 端点（搜索流、AI 会话流）；长任务全部「POST 启动 + 轮询」；
   响应统一 `ApiResponse{success,code,message,data}` 信封；敏感字段保存后不回读；
   生产环境 openapi.json 当前关闭（`app.py` 中 `docs_enabled = app_env == "local"`）。

---

## 1. 总体架构：两层命令面（less is more）

```
精选命令（overlay，手写，个位数）  mclaw download / mclaw search torrents / mclaw session start ...
   跨接口工作流、长任务 --wait、SSE 聚合 —— 覆盖「多步才能完成一件事」的场景
─────────────────────────────────────────────────────────────
生成命令（gen，自动，=接口数）     mclaw subscriptions list / mclaw library scan start / mclaw site add ...
   运行时由 OpenAPI spec 生成：命令名、参数、校验、help、示例全部来自 spec
   —— 新 API 合入后端即自动出现，CLI 零改动
```

刻意**不设** `mclaw api call` 式的裸调逃生舱，也不设结构化目录命令——模型面对的
每一条命令都是有正规名字、正规 help 的命令，工具面越小越干净，模型的选择
就越稳。「新 API 自动支持」不靠留后门保证，靠 §2.3 的 CI 守护独力保证：
生成器不认识的端点形态直接 CI 红，逼着当场扩映射规则或收进精选层，
**失败在合入时显式暴露，而不是留一条谁都可能误用的旁路**。

```
┌ 后端（唯一事实源）────────────────────────────────┐
│ FastAPI 路由: summary/description/operation_id     │
│ Pydantic schemas · x-cli-* 扩展元数据              │
└──────┬────────────────────────────┬───────────────┘
       │ 构建期：CI 从代码导出 spec │ 运行时：GET /api/v1/spec
       │ 随 CLI 包分发（内置基线）  │ 按 hash 协商增量刷新
       ↓                            ↓
┌ CLI ──────────────────────────────────────────────┐
│ core/   config·auth·http·sse·task·output·errors   │
│ gen/    spec 装载（内置基线 ∨ 刷新缓存）           │
│         → 命令树构建 → 参数映射 → 调用             │
│ overlay/ 精选命令注册（同名覆盖生成命令）          │
└───────────────────────────────────────────────────┘
```

---

## 2. OpenAPI 作为唯一事实源：后端要补的四件事

help、参数、校验、示例全部从 spec 来，等价于「API 文档写好，CLI 文档就写好了」。
后端需要一次性补齐并用 CI 守护（模式对标现有的 `test_auth.py` 全路由匿名扫描守护测试）：

### 2.1 spec 的产出与分发：构建期导出为主，运行时刷新为辅

**主通道（构建期导出，不需要起服务器）**：FastAPI 的 spec 可以离线导出——
`create_app().openapi()` 就是全量 openapi.json。新增一个导出脚本
（`python -m movieclaw_api.export_openapi`），CI 里执行并把产物作为**包数据
随 CLI 分发**（对标 AWS CLI/botocore 分发服务模型 JSON 的做法）。由于 CLI
与服务端同仓同镜像构建，**镜像内的 CLI 与服务器永远严格同版**——「新 API
自动支持」由同仓构建直接保证，不依赖任何网络拉取。

**辅通道（运行时刷新，服务偏斜时才用）**：远程安装的 CLI（pipx）可能与
服务器版本不一致。为此保留一个**受 `require_login` 保护**的刷新端点：

```
GET  /api/v1/spec       # 返回 app.openapi()，响应头带 ETag（内容 hash）
GET  /health            # 响应中附 spec_hash，探活时顺带完成偏斜检测（零额外请求）
```

CLI 每次调用时比对内置基线 hash 与 `/health` 返回的 `spec_hash`：一致（绝大
多数情况）→ 直接用内置基线，零拉取；不一致 → 拉取 `/spec` 缓存到
`~/.cache/movieclaw/spec-<server>.json` 并优先使用，同时提示版本偏斜。
刷新失败或新 spec 含生成器不认识的形态 → **回退内置基线 + 明确警告
「服务器较新，建议升级 CLI」**，绝不半瘫。

现有「生产关闭 openapi.json 防匿名暴露」的安全立场不变：`/spec` 在鉴权后，
匿名可见面零增加。

### 2.2 operation_id 命名约定 → 命令名

FastAPI 默认 operation_id 冗长（函数名+路径+方法）。统一改为
`<域>.<动作>` 两段式，直接决定命令树：

```
operation_id = "subscriptions.list"                         →  mclaw subscriptions list
operation_id = "subscriptions.set-tracking-state"           →  mclaw subscriptions set-tracking-state <id>
operation_id = "library.scan.start"                         →  mclaw library scan start <library_id>
operation_id = "library.identification.assign-file-to-title" →  mclaw library identification assign-file-to-title <file_id>
```

实现上用一个自定义 `generate_unique_id_function` 兜底 + 路由处显式声明，
CI 测试强制：全部路由 operation_id 匹配 `^[a-z][a-z0-9]*(\.[a-z0-9-]+)+$` 且唯一。

### 2.3 x-cli-* 扩展词表（写在路由装饰器 `openapi_extra` 里）

spec 标准字段表达不了的 CLI 语义，用少量扩展字段声明（声明式，不写代码）：

| 扩展字段 | 含义 | 示例 |
|---|---|---|
| `x-cli-examples` | help 里的示范用法（命令行形态，≥1 条） | `mclaw subscriptions create --title-ref tmdb:movie:438631` |
| `x-cli-dangerous` | 破坏性等级：`confirm`（需 --yes）/ `destructive`（删磁盘，需 --yes 且回显影响面） | 删条目、删库 |
| `x-cli-long-task` | 声明这是长任务启动端点 + 进度从哪读（端点或字段路径），驱动统一 `--wait` | `{"progress_op": "library.get", "progress_field": "scan_progress"}` |
| `x-cli-stream` | SSE 端点标记 + 终态事件名 | 搜索流 / Agent 流 |
| `x-cli-hidden` | 不生成命令（纯 Web 基础设施，如图片代理），CI 快照中显式记为豁免 | `/images/proxy` |
| `x-cli-paged` | 分页参数名，驱动统一 `--limit/--all` | `/sessions` |

**CI 守护测试**（新 API 自动支持 CLI 的强制机制）：遍历 OpenAPI 全部路由，
校验 ① summary 非空（已满足）② operation_id 合规 ③ 写操作有 description
④ `x-cli-dangerous`/`x-cli-long-task` 该标的都标了（DELETE 方法默认要求
dangerous 声明，除非显式豁免）。**漏标即 CI 红**——这一条把「自动支持」从
善意约定变成机械保证。

### 2.4 描述与示例的质量基线

- summary（已有，中文）→ 命令一行简介；docstring/description → `--help` 长说明；
  Pydantic `Field(description=...)` / `Query(description=...)` → 每个标志的说明。
- `x-cli-examples` 是新增工作量的大头，但它同时会出现在 Swagger 文档里，
  等于一份钱买两样：API 文档示例 + CLI help 示例。

---

## 3. 动态生成机制：spec → 命令树

### 3.1 三种方案的权衡：选「内置基线 + 运行时刷新」混合

| | 纯运行时拉取 | 纯构建期代码生成 | **混合：内置基线 spec + 按 hash 刷新（选定）** |
|---|---|---|---|
| 新 API 生效 | 服务器升级即生效 | 必须重发 CLI | 同仓同镜像构建 → 镜像内天然同步；远程 CLI 靠刷新跟上 |
| 冷启动 `--help` | ✗ 没连过服务器就没有任何命令，且 spec 在登录后面，探索顺序被倒置 | ✓ | ✓ 内置基线离线可用 |
| 服务器不可达 | ✗ 无缓存时全瘫 | ✓ help 可用 | ✓ help 可用，调用报退出码 4 |
| 新老搭配 | ✗ 老 CLI 解析新 spec，运行时才炸 | ✗ 静默不匹配 | ✓ hash 比对显式发现偏斜，解析失败回退基线 + 提示升级 |
| 每次调用开销 | 首次拉取 + 每次解析 | 零 | 常态零网络（hash 随 /health 捎带），仅偏斜时拉取 |
| 生成器正确性验证时机 | 用户运行时 | CI | CI（对着导出 spec 做命令树快照测试） |
| 业界先例 | kubectl（为 CRD 动态资源所迫，付出 ±1 版本偏斜策略的代价） | gcloud / Azure CLI | **AWS CLI（botocore 随包分发服务模型 JSON）** |

选**混合**的决定性理由是本产品的部署形态：CLI 与服务端同仓、打进同一个
Docker 镜像，第一消费者（产品内 Agent 工作区）面对的永远是同版本服务器——
「新 API 自动支持」由同仓构建直接保证，运行时拉取只需要解决「远程 pipx
安装的 CLI 落后于服务器」这一个次要场景。注意仍然**没有构建期代码生成**：
构建期产出的只是 spec 数据文件，命令树永远在启动时由装载的 spec 动态构建
——「数据随包走，逻辑不分叉」，两条通道共用同一个生成器。

**接口稳定性契约**（生成命令是脚本与 Agent 提示词的依赖，不能漂移）：
operation_id 视为公开 API——改名即破坏性变更，CI 用命令树快照 diff 强制
显式确认；确定重命名时必须同步更新后端、前端、CLI、文档与测试，不保留旧命令分支。

### 3.2 参数映射规则（生成器的全部约定，刻意保持少）

| OpenAPI 元素 | CLI 形态 |
|---|---|
| path 参数 | 位置参数，按路径顺序（`/subscriptions/{id}` → `mclaw subscriptions get <id>`） |
| query 参数 | `--kebab-case` 标志，类型/枚举/默认值/必填照搬 schema |
| requestBody（对象） | 顶层字段拍平成 `--标志`；嵌套对象/数组字段收折为 `--<字段>-json '<json>'`；整体替代形态 `--input body.json`（`-` 表示 stdin）三选一 |
| multipart 上传 | `--file <path>` |
| FileResponse 下载 | `--output-file <path>`（缺省打印保存到的临时路径） |
| 枚举 | 校验 + help 里列出候选值 |
| `x-cli-paged` | `--limit N` / `--all`（自动翻页聚合） |

规则之外的形态一律不猜——CI 快照测试会把「生成器不认识的端点」直接标红，
合入前就必须二选一：扩映射规则，或收进精选层。**不追求生成器全知全能，
追求失败显式可见、且没有静默的第三条路。**

---

## 4. Help 体系：一份 spec，help 即协议

### 4.1 `--help` 逐级探索——对人和模型是同一套

```
mclaw --help                # 域列表（来自 tags + 中文描述）
mclaw subscriptions --help         # 该域全部命令 + 一行简介（来自 summary）
mclaw subscriptions create --help  # 长说明(description) + 全部标志(参数 description)
                            # + 示例(x-cli-examples) + 关联命令(同域推荐)
```

**不为「模型发现能力」单设任何机制。** Agent 在工作区跑 bash 时，逐级 help
探索对模型和对人同样自然，模型读帮助文本毫无障碍——gcloud/gh 的 agent 用法
都是这么跑的。多一套结构化目录命令只会稀释工具面、干扰模型选择，less is more。
（若未来做 §6.1 的集成形态 B，Agent 模块直接消费后端 `/spec` 端点生成工具
注册表即可，同样不需要 CLI 提供目录命令。）

### 4.2 错误即帮助

Agent 最常见的「学习方式」是试错。因此错误输出必须携带修正路径：

```json
{"success": false, "code": "VALIDATION_ERROR",
 "message": "缺少必填参数 --title-ref",
 "hint": "先用 search titles 获取 title_ref；再看 mclaw subscriptions create --help"}
```

参数校验错、404、业务错（服务端中文 message 直接透传）都附 `hint`；
未知命令时基于编辑距离给「你是不是想用 …」。

---

## 5. Agent 友好设计准则（本方案的核心约束，逐条可测试）

1. **默认零交互。** 任何路径下都不会挂起等待输入。需要确认 → 没给 `--yes`
   就以退出码 5 失败并说明；有歧义 → 把候选作为结构化数据返回（见第 4 条）。
   交互式提示只在「显式 TTY + 人类模式」下才可能出现。
2. **非 TTY 默认输出 JSON。** Agent 场景自动命中；`-o json` 输出的是服务端
   `data` 字段原样（拆信封），字段名与 API schema 一致——这是稳定契约，
   任何「表格列怎么排」的调整都不影响它。TTY 下默认 table（人类副产品）。
3. **一次调用 = 一个完整结果（阻塞语义优先）。** 与人类 CLI 相反：
   - 长任务默认 `--wait`（轮询到终态才返回，超时可控，`--no-wait` 才立即返回）；
   - `mclaw search torrents` 内部走 SSE，但默认输出**聚合完成后的稳定结果**，站点进度
     打到 stderr；`--stream-events` 才逐帧输出 NDJSON（给需要增量的调用方）。
   Agent 的心智是「调用工具 → 拿到结果」，不是「盯着进度条」。
4. **歧义是数据，不是对话。** `mclaw subscriptions create --title-ref douban:...`
   命中多个 TMDB 候选时，返回候选清单（含可直接重试的 TMDB `title_ref`）+
   明确错误码。多轮消歧靠 Agent 的多次工具调用完成，
   每次调用自身保持无状态。
   `mclaw download <row>` 同理：默认先走 `/downloaders/resolve-target`，歧义时
   stdout 返回候选并以退出码 7 停止，Agent 带 `--tmdb-id` 重试；不得静默落到
   下载器默认目录。
5. **输出有预算。** 列表默认 `--limit`（各域给合理默认，如 50），截断时在
   stderr 明示「共 312 条，已截断，--all 取全量」；长文本字段（简介、日志）
   默认截断带标记。上下文窗口是 Agent 的稀缺资源，多余输出就是伤害。
6. **破坏性操作显式化。** `x-cli-dangerous` 驱动：`confirm` 级需 `--yes`；
   `destructive` 级（删磁盘文件）需 `--yes` 且执行前回显影响面（条目名、
   文件数、路径）到 stderr。help 里标注 ⚠，让模型在选工具阶段就看见风险等级。
7. **幂等与可重试友好。** 服务端已有的幂等语义（重复订阅幂等返回）在 help 中
   写明；网络错误自动重试仅限只读请求（GET），写请求失败原样报错绝不自动重发。
8. **退出码契约**：0 成功 / 1 业务错误 / 2 用法错误 / 3 认证失败 / 4 连不上
   服务器 / 5 缺 `--yes` / 6 长任务失败或超时 / 7 歧义待消解。

---

## 6. 内置到产品 Agent：两种集成形态与自动授权

CLI 的第一消费者是产品自带的 AI 助手（movieclaw_agent，隔离工作区跑 bash）。
「CLI 进入 Agent 的 tool 列表」有两种形态，授权机制两者共用：

### 6.1 集成形态

| | 形态 A：bash + 工作区（推荐先做） | 形态 B：命令注册为一等工具 |
|---|---|---|
| 做法 | CLI 装进 Agent 工作区，模型通过已有 bash 工具调用，靠 `--help` 探索 | Agent 模块直接读后端 `/spec` 生成工具注册表，把每条命令注册成独立 tool（function calling，带 input_schema），执行时拼装 argv 调 CLI |
| 改动量 | 几乎为零（工作区镜像加一个包 + 注入两个环境变量） | Agent 模块要做目录拉取、工具注册、参数拼装到 argv 的桥接层 |
| 模型体验 | 通用 bash 心智，组合能力强（管道、jq）；需要多轮 help 探索 | 工具即目录，schema 强约束参数，选择更稳、幂次更少 |
| 风险面 | bash 是全能工具，边界靠工作区隔离 | 工具面收窄到白名单命令，可按危险等级过滤注册 |

**结论：P1 落地形态 A（成本趋近于零，立刻可用）；形态 B 作为后续演进**——
所需的 spec 与映射规则届时都已存在，注册时可以只挑非 destructive 命令，
把「Agent 能碰什么」变成注册期的白名单决策。两形态不互斥，可共存。

### 6.2 自动授权：按 run 签发的短时效内部令牌（Agent 全程零登录）

```
用户发起 Agent 任务
  → agent 模块创建 run，向认证服务申请内部令牌
      令牌 = itsdangerous 签名（复用现有会话签名密钥，新 salt
             "movieclaw.agent-token.v1"），负载 {aud:"agent", run_id, exp}
      —— 无状态、不落库、无需新增存储
  → 拉起隔离工作区时注入环境变量：
      MOVIECLAW_SERVER=http://127.0.0.1:8000   （同容器回环直连）
      MOVIECLAW_TOKEN=<内部令牌>
  → CLI 环境变量优先级最高 → 每个请求自动带 Bearer → 零配置、零交互
  → 服务端 require_login 扩展：Cookie 或 Bearer（PAT / agent 令牌同一入口验签）
```

关键性质（选无状态签名令牌而非落库 PAT 的理由）：

- **生命周期 = run 生命周期**：`exp` 取 run 最大时长（如 2 小时），run 结束
  令牌自然作废，不需要吊销存储；长会话续聊时每次新 run 重新签发。
- **全局熔断免费获得**：管理员改密会轮换签名密钥（现有机制），所有 agent
  令牌与会话一起瞬间失效——安全兜底不用另写。
- **可审计**：令牌负载带 `run_id`，服务端访问日志可把每一次 CLI 调用归因到
  具体的 Agent 运行，配合订阅/媒体库已有的活动时间线，「是谁改的」可回答。
- **与用户手动 PAT 正交**：用户在自己终端用的长期 PAT（P1 的 /auth/tokens）
  走落库 + 可命名可吊销；agent 内部令牌走无状态短时效。两者验签入口同一个，
  实现共享，语义不混。

破坏性操作的双保险：即便持有效令牌，CLI 的危险门槛（`--yes`、destructive
回显影响面）依然生效；若采用形态 B，还可在注册期直接不注册 destructive 命令。

## 7. 精选命令层（overlay）：只收「跨接口的工作流」，个位数

生成层覆盖单接口调用，以下场景一条命令背后是多个接口的编排，值得手写
（同名注册即覆盖生成命令，其余全部放行给生成层）：

| 命令 | 编排内容 |
|---|---|
| `mclaw search titles "关键词"` | 搜索 TMDB、豆瓣或全部影视来源；默认保存统一搜索历史 |
| `mclaw search torrents "关键词"` | SSE 聚合 + 客户端侧筛选排序标志（--resolution/--sort…）+ 结果快照落本地供 `mclaw download` 引用；裸 `mclaw search "关键词"` 是等价简写 |
| `mclaw search library-items "关键词"` | 搜索当前账号可见媒体库中的已入库条目 |
| `mclaw download <行号|site:url>` | 行号形态一步完成：读搜索快照 → `resolve-target` 识别/预演 → 唯一且可入库就带 `auto_route` 提交；只有歧义或不可路由时才中止并提示。`--library`/`--save-path` 显式覆盖，`--downloader-default` 明确选择下载器默认目录；显式 URL 因无媒体身份维持低级提交形态 |
| `mclaw library organize-files <library_id>` | `--dry-run` 走 preview；正式执行强制先 preview 回显影响面再执行 |
| `mclaw session start "任务"` | 不传 `--session-id` 时以首条用户消息新建会话，传入时自动继续已有会话 → SSE 渲染（工具调用逐行）→ 终态定退出码；`--detach` 后可用 `session follow`（Last-Event-ID 续传），停止用 `session stop` |
| `mclaw session retry <session_id> --message-id <id>` | 删除指定 user message 及其后的轨迹，默认按原文重试；传 `--prompt` 时用新问题替换，再接入 SSE |
| `mclaw login` | bootstrap 探测 → 密码登录 → （P1 起）自动换取长期 Token |
| `mclaw status` | health + auth/me + spec 版本，一眼看部署状态 |
| `mclaw logs -f` | 轮询模拟 follow |

Session 命令面采用两层模型：`session` 是完整对话，`message` 是一条持久化的
`system/user/assistant/tool` 协议消息。开始与继续统一映射到 `POST /sessions`：
请求不含 `session_id` 时新建，含 `session_id` 时继续已有会话；回执返回稳定的
`session_id/message_id`。完整轨迹是 `message | compaction` 判别联合；重新提问统一使用
`session retry --message-id <message_id>`，可选 `--prompt` 改写原问题。`turn` 只允许作为 Web 将“一个 user
message 到下一个 user message 之前的输出”组合展示时的派生概念，不进入 API、CLI
参数或持久化身份。

`download <row>` 与 Web 下载弹窗共用同一组 API 状态，只在交互承载上不同：

| `resolve-target` 结果 | Web | CLI / Agent |
|---|---|---|
| `ready && ok` | 默认选中“智能入库”，用户确认后提交 | 直接带确认后的 TMDB 身份提交 |
| `ambiguous` | 展示候选按钮，点击后重新预检 | stdout 输出候选、退出码 7；带 `--tmdb-id` 重试 |
| `not_found` | 让用户改选目录或下载器默认目录 | 不提交，并提示显式覆盖参数 |
| `ready && !ok` | 展示配置警示，让用户改选 | 不提交，并透传警示与修复方向 |

两端提交 `auto_route` 后，API 都会按 TMDB 锚重新建档和路由；预检路径只用于
展示与提前拦错，不作为真实提交的可信路由结果，避免配置变化产生时序偏差。

预计 7 条左右。`subscriptions create` 的来源解析、建档和路由预检已收进后端，
因此由 OpenAPI 生成层直接提供。**准入标准：需要客户端编排或本地状态才收进精选层；单接口的便利包装
一律不收**（那是生成层 + x-cli 元数据该解决的事）。

---

## 8. 基础能力层（core/，与生成无关的地基）

### 8.1 认证（唯一需要后端新增的功能点）

> **本节已被 `docs/design/device-auth.md` 取代**：登录改走设备授权流程
> （客户端出示配对码、人在网页批准、令牌只回到发起进程），令牌带 scope，
> `mclaw login --password` 废弃。以下内容保留作为演进记录。


- **P0**：`mclaw login` 走 Cookie 会话（后端零改动），凭证落
  `~/.config/movieclaw/credentials`（0600）。
- **P1**：后端新增 PAT——`POST/GET/DELETE /auth/tokens`，`require_login` 扩展为
  「Cookie 或 `Authorization: Bearer <token>`」，实现直接复用插件同步令牌的
  加密落库（SecretBox）与 `hmac.compare_digest` 校验模式。
- **产品内 Agent 的自动授权**：按 run 签发的无状态短时效令牌 + 工作区环境
  变量注入，详见 §6.2；与用户手动 PAT 共用同一个 Bearer 验签入口。
- `--debug` 输出对 Authorization/Cookie/密码打码；密钥输入优先环境变量与
  `--input` 文件，标志形态在 help 里注明会留 shell 历史。

### 8.2 配置与多上下文

`~/.config/movieclaw/config.toml`，`[contexts.*]` 多服务器；优先级
**标志 > 环境变量（MOVIECLAW_SERVER/TOKEN/CONTEXT）> 配置文件 > 默认**。
环境变量形态是 Agent/CI 的主通道，可完全不落盘。

### 8.3 http / sse / task / output / errors

- **http**：httpx 封装——认证注入、超时、GET 自动重试、信封拆解、
  `ErrorResponse → 中文错误 + hint + 退出码` 映射。
- **sse**：手写分帧（`\n\n`），搜索流事件序列聚合；Agent 流 `Last-Event-ID`
  续传 + 指数退避（500ms→5s）。与前端刻意不用 EventSource 的理由相同。
- **task**：统一 `--wait`——进度来源由 `x-cli-long-task` 声明（内嵌字段 /
  独立端点 / SSE 三形态），轮询节奏自适应（3s→30s），Ctrl-C 只停等待不取消
  （明确告知，取消用对应 stop 命令）。
- **output**：stdout 只放数据、stderr 放进度与提示；`-o table|json|yaml`；
  `--quiet` 只输出关键标识（如新建资源 id）；NO_COLOR 与非 TTY 自动无色。

---

## 9. 技术选型与仓库落位

**Go + cobra（动态构建命令树）。**

CLI 最初用 Python + click 写成（与后端同栈，起步最快），2026-08 迁到 Go——
迁移的动机、实测数据与完整过程见 `docs/design/cli-go-migration.md`。一句话：
CLI 要装在 NAS、软路由、同事的机器和 CI 里，「先装个 Python」是装不上的
第一名原因；Go 交叉编译出来的单个静态二进制冷启动 6ms、体积 7MB，Python
方案（uv 拉独立运行时）是 172ms / 108MB。

- **运行时动态生成命令树**要求框架支持程序化注册——cobra 的 `Command` 对象
  模型与 pflag 的 `Changed()`（区分「没传」与「传了零值」）正好够用。
- 分发：① Docker 镜像内置（产品内 Agent 与 `docker exec` 用户零安装）；
  ② GitHub Release 的六平台二进制，`scripts/install-cli.sh` / `install-cli.ps1`
  一行装好，不需要任何预装运行时。
- 不做的事（防过度工程）：不做裸调逃生舱与结构化目录命令（less is more，
  见 §1/§4）、不做插件系统、不做本地数据库（仅「上次搜索快照」一个 JSON
  文件）、不做自动更新、不内嵌业务逻辑、不做 MCP 壳。

```
cli/
├── cmd/mclaw/         # 唯一入口 mclaw（不注册别名）：装配、退出码结算、偏斜刷新
├── internal/
│   ├── config/        # 全局凭证与上下文（config.toml + credentials）
│   ├── discover/      # 局域网找服务器（复用服务端已有的 UDP 7359 应答）
│   ├── api/           # HTTP 客户端：信封拆解、错误→退出码、SSE 连接
│   ├── output/        # 三种输出格式，保持服务端字段顺序
│   ├── jsonval/       # 保序 JSON 对象与取值助手
│   ├── clierr/        # 错误模型与退出码契约
│   ├── sse/           # SSE 分帧器
│   ├── wait/          # 两种「等任务跑完」的循环，生成层与精选层共用
│   ├── spec/          # 内嵌基线 spec + 指纹偏斜检测/刷新
│   ├── tree/          # 生成层：spec → 命令树（映射规则、参数、帮助）
│   └── overlay/       # 精选命令，每域一个文件
├── testdata/          # 命令树快照（生成命令清单 + 装配后的整棵树）
└── e2e/               # 对真服务器与协议桩的端到端验收脚本
```

后端改动集中且小：spec 导出脚本、operation_id 约定、x-cli-* 标注、
CI 守护测试、`/health` 附带 spec_hash、`GET /api/v1/spec` 刷新端点、
（P1）PAT 端点。全部是元数据与鉴权层面，不动业务逻辑。

---

## 10. 实施路线

| 阶段 | 内容 | 验证标准 |
|---|---|---|
| **P0 地基 + 生成层雏形** | 后端：spec 导出脚本 + operation_id 约定 + CI 守护测试。CLI：core 全套、内置基线 spec 装载、`mclaw login`(Cookie)、`mclaw status`、生成器先覆盖「纯 GET + path/query 参数」类端点 | 断网状态 `mclaw --help` 全树可浏览；命令树快照测试跑通；`mclaw login && mclaw subscriptions list -o json` 远程全通；退出码契约测试通过 |
| **P1 生成层全量 + Token** | gen/ 映射规则全量落地（requestBody/上传/下载/分页）；x-cli-* 标注铺完 129 端点；`/health` spec_hash + `/spec` 刷新通道；后端 PAT + Agent 工作区令牌注入；长任务 `--wait`、危险确认 | 命令树快照 = 全部非 hidden 端点；产品内 Agent 工作区里 `mclaw subscriptions list` 零配置跑通；偏斜场景（老 CLI × 新服务器）刷新与回退路径有测试覆盖；漏标元数据 CI 红 |
| **P2 精选层 + 流式** | 精选命令（search+download / organize / session start…）；SSE 两处；订阅创建编排下沉后端 | 「搜索→下载→订阅→扫描入库」全流程由 Agent 通过 mclaw 完成，全程零交互 |
| **P3 打磨** | 错误 hint 全覆盖、编辑距离建议、shell 补全、`logs -f`、README/示例扩充 | 抽样端点的 --help 含示例率 100%；退出码契约回归测试全绿 |

## 11. 需要产品拍板的开放问题

1. **Agent 令牌是否需要权限降级**：§6.2 的方案默认 agent 令牌与管理员同权
   （危险门槛由 CLI 的 `--yes`/影响面回显兜底）。是否要在令牌负载加 scope、
   服务端直接拒绝 agent 令牌执行 destructive 端点？更安全但多一层实现，
   建议先不做、观察形态 A 的实际使用后再定。
2. **集成形态 B（命令注册为一等工具）的启动时机**：形态 A 成本趋近于零先上；
   形态 B 需要 Agent 模块做注册桥接层，建议等形态 A 用出真实痛点
   （help 探索轮次过多、参数出错率高）再投入。
3. **x-cli-examples 的铺设节奏**：129 端点全铺工作量可观，是否接受 P1 只给
   写操作与危险操作铺示例、读操作靠 summary + 参数说明兜底？
