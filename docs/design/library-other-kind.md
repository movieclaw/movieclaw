# 媒体库「其他」类型：不刮削内容的入账、展示与播放

> 状态：**v2（2026-09-03）——方案重做，待拍板**。v1.x 走的是"给无身份文件另开
> 一条文件寻址通道"；评审追问「`media_item` 一定是 TMDB 专属吗」后重新核了
> 身份内核的约束（第 2 节），结论是可以且应该泛化：把「身份来源」做成
> `media_item` 的一个维度，本地视频成为真正的条目。整条并行通道（文件单元、
> `file_metadata`、`/play/f/`、Jellyfin VIDEO GUID）随之消失，设计反而变简单。
> v1.x 的 Jellyfin 考察（1.5 节）与拍板决策（第 8 节）保留。v0 草案的八个待决问题
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

结论：难点不在扫描，在**身份**——展示、播放、进度、Jellyfin 四条链全都
吊在 `media_item` 上。v1.x 的答案是绕开它另开一条文件寻址通道；v2 的答案
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

所以"TMDB 专属"不是结构性的，是历史上只有一个数据源时的默认。把它做成
显式维度，代价可控。

### 2.2 方案：`source` + `external_id` 泛化锚，本地视频是真条目

```
media_item
  kind         movie / tv / video          内容形态（决定单元、卡片、Jellyfin 类型）
  source       tmdb / local / (future: jav…)  身份来源（决定谁负责识别与刮削）
  external_id  TEXT NOT NULL               来源内的 id：tmdb → "603"；local → 入账时生成的稳定键
  tmdb_id      INT NULL                    保留：source=tmdb 时等于 external_id 的整数形式，便于 258 处调用点少改；CHECK ((source='tmdb') = (tmdb_id IS NOT NULL))
  UNIQUE (source, kind, external_id)       替代 UNIQUE(kind, tmdb_id)
```

- **`kind` 从「TMDB 路径段」变成「内容形态」**，与库类型是同一个枚举
  （movie / tv / video）。v1.x 把库类型和媒体类型拆成两个枚举，是因为
  当时 `kind` 兼任了 TMDB 命名空间；命名空间挪进 `source` 后，两者的
  区别消失，**一个枚举、一份能力档案**（3.1）。TMDB 请求拼
  `kind.value` 的地方只在 `source == tmdb` 的路径上，天然成立；
- **本地视频 = `media_item(kind=video, source=local)` + `media_metadata`**：
  标题、简介、内容时间、缩略图分别落 `media_item.title`、
  `media_metadata.overview`、`release_date`、`poster_file`（Primary 图，
  形态由能力档案决定是 2:3 还是 16:9，Jellyfin 对所有类型同样只有一个
  Primary）。一个文件一个条目（能力档案 `unit=SINGLE`，且 `source=local`
  不合并版本）；
- **播放单元不变**：仍是 `(media_item_id, season, episode)`，本地视频用
  `(item, 0, 0)`。`playback_state`、最近观看、活动面板、Jellyfin UserData、
  `/play/{media_item_id}` 地址**一行不改**；
- **守卫收敛在 `source` 上**，只有 5 处：定时刷新只选 `source='tmdb'`；
  `scrape_media_item`/`ensure_assets` 按 `source` 分派（local 的"资产"就是
  抓帧）；NFO 写出只对 tmdb；Jellyfin `ProviderIds` 只对 tmdb；前端 TMDB
  链接与订阅入口按 `tmdb_id != null`。加一条测试：用 MockTransport 起一个
  本地条目走完全部后台任务，断言零 TMDB 请求。

### 2.3 与 v1.x 方案 C 的对比：为什么这次是简化不是加码

