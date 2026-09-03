# 媒体库「其他」类型：不刮削内容的入账、展示与播放

> 状态：**v1.1（2026-09-03）——方案已拍板，待实施**；v1.1 增补第 1.5 节
> Jellyfin 源码考察并据此修正 3.2 / 4.1 / 4.2 / 4.3 / 4.6 / 第 5 节。v0 草案的八个待决问题
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
| `recorded_at` | 拍摄/录制时间：NFO `premiered`/`aired` → ffprobe 容器标签 `date` → `creation_time`（手机拍摄都写）→ 文件 mtime。相册排序轴。（`dateadded` 在 Kodi/Jellyfin 语义里是**入库时间**，不参与，见 1.5.2） |
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
sidecar NFO（<主干>.nfo；根元素不校验——Jellyfin 同款，见 1.5.2）
  → title / sorttitle / plot / premiered|aired / runtime / genre|tag
无 NFO → title = 文件名主干（不跑影视 NER 清洗，那套规则会把 "2019.10.01 国庆" 切碎）
recorded_at 按 3.2 的三级回落
```

必须关掉的影视规则（会误伤家庭视频的）：

- **extras 过滤**：`_IGNORE_MARKERS` 的 `sample`、`_EXTRAS_KEYWORDS` 的
  「花絮/预告片」子串匹配——`婚礼花絮.mp4` 会被当作影视花絮直接跳过；
  `_IGNORE_DIRS` 里的花絮/extras 目录名同理（`clips/`、`other/` 恰是家庭
  视频常见的分类目录）。other 库改用 Jellyfin 口径（1.5.4）：隐藏目录 +
  `@eaDir`/`metadata`/`.actors`/`lost+found` 类系统目录 + 文件名主干**精确
  等于** `sample`，不做任何子串规则、不做 extras 目录名规则；
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

1. sidecar 图，文件名对齐 Jellyfin `LocalImageProvider`（1.5.3）：横图
   `<主干>-thumb.jpg` / `<主干>-landscape.jpg` 优先（本就是 16:9 语义），
   其次 `<主干>.jpg` / `<主干>-poster.jpg`；`<主干>-fanart.jpg` 作详情页背景；
2. 容器内 `attached_pic` 封面流（ffprobe 已列出，零额外探测）；
3. ffmpeg 抓帧缩略图（4.3）；
4. 兜底：中性占位（图标 + 标题 + 时长 + 日期）。

库卡片封面：现有「氛围光货架」拼贴只取 `media_metadata.poster_file`，other
库改用 4 张最新缩略图做 16:9 变体；没有缩略图时用纯色卡。媒体库首页汇总
「N 部电影 · M 部剧集」补「K 个视频」。库内搜索补按 `title` 搜文件。

### 4.3 缩略图（P1，用户决策：体验分水岭）

扫描收尾新增 THUMBS 阶段（替代影视库的 ASSETS 阶段）：对 `thumb_file`
为 NULL 且已探测成功的在位文件抓一帧，写 `data/metadata/thumbs/{file_id}.jpg`，
参数照抄 Jellyfin Screen Grabber（1.5.3）：

```
ffmpeg -ss <10% 时长；未知时长则 10s> -i <文件> -threads N -v quiet -vframes 1
       -vf [bwdif,]scale=round(iw*sar/2)*2:round(ih/2)*2[,thumbnail=n=24][,tonemap]
       -f image2 <输出>
