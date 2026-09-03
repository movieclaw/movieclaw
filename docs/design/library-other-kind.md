# 媒体库「其他视频」类型：不刮削内容的入账、展示与播放

> 状态：**草案 v0（2026-09-03，待讨论）**。本文是围绕目标对现有代码的
> 评估与方案建议，尚未拍板；第 8 节列出需要用户决定的问题。
> 关联文档：[library.md](library.md)（媒体库架构）、[metadata.md](metadata.md)
> （元数据自足）、[web-player.md](web-player.md)（网页播放器）、
> [jellyfin-compat.md](jellyfin-compat.md)（播放器兼容层）、
> [library-home-recently-watched.md](library-home-recently-watched.md)。

## 0. 目标与定位

用户诉求：媒体库除电影/剧集外增加一类**不走影视识别链**的库——家庭
录像、自录内容、课程视频等。这些视频只有目录结构和文件，多数无法在
TMDB 上找到对应条目，不该触发电影/剧集的识别、刮削与订阅联通；但要
兼容标准协议：视频旁若有 NFO 就读 NFO 作为信息来源，否则用文件名。
入账后要能正常浏览、播放、记进度，并尽量供给第三方播放器。

定位一句话：**这是一类「按目录看、按文件放」的库**，它的内容没有作品
身份，只有文件本体。Emby 的 Home Videos、Jellyfin 的 `homevideos`、
Plex 的 Other Videos 都是同一心智——目录树浏览，条目就是文件。

## 1. 现状评估：整条链路锚在 `media_item` 上

三条探查（类型枚举 / 扫描识别展示链 / 播放与 Jellyfin）得出的核心事实：

1. **`MediaKind` 只有 movie/tv，且取值就是 TMDB 路径段**
   （`movieclaw_media/models.py`）。`Library.kind` 是裸字符串列，但扫描、
   整理、认领、条目列表、入库等约十处入口第一行就是 `MediaKind(library.kind)`，
   第三个值会直接抛 `ValueError`；`nfo.py` / `organize.py` / Jellyfin
   `CollectionType` 等十余处是「非 movie 即 tv」的隐式二分，第三个值会**静默
   按剧集处理**。全仓两值假设约 40 处后端、30 处前端（见附录 A）。
2. **`media_item.tmdb_id` NOT NULL，唯一键 `(kind, tmdb_id)`**，设计文档明确
   「不允许创建无 tmdb_id 的无锚条目」。而海报墙（`items.py` 三条分页查询
   都硬过滤 `media_item_id IS NOT NULL`）、条目详情、`/play/{media_item_id}`
   地址、`playback_state`（`media_item_id` NOT NULL 外键）、最近观看、
   Jellyfin 目录（`item_ids_with_files` 同样过滤 NULL）**全部只认这个锚**。
   今天产品对「没有 TMDB 身份的文件」的唯一答案是**待识别清单**——一个
   待办队列，不是可浏览内容；「这不是独立作品」出口的语义是**隐藏**。
3. **`library_file` 没有任何展示标题字段**。待识别清单的名字是请求时用
   条目目录名/文件名临时拼的。
4. **两处现成的缝**：① 播放决策与取流已经是文件寻址的——
   `PlaybackDecideRequest.file_id`、`/playback/files/{file_id}/stream`
   不碰 `media_item`，只是网页端从未用过；② Jellyfin GUID 已有 0x05
   `MEDIA_SOURCE(library_file_id)` 类型，0x08 起空闲。
5. **扫描器的大部分机制是类型无关的**：目录遍历、ffprobe、台账 upsert、
   改名归并（尺寸+时长指纹）、缺失对账、回收站、实时监控、统计快照——
   只有 `_identify` / `_unit_for` / `_kind_conflict` / 资产阶段四处是影视
   专属。这决定了新类型**不是另写一个扫描器**，而是在这四处分叉。

结论：新类型的难点不在扫描（分叉即可），在**身份与寻址**——展示、播放、
进度、Jellyfin 四条链要么给它一个假的 `media_item`，要么给它一条
文件寻址的并行通道。

## 2. 核心决策：不造假身份，走文件寻址

