# Agent 图片输入（多模态）支持

让用户在 AI 会话里发图片提问（截图报错、海报识别、字幕画面等），Agent 把图片
喂给视觉模型并正常走完 agent loop。本文记录设计定案与取舍，并给出分期落地清单。

方案调研了 Apache Maka（`apache/maka`，一个 TypeScript Agent 工作台）的多模态
实现，借鉴其三条核心结论：**图片字节与消息历史分离（引用化）**、**发请求时
按需水合成 base64**、**视觉能力 fail-closed 门控 + 文本降级**；同时按本项目
「简洁优先」的原则大幅裁剪（不做逐请求 decision cache、不做溢出后反应式扔图
等重型机制，理由见 §11）。

## 1. 现状盘点

已有的地基（本期直接复用，不动）：

| 组件 | 现状 |
|---|---|
| `movieclaw_llm/models.py` | `ImagePart`（url 或 base64+media_type）已定义，`ChatMessage.content` 支持 `list[ContentPart]` |
| `protocols/openai_chat.py` | `_convert_content` 已能把 `ImagePart` 转成 OpenAI 线协议的 `image_url`（data URL 或 http url） |
| `ModelInfo.modalities` | 字段已存在，预设目录已标注（qwen-vl / kimi 视觉系 / gpt-4o 等为 `[text, image]`），但**当前无任何消费方** |
| 上传基建 | `python-multipart`、`pillow` 已是运行时依赖（首页背景图上传在用），**无需 bump `docker/runtime-version`** |

缺口（本期要补的）：

1. **入口**：`SessionStartPayload.content` 是纯字符串，Web/IM 都没有图片入口；
2. **存储**：会话转录是 JSONL 原样落 `ChatMessage`——base64 直接内联会让转录
   膨胀（一张 3MB 图 ≈ 4MB base64，compaction/handoff 的 `replacement_history`
   还会整份复制）；
3. **门控**：不区分模型是否支持视觉，非视觉模型收到 `image_url` 会被供应商 400；
4. **预算**：`estimate_tokens` 明确不计图片块（当时的注释：Agent 不产图片输入，
   为它建模属过度设计——现在前提变了）；
5. **通道**：`InboundMessage` 只有 `text`，微信/TG/Discord 的图片消息直接丢失；
6. **工具**：`read` 工具明确拒绝图片文件（OpenAI 协议 tool 消息不收图，见 §10）。

## 2. 总体设计：引用化 + 按需水合

核心原则一句话：**转录里只存引用，供应商请求里才有字节。**

```
┌─上传────────────┐   ┌─转录(JSONL)──────────────┐   ┌─发请求────────────────┐
│ POST /sessions/  │   │ ImagePart{                │   │ 水合: 读文件→base64    │
│  attachments     │──▶│   attachment_id: "ab12…", │──▶│ + 视觉门控             │
│ 字节落           │   │   media_type: "image/…"   │   │ + 请求级图片预算       │
│ data/agent-      │   │ }  （data 恒为 None）      │   │ → data URL 给协议层    │
│  attachments/    │   └───────────────────────────┘   └───────────────────────┘
└──────────────────┘
```

为什么不内联 base64 进 JSONL（Maka 同样的结论）：

- 转录文件是事实源，`session.get-transcript` 全量返回 entries——内联意味着每次
  打开会话前端要拉几 MB 无用 base64；
- compaction / handoff 行携带整份 `replacement_history`，内联图会被复制多次；
- 引用化后磁盘上每张图只有一份，字节的生命周期可独立管理（会话删除联动清理、
  孤儿回收）。

## 3. 附件存储（`data/agent-attachments/`）

新增目录，与 `agent-sessions/`、`agent-workspace/` 平级，Docker 挂 `data/` 一个
卷即整体持久化（符合 CLAUDE.md 的运行期数据约定）。

```
data/agent-attachments/
  <attachment_id>.jpg          # 原始字节，扩展名按嗅探出的真实类型
  <attachment_id>.json         # sidecar 元数据
```

sidecar 字段：`{mime, bytes, width, height, original_name, created_at, session_id}`。
`session_id` 上传时为空（staging 态），消息提交成功后回填绑定。

不建数据库表：与会话转录同哲学——文件即事实源，且没有「按条件检索附件」的
查询需求，省掉一次 alembic 迁移（也就不触碰「迁移只能向前兼容」的硬约束）。

生命周期：

