# MCP Server 端点：把 mclaw 的服务目录开放给外部 AI 客户端

> 状态：**方案待评审**（本文只出设计与取舍，尚未实现）。
> 配套样稿：`docs/design/mockups/mcp-server-demo.html`（管理页每个功能一屏）。
> 相关设计：`docs/design/agent-cli-integration.md`（产品内 Agent 的 mclaw 工具）、
> `docs/design/device-auth.md`（令牌签发与吊销的既有立场）、`docs/design/cli.md`。

---

## 0. 一句话

把已经在产品内 Agent 上跑通的那套「一级服务目录 + mclaw 执行」原样搬到 MCP 协议上，
让管理员能**自己组合出多个 MCP 端点**：选哪些服务，这个端点就只有那些工具；
端点各自持有独立令牌、可单独启停、可随时吊销。

---

## 1. 目标与非目标

**目标**

1. 设置页新增「MCP 服务」分区（仅管理员可见），可新建 / 编辑 / 启停 / 删除端点。
2. 新建时**多选服务**（就是今天注入 Agent 的那份一级服务目录），选中即工具面。
3. 端点走**当前稳定版 MCP 的 HTTP 传输**（Streamable HTTP），外部客户端
   （Claude Code、Cursor、Cline、自建 Agent）填一个 URL + 一个令牌即可接入。
4. 端点默认按管理员权限执行——因为它能做的事等价于管理员在命令行上做的事，
   所以这个能力本身只对管理员开放、也只有管理员看得见。

**非目标（v1 明确不做）**

- 不做 OAuth 授权服务器（见 §4.4，代价与后路都写清楚了）。
- 不做 MCP 的 resources / prompts / sampling / elicitation——本产品的价值面全在工具上。
- 不做「给成员发 MCP 端点」。成员体系有能力开关与可见范围，MCP 端点是管理员权限的
  凭证面，两者混在一起会把授权模型搞成两套（可作 v3 议题）。

---

## 2. MCP 现状调研（截至 2026-09）

### 2.1 协议版本：`2026-07-28` 是当前稳定版，且它把协议**改成无状态**

| 变化 | 旧（`2025-03-26` … `2025-11-25`） | 现行 `2026-07-28` |
| --- | --- | --- |
| 握手 | `initialize` + `notifications/initialized` | **取消**，每个请求自带版本与能力（`_meta`） |
| 会话 | `Mcp-Session-Id` 头（可选）+ DELETE 终止 | **取消**，服务端不得下发会话 id |
| 能力发现 | 从 `initialize` 结果里读 | 新增 `server/discover`（服务端**必须**实现） |
| 服务端→客户端 | SSE 上直接发 JSON-RPC 请求 | **MRTR**：返回 `input_required` 结果，客户端带答案重试 |
| GET /mcp 长连接 | 有（服务端主动推送通道） | **取消**，改为 `subscriptions/listen` 的响应流 |
| 断流重连 | `Last-Event-ID` 续传 | **取消**，断了就重发新请求 |
| 结果字段 | — | 所有结果必带 `resultType`；list 类结果必带 `ttlMs` / `cacheScope` |
| 请求头 | 仅 `MCP-Protocol-Version` | 追加必填 `Mcp-Method`、`Mcp-Name`（网关可不解包体就路由/限流） |

对我们是**利好**：无状态意味着 MCP 端点就是一个普通的 `POST` 接口，
不需要会话表、不需要粘性路由、不需要在多 worker 部署下共享状态——
这和 movieclaw「一个容器、uvicorn 多 worker」的部署形态天然契合。

### 2.2 传输：单端点 POST，没有别的形态

- 服务端**必须**提供一个支持 POST 的 MCP 端点（如 `https://host/mcp/media`）。
- 客户端每条 JSON-RPC 消息一个 POST；服务端按需返回 `application/json`
  或 `text/event-stream`（长任务边跑边推 `notifications/progress`）。
- 通知类消息 → `202 Accepted` 空响应体。
- **必须校验 `Origin` 头**防 DNS 重绑定；不合法 → `403`。
- 收到旧客户端的 GET / DELETE → `405`；收到 `Mcp-Session-Id` → 忽略。
- 头与包体不一致（如 `Mcp-Name` ≠ `params.name`）→ `400` + JSON-RPC `-32020`。
- 不认识的方法 → `404` + `-32601`；不支持的协议版本 → `400` + `-32022`（带 `supported` 列表）。
- 开 SSE 时建议带 `X-Accel-Buffering: no`（防反代缓冲——**这条对我们尤其重要**，
  movieclaw 的 Web 层就是 Next 反代到后端）。