| 方案 | 做法 | 评价 |
|---|---|---|
| A 合成 `media_item` | 每个文件/目录造一条 `kind=other, tmdb_id=负数/哈希` 的条目，复用全部下游 | 改动最少，但污染身份中枢：定时刷新按条目全表扫会拿假 id 打 TMDB、匹配内核与订阅按 `(kind,tmdb_id)` 比对、前端拼 themoviedb 链接、`cleanup_orphan_items` 等都要加守卫；且「一个条目=一部作品」的语义对目录树内容本就不成立。与 library.md 决策 4「不静默错挂」、metadata.md「一部片一份档案」同一精神相悖 |
| B `media_item` 泛化 | `tmdb_id` 可空 + `source` 列 | 触面最大：所有读 `tmdb_id` 的地方（匹配/订阅/发现页/刮削/刷新）都要判空，为一类**没有身份**的内容改造身份核心，收益不成比例 |
| **C 文件寻址（推荐）** | 新类型的文件 `media_item_id` 恒 NULL；展示、播放、进度、Jellyfin 各加一条以 `library_file.id` 为键的并行通道；浏览按目录树 | 代码量比 A 多，但边界干净：影视链路零改动、新链路不带任何 TMDB 概念；且目录树浏览是这类内容唯一合理的 UX，A 也逃不掉这部分工作 |

推荐 **C**。理由回到第一性：这类内容的「身份」就是它在磁盘上的位置，
把它塞进为作品身份设计的表里，只会让两套语义互相守卫。

代价要明说：`(media_item_id, season, episode)` 是全站播放单元的唯一约定，
C 引入第二种单元 `file_id`。第 4 节把它收敛在领域层一个联合类型上，
避免每个消费方各自 if/else。

## 3. 数据模型

### 3.1 库类型

- 新建 `LibraryKind` 枚举（`movie` / `tv` / `other`），**不扩展 `MediaKind`**
  （后者语义是 TMDB 路径段，`resolve.py` 直接拼 `search/{kind.value}`）。
- `Library` 增两个便捷属性：`scraped`（是否影视库）、`media_kind`
  （影视库返回 `MediaKind`，其他库抛中文错误）。现有 `MediaKind(library.kind)`
  调用点改读 `library.media_kind`，从「第三个值崩溃」变成「进不到这里」。
- 类型只加一个通用值 `other`（展示名「其他视频」），**类别由库名承担**
  （"家庭录像""课程"各建一库），与「动漫库用户自建」同一模式。不为家庭
  视频/音乐视频/讲座各造一个类型：它们的入账与展示逻辑完全相同。
- `is_default` 对 other 无消费方（无订阅/无下载入库），保持仓库层不变量即可。

### 3.2 `library_file` 加两列（可空，影视行恒 NULL）

| 列 | 说明 |
|---|---|
| `title` | 展示标题：NFO `<title>` → 文件名主干。列表/排序/搜索都靠它，必须落库 |
| `recorded_at` | 拍摄/录制时间：NFO `premiered`/`aired`/`dateadded` → ffprobe `format.tags.creation_time`（手机拍摄都写）→ 文件 mtime。家庭录像最自然的排序轴 |

简介等其余 NFO 字段不落库，详情时按需读文件（与影视详情页 NFO 层同款）。
other 库的行 `media_item_id` / `unidentified_*` / `identity_source` 恒 NULL——
「待识别」这个概念对它不存在，见 4.1 的统计与清单排除。

### 3.3 目录：派生，不建表（v1）

目录树由 `file_path` 相对库根的前缀派生：浏览接口一次取该库在位行的少量
标量列（id/路径/标题/时长/时间），在 Python 里按当前层聚合。家庭库规模
（千级文件）下是毫秒级，不需要 `library_folder` 表；目录本身没有元数据。

开口：若后续要目录级 NFO/封面/自定义排序，再建 `library_folder`
（扫描收尾幂等重建），届时目录 id 替换掉本文的路径键，对外接口不变。

### 3.4 播放状态：新表 `file_playback_state`

`playback_state` 的唯一约束 `(member_id, media_item_id, season, episode)`
不能让 `media_item_id` 可空——SQLite 视 NULL 互不相等，可空列进唯一约束会
插出无限行（该模型注释已把这个坑写明）。把 NOT NULL 改可空要重建表，与
「迁移向前兼容、回退靠备份」的硬约束相性也差。

新表 `(member_id, library_file_id UNIQUE 组合, position_ms, played, play_count,
last_played_at, is_favorite, audio_track, subtitle_track)`，外键
`library_file` CASCADE：文件删了进度随之消失，对无外部身份的内容是正确
语义；改名/移动由扫描的指纹归并保住行 id，进度自然延续。旧版本回退时
多一张不认识的表，零影响。

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

必须关掉的影视规则（实测会误伤家庭视频的）：

- **extras 过滤**：`_IGNORE_MARKERS` 的 `sample`、`_EXTRAS_KEYWORDS` 的
  「花絮/预告片」子串匹配——`婚礼花絮.mp4` 会被当作影视花絮直接跳过。
  other 库只保留隐藏目录与 `@eaDir` 类系统目录的忽略；
