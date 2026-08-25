# movieclaw 字幕支持计划：媒体库能力与 Jellyfin 适配的分层设计

> 状态：**最终定稿 v4（2026-08-10），可实施**。
> 版本轨迹：v1 定接口与台账；v2 补齐与 Jellyfin master 入库/播放两线的
> 细节比对；v3 按 [jellyfin-compat.md](jellyfin-compat.md) §8.5 分层原则
> 重切职责（A 媒体库 / B 播放领域 / C Jellyfin 适配），修复轨记忆存
> Jellyfin 流序号的分层违规（顺带消灭编号漂移缺陷，§3.3）；v3.1 补
> §2.4 元数据存储与探测成本模型（内封/外挂全落元数据、失效键显式化，
> 记录并修复"秒过不比对 mtime、视频原地替换永不重探"的现状缺陷）；
> v4 终审修正 §3.4 默认轨排序的 forced 方向错误（对照
> MediaStreamSelector 复核：非 forced 完整字幕优先、forced 沉底），补
> 原盘条目边界、中文语言别名、strm"仅字幕流"形态、query 键归一化
> 实施要点四处。
> v4.1 同步主干成员体系（[member-management.md](member-management.md)
> P0-P2 已合入 main）：轨记忆随 playback_state 天然按成员隔离（保真度
> 反而更高）、Subtitles 路由强制库可见性、"单用户模型"表述全部改为
> "成员体系无每成员字幕偏好"。
> 源起：jellyfin-compat.md §6.5 与 §11 开放问题 1。
> 关联：[library.md](library.md)、[strm-workflow.md](strm-workflow.md)。

## 0. 全景比对（结论压缩版，细节见各层章节）

真 Jellyfin 字幕链路九环节 × movieclaw 现状：内封轨探测（环节 1）✅ 已有；
外挂发现/命名解析/外挂探测/库选项下载（2-5）❌ 全无；默认轨选择（6）❌ 无；
记忆选择（7）⚠️ **宣告未兑现**——UserDto 已输出
`RememberSubtitleSelections: true` 但上报的轨序号被忽略；投递协商（8）与
字幕输出接口（9）❌ 无。

不做清单（比对裁决，理由见 v2 审定，此处存目）：HLS 字幕/烧录（无转码）、
Attachments/FallbackFont（转码与网页 libass 场景）、字幕管理五接口与在线
下载 provider 生态（Policy 已宣告关闭；播放器自带在线字幕）、外挂图形字幕
`.sub/.idx/.sup`（歧义与支持参差）、外挂音轨/歌词（不搭车）、SubtitleMode
五态全量（成员体系无每成员字幕偏好配置——`user_configuration()` 对所有
成员恒输出 Default 模式与空语言偏好，实现 Default 一种即语义自洽；将来
成员页若加字幕偏好，B 层选择函数加成员维度参数即可）。

## 1. 分层切分总表（本设计的骨架）

三层与归属模块，每一项能力只允许出现在一层：

| 能力 | 层 | 模块 | 为什么在这层 |
|---|---|---|---|
| 外挂字幕发现（同目录前缀匹配） | A 媒体库 | `movieclaw_api/services/library/` | "库里有什么"的事实，与谁来播放无关；控制台详情页同样要展示 |
| 文件名 token 解析（语言/旗标/标题） | A 媒体库 | 同上（纯函数） | 台账落库的一部分，入库时一次性完成 |
| `external_subtitles` 台账列 | A 媒体库 | `movieclaw_db` | 与 `subtitle_streams`（内封）平行的库存事实 |
| 字幕内容服务（编码归一 + 格式转换） | B 播放领域 | `movieclaw_playback/subtitles.py` | 网页播放器要 vtt 走同一函数（§8.5 早已点名） |
| 轨引用中性模型（embedded/external/off） | B 播放领域 | 同上 | 记忆与选择的通用语言，不含任何协议方言 |
| 默认字幕轨选择策略 | B 播放领域 | 同上 | "有外挂优先、尊重 default/forced"是播放器通行 UX，网页端同款 |
| 轨选择记忆（读写服务 + 存储） | B 播放领域 | `movieclaw_playback/state.py` + `playback_state` 表 | scrobble 同族语义，进度回报已在此层 |
| 流编号 Index 分配与反解 | C Jellyfin 适配 | `movieclaw_jellyfin` | **Index 是 Jellyfin 方言**（合成序号），领域层禁止出现 |
| MediaStream DTO / DisplayTitle / Codec 惯用名 | C Jellyfin 适配 | `movieclaw_jellyfin/catalog.py` | PascalCase 与拼串规则纯属协议形态 |
| DeliveryMethod/DeliveryUrl（含 ApiKey） | C Jellyfin 适配 | 同上 | 投递协商是 Jellyfin 的 DeviceProfile 体系概念 |
| 两条 Subtitles Stream 路由 | C Jellyfin 适配 | `movieclaw_jellyfin/routes/playback.py` | GUID 反解 + HTTP 形态，内容来自 B 层 |
| DefaultSubtitleStreamIndex（含 -1）翻译 | C Jellyfin 适配 | 同上 | 中性引用 ↔ Index/-1 的双向换算 |
| 进度上报轨字段消费 | C Jellyfin 适配 | `movieclaw_jellyfin/routes/playstate.py` | Index → 中性引用换算后调 B 层落库 |