| | v1.x（文件寻址并行通道） | v2（泛化锚） |
|---|---|---|
| 新表 / 迁移 | `file_metadata` 新表 + `playback_state` 重建（可空列 + 部分唯一索引） | 只重建 `media_item`（加 2 列、改 1 个唯一键、`tmdb_id` 改可空） |
| 播放单元 | 领域层引入 `ItemUnit \| FileUnit` 判别联合，会话/进度/续播/最近观看/活动面板/Jellyfin UserData 全部按变体分派 | 不变 |
| 播放地址 | `/play/[...unit]` 三种写法 | 不变 |
| 展示 | 新 `/videos` 接口 + 新文件详情页 | 复用海报墙与条目详情页，按能力档案换卡片形态与隐藏影视区块 |
| Jellyfin | 新 GUID 类型 VIDEO，6 条路由加分支 | 条目 GUID 不变，`Type` 按 `kind` 出 `"Video"`，库视图 `homevideos` |
| 身份核心 | 不碰 | 重建一次、加 5 处 `source` 守卫 |
| 三值 `kind` 的影响面 | 库类型三值：约 40 处 `library.kind` 分叉要改 | 内容形态三值：同样约 40 处（`library.kind` 与 `item.kind` 的分叉大半是同一批），改法同为读能力档案 |
| 将来加有身份的类型（成人/番号） | 库侧加档案 + **另做**一次身份内核泛化 | 加一个 `source` 值 + 一个识别策略 + 一个刮削器，内核已就位 |

v1.x 为了不碰身份核心，在四条链上各造了一条平行的文件通道；v2 碰一次
身份核心，四条平行通道全部不需要。触面更小、概念更少、扩展路径更直。
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
就是 `movie`（标题、演员、厂牌、封面、可识别、按作品整理），只是身份不来自
TMDB——那正是 `source` 的事。把它放进 `video` 形态等于放弃它本可以有的
全部元数据能力。同理"动漫"是 `tv` 形态、TMDB 来源、库名叫动漫。

**能力档案按 `(kind, source)` 这一对建**，不按形态单独建——`video` 单独看
显得宽泛，是因为它本来就只是半个坐标：