### 2.3 授权：规范里是**可选**的，但一旦做就得做全套 OAuth

MCP 授权规范对 HTTP 传输是 `OPTIONAL`；一旦声明支持，服务端就**必须**实现
RFC 9728 受保护资源元数据、客户端必须走 OAuth 2.1 + PKCE + RFC 8707 资源指示。
对一个自部署的家庭媒体应用，这是明显过重的一套东西。

现实的客户端支持面（2026-09）：

| 客户端 | 静态 `Authorization: Bearer` | OAuth |
| --- | --- | --- |
| Claude Code (`claude mcp add --transport http … --header`) | ✅ | ✅ |
| Cursor / Cline / 自建 Agent（配置文件里写 headers） | ✅ | 视实现 |
| claude.ai 网页版「自定义连接器」 | ❌（只给 OAuth 字段） | ✅ |

结论：**v1 用静态 Bearer**，代价是网页版自定义连接器接不进来（本地/桌面客户端全都能用），
这个代价明确写进产品文案，不藏着。OAuth 留作 P3（见 §10）。

### 2.4 工具设计：数量是第一约束

业界这一年反复被验证的经验：**模型在 30–40 个工具以上开始明显退化**
（选错工具、编造不存在的工具），因此「精选工具面」比「暴露全部能力」重要得多。
这条直接决定了我们的工具粒度选择（§4.1），也正好是用户提出的
「自己组合端点」这个需求的价值所在——它本质上是**让人来做工具面精选**。

其余可落地的实践：工具名要稳定且带命名空间前缀、`tools/list` 顺序要确定
（利于客户端缓存与提示词缓存命中）、危险操作打 `destructiveHint` 注解、
结果尽量结构化、列表结果给 `ttlMs` 让客户端别反复拉。

### 2.5 官方 Python SDK 的现状

`mcp` 包已发到 `2.1.1`（v2 线支持 `2026-07-28`，`FastMCP` 更名 `MCPServer`，
同一个 HTTP 应用同时应答新旧两代）。但它拖进来的运行时依赖不轻：
`httpx2`、`mcp-types`、`opentelemetry-api`、`pyjwt[crypto]`、`sse-starlette`、
`jsonschema`、`python-multipart`、`starlette`、`uvicorn`。见 §4.3 的取舍。

---

## 3. 方案总览

```
外部 AI 客户端 (Claude Code / Cursor / …)
   │  POST https://<external_url>/mcp/<slug>
   │  Authorization: Bearer mcp_xxxxxxxx
   ▼
┌──────────────────────────────────────────────────────────┐
│ MCP 协议层  src/movieclaw_mcp/            （不进业务 OpenAPI）│
│  ├ 端点解析 slug → 端点配置（禁用/不存在 → 404）              │
│  ├ 鉴权：令牌哈希常量时间比对 → 401                          │
│  ├ Origin 校验 → 403                                       │
│  ├ JSON-RPC 分发：server/discover · tools/list · tools/call │
│  │   （兼容旧代：initialize · notifications/initialized · ping）│
│  └ 工具面 = 端点选中的服务 → 每服务一个工具                    │
└───────────────┬──────────────────────────────────────────┘
                │ tools/call
                ▼
        mclaw 子进程（与产品内 Agent 完全同一条执行路径）
        env: MOVIECLAW_SERVER=127.0.0.1  MOVIECLAW_TOKEN=<短时效签名令牌 aud=mcp>
                │
                ▼
        本机 API（既有鉴权 / 校验 / 错误形态一字不改）
```

管理面则是普通的业务接口（进 OpenAPI，因而自动长出 `mclaw mcp …` 命令）：

```
Web 设置页「MCP 服务」 ──REST──> /api/v1/mcp/endpoints…（管理区，require_admin）
                                令牌签发/轮换额外挂 require_admin_session（人+浏览器）
```

---

## 4. 关键设计决策

每条都给了**选项 / 权衡 / 结论**，需要你拍板的三条已在 §11 单列。

### 4.1 工具粒度：一个服务 = 一个工具（推荐 A）