依赖方向恒为 C → B → A（查询台账），反向引用禁止。未来网页播放器走
`/api/playback/*` 直接消费 B 层（vtt 输出、默认轨、记忆），不碰 C 层——
与 §8.5 的既定架构完全一致。

## 2. A 层：媒体库能力（与播放无关的库存事实）

### 2.1 外挂字幕发现（对照 Jellyfin MediaInfoResolver，扫描期执行）

同目录下，文件名去扩展名后满足：以视频 stem 为前缀（OrdinalIgnoreCase），
且要么恰好等于 stem、要么紧跟字符是 `.`（Jellyfin MediaFlagDelimiters
仅 `.`）。`Movie.mkv` 匹配 `Movie.srt/Movie.chs.srt/Movie.双语.ass`，
不匹配 `Movie2.srt`。扩展名 v1 收 `srt/ass/ssa/vtt`。

- Jellyfin 还扫 internalMetadataPath（在线下载字幕落位）；我们无下载，跳过；
- **不 ffprobe 外挂文件**（Jellyfin 逐个探测确认真实 codec；我们信任
  扩展名——四种扩展名语义明确，`.sub` 歧义源已排除在外；坏文件在 B 层
  服务期以 404+中文日志显性暴露，另省下云盘挂载上的逐文件子进程开销）；
- strm 条目同样适用：strm 占位文件旁的字幕在本地，照常发现——云端媒体 +
  本地中文字幕正是 strm 工作流的常见形态；
- 原盘条目（iso/BDMV 目录）v1 不做发现：原盘播放本就由播放器整盘处理，
  外挂字幕对齐成本高价值低，台账恒 `[]`；
- 已知行为（对齐 Jellyfin，非缺陷）：多版本同目录时短 stem 是长 stem 的
  前缀（`Movie.mkv` 与 `Movie.2160p.mkv`），`Movie.2160p.chs.srt` 会同时
  匹配到 `Movie.mkv`（剩余 token 进 title）——前缀匹配的固有特性，
  真 Jellyfin 同样如此。

接入点：全量/增量扫描在视频行建立/秒过时顺带匹配（目录列表已在内存，
零额外 IO 轮次）；watchdog 的字幕扩展名事件映射到同目录同前缀视频行触发
重发现（无宿主视频的字幕事件忽略）；探测补探（PROBING）不碰外挂——
内封/外挂两套数据源互不牵连。

### 2.2 命名解析（对照 ExternalPathParser，纯函数）

stem 之后的剩余段按 `.` 分隔，**从右往左**逐 token（判定一律大小写
不敏感）：

1. 等于 `default` → `default=true`；
2. 等于 `forced`/`foreign` → `forced=true`；
3. 等于 `cc`/`hi`/`sdh` → `sdh=true`；
4. 语言映射表命中 → `language`（首个命中生效）；
5. 都不是 → 拼入 `title`（保持原序）。

与 Jellyfin 的两处有意差异：旗标用**整段相等**而非 Contains（中文命名里
巧合子串误判风险 > 宽松收益）；语言表**不收 `hi`**（印地语/听障旗标撞名，
Jellyfin 为此写特判，我们直接规避）。语言映射十几行常量落 A 层（台账存
ISO 639-2/B 三字码；C 层展示表 `_LANG_DISPLAY` 是另一件事，各归各层）：
`chs/cht/zh/zh-cn/zh-hans/zh-hant/chi/zho→chi`、`en/eng→eng`、
`ja/jp/jpn→jpn` 等——并比 Jellyfin 多收**中文命名别名**（它的
FindLanguageInfo 只认 ISO 码与英文名）：`简中/简体/繁中/繁体/中字/
中英/双语→chi`，这是面向中文字幕组命名习惯的差异化价值，成本只是表行。

