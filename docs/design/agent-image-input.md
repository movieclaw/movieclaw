# Agent 图片输入（多模态）支持

让用户在 AI 会话里发图片提问（截图报错、海报识别、字幕画面等），Agent 把图片
喂给视觉模型并正常走完 agent loop。本文记录设计定案与取舍，并给出分期落地清单。

方案调研了 Apache Maka（`apache/maka`，一个 TypeScript Agent 工作台）的多模态
实现，借鉴其四条核心结论：**图片字节与消息历史分离（引用化）**、**发请求时
按需水合成 base64**、**视觉能力 fail-closed 门控 + 文本降级**、**折叠给模型的
附件提醒文本只存在于请求投影，展示层永不渲染**；同时按本项目「简洁优先」的
原则大幅裁剪（不做逐请求 decision cache、不做溢出后反应式扔图等重型机制，
理由见 §12）。

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
6. **工具**：`read` 工具明确拒绝图片文件（OpenAI 协议 tool 消息不收图，见 §11）。

## 2. 总体设计：引用化 + 按需水合

核心原则一句话：**转录里只存引用，供应商请求里才有字节，展示层只渲染结构块。**

```
┌─上传────────────────┐   ┌─转录(JSONL)──────────────┐   ┌─发请求──────────────────┐
│ POST /sessions/      │   │ ImagePart{                │   │ 水合: 读文件→base64      │
│  attachments         │   │   attachment_id: "ab12…", │   │ + 视觉门控（fail-closed）│
│ 字节落 .staging/，   │──▶│   media_type, name        │──▶│ + 请求级图片预算         │
│ 消息提交时 rename 进 │   │ }  （data 恒为 None）      │   │ + 附件提醒文本（只在此   │
│ <session_id>.assets/ │   └───────────────────────────┘   │   投影里生成，不落库）   │
└──────────────────────┘                                   └──────────────────────────┘
```

为什么不内联 base64 进 JSONL（Maka 同样的结论）：

- 转录文件是事实源，`session.get-transcript` 全量返回 entries——内联意味着每次
  打开会话前端要拉几 MB 无用 base64；
- compaction / handoff 行携带整份 `replacement_history`，内联图会被复制多次；
- 引用化后磁盘上每张图每会话只有一份，字节生命周期与会话严格绑定（见 §3）。

引用化还有一个隐性收益：水合是确定性的（同一文件永远编码出同一 base64），
多轮请求里历史前缀字节稳定，供应商 prompt cache 照常命中；仅当预算把旧图挤成
占位文本的那一轮会 miss 一次，可接受。

## 3. 附件存储（并入 `agent-sessions/`）

**不新开顶层目录**：附件是会话的一部分，与转录同生命周期，放进现有的会话目录
让「一个会话 = 一个 JSONL + 一个 assets 目录」成为完整事实源，备份/迁移 data/
时天然带全：

```
data/agent-sessions/
  <session_id>.jsonl                     # 转录（现状）
  <session_id>.assets/                   # 本会话的附件
    <attachment_id>.jpg                  #   原始字节，扩展名按嗅探出的真实类型
    <attachment_id>.json                 #   sidecar 元数据
  .staging/                              # 上传后尚未绑定会话的中转区
    <attachment_id>.jpg / .json
```

sidecar 字段：`{mime, bytes, width, height, original_name, created_at}`。
不建数据库表：与会话转录同哲学——文件即事实源，且没有「按条件检索附件」的
查询需求，省掉一次 alembic 迁移（也就不触碰「迁移只能向前兼容」的硬约束）。

生命周期与绑定原子性：

- **上传**：落 `.staging/`（此刻可能还没有 session_id——新会话第一条消息就是
  这个场景）；
- **绑定**：`session.start` 引用附件时，把文件从 `.staging/` `os.rename` 进
  `<session_id>.assets/`（同一文件系统内原子）。顺序固定为**先 move、后落
  消息行**：move 后崩溃只留一个孤儿文件（清理兜底），反过来会留下用户看得见
  的悬空引用。并发两次 start 引用同一 staging 附件时，第二个 rename 直接
  `FileNotFoundError` → 报「附件已被使用」，竞态天然消解，无需加锁；
- **会话删除**：`session.delete` 连 `<session_id>.assets/` 目录一起删；
- **孤儿回收**：`.staging/` 里超过 24h 的附件由启动时 + 每日一次的惰性清理
  删除（上传了但没点发送）；retry 丢弃消息后「已绑定但无引用」的附件不立即删
  （用户大概率马上重发复用），由删会话兜底；
