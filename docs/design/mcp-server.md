# MCP Server 端点：把 movieclaw 的服务目录开放给外部 AI 客户端

> 状态：**已实现**（P1 落地，六条拍板见 §11）。本文与代码同步：
> `src/movieclaw_mcp/`（协议与调度）、`src/movieclaw_api/{settings,services,schemas,api/routes}/mcp*`
> （管理面）、`apps/web/components/mcp-section.tsx`（设置分区）、`tests/mcp_server/`（守护）。
> 配套样稿：`docs/design/mockups/mcp-server-demo.html`（管理页每个功能一屏）。
> 相关设计：`docs/design/agent-cli-integration.md`（产品内 Agent 的 mclaw 工具）、
> `docs/design/device-auth.md`（令牌签发与吊销的既有立场）、`docs/design/cli.md`。

---

## 0. 一句话

把产品内 Agent 那份「一级服务目录」搬到 MCP 协议上，让管理员能**自己组合出多个
MCP 端点**：选哪些服务，这个端点就只有那些工具；端点各自持有独立令牌、可单独启停、
可随时吊销。协议层用官方 SDK，工具调用直连本机 API。

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
- 客户端每条 JSON-RPC 消息一个 POST；服务端**可以**返回 `application/json`
  （单个 JSON 体）**或** `text/event-stream`（边跑边推进度）——两种都合规，选哪种由服务端定。
  我们恒选前者，见 §4.8。
- 注意别把这个和 2024-11-05 那套 **HTTP+SSE 传输**混为一谈：那套是「GET 开一条常驻
  SSE 通道 + 另一个 POST 端点回消息」，早已废弃，我们不实现、也不兼容。
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

其余可落地的实践：工具名要**稳定**（改名等于换了个工具，客户端缓存与用户配置都会失效）、
`tools/list` 顺序要确定（利于客户端缓存与提示词缓存命中）、危险操作打 `destructiveHint`
注解、结果尽量结构化、列表结果给 `ttlMs` 让客户端别反复拉。

> 关于「命名空间前缀」：不少文章建议给工具名加服务器前缀防撞名。我们**不加**
> （§4.1）——主流客户端都已按服务器分组展示与调用，前缀只是每个工具白占一截 token。

### 2.5 官方 Python SDK 的现状

`mcp` 包已发到 `2.1.1`。v2 线的关键事实（都已核对文档，实现时仍需在本仓验证一遍）：

- **同一个 HTTP 应用同时应答新旧两代**（`mode="auto"`：先应 `server/discover`，
  旧客户端来 `initialize` 也照答），协议兼容这件事我们一行都不用写；
- **低阶 `Server` 正好适配「工具面是动态的」**：
  `Server(name, on_list_tools=…, on_call_tool=…)`，`Tool(name, description,
  input_schema=<裸 JSON Schema dict>)`，返回 `CallToolResult(content=[...],
  structured_content=…, is_error=…)`——工具不需要是 Python 函数，正合我们
  「按端点从 spec 现算工具面」的形态；
- `server.streamable_http_app()` 返回一个 Starlette 应用，可挂进现有 FastAPI；
  **挂载的子应用 lifespan 不会自动跑**，宿主 lifespan 要显式进入其会话管理器上下文；
- DNS 重绑定防护由 SDK 的 `transport_security=`（允许的 Host/Origin 白名单）承担。

依赖代价（见 §4.3 的结论）：新增 `mcp` + `mcp-types` + `httpx2`（httpx 2.x 的新发行名，
与我们现用的 `httpx 0.28` 并存而非冲突）+ `pyjwt[crypto]` + `opentelemetry-api` +
`sse-starlette` + `typing-inspection`；`jsonschema` / `starlette` / `uvicorn` /
`python-multipart` / `anyio` 我们本来就有。

---

## 3. 方案总览