### 2.3 台账：`library_file.external_subtitles`（可空 JSON 列）

三态惯例同 `subtitle_streams`：NULL=未发现过（旧行重扫回填）、`[]`=发现过
但没有、非空=清单。元素：

```json
{"filename": "Movie.2024.chs.default.srt",
 "format": "srt", "language": "chi", "title": "chs",
 "default": false, "forced": false, "sdh": false,
 "size_bytes": 51234, "file_mtime_ns": 1730000000000000000}
```

只存 basename（外挂必须与视频同目录，搬迁时同移，全路径徒增一致性负担）；
`size_bytes/file_mtime_ns` 发现时 stat 落库，仅供服务期新鲜度探测——
**浏览零媒体 IO** 硬约束不被破坏。迁移为纯增列、可空、无回填（发布规范
硬约束 3）。

### 2.4 元数据存储与探测成本模型（失效键显式化）

原则（用户拍板 2026-08-10）：**内封与外挂字幕信息全部落元数据台账，
浏览/播放期零探测；视频文件不变就永不重探**。展开成可实施的规则：

**存储保持两列分开，"类型"即列本身**——不合并为一个带 type 字段的大
JSON 列。两类字幕的生命周期与失效键完全不同，合列只会互相拖累：

| | 内封 `subtitle_streams` | 外挂 `external_subtitles` |
|---|---|---|
| 数据来源 | ffprobe 读视频本体 | 目录列表 + 文件名解析 |
| 失效键 | 视频文件 (size, mtime) | 同目录 sidecar 文件集 (名/size/mtime) |
| 重建成本 | 子进程 + 读视频头（云盘挂载上是网络往返） | 纯内存匹配，**零视频读取** |
| 写入方 | 入库探测 / PROBING 补探 | 扫描 sidecar 发现 / watchdog 字幕事件 |

分列后两个写入方各写各列，无读改写竞争；外挂字幕增删改**永不触发**
视频 ffprobe。统一的"带类型字幕清单"视图在 B 层（§3.1 中性引用
`embedded:k` / `external:filename` 本身就是类型区分），读方不感知分列。

**内封重探的触发条件（顺带修复一个现状缺陷）**：现状秒过路径对已识别
在位行不比对 mtime，watchdog 的 modified 事件最终也走秒过——**视频文件
原地替换（洗版/重灌同路径）后内封台账永远陈旧**，ETag 也是错的。补两个
挂钩，均以 (size, mtime) 变化为准：

- 手动全量/增量扫描：在位行加一次 stat 比对，变了→重探内封 + 刷
  `file_mtime_ns/size_bytes`（用户主动发起的扫描，每文件一次 stat 成本
  可接受——本就是"重新扫描"的语义预期）;
- watchdog 视频文件 modified 事件：事件本身就是变更信号，去抖后对该行
  stat 确认变化再重探（免全库比对）；
- **6 小时对账保持现状不加 stat**：云盘挂载整库逐文件 stat 正是
  issue #88 的教训，对账只管在位性，规格新鲜度由上面两个入口负责。

视频 (size, mtime) 不变 → 内封列与 ETag 永不重算，探测成本为零——
这就是"文件不变就不更新"的落地形态。

### 2.5 顺带收益：控制台详情页

详情页现已展示内封音轨/字幕轨（`subtitle_streams`）；`external_subtitles`
入台账后业务详情接口顺带输出，前端多渲染一节"外挂字幕"——纯展示增量，
不在本设计强制范围，实施时看改动大小决定是否同 PR。

## 3. B 层：播放领域服务（协议无关，双端复用）

### 3.1 轨引用中性模型（领域层的通用语言）

```
"embedded:<k>"    # 内封字幕轨，k = subtitle_streams 数组下标
"external:<filename>"  # 外挂字幕，台账 filename
"off"             # 用户明确关闭字幕（audio 无此值）
```

字符串编码，直接可入库可入 API。**Jellyfin 的 Index 合成序号不出现在
本层**——它由 C 层按自己的编号方案换算。这同时是 §2.4 分列存储之上的
统一"带类型字幕清单"读视图：读方拿到的就是类型区分的轨列表，不感知
底下内封/外挂两列的存储与失效差异。

### 3.2 字幕内容服务