```python
@dataclass(frozen=True)
class LibraryProfile:
    kind: MediaKind              # 形态
    source: str                  # 身份来源：tmdb / local / (future) jav / douban …
    label: str                   # 创建库时用户看到的名字
    identity: IdentityStrategy   # 识别与建档（tmdb 包住现有链；local 读 sidecar/文件名）
    scraper: Scraper | None      # 刮削/资产（tmdb 现有；local = 抓帧；None 不刮）
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
外还要记 `source`（`media_item.source` 的默认值来源于此）。

**卡片形态不在档案里**：2:3 还是 16:9 由**主图的真实长宽比**决定，与
Jellyfin 一致（`PrimaryImageAspectRatio` 按图片尺寸算，客户端据此选竖版或
横版卡）。否则番号作品的封面会被硬套 2:3，家庭视频放了 sidecar 海报却被
硬套 16:9。`default_aspect` 只在没有主图时兜底。网页端 `poster-card` 与
Jellyfin DTO 同一口径：资产落盘时记下宽高（`sources.json` 已有溯源结构，
加两个字段），列表接口带出 `primary_aspect`。

各消费方读法：

| 消费方 | 原先 | 改为 |
|---|---|---|
| 扫描识别 / 刮削 / 资产 | `MediaKind(library.kind)` 再 if movie/tv | `profile.identity` / `profile.scraper` |
| 扫描单元 / 遍历忽略 / 整理命名 / NFO 写出 | 按 kind 二分 | `kind` 的单元形态 / `profile.ignore_rules` / `profile.naming` / `profile.write_nfo` |
| 订阅目标库校验、路由候选 | `library.kind == subscription.kind` | 再加 `profile.subscribable` |
| 统计快照 | `kind == "tv"` 算分集 | `kind is tv` 才算分集（单元形态是 kind 的固有属性） |
| Jellyfin 条目 / 视图 | 二分 | `profile.jellyfin_type` / `profile.jellyfin_collection` |
| 卡片形态 | 无 | 主图长宽比 → `default_aspect` 兜底 |
| 前端 | `kind === "movie"` 散落 | 库与条目接口带 `capabilities`（`unit / naming / subscribable / scraped / default_aspect`），组件按能力位分叉 |

现有 8 处 `MediaKind(library.kind)` 不再抛错（`video` 是合法值）；拿 kind 去
拼 TMDB 请求的调用只发生在 `source=tmdb` 的策略内部。

### 3.2 `media_item` 迁移

一次 batch 重建（先例：`d9f4b1c73e85`）：

```
+ source        TEXT NOT NULL DEFAULT 'tmdb'
+ external_id   TEXT NOT NULL              回填 = CAST(tmdb_id AS TEXT)
~ tmdb_id       INT NULL                   存量全部保留；CHECK ((source='tmdb') = (tmdb_id IS NOT NULL))
- uq_media_item_kind_tmdb
+ uq_media_item_anchor (source, kind, external_id)
+ ix_media_item_source
```

`local` 条目的 `external_id` 是入账时生成的稳定随机键（uuid），只承担
唯一性；条目与文件的配对靠 `library_file.media_item_id`。改名/移动由扫描
的尺寸+时长指纹归并保住 `library_file` 行，条目随之保留；文件删除后
`cleanup_orphan_items` 按既有规则（无文件、无订阅）清掉条目与资产。

`media_metadata` 不加列：`overview` ← NFO plot、`release_date` ← 内容日期、
`genres` ← NFO genre/tag、`poster_file` ← 缩略图、`scraped_at` ← 入账时间。
`media_item.title/original_title` ← NFO title / 文件名主干，`year` ← 内容年份，
`aliases=[]`，`status/imdb_id/douban_id/poster_path/backdrop_path` 为 NULL。
内容时间的时分秒（同一天多段录像的排序）用 `library_file.file_mtime_ns`
作次序键，不为此加列。

`library` 加两列：`source`（该库的身份来源，与 `kind` 一起定位能力档案；
存量回填 `tmdb`）与 `generate_thumbnails`（默认真）。`library_file` 不动。

### 3.3 `playback_state` 不动

播放单元仍是 `(media_item_id, season_number, episode_number)`，本地视频
`(item, 0, 0)`。v1.x 的多态重建取消。

## 4. 机制

### 4.1 扫描：策略分派，其余复用

复用 `scan.py` 的遍历/探测/落账/归并/对账/统计/监听全套。`_ingest_file`
把识别交给 `strategies[profile.default_source]`：

- **tmdb 策略**：现有 `_identify`（NFO tmdbid → 路径标记 → 名称收敛）+
  `ensure_media_item`，不变；
- **local 策略**：sidecar `<主干>.nfo`（根元素不校验，1.5.2）**全字段吸收**
  ——title / sorttitle / plot / premiered\|aired\|releasedate / year / runtime /
  genre\|tag / studio / director / actor（含 thumb 头像地址）/ rating，分别落
  `media_item` 与 `media_metadata`（`overview` / `genres` / `studios` /
  `directors` / `cast` / `vote_average` / `runtime_minutes`）。现有
  `nfo.read_entry_metadata` 已解析这些字段，复用即可。动机：大量"不想让
  movieclaw 刮削"的内容其实早被第三方工具（TMM、各类番号整理器）刮过一遍，
  目录里躺着完整 NFO 与图片；local 策略把它们原样吸收，用户零成本得到
  完整展示，而我们仍然一个请求都不发。无 NFO 则 title = 文件名主干（不清洗）。
  图片同理：除 sidecar 前缀图外，**目录里只有一个视频时**接受不带前缀的
  `poster.jpg` / `folder.jpg` / `fanart.jpg`（Jellyfin 的 `IsInMixedFolder`
  语义，1.5.3）——"一部一目录"正是这类整理器的默认布局。内容日期：NFO → 容器标签 `date` →
  `creation_time` → mtime（`MediaSpec` 加可选 `creation_time` / `tag_date`）。
  然后 `ensure_local_item(kind=video, ...)` 同一事务写 `media_item` +
  `media_metadata`，返回条目——从这里往下与 tmdb 路径完全一样：
  `library_file.media_item_id` 挂锚、单元 `(0,0)`、`identity_source=NFO|LOCAL`。
  **本地文件永远是"已识别"**，待识别清单与身份复核天然与它无关。

遍历忽略按 `profile.ignore_rules`：`PLAIN` 口径 = 隐藏目录 + 系统目录
（`@eaDir` / `metadata` / `.actors` / `lost+found` / `#recycle`）+ 主干精确等于
`sample`；不做子串规则、不做 extras 目录名规则（1.5.4）。

已知行重扫：`dir_files` 里出现 `<主干>.nfo` 且条目 `identity_source=LOCAL`
（即标题来自文件名）时重读 sidecar 并更新条目（零额外目录 IO）。