```
外部 AI 客户端 (Claude Code / Cursor / …)
   │  POST https://<external_url>/mcp/<slug>
   │  Authorization: Bearer mcp_xxxxxxxx
   ▼
┌──────────────────────────────────────────────────────────┐
│ 我们写的调度层 src/movieclaw_mcp/         （不进业务 OpenAPI）│
│  ├ 解析 /mcp/<slug> → 端点配置（禁用/不存在 → 404）           │
│  ├ 鉴权：令牌哈希常量时间比对 → 401（在进 SDK 之前挡住）        │
│  └ 取该端点的 SDK Server 实例（按配置版本缓存），改写 path 后转交 │
├──────────────────────────────────────────────────────────┤
│ 官方 mcp SDK（低阶 Server + streamable_http_app）           │
│  ├ 协议两代兼容、JSON-RPC 编解码、SSE、Origin 白名单           │
│  └ 回调进我们的 on_list_tools / on_call_tool                 │
├──────────────────────────────────────────────────────────┤
│ 工具面 = 端点选中的服务 × 展开开关（§4.1），全部从 spec 现算     │
│  展开：subscriptions_update(subscription_id, …)（默认）      │
│  折叠：subscriptions(command: enum, params: object)         │
└───────────────┬──────────────────────────────────────────┘
                │ tools/call → operation_id → 方法 + 路径 + 参数分箱
                ▼
        进程内 ASGI 直调（httpx ASGITransport，无网络跳）
        Authorization: Bearer <短时效签名令牌 aud=mcp>
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

四条决定已拍板（记录见 §11），下面每条给出结论与理由，以及被否掉的选项为什么否。

### 4.1 工具粒度：端点级开关「展开工具」，默认展开

**已定**：不替用户在两种形态里二选一——不同 MCP 客户端对工具面的
适配逻辑差别很大（有的擅长在大工具集里检索，有的会把工具全量塞进系统提示词），
所以做成**端点级选项**，两种形态都实现，默认展开。

| | 展开（`expand_tools: true`，默认） | 折叠（`expand_tools: false`） |
| --- | --- | --- |
| 形态 | 一条命令一个工具 | 一个服务一个工具 |
| 命名 | `<模块>_<命令>`，如 `subscriptions_update` | `<模块>`，如 `subscriptions` |
| 参数 | 从 spec 生成 JSON Schema（类型化） | `command`（该域命令的枚举）+ `params` 对象 |
| 选「订阅」后的工具数 | 16 | 1 |
| 选 4 个服务（订阅/搜索/媒体库/下载器） | 91 | 4 |
| 这 4 个服务的工具定义体积（实测） | 105 KB | 16.5 KB |
| 危险操作注解 | **逐工具精确**（`readOnlyHint` 由 HTTP 方法推导，`destructiveHint` 由 `x-cli-dangerous` 推导） | 只能整域标注 |
| 模型出错的形态 | 工具太多时选错/幻觉工具名 | 命令名有枚举兜底，但 `params` 是弱类型，可能少传字段 |

命名规范：工具名 = `operation_id` 把 `.` 和 `-` 都换成 `_`
（`subscriptions.list-active-downloads` → `subscriptions_list_active_downloads`，
`members.status.set` → `members_status_set`）。**不加 `mclaw_` 前缀**——客户端侧
本来就会按服务器名分组，再加一层前缀只是白占 token。唯一性由守护测试保证。

两种形态**共用同一条执行路径**（§4.2）：都是「`operation_id` → 方法 + 路径 +
参数分箱（path / query / body）→ 进程内调本机 API」。展开模式下工具名本身就是
`operation_id`；折叠模式下 `command` 枚举值就是命令名，再拼回 `operation_id`。
于是同一条命令在两种模式下走同一段代码，用户切换开关不会得到不同结果。

折叠模式的工具形态：

```json
{
  "name": "subscriptions",
  "description": "订阅与自动追更…\n\n可用命令：\n  list    列出订阅（status, media_type, limit）\n  update  修改选季/续订/规则/目标库（subscription_id*, seasons, follow_future, …）\n  …",
  "inputSchema": {
    "type": "object",
    "properties": {
      "command": { "type": "string", "enum": ["create", "list", "get", "update", "…"] },
      "params":  { "type": "object", "description": "该命令的参数，字段见描述里的清单" }
    },
    "required": ["command"]
  }
}
```

> 参数形态从「CLI 参数串」改成「command + params」是**直连 API 的必然结果**
> （§4.2）：不走 mclaw 就没有 CLI 解析器，我们不该在服务端再实现一个。
> 好处是命令名有枚举兜底，模型编不出不存在的命令；代价是 `params` 弱类型，
> 靠描述里的字段清单引导。真需要强类型就打开展开模式——这正是这个开关的意义。

> 为什么默认展开而不是默认折叠：默认值应该服务「第一次用的人」。展开模式下模型
> 不需要理解 movieclaw 的参数体系，照 schema 填就行，首次成功率更高。老练用户
> 想要小而稳的工具面时，一个开关就切过去。

### 4.1.1 举例：同一个「订阅」服务，两种模式下模型看到什么

展开（默认）——`tools/list` 里是 16 个工具，其中一个：

```json
{
  "name": "subscriptions_update",
  "description": "修改订阅的选季、自动续订、过滤规则或目标媒体库",
  "inputSchema": {
    "type": "object",
    "properties": {
      "subscription_id": { "type": "integer", "description": "订阅 ID" },
      "seasons":       { "type": "array", "items": { "type": "integer" } },
      "follow_future": { "type": "boolean", "description": "是否自动续订新季" },
      "rule_set_id":   { "type": "integer", "description": "过滤规则组 ID" },
      "library_id":    { "type": "integer", "description": "目标媒体库 ID" }
    },
    "required": ["subscription_id"]
  },
  "annotations": { "readOnlyHint": false, "destructiveHint": false }
}
```

调用：

```json
{ "name": "subscriptions_update",
  "arguments": { "subscription_id": 42, "follow_future": false, "rule_set_id": 3 } }
```

折叠模式下的同一件事（工具定义见上一节）：

```json
{ "name": "subscriptions",
  "arguments": { "command": "update",
                 "params": { "subscription_id": 42, "follow_future": false, "rule_set_id": 3 } } }
