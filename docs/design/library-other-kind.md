# 媒体库「其他」类型：不刮削内容的入账、展示与播放

> 状态：**v2.1 最终版（2026-09-03）——已拍板，待实施**。仅一项待定（第 9 节）。
> 演进：v0 文件寻址草案 → v1 决策落地 → v1.1 Jellyfin 源码考察（1.5 节保留）
> → v1.2 能力档案 → v2 身份来源成为 `media_item` 的维度 → v2.1 整合全部
> 评审结论（形态 × 来源档案、未识别文件临时身份、类型切换前瞻），并清理
> 各轮之间的矛盾表述。
> 关联文档：[library.md](library.md)、[metadata.md](metadata.md)、
> [web-player.md](web-player.md)、[jellyfin-compat.md](jellyfin-compat.md)、
> [library-home-recently-watched.md](library-home-recently-watched.md)、
> [library-file-recycle.md](library-file-recycle.md)、
> [member-management.md](member-management.md)。

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
   待办队列，不是可浏览内容（v2 第 11 节改为给临时本地身份，可见可播）。
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

结论：难点不在扫描，在**身份**——展示、播放、进度、Jellyfin 四条链全都
吊在 `media_item` 上。v1.x 的答案是绕开它另开一条文件寻址通道；v2 起的答案
是把它泛化到能容纳没有 TMDB 身份的内容（第 2 节）。

## 1.5 参考考察：Jellyfin 对非影视视频的处理（2026-09-03）

对照 `jellyfin/jellyfin` 源码（HEAD `1ccec11`，12.0 dev）逐条核实 Jellyfin 是
如何支撑 `homevideos`（Home Videos & Photos）这类库的。结论先行：**本文的
核心决策与 Jellyfin 的做法同构**，另外捞出七处可以直接抄的实现细节与三处
需要对齐的协议行为。

### 1.5.1 模型：条目就是文件，身份就是条目 id

- 库类型枚举 `CollectionType`（`Jellyfin.Data/Enums/CollectionType.cs`）：
  `movies / tvshows / music / musicvideos / trailers / homevideos / boxsets /
  books / photos / livetv / playlists / folders / unknown`。家庭视频对应
  `homevideos`；`folders` 是"什么都不猜"的通用目录库。
- **视频文件 → 裸 `Video` 类**，不是 `Movie`：`MovieResolver.Resolve`
  对 `homevideos`/`photos` 走 `ResolveVideo<Video>(args, false)`
  （`MovieResolver.cs:163-165`），`musicvideos` 走 `MusicVideo`，`movies`
  才是 `Movie`。第二个参数 `parseName=false` 意味着**文件名主干原样当
  Name，不做年份剥离与发布组清洗**（`Emby.Naming/Video/VideoResolver.cs:83-93`：
  `name = GetFileNameWithoutExtension(path)`，只有 `parseName` 为真才跑
  `CleanDateTime` / `CleanString`）。与本文 4.1「不跑影视 NER 清洗」一致。
- **观看状态键 = 条目 id**：`BaseItem.GetUserDataKeys()` 默认只返回
  `Id.ToString()`（`BaseItem.cs:1704-1717`）；`Video.GetUserDataKeys` 只在
  条目是 extra 时才插入 TMDB/IMDb 键（`Video.cs:293-310`）。`Movie` 才有
  provider id 键。也就是说 Jellyfin 的家庭视频**没有任何外部身份**，进度
  与已看直接钉在文件条目上——正是本文第 2 节的方案 C。而 Jellyfin 的条目
  id 是路径哈希，改名即换 id、进度丢失；我们靠尺寸+时长指纹归并保住
  `library_file.id`，这一点比它强。
- `Video.SupportsPlayedStatus` / `SupportsPositionTicksResume` 均为真
  （`Video.cs:54, 63`）：家庭视频进「继续观看」，不进 NextUp（TV 专属）。
- **无远程刮削**：`Video` 类型没有任何远程 metadata provider（TMDB 插件只
  注册在 `Movie`/`Series`/…上），Screen Grabber 与 Image Extractor 是
  该类型默认开启的两个图片来源（`LibraryController.cs:1067-1082`）。
- **多版本不合并**：`homevideos` 的目录级解析 `ResolveMultipleInternal`
  传 `supportMultiEditions=false`（`MovieResolver.cs:206-208`），且
  `FindMovie` 对 photos/homevideos 集合跳过"整目录=一部片"的折叠
  （`MovieResolver.cs:478-479`）。一个文件一个条目，与本文平铺决策一致。
- 子目录由兜底的 `FolderResolver` 解析成 `Folder` 条目（浏览是层级的）；
  `EnablePhotos` 默认开（`LibraryOptions.cs:29`），图片文件解析成 `Photo`
  条目（`PhotoResolver.cs`），`homevideos` 的代表类型是 `["Video","Photo"]`
  （`LibraryController.cs:1026`）。**本文有意偏离**：不建目录层级、不收
  图片（第 7 节开口）。

### 1.5.2 元数据：NFO 只认 sidecar，日期来自容器标签

- **NFO 读取**：`VideoNfoProvider : BaseVideoNfoProvider<Video>` 与
  `MovieNfoProvider` 共用同一个 `MovieNfoParser`（`BaseVideoNfoProvider.cs:45-56`）。
  文件查找 `MovieNfoSaver.GetMovieSavePaths`（`MovieNfoSaver.cs:47-69`）：
  `movie.nfo` **只对 `Movie` 类型且非混合目录**放行（源码注释："not owned
  videos, which will be itemtype video"），`Video` 只读**视频同名 `.nfo`**
  （原盘目录读 `<目录名>.nfo`）。**解析器不校验根元素名**：
  `BaseNfoParser.Fetch` 直接 `MoveToContent(); Read();` 跳过根元素后遍历
  子节点（`BaseNfoParser.cs:133-147`），`<movie>` / `<video>` / 任何根都行。
  写出时根元素：`MusicVideo` 写 `<musicvideo>`，其余一律 `<movie>`
  （`MovieNfoSaver.cs:72-73`）。本文第 5 节「待核实」就此闭环。
- 读取的标签（`BaseNfoParser.cs:271-600`）：`title/name/localtitle` →
  Name；`sorttitle` → 排序名；`plot/outline`；**`dateadded` → DateCreated
  （入库时间）**；`premiered/aired/releasedate` → PremiereDate 并回填
  ProductionYear（`:556-566`）；`year`；`runtime`；`genre`/`tag`；以及
  `watched/playcount/lastplayed` 会导入观看状态。