扫描收尾：`summary.identified_item_ids` 照常收集，ASSETS 阶段照常调
`ensure_assets(item_id)`——它内部按 `source` 分派：tmdb 下图/镜像，local
抓帧（4.3）。不再需要独立的 THUMBS 阶段。

`.strm`、原盘、改名归并、缺失标记、回收站、外挂字幕、AI 字幕 `queue_after_scan`、
探测回填：不动。

### 4.2 展示：同一套海报墙与详情页，换形态

- 海报墙 `build_library_wall` 不改查询（本地条目有 `media_item_id`）；
  `LibraryItemView.tmdb_id` 改 `int | None`，新增 `capabilities`；卡片按
  `profile.card` 渲染 16:9（缩略图 + 时长角标 + 进度条），排序多一档
  `release_date`（内容时间）；
- 条目详情页 `build_item_detail` 复用：本地条目读 `media_metadata`
  （overview/图）与文件行；前端按能力位隐藏演职员、季集、订阅/洗版入口、
  TMDB 链接、刮削归属，保留播放键、事实芯片、`MediaTrackRows`、
  `FileSection`（回收、字幕）——**不需要新的文件详情页**；
- 图片三级回落对本地条目：sidecar 图（`-thumb`/`-landscape` → 同名 →
  `-poster`；`-fanart` 作背景，1.5.3）→ 容器 `attached_pic` → 抓帧 → 占位；
- 库封面：`cover.py` 按 `profile.card` 出 16:9 变体；首页汇总补「K 个视频」；
  库内搜索 `search_library_items` 天然覆盖（按条目标题）。

### 4.3 缩略图 = 本地条目的 `ensure_assets`

`media_scrape.ensure_assets(item_id)` 按 `source` 分派：`local` 走
`thumbs.py`（4.3 v1.1 的参数照抄 Jellyfin：sidecar/`attached_pic` 优先；
否则 ffmpeg 10% 位置、`thumbnail=n=24`、反交错、HDR tonemap 复用
`playback/ffmpeg_args.py`、mpegts `-skip_frame nokey`、宽度上限 1280、
strm 跳过、失败留 NULL 下次重试），产物写
`data/metadata/images/{media_item_id}/poster.jpg`（**与影视海报同一资产
目录规范**，`sources.json` 记 `thumb_source`），登记 `media_metadata.poster_file`。
经 `image_variants` 出卡片尺寸；库级开关 `generate_thumbnails`。

### 4.4 播放与进度：零改动

`/play/{media_item_id}`、会话、进度、续播、最近观看、活动面板对本地条目
原样工作（单元 `(item,0,0)`）。`GET /playback/items/{id}` 的海报即缩略图。
上一个/下一个：`player-page.tsx` 对 `unit=SINGLE` 且 `card=THUMB` 的库，
用库内按当前排序的相邻条目（相册连播）——这是唯一的前端增量。

### 4.5 下载与导入目标：原样落库

（v1.x 4.5 不变）目标为 `video` 库时 save_path = `{主根}`，监听导入
「指定库」退化为原样转移，不识别不改名；订阅与自动路由排除
`default_source != tmdb` 的库。

### 4.6 Jellyfin

- 库视图 `CollectionType = profile.jellyfin_collection`（`homevideos`）；
- 条目 DTO：`Type = profile.jellyfin_type`（`"Video"`）、`MediaType: "Video"`、
  `IsFolder: false`、`PremiereDate/ProductionYear` ← `release_date`、
  `ProviderIds` 仅 `source=tmdb` 输出、**`PrimaryImageAspectRatio` 按真实
  图片尺寸输出**（影视条目顺手补）；GUID 仍是条目 GUID（0x02），
  `_entries_for_parent` 的 LIBRARY 分支对 `video` 库返回全部条目；
- PlaybackInfo / stream / 字幕 / playstate / images：**零改动**（都按条目
  GUID 与 `(item,0,0)` 单元工作）；
- `Counts`：`Video` 只计 `ItemCount`；`Latest` 对 `video` 库逐条不聚合；
  `Resume` 天然包含；
- 偏离登记：不输出 `Folder` 层级、不输出 `Photo`。