- **配额**：单会话附件总数上限 100，防滥用刷盘。

## 4. 上传与下载接口

```
POST /sessions/attachments            multipart 上传，require_login
  → { attachment_id, name, width, height, bytes }
GET  /sessions/{session_id}/attachments/{attachment_id}
  → FileResponse，require_login
  → Cache-Control: private, max-age=31536000, immutable
```

- 下载路由带 `session_id`：按 §3 布局 O(1) 定位文件，归属校验天然完成；只服务
  已绑定的附件（staging 阶段前端用本地 objectURL 预览，不需要回读服务端）；
- 附件 id 即内容（绑定后永不被覆盖），所以能打 `immutable`——浏览器缓存一次，
  翻历史会话零请求。对比 appearance 背景图要拼 mtime 版本号，这里不需要；
- **`attachment_id` 强校验为 32 位 hex 再参与任何路径拼接**（防路径穿越），
  `session_id` 同理沿用现有校验口径。

上传校验（全部中文报错，风格对齐 `appearance.backdrops.upload`）：

- **读取**：`await file.read(MAX + 1)` 后判超限——uvicorn 默认不限 body，
  不能无脑全量读；
- **格式**：仅 JPEG / PNG / GIF / WebP。以**魔数嗅探**判定真实类型（Maka 同款：
  PNG/JPEG/GIF87a/GIF89a/RIFF+WEBP 签名），不信 Content-Type 与扩展名——天然
  拒绝 SVG（可内嵌脚本）与改后缀的任意文件。GIF 有已知风险：部分兼容端点不认
  data URL 形态的 gif，保留支持、靠错误翻译兜底，出问题再收紧；
- **大小**：单张 ≤ 5MB（对齐主流视觉 API 的单图限制，也给 base64 膨胀留余量）；
- **可解码性与尺寸**：Pillow `Image.open` 读取尺寸并校验可解码；最长边 > 8000px
  拒绝（提示用户缩图）。服务端不做压缩/降采样——Web 前端在上传前用 canvas 把
  最长边压到 2048、JPEG 质量 0.85（视觉模型的有效分辨率就在这个量级，qwen-vl
  等端点还会再自行缩放），服务端只兜底校验。IM 通道接入时（无前端可压）再补
  服务端降采样（§10）。

## 5. 消息模型与协议改动（`movieclaw_llm`）

### 5.1 `ImagePart` 增加引用与展示字段

```python
class ImagePart(BaseModel):
    type: Literal["image"] = "image"
    url: str | None = None
    data: str | None = None
    media_type: str | None = None
    #: 服务端附件引用（会话 assets 目录下的 id）。转录里只存它；发请求前由
    #: API 层水合成 data。传输层看到「只有 attachment_id、没有 data/url」的
    #: 块时降级为占位文本（见 5.2），绝不把内部引用发给供应商。
    attachment_id: str | None = None
    #: 原始文件名。前端 chip/alt 文案，也是水合层生成附件提醒文本的原料
    #: （Maka 的 AttachmentRef 同样把 name/mime 存消息里、字节存别处）。
    name: str | None = None
```

纯增量改动：老版本读端（pydantic 默认忽略未知字段）读到新转录只会丢图不会坏行，
`SESSION_FORMAT_VERSION` 无需 bump。

### 5.2 协议层防御（`openai_chat._convert_content`）

水合是 API 层的职责（见 §6），但传输层要 fail-safe：遇到未水合的 ImagePart
（无 data 也无 url）时转成占位 TextPart「[图片未能加载，已略过]」并打中文
warning——宁可丢一张图，不把内部引用漏给供应商。占位后该消息若不再含图，
现有「纯文本压成字符串」的兼容分支自然回落，无需改动。

### 5.3 `AgentStartParams.input` 放宽

`input: str` → `input: str | list[ContentPart]`，runner 里
`ChatMessage(role="user", content=params.input)` 不变即可兼容两种形态。
IM 通道现有的 `input=msg.text` 调用不受影响。
`recorder.record_user_message` 签名同步放宽。

### 5.4 纯图消息与附件提醒文本（只在请求投影里）

允许用户只发图不打字（IM 上尤其常见）：转录里该 user 消息就是
`content=[ImagePart(...)]`，没有合成文字。**给模型的提醒文本在水合层生成、
不落库、前端永不渲染**（Maka 定案："presentation layers never show the folded
form"）：