```

两者最终都落到同一次请求：`PATCH /api/v1/subscriptions/42`，
body `{"follow_future": false, "rule_set_id": 3}`。

实测规模（实现后从代码现算，`mcp.status` 接口就是这么算给管理页的）：
进入工具面的操作 225 个、26 个服务域，按域分布
`library 54 / subscriptions 16 / app 16 / dl 12 / jobs 10 / search 9 …`。
体积（含完整 inputSchema）：

| | 展开 | 折叠 |
| --- | --- | --- |
| library（54 条命令） | 60.1 KB | 9.8 KB |
| subscriptions（16 条） | 16.5 KB | 3.0 KB |
| 四服务组合 | 91 个工具 · 105 KB | 4 个工具 · 16.5 KB |
| 全部 26 个服务 | 225 个工具 | 26 个工具 · 42 KB |

**注意展开模式的体积比方案期估的（30–40 KB）大得多**——JSON Schema 把每个参数的
类型、描述、嵌套模型都写全了，这正是它换来准确率的代价。管理页把这个数字直接摆出来，
让用户自己权衡。

**超过 30 个工具时给一条提示**：展开模式下工具数一旦超过 30，汇总行给出
「工具偏多，建议改用折叠模式」的黄色提示，并把折叠后的数字一并算出来做对照
（「同样这 4 个服务会变成 4 个工具，上下文从约 105 KB 降到约 16.5 KB」）。
这是**建议不是限制**——不拦创建、不禁用按钮。阈值取 30 而不是 40：30 是业界观察到
开始退化的下沿，提示要早于问题出现。折叠模式下不提示（它本来就只有几个工具）。

### 4.2 执行路径：直连本机 API（进程内 ASGI），不经 mclaw 子进程

**已定**：`tools/call` 直接调对应的业务接口，不再绕 CLI。

```python
# 一次工具调用 = 一次进程内 HTTP
transport = httpx.ASGITransport(app=fastapi_app)
async with httpx.AsyncClient(transport=transport, base_url="http://mcp.internal") as c:
    resp = await c.request(method, path, params=query, json=body,
                           headers={"Authorization": f"Bearer {short_lived_token}"})
```

走 ASGITransport 而不是直接调处理器函数，是为了**保住既有的鉴权、参数校验、
中间件与统一错误体**——授权判定只此一份，不给 MCP 开后门。开销是进程内函数调用级别
（~1–3 ms），比子进程（~30–80 ms）低一个量级，也不再要求镜像里有 mclaw。

令牌：每次调用现签一枚短时效令牌（`aud="mcp"`，载荷带端点 id），
与 `issue_agent_token` 同一套签名机制。**不复用端点的外部令牌**——
外部令牌只用于「认这个客户端」，进不了业务接口；
业务侧看到的是一个可审计、几分钟即失效的 `Principal(kind="mcp", ...)`。

**代价：CLI 白拿的那几件事得自己补。** 逐条交代，别到实现时才发现：

| CLI 原本提供 | 直连后的做法 |
| --- | --- |
| 参数解析与校验 | spec 生成 inputSchema（前置）+ 接口自身 422（兜底） |
| 退出码语义（0/1/2/3/5/6/7） | 映射成 MCP 的 `is_error` + 统一错误体的 `message`/`details`；业务错误照原样把中文 message 回给模型 |
| `⚠ --yes` 确认闸 | 不再有这一环。端点等价于管理员，危险操作靠**选服务时不勾它**来控制（§4.7），以及工具注解上的 `destructiveHint` 让客户端自行提示 |
| 长任务 `--wait` 轮询 | `x-cli-job` 的操作直接返回 `job_id`，模型改调 `jobs_wait`（`jobs.wait` 是真实接口，长轮询）；**工具描述里写清这条链路** |
| 输出截断与默认 `--limit` | **必须自己实现**：列表类接口的响应可能极大（媒体库动辄上万条）。做法：注入默认 `limit`、结果字节上限（超了截断并在文末标注「已截断，用 limit/offset 取更多」） |
| 搜索结果行号（`download 3`） | 直连没有客户端会话态，本来也不该有——统一用显式参数（`dl_submit` 传 `site_id` + `url`） |
| `--help` | 展开模式不需要；折叠模式靠描述里的命令与字段清单 |

**反而变好的两件事**：

1. `x-cli-stream` 的缺口没了——`search.torrents` 的 SSE 由 MCP 层自己消费并聚合，
   不再需要「手工登记 CLI 命令」那张表；
2. 上传/下载类接口（CLI 里的 `--file` / `--output-file`）**直接从工具面过滤掉**：
   MCP 客户端没有服务端文件系统的概念，这类工具给出去只会让模型反复失败。

顺带：MCP 层从此与 CLI 完全解耦（不 import `movieclaw_agent`，不依赖 mclaw 二进制），
产品内 Agent 的 mclaw 工具**一行不改**，两条路各走各的。

### 4.3 协议实现：用官方 `mcp` SDK

**已定**：协议层不自己写，引官方 SDK。MCP 还在快速演进
（一年内经历了传输换代 + 无状态化两次大改），协议编解码与版本兼容是**别人会持续维护
的部分**，我们只该维护「movieclaw 有什么工具」这件自己的事。

用法（低阶 `Server`，正是为动态工具面准备的那层）：

```python
from mcp.server import Server, ServerRequestContext
from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult, TextContent, Tool

def build_server(endpoint: McpEndpoint) -> Server:
    async def on_list_tools(ctx, params) -> ListToolsResult:
        return ListToolsResult(tools=render_tools(endpoint))      # 从 spec 现算

    async def on_call_tool(ctx, params: CallToolRequestParams) -> CallToolResult:
        return await dispatch(endpoint, params.name, params.arguments or {})   # §4.2

    return Server(f"movieclaw/{endpoint.slug}",
                  on_list_tools=on_list_tools, on_call_tool=on_call_tool)