```

- `thumbnail=n=24` 在 24 帧里挑最有代表性的一帧（避开黑场/转场）；
- **HDR → SDR tonemap**（`zscale`+`tonemap=hable`，或 `tonemapx`）：iPhone
  录像大量是 HLG/杜比视界，不做这一步帧图发灰发白。播放侧
  `ffmpeg_args.py` 已有 tonemap 滤镜构造，复用；
- 隔行源加 `bwdif`；mpegts 容器加 `-skip_frame nokey`；
- 输出宽度上限 1280（Jellyfin 存原尺寸、出图时缩放；我们有 `image_variants`
  管线，存一档中等尺寸再按请求缩放即可），JPEG 质量中档；
- 优先级：sidecar 图 / `attached_pic` 存在则直接登记为 `thumb_file`（不抓帧）；
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
- images：VIDEO 的 `Primary` = `thumb_file`；有 `-fanart` 时给 `Backdrop`；
  库封面走 4.2 的变体；
- **`PrimaryImageAspectRatio` 必须输出**（1.5.6 第 7 条）：按缩略图真实
  宽高比给（通常 1.777…），`Video` 在 Jellyfin 里没有 2/3 的默认值，客户端
  靠这个字段决定卡片形状。现有影视条目也顺手补上（2/3 或按资产尺寸），
  与真 Jellyfin 对齐；
- `/Items/Counts`：`Video` 只计入 `ItemCount`（Jellyfin 没有 `VideoCount`）；
  Latest 逐条输出 Video、按 `created_at` 倒序、不聚合；Resume 含 `FileUnit`
  行；NextUp 不涉及；搜索按 `title`；
- 排序：`SortName` 用 `title`、`DateCreated` 用 `created_at`、
  `PremiereDate` 用 `recorded_at`；
- 有意偏离（实施时登记进 jellyfin-compat.md 的偏离清单）：不输出 `Folder`
  层级、不输出 `Photo` 条目；`/Items?parentId=<库>` 不论是否递归都返回全部
  Video。

### 4.7 必须关掉的身份级入口（守卫清单）

- 订阅目标库与自动路由：`resolve_for_subscription`、`route()`、
  import-watch 的自动路由 kind 只列 movie/tv；
- 整理（命名模板）、认领/重新识别/身份复核面板、刮削设置 tab、元数据
  刷新、收藏范围（match_rules）：other 库隐藏或 400 中文拒绝；
- 转移：仅同类型（other↔other），不改名；
- 创建库弹窗类型选项加「其他」，类型创建后不可改（现状约定）。

## 5. NFO 兼容的边界

读：只读 sidecar `<主干>.nfo`（**不读目录级 `movie.nfo`**——Jellyfin 对
`Video` 类型同样不读，见 1.5.2），根元素**不校验**（`<movie>`/`<video>`/
`<musicvideo>` 都行，Jellyfin 解析器同款），取 `title`、`sorttitle`、`plot`、
`premiered|aired|releasedate`、`year`、`runtime`、`genre`/`tag`；
`tmdbid`/`uniqueid` **忽略**（有也不建身份）；`dateadded` 是入库时间语义，
不当拍摄时间用。

写：**永不**。影视库写 NFO 的价值是把 tmdb id 交给下游；other 库没有
任何我们比用户更清楚的信息，写出只会污染用户目录。Jellyfin 对新库的
NFO saver 默认也是关的（`IsSaverEnabledByDefault` 恒 false）。

## 6. 实施计划（完整版，2026-09-03 定稿）

三期在同一个发布周期内完成，按依赖排序：一期是二、三期的地基；二、三期
互不依赖。每一项都标了代码落点与验收判据，实施时逐项勾。

### 一期：类型、入账、展示、缩略图、播放

**1.1 库类型与守卫**

| 事项 | 落点 |
|---|---|
| `LibraryKind`（movie/tv/other）枚举；`Library.is_scraped` 属性 | `movieclaw_db/models/library.py`（db 层不反向依赖 media 层，枚举放这里） |
| `library_media_kind(library) -> MediaKind`，other 抛中文错误；替换全部 8 处 `MediaKind(library.kind)` | `services/library/config.py`；调用点 `scan.py:842/3508/3670`、`organize.py:194`、`claim.py:74`、`items.py:671`、`ingest.py:1663/3690`、`schemas/library.py:231` |
| `LibraryPayload.kind` / `LibraryView.kind` 改 `LibraryKind`；`LibraryItemView.kind` 保持 `MediaKind`（只描述已识别条目） | `schemas/library.py` |
| 统计快照按类型：other 库 `stats_item_count` = 在位文件数、`stats_unidentified_count` = 0、`stats_episode_count` = 0 | `movieclaw_db/repositories/library_repo.py:146-176` |
| 全局待识别清单、身份复核清单、忽略清单排除 other 库 | `api/routes/libraries.py:638-675`，`library_file_repo.list_unidentified` |
| 守卫：订阅 `_validate_library` 拒绝 other（中文文案）；整理/认领/重识别/复核/刮削设置/元数据刷新对 other 库 400；转移仅同类型 | `subscription/core.py:1349`、`organize.py`、`claim.py`、`scan.py:3490`、`scrape_config.py`、`transfer.py:349` |
| `_KIND_NAMES` / `_KIND_LABELS` 字典补 other 或改 `.get` | `scan.py:237`、`nfo.py:31`、`import_watch_config.py:50` |
| `genres.py:64` 对 other 返回空表；`media_scrape` 资产写出按 `kind` 匹配已天然排除 | `movieclaw_media/genres.py` |
| 前端：`LibraryKind = MediaType \| "other"`；`LIBRARY_KIND_META` 加「其他」（图标 Video）→ 创建弹窗自动多一项；首页汇总补「K 个视频」；`defaultLibraryFor`；`listLibraries(kind)` | `apps/web/lib/media-types.ts`、`lib/api/libraries.ts`、`components/library-view.tsx:56/198/1233` |
| OpenAPI 基线重生成（两份 spec.json）；`mclaw_tool.py` 域说明补「其他视频库」 | `export_openapi.py`、`cli/internal/spec/data/spec.json`、`services/mclaw_tool.py:41` |

**1.2 台账与扫描分叉**

| 事项 | 落点 |
|---|---|
| `library_file` 加 `title` / `recorded_at` / `thumb_file`（可空）；`library` 加 `generate_thumbnails`（默认真）——同一个迁移 | `models/library_file.py`、`models/library.py`、`alembic/versions/` |
| `MediaSpec` 加可选 `creation_time` / `tag_date`（读 `format.tags` 的 `creation_time`、`date`、`com.apple.quicktime.creationdate`） | `services/media_probe.py:380` |
| `nfo.read_video_sidecar(path) -> VideoNfo`：不校验根元素，取 title / sorttitle / plot / premiered\|aired\|releasedate / year / runtime / genre\|tag；忽略 tmdbid | `services/library/nfo.py` |
| `_ingest_file` 按 `library.is_scraped` 分叉：other 走 `_local_video_identity`（sidecar NFO → 文件名主干；`recorded_at` = NFO → tags.date → creation_time → mtime），`media_item_id`/`unidentified_*`/`identity_source` 恒 NULL，单元 (0,0) | `scan.py:2378-2560` |
| 遍历规则按类型：other 库不做 `_IGNORE_MARKERS` 子串、`_EXTRAS_KEYWORDS`、`extras_marker` 后缀、`_IGNORE_DIRS` 中的花絮/extras 目录名；只保留隐藏目录、系统目录（`@eaDir` / `metadata` / `.actors` / `lost+found` / `#recycle`）与主干精确等于 `sample` | `scan.py:134-216, 1876-1950` |
| other 库跳过：TMDB 预取、`_kind_conflict`、路径 tmdbid、`_review_due`/`_review_identity`、ASSETS 阶段；改进 THUMBS 阶段（1.3） | `scan.py:963, 1005-1010, 1298-1352` |
| 已知行重扫：`dir_files` 里出现 `<主干>.nfo` 且当前 `title` 来自文件名时重读 sidecar（零额外目录 IO）；缺 `thumb_file` 的行进 THUMBS 队列 | `scan.py:2284`（`_refresh_known_row`） |
| 改名归并、缺失对账、回收站、实时监控、外挂字幕发现、AI 字幕 `queue_after_scan`、探测回填：不动 | — |