### 4.7 守卫清单（按 `source` / 能力位）

| 守卫 | 位置 |
|---|---|
| 定时刷新只取 `source='tmdb'` | `media_refresh.py:46` |
| `scrape_media_item` / `ensure_assets` 按 `source` 分派 | `media_scrape.py` |
| NFO 写出（身份/完整/分集）只对 `source='tmdb'` | `media_scrape.py:1521,1535`、`nfo.py` |
| Jellyfin `ProviderIds` 只对 tmdb | `catalog.py` |
| 前端 TMDB 链接 / 订阅 / 洗版 / 认领 / 重识别入口按 `tmdb_id != null` 与 `capabilities` | `library-item-detail-view.tsx:450-479, 665-673` 等 |
| 订阅目标库、路由、自动导入规则排除 `default_source != tmdb` 的库 | `subscription/core.py:1349`、`routing.py`、`import_watch_config.py` |
| 整理 / 认领 / 重识别 / 复核 / 刮削设置对 `naming is None` 或 `source=local` 拒绝 | 各入口 |
| 回归测试：本地条目跑完全部后台任务与详情页，MockTransport 断言零 TMDB 请求 | `tests/api/test_library_video_kind.py` |

## 5. NFO 兼容的边界

（同 v1.1）只读 sidecar `<主干>.nfo`，不校验根元素，忽略 `tmdbid`；
`dateadded` 不当内容时间；**永不写**。

## 6. 实施计划（v2）

### 一期：身份泛化 + 入账 + 展示 + 缩略图

| 事项 | 落点 |
|---|---|
| `media_item` 迁移（3.2）；`MediaItem` 模型加 `source`/`external_id`，`tmdb_id` 可空；`media_repo.get_by_anchor` 改按 `(source, kind, external_id)`；3 处 `MediaItem.tmdb_id ==` 查询改 `external_id` | `models/media_item.py`、`repositories/media_repo.py`、`routing.py`、`subscription/dispatch.py`、`alembic/versions/` |
| `MediaKind` 加 `video`；`LibraryProfile` 按 `(kind, source)` 建 `PROFILES`；`IdentityStrategy` 注册表（tmdb 包住现有链、local 新写）；`ensure_local_item`；资产落盘记宽高、接口带 `primary_aspect` | 新 `services/library/profile.py`；`services/media_library.py` |
| 8 处 `MediaKind(library.kind)` 周边二分改读能力位；`_KIND_NAMES`/`_KIND_LABELS` 补项；`genres.py` 对 video 返回空 | `scan.py`、`organize.py`、`claim.py`、`items.py`、`ingest.py`、`nfo.py`、`import_watch_config.py`、`genres.py` |
| 4.7 守卫清单五处 `source` 守卫 + 零 TMDB 请求回归测试 | 见 4.7 |
| 扫描：策略分派、PLAIN 忽略口径、sidecar NFO 全字段吸收（复用 `read_entry_metadata`）、单视频目录接受不带前缀的 poster/folder/fanart、sidecar 重读；`MediaSpec` 加 `creation_time`/`tag_date` | `scan.py:2378-2560, 134-216, 1876-1950, 2284`、`media_probe.py` |
| 统计快照按 `profile.unit`；待识别/复核清单自然不含本地条目（它们已识别） | `library_repo.py:146-176` |
| `ensure_assets` 的 local 分派 + `thumbs.py`；`library.generate_thumbnails` | `media_scrape.py`、新 `services/library/thumbs.py` |
| 接口：`LibraryView`/`LibraryItemView`/`LibraryItemDetailView` 加 `capabilities`，`tmdb_id` 可空；库列表 `?kind=video`；海报墙排序加 `release_date` | `schemas/library.py`、`api/routes/libraries.py`、`items.py` |
| 前端：`MediaType` 加 `"video"`；`LIBRARY_KIND_META` 加「其他」；海报墙卡片按 `primary_aspect`（无图按 `default_aspect`）选竖/横版；条目详情页按能力位隐藏影视区块；TMDB 链接/订阅入口判空；首页汇总；库封面变体 | `lib/media-types.ts`、`library-view.tsx`、`library-detail-view.tsx`、`library-item-detail-view.tsx`、`poster-card.tsx`、`cover.py` |
| 播放器相邻条目连播（4.4） | `components/player/player-page.tsx` |
| OpenAPI 基线重生成；`mclaw_tool.py` 域说明 | `export_openapi.py`、两份 `spec.json` |