- **NFO 写出默认关**：`MovieNfoSaver.IsEnabledFor` 对任何非分集、非 extra
  的 `Video` 都返回真（`MovieNfoSaver.cs:76-88`），但 `IsSaverEnabledByDefault`
  对新库恒 `false`（`LibraryController.cs:1033-1038`）。本文「永不写」比它
  更保守，方向一致。
- **日期来自容器标签**（`ProbeResultNormalizer.cs:164-174`）：
  `ProductionYear ← tags.date`；`PremiereDate ← originaldate → retaildate →
  retail date → retail_date → date_released → date → creation_time`。
  `FFProbeVideoInfo.FetchEmbeddedInfo` 对非 extra 的视频**无条件**把它们
  填进空着的 PremiereDate/ProductionYear（`FFProbeVideoInfo.cs:436-449, 486-489`）。
  这与本文 3.2 `recorded_at` 的三级回落完全同构，并补了一条：`date` 标签
  排在 `creation_time` 前。
- **容器 `title` 标签默认不用**：`EnableEmbeddedTitles` 默认关
  （`LibraryOptions.cs:68`；`FFProbeVideoInfo.cs:471-476`）——嵌入标题常是
  编码器或"Untitled"之类的垃圾，Jellyfin 把它做成 opt-in。我们 v1 不读，
  留作开口。
- **入库时间**：`ResolverHelper.SetDateCreated` 按 `UseFileCreationTimeForDateAdded`
  （默认真，`MetadataConfiguration.cs:9`）取文件创建时间，否则取当前时间
  （`ResolverHelper.cs:131-153`）。我们的 `created_at` 是入账时间，语义同后者。

### 1.5.3 图片：本地文件名规范 + 抓帧

`LocalImageProvider`（`MediaBrowser.LocalMetadata/Images/LocalImageProvider.cs`）
对混合目录中的 `Video`：

| 图类型 | 认的文件名（`<主干>` = 视频文件名主干） |
|---|---|
| Primary | `<主干>.jpg`（精确同名，`:288-294`）→ `<主干>-poster` / `-folder` / `-cover` / `-default` / `-movie`（`:59, :296-302`） |
| Thumb | `<主干>-landscape` → `<主干>-thumb`（`:246-250`） |
| Backdrop | `<主干>-fanart`（`:314-322`） |

不带前缀的 `poster.jpg`/`folder.jpg` 只在**非混合目录**（一个视频独占一个
目录）才认（`:304-312`）。**修正本文 4.2**：`<主干>-thumb.jpg` 在 Jellyfin
语义里是 Thumb 而非 Primary，我们的 16:9 卡片取用顺序应为
`-thumb`/`-landscape` → `<主干>.jpg`/`-poster` → 抓帧。

**Screen Grabber**（`MediaBrowser.Providers/MediaInfo/VideoImageProvider.cs`）：

- 只出 Primary；`Order = 100` 排在网络图源之后（`:36-41`）；
- 条件：`IsFileProtocol`（**strm 不抓**）、非快捷方式、非占位、非 DVD、
  探测到视频流（`:47-58, :116-128`）；
- 位置：已知时长取 **10%** 处，否则第 10 秒（`:72-74`）；
- ffmpeg（`MediaEncoder.cs:674-800`）：`-ss <t> -i in -threads N -v quiet
  -vframes 1 -vf <滤镜> -f image2 out.jpg`。滤镜链：隔行则 `bwdif` 反交错 →
  `scale=round(iw*sar/2)*2:round(ih/2)*2`（只修正 SAR 与奇数尺寸，**不缩小**，
  缩放在出图时按请求做）→ **`thumbnail=n=24`**（24 帧里挑最有代表性的一帧，
  避开黑场/转场；性能取舍配置开启时省掉）→ **HDR 软件 tonemap**
  （`tonemapx` 或 `zscale+tonemap=hable`，杜比视界只在 `tonemapx` 可用时做）。
  mpegts 容器加 `-skip_frame nokey`（无法按关键帧 seek）。全局信号量限并发；
- 图片长宽比：`DtoService.GetPrimaryImageAspectRatio` 读**真实图片尺寸**
  （`DtoService.cs:1745-1774`），`Video` 的默认值是 0（`BaseItem.cs:834`），
  `Movie` 才是 2/3。客户端据此把家庭视频排成 16:9 卡片。

**Image Extractor**（`EmbeddedImageProvider`）排在 Screen Grabber 之前：
容器里带 `attached_pic` 封面流时直接取封面。手机录像不会有，但值得作为
零成本的前置一档（ffprobe 已列出全部流）。

### 1.5.4 忽略规则：通用适用，但只做精确/后缀匹配

- 全局 glob（`IgnorePatterns.cs`）：`sample.*`、`*.sample.*`、`sample/`
  目录、`metadata/`、`extrafanart/`、`.actors/`、`subs/`、`@eaDir` 类系统目录、
  `*.trickplay`——**对所有库类型生效**，`homevideos` 不例外；
- extras 规则（`NamingOptions.cs:497-704`）在 `VideoResolver.Resolve` 里
  **无视库类型**计算，`MovieResolver` 对 `ExtraType != null` 的条目一律丢弃
  （`:176-179`）。但匹配语义是**精确或后缀**：文件名规则要求主干**整体等于**
  `trailer`/`sample`（`ExtraRuleResolver.cs:48`），后缀规则是 `-trailer`
  `-sample` `-scene` `-clip` `-interview` `-behindthescenes` `-deleted`
  `-featurette` `-short` `-extra` `-other` 等；目录名规则 `trailers` `extras`
  `other` `clips` `shorts` `samples` … 对**库根下的一级目录不生效**
  （`CoreResolutionIgnoreRule.cs:37-41`，"Don't ignore top level folders"），
  更深层才忽略。**没有任何子串关键词匹配**。
- 对照我们：影视库的 `_IGNORE_MARKERS`（`sample` 子串）与 `_EXTRAS_KEYWORDS`
  （「花絮/预告片」子串）比 Jellyfin 激进得多，用在家庭视频上必伤
  （`婚礼花絮.mp4`、`采样测试.mp4`）。**修正本文 4.1**：其他库采用 Jellyfin
  口径——隐藏目录 + 系统目录 + 主干精确等于 `sample`，不做任何子串规则，
  也不做 extras 目录名规则（我们对其他库没有 extras 概念，`clips/`、`other/`
  这种名字恰恰是家庭视频的常见分类目录）。

### 1.5.5 协议面：`homevideos` 视图的接口行为

- `UserViews`：`homevideos` 属于 `_originalFolderViewTypes`（`UserView.cs:31-39`），
  按原样目录视图输出，`CollectionType: "homevideos"`；