# 挂载形态：恒 JSON 应答、无会话、Host 白名单（§4.8 / §8）
app = server.streamable_http_app(json_response=True, stateless_http=True,
                                 transport_security=TransportSecuritySettings(...))
```

SDK 负责：两代协议兼容（`server/discover` 与旧代 `initialize` 同时应答）、
JSON-RPC 编解码、SSE、必填请求头校验、`transport_security` 的 Origin/Host 白名单。
我们负责：端点路由、鉴权、工具面渲染、调度。

**多端点怎么挂**（实现结论，与方案期设想不同）：**不挂 SDK 的 Starlette 子应用**，
而是直接用它底下的 `StreamableHTTPSessionManager`——我们自己的 ASGI 调度器挂在
`/mcp`，解析 `/mcp/<slug>`、鉴权，然后：

```python
manager = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True, ...)
async with manager.run():
    await manager.handle_request(scope, receive, send)
```

一个请求现建一个 Server 与管理器。这么做是因为 SDK 明确规定
`run()` 一个实例只能进一次，且它内部是一个 anyio 任务组——**任务组必须在创建它的
那个任务里退出**。端点是运行期增删的，把管理器生命周期挂到应用 lifespan 上就要跨任务
进出，那是纯粹的隐患（方案期把这列为头号风险，实现时用这个办法直接绕开了）。
无状态模式下管理器本来就不持有跨请求状态，现建现用的开销是纯内存对象创建，
与随后那次 API 调用相比可以忽略。

副产品：不经 Starlette 子应用，也就不需要改写 `path`，路由这一段少一层可能出错的地方。

> 实现时踩到的一个坑记在这里：新版 Starlette 的 `Mount` 不再把挂载前缀从
> `scope["path"]` 里截掉，而是放进 `root_path`。只认其中一种写法，换个版本就会全线
> 404，且没有任何报错。`_slug_of()` 对两种形态都成立。

发版影响（CLAUDE.md 硬约束 2）：新增运行时依赖 → **必须 bump
`docker/runtime-version`（13 → 14）并在合并后发布新镜像**，CI 守卫会拦漏 bump 的 PR。
依赖清单见 §2.5；`mcp` 版本在 pyproject 里**按小版本锁上限**
（如 `mcp>=2.1,<3.0`），协议大改时由我们主动升级，而不是被动跟。

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

### 4.7 权限边界：不做执行档位，控制手段就是「选服务 / 启停 / 吊销」

**已定**：**不做**执行档位（原方案里的只读/标准/完全三档已删）。理由是它属于重复控制——
端点本来就是管理员权限，而这套设计已经有三个更直接、用户也更容易理解的闸：

1. **建端点时选哪些服务**——不想让它碰媒体库，就别勾媒体库；
2. **端点启停**——不用的时候关掉，地址立刻 404；
3. **吊销/轮换令牌**——出事时的止损手段。

再叠一层「档位」，用户要在两套心智模型之间来回换算（「我选了媒体库，但标准档又禁了删除，
那它到底能不能整理文件？」），而真正的边界其实还是第 1 条。

**这意味着什么，说清楚不藏着**：一个勾了「媒体库」的端点，接进去的模型**可以删除磁盘上的
媒体文件**（`library.items.delete`）——就跟管理员自己在命令行上能做的一样。产品文案要在
选服务那一屏把这句话写出来，让人在勾选时就知道自己在授予什么。

工具注解仍然照常推导（`readOnlyHint` / `destructiveHint`，§6.2）：支持注解的客户端会在
执行破坏性工具前向人确认。这是 v1 唯一的「危险操作提醒」，且落在客户端。

仍然保留的、与危险程度无关的工具面构成规则：

- 工具面**只取 `iter_command_operations` 认可的操作**（`x-cli-hidden` 的纯 Web
  基础设施接口天然不在内），再减去上传/下载类（§4.2）与 `mcp` 自身这个域（§5）；
- `session.*` 的会话创建/续跑不进工具面（防 Agent 递归拉起 Agent），
  与 mclaw 工具里的那条硬闸同义；
- 单次调用超时（端点可配，默认 300 秒）+ 每端点并发信号量（默认 4），
  防止跑飞的客户端把连接池和 CPU 吃满；超限排队，超时按 MCP 错误返回。

> 后路留着：`x-cli-dangerous` 的标注一直在 spec 里，将来真想加回档位，就是
> 工具面渲染时多一个过滤条件的事，不影响其余任何设计。

### 4.8 应答形态：恒为 `application/json`，不开 SSE

**已定**：端点只用 Streamable HTTP 这一种传输，且**每个 POST 都用单个 JSON 体应答**，
服务端永不开 `text/event-stream` 流。SDK 侧就是一个开关：

```python
server.streamable_http_app(
    json_response=True,        # 每个 POST 单个 JSON 体，不开 SSE 流
    stateless_http=True,       # 每请求一个传输，不做会话跟踪
    transport_security=...,    # Host/Origin 白名单，见 §8
)
```

为什么可以这么定：SSE 在这套协议里只服务三件事，我们一件都不用——

| SSE 才能做的事 | 我们为什么不需要 |
| --- | --- |
| 调用中推 `notifications/progress` | 长任务本来就立即返回 `job_id`，进度靠模型调 `jobs_wait`（§4.2） |
| 调用中回问客户端（elicitation / sampling） | 不实现，非目标（§1） |
| `subscriptions/listen` 的变更通知长流 | 不实现——端点的工具面只在管理员改配置时才变，不需要推给客户端 |

代价写明白：开了 `json_response=True` 之后，若将来真要推进度或回问客户端，
SDK 会在那条腿上抛 `NoBackChannelError`——那时把这个开关关掉即可，属于一行的事。

顺带的好处：JSON 应答对反代最友好。movieclaw 的 Web 层是 Next 反代到后端，
SSE 要处理缓冲（`X-Accel-Buffering: no`）、空闲超时、连接数，恒 JSON 一个都不用管。

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
    expand_tools: bool = True      # 展开：一命令一工具；关闭：一服务一工具（§4.1）
    enabled: bool = True
    token_hash: str                # sha256(明文)，明文不落库
    token_hint: str                # 明文前 8 位，列表里显示 mcp_a1b2****
    timeout_seconds: int = 300
    created_at: str
    last_used_at: str | None = None   # 按分钟粒度节流落盘（复用 PAT 的做法）
```