- **绑定**：`session.start` 引用某 attachment_id 时校验「存在 &&（未绑定 ||
  已绑定同一会话）」，成功落消息后写入 session_id；
- **会话删除**：`session.delete` 时扫描 sidecar，把 `session_id` 匹配的附件
  一并删除；
- **孤儿回收**：上传后 24h 仍未绑定会话的附件，由启动时 + 每日一次的惰性清理
  删除（用户上传了但没点发送的场景）。

## 4. 上传与下载接口

```
POST /sessions/attachments        multipart 上传，require_login
  → { attachment_id, width, height, bytes }
GET  /sessions/attachments/{id}   FileResponse，require_login
  → 图片字节（前端 <img> 与转录回放用；Cache-Control: private, max-age 长期）
```

校验（全部中文报错，风格对齐 `appearance.backdrops.upload`）：

- **格式**：仅 JPEG / PNG / GIF / WebP。以**魔数嗅探**判定真实类型（Maka 同款：
  PNG/JPEG/GIF87a/GIF89a/RIFF+WEBP 签名），不信 Content-Type 与扩展名——天然
  拒绝 SVG（可内嵌脚本）与改后缀的任意文件；
- **大小**：单张 ≤ 5MB（对齐主流视觉 API 的单图限制，也给 base64 膨胀留余量）；
- **可解码性与尺寸**：Pillow `Image.open` 读取尺寸并校验可解码；最长边 > 8000px
  拒绝（提示用户缩图）。服务端不做压缩/降采样——Web 前端在上传前用 canvas 把
  最长边压到 2048、JPEG 质量 0.85（视觉模型的有效分辨率就在这个量级，qwen-vl
  等端点还会再自行缩放），服务端只兜底校验。

## 5. 消息模型与协议改动（`movieclaw_llm`）

### 5.1 `ImagePart` 增加引用字段

```python
class ImagePart(BaseModel):
    type: Literal["image"] = "image"
    url: str | None = None
    data: str | None = None
    media_type: str | None = None
    #: 服务端附件引用（data/agent-attachments 下的 id）。转录里只存它；
    #: 发请求前由 API 层水合成 data。传输层看到「只有 attachment_id、没有
    #: data/url」的块时降级为占位文本（见 5.2），绝不把内部引用发给供应商。
    attachment_id: str | None = None
```

纯增量改动：老版本读端（pydantic 默认忽略未知字段）读到新转录只会丢图不会坏行，
`SESSION_FORMAT_VERSION` 无需 bump。

### 5.2 协议层防御（`openai_chat._convert_content`）

水合是 API 层的职责（见 §6），但传输层要 fail-safe：遇到未水合的 ImagePart
（无 data 也无 url）时转成占位 TextPart「[图片未能加载，已略过]」并打中文
warning——宁可丢一张图，不把 `attachment://` 之类的内部引用漏给供应商。

### 5.3 `AgentStartParams.input` 放宽

`input: str` → `input: str | list[ContentPart]`，runner 里
`ChatMessage(role="user", content=params.input)` 不变即可兼容两种形态。
IM 通道现有的 `input=msg.text` 调用不受影响。

## 6. 请求水合、视觉门控与图片预算（API 层）

新增 `movieclaw_api/services/agent_attachments.py`，职责：存储读写（§3/§4）+
水合函数。水合在 `_launch_user_message` 组装 `AgentStartParams` 前一次性完成，
作用于 `history + 本轮 input`：

```python
def hydrate_images(messages, model_info) -> HydrationResult:
    """把消息里的引用型 ImagePart 换成可发送形态，返回替换后的消息与统计。"""
```

规则按优先级：

1. **视觉门控（fail-closed）**：`"image" not in model_info.modalities` 时，所有
   ImagePart 替换为占位文本：
   `[用户发送了图片 <原名>，当前模型不支持图片输入，无法查看。请告知用户：可在设置中切换视觉模型（如 qwen3-vl-plus / moonshot-v1-vision）后重试。]`
   ——不阻塞提交、不报错，模型能向用户解释清楚（Maka 同款降级）。目录外模型
   `modalities` 默认 `["text"]`，同样不发图；自建端点用户通过供应商配置的
   `extra_models` 自行声明 `modalities: [text, image]` 即可放行（字段现成）。
2. **读文件**：按 attachment_id 读字节 → `data=base64`、`media_type` 来自
   sidecar。文件不存在（被清理/跨实例迁移丢失）→ 占位文本
   `[图片 <原名> 已过期或被清理，无法查看；如需请让用户重新发送。]`，不失败。