- `/Items?parentId=<库>` **不传 `includeItemTypes`** 时保持空、**非递归**
  （`ItemsController.cs:326-333`，只有 boxsets 有特例）→ 返回直接子项
  （Folder + Video）；传了类型则默认递归（`:335-340`）。我们平铺，两种请求
  都直接返回全部 Video，协议合法（一个所有文件都在根下的 homevideos 库
  就长这样）；
- `Latest`：`homevideos` 的 `MediaTypes=[Photo, Video]`、`IsFolder=false`、
  按 `DateCreated` 倒序（`UserViewManager.cs:325-333, 367-380`）；`Video` 的
  `LatestItemsIndexContainer` 为 null（`BaseItem.cs:737`）→ **不聚合**，逐条
  输出。与本文 4.6 一致；
- `/Items/Counts`：`Video` 只计入 `ItemCount`，没有 `VideoCount`
  （`ItemCountService.cs:70-100`）；
- DTO：`Type: "Video"`、`MediaType: "Video"`、`IsFolder: false`、`VideoType`、
  多源时 `MediaSourceCount`（`DtoService.cs:1303-1345`）；有效 `Type` 全集见
  `BaseItemKind`（含 `Video` `Folder` `Photo` `PhotoAlbum` `MusicVideo`）。

### 1.5.6 对本计划的修正与吸收

| # | 结论 | 落到哪 |
|---|---|---|
| 1 | 方案 C（文件即单元、观看状态钉文件、无远程身份、无版本合并、文件名不清洗）与 Jellyfin `Video` 的处理**全部同构**，不再有"我们是不是发明了奇怪东西"的顾虑 | 第 2 节 |
| 2 | NFO：只读 sidecar `<主干>.nfo`，根元素不校验；`premiered/aired` → 拍摄时间，`dateadded` 是入库时间不是拍摄时间 | 3.2 / 4.1 / 第 5 节改写 |
| 3 | `recorded_at` 回落链补 `date` 标签：NFO `premiered/aired` → 容器 `date` → `creation_time` → mtime | 3.2 |
| 4 | 本地图片文件名对齐 `LocalImageProvider`：`-thumb`/`-landscape` 是 Thumb，`<主干>.jpg`/`-poster` 是 Primary，`-fanart` 是背景 | 4.2 |
| 5 | 抓帧照抄：10% 位置、`thumbnail=n=24` 选帧、HDR tonemap（iPhone 录像大量 HLG/杜比视界，不 tonemap 会灰）、反交错、strm 不抓、限并发；`attached_pic` 封面流优先 | 4.3 |
| 6 | 忽略规则改 Jellyfin 口径：无子串匹配，只精确 `sample` + 系统/隐藏目录 | 4.1 |
| 7 | Jellyfin 层 `Video` 条目必须输出 **`PrimaryImageAspectRatio`**（按真实图片尺寸），否则客户端按海报比例裁 16:9 帧图。我们现有 catalog 从未输出该字段，影视条目靠客户端默认 2/3 蒙对了，Video 蒙不对 | 4.6 |
| 8 | `Counts` 只加 `ItemCount`；`Latest` 逐条不聚合；`Resume` 含 Video；不传类型的库级 `/Items` 返回全部 Video | 4.6 |
| 9 | 有意偏离登记：不建 `Folder` 层级、不收 `Photo`。实施时写进 jellyfin-compat.md 的偏离清单 | 4.6 / 第 7 节 |
| 10 | 开口：容器 `title` 标签作标题（Jellyfin opt-in）、NFO 的 `watched/playcount` 导入、图片收录做成"相册" | 第 7 节 |

## 2. 核心决策：身份来源成为 `media_item` 的一个维度

### 2.1 重新核实：`media_item` 到底被 TMDB 钉死在哪

v1.x 把 `media_item` 当成不可触碰的 TMDB 专属内核，理由是 subscription.md 的
「不允许创建无 tmdb_id 的无锚条目」。逐条核实后，这个判断过重了：

| 约束 | 现状 | 能不能改 |
|---|---|---|
| `tmdb_id NOT NULL`、`UNIQUE(kind, tmdb_id)` | `models/media_item.py:36, 45` | 能。`media_item` 已有 batch 重建先例（迁移 `d9f4b1c73e85` 加 `scrape_library_id` 外键就是整表重建），入向外键 9 处按表名引用不受影响 |
| `ensure_media_item(kind, tmdb_id)` | 11 个调用点 | 全部在 TMDB 路径上（订阅、发现页、扫描识别、认领、入库）。本地条目走另一个构造函数，不碰它们 |
| `item.tmdb_id` 引用 258 处 / 38 个文件 | 集中在 `scan.py`(31)、`resolve.py`(29)、`movieclaw_media/library.py`(26)、`media_library.py`(17)、`nfo.py`(16)、`downloaders.py`(13)… | 这些模块本身就是 TMDB 识别/刮削/订阅链，本地条目**不会进入**。真正会"全表扫到"本地条目的只有 5 处：定时刷新 `media_refresh.py:46`、`scrape_media_item`/`ensure_assets`、NFO 写出、Jellyfin `ProviderIds`、前端 TMDB 链接与 `LibraryItemView.tmdb_id: int` |
| 「无锚条目」禁令的本意 | subscription.md 1.1/1.3：匹配内核依赖 TMDB 的别名/季集/播出日期 | 禁的是**订阅与匹配**拿到没有 TMDB 数据的条目。本地条目永不参与订阅与匹配（能力档案 `subscribable=False`、`source` 守卫），禁令的目的完好 |
| 类型门禁 | CI 只跑 ruff + pytest + 前端 typecheck，后端无 mypy | 后端把 `tmdb_id` 改可空不会掀起类型雪崩；前端 `tmdb_id: number \| null` 会被 typecheck 逼着在 22 个文件里判空——这是好事 |

"TMDB 专属"不是结构性的，是只有一个数据源时的历史默认。把它做成显式
维度，代价可控。

### 2.2 方案：`source` + `external_id` 泛化锚，本地内容是真条目

```
media_item
  kind         movie / tv / video            内容形态（决定单元结构、Jellyfin 类型、命名/整理能力）
  source       tmdb / local / (future: jav, douban …)   身份来源（决定谁负责识别与刮削）
  external_id  TEXT NOT NULL                 来源内 id：tmdb → "603"；local → 入账时生成的稳定键
  tmdb_id      INT NULL                      保留：source=tmdb 时等于 external_id 的整数形式，258 处调用点少改；
                                             CHECK ((source='tmdb') = (tmdb_id IS NOT NULL))
  UNIQUE (source, kind, external_id)         替代 UNIQUE(kind, tmdb_id)
```