`services` 的合法取值 = `services/spec_catalog.command_domains()` 减去 `mcp` 自身
（端点不该能增删 MCP 端点——与 `require_admin_session` 那条红线同义）。
读取时对已消失的域静默丢弃（升级后某个域被移除也不至于让端点整个坏掉），
并在管理页标一行「有 1 个服务已不存在，已忽略」。

> 注意这里**不复用** `mclaw_tool.spec_domains()`：那个函数额外排除了 `logs` 与
> `members`，理由是 Agent 专属的（Agent 有 bash 所以不需要 logs；对话式代劳建号
> 不合适）。MCP 端点由管理员逐个勾选，勾了就是明示授权，所以两者都开放——
> 其中 `members` 能建号与重置密码，服务卡片上要写明这一点。
> 如果你不想开放 members，改成 `spec_domains()` 即可，一行的事。

---

## 6. 接口清单

### 6.1 管理面（`/api/v1/mcp`，管理区）

| operation_id | 方法/路径 | 说明 |
| --- | --- | --- |
| `mcp.status` | GET `/mcp/status` | 总开关、端点数、基址、可选服务目录（含每服务工具描述体积） |
| `mcp.toggle` | PUT `/mcp/status` | 总开关启停 |
| `mcp.endpoints.list` | GET `/mcp/endpoints` | 列表（不含令牌明文） |
| `mcp.endpoints.create` | POST `/mcp/endpoints` | 建端点，**响应含令牌明文（唯一一次）**；`require_admin_session` |
| `mcp.endpoints.update` | PUT `/mcp/endpoints/{id}` | 改名/改服务/改展开开关/启停 |
| `mcp.endpoints.rotate-token` | POST `/mcp/endpoints/{id}/token` | 轮换令牌；`require_admin_session` |
| `mcp.endpoints.delete` | DELETE `/mcp/endpoints/{id}` | 删除（`x-cli-dangerous: confirm`） |
| `mcp.endpoints.preview` | POST `/mcp/endpoints/preview` | 试算：给定服务集合，返回将暴露的工具清单与描述体积；折叠模式额外给出结构化的 `commands`（命令名/摘要/params 字段/风险），管理页据此渲染命令表 |
| `mcp.endpoints.check` | POST `/mcp/endpoints/{id}/check` | 连通性自检：用 SDK 的内存客户端跑一遍真实协议握手 + `tools/list` + 一次只读试调 |

### 6.2 协议面（`/mcp/{slug}`，不进 OpenAPI）

协议方法、两代兼容、必填头校验、Host/Origin 白名单、错误码（`-32020` / `-32022` /
`-32601`）与 `resultType`、`ttlMs`、`cacheScope` 这些字段，**全部由 SDK 负责**——
我们不复述规范，只在契约冒烟测试里确认它确实这么答（§9）。
应答形态恒为 `application/json`（§4.8），不会出现 `text/event-stream`。

我们实现的只有两个回调与一层调度：

| 我们写的 | 内容 |
| --- | --- |
| ASGI 调度器 | `/mcp/<slug>` → 端点查找（不存在/停用/总开关关 → 404）→ 令牌校验（401）→ 转交该端点的 SDK 应用 |
| `on_list_tools` | 按端点的服务集合 × 展开开关，从 spec 现算工具清单 |
| `on_call_tool` | 工具名 → `operation_id` → 进程内调本机 API（§4.2）→ 整形成 `CallToolResult` |

结果整形：业务统一响应体的 `data` 放进 `structured_content`，同时给一份紧凑 JSON 文本
放 `content`（老客户端只认 text）；业务错误 → `is_error=True` +
把 `message`（本来就是给非开发者看的中文）原样回给模型。

工具命名（不加 `mclaw_` 前缀——客户端本就按服务器名分组，再加前缀只是白占 token）：