- 带图消息统一在请求投影里追加一个附件清单 TextPart（name/mime 列表）；
- 纯图无文字时清单文本再带一句「用户未附文字，请查看图片内容并直接回应」。

它是从 ImagePart 元数据生成的确定性函数，每次请求重新生成——即使后续轮次里
图片字节被预算省略，清单文本仍在，模型的多轮记忆不断档；assistant 首轮对图片
的文字描述也会留在历史里，共同承载「记得这张图」的语义。

## 6. 请求水合、视觉门控与图片预算（API 层）

新增 `movieclaw_api/services/agent_attachments.py`，职责：存储读写（§3/§4）+
水合函数：

```python
async def hydrate_images(messages, model_info) -> HydrationResult:
    """把消息里的引用型 ImagePart 换成可发送形态，返回替换后的消息与统计。"""
```

**两个调用点**共用同一入口，缺一不可：

1. `_launch_user_message`：组装 `AgentStartParams` 前对 `history + 本轮 input`
   一次性水合（运行内多步循环复用同一 messages，图片只来自 user 消息，
   mid-run 不会新增，无需每步重复水合）；
2. `compact_session_context`（手动压缩接口）：它不经过 runner、直接
   `build_history()` 发模型——不水合的话视觉模型写摘要时看不到图（协议层防御
   只保证不崩，不保证看得见）。

规则按优先级：

1. **视觉门控（fail-closed）**：`"image" not in model_info.modalities` 时，所有
   ImagePart 替换为占位文本：
   `[用户发送了图片 <原名>，当前模型不支持图片输入，无法查看。请告知用户：可在设置中切换视觉模型（如 qwen3-vl-plus）；若当前模型实际支持视觉，请在供应商设置的「补录模型」中为它声明 modalities 后重试。]`
   ——不阻塞提交、不报错，模型能向用户解释清楚（Maka 同款降级）。目录外模型
   `modalities` 默认 `["text"]` 同样不发图；显式 `实例名/模型id` 写法的目录外
   视觉模型会被静默门控，所以文案必须给出 `extra_models` 声明这条出路（字段
   现成，设置页需要能编辑，见 §10）。
2. **读文件**：按 attachment_id 从会话 assets 目录读字节 → `data=base64`、
   `media_type` 来自 sidecar。文件不存在（被清理/迁移丢失）→ 占位文本
   `[图片 <原名> 已过期或被清理，无法查看；如需请让用户重新发送。]`，不失败。
3. **请求级图片预算**：单次运行水合的图片原始字节总量 ≤ 8MB（base64 后约
   10.7MB，给正文与工具 schema 留出供应商请求体上限的余量——Maka 取 12MB，
   我们的消息面更小取 8MB）。**从最新往旧保留**：对话场景里用户最近发的图
   才是当前任务对象，超预算的旧图替换为占位文本
   `[更早的一张图片因请求体积限制已省略；如仍需要请让用户重发。]`。
   同一附件在历史中出现多次（retry 沿用等）按出现次数计费——请求体就是多份。

实现注意：读文件 + base64 编码是几 MB 级、几十毫秒量级的操作，走
`asyncio.to_thread`，不套用会话 JSONL「微秒级同步 IO 不进线程池」的豁免。

**落库脱水（不变量，下沉到 store）**：`AgentSessionStore` 的三个写入口
（`append` / `append_compaction` / `append_handoff`）统一归一化——凡带
`attachment_id` 的 ImagePart 一律置 `data=None`。store 是唯一写盘口，守在这里
才覆盖所有路径；此前设想放在 recorder 是错的：手动压缩直接调
`store.append_compaction`，不经过 recorder，水合后的 `replacement_history`
会把 base64 写进转录。

## 7. 历史链路逐一核对

