# 媒体库「其他」类型：不刮削内容的入账、展示与播放

> 状态：**v1（2026-09-03）——方案已拍板，待实施**。v0 草案的八个待决问题
> 已由用户逐条决定（第 8 节决策记录），本文按决策改写；第 9 节是实施前
> 剩余的两个小口子。
> 关联文档：[library.md](library.md)（媒体库架构）、[metadata.md](metadata.md)
> （元数据自足）、[web-player.md](web-player.md)（网页播放器）、
> [jellyfin-compat.md](jellyfin-compat.md)（播放器兼容层）、
> [library-home-recently-watched.md](library-home-recently-watched.md)、
> [library-file-recycle.md](library-file-recycle.md)。

## 0. 目标与定位

用户诉求：媒体库除电影/剧集外增加一类**不走影视识别链**的库——家庭
录像、自录内容、课程视频等。这些视频只有目录结构和文件，多数在 TMDB 上
没有对应条目，不该触发电影/剧集的识别、刮削与订阅联通；但要兼容标准
协议：视频旁若有 NFO 就读 NFO 作为信息来源，否则用文件名。入账后要能
正常浏览、播放、记进度，并供给第三方播放器（Infuse 等）使用。

定位一句话：**这是一类「条目就是文件」的库**。它的内容没有作品身份，
只有文件本体；Emby 的 Home Videos、Jellyfin 的 `homevideos`、Plex 的
Other Videos 都是同一心智。

两条产品原则（用户决策 2026-09-03）：

- **文件级原生能力一律保持一致**：播放、进度、最近观看、回收站、AI 字幕、
  trickplay、外挂字幕、下载/导入落库……凡作用于文件本体的能力，其他库与
  影视库同等享有。只有作用于**作品身份**的能力（识别、刮削、订阅、命名
  模板整理、认领/复核）与它无关；
- **不引入目录层级**：库内平铺展示（相册心智），目录只是磁盘上的组织
  方式，不建模、不导航。

## 1. 现状评估：整条链路锚在 `media_item` 上

三条探查（类型枚举 / 扫描识别展示链 / 播放与 Jellyfin）得出的核心事实：

1. **`MediaKind` 只有 movie/tv，且取值就是 TMDB 路径段**
   （`movieclaw_media/models.py`）。`Library.kind` 是裸字符串列，但扫描、
   整理、认领、条目列表、入库等约十处入口第一行就是 `MediaKind(library.kind)`，
   第三个值会直接抛 `ValueError`；`nfo.py` / `organize.py` / Jellyfin
   `CollectionType` 等十余处是「非 movie 即 tv」的隐式二分，第三个值会**静默
   按剧集处理**。全仓两值假设约 40 处后端、30 处前端（见附录 A）。
2. **`media_item.tmdb_id` NOT NULL，唯一键 `(kind, tmdb_id)`**，设计文档明确
   「不允许创建无 tmdb_id 的无锚条目」。海报墙（`items.py` 三条分页查询
   都硬过滤 `media_item_id IS NOT NULL`）、条目详情、`/play/{media_item_id}`
   地址、`playback_state`（`media_item_id` NOT NULL 外键）、最近观看、
   Jellyfin 目录（`item_ids_with_files` 同样过滤 NULL）**全部只认这个锚**。
   今天产品对「没有 TMDB 身份的文件」的唯一答案是**待识别清单**——一个
   待办队列，不是可浏览内容。
3. **`library_file` 没有任何展示标题字段**。待识别清单的名字是请求时用
   条目目录名/文件名临时拼的。
4. **两处现成的缝**：① 播放决策与取流已经是文件寻址的——
   `PlaybackDecideRequest.file_id`、`/playback/files/{file_id}/stream`
   不碰 `media_item`，只是网页端从未用过；② Jellyfin GUID 已有 0x05
   `MEDIA_SOURCE(library_file_id)` 类型，0x08 起空闲。