```
resolve_subtitle(file: LibraryFile, track: str) -> SubtitleRef | None
    # "external:<filename>" → 校验台账存在 + 拼绝对路径
serve_subtitle(ref: SubtitleRef, out_format: str | None) -> tuple[bytes, str]
    # 读文件 → 编码归一(UTF-8) → 按需转格式 → (字节, MIME)
```

流水线（对齐 Jellyfin SubtitleEncoder 的必要子集）：

1. **编码归一（中文用户核心价值）**：charset-normalizer 探测，非
   UTF-8/ASCII（GBK/GB18030/BIG5 常见）→ 统一按 `gb18030` 超集解码重编
   UTF-8，失败退 `errors="replace"` 保底出字；同格式直出也过这步——
   真 Jellyfin 同款行为，乱码字幕比没字幕更劝退；
2. **格式转换**：pysubs2 解析 → 目标格式序列化，v1 仅 `srt↔vtt`
   （ass/ssa 不跨格式转换，对齐 Jellyfin"无转换器即失败"）；
3. **不做磁盘缓存**（有意简化）：文本字幕 <1MB、解析毫秒级，现读现转；
   Jellyfin 的缓存服务的是内封轨 ffmpeg 抽取（秒级），我们无此场景。

网页播放器将来经 `/api/playback/*` 调同一 `serve_subtitle` 拿 vtt。

### 3.3 轨选择记忆（修复 v2 的分层违规与漂移缺陷）

`playback_state` 增两列（可空、向前兼容）：

- `audio_track: str | None`、`subtitle_track: str | None`，存 §3.1 中性
  引用；NULL=从未上报。

v2 曾设计直接存 Jellyfin Index——那是方言渗入领域层，且有实际缺陷：
补探回填改变音轨数后 Index 全体漂移，记忆失效甚至错轨。中性引用天然
免疫（`external:Movie.chs.srt` 不随编号变）——**解耦不只是洁癖，
直接消灭了一个缺陷**。

**成员维度（主干成员体系合入后的同步）**：`playback_state` 行已按
`member_id` 分行（唯一键含成员，0=超管哨兵），新增两列**天然按成员
隔离**——每人各记各的字幕选择，语义与真 Jellyfin 的 per-user userData
完全对齐，保真度比单用户时代的设计反而更高。读写服务照 state.py 新约定
**显式传 `member_id`**（协议层从设备/会话凭据解析身份后传入，本层不做
身份判定）。

读写服务落 `movieclaw_playback/state.py`（进度回报已在此层）：值变化才
写（Progress 高频）；读出时校验引用仍有效（台账/内封轨还在），失效回落
§3.4 选择策略。

### 3.4 默认字幕轨选择策略

```
pick_default_subtitle(file: LibraryFile) -> str | None   # 中性引用或 None
```

单用户模型只实现 Default 模式语义（UserDto 宣告值），语言偏好空串=通配，
对照 Jellyfin MediaStreamSelector 在通配下的化简：候选按
`外挂 ↓ →（外挂内部）AI 生成 ↓ → default 旗标 ↓ → 非 forced ↓ → 稳定序`
排序（**forced 沉底**：它是"只在说外语片段显示"的部分字幕，Jellyfin 的
排序键在通配语言下正是非 forced 完整字幕优先，v3 曾把方向写反，定稿修正；
**AI 优先**是产品拍板 2026-08-25：AI 字幕是为这部片现生成的，比随片源顺来
的外挂更可能是用户想看的那条，优先级压过外挂的 default 旗标），过滤条件
`外挂 || default || forced`，取首个；全不命中 → None（不自动开字幕）。
效果：装了外挂字幕就预选外挂（中文用户装了就是要看的），否则尊重内封
default 旗标，仅当只有 forced 轨可选时才选 forced。音轨侧维持现状算法
（default 旗标优先），仅接入记忆优先级。

**这是全端唯一的默认字幕策略**（2026-08-25 对齐）：网页端此前自带一套
「default 旗标 → 兜底第一条」的简化版，同一部片在两个入口会自动选出不同
字幕。现在 profile 装配轨列表时直接把本函数的结论写进 `is_default`（不再
透传容器原始旗标），网页端只认这个标记、没标就不自动开——策略只在这一个
函数里，改动两端同时生效。

优先级组合（PlaybackInfo 时由 C 层调用）：
记忆值（有效）→ 选择策略 → None；记忆值 `"off"` 原样生效。

## 4. C 层：Jellyfin 协议适配（纯翻译，无业务逻辑）

### 4.1 流编号（Index 方言的唯一产地）