- **`kind` 是内容形态，与库类型同一个枚举**。v1.x 把库类型和媒体类型拆成两个
  枚举，是因为当时 `kind` 兼任了 TMDB 命名空间；命名空间挪进 `source` 后区别
  消失。拼 TMDB 请求的 `kind.value` 只出现在 `source=tmdb` 的策略内部；
- **没有 TMDB 身份的内容 = `media_item(source=local)` + `media_metadata`**：
  `video` 库里的文件如此，影视库里认不出的文件也如此（4.2）。标题、简介、
  内容时间、主图分别落 `media_item.title`、`media_metadata.overview`、
  `release_date`、`poster_file`（Jellyfin 对所有类型同样只有一个 Primary 图，
  卡片形态由图片长宽比决定，3.1）；
- **播放单元不变**：仍是 `(media_item_id, season, episode)`。`playback_state`、
  最近观看、活动面板、Jellyfin UserData、`/play/{media_item_id}` 地址**一行不改**；
- **守卫收敛在 `source` 上**，5 处（4.8）+ 一条"零 TMDB 请求"回归测试。

### 2.3 与 v1.x 方案 C 的对比：为什么这次是简化不是加码

| | v1.x（文件寻址并行通道） | v2（泛化锚） |
|---|---|---|
| 新表 / 迁移 | `file_metadata` 新表 + `playback_state` 重建 | 只重建 `media_item`（加 2 列、改 1 个唯一键、`tmdb_id` 改可空） |
| 播放单元 / 地址 | `ItemUnit \| FileUnit` 判别联合，六处消费方按变体分派；`/play/[...unit]` 三种写法 | 不变 |
| 展示 | 新 `/videos` 接口 + 新文件详情页 | 复用海报墙与条目详情页，按图片比例换卡片、按能力位隐藏影视区块 |
| Jellyfin | 新 GUID 类型 VIDEO，6 条路由加分支 | 条目 GUID 不变，`Type` 按形态出 `"Video"`，库视图 `homevideos` |
| 身份核心 | 不碰 | 重建一次、5 处 `source` 守卫 |
| 影视库认不出的文件 | 依旧不可见不可播 | 临时本地身份，可见可播，转正迁进度（4.2） |
| 将来加有身份的类型（成人/番号、豆瓣独有条目） | 库侧加档案 + **另做**一次身份内核泛化 | 加 `source` 值 + 识别策略 + 刮削器，内核已就位 |

先前对"合成 `media_item`"的否决（方案 A）针对的是**用假 TMDB id 冒充**——
`source` 是显式维度，不是冒充。

## 3. 数据模型

### 3.1 形态 × 来源：一份按 `(kind, source)` 建的能力档案

`MediaKind` 的语义改为**内容形态**（存储值 `movie` / `tv` + 新增 `video`），
只描述结构：

| 形态 | 单元 | 结构假设 |
|---|---|---|
| `movie` | 单本 | 有标题/年份/演职员/制作方/封面，有可识别的外部身份，目录按作品整理，可写 NFO，可刮削 |
| `tv` | 季集 | 同上，另有季/集结构 |
| `video` | 单本 | **没有结构假设**：标题来自文件名或 sidecar，不整理、不写 NFO、不识别 |

形态**不是题材、不是来源**。"成人"不是一种形态：番号体系下的作品在结构上
就是 `movie`，只是身份不来自 TMDB——那正是 `source` 的事。"动漫"是 `tv`
形态、TMDB 来源、库名叫动漫。用 `video` 库存放**暂不刮削**的成人内容是
预期用法（"现在不识别"选 `video`，"以后要识别"是换来源，见第 8 节）。

**能力档案按 `(kind, source)` 这一对建**——`video` 单独看显得宽泛，是因为
它本来就只是半个坐标：

```python
@dataclass(frozen=True)
class LibraryProfile:
    kind: MediaKind              # 形态
    source: str                  # 身份来源：tmdb / local / (future) jav / douban …
    label: str                   # 创建库时用户看到的名字
    identity: IdentityStrategy   # 识别与建档：TMDB（失败回落 LOCAL，4.2）/ LOCAL
    scraper: Scraper | None      # 刮削/资产：tmdb 现有；local = 吸收本地 NFO/图 + 抓帧
    naming: NamingScheme | None  # 命名模板组：MOVIE / TV / None（不整理）
    ignore_rules: IgnoreProfile  # 扫描忽略口径：SCRAPED / PLAIN
    write_nfo: bool
    subscribable: bool
    jellyfin_type: str           # "Movie" / "Series" / "Video"
    jellyfin_collection: str     # "movies" / "tvshows" / "homevideos"
    default_aspect: float        # 无主图时卡片兜底比例：2/3 或 16/9

PROFILES = {
    ("movie", "tmdb"):  LibraryProfile(label="电影", …),
    ("tv",    "tmdb"):  LibraryProfile(label="剧集", …),
    ("video", "local"): LibraryProfile(label="其他", …),
    # 将来：("movie","jav") 成人（番号）、("movie","douban") 豆瓣独有条目、("tv","bangumi") …
}
```

用户创建库时选的"库类型"就是这一对加它的标签；`library` 表因此除 `kind`
外还记 `source`。

**卡片形态不在档案里**：2:3 还是 16:9 由**主图的真实长宽比**决定，与
Jellyfin 一致（`PrimaryImageAspectRatio` 按图片尺寸算）。否则番号作品的封面
会被硬套 2:3，家庭视频放了 sidecar 海报却被硬套 16:9。`default_aspect` 只在
没有主图时兜底。资产落盘时记宽高（`sources.json` 加两个字段），列表接口带
`primary_aspect`，网页 `poster-card` 与 Jellyfin DTO 同一口径。

各消费方读法：

| 消费方 | 原先 | 改为 |
|---|---|---|
| 扫描识别 / 刮削 / 资产 | `MediaKind(library.kind)` 再 if movie/tv | `profile.identity` / `profile.scraper` |
| 扫描单元 / 整理命名 / NFO 写出 / 遍历忽略 | 按 kind 二分 | 单元看 `kind`（形态固有）/ `profile.naming` / `profile.write_nfo` / `profile.ignore_rules` |
| 订阅目标库校验、路由候选、自动导入规则 | `library.kind == subscription.kind` | 再加 `profile.subscribable` |
| 统计快照 | `kind == "tv"` 算分集 | `kind is tv` 才算分集；待识别按 `unidentified_code`（3.4） |
| Jellyfin 条目 / 视图 | 二分 | `profile.jellyfin_type` / `profile.jellyfin_collection` |
| 卡片形态 | 无 | `primary_aspect` → `default_aspect` 兜底 |
| 前端 | `kind === "movie"` 散落 | 库与条目接口带 `capabilities`（`unit / naming / subscribable / scraped / default_aspect`），组件按能力位分叉 |