- 展开模式：`operation_id` 的 `.` 与 `-` 换成 `_`，如 `subscriptions_update`、
  `search_history_get_results`。
- 折叠模式：就是服务名，如 `subscriptions`。

`tools/list` 按工具名字典序输出（顺序确定，利于客户端与提示词缓存命中）。

工具注解（只是给客户端的提示，不构成闸门）：展开模式逐工具推导——
`GET` → `readOnlyHint: true`，`x-cli-dangerous: destructive` → `destructiveHint: true`，
外网数据源（discover/search/site）→ `openWorldHint: true`；
折叠模式只能整域标注（含危险操作的域标 `destructiveHint: true`）。
支持这些注解的客户端会据此在执行前向人确认——这是「危险操作要不要拦」这件事
目前唯一的落点，且落在客户端而不是我们这里。

---

## 7. 前端：设置 →「MCP 服务」分区

放在「通知与集成」组，紧挨「模型接入」（同属对外集成面），仅管理员可见
（`settingsSectionGroupsFor` 的既有角色裁剪自动生效，安全边界仍在后端）。
样稿见 `docs/design/mockups/mcp-server-demo.html`，屏号与功能对应如下：

| 屏 | 功能 | 要点 |
| --- | --- | --- |
| ① | 空态 | 一句话讲清 MCP 是什么、能干什么，一个「新建端点」主按钮 |
| ② | 端点列表 | 每端点：名称、URL、工具模式徽标、工具数、最近调用、启停开关、更多菜单 |
| ③ | 新建 · 第 1 步 | 名称 + slug（实时拼出完整 URL，重名即时报错） |
| ④ | 新建 · 第 2 步 | 服务多选 + **「展开工具」开关**（默认开）+ 超时；底部实时汇总随模式切换：展开算命令数、折叠算服务数；**展开超过 30 个工具时给黄色建议**（含「折叠后是几个」的对照数字），只建议不拦截 |
| ⑤ | 创建完成 · 令牌 | 明文只显示这一次；关掉后直接落到端点概览 |
| ⑥ | 端点详情 | 三栏：概览（地址 + 认证请求头 + 连通性自检 + 令牌轮换）、工具（展开模式列工具名 + 参数，折叠模式列服务与其命令表）、设置 |
| ⑦ | 危险操作确认 | 停用 / 删除 / 轮换令牌各自的后果文案 |
| ⑧ | 总开关关闭态 | 全局关掉后列表置灰，说明「所有端点一律 404」 |

---

## 8. 安全清单

| 威胁 | 缓解 |
| --- | --- |
| 端点 URL 被扫到 | 未带合法令牌一律 401；令牌 32 字节随机；slug 猜到也没用 |
| 令牌泄漏 | 只存哈希、一次性回显、可单端点轮换/吊销；端点粒度限制了爆炸半径 |
| 浏览器里的网页偷打本机端点 | **调度层自己校验 `Origin`**：无 Origin（原生客户端）放行，有 Origin 则必须匹配「外部访问地址 / 本机 / 当前 Host」，否则 `403`。刻意不用 SDK 那道 Host 白名单——它空名单时拒绝一切，自部署用户从局域网 IP 访问会撞 `421`；而浏览器发起的跨源请求一定带 Origin，攻击面正好被这一条盖住 |
| 模型被诱导执行破坏性操作 | **v1 不做服务端拦截**（见 §4.7）：控制手段是建端点时不勾会删东西的服务、随时停用、随时吊销令牌；工具上的 `destructiveHint` 交给客户端提示 |
| 客户端跑飞 | 每端点并发信号量 + 单次调用超时 + 令牌只有几分钟有效期 |
| 提权 | 端点令牌进不了业务接口；业务侧只认现签的 `aud=mcp` 短时令牌，走的还是既有 `require_login` / `require_admin` |
| 直连绕过鉴权 | 走 ASGITransport 打完整应用栈，不直调处理器函数——授权判定只此一份 |
| 大响应打爆客户端上下文 | 默认 `limit` 注入 + 结果字节上限 + 截断提示（§4.2） |
| 审计缺失 | 每次 `tools/call` 记一条中文日志：端点名、工具、参数（截断）、耗时、HTTP 状态码 |

---

## 9. 测试与守护

- **协议冒烟（`tests/mcp_server/`）**：直接发真实 JSON-RPC 请求跑通
  `server/discover` → `tools/list` → `tools/call`，外加旧代 `initialize`。
  规范细节由 SDK 保证，我们只验证「接进来确实能用」以及升级 SDK 后没退化。

  > 目录名叫 `mcp_server` 而不是 `mcp`：`tests/` 不是包，pytest 会把它塞进 sys.path，
  > 叫 `mcp` 会把官方 SDK 那个包整个盖住，报错是让人摸不着头脑的
  > 「No module named 'mcp.server'」。
- **应答形态守护**：`tools/list` / `tools/call` 的响应 `Content-Type` 必须是
  `application/json`，不得出现 `text/event-stream`（防止哪天改配置把 SSE 打开了没人发现）。
- **工具面守护**：端点选中的服务集合 ⇔ `tools/list` 的工具集合严格一致（两种模式各一组）；
  服务域来源必须是 `spec_domains()`（防止手抄一份域清单造成漂移）。
