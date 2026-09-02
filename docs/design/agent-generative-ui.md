# Agent 生成式 UI：render_media_cards 工具与前端卡片渲染

> 背景：Agent 的工具集（bash/read/write/edit/mclaw）是通用的，模型的产出只有
> Markdown 正文。影音产品的核心资产——海报、库封面、订阅与播放入口——在会话
> 里只能被「说」出来。本设计给模型一个**只描述、不取数**的绘图工具，前端拦截
> 该工具调用后按编号绘制产品同款卡片，让会话页长出影音特色。

---

## 0. 原理：对齐 AG-UI 的 render-only frontend tool

AG-UI（Agent-User Interaction Protocol，CopilotKit 主导）把「生成式 UI」做成
一种特殊的工具：

1. 工具声明照常给模型（name / description / JSON Schema），模型像调用任何
   工具一样发起 `TOOL_CALL_START → TOOL_CALL_ARGS → TOOL_CALL_END`；
2. 前端按**工具名**注册渲染器（`useComponent` / `useRenderToolCall`），拦截到
   同名调用后把参数当作组件 props 直接绘制；参数流式生成中可先画骨架，参数
   定稿后画完整卡片；
3. 工具本身不需要真正执行——CopilotKit 对 render-only 工具回喂一个空字符串，
   模型看到「有回执」就继续下一步；
4. 历史消息重放走同一条路：`assistant.toolCalls` 里的调用与实时事件用同一个
   渲染器绘制，所以旧会话打开时卡片仍在。

movieclaw 已有的事件协议（`tool_call_start / tool_call_delta / tool_call /
tool_result`，见 `movieclaw_agent/events.py`）与 AG-UI 的三段式一一对应，
转录也完整保存 `tool_calls`，因此**不需要引入 AG-UI 的 SDK 或改协议**，
只需：后端加一个工具，前端加一个按名字匹配的渲染器。

与 AG-UI 的两点刻意差异：

- **回执不是空串**：handler 返回一句中文回执（画了几张什么卡 + 提醒不要复述），
  是给模型的运行时教学；前端不读它。
- **工具在服务端注册，不由前端随请求上送**：工具集属于服务端编排职责
  （runner 设计既定），且 IM 通道不该拿到这个工具。

## 1. 工具定义（`movieclaw_agent/tools/media_ui.py`）

```
name: render_media_cards_v1
```

**名字带版本**是本设计的硬约束：工具名就是前端渲染器的匹配键。参数契约发生
不兼容变更时发 `_v2` 并在前端新增一套解析器；旧会话转录里的 `_v1` 调用继续
按旧规则绘制，永远不会出现「升级后历史会话的卡片认不出来」。兼容的新增
可选字段不升版本。

参数（刻意扁平，不用 oneOf——不少 OpenAI 兼容端点对 oneOf 支持很差）：

```json
{
  "component": "library | title | library_item",
  "items": [
    { "library_id": 3 },
    { "tmdb_id": 693134, "media_type": "movie" },
    { "douban_id": "1292052" },
    { "media_item_id": 42, "season": 1, "episode": 3 }
  ],
  "title": "可选的一行小标题"
}
```

| component | 画什么 | 每项参数 | 编号来源 |
|---|---|---|---|
| `library` | 媒体库卡片：封面拼贴 + 库名 + 类型与库存统计 | `library_id` | `mclaw library list` |
| `title` | 影片/剧集海报卡片：海报、评分、年份，自动标注已入库/已订阅，悬停一键订阅 | `tmdb_id`+`media_type` 或 `douban_id` | `search titles` / `discover` 的 `title_ref` 换算 |
| `library_item` | 库内条目播放卡片：剧照或海报 + 一键播放 + 观看进度 + 片源规格 | `media_item_id`，剧集可选 `season`+`episode` | `library items list` / `search library-items` |

handler 语义：

- 只做跨字段校验（title 必须给 tmdb_id+media_type 或 douban_id、season/episode
  成对、一次最多 12 项），错误文案带 `items[i].字段` 指向该改哪里，由 runner
  作为失败结果回喂，模型自行修正后重发；
- **不查编号是否存在**，也不取任何数据：卡片数据由前端现取，转录里不留过期
  快照；编号不存在时卡片显示「未找到」，用户能看出模型引用了不存在的东西；
- 返回固定回执：`已在会话页展示 N 张xx卡片。卡片内容由界面实时加载，无需再用
  文字复述……`。

description 承担全部领域语义（与 mclaw 同一原则，系统提示词正文零改动）：
每个组件传什么、编号从哪来（`tmdb:movie:123 → tmdb_id=123, media_type=movie`）、
什么时候该画（先用 mclaw 查证编号真实存在）、以及画完不要复述。