合成编号对齐 Jellyfin master：**外挂字幕 0..e-1**（台账数组序），随后是
video=e、audio e+1..n、内封字幕继续编号。双向换算函数与 DTO 构建同源，
供三处使用：MediaStream 输出、Subtitles 路由反解、进度上报换算。

此前曾把外挂流接在容器流之后，以避免增删 sidecar 漂移内封编号；VidHub
实测会因此过滤外挂流，说明客户端并不都只依赖 Index 自洽。现改为严格对齐
官方顺序；编号漂移对记忆的影响由 B 层中性引用消除（§3.3）。

### 4.2 MediaStream 输出（列表/详情/PlaybackInfo 通用）

```json
{"Type": "Subtitle", "Index": 0, "Codec": "subrip", "Language": "chi",
 "IsExternal": true, "SupportsExternalStream": true,
 "IsTextSubtitleStream": true, "IsDefault": false, "IsForced": false,
 "IsHearingImpaired": false, "DisplayTitle": "Chinese - SUBRIP - External"}
```

Codec 惯用名 srt→`subrip`、vtt→`webvtt`、ass/ssa 原名；DisplayTitle 照
既有拼接规则加 ` - External` 尾缀，language 空时用 title 原文顶格。

### 4.3 PlaybackInfo 增量（仅此场景）

- 外挂字幕流追加投递字段（**无条件输出**是既定偏离——真 Jellyfin 无
  DeviceProfile 不输出，我们不解析 profile，无条件输出是外挂可用的必要
  超集）：

```json
 "DeliveryMethod": "External", "IsExternalUrl": false,
 "DeliveryUrl": "/Videos/{itemGuid}/{msGuid}/Subtitles/{idx}/0/Stream.{fmt}?ApiKey=<token>"
```

  `fmt` 恒为源格式（Infuse/VidHub 全支持，无需预转换）；token 取当前
  请求已验证 token；列表/详情不带投递字段（对齐真 Jellyfin 只在
  PlaybackInfo 填充）；内封流照旧不填 DeliveryMethod（DirectPlay 播放器
  自解，真 Jellyfin 无 profile 时同样不填，无偏离）。

- **strm 源的特殊形态点名**：strm 条目不探测云端，`MediaStreams` 此前
  可为空数组；外挂字幕入台账后会变成"只有 Subtitle 流、无 Video/Audio
  流"的合法形态——这正是"云端媒体 + 本地字幕"的核心价值场景。协议上
  无禁忌（客户端 DirectPlay 时自行探测音视频轨），但属于真 Jellyfin
  不会出现的组合（它对 strm 强制远程探测），列入 S2 手测第一优先。

- `DefaultSubtitleStreamIndex`：调 B 层（记忆→策略），中性引用换算成
  Index；`"off"` → `-1`（协议里"-1=用户明确不要字幕"是有效值）；None →
  省略字段。`DefaultAudioStreamIndex` 同理接入记忆优先。

### 4.4 Subtitles 路由（2 条）

```
GET /Videos/{itemId}/{mediaSourceId}/Subtitles/{index}/{startPositionTicks}/Stream.{format}
GET /Videos/{itemId}/{mediaSourceId}/Subtitles/{index}/Stream.{format}
```

- 带 ticks 版转调不带 ticks 版，ticks 接受并忽略（不转码无 seek 平移；
  DeliveryUrl 恒填 0）；route 段同名 query 可覆盖（对齐 ParameterObsolete
  行为）；
- 路由 `format` 同时接受文件格式名与 Jellyfin/FFmpeg codec 名：
  `subrip→srt`、`webvtt→vtt`。VidHub 会按 `MediaStream.Codec`
  自行构造无 ticks 的 `Stream.subrip`，别名归一后再交 B 层判断是否转换；
- 鉴权 `require_device`（偏离③照旧：真 Jellyfin 匿名，我们要 token，
  DeliveryUrl 自带 `?ApiKey=`）；
- **库可见性强制**（成员体系同步）：文件装载复用
  `_files_for_ref(ref, identity.device.member_id)`——成员白名单外的库
  对其 404，与三个播放处理器同一约束（member-management.md §3.6：GUID
  可枚举，不能只在浏览路径挡、放字幕路径直进）；
- `mediaSourceId` 复用 `_select_source`（小写归一 + 等于 itemId 回落
  第一个版本——DeliveryUrl 的 msGuid 必须能反解）；