3. **请求级图片预算**：单次运行水合的图片原始字节总量 ≤ 8MB（base64 后约
   10.7MB，给正文与工具 schema 留出供应商请求体上限的余量——Maka 取 12MB，
   我们的消息面更小取 8MB）。**从最新往旧保留**：对话场景里用户最近发的图
   才是当前任务对象，超预算的旧图替换为占位文本
   `[更早的一张图片因请求体积限制已省略；如仍需要请让用户重发。]`。

水合结果只存在于本次运行的内存 `messages` 里；运行中的多步循环复用同一列表，
无需每步重复水合（图片只来自 user 消息，mid-run 不会新增）。

**落库脱水**：`AgentSessionRecorder` 与 compaction 落盘路径写 JSONL 前做一次
归一化——凡带 `attachment_id` 的 ImagePart 一律置 `data=None`。这是引用化
不变量的守门员：compaction 的 `replacement_history` 来自内存中已水合的消息，
不脱水就会把 base64 写进转录。

## 7. 历史链路逐一核对

| 场景 | 处理 |
|---|---|
| 续聊（`build_history`） | 转录里是引用，水合层统一处理，无需改动 |
| retry | 原文重试沿用原消息（含图）；传新 `content` 时附件沿用原消息的附件（换图 = 删掉重发，不做部分编辑） |
| fork / handoff | **复制附件文件到新 id** 并改写快照里的引用。handoff 的既有原则是「新会话不依赖源文件」（源会话删除不影响新会话），附件必须遵守同一原则；图片单份 ≤5MB，复制成本可接受 |
| 压缩（`build_replacement_history`） | 保留的用户原话**连图片引用一起保留**（引用很便宜，真正的成本由 §6 的请求级预算兜底）；`_truncate_middle` 只作用于文本——带图消息超预算时不做中部截断，整条按预算取舍 |
| 压缩调用本身（`compact()`） | 自然成立：它复用运行内已水合的 messages，视觉模型写摘要时看得到图，非视觉模型看到的是门控占位文本 |
| `estimate_tokens` | 每个 ImagePart 计 **1000 token**（qwen-vl 按 28×28 patch 计价，2048×1024 的图约 2600 token，1000 是「前端已压到 2048 边长」后中等图的量级；估算只服务 90% 水位的压缩触发，偏差可接受，常量留出调整空间） |
| 中断收尾 / 孤儿 tool_call 修复 | 不涉及（图片只在 user 消息） |

## 8. API 与前端

### 8.1 提交协议

```python
class SessionStartPayload(BaseModel):
    content: str = Field(min_length=0, max_length=4000)   # 带图时允许纯图无文字
    attachments: list[str] = Field(default_factory=list, max_length=4)  # attachment_id
    ...
    # 校验：content 与 attachments 不能同时为空
```

服务端组装 `content=[TextPart(text), ImagePart(attachment_id=..., media_type=...)...]`；
无附件时保持纯字符串形态不变（兼容端点对字符串 content 最稳，现有行为零变化）。

### 8.2 转录与事件

- `session.get-transcript` 返回的 `SessionMessageView.content` 天然携带引用型
  ImagePart（`ContentPart` 联合已含 image），前端按
  `GET /sessions/attachments/{id}` 渲染 `<img>`；
- SSE 事件协议**不变**：图片只出现在 user 消息，而 user 消息不走事件流。

### 8.3 Web 前端（composer + 会话视图）

- composer 支持 选图 / 拖拽 / 粘贴截图 → canvas 压缩（最长边 2048）→ 上传
  → 缩略图 chip（可删除）→ 发送时带 attachment_id 列表；
- 会话气泡里 user 消息渲染图片缩略图，点击看大图；
- 模型选择器上给非视觉模型一个「不支持图片」的置灰提示（读 `modalities`，
  需要在模型列表接口透出该字段）。

## 9. 安全清单

- 魔数嗅探 + Pillow 解码双重校验，拒绝 SVG / 伪装文件；
- 附件 id 用 `uuid4().hex`，不可枚举；下载接口 require_login；
- 附件目录只由服务端写入，路径由 id 拼接（不接受用户提供的文件名入路径）；
- 上传接口对 `identity.kind == "agent"` 拒绝（与「Agent 不能递归发起运行」同
  口径，防 Agent 用产品令牌向会话注入图片）；