5. **扫描器的大部分机制是类型无关的**：目录遍历、ffprobe、台账 upsert、
   改名归并（尺寸+时长指纹）、缺失对账、回收站、实时监控、统计快照——
   只有 `_identify` / `_unit_for` / `_kind_conflict` / 资产阶段四处是影视
   专属。新类型**不是另写一个扫描器**，而是在这四处分叉。

结论：难点不在扫描，在**身份与寻址**——展示、播放、进度、Jellyfin 四条链
要么给它一个假的 `media_item`，要么给它一条文件寻址的并行通道。

## 2. 核心决策：不造假身份，播放单元升级为「条目单元 | 文件」

| 方案 | 做法 | 评价 |
|---|---|---|
| A 合成 `media_item` | 每个文件造一条 `kind=other, tmdb_id=负数/哈希` 的条目，复用全部下游 | 改动最少，但污染身份中枢：定时刷新按条目全表扫会拿假 id 打 TMDB、匹配内核与订阅按 `(kind,tmdb_id)` 比对、前端拼 themoviedb 链接、`cleanup_orphan_items` 等都要加守卫。与 library.md 决策 4、metadata.md「一部片一份档案」同一精神相悖 |
| B `media_item` 泛化 | `tmdb_id` 可空 + `source` 列 | 触面最大：所有读 `tmdb_id` 的地方都要判空，为一类**没有身份**的内容改造身份核心 |
| **C 文件寻址（采用）** | 其他库的文件 `media_item_id` 恒 NULL；展示、播放、进度、Jellyfin 以 `library_file.id` 为键各加一条通道 | 边界干净：影视链路零改动、新链路不带任何 TMDB 概念 |

C 引入第二种播放单元。为避免每个消费方各自 if/else，**播放单元在领域层
统一为一个联合类型，观看状态统一在一张表**（用户决策：不拆表）：

```
PlayUnit = ItemUnit(media_item_id, season, episode)   # 影视：现状不变
         | FileUnit(library_file_id)                   # 其他库：文件即单元
```

`movieclaw_playback/state.py` 的读写、`playback_recent`、Jellyfin UserData、
活动面板全部只认这个联合类型，按变体落到同一张 `playback_state` 的两组列
（3.3 节）。这是本设计唯一允许出现「按类型分派」的地方；其余消费方拿到
的都是已经解析好的单元。

## 3. 数据模型

### 3.1 库类型

- 新建 `LibraryKind`（`movie` / `tv` / `other`），**不扩展 `MediaKind`**
  （后者语义是 TMDB 路径段，`resolve.py` 直接拼 `search/{kind.value}`）。
- `Library` 增两个便捷属性：`scraped`（是否影视库）、`media_kind`
  （影视库返回 `MediaKind`，其他库抛中文错误）。现有 `MediaKind(library.kind)`
  调用点改读 `library.media_kind`，从「第三个值崩溃」变成「进不到这里」。
- 只加一个通用值 `other`（展示名「其他」），类别由库名承担（"家庭录像"
  "课程"各建一库），与「动漫库用户自建」同一模式。
- `is_default` 对 other 保持仓库层不变量（首库自动默认），消费方是手动
  下载/监听导入选库时的缺省（4.5 节）。

### 3.2 `library_file` 加三列（可空，影视行恒 NULL）

| 列 | 说明 |
|---|---|
| `title` | 展示标题：NFO `<title>` → 文件名主干。列表/排序/搜索都靠它 |
| `recorded_at` | 拍摄/录制时间：NFO `premiered`/`aired`/`dateadded` → ffprobe `format.tags.creation_time`（手机拍摄都写）→ 文件 mtime。相册排序轴 |
| `thumb_file` | 缩略图资产相对路径（`data/metadata/thumbs/{file_id}.jpg`）；NULL=未生成/生成失败（下次扫描重试） |

简介等其余 NFO 字段不落库，详情时按需读文件（与影视详情页 NFO 层同款）。
other 库的行 `media_item_id` / `unidentified_*` / `identity_source` 恒 NULL——
「待识别」这个概念对它不存在，见 4.1 的统计与清单排除。

### 3.3 `playback_state` 统一为多态单元（一张表）