- `_kind_conflict`、路径 `[tmdbid=N]`、TMDB 名称收敛、身份复核
  （`_review_due`）、条目目录 `movie.nfo`/`tvshow.nfo` 查找：整段不进；
- 收尾 ASSETS 阶段（`ensure_assets` 刮削图片/NFO 镜像）：不进。**other 库
  永不写 NFO**——我们没有比用户更权威的信息；
- AI 字幕自动生成 `queue_after_scan`：开关默认关且可限定库集合，暂不
  特殊处理，文档里提醒即可（待定，见第 8 节）。

统计快照要按类型算：`stats_unidentified_count` 现口径是
`media_item_id IS NULL AND ignored_at IS NULL`，对 other 库会把**每个文件**
都算成待识别；`stats_item_count` 对 other 库取在位文件数。全局待识别清单
（`list_unidentified(library_id=None)`）与身份复核清单排除 other 库。

`.strm`、原盘目录、改名归并、缺失标记、回收站：类型无关，照常。
探测层 `MediaSpec` 加一个可选 `creation_time`（读 `format.tags`），影视
行不消费。

### 4.2 浏览接口与网页展示

```
GET /libraries/{id}/browse?path=<相对目录>&sort=recorded_at|title
→ { breadcrumb, folders: [{name, rel_path, file_count, latest_at, thumb_url}],
    videos:  [{file_id, title, recorded_at, duration_seconds, resolution,
               size_bytes, thumb_url, watch: {position_ms, played}}] }
```

网页端 `library/[id]` 对 other 库渲染**目录浏览器**而非海报墙：面包屑 +
目录卡 + 视频网格，默认按 `recorded_at` 倒序（相册心智），可切按名。
点视频直接进播放（相册心智，不经详情页）；卡片菜单给「信息」（文件事实、
音轨字幕、路径）与「移除」（复用回收站，`recycle.py` 类型无关）。

**没有海报怎么展示**——三级：

1. sidecar 图（Kodi 惯例 `<主干>-thumb.jpg` / `<主干>.jpg` / 目录 `folder.jpg`）；
2. **ffmpeg 抓帧缩略图**（P2）：扫描收尾对新文件抓一帧（10% 处、限并发、
   strm 与探测失败的跳过），存 `data/metadata/thumbs/{file_id}.jpg`。这是
   体验的分水岭——有帧图的网格像相册，没有的是一堆文件名。播放页的
   trickplay 已有 ffmpeg 参数构造可复用；
3. 兜底：中性占位（图标 + 标题 + 时长 + 日期），卡片 16:9 而不是 2:3。

库卡片封面：现有「氛围光货架」拼贴只取 `media_metadata.poster_file`，other
库会空；用 4 张最新缩略图做同构的 16:9 变体，没有缩略图时用纯色卡。
媒体库首页汇总「N 部电影 · M 部剧集」补「K 个视频」。库内搜索
（现只搜已识别条目）补按 `title` 搜文件。

### 4.3 播放与进度

- 地址 `/play/f/{file_id}`，与现有 `/play/{media_item_id}[/sXXeYY]` 并列。
  web-player.md 选 `media_item_id` 是因为它跨重扫稳定；文件行 id 经指纹
  归并也跨改名稳定，删了再放回会换 id——对没有外部身份的内容这是可接受的
  语义，写进文档；
- 起播链路照旧：单元从地址可算出，播放器立即挂载、开会话（`file_id`
  分支已存在），条目信息晚到再补——新增 `GET /playback/files/{file_id}`
  返回 `{title, library_id, thumb_url, recorded_at, duration}`；
- 上一个/下一个 = 同目录按当前排序的相邻文件（相册连播）；
- 进度：领域层 `movieclaw_playback/state.py` 的 `Unit` 从三元组扩成联合
  `ItemUnit | FileRef`，读写按变体落两张表；`POST /playback/progress` 与会话
  接口接受 `file_id`；**最近观看**行按 `FileRef` 补一路（标题/缩略图/进度，
  无剧集字段），聚合键用 `file_id`。

### 4.4 Jellyfin 兼容（P3）

- other 库 → `CollectionType: "homevideos"`；条目 `Type: "Folder"`
  （`IsFolder: true`）与 `Type: "Video"`（`MediaType: "Video"`）——Infuse 等
  对 homevideos 库就是目录浏览；
- GUID 新增 0x08 `VIDEO(file_id)`、0x09 `FOLDER`（库 id + 相对路径哈希，
  解码用每库一份的路径哈希索引，随 `stats_refreshed_at` 失效）；