### 3.2 `media_item` 迁移与 `library` 加列

`media_item` 一次 batch 重建（先例：`d9f4b1c73e85`）：

```
+ source        TEXT NOT NULL DEFAULT 'tmdb'
+ external_id   TEXT NOT NULL              回填 = CAST(tmdb_id AS TEXT)
~ tmdb_id       INT NULL                   存量全部保留；CHECK ((source='tmdb') = (tmdb_id IS NOT NULL))
- uq_media_item_kind_tmdb
+ uq_media_item_anchor (source, kind, external_id)
+ ix_media_item_source
```

`local` 条目的 `external_id` 是入账时生成的稳定随机键（uuid），只承担唯一性；
条目与文件的配对靠 `library_file.media_item_id`。改名/移动由扫描的尺寸+时长
指纹归并保住 `library_file` 行，条目随之保留；文件删除后 `cleanup_orphan_items`
按既有规则（无文件、无订阅）清掉条目与资产。

`media_metadata` 不加列：`overview` ← NFO plot、`release_date` ← 内容日期、
`genres/studios/directors/cast/vote_average/runtime_minutes` ← NFO 对应字段、
`poster_file` ← 主图、`scraped_at` ← 入账时间。`media_item.title/original_title`
← NFO title → 解析标题（影视库）/ 文件名主干（`video` 库），`year` ← 内容
年份，`aliases=[]`，其余外部 id 与 TMDB 图片路径为 NULL。同一天多段录像的
次序用 `library_file.file_mtime_ns`，不为此加列。

`library` 加 `source`（存量回填 `tmdb`）与 `generate_thumbnails`（默认真）；
`exclude_from_home` 视第 9 节拍板。`library_file` 不动；`IdentitySource`
枚举加 `LOCAL`（标题来自本地推断，区别于 `NFO`）。

### 3.3 `playback_state` 不动

播放单元仍是 `(media_item_id, season_number, episode_number)`，`video` 库与
影视库临时条目的单本内容用 `(item, 0, 0)`，剧集形态的临时条目用解析出的
季集号。

### 3.4 统计与清单口径

`stats_unidentified_count` 与待处理清单改按 `unidentified_code IS NOT NULL
AND ignored_at IS NULL`，不再依赖 `media_item_id IS NULL`——因为认不出的
文件也有（临时）锚了。`media_item_id IS NULL` 只剩两种合法情形：用户忽略、
类型错放（4.2）。

## 4. 机制

### 4.1 扫描：策略分派，其余复用

复用 `scan.py` 的遍历/探测/落账/归并/对账/统计/监听全套，`_ingest_file`
把识别交给 `profile.identity`：

- **TMDB 策略**：现有 `_identify`（NFO tmdbid → 路径标记 → 名称收敛）+
  `ensure_media_item`，不变；**失败时回落 LOCAL 策略**（4.2）；
- **LOCAL 策略** `ensure_local_item(kind, *, evidence, group, absorb)`：
  - 标题：sidecar `<主干>.nfo` 的 `<title>` → 影视库用 `guess_evidence` 的
    解析标题与年份（发布名 `Show.Name.S01E01.1080p` → "Show Name" 2024）
    → `video` 库用文件名主干原样（家庭视频命名无规律，任何清洗都有误伤）；
  - 分组：`video` 库一文件一条目；影视库按作品——条目目录内的文件共享一条
    临时条目，库根散文件按解析出的 `(title, year)` 分组；剧集形态的季集号
    沿用 `_unit_for`（不依赖 TMDB）；
  - **全字段吸收第三方 NFO**（复用 `nfo.read_entry_metadata`）：plot /
    premiered\|aired\|releasedate / year / runtime / genre\|tag / studio /
    director / actor（含头像地址）/ rating 落 `media_metadata`；根元素不校验、
    `tmdbid` 忽略（1.5.2）。大量"不想让 movieclaw 刮削"的内容早被 TMM、番号
    整理器刮过一遍，原样吸收即零成本完整展示，且一个外部请求都不发；
  - 内容日期：NFO → 容器标签 `date` → `creation_time` → mtime（`MediaSpec`
    加可选 `creation_time` / `tag_date`；`dateadded` 是入库时间语义，不用）；
  - 同一事务写 `media_item` + `media_metadata`，从这里往下与 TMDB 路径
    完全一样：`library_file.media_item_id` 挂锚、`identity_source=NFO|LOCAL`。

遍历忽略按 `profile.ignore_rules`：`PLAIN`（`video` 库）= 隐藏目录 + 系统目录
（`@eaDir` / `metadata` / `.actors` / `lost+found` / `#recycle`）+ 主干精确等于
`sample`；不做子串规则、不做 extras 目录名规则（1.5.4：`婚礼花絮.mp4`、
`clips/` 这类名字正是家庭视频的常态）。影视库维持 `SCRAPED` 口径。

已知行重扫：`dir_files` 里出现 `<主干>.nfo` 且行 `identity_source=LOCAL`
（标题来自推断）时重读 sidecar 并更新条目（零额外目录 IO）；影视库的临时
条目文件照旧每轮重跑 TMDB 识别（现状 `retried` 语义），命中即转正（4.2）。

扫描收尾：`summary.identified_item_ids` 照常收集，ASSETS 阶段照常调
`ensure_assets(item_id)`，它内部按 `source` 分派（4.4）。`.strm`、原盘、
改名归并、缺失标记、回收站、外挂字幕、AI 字幕 `queue_after_scan`、探测回填：
不动。

### 4.2 影视库里认不出的文件：临时本地身份，可见可播

T0 剧集（TMDB 还没建条）、冷门片、自压内容进了影视库，今天只出现在
「待处理」抽屉，海报墙不显示、播不了、不记进度。library.md 决策 4 解决的是
**不错挂**，没解决**能不能看**。v2 下不需要新机制：

```
_ingest_file（影视库）:
  TMDB 策略 identify(file)
    ├─ 命中 → 现状
    └─ 失败（no_match / ambiguous / unparsable / tmdb_unreachable）
         → LOCAL 策略建临时条目 media_item(kind=库形态, source=local)（解析标题、按作品分组）
         → library_file.media_item_id = 临时条目；unidentified_code/reason/candidates 照记
```