- `index` 经 §4.1 反解成中性引用，指到内封轨/越界 → 404；内容与
  Content-Type 全部来自 B 层 `serve_subtitle`（srt→
  `application/x-subrip`、vtt→`text/vtt`、ass/ssa→`text/x-ssa`，对齐
  Jellyfin MimeTypes 表）；
- 错误形态照本层惯例 404 空 body；日志中文说明原因（文件不在/解析失败/
  编码不明），非开发者可读；
- 实施要点：新增 query 参数名（`itemId/index/startPositionTicks`，
  `mediaSourceId/format` 已有）要登记进 router 的 `_KNOWN_QUERY_KEYS`
  大小写归一化表，否则 PascalCase 客户端的 query 覆盖取不到；路径段
  `Subtitles`/`Stream.` 由路由模板自动进归一化映射，无需额外处理。

### 4.5 进度上报消费（playstate 路由）

`/Sessions/Playing` 与 `/Sessions/Playing/Progress` 开始消费
`AudioStreamIndex/SubtitleStreamIndex`（此前接受并忽略）：经 §4.1 换算成
中性引用后带 `identity.device.member_id` 调 B 层记忆服务（playstate 路由
的进度落库已按成员隔离，轨记忆走同一身份）；`SubtitleStreamIndex: -1` →
`"off"`；换算失败（悬空索引）丢弃不落库；`Failed=true` 的 Stopped 照既有
规则整体跳过。§4.3 的 DefaultSubtitleStreamIndex 读取同理按当前设备的
member_id 查记忆。

## 5. 技术选型（Python 社区比对结论）

| 模块 | 选型 | 理由与备选 |
|---|---|---|
| 格式解析/转换 | **pysubs2** | MIT、纯 Python、活跃维护；SRT/ASS/SSA/VTT/MicroDVD 全读写——SubtitleEdit 的 Python 对应物。备选：srt（单格式）、webvtt-py（只围绕 vtt）、aeidon（GPL 依赖重） |
| 编码探测 | **charset-normalizer** | MIT、纯 Python，requests 官方以它替换 chardet（LGPL、维护放缓）；GB18030/BIG5 识别好；cchardet 系对 KB 级文件无意义 |
| 语言 token 映射 | 不引库，查表 | 十几行常量覆盖实际命名；langcodes 对"文件名猜语言"过重 |
| 内封轨抽取 | v1 不做 | DirectPlay 无此需求；将来沿用 media_probe 的 subprocess 风格调 ffmpeg，不引 ffmpeg-python（多年无维护） |

新增运行时依赖 `pysubs2`、`charset-normalizer` → **实施 PR 必须 bump
`docker/runtime-version`**（发布规范硬约束 2），合并后发新镜像。

## 6. 分期与验收（按层标注）

| 期 | 层 | 内容 | 验收 |
|---|---|---|---|
| S1 | A | 台账列 + 迁移 + 扫描/watchdog 发现 + 命名解析 + 内封重探挂钩（§2.4） | 单测:前缀匹配边界（stem 相等/带分隔/撞名不误收/多版本前缀重叠）、token 矩阵（语言含中文别名/旗标/大小写/从右往左序）、mtime 变→重探/不变→零探测;扫描后台账正确,strm 旁挂同样入账,原盘条目恒 [] |
| S2 | B+C | 内容服务（编码归一+srt↔vtt）+ 编号双向换算 + 外挂流 DTO + 投递字段 + Subtitles 路由（含库可见性）+ query 键归一登记 | 单测:编号换算互逆、GBK→UTF-8、srt→vtt 金样、坏文件 404+中文日志、**白名单外成员拉字幕 404**;手测:Infuse/VidHub 外挂字幕可选可显、GBK 不乱码、**strm"仅字幕流"源可播可显字幕（第一优先）** |
| S3 | B+C | 中性轨引用 + playback_state 增列 + 记忆读写（按成员） + 默认轨策略 + DefaultSubtitleStreamIndex/上报消费 | 单测:策略排序矩阵（**含 forced 沉底、仅 forced 可选才选 forced**）、记忆往返（含 off/-1）、**成员隔离（甲记忆不串到乙）**、引用失效回落、悬空索引丢弃;手测:切轨重进沿用、关字幕重进仍关 |

S1/S2 一起交付才有用户可见价值；S3 可独立后行。B 层全部函数不 import
`movieclaw_jellyfin`（架构守护测试加一条断言）。合并前照例全绿：
`pytest`、`ruff check .`、`pnpm web:lint`、`pnpm web:typecheck`。