| | A. 域网关工具（推荐） | B. 逐命令类型化工具 |
| --- | --- | --- |
| 形态 | `mclaw_subscriptions(args: string)` | `subscriptions_create(title_ref, seasons, …)` |
| 工具数 | = 所选服务数，典型 3–8，最多 25 | 单选「媒体库」就 **57** 个，两三个服务即破 100 |
| 参数 | 字符串（就是 CLI 参数串） | 从 spec 生成 JSON Schema |
| 与现状 | 与产品内 Agent **完全同构**，行为一致 | 另起一套语义，两条路会漂移 |
| 风险 | 模型偶尔要 `--help` 探参数（一次往返） | 超过模型可靠选择的工具数上限（§2.4） |

实测数据（本仓当前 spec）：CLI 可见操作 329 个，按域分布
`library 57 / playback 35 / auth 20 / subscriptions 16 / app 16 / dl 15 / site 13 …`。
方案 B 在「媒体库」这一个服务上就直接越过了 30–40 的可靠区。

**结论：v1 选 A。** 每个工具的 description 由 spec 现渲染：
「域说明（复用 `_DOMAIN_LINES` 那份润色文案）+ 该域命令清单（命令 — 一行摘要）」。
实测描述体积：`library ≈ 3.3 KB`、`subscriptions ≈ 0.9 KB`、全量 25 个服务 ≈ 12 KB。
管理页在选服务时**实时显示预计工具数与描述体积**，把「上下文成本」变成用户看得见的东西。

> 方案 B 不是不能做，而是应该在 A 跑通、且确实有人抱怨「模型猜参数」之后，
> 作为端点级选项（工具粒度：服务级 / 命令级）增量加上，而不是一上来就双轨。

### 4.2 执行路径：复用 mclaw 子进程（推荐），不另起进程内调用

- **子进程 mclaw（推荐）**：与 `movieclaw_agent/tools/mclaw.py` 同一条路。
  白拿：参数体系、`--help`、退出码契约、`⚠ --yes` 确认闸、长任务 `--no-wait`、
  输出截断、中文错误提示。新代码只有「JSON-RPC ↔ argv」这一层薄壳。
  成本：每次调用一个进程（本机 ~30–80 ms），可接受。
- **进程内 ASGI 直调**：省掉进程开销，但要重写参数映射、确认闸、任务等待、
  结果整形——等于把 CLI 那套契约再实现一遍，且两边会漂移。

**结论：子进程。** 把 `movieclaw_agent/tools/mclaw.py` 里的执行内核
（定位二进制、shlex、硬闸、进程组击杀、退出码标注）抽成一个共享函数，
Agent 工具与 MCP 层各自包一层薄壳；**不改 Agent 侧任何对外行为**。

令牌：每次调用现签一枚短时效令牌（`aud="mcp"`，载荷带端点 id），
与 `issue_agent_token` 同一套签名机制。**不复用端点的外部令牌**——
外部令牌只用于「认这个客户端」，进不了业务接口；
业务侧看到的是一个可审计、几分钟即失效的 `Principal(kind="mcp", ...)`。

### 4.3 协议实现：手写（推荐）还是官方 SDK

| | 手写（推荐） | 官方 `mcp` SDK v2 |
| --- | --- | --- |
| 代码量 | ~350 行（JSON-RPC 分发 + 两代兼容 + SSE） | ~120 行胶水 |
| 新增运行时依赖 | **0** | httpx2、mcp-types、opentelemetry-api、pyjwt[crypto]、sse-starlette… |
| 发版影响 | 无 | **必须 bump `docker/runtime-version`** 并重新发镜像（CLAUDE.md 硬约束 2） |
| 多端点动态工具面 | 天然（按 slug 路由，无状态） | 要么每端点一个 Server 实例，要么按请求上下文过滤 |
| 鉴权 / 错误形态 | 完全自控，与 Jellyfin 兼容层同一立场 | SDK 想接管 ASGI 应用与授权模型 |
| 规范跟进 | 我们自己盯 | SDK 跟 |

仓内先例很强：Jellyfin 兼容层就是手写协议模仿层（根命名空间、自带 token 体系与
错误形态、不进业务 OpenAPI）。MCP 这次「无状态化」之后需要实现的面**比一年前小得多**：
`server/discover` + `tools/list` + `tools/call`，加上旧代的 `initialize` /
`notifications/initialized` / `ping` 三个方法。

**结论：手写。** 但必须配一套**协议契约测试**（黄金 JSON-RPC 往返，新旧两代各一组）
把规范细节钉死，见 §9。

### 4.4 端点授权：每端点独立 Bearer 令牌