| 场景 | 处理 |
|---|---|
| 续聊（`build_history`） | 转录里是引用，水合层统一处理，无需改动 |
| retry | **现有代码会丢图**：`content = payload.content or target.message.text()`，`text()` 只取 TextPart。改为：重试时从目标消息提取 ImagePart 一并重组；`SessionRetryPayload` 增加 `attachments: list[str] | None`——`None`=沿用原消息附件，`[]`=显式去图，非空=替换。discard 后附件仍在会话 assets 目录里，id 继续有效 |
| fork / handoff | **同 id 复制附件文件**到新会话的 assets 目录（目录按会话隔离，id 不冲突，快照里的引用一个字不用改）。handoff 的既有原则是「新会话不依赖源文件」，附件必须遵守同一原则；单图 ≤5MB，复制成本可接受 |
| 压缩（`build_replacement_history`） | 保留的用户原话**连图片引用一起保留**（引用很便宜，真正的成本由 §6 的请求级预算兜底）；`_truncate_middle` 只作用于纯文本消息——带图消息超预算时不做中部截断（截断路径用 `text()` 重建 content 会丢图），整条按预算取舍：装得下整条保留，装不下整条丢弃 |
| 压缩调用本身（`compact()`） | runner 内自动压缩复用运行内已水合的 messages；手动压缩接口显式水合（§6）。视觉模型写摘要时看得到图，非视觉模型看到门控占位文本 |
| `estimate_tokens` | 每个 ImagePart 计 **1000 token**（qwen-vl 按 28×28 patch 计价，2048×1024 的图约 2600 token，1000 是「前端已压到 2048 边长」后中等图的量级；估算只服务 90% 水位的压缩触发，偏差可接受，常量留出调整空间） |
| 空响应重试 | runner 原样 `continue` 重发，messages 未动，带图请求不受影响 |
| 中断收尾 / 孤儿 tool_call 修复 / `_repair_handoff_history` | 不涉及（图片只在 user 消息） |

## 8. API 协议与引用规则

**引用规则一句话：`attachment_id` 是接口层图片的唯一引用，任何接口都不下发
字节。**

### 8.1 提交协议

```python
class SessionStartPayload(BaseModel):
    content: str = Field(min_length=0, max_length=4000)   # 带图时允许纯图无文字
    attachments: list[str] = Field(default_factory=list, max_length=4)  # attachment_id
    ...
    # 校验：content 与 attachments 不能同时为空
```

服务端组装 `content=[TextPart(text), ImagePart(attachment_id=..., media_type=...,
name=...)...]`；无附件时保持纯字符串形态不变（兼容端点对字符串 content 最稳，
现有行为零变化）。

**协议约束（安全属性）**：对外接口只收 `attachment_id` 列表，ContentPart 永远
由服务端组装——调用方没有任何途径注入 `url:` 形态（SSRF 面）或内联 base64。
retry 同口径。

### 8.2 转录与事件

- `session.get-transcript` **结构零改动**：`SessionMessageView.content` 本来
  就是 `ContentPart` 判别联合，ImagePart 新字段随 pydantic 自动透出，老读端
  （含 CLI）忽略新字段；
- `SessionMessageView.from_model` 里**防御性剔除 `data`**——脱水不变量之外的
  第二道闸，防老数据或异常路径把几 MB base64 推给前端；
- SSE 事件协议**不变**：图片只出现在 user 消息，而 user 消息不走事件流；
- 侧栏预览（`summarize()` 的 title/last_prompt）：text 为空且含 ImagePart 的
  消息用 `[图片]`（多张 `[图片 ×N]`）占位，带文字时文字优先。

### 8.3 Web 前端

- **composer**：选图 / 拖拽 / 粘贴截图 → canvas 压缩（最长边 2048）→ 上传
  → 缩略图 chip（可删除，staging 阶段用 `URL.createObjectURL` 本地预览）
  → 发送时带 attachment_id 列表；
- **气泡渲染**：遍历 content parts——TextPart 走现有 markdown 渲染；ImagePart
  渲染 `<img src={api}/sessions/{sid}/attachments/{id}>` 缩略图（CSS 限最大
  高度，`name` 作 alt），点击开大图。附件提醒文本不落库（§5.4），气泡里只有
  用户原话 + 图片，没有样板文案；
- **乐观 UI**：刚发出的消息用本地 objectURL 渲染，刷新/其他端打开才走
  transcript + 下载接口——所以下载接口只服务已绑定附件，与 §4 自洽；
- **模型选择器**：非视觉模型加「不支持图片」置灰提示（模型列表接口需透出
  `modalities` 字段）；
- **CLI**：transcript 渲染 ImagePart 用 `[图片 <name>]` 文本形态；CLI 发图
  放 P1。

## 9. 安全清单

- 魔数嗅探 + Pillow 解码双重校验，拒绝 SVG / 伪装文件；
- `attachment_id` 强校验 32 位 hex 再进路径拼接（防路径穿越）；
- ContentPart 永不接受外部直传（防 url 注入 / SSRF，见 §8.1）；
- 附件 id 用 `uuid4().hex`，不可枚举；下载接口 require_login（会话现状无
  per-member 隔离，附件同口径，无新增泄露面）；