现状唯一约束 `(member_id, media_item_id, season, episode)`，`media_item_id`
NOT NULL。改为：

```
media_item_id      可空 FK media_item CASCADE
season_number / episode_number   不变（FileUnit 行恒 0）
+ library_file_id  可空 FK library_file CASCADE
CHECK ((media_item_id IS NULL) <> (library_file_id IS NULL))     -- 恰有其一
UNIQUE (member_id, media_item_id, season_number, episode_number)
        WHERE media_item_id IS NOT NULL                          -- 部分唯一索引
UNIQUE (member_id, library_file_id) WHERE library_file_id IS NOT NULL
```

- 用**部分唯一索引**而不是把可空列塞进普通 UNIQUE：SQLite 视 NULL 互不
  相等，可空列进唯一约束会插出无限行（模型注释已把这个坑写明）。仓库已有
  `sqlite_where` 部分索引与 `batch_alter_table` 先例，迁移可行；
- 迁移要重建表（SQLite 改 NOT NULL 只能 batch 重建）。按发布规范第 3 条，
  迁移单向、回退靠更新前备份，可接受；
- 文件删除（回收站清理）级联删掉进度，对无外部身份的内容是正确语义；
  改名/移动由扫描的指纹归并保住 `library_file.id`，进度自然延续。

### 3.4 不建目录表

平铺展示（用户决策），目录不建模；`file_path` 只作为文件事实展示。

## 4. 机制

### 4.1 扫描：同一台机器，四处分叉

复用 `scan.py` 的遍历/探测/落账/归并/对账/统计/监听全套，other 库在
`_ingest_file` 里走 `_local_video_identity(file)`：

```
sidecar NFO（<主干>.nfo，根元素 movie / video / musicvideo / episodedetails 任一）
  → title / plot / premiered|aired|dateadded / runtime
无 NFO → title = 文件名主干（不跑影视 NER 清洗，那套规则会把 "2019.10.01 国庆" 切碎）
recorded_at 按 3.2 的三级回落
```

必须关掉的影视规则（会误伤家庭视频的）：

- **extras 过滤**：`_IGNORE_MARKERS` 的 `sample`、`_EXTRAS_KEYWORDS` 的
  「花絮/预告片」子串匹配——`婚礼花絮.mp4` 会被当作影视花絮直接跳过；
  `_IGNORE_DIRS` 里的中文花絮目录名同理。other 库只保留隐藏目录与 `@eaDir`
  类系统目录的忽略；
- `_kind_conflict`、路径 `[tmdbid=N]`、TMDB 名称收敛、身份复核
  （`_review_due`）、条目目录 `movie.nfo`/`tvshow.nfo` 查找：整段不进；
- 收尾 ASSETS 阶段的 `ensure_assets`（刮削图片/NFO 镜像）不进；换成
  **缩略图阶段**（4.3）。**other 库永不写 NFO**。

保持一致的收尾钩子：AI 字幕自动生成 `queue_after_scan`、外挂字幕发现、
探测回填、统计快照——照常。

统计快照按类型算：`stats_unidentified_count` 现口径是
`media_item_id IS NULL AND ignored_at IS NULL`，对 other 库会把**每个文件**
都算成待识别，改为 other 库恒 0；`stats_item_count` 对 other 库取在位文件
数。全局待识别清单（`list_unidentified(library_id=None)`）与身份复核清单
排除 other 库。

`.strm`、原盘目录、改名归并、缺失标记、回收站：类型无关，照常。
探测层 `MediaSpec` 加一个可选 `creation_time`（读 `format.tags`），影视
行不消费。

### 4.2 展示：平铺网格 + 文件详情页

```
GET /libraries/{id}/videos?sort=recorded_at|title|added_at&offset&limit
→ [{file_id, title, recorded_at, duration_seconds, resolution, size_bytes,
    thumb_url, missing, watch: {position_ms, played}}]
```

网页端 `library/[id]` 对 other 库渲染**视频网格**而非海报墙：卡片 16:9
（缩略图 + 时长角标 + 观看进度条），默认按 `recorded_at` 倒序，可切按名/
按入库时间；复用海报墙的分页与索引条机制（索引条按标题拼音）。