- 令牌形态 `mcp_<43 字符随机>`；**只存 sha256 哈希**，明文只在创建/轮换时回显一次
  （与 `ApiTokenRecord` 同款立场）。
- 签发与轮换挂 `require_admin_session`——**人在浏览器里**才能签发凭证，
  这条是 `docs/design/device-auth.md §8` 已确立的红线，MCP 不破例：
  Agent 和 PAT 都不能给自己造一个 MCP 端点。
- 不复用现有 PAT：PAT 是「一台设备的完全权限」，MCP 端点是「一个工具面的受限权限」，
  两者的吊销粒度和展示位置都不同，混用会让「设备」页语义崩掉。
- 401 响应带 `WWW-Authenticate: Bearer`（不带 `resource_metadata`，因为 v1 无 OAuth）。

### 4.5 存储：设置域，不建表

端点是「一把手配的几条记录」，不是业务数据；`app_setting` 表 + `SettingSchema`
已经提供校验、默认值、加密字段与缓存。**新增 `mcp.endpoints` 配置域**，
无 alembic 迁移（也就不触碰 CLAUDE.md 硬约束 3），无新增 `data/` 目录
（不触碰硬约束 4）。

### 4.6 URL 命名空间：`/mcp/<slug>`，不进业务 OpenAPI

协议面注册在根命名空间、`include_in_schema=False`——和 Jellyfin 兼容层一致。
三个收益：不会被 CLI 生成器变成命令；不进匿名/成员守护测试的遍历面
（它自带令牌体系，属于既有的「插件区/分享区」同类例外，需在守护测试白名单登记一次）；
JSON-RPC 的错误形态不必迁就业务统一响应体。

管理面反过来**要**进 OpenAPI（`/api/v1/mcp/endpoints`），于是 CLI 自动长出
`mclaw mcp endpoints list/create/…`。同时把 `mcp` 加进
`services/mclaw_tool.py` 的 `_EXCLUDED_DOMAINS`——理由与 `members` 相同：
**凭证签发面不该出现在 Agent 的服务目录里**。

### 4.7 权限边界：三档执行策略

外部端点比产品内 Agent 更远，闸门要更硬。每个端点选一档：

| 档位 | 放行 | 用途 |
| --- | --- | --- |
| 只读（`read_only`） | 仅 spec 里 `GET` 的命令 + `--help` | 给分析型 Agent 看数据，绝不改状态 |
| 标准（`standard`，默认） | 读 + 常规写；**拒绝 `x-cli-dangerous` 的命令** | 日常自动化：订阅、搜索、投递下载 |
| 完全（`full`） | 全部，`⚠` 命令由 MCP 层自动补 `--yes` | 明确知道自己在干什么的场景 |

判定口径：argv 首段按 `operation_id`（点号即命令层级，`members.status.set`
→ `members status set`）反查 spec 得到 HTTP 方法与 `x-cli-dangerous`；
`cli/internal/overlay` 那批手写命令（`download`、`library organize-files`、
`library reconcile-paths`、`search *`、`status`、`logs tail`）另有一张写死的小表。
选「完全」档时管理页要二次确认，并在端点卡上常驻一枚红色徽标。

另外三条与档位无关的硬闸（沿用 Agent 侧的现成逻辑）：
`login`/`logout` 拒绝、`--server` 拒绝、`session start|retry|follow` 拒绝（防递归）。

并发上限：每端点一个 `asyncio.Semaphore`（默认 4），防止一个跑飞的客户端把
NAS 上的进程数打满；超限时排队，超时按 MCP 错误返回。

---

## 5. 数据模型

```python
# src/movieclaw_api/settings/mcp.py
@register_setting(namespace="mcp.endpoints", title="MCP 服务端点")
class McpEndpointsSetting(SettingSchema):
    enabled: bool = False          # 总开关：关掉后所有端点一律 404
    endpoints: list[McpEndpoint] = []

class McpEndpoint(BaseModel):
    id: str                        # 内部 id（吊销/编辑用）
    slug: str                      # URL 末段，小写字母数字与连字符，全局唯一
    name: str                      # 展示名，如「家庭影音助理」
    description: str = ""          # 备注：这个端点给谁用的
    services: list[str]            # 选中的服务域，如 ["subscriptions", "search", "library"]
    policy: Literal["read_only", "standard", "full"] = "standard"
    enabled: bool = True
    token_hash: str                # sha256(明文)，明文不落库
    token_hint: str                # 明文前 8 位，列表里显示 mcp_a1b2****
    timeout_seconds: int = 300
    created_at: str
    last_used_at: str | None = None   # 按分钟粒度节流落盘（复用 PAT 的做法）
```