- **工具名守护**（展开模式）：全量 spec 生成的工具名两两不重复
  （`.`/`-` 归一成 `_` 后可能撞名，撞了就必须改名而不是静默覆盖），
  且都符合 `^[a-zA-Z0-9_]{1,64}$`（兼容对函数名有限制的客户端）。
- **参数映射守护**：遍历全部生成工具，用 schema 的示例值构造请求，断言
  路径参数填满、query/body 分箱正确、且请求能被目标接口的签名接受（不产生 422）。
  这是直连模式最脆弱的一处，必须钉在 CI 上。
- **两模式等价守护**：同一条命令 + 同一组参数，展开与折叠模式构造出的请求
  （方法、路径、query、body）完全一致。
- **结果整形守护**：超大响应必须被截断且带提示；业务错误必须变成
  `is_error=True` 且保留中文 `message`；`data` 必须同时出现在
  `structured_content` 与文本 `content` 里。
- **工具面构成守护**：上传/下载类、`x-cli-hidden`、会话递归类操作一律不出现在
  `tools/list`（样本从 spec 现取，新增同类操作自动纳入）；注解推导正确
  （`GET` → `readOnlyHint`，`x-cli-dangerous: destructive` → `destructiveHint`）。
- **手动验证**（不进 CI，给人用）：`scripts/mcp_client_demo.py <url> <token>` 用**官方
  客户端**连一个真实端点，跑通握手 → `tools/list` → `tools/call`。自动化测试走的是
  进程内 ASGI，这个脚本走真实 HTTP，两者互补。P1 验收时用它在真服务上跑过：
  `mode` 取 `2026-07-28` / `legacy` / `auto` 三种都能接（旧代协商到 `2025-11-25`）。
- **依赖守护**：`mcp` 版本上限锁死；`docker/runtime-version` 已 bump 13 → 14
  （CI 既有守卫会拦漏 bump）。
- **CLI 命令面**：新增 `mcp` 域会让 Go 侧三个守护变红（命令树快照 ×2、域帮助覆盖），
  已同步 `cli/testdata/*.txt` 与 `help_text.go` 的 `domainHelp`。
- **鉴权守护**：无令牌/错令牌/已吊销/端点停用/总开关关 → 401/401/401/404/404。
- **既有守护的登记**：`tests/api/test_auth.py` 匿名白名单加 `/mcp/{slug}`；
  `tests/api/test_mclaw_tool_wiring.py` 因 `mcp` 进 `_EXCLUDED_DOMAINS` 自动通过。

---

## 10. 交付分期

| 期 | 内容 | 估量 |
| --- | --- | --- |
| **P1**（已完成） | 依赖引入 + runtime-version bump · 设置域 · 管理面 REST · ASGI 调度器 + SDK 接线 · 工具面渲染（两种模式）· 调度执行（spec→请求、结果整形、截断）· 设置页分区 · 冒烟与守护测试 | 实际：后端 ~1100 行、前端 ~830 行、测试 ~380 行 |
| **P2** | 调用日志页、端点级速率限制、工具描述的按域润色 | 中 |
| **P3** | OAuth 2.1 + RFC 9728（打通 claude.ai 网页版自定义连接器） | 大 |

比手写协议少掉的：JSON-RPC 分发、两代兼容、SSE、错误码与头校验（约 350 行 + 长期跟规范）。
新增的：SDK 接线与 lifespan 处理（§4.3 的两点风险）、依赖升级流程。
比走 CLI 少掉的：argv 渲染与流式命令登记表。
新增的：结果截断与默认 limit 注入、长任务改由模型调 `jobs_wait`。

**P1 必须 bump `docker/runtime-version`（13 → 14）并在合并后发布新镜像**
（CLAUDE.md 硬约束 2：动了 pyproject dependencies 就要 +1）。
仍然不需要数据库迁移，也不新增 `data/` 目录。

引入依赖时要顺带验证一次（否则可能返工）：`mcp` 要求 `pydantic>=2.12`，
本仓的 `sqlmodel<0.1` / `fastapi` / `pydantic-settings` 都得在这个版本下跑通——
装完先跑一遍 `pytest -m "not integration"`，这是 P1 的第一个检查点。

### 10.1 P1 落地顺序（已按此执行完毕）

每步都有可验证的完成标志，前一步不绿不进下一步：

| # | 做什么 | 完成标志 |
| --- | --- | --- |
| 1 ✅ | `pyproject` 加 `mcp>=2.1,<3.0`；`docker/runtime-version` 13 → 14 | 全量回归与改动前基线逐条一致，无新增失败；实装版本 `mcp 2.1.1` / `pydantic 2.13.5` / `httpx 0.28` 与 `httpx2 2.12` 并存 |
| 2 ✅ | 设置域 `settings/mcp.py` + 在 `settings/__init__.py` 登记 | 建/读/改端点的单测过；令牌只落哈希 |
| 3 ✅ | 工具面渲染：spec → `Tool[]`（两种模式、注解推导、构成过滤） | 工具名唯一性 + 工具面构成守护过 |
| 4 ✅ | 调度执行：`operation_id` → 请求 → `CallToolResult`（含截断） | 参数映射守护 + 两模式等价守护 + 结果整形守护过 |
| 5 ✅ | SDK 接线与 ASGI 调度器（含 lifespan 处理，§4.3 风险点） | SDK 客户端冒烟（新旧两代）+ 鉴权守护过 |
| 6 ✅ | 管理面 REST，挂管理区；`mcp` 加进 `_EXCLUDED_DOMAINS` | 匿名/成员守护测试过；`mclaw mcp …` 命令自动可用 |
| 7 ✅ | 设置页「MCP 服务」分区 | 起服务建一个端点，用 Claude Code 实际接上并跑通一次工具调用 |