**1.3 缩略图**

| 事项 | 落点 |
|---|---|
| `services/library/thumbs.py`：`resolve_sidecar_thumb(file, dir_names)`（`-thumb`/`-landscape` → 同名 → `-poster`）、`has_attached_pic(spec)`、`extract_frame(file, spec) -> Path`（4.3 的 ffmpeg 参数：10% 位置、`thumbnail=n=24`、反交错、HDR tonemap 复用 `playback/ffmpeg_args.py` 的滤镜构造、mpegts `-skip_frame nokey`、宽度上限 1280、超时） | 新文件 |
| 资产目录 `data/metadata/thumbs/{file_id}.jpg`；服务路由复用元数据资产路由（长缓存头）；卡片尺寸经 `image_variants` | `services/image_variants.py`、`api/routes/images.py` |
| 扫描 THUMBS 阶段：3 路队列、strm 跳过、探测失败跳过、失败只记日志；`ScanPhase.THUMBS` 中文标签 | `scan.py:280-297, 1298` |
| 库级开关 `generate_thumbnails`；删库/删文件时清理缩略图（挂到 `cleanup_orphan_items` 同类路径） | `services/library/recycle.py`、`media_scrape.cleanup_orphan_items` |

**1.4 浏览接口与网页**

| 事项 | 落点 |
|---|---|
| `GET /libraries/{id}/videos?sort=recorded_at\|title\|added_at&offset&limit`（in_place、含观看状态、`thumb_url`）；`GET /libraries/{id}/files/{file_id}`（文件详情：台账字段 + title/recorded_at/thumb/NFO plot + 观看状态） | 新 `services/library/videos.py`；`api/routes/libraries.py`；`schemas/library.py` 新 `LibraryVideoView` / `LibraryFileDetailView` |
| 库内搜索补按 `title` 搜 other 库文件 | `services/library/items.py:554`、`components/library-search-results.tsx` |
| 库封面：other 库用 4 张最新缩略图的 16:9 变体，无图纯色卡 | `services/library/cover.py` |
| 网页：`library-detail-view.tsx` 按 kind 分叉 → 新 `library-video-grid.tsx`（16:9 卡、时长角标、进度条、排序切换、复用分页与索引条） | `apps/web/components/` |
| 新路由 `library/[id]/file/[fileId]` → `library-file-detail-view.tsx`（沉浸背景、标题/日期、事实芯片、`MediaTrackRows`、播放键、NFO 简介、`FileSection` 含回收与字幕操作） | `apps/web/app/(app)/library/[id]/file/[fileId]/page.tsx` |
| 隐藏 other 库的：待处理抽屉、认领/复核面板、整理入口、刮削设置 tab、收藏范围、订阅相关按钮 | `library-detail-view.tsx`、`library-view.tsx` |