验收：建 `video` 库 → 放入带 NFO、无 NFO、`婚礼花絮.mp4`、`sample.mp4`、
HLG 录像、`.strm` → 扫描后全部为已识别条目（`source=local`）、标题与
`release_date` 正确、花絮在账、`sample.mp4` 不在账、缩略图落
`data/metadata/images/{id}/poster.jpg`、strm 无图 → 海报墙 16:9 → 详情页无
影视区块 → `/play/{id}` 播放 → 进度落 `playback_state` → 改名重扫进度仍在 →
最近观看出现 → 后台刷新/刮削/NFO 写出对本地条目零 TMDB 请求、零 NFO
写出；`media_item` 迁移升降、存量 `(kind, tmdb_id)` 全部保留且唯一。
影视既有用例零改动全绿。

### 二期：Jellyfin

| 事项 | 落点 |
|---|---|
| 库视图 `homevideos`；条目 DTO `Type: "Video"`、`ProviderIds` 守卫、`PremiereDate`；**`PrimaryImageAspectRatio` 按真实尺寸**（影视顺手补） | `catalog.py:1245-1484`、`routes/library.py:281` |
| `_entries_for_parent` LIBRARY 分支对 video 库返回全部条目；`Counts` 只计 `ItemCount`；`Latest` 不聚合 | `routes/library.py:790-895, 913-932, 1125-1145` |
| 偏离清单登记 | `docs/design/jellyfin-compat.md` |

验收：协议用例 + Infuse 真机（列出、16:9 缩略图、直连、进度回传、继续观看）。
播放/进度/图片路由不需要改动，用例只做回归。

### 三期：下载与导入目标

（同 v1.x 三期）`resolve_save_path` 对 video 库 = `{主根}`；手动下载选库含
video 库（Go CLI 校验放行）；监听导入「指定库」原样转移；订阅/自动路由
维持排除。

### 跨期

不 bump `runtime-version`（ffmpeg 已在镜像）；迁移一个（`media_item`），
单向，重建前后行数与锚逐一断言；文档随实施补实施记录、Jellyfin 偏离清单、
README「其他视频库」一节。

## 7. 风险与开口

1. **`media_item` 重建**：入向外键 9 处，SQLite batch 按表名引用无影响，
   但迁移必须在 `PRAGMA foreign_keys` 处理上与 `d9f4b1c73e85` 同一写法；
   升降测试与存量锚断言必做；
2. **本地条目泄漏进 TMDB 路径**：五处守卫 + 零请求回归测试；新增任何
   "遍历全部条目"的任务时按 `source` 过滤是硬约定，写进 `media_item` 模型
   注释；
3. **三值 `kind`**：所有新分叉必须读能力档案，PR 评审拒绝新增
   `kind == "movie"` 字面比较（现有约 40 处随本期一并改掉）；
4. **抓帧 IO**：只对新条目、限并发、strm 跳过、库级开关；
5. **敏感内容的暴露面**（用户明确会用 `video` 库放成人内容）：三道既有或
   低成本的闸——① 库级 `generate_thumbnails` 关掉后不抓帧，只用目录里已有
   的图或占位；② 成员可见库（member-management，`visible_library_ids`）本就
   按库授权，播放、浏览、最近观看、Jellyfin 视图都经它过滤；③ **建议新增**
   库级 `exclude_from_home`（不进首页「最近添加」聚合区、不参与首页封面
   拼贴，Plex "Include in dashboard" / Jellyfin `LatestItemsExcludes` 同款），
   一列一处过滤，首页只剩它自己的库卡片。③ 未拍板，一期顺手做成本最低；
6. **以后想给这些文件加身份**（如番号刮削）：那是库从 `(video, local)` 换到
   `(movie, jav)`。库类型今天"创建后不可改"是因为订阅挂在 kind 上，而
   `video` 库没有订阅，放开转换是安全的：重扫按新档案建新条目、
   `library_file` 行不动、观看状态按文件映射到新条目。本期不做转换器，
   前瞻设计见第 10 节；在转换器出现前，换类型 = 删库重建 + 丢观看状态，
   文档里要写明；