点卡片进**文件详情页** `library/[id]/file/[fileId]`——不直接播放，因为
文件级原生能力（回收、AI 字幕、轨道信息、外挂字幕）都长在详情页的
FileSection 上，保持一致就得有这个页面。页面结构是影视详情页的子集：
缩略图沉浸背景、标题、日期、事实芯片（分辨率/HDR/时长/大小）、
`MediaTrackRows`（音轨/字幕）、播放键、NFO 简介（若有）、FileSection
（路径、回收、字幕操作）。没有海报/演职员/季集区。

**没有海报怎么办**——三级回落，全部在网格与详情页共用：

1. sidecar 图（Kodi 惯例 `<主干>-thumb.jpg` / `<主干>.jpg`）；
2. ffmpeg 抓帧缩略图（4.3）；
3. 兜底：中性占位（图标 + 标题 + 时长 + 日期）。

库卡片封面：现有「氛围光货架」拼贴只取 `media_metadata.poster_file`，other
库改用 4 张最新缩略图做 16:9 变体；没有缩略图时用纯色卡。媒体库首页汇总
「N 部电影 · M 部剧集」补「K 个视频」。库内搜索补按 `title` 搜文件。

### 4.3 缩略图（P1，用户决策：体验分水岭）

扫描收尾新增 THUMBS 阶段（替代影视库的 ASSETS 阶段）：对 `thumb_file`
为 NULL 且已探测成功的在位文件，`ffmpeg -ss <10% 时长> -frames:v 1` 抓一帧
缩到 480px 宽，写 `data/metadata/thumbs/{file_id}.jpg`。

- 优先级：sidecar 图存在则直接登记为 `thumb_file`（不抓帧）；
- 限并发（复用 ASSETS 的 3 路队列）、strm 跳过、探测失败跳过、抓帧失败
  只记日志（下次扫描重试）；
- 与 trickplay 同一类 IO 顾虑（云盘挂载唤醒休眠盘）：只对新文件做，
  且一次 seek 一帧，不通读容器；
- 服务路由复用 `/images/assets/{path}`（同 metadata 资产，长缓存头）；
- 库级开关 `generate_thumbnails`（默认开）。

### 4.4 播放与进度

- 地址 `/play/f/{file_id}`，与现有 `/play/{media_item_id}[/sXXeYY]` 并列。
  web-player.md 选 `media_item_id` 是因为它跨重扫稳定；文件行 id 经指纹
  归并也跨改名稳定，删了再放回会换 id——对没有外部身份的内容可接受；
- 起播链路照旧：单元从地址可算出，播放器立即挂载、开会话（`file_id`
  分支已存在），条目信息晚到再补——新增 `GET /playback/files/{file_id}`
  返回 `{title, library_id, thumb_url, recorded_at, duration}`；
- 上一个/下一个 = 同库按当前排序的相邻文件（相册连播），排序键随
  网格页传入；
- 进度、续播、已看、收藏、轨道记忆：`POST /playback/progress`、会话接口、
  `/playback/resume` 接受 `file_id`，领域层按 `FileUnit` 落 3.3 的表；
- **最近观看**行按 `FileUnit` 补一路（标题/缩略图/进度，无剧集字段），
  聚合键用 `file_id`；跳转到文件详情页。

### 4.5 下载与导入目标：原样落库

用户决策：other 库可作为手动下载与监听导入的目标。语义定义为
**「原样落库」**——不识别、不改名、不建条目：

- **手动下载选库**：目标为 other 库时 save_path = `{主根}`（种子自带的
  目录结构原样落下），下载完成由实时监控/扫描按文件入账；
- **监听导入规则「指定库」**：目标为 other 库时整理器退化为原样转移
  （硬链/复制整个下载条目到 `{主根}/`，保留原名），跳过识别与命名，
  `ingest_entry` 台账照记（幂等与退避不变）；