- 上传接口对 `identity.kind == "agent"` 拒绝（与「Agent 不能递归发起运行」同
  口径，防 Agent 用产品令牌向会话注入图片）；
- 上传 `read(MAX+1)` 提前掐断超大 body；
- 单图 5MB、单消息 4 张、单请求 8MB、单会话 100 张四级限额。

## 10. P1 —— IM 通道图片接入（三通道均可支持，微信协议已验证）

三个通道的入站图片能力已逐一核实（微信对照参考实现
`Tencent/openclaw-weixin` 源码验证），**没有不可行项**：

| 通道 | 入站图片路径 | 结论 |
|---|---|---|
| Telegram | `message.photo` 是多尺寸 `PhotoSize` 数组（caption 现已在读、图被丢弃），取最大档 `file_id` → `getFile` → `https://api.telegram.org/file/bot<token>/<file_path>` 下载；走既有 `egress_transport("telegram")` 代理。TG 的 photo 经服务端压缩（通常几百 KB），天然 ≤5MB；`document` 形式的原图先不接 | 最容易，约 1 天 |
| Discord | `message.attachments[]` 直接带 CDN url + `content_type/size/width/height`，按 content_type 过滤图片 GET 下载即可；走 `egress_transport("discord")` | 容易，约 1 天 |
| 微信 iLink | **协议支持收图**：`MessageItemType.IMAGE = 2`（现有 adapter 只解析 1=文本/3=语音，注释里未列图片枚举）。`image_item.media` 携带 `full_url`（新协议：服务端直接给完整下载 URL）或 `encrypt_query_param`（回退拼 `{cdn_base}/download?encrypted_query_param=…`，默认 CDN 基址 `https://novac2c.cdn.weixin.qq.com/c2c`）；密文需 **AES-128-ECB（PKCS7）** 解密，key 优先取 `image_item.aeskey`（hex），否则 `media.aes_key`（base64，历史上还有 base64(hex) 双重编码，两种都要认）；无 key 时是明文 CDN 直下。解密用已有依赖 `cryptography`——**无新增运行时依赖，不用 bump `docker/runtime-version`**。CDN 与 iLink 网关同口径国内直连、不走代理。`thumb_media`（缩略图）可作原图过大时的降级 | 可行，参考实现完整可对照移植，约 2~3 天 + 真机验证 |

已知风险与顺带发现：

- iLink 是半公开协议，key 编码存在版本差异（参考实现注释「in the wild」确认
  两种编码并存），实现按双编码兼容 + 真机测试兜底；
- 现有注释「iLink 未开放图片上传」已过时——参考实现已有完整上传路径
  （GetUploadUrl + AES 加密上传），未来 bot 主动发海报可以升级，与本期无关。

通用管线（三通道共享）：

- `InboundMessage` 增加 `images: list[tuple[bytes, str]]`（字节 + mime）；
  各 adapter 下载后落 §3 的同一存储、组装同样的 parts，Agent 侧零改动；
- **纯图消息直接触发运行**：`content=[ImagePart]`，靠 §5.4 的请求投影提醒
  文本让模型知道「用户发来一张图」并主动回应；多轮记忆由清单文本 + assistant
  首轮描述承载，与 Web 同一套机制，IM 侧不需要特殊逻辑；
- **入站聚合（debounce）**：微信用户习惯先甩图再打字，图一到就触发会浪费一轮
  并逼出一句「请问要做什么」。入站侧做 2~3 秒聚合窗口，把连续到达的图+文合并
  成一条消息再触发（session_key 已串行，聚合天然安全）；
- 两处 Agent 装配（`im_channel.py` 与 `weixin_channel.py`）都要接；
- 服务端降采样（Pillow 压到 2048 边长）在此期落地——IM 进来的图没有前端可压；
- 设置页 `extra_models` 的图片声明（「视觉」勾选 → modalities）**已存在**，
  即 §6 门控文案指引的出路；补录表单向完整「能力声明」表（图片/视频/
  思考控制统一一节）的演进见 `agent-thinking-level.md` §4.4——视频声明
  先行落表，Agent 侧视频输入落地时门控从只读 image 扩展。

## 11. P2 —— 暂不做但已想清楚的

**工具结果图片**（read 读图、截图类工具）：OpenAI Chat Completions 的 tool
消息 content 只收字符串/文本块，**不收 image_url**（多数兼容端点同样不收）。
业界通行桥接是「tool 消息回文本占位 + 紧随一条 user 消息携带图片块」；Maka 因
走 Anthropic 协议可以把图片放进 tool result 的 content 数组，我们不行。该桥接
会引入「合成 user 消息」的历史语义污染（retry 定位、压缩保留规则都要排除它），
复杂度不小，等真实需求出现再做。