7. 目录层级、图片收录（相册）、容器 `title` 标签、NFO 观看状态导入：开口不变；
8. 将来的新来源：`source` 是通用维度，不限于"成人/番号"。豆瓣今天只是
   `media_item.douban_id` 这个辅助列（条目必须先收敛到 TMDB），有了 `source`
   之后，TMDB 没有而豆瓣有的条目可以以 `source=douban` 建档并刮削展示；
   同理 Bangumi、TVDB、Fanart.tv 等都是加一个 `source` 值 + 识别策略 +
   刮削器。边界只有一条：**订阅与匹配**依赖别名集合、季集结构、播出日期，
   新来源要参与订阅得先补齐这些输入，展示与播放则不受此限。

## 8. 决策记录

用户拍板（2026-09-03）：单一通用类型；平铺；缩略图入一期；播放状态不拆表；
Jellyfin 高优先级；video 库可作下载/导入目标；文件级原生能力一致；
命名暂用 `video` / 「其他」。

设计演进：v0 文件寻址草案 → v1 决策落地 → v1.1 Jellyfin 考察 → v1.2
能力档案 / 独立表 / 统一单元 → **v2 身份泛化**（本版）。v1.2 的三项改动
中，能力档案保留并简化为一个枚举一份档案；`file_metadata` 与统一
`PlaybackUnit` 因不再需要而取消。

## 9. 拍板补记（2026-09-03）

1. **接受重建 `media_item`**，引入 `source` / `external_id`（用户决策）。
2. **`kind` 的取值集合**：本期三个——`movie`（单本、2:3 海报、TMDB 电影
   id 空间）、`tv`（季集、2:3 海报、TMDB 剧集 id 空间）、`video`（单本、
   16:9 缩略图、本地）。`kind` 描述的是**内容形态**，不是题材也不是来源：
   "动漫"是 `tv` + TMDB 来源、库名叫动漫；"成人"是 `movie` 形态 +
   将来的番号来源；"家庭录像""课程""监控"都是 `video`，库名区分。
   形态才需要新值——将来若收图片是 `photo`（无时长、相册）、收音频是
   `music`（曲目/专辑），与 Jellyfin 的 `Photo` / `Audio` 类型对应，本期不做。
   存储值二选一待定：`video`（Jellyfin/Emby 词汇，与 `movie`/`tv` 同层级，
   推荐）或 `other`（Plex 词汇，即展示名）；展示名「其他」。
   **档案按 `(kind, source)` 建**（3.1），`video` 只是半个坐标，成人=`(movie, jav)`。
3. **不需要新的文件详情页**。v1.x 曾计划新建 `library/[id]/file/[fileId]`
   页面，因为当时本地文件没有 `media_item`，进不了现有的条目详情页
   （`library/[id]/item/[mediaItemId]`）。v2 里本地视频就是条目，直接复用
   现有详情页，按能力位隐藏演职员、季集、订阅入口即可。那个页面从未存在
   于产品中，只是 v1.x 草案里的一项，现已作废。

## 10. 前瞻：媒体库类型切换（2026-09-03 评估，本期不实现）

### 10.1 结论

**应当支持**，形态是一个带预览的「转换库类型」作业（与整理/转移同款：
预览 → 确认 → 持久 Job），不是库编辑表单里的一个下拉。Jellyfin / Plex /
Emby 都不允许改库类型（只能删库重建），我们能做是因为身份锚在文件行上
（`library_file` 不随类型变），而它们的条目就是路径哈希。

真实场景有三类，按可能性排：

1. `(video, local)` → `(movie, tmdb)` / `(movie, jav)`：先"能放进去就行"，
   后来想要身份与刮削（用户本人的路径）；
2. `(movie, tmdb)` ↔ `(tv, tmdb)`：建库时选错类型。今天的出口是
   `KIND_MISMATCH` 告警 + 转移到别的库 / 删库重建，转换是更自然的修法；
3. 只换 `source`：`(movie, tmdb)` → `(movie, douban)` 之类，将来多来源时出现。

### 10.2 语义：转换 = 在新档案下重扫，锚随文件携带