- 单图 5MB、单消息 4 张、单请求 8MB 三级限额，防转录与请求体膨胀。

## 10. 暂不做但已想清楚的（P1 / P2）

**P1 —— IM 通道图片接入**：`InboundMessage` 增加
`images: list[tuple[bytes, str]]`（字节 + mime）；Telegram 走 `getFile` 下载
photo，Discord 直接下载 attachment url，微信 iLink 的图片下载能力待调研
（现有注释确认 iLink 未开放图片**上传**，下载未知）。下载后走 §3 的同一存储
与消息组装，Agent 侧零改动。

**P1 —— 服务端降采样**：Pillow 把超尺寸图压到 2048 边长，替代对前端压缩的
依赖（IM 通道进来的图没有前端可压，届时必须做）。

**P2 —— 工具结果图片**（read 读图、截图类工具）：OpenAI Chat Completions 的
tool 消息 content 只收字符串/文本块，**不收 image_url**（多数兼容端点同样
不收）。业界通行桥接是「tool 消息回文本占位 + 紧随一条 user 消息携带图片块」；
Maka 因走 Anthropic 协议可以把图片放进 tool result 的 content 数组，我们不行。
该桥接会引入「合成 user 消息」的历史语义污染（retry 定位、压缩保留规则都要
排除它），复杂度不小，等真实需求出现再做。

**P2 —— 供应商图片引用（Files API）**：`ImagePart.url` 形态已支持 http 引用，
若未来接入支持文件上传的端点（如 kimi 文件 API），可上传一次换 url 复用，
省掉逐请求 base64。协议层无需改动。

## 11. 主要取舍记录

- **不做 Maka 式逐图 decision cache**：Maka 需要它是因为「每次请求现场重新
  水合 + 请求可能中途重建」，判定必须跨重试幂等。我们每次运行只水合一次、
  运行内复用，天然幂等，cache 属过度设计。
- **不做溢出后反应式扔图**（Maka 的 `provider-image-overflow-recovery`）：
  那是「上下文超长错误 → 先扔历史图再摘要压缩」的第二道防线。我们的图片
  只在 user 消息且有请求级预算 + 90% 水位主动压缩，触发面小得多；真撞上
  供应商超长错误时走现有的压缩失败降级路径即可。出现实际案例再补。
- **图片 token 用固定估算而非按分辨率计算**：估算只影响压缩触发时机，按
  分辨率精算对四家供应商各有公式，收益配不上复杂度。
- **附件不建 DB 表**：sidecar json 已满足绑定/清理需求，未来若要做「按用户
  检索附件」再迁移（届时注意迁移向前兼容的硬约束）。

## 12. 落地步骤（P0，每步可独立验证）

```
1. movieclaw_llm：ImagePart.attachment_id + 协议层未水合防御
   → 验证：单测覆盖「引用块→占位文本」「data 块→data URL」两条路径
2. agent_attachments 服务：存储布局 / 魔数嗅探 / sidecar / 绑定 / 清理
   → 验证：单测覆盖 伪装文件拒绝、超限拒绝、孤儿回收、删除会话联动清理
3. 上传/下载路由 + SessionStartPayload.attachments + 消息组装 + retry 沿用
   → 验证：API 测试覆盖 纯图消息、图文混合、attachment 复用他人会话被拒
4. hydrate_images：门控 / 读文件 / 预算 + recorder 落库脱水
   → 验证：单测覆盖 非视觉降级文案、缺文件占位、超预算旧图占位、
     转录文件中 grep 不到 base64（脱水不变量）
5. compaction：estimate_tokens 计图 + 保留规则带引用 + fork 附件复制
   → 验证：现有压缩测试矩阵扩展带图用例；fork 后删源会话，新会话图仍可水合
6. Web：composer 上传与预览、气泡渲染、模型选择器视觉标记
   → 验证：pnpm 侧组件测试 + 手工全链路（发图 → qwen-vl 描述图片内容）
```

全链路验收标准：给 bailian 实例配 `qwen3-vl-plus`，在 Web 会话粘贴一张报错
截图并问「这是什么错」，Agent 正确描述图片内容并继续调用工具排查；切换到
`deepseek-chat` 重试同一条消息，Agent 明确告知无法看图并建议切换视觉模型；
转录 JSONL 中无 base64、`data/agent-attachments/` 中有对应文件。