## 2. 启用开关

工具的效果完全依赖前端拦截 tool_call 后绘制，因此**是否带上它由装配方按通道决定**：

- `routes/agent.py::get_agent_tools(cli_env, *, generative_ui=False)`：开关默认关，
  新接入的通道不会因为忘记关而误带；网页会话的两条装配路径（发消息 / 改写重问）
  显式传 `generative_ui=True`；
- **IM / 微信通道**（`im_channel` / `weixin_channel` 的受限工具集）永远不带：那边
  无法解析卡片，模型调了只会收到一句「已展示卡片」而用户什么都看不到；
- `tests/api/test_media_ui_tool_wiring.py` 三条守护：默认关、网页显式开、IM/微信
  模块连 `make_media_ui_tool` 的 import 都不允许出现。

description 除了「能画什么、编号从哪来」，还写明**为什么要画**（海报是影音产品的
第一印象、卡片上能直接订阅/播放、纯文字罗列显得单薄）——模型知道好处才会在
合适的时机主动使用，而不是把它当成可有可无的装饰。

## 3. 前端渲染（`apps/web`）

```
lib/agent-media-cards.ts        纯逻辑：工具名版本匹配 + 参数 → 卡片规格（node --test 覆盖）
components/agent-media-cards.tsx 三种卡片 + 卡片组 + 处理过程块的接入组件
lib/agent-conversations.tsx     AgentTurnToolCall 新增 args（tool_call 事件与转录回放都填）
components/agent-conversation-view.tsx  process 段之后渲染该段内的卡片组
```

时间线接入：卡片组作为**常显内容**紧跟在对应「处理过程」折叠块之后——卡片是给
用户看的，不能藏在折叠块里。折叠块本身**不再列出**这次绘制调用：它的产出就是
紧随其后的卡片，再列一行「调用 render_media_cards_v1」只是噪音；整块只剩这一个
调用时连折叠头也不出现，用户看到的就是「模型的回答里直接带着卡片」。

绘制时机：

- `tool_call_start` / `tool_call_delta`（参数未定稿）：不画。编号只有几十个字节，
  骨架态没有意义；
- `tool_call`（参数定稿）：立即按参数取数并绘制，不等 `tool_result`——工具执行
  本身几乎零耗时；
- 调用失败（`is_error`，或回放数据里 runner 的「工具执行失败：」前缀）：不画，
  模型会按错误回执重发；
- 历史回放：`entriesToTurns` 把转录 `tool_calls.arguments` 填进 `args`，与流式
  路径完全相同。

数据与视觉全部复用既有资产：

| 卡片 | 取数 | 视觉 |
|---|---|---|
| 媒体库 | `GET /libraries/{id}` | 服务端封面拼贴 `GET /libraries/{id}/cover`（媒体库首页同图），空库/失败退回类型图标 |
| 影片 | `GET /discover/titles/{title_ref}` → `MediaItem` | `PosterCardVisual`（发现页同款：评分、已入库斜标、悬停订阅键）；订阅状态来自 `SubscribeEntryProvider`，另加「已订阅」斜标 |
| 库内条目 | `GET /playback/items/{id}` → 库归属，再并行 `GET /libraries/{lib}/items/{id}`（剧照/规格）与 `GET /playback/resume`（进度） | 最近观看卡同款：16:9 剧照 + 中央播放键（兄弟节点 `<a>`）+ 进度条 + 规格行 |

失败与加载占位与真实卡片同尺寸——卡片组出现在正文中间，尺寸抖动会把正在阅读
的内容推来推去。

## 4. 扩展新组件的步骤

1. `media_ui.py`：`COMPONENT_LABELS` 加一项、`_DESCRIPTION` 写清参数与编号来源、
   `validate_items` 加跨字段校验、schema 的 `items.properties` 加字段；
2. `lib/agent-media-cards.ts`：`MediaCardSpec` 加一种、`parseItem` 加分支、补测试；
3. `components/agent-media-cards.tsx`：写卡片组件并接进 `MediaCard`；
4. 只加组件属于兼容扩展，不升版本。改动已有组件的参数含义才升 `_v2`：后端
   改 `TOOL_NAME`，前端保留 `_v1` 解析器并新增 `_v2`。

## 5. 测试

- `tests/agent/unit/test_media_ui_tool.py`：名字带版本、description 覆盖每个组件与
  编号换算、schema 过 `validate_tool_call`、跨字段校验错误指向具体字段、回执文案；
- `apps/web/test/agent-media-cards.test.mjs`：版本匹配、三种组件的参数解析、非法项
  跳过与去重、未知版本返回 null。