```
前置校验 → 预览（会变什么）→ 确认 →
  ① 记录映射基线：每个在位文件 (file_id → 旧单元 (item, s, e))
  ② 清除文件行的身份列（media_item_id / season / episode / identity_source /
     unidentified_* / resolved_version / review_suggestion）
  ③ 改 library.kind / source；校验并裁掉 scrape_overrides 中不适用于新档案的字段
     （命名模板按形态分组）
  ④ 按新档案全量扫描（现有 scan，只是档案换了）：文件逐个重新识别、建新条目
  ⑤ 观看状态迁移：对每个文件，把旧单元的 playback_state 行复制到新单元
     （多文件共享旧单元时逐一复制；目标已存在则取进度更靠后的一份）
  ⑥ 旧条目走 cleanup_orphan_items（无文件、无订阅 → 连同档案与资产删除）
  ⑦ 统计快照重算；活动记录"库《X》已从 电影 转换为 剧集，N 个文件重识别，
     M 个待识别"
```

⑤ 是转换值得做的核心：`playback_state` 键在条目单元上，条目会换，但
**文件不会**——文件行是两代条目之间唯一稳定的桥。这也是 3.3「播放状态
不按文件键」的决策不构成障碍的原因：映射在作业里做一次即可。

### 10.3 前置校验（拒绝或引导，不静默）

- **订阅**：有订阅以本库为目标库 → 拒绝，引导先把订阅改到同类型的另一个库
  （或转换向导内一键改到该形态默认库）。这是「库类型不可改」规则的
  真正来源，规则退化为"有订阅挂着时不可改"；
- **默认库**：本库是旧形态默认库 → 交接给同形态最早的一个（复用删库时的
  交接逻辑）；转换后若新形态无默认库则自动成为默认；
- **监听导入 / 手动下载规则**：引用本库 id 的规则在新形态下是否仍合法
  （自动路由规则按形态），不合法的列出并要求处理；
- **忽略口径变化**：`PLAIN` → `SCRAPED` 会让 `婚礼花絮.mp4`、`sample.mp4`
  这类文件从遍历结果消失。它们不能被当成"缺失"——预览里单列"新规则下
  将不再收录的 N 个文件"，转换时把这些行**删出台账**（不是标 missing），
  并写活动；
- **镜像产物**：旧形态下我们写进媒体目录的 `movie.nfo` / `tvshow.nfo` /
  `poster.jpg` / `fanart.jpg` / 季海报 / 分集 `-thumb` 在新形态下是错误信号
  （`_kind_conflict` 会把自己写的 `movie.nfo` 当成"放错库"）。识别"是我们
  写的"不需要新台账：NFO 由 `media_metadata` 确定性生成，**重新生成一遍
  逐字节比对**即知；图片与 `data/metadata/images/{id}/` 下的资产逐字节比对
  即知（整理器清理重复副本已用同一招）。预览列出、确认后删除；第三方
  的文件一律不碰。

### 10.4 本期必须守住的约束（为转换让路）

1. `library.kind` / `library.source` 是普通列，"不可改"只在库编辑接口层
   拒绝，不在模型/迁移层加 CHECK 或触发器；
2. 不新增任何**按库类型派生却无法重算**的状态。本期新增的列都过得了这条：
   `source`（转换对象）、`generate_thumbnails` / `exclude_from_home`（与形态
   无关）、`media_item.source`（随新条目重建）、统计快照（重算）；
3. `library_file` 继续是唯一跨代稳定的键：改名归并、回收站、观看状态映射
   都依赖它，任何"把文件行随类型重建"的念头都要拒绝；
4. 镜像写出保持**确定性**（同一份档案生成同一份字节），这是 10.3 识别自家
   产物的前提——已有的"内容比对无变化不落盘"逻辑正依赖它；
5. `cleanup_orphan_items` 的语义（无文件无订阅即删）不放宽，它是转换后
   旧条目的自然出口。

### 10.5 不做的事

- 不做"边用边改"的即时切换：转换是分钟级作业、要重识别整库，走 Job；
- 不做跨形态的**部分**转换（一个库里一半电影一半剧集）——那是两个库，
  用转移功能拆；
- 不为转换预建任何表或列。

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