**1.5 播放与进度**

| 事项 | 落点 |
|---|---|
| `playback_state` 多态迁移（batch 重建）：`media_item_id` 可空、加 `library_file_id` 可空 FK CASCADE、CHECK 恰有其一、两条部分唯一索引 | `models/playback_state.py`、`alembic/versions/` |
| 领域层 `PlayUnit = ItemUnit \| FileUnit`；`get_states` / `record_playback_*` / `mark_played` / `get_remembered_tracks` / `unit_runtime_ms` 按变体落列 | `movieclaw_playback/state.py` |
| `GET /playback/files/{file_id}`（title / library_id / thumb_url / recorded_at / duration）；会话、`/progress`、`/resume` 接受 `file_id`；`_visible_file` 可见性 | `api/routes/playback.py:514-643, 1269-1357`、`schemas/playback.py` |
| 网页：`/play/f/[fileId]` 路由；`play-links.ts` 加 `fileHref`；`player-page.tsx` 文件模式（信息接口、上一/下一 = 同库同排序相邻文件）；`video-player.tsx` 进度上报带 `file_id` | `apps/web/app/play/`、`lib/player/play-links.ts`、`components/player/` |
| 最近观看：`playback_recent` 增 `FileUnit` 路（标题/缩略图/进度，无剧集字段，聚合键 `file_id`）；`recent-watch-row.tsx` 跳文件详情 | `services/playback_recent.py`、`schemas/playback.py:13`、`components/recent-watch-row.tsx` |
| 活动面板 `MediaActivityTarget` 支持文件目标（已有 `file_id` 字段） | `services/playback_activity.py` |
| 起播预热 `warmup.py`、trickplay：确认按文件工作，other 库照常 | `services/playback/warmup.py`、`trickplay.py` |

**一期验收**（e2e，`tests/api/test_library_other_kind.py` 新建）：建 other 库 →
放入带 sidecar NFO、不带 NFO、`婚礼花絮.mp4`、`sample.mp4`、HLG 录像、
`.strm` 各一 → 扫描：零待识别、标题与 `recorded_at` 正确、花絮文件在账、
`sample.mp4` 不在账、缩略图落盘、strm 无缩略图 → `/videos` 排序正确 →
文件详情 → 播放会话 `file_id` → 进度落 `playback_state` 的 `library_file_id`
列 → 改名重扫进度仍在 → 最近观看出现该文件；`playback_state` 迁移升降与
存量行保留；影视库全套既有用例（scan/ingest/organize/claim/playback/jellyfin）
零改动全绿。

### 二期：Jellyfin 兼容层