`services` 的合法取值 = `services/mclaw_tool.spec_domains()`；
读取时对已消失的域静默丢弃（升级后某个域被移除也不至于让端点整个坏掉），
并在管理页标一行「有 1 个服务已不存在，已忽略」。

---

## 6. 接口清单

### 6.1 管理面（`/api/v1/mcp`，管理区）

| operation_id | 方法/路径 | 说明 |
| --- | --- | --- |
| `mcp.status` | GET `/mcp/status` | 总开关、端点数、基址、可选服务目录（含每服务工具描述体积） |
| `mcp.toggle` | PUT `/mcp/status` | 总开关启停 |
| `mcp.endpoints.list` | GET `/mcp/endpoints` | 列表（不含令牌明文） |
| `mcp.endpoints.create` | POST `/mcp/endpoints` | 建端点，**响应含令牌明文（唯一一次）**；`require_admin_session` |
| `mcp.endpoints.update` | PUT `/mcp/endpoints/{id}` | 改名/改服务/改档位/启停 |
| `mcp.endpoints.rotate-token` | POST `/mcp/endpoints/{id}/token` | 轮换令牌；`require_admin_session` |
| `mcp.endpoints.delete` | DELETE `/mcp/endpoints/{id}` | 删除（`x-cli-dangerous: confirm`） |
| `mcp.endpoints.preview` | POST `/mcp/endpoints/preview` | 试算：给定服务集合，返回将暴露的工具清单与描述体积 |

### 6.2 协议面（`/mcp/{slug}`，不进 OpenAPI）

| JSON-RPC 方法 | 现行 `2026-07-28` | 旧代 `2025-06-18` / `2025-11-25` |
| --- | --- | --- |
| `server/discover` | ✅ 必须实现 | — |
| `initialize` | — | ✅ 兼容应答（capabilities 只声明 `tools`） |
| `notifications/initialized` | — | ✅ → `202` |
| `tools/list` | ✅ 带 `ttlMs`/`cacheScope`/`resultType` | ✅（无这些字段） |
| `tools/call` | ✅ 长任务走 SSE 推 `notifications/progress` | ✅ |
| `ping` | 已从规范移除 | ✅ 兼容应答 |
| `subscriptions/listen` | ⛔ 不实现（工具面不会动态变） | — |
| 其他 | `404` + `-32601` | 同 |

HTTP 层必答项：`Origin` 非法 → `403`；GET/DELETE → `405`；
头与包体不符 → `400`/`-32020`；不支持的版本 → `400`/`-32022` 且列出 `supported`。

工具命名：`mclaw_<service>`（如 `mclaw_subscriptions`）。带前缀是为了在客户端的
扁平工具名空间里不与别家服务器撞名；`tools/list` 按服务名字典序输出（顺序确定，利于缓存）。

工具注解：只读档 → `readOnlyHint: true`；标准/完全档 → `destructiveHint: true`、
`openWorldHint: true`。

---

## 7. 前端：设置 →「MCP 服务」分区

放在「通知与集成」组，紧挨「模型接入」（同属对外集成面），仅管理员可见
（`settingsSectionGroupsFor` 的既有角色裁剪自动生效，安全边界仍在后端）。
样稿见 `docs/design/mockups/mcp-server-demo.html`，屏号与功能对应如下：

| 屏 | 功能 | 要点 |
| --- | --- | --- |
| ① | 空态 | 一句话讲清 MCP 是什么、能干什么，一个「新建端点」主按钮 |
| ② | 端点列表 | 每端点：名称、URL、档位徽标、工具数、最近调用、启停开关、更多菜单 |
| ③ | 新建 · 基本信息 | 名称 + slug（实时拼出完整 URL，重名即时报错） |
| ④ | 新建 · 选服务 | 多选卡片，按既有服务目录分组；底部实时汇总「N 个工具 · 约 X KB 描述」 |
| ⑤ | 新建 · 执行策略 | 三档单选 + 超时；选「完全」弹二次确认 |
| ⑥ | 创建完成 · 令牌 | 明文只显示这一次；三个页签给 Claude Code / JSON 配置 / cURL 自检片段 |
| ⑦ | 端点详情 | 工具目录预览（每个工具的描述可展开）、连通性自检、令牌轮换、最近调用 |
| ⑧ | 危险操作确认 | 停用 / 删除 / 轮换令牌各自的后果文案 |
| ⑨ | 总开关关闭态 | 全局关掉后列表置灰，说明「所有端点一律 404」 |