- 文件从此有锚：海报墙、详情页、播放、进度、最近观看、Jellyfin 全部照常；
  一部 T0 剧的 5 个分集是一张卡，季集区用 `build_season_episodes` 里"库里
  实际有的集"那一半（集名取自文件名）；资产走 LOCAL 的 `ensure_assets`
  （吸收目录里的第三方 NFO/图，否则抓帧）——Infuse 里立刻有 16:9 帧图；
- 「待识别」从看不见的队列变成**条目上的状态**：海报墙卡片打「未识别」
  角标，详情页顶部给显眼的「识别 / 认领」入口（复用现有认领面板与候选
  点选），「待处理」抽屉保留为筛选视图并链接到详情页；
- **转正 = 改锚 + 迁进度 + 清孤儿**（4.9 的 `reanchor_files`）：重扫命中、
  人工认领、重新识别都走它；临时条目随后被 `cleanup_orphan_items` 收走；
- **仍保持 NULL 锚的只剩两类**：用户点过「忽略」的文件；`kind_mismatch`
  （剧集放进电影库）——那不是"认不出"而是"放错了"，给它一条电影形态的
  临时条目会把分集摆成版本，比不显示更误导，仍走「放错库了」引导转移；
- 存量：现有 NULL 锚的未识别行不需迁移，下次扫描重跑识别链自然落到 LOCAL
  策略；升级说明写"重扫一次"。

library.md 决策 4 相应修订为：「未识别文件落账并挂**临时本地身份**（可见
可播），待识别清单人工认领；NULL 锚只用于忽略与类型错放」。

### 4.3 展示：同一套海报墙与详情页

- 海报墙 `build_library_wall` 查询不改；`LibraryItemView.tmdb_id` 改
  `int | None`，新增 `capabilities` 与 `primary_aspect`；卡片按比例出竖/横版
  （横版带时长角标与进度条）；`source=local` 且库为影视形态的条目打
  「未识别」角标；排序多一档 `release_date`（内容时间）；
- 条目详情页 `build_item_detail` 复用：按能力位隐藏演职员（无 cast 时）、
  季集（单本时）、订阅/洗版入口、TMDB 链接、刮削归属，保留播放键、事实
  芯片、`MediaTrackRows`、`FileSection`（回收、字幕）——**不需要新页面**；
- 图片回落：sidecar 图（`-thumb`/`-landscape` → 同名 → `-poster`；`-fanart`
  作背景；单视频目录接受不带前缀的 `poster/folder/fanart`，1.5.3）→ 容器
  `attached_pic` → 抓帧 → 占位；
- 库封面 `cover.py` 按主图比例出拼贴变体；首页汇总补「K 个视频」；库内
  搜索按条目标题天然覆盖。

### 4.4 主图 / 缩略图 = LOCAL 条目的 `ensure_assets`

`media_scrape.ensure_assets(item_id)` 按 `source` 分派：`local` 走 `thumbs.py`
——sidecar 图 / `attached_pic` 优先，否则 ffmpeg 抓帧，参数照抄 Jellyfin
（1.5.3）：10% 位置（未知时长 10s）、`thumbnail=n=24` 选帧、隔行 `bwdif`、
HDR tonemap（复用 `playback/ffmpeg_args.py` 的滤镜构造；iPhone 录像大量
HLG/杜比视界）、mpegts `-skip_frame nokey`、宽度上限 1280、strm 跳过、失败
留 NULL 下次重试、限并发。产物写 `data/metadata/images/{media_item_id}/poster.jpg`
（与影视海报同一资产目录，`sources.json` 记来源与宽高），登记
`media_metadata.poster_file`，经 `image_variants` 出卡片尺寸。库级开关
`generate_thumbnails`。

### 4.5 播放与进度：零改动

`/play/{media_item_id}`、会话、进度、续播、最近观看、活动面板对 LOCAL 条目
原样工作。唯一前端增量：`player-page.tsx` 对 `video` 形态的库，上一个/下一个
= 库内按当前排序的相邻条目（相册连播）。

### 4.6 下载与导入目标：原样落库

目标为 `video` 库时 save_path = `{主根}`（种子结构原样落下）；监听导入
「指定库」为 `video` 库时整理器退化为原样转移（硬链/复制到 `{主根}/`，
保留原名，不识别不改名；`ingest_entry` 台账、幂等、退避不变）；落盘后由
实时监控/扫描入账。订阅与自动路由排除 `subscribable=False` 的库。

### 4.7 Jellyfin

- 库视图 `CollectionType = profile.jellyfin_collection`（`homevideos`）；
- 条目 DTO：`Type = profile.jellyfin_type`（`"Video"`）、`MediaType: "Video"`、
  `IsFolder: false`、`PremiereDate/ProductionYear` ← `release_date`、
  `ProviderIds` 仅 `source=tmdb`、**`PrimaryImageAspectRatio` 按真实图片尺寸**
  （影视条目顺手补；`Video` 在 Jellyfin 没有 2/3 默认值，客户端靠它定卡片
  形状）；GUID 仍是条目 GUID；影视库临时条目按形态出 `Movie`/`Series`，
  无 `ProviderIds`；
- `_entries_for_parent` 的 LIBRARY 分支对 `video` 库不论递归与否返回全部条目；
  `Counts` 中 `Video` 只计 `ItemCount`；`Latest` 对 `video` 库逐条不聚合；
  `Resume` 天然包含；
- PlaybackInfo / stream / 字幕 / playstate / images：**零改动**；
- 偏离登记（jellyfin-compat.md）：不输出 `Folder` 层级、不输出 `Photo`。

### 4.8 守卫清单

| 守卫 | 位置 |
|---|---|
| 定时刷新只取 `source='tmdb'` | `media_refresh.py:46` |
| `scrape_media_item` / `ensure_assets` 按 `source` 分派 | `media_scrape.py` |
| NFO 写出（身份/完整/分集）只对 `source='tmdb'` | `media_scrape.py:1521,1535`、`nfo.py` |
| Jellyfin `ProviderIds` 只对 tmdb | `catalog.py` |
| 前端 TMDB 链接 / 订阅 / 洗版入口按 `tmdb_id != null` 与 `capabilities`；认领/重识别入口对影视库临时条目**开放** | `library-item-detail-view.tsx:450-479, 665-673` 等 |
| 订阅目标库、路由、自动导入规则按 `subscribable` | `subscription/core.py:1349`、`routing.py`、`import_watch_config.py` |
| 整理对 `naming is None` 拒绝；刮削设置对 `scraper is None` 隐藏 | 各入口 |
| 新增"遍历全部条目"的任务必须按 `source` 过滤（写进模型注释） | `models/media_item.py` |
| 回归测试：LOCAL 条目跑完全部后台任务与详情页，MockTransport 断言零 TMDB 请求 | `tests/api/test_library_video_kind.py` |