新增文件（预计）：

```
src/movieclaw_mcp/__init__.py        # 包入口与 register(app)
src/movieclaw_mcp/app.py             # ASGI 调度器 + SDK Server 构建与缓存
src/movieclaw_mcp/tools.py           # spec → Tool[]（两种模式、注解、过滤）
src/movieclaw_mcp/dispatch.py        # 工具调用 → 本机 API → CallToolResult
src/movieclaw_api/settings/mcp.py    # mcp.endpoints 配置域
src/movieclaw_api/api/routes/mcp.py  # 管理面 REST
src/movieclaw_api/schemas/mcp.py     # 管理面请求/响应模型
src/movieclaw_mcp/selfcheck.py       # 连通性自检（SDK 内存客户端跑真实一轮）
apps/web/components/mcp-section.tsx  # 设置分区外壳（列表 / 详情 / 创建三态）
apps/web/components/mcp/…            # 工具目录、端点表单、端点详情与共用小部件
apps/web/lib/api/mcp.ts              # 前端接口封装
tests/mcp_server/…                   # 冒烟 + 六组守护（目录不叫 mcp：会盖住 SDK 的包）
scripts/mcp_client_demo.py           # 用官方客户端连真实端点的连通性自检
```

改动既有文件：`settings/__init__.py`（登记配置域）、`api/router.py`（挂管理面）、
`app.py`（注册 `/mcp` 调度器，与 Jellyfin 同一位置）、`lifespan.py`（SDK 生命周期）、
`services/mclaw_tool.py`（`_EXCLUDED_DOMAINS` 加 `mcp`）、
`apps/web/lib/mock-data.ts` + `components/settings-view.tsx`（注册新分区）、
`pyproject.toml`、`docker/runtime-version`。

---

## 11. 拍板记录

六条评审决定（2026-09），按拍板顺序：

| # | 议题 | 结论 | 详见 |
| --- | --- | --- | --- |
| 1 | 工具粒度 | 做成端点级开关「展开工具」，**默认展开**；展开后工具名 `<模块>_<命令>`，不加前缀。不同客户端对工具面的适配逻辑不一致，选择权交给用户 | §4.1 |
| 2 | 协议实现 | **用官方 `mcp` SDK**，不自己写协议层。代价是新增依赖并 bump runtime-version，换来协议演进由上游承担 | §4.3 |
| 3 | 执行路径 | **直连本机 API**（进程内 ASGI），不经 mclaw 子进程。延迟低一个量级、与 CLI 解耦；代价是截断/长任务/错误映射要自己补。连带把折叠模式的参数改成 `command` + `params` | §4.2 |
| 4 | 执行策略 | **不做**执行档位（只读/标准/完全三档已删）。控制手段就是「选服务 / 启停 / 吊销」三件已有的事 | §4.7 |
| 5 | 应答形态 | **恒 `application/json`，不开 SSE**（`json_response=True` + `stateless_http=True`）。SSE 只服务进度推送 / 回问客户端 / 变更长流，三件我们都不用 | §4.8 |
| 6 | 工具数提示 | **不设硬上限**，但展开模式超过 **30** 个工具时给黄色建议「改用折叠模式」，并显示折叠后的数字。只建议不拦截 | §4.1 |

**待定：无。方案可进入实现（落地顺序见 §10.1）。**

实现前请留意两处已知风险，都写在正文里：SDK 挂载子应用的 lifespan 处理（§4.3），
以及 `mcp` 要求的 `pydantic>=2.12` 与本仓 sqlmodel/fastapi 的兼容性（§10.1 第 1 步）。

---

## 12. 参考

- MCP 规范 `2026-07-28`：<https://modelcontextprotocol.io/specification/2026-07-28/>
  （[变更清单](https://modelcontextprotocol.io/specification/2026-07-28/changelog)、
  [Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)、
  [授权](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)）
- 版本策略：<https://modelcontextprotocol.io/specification/versioning>
- Python SDK v2 beta 说明：<https://blog.modelcontextprotocol.io/posts/sdk-betas-2026-07-28/>
- Python SDK v2 文档：<https://py.sdk.modelcontextprotocol.io/>
  （[低阶 Server](https://py.sdk.modelcontextprotocol.io/advanced/low-level-server/)、
  [挂进现有 ASGI 应用](https://py.sdk.modelcontextprotocol.io/run/asgi/)、
  [协议版本支持](https://py.sdk.modelcontextprotocol.io/protocol-versions/)）
- 工具面精选与「工具太多」的实证：
  <https://thenewstack.io/15-best-practices-for-building-mcp-servers-in-production/>、
  <https://www.speakeasy.com/docs/mcp/build/toolsets/advanced-tool-curation>
- 客户端接入形态：<https://code.claude.com/docs/en/mcp-quickstart>