---

## 8. 安全清单

| 威胁 | 缓解 |
| --- | --- |
| 端点 URL 被扫到 | 未带合法令牌一律 401；令牌 32 字节随机；slug 猜到也没用 |
| 令牌泄漏 | 只存哈希、一次性回显、可单端点轮换/吊销；端点粒度限制了爆炸半径 |
| 浏览器里的网页偷打本机端点 | 强制 `Origin` 校验（规范硬性要求），非法 403 |
| 模型被诱导执行破坏性命令 | 默认档位就禁 `⚠` 命令；`library items delete` 只可能出现在「完全」档 |
| 客户端跑飞 | 每端点并发信号量 + 单次调用超时 + 令牌只有几分钟有效期 |
| 提权 | 端点令牌进不了业务接口；业务侧只认现签的 `aud=mcp` 短时令牌 |
| 审计缺失 | 每次 `tools/call` 记一条中文日志：端点名、工具、参数（截断）、耗时、退出码 |

---

## 9. 测试与守护

- **协议契约（新增 `tests/mcp/`）**：新旧两代各一组黄金往返——
  `server/discover` / `initialize` / `tools/list` / `tools/call`，
  外加 `Origin` 拒绝、GET→405、头体不符→-32020、未知方法→-32601、版本不支持→-32022。
- **工具面守护**：端点选中的服务集合 ⇔ `tools/list` 的工具集合严格一致；
  服务域来源必须是 `spec_domains()`（防止手抄一份域清单造成漂移）。
- **执行策略守护**：三档各跑一组命令样本，`read_only` 必须拒绝所有非 GET 命令；
  `standard` 必须拒绝全部 `x-cli-dangerous` 命令（样本从 spec 现取，新增危险命令自动纳入）。
- **鉴权守护**：无令牌/错令牌/已吊销/端点停用/总开关关 → 401/401/401/404/404。
- **既有守护的登记**：`tests/api/test_auth.py` 匿名白名单加 `/mcp/{slug}`；
  `tests/api/test_mclaw_tool_wiring.py` 因 `mcp` 进 `_EXCLUDED_DOMAINS` 自动通过。

---

## 10. 交付分期

| 期 | 内容 | 估量 |
| --- | --- | --- |
| **P1**（本次） | 设置域 + 管理面 REST + 协议层（两代兼容、非流式）+ 设置页分区 + 契约测试 | 后端 ~900 行、前端 ~500 行、测试 ~400 行 |
| **P2** | `tools/call` 的 SSE 流式（长任务推 `notifications/progress`）、调用日志页、端点级速率限制 | 中 |
| **P3** | OAuth 2.1 + RFC 9728（打通 claude.ai 网页版自定义连接器）；可选的「命令级工具粒度」 | 大 |

P1 不需要 bump `docker/runtime-version`（无新增运行时依赖、不动 Dockerfile 与 entrypoint），
也不需要数据库迁移。

---

## 11. 需要你拍板的三件事

1. **工具粒度**：接受 §4.1 的「一服务一工具（args 字符串）」吗？
   还是希望 v1 就做类型化的命令级工具（我会需要加分页与精选层，工作量约翻倍）？
2. **协议实现**：手写（0 依赖、要自己跟规范）vs 官方 SDK（省事、但要 bump runtime-version
   并新增 8 个传递依赖）——我推荐手写，理由见 §4.3。
3. **默认执行档位**：默认「标准」（禁 `⚠` 危险命令）合适吗？
   还是新建端点时默认「只读」，让用户显式放开写权限更稳妥？

---

## 12. 参考

- MCP 规范 `2026-07-28`：<https://modelcontextprotocol.io/specification/2026-07-28/>
  （[变更清单](https://modelcontextprotocol.io/specification/2026-07-28/changelog)、
  [Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)、
  [授权](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)）
- 版本策略：<https://modelcontextprotocol.io/specification/versioning>
- Python SDK v2 beta 说明：<https://blog.modelcontextprotocol.io/posts/sdk-betas-2026-07-28/>
- 工具面精选与「工具太多」的实证：
  <https://thenewstack.io/15-best-practices-for-building-mcp-servers-in-production/>、
  <https://www.speakeasy.com/docs/mcp/build/toolsets/advanced-tool-curation>
- 客户端接入形态：<https://code.claude.com/docs/en/mcp-quickstart>