### 4.9 `reanchor_files` 原语

一个文件集合从旧单元换到新单元时的标准动作：改 `library_file.media_item_id`
/季集号 → 按 (文件 → 旧单元 → 新单元) 复制 `playback_state`（目标已存在取
进度更靠后的一份）→ 触发 `cleanup_orphan_items` → 重算统计。四个调用方：
扫描自动转正、`claim_files`、`reidentify_item`、将来的库类型转换（第 8 节）。

## 5. NFO 兼容的边界

读：LOCAL 策略只读 sidecar `<主干>.nfo`（不读目录级 `movie.nfo`——Jellyfin 对
`Video` 同样不读），根元素不校验，全字段吸收，`tmdbid`/`uniqueid` 忽略，
`dateadded` 不当内容时间。TMDB 策略的 NFO 读取不变。

写：`write_nfo=False` 的档案**永不写**（`video` 库；影视库临时条目也不写——
没有比用户更权威的信息）。Jellyfin 对新库的 NFO saver 默认也是关的。

## 6. 实施计划（最终版）

三期同一发布周期，按依赖排序；一期是地基，二、三期互不依赖。

### 一期：身份泛化、入账、临时身份、展示、缩略图

| # | 事项 | 落点 |
|---|---|---|
| 1.1 | `media_item` 迁移（3.2）；模型加 `source`/`external_id`，`tmdb_id` 可空 + CHECK；`get_by_anchor` 改按 `(source, kind, external_id)`；3 处 `MediaItem.tmdb_id ==` 查询改 `external_id`；`library` 加 `source`（回填 tmdb）、`generate_thumbnails`（、`exclude_from_home` 视拍板） | `models/media_item.py`、`models/library.py`、`repositories/media_repo.py`、`routing.py`、`subscription/dispatch.py`、`alembic/versions/` |
| 1.2 | `MediaKind` 加 `video`；`LibraryProfile` + `PROFILES`（3.1）；`IdentityStrategy`（TMDB 包住现有链并回落 LOCAL；LOCAL 新写）；`ensure_local_item`（标题来源/分组/NFO 吸收）；`IdentitySource.LOCAL` | 新 `services/library/profile.py`、`services/media_library.py` |
| 1.3 | 8 处 `MediaKind(library.kind)` 周边二分改读能力位；`_KIND_NAMES`/`_KIND_LABELS` 补项；`genres.py` 对 video 返回空；创建库接口按 `(kind, source)` 校验 | `scan.py`、`organize.py`、`claim.py`、`items.py`、`ingest.py`、`nfo.py`、`import_watch_config.py`、`genres.py`、`api/routes/libraries.py` |
| 1.4 | 4.8 守卫清单 + 零 TMDB 请求回归测试 | 见 4.8 |
| 1.5 | 扫描：策略分派与 LOCAL 回落（4.1/4.2）、PLAIN 忽略口径、单视频目录不带前缀图、sidecar 重读；`MediaSpec` 加 `creation_time`/`tag_date` | `scan.py:2378-2560, 2942-3107, 134-216, 1876-1950, 2284`、`media_probe.py` |
| 1.6 | `reanchor_files` 原语；接入扫描转正、`claim_files`、`reidentify_item` | 新 `services/library/reanchor.py`、`claim.py:40`、`scan.py:3611` |
| 1.7 | 统计快照与待处理清单改按 `unidentified_code`（3.4） | `library_repo.py:60-176`、`library_file_repo.list_unidentified` |
| 1.8 | `ensure_assets` 的 local 分派 + `thumbs.py`；资产记宽高；删条目/删库清理 | `media_scrape.py`、新 `services/library/thumbs.py`、`image_variants.py` |
| 1.9 | 接口：`LibraryView`/`LibraryItemView`/`LibraryItemDetailView` 加 `capabilities`、`primary_aspect`，`tmdb_id` 可空；库列表 `?kind=video`；海报墙排序加 `release_date` | `schemas/library.py`、`api/routes/libraries.py`、`items.py` |
| 1.10 | 前端：`MediaType` 加 `"video"`；`LIBRARY_KIND_META` 加「其他」；卡片按 `primary_aspect` 选竖/横版；「未识别」角标与认领入口；详情页按能力位隐藏影视区块；TMDB 链接/订阅入口判空；首页汇总；库封面变体；播放器相邻条目连播 | `lib/media-types.ts`、`library-view.tsx`、`library-detail-view.tsx`、`library-item-detail-view.tsx`、`poster-card.tsx`、`cover.py`、`player/player-page.tsx` |
| 1.11 | OpenAPI 基线重生成；`mclaw_tool.py` 域说明；library.md 决策 4 修订；README「其他视频库」一节（含"升级后重扫一次"） | `export_openapi.py`、两份 `spec.json`、`docs/` |

**一期验收**（`tests/api/test_library_video_kind.py` 新建，影视既有用例零改动全绿）：

1. `video` 库：放入带 NFO、无 NFO、`婚礼花絮.mp4`、`sample.mp4`、HLG 录像、
   `.strm` → 扫描后全部为 `source=local` 条目、标题与 `release_date` 正确、
   花絮在账、`sample.mp4` 不在账、缩略图落 `data/metadata/images/{id}/poster.jpg`、
   strm 无图 → 海报墙横版卡 → 详情页无影视区块 → `/play/{id}` → 进度落
   `playback_state` → 改名重扫进度仍在 → 最近观看出现；
2. 第三方刮削目录（`番号/番号.mp4 + 番号.nfo + poster.jpg + fanart.jpg`）→
   标题/简介/演员/封面全部吸收、卡片竖版、零外部请求；
3. 影视库 T0 剧 5 集 → 一张「未识别」卡、季集区五集可播、进度落库 → TMDB
   建条后重扫自动转正、卡片变海报、**进度仍在**；忽略文件与 `kind_mismatch`
   文件不出卡；
4. 后台刷新/刮削/NFO 写出对 LOCAL 条目零 TMDB 请求、零 NFO 写出；
5. `media_item` 迁移升降，存量 `(kind, tmdb_id)` 全部保留且唯一。

### 二期：Jellyfin

| # | 事项 | 落点 |
|---|---|---|
| 2.1 | 库视图 `homevideos`；条目 DTO `Type` 按档案、`ProviderIds` 守卫、`PremiereDate`；**`PrimaryImageAspectRatio` 按真实尺寸**（影视顺手补） | `catalog.py:1245-1484`、`routes/library.py:281` |
| 2.2 | `_entries_for_parent` LIBRARY 分支对 video 库返回全部条目；`Counts` 只计 `ItemCount`；`Latest` 不聚合 | `routes/library.py:790-895, 913-932, 1125-1145` |
| 2.3 | 偏离清单登记 | `docs/design/jellyfin-compat.md` |