- **不参与**：订阅（订阅对象是 TMDB 条目）、自动路由（按影视类型与
  收藏范围）、自定义目录规则的识别改名。选库弹窗里 other 库照常列出，
  自动路由的 kind 仍只有 movie/tv。

这与 library.md「库根里有什么，库就是什么」一致：other 库的整理规则就是
没有规则。

### 4.6 Jellyfin 兼容（P1 同期，用户决策：外部播放器高优先级）

- other 库 → `CollectionType: "homevideos"`；子条目 `Type: "Video"`
  （`MediaType: "Video"`、`IsFolder: false`），**平铺**——Infuse 等对
  homevideos 库直接列视频；
- GUID 新增 0x08 `VIDEO(library_file_id)`；`_entries_for_parent` 的 LIBRARY
  分支对 other 库返回 Video 列表（`Recursive` 语义天然成立，无子层级）；
- `/Items/{id}` 加 VIDEO 分支；PlaybackInfo/stream/Download/字幕接口的
  `_files_for_ref` 对 VIDEO 直接取该文件——MediaSource 构造本就是文件级的
  （`media_source_dto`），复用；
- playstate 的 `_resolve_units` 对 VIDEO 返回 `FileUnit`，UserData 读统一表；
- images：VIDEO 的 `Primary` = `thumb_file`；库封面走 4.2 的变体；
- `/Items/Counts`（other 库计入 `MovieCount` 之外的 `ItemCount`）、Latest
  （按 `created_at` 出 Video 条目）、Resume（`FileUnit` 行）、NextUp
  （不涉及）、搜索（按 `title`）逐一补上；
- 排序：`SortName` 用 `title`、`DateCreated` 用 `created_at`、
  `PremiereDate` 用 `recorded_at`。

### 4.7 必须关掉的身份级入口（守卫清单）

- 订阅目标库与自动路由：`resolve_for_subscription`、`route()`、
  import-watch 的自动路由 kind 只列 movie/tv；
- 整理（命名模板）、认领/重新识别/身份复核面板、刮削设置 tab、元数据
  刷新、收藏范围（match_rules）：other 库隐藏或 400 中文拒绝；
- 转移：仅同类型（other↔other），不改名；
- 创建库弹窗类型选项加「其他」，类型创建后不可改（现状约定）。

## 5. NFO 兼容的边界

读：sidecar `<主干>.nfo`，根元素 `movie` / `video` / `musicvideo` /
`episodedetails` 任一，取 `title`、`plot`、`premiered|aired|dateadded`、
`runtime`、`genre`/`tag`；`tmdbid`/`uniqueid` **忽略**（有也不建身份）。

写：**永不**。影视库写 NFO 的价值是把 tmdb id 交给下游；other 库没有
任何我们比用户更清楚的信息，写出只会污染用户目录。

> 待核实：Jellyfin 对 homevideos 库中 `Video` 条目的 NFO 读写具体走哪个
> 根元素（记忆中沿用 `<movie>` 解析器），实施前对照源码确认一次。

## 6. 分期实施

三期同一个发布周期内完成，顺序按依赖排：

| 期 | 内容 | 验证 |
|---|---|---|
| **一 入账 + 展示 + 播放 + 缩略图** | `LibraryKind` + 守卫清单；扫描分叉（标题/时间/NFO、关 extras 过滤、统计口径、清单排除）；三列迁移；`playback_state` 多态迁移；视频网格接口与页面；文件详情页；缩略图阶段；`/play/f/{id}` + `GET /playback/files/{id}`；进度/续播/最近观看按 `FileUnit` 补路 | e2e：建 other 库 → 放带 NFO 与不带 NFO 的文件 → 扫描零待识别、标题/时间正确、`婚礼花絮.mp4` 不被跳过、缩略图落盘 → 网格 → 详情 → 播放 → 进度落统一表 → 改名后进度仍在；影视库全套既有用例零改动；`playback_state` 迁移升降与存量数据不丢 |
| **二 Jellyfin** | homevideos 视图、Video 条目、播放/进度/图片/搜索/Counts/Latest/Resume 分支 | 协议单测 + Infuse 真机：列出视频、直连播放、进度回传、缩略图显示 |
| **三 下载与导入目标** | 手动下载选库含 other；监听导入「指定库」原样转移；订阅/自动路由维持排除 | e2e：下载到 other 库原名落盘 → 监控入账；监听导入原样硬链 → 台账；订阅弹窗不出现 other 库 |