- `_entries_for_parent` 加 FOLDER 分支；PlaybackInfo/stream/Download/字幕/
  playstate/images 各加 VIDEO 分支——MediaSource 构造本就是文件级的
  （`media_source_dto`），复用；Primary 图 = 缩略图；UserData 读新表；
- `/Items/Counts`、Latest、Resume、NextUp 按类型补或排除。
- **P1 期间 other 库不出现在 `UserViews`**，避免老客户端拿到不认识的库崩掉。

### 4.5 必须关掉的影视入口（守卫清单）

- 订阅/手动下载/监听导入的目标库：`resolve_for_subscription`、路由、
  import-watch 规则的 kind 只列 movie/tv，other 库不可选；
- 整理（命名模板）、转移（仅同类型，other↔other 允许但不改名）、认领/
  重新识别/身份复核面板、刮削设置 tab、元数据刷新：other 库隐藏或 400 中文
  拒绝；
- 创建库弹窗类型选项加「其他视频」；收藏范围（match_rules）对 other 不显示。

## 5. NFO 兼容的边界

读：sidecar `<主干>.nfo`，根元素 `movie` / `video` / `musicvideo` /
`episodedetails` 任一，取 `title`、`plot`、`premiered|aired|dateadded`、
`runtime`、`genre`/`tag`；`tmdbid`/`uniqueid` **忽略**（有也不建身份）。
目录级 `movie.nfo` 不作用于目录下全部文件（v1 不做目录元数据）。

写：**永不**。影视库写 NFO 的价值是把 tmdb id 交给下游；other 库没有
任何我们比用户更清楚的信息，写出只会污染用户目录。

> 待核实：Jellyfin 对 homevideos 库中 `Video` 条目的 NFO 读写具体走哪个
> 根元素（记忆中沿用 `<movie>` 解析器），实施前对照源码确认一次。

## 6. 分期

| 期 | 内容 | 验证 |
|---|---|---|
| **P1 入账 + 浏览 + 播放** | `LibraryKind` + 守卫清单；扫描分叉（标题/时间/NFO、关 extras 过滤、统计口径、清单排除）；两列迁移；browse 接口 + 目录浏览器；`/play/f/{id}` + `GET /playback/files/{id}`；`file_playback_state` + 进度/续播/最近观看 | e2e：建 other 库 → 放带 NFO 与不带 NFO 的目录树 → 扫描零待识别、标题/时间正确、`婚礼花絮.mp4` 不被跳过 → 浏览 → 播放 → 进度落新表 → 改名后进度仍在；影视库全套既有用例零改动 |
| **P2 缩略图** | sidecar 图 → ffmpeg 抓帧 → 占位；库封面变体；搜索 | 抓帧只对新文件、strm 跳过、失败不阻断扫描 |
| **P3 Jellyfin** | homevideos 视图、Folder/Video 条目、播放/进度/图片分支 | Infuse 真机：目录浏览、直连播放、进度回传 |

P1 独立可发版；P2/P3 互不依赖。

## 7. 风险与开口

1. **抓帧 IO**：与 trickplay 同一类问题（云盘挂载唤醒休眠盘）。只在扫描
   收尾对新文件做、限并发、strm 跳过；库级开关（默认开）；
2. **第二种播放单元**的扩散：收敛在 `state.py` 的联合类型与 `playback_recent`
   一处，其余消费方（Jellyfin UserData、活动面板）按变体分派，不允许各自
   拼键；
3. **文件名即标题**的粗糙：不做清洗规则是有意的（家庭视频命名没有规律，
   任何清洗都有误伤），用户想要好名字就放 NFO 或改文件名；
4. 目录派生不建表：目录级元数据、跨库共享目录等需求出现时升级为
   `library_folder`，接口形态不变。

## 8. 需要拍板的问题

1. **类型粒度**：一个通用 `other`（库名当类别）够不够？还是要区分家庭录像/
   音乐视频等多个类型？（推荐前者）
2. **浏览形态**：目录树（推荐）还是平铺网格？点视频直接播放还是先进详情页？
3. **抓帧缩略图**：接受扫描期的 ffmpeg 成本吗？默认开还是默认关？
4. **播放状态新表**：接受 `file_playback_state` 独立表（而不是改造
   `playback_state`）？
5. **Jellyfin 优先级**：P3 放后面可以吗，还是 P1 就要 Infuse 能看？
6. **other 库作为下载/导入目标**：v1 明确不支持（无身份无命名），可以吗？
7. **AI 字幕自动生成**对 other 库是否默认排除？
8. **命名**：库类型内部值 `other`、展示「其他视频」，有更贴切的叫法吗
   （如「视频库」「通用视频」）？

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