验收：协议用例 + Infuse 真机（列出、横版缩略图、直连、进度回传、继续
观看、影视库临时条目可见可播）。播放/进度/图片路由不改，只跑回归。

### 三期：下载与导入目标

| # | 事项 | 落点 |
|---|---|---|
| 3.1 | `resolve_save_path` / `derive_save_path` 对 video 库 = `{主根}` | `routing.py:224`、`config.py:43-71` |
| 3.2 | 手动下载选库含 video 库；下载目标弹窗列出；Go CLI kind 校验放行 | `torrent_submit.py`、`download-target-dialog.tsx`、`cli/internal/overlay/download.go:249` |
| 3.3 | 监听导入「指定库」为 video 库时原样转移 | `ingest.py:1662-1800` |
| 3.4 | 订阅/自动路由维持按 `subscribable` 排除 | `import_watch_config.py`、`subscription/core.py` |

验收：下载到 video 库原名落 `{主根}` → 监控入账带缩略图；监听导入原样硬链
（同 inode）→ 台账；订阅弹窗与自动路由不出现 video 库。

### 跨期

不 bump `runtime-version`（ffmpeg 已在镜像）；迁移一个（`media_item` +
`library` 加列可并入同一版本），单向，重建前后行数与锚逐一断言；文档随
实施补实施记录、Jellyfin 偏离清单、README。

## 7. 风险与开口

1. **`media_item` 重建**：迁移的 `PRAGMA foreign_keys` 处理与 `d9f4b1c73e85`
   同一写法；升降测试与存量锚断言必做；
2. **LOCAL 条目泄漏进 TMDB 路径**：五处守卫 + 零请求回归测试；"遍历全部
   条目必须按 `source` 过滤"写进模型注释；
3. **三值 `kind`**：新分叉一律读能力档案，评审拒绝新增 `kind == "movie"`
   字面比较（现有约 40 处随本期改掉）；
4. **抓帧 IO**：只对新条目、限并发、strm 跳过、库级开关；
5. **敏感内容暴露面**：① `generate_thumbnails` 关掉不抓帧；② 成员可见库
   （`visible_library_ids`）本就按库授权，浏览/播放/最近观看/Jellyfin 视图
   都经它过滤；③ `exclude_from_home`（第 9 节待拍板）；
6. **以后给 `video` 库的文件加身份**：换来源/形态，走第 8 节的转换作业；
   转换器出现前 = 删库重建 + 丢观看状态，用户文档写明；
7. **临时条目的误分组**：散文件按解析 `(title, year)` 分组可能把不同作品合
   到一起（解析出同名同年）。代价是"两部片一张卡"，用户在详情页可拆
   （认领其中一部即触发 `reanchor_files`）；比"五集五张卡"轻；
8. 目录层级、图片收录（相册）、容器 `title` 标签、NFO 观看状态导入：开口；
9. **新来源**：`source` 是通用维度。豆瓣今天只是辅助列，有了 `source` 后
   TMDB 没有而豆瓣有的条目可以以 `source=douban` 建档刮削；Bangumi、TVDB、
   Fanart.tv 同理。边界：**订阅与匹配**依赖别名集合、季集结构、播出日期，
   新来源要参与订阅得先补齐这些输入，展示与播放不受此限。

## 8. 前瞻：媒体库类型切换（本期不实现，模型为它让路）

**结论：应当支持**，形态是带预览的「转换库类型」作业（预览 → 确认 → 持久
Job）。Jellyfin / Plex / Emby 都只能删库重建；我们能做是因为身份锚在文件行
上。场景：`(video, local)` → `(movie, tmdb|jav)`（先能放、后要身份）；
`(movie, tmdb)` ↔ `(tv, tmdb)`（建库选错）；只换 `source`（多来源时代）。

**语义：转换 = 在新档案下重扫，锚随文件携带**——记录 (文件 → 旧单元) 基线
→ 清文件行身份列 → 改 `library.kind/source`、裁掉不适用的 `scrape_overrides`
→ 按新档案全量扫描 → `reanchor_files` 迁观看状态 → 旧条目走孤儿清理 →
重算统计、写活动。文件行是两代条目之间唯一稳定的桥。

**前置校验（拒绝或引导，不静默）**：有订阅挂在本库 → 拒绝并引导改库
（"库类型不可改"规则的真正来源，退化为"有订阅时不可改"）；默认库交接复用
删库逻辑；引用本库的导入/下载规则重新校验；忽略口径变严时"将不再收录的
N 个文件"预览单列并删出台账（不标 missing）；我们自己写进媒体目录的镜像
产物（NFO 按档案重新生成逐字节比对、图片与资产逐字节比对即可识别）预览
列出、确认后删除，第三方文件一律不碰。

**本期必须守住的约束**：① `library.kind/source` 是普通列，"不可改"只在
接口层拒绝，不加模型层约束；② 不新增任何按类型派生却无法重算的状态
（本期新列都过得了这条）；③ `library_file` 是唯一跨代稳定的键，绝不随类型
重建；④ 镜像写出保持确定性；⑤ `cleanup_orphan_items` 语义不放宽。

**不做**：即时切换（必须走 Job）；一个库内的部分转换（用转移拆成两个库）；
为转换预建任何表或列。

## 9. 决策记录与待拍板

**已拍板（用户，2026-09-03）**：单一通用形态 `video`，展示名「其他」；平铺不建目录；
缩略图入一期；播放状态不拆表（v2 下自然不动）；Jellyfin 高优先级（二期同
周期）；`video` 库可作下载/导入目标（原样落库）；文件级原生能力（AI 字幕、
trickplay、回收、外挂字幕）一致；接受重建 `media_item` 引入 `source`/
`external_id`；`video` 库存放暂不刮削的成人内容属预期用法；影视库认不出的
文件给临时本地身份、可见可播；库类型切换应当支持（本期只让路不实现）。

**已定的设计选择**：能力档案按 `(kind, source)` 建；卡片形态由主图长宽比
决定；点卡片进现有条目详情页（不新建文件详情页）；原样落库时目录型种子落
`{主根}/种子目录/`。

**待拍板（唯一）**：库级 `exclude_from_home`（不进首页最近添加聚合区、不参与
首页封面拼贴；Plex "Include in dashboard" / Jellyfin `LatestItemsExcludes`
同款）。建议一期顺手做，一列一处过滤。

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