一期独立可发版；二、三期互不依赖。

## 7. 风险与开口

1. **抓帧 IO**：只在扫描收尾对新文件做、限并发、strm 跳过、库级开关；
2. **第二种播放单元**的扩散：收敛在 `state.py` 的联合类型与
   `playback_recent` 一处，其余消费方按变体分派，不允许各自拼键；
3. **`playback_state` 表重建**：迁移里对存量行逐一校验 CHECK 成立
   （存量 `media_item_id` 全非空，天然满足）；升降测试必做；
4. **文件名即标题**的粗糙：不做清洗规则是有意的（家庭视频命名没有规律，
   任何清洗都有误伤），用户想要好名字就放 NFO 或改文件名；
5. **超大其他库**（万级文件）的网格分页与缩略图磁盘占用（480px JPEG
   约 30KB/张，一万个文件 300MB）：可接受，随「清理无引用资产」一并清理；
6. 目录层级：当前不做；若日后要，按 v0 草案的派生目录方案补
   `browse?path=` 与 Jellyfin FOLDER GUID，本设计的接口形态不变。

## 8. 决策记录（用户拍板 2026-09-03）

1. 类型只加一个通用 `other`，不细分家庭录像/音乐视频等；
2. **平铺**展示，不引入目录层级；
3. 缩略图抓帧要做，且是一期内容；
4. 播放状态**不拆表**，统一在 `playback_state`（3.3 的多态方案由此而来）；
5. Jellyfin 与外部播放器高优先级，与一期同周期；
6. other 库**可以**作为下载与导入目标（4.5 定义为原样落库）；
7. AI 字幕等文件级原生能力对 other 库保持一致，不做排除；
8. 命名暂用 `other` / 「其他」。

## 9. 实施前剩余的两个小口子

1. **文件详情页**：本文选择「点卡片进详情页再播放」，理由是回收/AI 字幕/
   轨道等文件级能力都长在详情页上（4.2）。若更想要相册那种点即播放，
   可以把详情收成卡片上的「⋯」菜单，播放键直接放卡片——两种都不影响
   数据与接口，只是前端形态。
2. **原样落库的目录形态**：手动下载到 other 库时，种子是单文件就直接落
   `{主根}/文件`，是目录就落 `{主根}/种子目录/`（4.5）。要不要给一个
   「按下载日期分目录」之类的选项，暂不做，等真实反馈。

## 附录 A：两值假设的高风险点（实施时逐个处理）

- 抛错点：`organize.py:194`、`scan.py:842/3508/3670`、`claim.py:74`、
  `items.py:671`、`ingest.py:1663/3690`、`schemas/library.py:231`
  （`MediaKind(library.kind)`）；`scan.py:237`、`nfo.py:31`、
  `import_watch_config.py:50`（`_KIND_NAMES` 字典索引）；
- 静默按剧集处理：`organize.py:241`、`nfo.py:44/86/140/398`、
  `catalog.py:1460`、`jellyfin/routes/library.py:281/834`、`genres.py:64`；
- 聚合漏掉第三类：`library_repo.py:146`（episode 统计）、
  `jellyfin/routes/library.py:1130-1145`（Counts）、`library-view.tsx:198-203`
  （首页汇总）、`subscription-overview.ts:23-28`；
- 前端：`LIBRARY_KIND_META`（`library-view.tsx:56`）驱动创建弹窗选项；
  `library-detail-view.tsx:848` / `claim-panels.tsx` 的 `movie: boolean`
  把非电影一律当剧集；`library-item-detail-view.tsx:673` 拼 themoviedb 链接；
- CLI：`cli/internal/overlay/download.go:249` 的 kind 硬校验；
  `spec.json` 两份需重新生成。