**供应商图片引用（Files API）**：`ImagePart.url` 形态已支持 http 引用，若未来
接入支持文件上传的端点（如 kimi 文件 API），可上传一次换 url 复用，省掉逐请求
base64。协议层无需改动。

## 12. 主要取舍记录

- **不做 Maka 式逐图 decision cache**：Maka 需要它是因为「每次请求现场重新
  水合 + 请求可能中途重建」，判定必须跨重试幂等。我们每次运行只水合一次、
  运行内复用，天然幂等，cache 属过度设计。
- **不做溢出后反应式扔图**（Maka 的 `provider-image-overflow-recovery`）：
  那是「上下文超长错误 → 先扔历史图再摘要压缩」的第二道防线。我们的图片
  只在 user 消息且有请求级预算 + 90% 水位主动压缩，触发面小得多；真撞上
  供应商超长错误时走现有的压缩失败降级路径即可。出现实际案例再补。
- **附件提醒文本不落库**：落库省一次投影计算，但前端气泡会渲染出样板文案、
  retry/压缩还要识别并排除它；请求时生成是确定性函数，成本忽略不计
  （Maka 同款定案）。
- **图片 token 用固定估算而非按分辨率计算**：估算只影响压缩触发时机，按
  分辨率精算对四家供应商各有公式，收益配不上复杂度。
- **附件不建 DB 表、不新开顶层目录**：sidecar json + 会话目录布局已满足
  绑定/清理/备份需求；未来若要做「按用户检索附件」再迁移（届时注意迁移向前
  兼容的硬约束）。
- **自建网关 413 风险**：带图请求体可达 10MB+，用户自建反代的
  `client_max_body_size` 可能拦截——错误翻译层已把 4xx 转成中文
  `LlmRequestError`，文档 FAQ 提示调大即可，不在代码侧做探测。

## 13. 落地步骤（P0，每步可独立验证）

```
1. movieclaw_llm：ImagePart.attachment_id/name + 协议层未水合防御
   → 验证：单测覆盖「引用块→占位文本」「data 块→data URL」「占位后纯文本
     回落字符串」三条路径
2. agent_attachments 服务：目录布局 / 魔数嗅探 / sidecar / staging 绑定 / 清理
   → 验证：单测覆盖 伪装文件拒绝、超限拒绝、id 非 hex 拒绝、并发绑定同一
     staging 附件二次 rename 报错、孤儿回收、删除会话联动清理
3. 上传/下载路由 + SessionStartPayload.attachments + 消息组装 +
   retry 附件三态（None 沿用 / [] 去图 / 列表替换）
   → 验证：API 测试覆盖 纯图消息、图文混合、content 与 attachments 同时为空
     被拒、引用他人会话附件被拒、retry 原文重试图不丢
4. hydrate_images（两个调用点：_launch_user_message + 手动压缩）：
   门控 / 读文件 / 预算 + store 三写入口统一脱水 + 请求投影附件提醒文本
   → 验证：单测覆盖 非视觉降级文案、缺文件占位、超预算旧图占位（倒序保留）、
     纯图消息提醒文本只出现在请求投影、手动压缩路径转录 grep 不到 base64
     （脱水不变量）
5. compaction：estimate_tokens 计图 + 带图消息不做中部截断 + fork 同 id 复制
   → 验证：现有压缩测试矩阵扩展带图用例；fork 后删源会话，新会话图仍可水合
6. Web：composer 上传与预览、气泡渲染、乐观 UI、侧栏 [图片] 占位、
   模型选择器视觉标记（模型列表接口透出 modalities）
   → 验证：pnpm 侧组件测试 + 手工全链路（发图 → qwen-vl 描述图片内容）
```

全链路验收标准：给 bailian 实例配 `qwen3-vl-plus`，在 Web 会话粘贴一张报错
截图**不打字直接发送**，Agent 主动描述图片内容并继续调用工具排查；追问
「刚才那张图里的错误码是多少」能答对（多轮记忆）；切换到 `deepseek-chat`
retry 同一条消息，Agent 明确告知无法看图并建议切换视觉模型，且 retry 后图
未丢失；转录 JSONL 中 grep 不到 base64、`<session_id>.assets/` 中有对应文件、
删除会话后 assets 目录同步消失。