| 事项 | 落点 |
|---|---|
| `EntityKind.VIDEO = 0x08`，`video_guid(file_id)` | `movieclaw_jellyfin/ids.py` |
| `video_dto(file)`：`Type: "Video"`、`MediaType: "Video"`、`IsFolder: false`、`Name = title`、`PremiereDate`/`ProductionYear` ← `recorded_at`、`RunTimeTicks`、`DateCreated`、`VideoType`、`UserData` ← `FileUnit`、`ImageTags.Primary`（缩略图）/ `BackdropImageTags`（`-fanart`）、**`PrimaryImageAspectRatio` 按真实图片尺寸**、fields 门控的 `MediaSources`/`MediaStreams`/`Path`；影视 DTO 顺手补 `PrimaryImageAspectRatio` | `catalog.py` |
| 库视图 `CollectionType: "homevideos"`，`RecursiveItemCount` = 文件数 | `catalog.py:1453-1484`、`routes/library.py:281` |
| `_entries_for_parent` LIBRARY 分支：other 库无论是否递归、`includeItemTypes` 为空或含 `Video` 都返回全部 Video；`/Items/{id}` VIDEO 分支；`Latest` 逐条不聚合按 `created_at`；`Resume` 含 `FileUnit`；`Counts` 只计 `ItemCount`；`searchTerm` 按 `title`；`sortBy` 映射 SortName/DateCreated/PremiereDate | `routes/library.py:790-895, 913-932, 1027, 1125-1145, 1290` |
| PlaybackInfo / stream / Download / File / 外挂字幕：`_files_for_ref` VIDEO 直接取该文件（MediaSource 构造本就文件级） | `routes/playback.py:68-92, 111, 255, 322, 408` |
| playstate：`_resolve_units` VIDEO → `FileUnit`；已看/收藏/进度 | `routes/playstate.py:95-159` |
| images：VIDEO 的 Primary/Backdrop；库封面走一期变体 | `routes/images.py:64-112` |
| jellyfin-compat.md 偏离清单登记：不输出 `Folder` 层级、不输出 `Photo`、库级 `/Items` 恒返回全部 Video | `docs/design/jellyfin-compat.md` |

**二期验收**：`tests/jellyfin/` 协议用例（UserViews 出现 homevideos、
Items 列出 Video、PlaybackInfo/stream 206、Playing/Progress/Stopped 落
`FileUnit`、Images 200 + 正确长宽比、Counts/Latest/Resume）；Infuse 真机：
列出视频、缩略图 16:9 显示、直连播放、进度回传、继续观看出现。

### 三期：下载与导入目标（原样落库）

| 事项 | 落点 |
|---|---|
| `resolve_save_path` / `derive_save_path`：目标为 other 库时 save_path = `{主根}`，不推导条目目录 | `services/library/routing.py:224`、`config.py:43-71` |
| 手动下载选库：`torrent_submit` 接受 other 库 id；下载目标弹窗列出 other 库（`RememberKind` 已有 `other` 桶）；Go CLI 的 kind 校验放行 | `services/torrent_submit.py`、`components/download-target-dialog.tsx`、`cli/internal/overlay/download.go:249` |
| 监听导入「指定库」为 other 时：`_ingest_entry` 走原样转移（整个下载条目硬链/复制到 `{主根}/`，保留原名，跳过识别与命名；`ingest_entry` 台账、幂等、退避不变；完成文案陈述事实） | `services/library/ingest.py:1662-1800` |
| 自动路由与自定义目录规则的 kind 维持 movie/tv；订阅目标库维持排除 other | `import_watch_config.py:202-244`、`subscription/core.py` |
| 手动下载后由实时监控/扫描入账（一期能力） | — |

**三期验收**：下载到 other 库原名落 `{主根}` → 监控入账带缩略图；监听导入
原样硬链（同 inode）→ 台账；订阅弹窗与自动路由不出现 other 库；
`download.go` 接受 other 库。

### 跨期事项

- **不需要 bump `docker/runtime-version`**：抓帧只用镜像里已有的 ffmpeg，
  无新运行时依赖；
- 迁移两个（一期 1.2 与 1.5 各一个），均单向；`playback_state` 重建前后
  行数与存量键逐一断言；
- 文档：本文随实施补「实施记录」；jellyfin-compat.md 偏离清单；README
  用户手册加「其他视频库」一节（NFO/图片文件命名惯例、缩略图开关）。

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
   `browse?path=` 与 Jellyfin FOLDER GUID，本设计的接口形态不变；
7. 图片收录：Jellyfin 的 homevideos 默认把 jpg/heic 也当 `Photo` 条目收进来
   （1.5.1），做成真正的相册。我们 v1 只收视频；要做的话是独立的类型
   （`photo`）而不是往 other 里塞，Jellyfin 也单独有 `photos` 类型；
8. 容器 `title` 标签作标题：Jellyfin 做成 opt-in（嵌入标题常是垃圾），我们
   v1 不读；有诉求时加库级开关，落在 NFO 与文件名之间；
9. NFO 里的 `watched/playcount/lastplayed`：Jellyfin 会导入为观看状态，
   我们不导（观看状态是成员维度的，NFO 里的是谁的说不清）。

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
