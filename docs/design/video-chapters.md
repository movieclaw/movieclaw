# 视频章节与场景图：探测、抓图、详情页跳播与 Jellyfin 输出

> 状态：**v1 一至三期已实施（2026-09-06）**。迁移 `d4e5f6a7b8c9`、
> `services/library/chapters.py`（有效章节、抓图、单条目刷新、懒触发、整库作业
> `library.chapter_images`）、`media_probe.parse_chapters`、详情接口
> `LibraryFileView.chapters` + `chapters_pending`、前端 `chapter-strip.tsx`、
> Jellyfin `Chapters` 与 `Images/Chapter/{index}`；回归测试
> `tests/media/test_chapters.py`、`tests/api/test_chapter_images.py`、
> `tests/jellyfin/test_chapters.py`。第四期（网页播放器章节刻度）未做。
> 实施说明：场景图的重新生成是独立动作（条目菜单「重新生成场景图」、库菜单
> 「生成场景图」/「重新生成场景图」），不挂在刷新元数据上；原「重新生成缩略图」
> 改名「重新生成封面」；合成章节输出给 Jellyfin 客户端（均为用户决策 2026-09-06）。
> 文案后续统一：用户可见的地方一律叫「章节」——详情页横排标题、计数、无名章节
> 的「第 N 章」、菜单「生成章节」——不再叫「场景」（Jellyfin 的叫法，与菜单对不上），
> 也不叫「片段」（项目里指花絮/预告这类独立短片，有歧义）（用户决策 2026-09-06）。
> 下文的「场景」仅指 Jellyfin 的功能名与源码考察。
> 本文是"按时长多抓几张剧照、
> 每张记时间点、hover 后点击从该时间点播放"这一诉求的完整设计。核心结论：
> 把它建模成 **章节（chapter）** 而不是"多张缩略图"——内嵌章节有则用之，
> 没有再按时长合成，图是章节的附属物；这样详情页横排、网页播放器进度条
> 刻度、Jellyfin 协议的 `Chapters` 字段三处共用一份数据。
> Jellyfin 源码考察基于 2026-09 master（第 2 节）；本项目现状基于当日代码。
> 关联文档：[library-other-kind.md](library-other-kind.md)（4.4 主图抓帧）、
> [jellyfin-compat.md](jellyfin-compat.md)（5.3 字段映射、偏离清单）、
> [web-player.md](web-player.md)（§6.5 trickplay）、
> [persistent-jobs.md](persistent-jobs.md)、[metadata.md](metadata.md)。

## 0. 目标与定位

用户诉求：条目页现在只有一张主图（10% 处抓帧或 sidecar 海报），希望
按视频时长多抓几张剧照，每张记录时间点，展示在条目页；hover 到某张时
可以点击，直接从那一帧的时间点开始播放。

定位一句话：**这是 Jellyfin / Emby 的「场景」（Scenes）功能**。Jellyfin
详情页的场景区块就是"每个章节一张图 + 点击从该章节起播"，图来自容器内嵌
章节，没有章节时按固定间隔合成"虚拟章节"。Plex 的 Chapters 栏、Kodi OSD
的章节列表、YouTube 的 Key moments 是同一心智。我们对标 Jellyfin，因为
兼容层本来就在模仿它的协议，章节做成 Jellyfin 语义后 Infuse、Swiftfin、
Jellyfin 官方客户端可以直接消费。

三条产品原则：

- **章节是文件的属性，不是作品的属性**。同一条目的两个版本（1080p 与
  4K）章节位置可以不同；剧集每一集各有各的章节。数据挂 `library_file`。
- **文件级原生能力对所有库一致**（library-other-kind.md §0）：电影库、
  剧集库、其他库同等享有，TMDB 条目与本地条目同等享有。
- **图是锦上添花，章节列表本身就有用**。没抓到图的章节仍在详情页显示
  时间点并可点击跳播，仍输出给 Jellyfin 客户端做章节跳转。

## 1. 产品形态

### 1.1 条目详情页「场景」横排

位置：简介之下、分集横排/文件区之上（`library-item-detail-view.tsx`
的 `ExpandablePlot` 与 `SeasonEpisodesSection` 之间）。

- 每张卡 16:9，左下角时间戳角标（`00:12:30`），卡下一行章节标题；
  没有标题的章节（合成章节、无名内嵌章节）只显示时间戳。
- 图未生成时卡片为深色占位 + 时间戳，仍可点击。
- **看图优先，播放键不常驻**（用户决策 2026-09-06）。桌面：hover 浮起并
  显示中央播放键，点播放键从该点起播，点图片其他区域进灯箱；触摸屏：
  没有 hover，直接点卡片进灯箱。灯箱复用 `image-lightbox.tsx`（已支持
  16:9 剧照形态与底部缩略图条），左右切换章节，底部一行「从 00:12:30
  播放」按钮——大图与播放键都在灯箱里，小卡片上什么都不盖。
- 点击起播：`rememberPlayerReturnPath()` 后 `router.push(playHref(id, {season,
  episode, tSeconds}))`。**`?t=<秒>` 跳播链路今天已端到端存在**
  （`play-links.ts:24` → `play/[mediaItemId]/page.tsx` 的 `startMsOverride`
  → `POST /playback/sessions` 的 `start_ms` → 关键帧对齐），前端零新协议。
  起播时间用 `frame_ms`（图上那一帧的真实时间，§4.3），不是章节的名义
  起点——用户点的是"这一帧"，进度条就要落在这一帧。
- 卡片可键盘聚焦，Enter 进灯箱。
- 上次看到的位置落在哪一章，那张卡右上角标「上次看到这里」，横排初始
  滚动到它；数据来自详情页已有的观看状态，零新接口。
- 横排跟随当前选中的文件：电影随版本选择器（`selectedTrackFileId`），
  剧集随分集横排里选中的一集（`selectedSeriesEpisode`）。没有在位文件、
  原盘目录（bluray/dvd）、strm 时整个区块不渲染。
- 章节数多（内嵌章节 30+）时横排可横滚，不分页不折叠。

### 1.2 网页播放器（第四期，可选）

进度条上按章节起点画刻度；hover 刻度显示章节标题 + trickplay 帧；
OSD 左上显示当前章节名；`[` / `]` 键上一章/下一章。数据与详情页同源。

### 1.3 Jellyfin 客户端

`Fields=Chapters` 时 DTO 带 `Chapters[]`，章节图经
`/Items/{id}/Images/Chapter/{index}` 取。jellyfin-web 详情页出「场景」
区块，Infuse / Swiftfin / Android TV 播放器内出章节列表与章节跳转。
HLS 转码不携带容器章节（Jellyfin 自己也是 `-map_chapters -1`，
`DynamicHlsController.cs:1642`），所以远程播放器**只能靠 DTO 拿章节**，
这一字段是章节能力在第三方播放器上的唯一通路。

## 2. Jellyfin 源码考察（章节机制怎么做的）

只记与本方案直接相关的事实，文件路径相对 jellyfin 仓库根。

### 2.1 数据模型与协议

- `ChapterInfo`（`MediaBrowser.Model/Entities/ChapterInfo.cs`）：
  `StartPositionTicks`（100ns tick）、`Name`、`ImagePath`（服务器本地路径）、
  `ImageDateModified`、`ImageTag`。
- `BaseItemDto.Chapters: List<ChapterInfo>`（`Dto/BaseItemDto.cs:566`），
  **受 fields 门控**：`DtoService.cs:1358` 仅在
  `options.ContainsField(ItemFields.Chapters)` 时填充；单条目接口全字段
  语义下自然带出。
- `ImageType.Chapter = 10`（`Entities/ImageType.cs:65`）。章节图走通用
  图片路由 `GET /Items/{itemId}/Images/Chapter/{imageIndex}`
  （`ImageController.cs:630`），`imageIndex` = 章节序号；
  `BaseItem.GetImageInfo(ImageType.Chapter, index)`（`BaseItem.cs:2411`）
  经 `ChapterManager.GetChapter(id, index)` 取 `ImagePath`。章节图不能
  用 `SetImage` 设置（`BaseItem.cs:2265` 抛异常）——只能由抽帧产生。
- `ImageTag` 在 `ChapterRepository.Map` 里按 `(条目路径, ImageDateModified)`
  算缓存标签，仅当 `ImagePath` 非空时给出。
- 库选项 `EnableChapterImageExtraction`、`ExtractChapterImagesDuringLibraryScan`
  （`LibraryOptions.cs:51-53`）；服务器配置 `DummyChapterDuration`（秒，
  ≤0 关闭）与 `ChapterImageResolution`（默认 `MatchSource`）
  （`ServerConfiguration.cs:259-268`）。

### 2.2 探测与虚拟章节

- ffprobe 的 `chapters[]` → `ChapterInfo`（`ProbeResultNormalizer.cs:1598`）：
  `tags.title` 作 `Name`，`start_time` 秒取整到毫秒。
- `FFProbeVideoInfo.cs:279`：`DummyChapterDuration > 0 && chapters.Length <= 1
  && 有视频流` 时用 `CreateDummyChapters` 从 0 起按固定间隔合成，
  片长 > 12h 视为损坏抛错。**只有 0 或 1 个章节都算"没有章节"**。
- `NormalizeChapterNames`（`:300`）：名字为空或能解析成时间
  （某些压制工具把时间戳写进标题）→ 替换为本地化的 `Chapter N`。
- 章节列表在 `SaveChapters` 时丢弃 `StartPositionTicks >= RunTimeTicks` 的。

### 2.3 抽帧

`ChapterManager.RefreshChapterImages`（`Emby.Server.Implementations/Chapters/ChapterManager.cs`）：

- 资格：非占位/快捷方式、完整媒体、有视频流、库开关打开。
- 章节平均间隔 < 1s 时跳过抽帧（碎片化章节）。
- **第 0 章从 15s 处抓**（`_firstChapterTicks`），避免片头黑场；其余章节
  精确到章节起点。
- 起点 ≥ 片长的章节停止。
- 图片路径 `metadata/<item>/chapters/{DateModified}_{ticks}.jpg`
  （`PathManager.cs:112`），文件修改时间变了路径就变，旧图由
  `DeleteDeadImages` 清理。
- 抽帧命令（`MediaEncoder.cs:674-771`）：`-ss <t>` 输入侧定位、
  `-skip_frame nokey` 只解关键帧、滤镜 `deint → scale → thumbnail=n=24
  → tonemap`、`-vframes 1`；分辨率按 `ChapterImageResolution`。
- 时机：扫描中仅在 `ExtractChapterImagesDuringLibraryScan` 打开时抽；否则
  由计划任务 `ChapterImagesTask` 每日 02:00 跑、最长 4h，失败的
  `路径+mtime` 记进 `chapter-failures.txt` 不再重试。

### 2.4 对本方案的吸收

| Jellyfin 做法 | 本方案 |
|---|---|
| 章节挂条目（Video/Episode 各自是 BaseItem） | 章节挂 `library_file`；DTO 取单元首文件的章节（与 `MediaSources[0]`、`Path` 同源） |
| `Fields=Chapters` 门控、单条目全开 | 照抄，`DtoOptions.has("Chapters")` |
| `/Items/{id}/Images/Chapter/{index}` | 照抄，`_resolve_asset` 增 `chapter` 分支 |
| 虚拟章节固定 5 分钟一段（默认关闭） | **按时长定张数**（§4.2），因为我们的首要展示面是详情页横排，24 张塞不下 |
| ≤1 个章节视为无章节 | 照抄 |
| 名字为空/时间戳 → `Chapter N` | 存储侧置 `null`；控制台显示时间戳，Jellyfin DTO 补 `第 N 章` |
| 第 0 章 15s 抓、平均间隔 <1s 跳过、越界停止 | 照抄 |
| `thumbnail=n=24` | 改 `n=5`：只解关键帧时 24 个关键帧可能已离章节起点数分钟，图与时间戳对不上 |
| 图的真实帧时间不记录，跳播用章节起点 | `showinfo` 记 `frame_ms`，控制台跳播落在图上那一帧 |
| 抽帧失败整文件作废 | 部分产物落库，超时预算内能抓几张是几张 |
| 分辨率跟源 | 固定宽 960（§4.4） |
| 图片路径含 mtime | 路径按 `start_ms` 命名，失效靠文件 `file_mtime_ns` 变化触发重抓 |
| 每日计划任务 + 失败记忆文件 | 持久化 Job（§4.5），失败的章节记墓碑、由 force 重试 |
| `ImagePath` 输出到客户端 | **省略**（服务器内部路径对客户端无意义），登记为偏离 |

## 3. 本项目现状

- **探测**：`media_probe.probe_media()` 跑 `ffprobe -show_format -show_streams`
  （`media_probe.py:74-83`），**没有 `-show_chapters`**，仓库里没有任何
  章节相关代码。规格落 `LibraryFile` 的 `resolution/…/subtitle_streams`
  （`ingest.py:2490-2514`），JSON 列三态惯例见 `external_subtitles`
  （`library_file.py:205-216`：NULL=未探测，[]=探测过但没有）。
- **主图**：`thumbs.ensure_local_assets()` 只对 LOCAL 条目、只一张
  （10% 处，`_grab_frame` 有关键帧/全解码/从头三级回退与 HDR 色调映射链）。
  产物 `data/metadata/images/{item}/poster.jpg`。库开关 `generate_thumbnails`。
- **trickplay**：`playback/trickplay.py` 按 10s 间隔出 320 宽雪碧图，只在
  开播放会话时后台生成，不能当场景图，但 `-skip_frame nokey` 思路照搬。
- **资产服务**：`/images/assets/{path}` 按路径首段（media_item_id）做可见性
  校验（`routes/images.py:83-85`），支持 `variant=landscape-card`
  （480×270 webp，`image_variants.py:44`）与 `v=` 不可变缓存。
- **跳播**：`playHref(id, {tSeconds})` → `?t=` → `start_ms`，已存在。
- **Jellyfin**：`catalog.py` 的 `movie_dto`/`episode_dto` 不输出 `Chapters`；
  `routes/library.py:295-296` 把 `EnableChapterImageExtraction` 硬编码为
  False；`routes/images.py:_resolve_asset` 对 primary/backdrop 之外一律 None。
- **Jobs**：`jobs.register_job_handler`，扫描/刷新/整理都是持久化 Job；
  扫描有独立的 `ScanPhase.ASSETS` 阶段（`scan.py:1373-1400`），但只覆盖
  本轮新挂锚的条目，**存量条目不经过它**——这决定了场景图不能只挂在扫描上。
- ffmpeg 已在 Docker 镜像（jellyfin-ffmpeg7），本方案**不改运行时依赖，
  不 bump `docker/runtime-version`**。

## 4. 设计

### 4.1 章节的定义与来源

一个文件的**有效章节列表**由纯函数决定，不落库：

```
effective_chapters(file):
    embedded = file.chapters            # 探测事实（§4.3），NULL 视为 []
    if len(embedded) >= 2: return embedded       # Jellyfin：≤1 个视为没有
    return synthesize(file.duration_seconds)     # §4.2；时长未知 → []
```

内嵌章节的标题规范化在探测时做：空、纯空白、或能被解析为时间
（`HH:MM:SS(.mmm)`）的标题存 `null`。合成章节标题恒 `null`。

### 4.2 合成策略：按时长定张数

| 时长 D | 张数 N |
|---|---|
| < 90 s | 0（只留主图） |
| 90 s ～ 10 min | 3 |
| 10 ～ 40 min | 6 |
| 40 ～ 90 min | 8 |
| 90 ～ 180 min | 10 |
| > 180 min | 12 |

起点 `t_i = D × (0.06 + 0.88 × i / (N − 1))`，即在 6% ～ 94% 之间等距，
掐掉片头 logo/黑场与片尾字幕；`end_ms` 取下一章起点（末章取 D）。
表放模块常量 `_SYNTH_TABLE`，改档位不需要迁移与重探测（§4.3 分列的收益）。

不做场景检测（`scenedetect` 要全解码，网络挂载不可接受），不做画面
"代表性"评估——等距 + 只解关键帧在场景图用途上够用，Jellyfin 亦如此。

### 4.3 存储模型

`library_file` 增两列（迁移仅加列，向前兼容）：

```
chapters        JSON NULL   -- 探测事实。NULL=未探测（旧行）；[]=探测过没有
                            -- 元素 {"start_ms": int, "end_ms": int|null, "title": str|null}
chapter_images  JSON NULL   -- 抓图状态。NULL=没抓过；[]=抓过无产物（无章节/跳过/失败）
                            -- 元素 {"start_ms": int, "frame_ms": int, "image": "<相对 assets_root 的路径>"}
                            -- start_ms 是与有效章节 join 的键；frame_ms 是图上那一帧的真实时间
```

两列分开的理由与 `external_subtitles` 相同：数据来源与失效键不同
（章节=容器头，图=抽帧产物），各自刷新互不牵连；合成策略调整、抓图
失败都不会动探测事实。图按 `start_ms` 与有效章节列表 join；有效列表变了
（比如内嵌章节从 1 个变 3 个），对不上的图是死图，下次抓图清理。

`frame_ms` 的用法：只解关键帧时抓到的是章节起点之后最近的关键帧，可能
晚几秒。控制台的跳播与角标一律用 `frame_ms`——用户看到哪一帧就从哪一帧
播。Jellyfin 的 `StartPositionTicks` 对内嵌章节用 `start_ms`（章节起点是
权威语义，播放器"下一章"要跳到那里），对合成章节用 `frame_ms`（名义起点
本来就是算出来的，没有比"图上这一帧"更好的定义）。

图片路径：`{media_item_id}/chapters/{file_id}/{start_ms:010d}.jpg`，
落在 `assets_root()` 下。首段是 media_item_id，`/images/assets` 的
可见性校验与 `v=` 缓存直接可用；文件从条目下移走/转移时随条目目录处理。

`library` 增一列：`extract_chapter_images BOOLEAN NOT NULL DEFAULT 1`。

### 4.4 抓图

放 `services/library/chapters.py`，与 `thumbs.py` 共用滤镜构造（把
`_grab_frame` 的 `bwdif` + HDR tonemap 链抽成 `frame_filters(hdr)`）。

每章一条命令（实测 3 张 0.3s；`-ss` 输入侧定位只读关键帧附近的数据，
对网络挂载比单次通读 `select` 友好）：

```
ffmpeg -v info -y -skip_frame nokey -ss <t> -copyts -i <file> -an -sn \
  -vf "bwdif=mode=send_frame:deint=interlaced,[tonemap…,]thumbnail=n=5,scale='min(960,iw)':-2,format=yuv420p,showinfo" \
  -frames:v 1 -q:v 4 <dest>
```

规则（照抄 Jellyfin §2.3，差异已在 §2.4 登记）：

- 第 0 章从 `min(15s, D)` 处抓；其余章节 `t = start_ms / 1000`。
- `start_ms >= D` 的章节停止；平均间隔 < 1s 跳过整个文件；有效章节数
  > 48 只记列表不抓图（KTV 合集这类，横排也放不下）。
- 原盘目录（container ∈ bluray/dvd）、strm、非在位文件（`in_place()`
  为假）不抓。
- 宽 960：横排卡 480 变体够用，灯箱/hover 放大时 960 不糊；比主图 1280
  小是因为一部片 8～12 张。
- 单文件内串行；跨文件并发由 Job 并发数控制；与 `thumbs.py` 共享一把
  `asyncio.Semaphore(2)` 的 ffmpeg 闸，避免扫描期 CPU 被打满。
- 失效：`chapter_images` 里记录时的 `file_mtime_ns` 不必单独存——Job 用
  "**没抓齐**（`stills_complete` 为假）或 force"选目标，文件内容变更走扫描的
  重探测路径把 `chapters` 与 `chapter_images` 一并置 NULL。
- **真实帧时间**：滤镜链末尾加 `showinfo`，从 stderr 解析被选中那一帧的
  `pts_time` 得到 `frame_ms`（`thumbnail` 选帧后 showinfo 只打印一行）。
  必须带 `-copyts`，否则输入侧 `-ss` 会把时间戳归零、`pts_time` 变成相对
  定位点的偏移。实测：测试片 `-ss 45` 时 `thumbnail=n=5` 选中的是 52s 的
  关键帧——7 秒的漂移正是要记 `frame_ms` 的理由。解析失败退回 `start_ms`，
  不影响出图。
- **部分产物也落库**：单条命令超时 60s，单文件总预算 5 分钟；预算耗尽
  把已抓到的写进 `chapter_images`，没抓到的章节没图但仍在列表里。
  比"整个文件算失败"好——网络抖动时用户至少看到前几张。
- **抓取顺序按新近优先**：Job 目标按 `library_file.created_at DESC`，刚入库、
  用户最可能去看的先有图；单条目懒触发（§4.5）再兜住点开的那一部。
- **体积**：JPEG `-q:v 4`，960 宽一张约 60～90KB，一部片 10 张 ≈ 0.8MB，
  1000 部 ≈ 0.8GB（对照 metadata.md §289 的海报估算 2.3GB）。发版说明写明。
- 失败（超时/损坏/编码不支持）给那一章记一条**墓碑** `{"start_ms",
  "failed": true}` 并记中文日志：没有 `image`，消费方照旧当"这章没图"，
  只有补缺判据认它。没有墓碑，"只跳过抓齐的"会让每一轮作业、每一次打开
  详情页都对着同一个抓不出图的文件重跑一遍。force 时不复用墓碑，仍重试。
  ffmpeg 整个缺失是另一回事：什么都不写（保持 NULL），装好后自动补。
- **孤儿清理**：文件行被删除/洗版替换后，`{item}/chapters/{old_file_id}/`
  成为孤儿。Job 处理某条目时顺手删掉该条目下不再对应任何在位文件行的
  chapters 子目录；条目删除时随资产目录一起清。

### 4.5 触发时机

**章节列表**：`probe_media()` 的 ffprobe 加 `-show_chapters`，零额外 IO，
`MediaSpec` 增 `chapters` 字段，5 处落库点（`ingest.py:1727/2068/2381/2477`、
`scan.py:2402/2454/2500`）一并写入。存量行 `chapters IS NULL` 由抓图 Job
顺带补探（只跑 `ffprobe -show_chapters`，读容器头，毫秒级）。

**场景图**：新持久化 Job `library.chapter_images`（`register_job_handler`），
输入 `{library_id, force}`，目标 = 库内在位文件中**还没抓齐**的
（force 时全部），`dedupe_key` 按库，低优先级，进度分子分母=文件数，
`raise_if_cancelled` 逐文件检查、可停可续。

**补缺的判据是"抓齐没有"而不是"抓过没有"**（`stills_complete`）：一个文件算
齐，当且仅当计划抓图的每一章都有结论——图登记在台账里**且文件还在磁盘上**，
或者留了抓不出来的墓碑；按规则整体不抓图的（没有章节、章节过密、章节过多）
也算齐。只看 `chapter_images IS NULL` 会漏掉三种半成品，它们再也等不到补缺：
单文件预算耗尽写回的部分产物（§4.4 明说部分产物落库，却没有"下次接着补"的
另一半）、事后没了的图文件（清过资产目录、只恢复了数据库备份、迁移丢图）、
以及当时该有图却写成 `[]` 的行（抓帧全失败、时长当时没探出来）。判据与抓图
计划共用同一个 `still_plan`——两处各写一遍，迟早出现"判定没抓齐、真去抓又抓
不出那一张"的死循环。代价是目标筛选从一条 SQL 变成取回候选行在 Python 里判
（含一次列目录的磁盘 IO，放线程里做）。

**断点**（重启/应用内更新会把 Job 退回队列、处理器整体重跑一遍）：补缺模式
下逐文件写回台账即是检查点，重取目标时 `stills_complete` 自然排除已
完成的行；**force 重抓每一行都要重做，台账不再是检查点**，因此把"上一轮最后
一个处理完的文件"记进进度的 `details.cursor`，恢复时按同一份排序跳过它之前
的行（节流窗口内的那一两个文件会重做，抓图幂等）。进度的 `current`/`total`
一律是整轮作业的累计口径而不是本次执行的：重启后从 0 数到"剩下的文件数"，
用户看到的就是又从头跑了一遍。

1. 扫描结束后自动入队（库开关打开时）——覆盖新文件与存量回填；
2. 库管理菜单「生成场景图」/「重新生成场景图」（force）；
3. 条目菜单「重新生成场景图」（`POST /libraries/{lib}/items/{id}/chapter-images`）：
   该条目全部在位文件 force 重抓，前端按 `chapters_pending` 轮询。**这一条也是
   持久化 Job**（`media.chapter_images`，按条目去重、与整库那份共用同一套断点
   与进度口径）：一部剧几十集重抓要跑很久，用户点了就该在任务中心看到它、能停
   它，重启也不能白跑。`chapters_pending` 因此要同时看内存里的懒触发标记与该
   条目未完成的作业——只看前者，重启后前端会以为没在跑而停止轮询。
   **与刷新元数据相互独立**（用户决策 2026-09-06：重新生成章节应独立，
   条目与库两级都要有）；库菜单同样分「生成场景图」（补缺）与「重新生成
   场景图」（force）两项。原「重新生成缩略图」菜单改名「重新生成封面」，
   后端进度短语同步（封面 = 主图，与场景图是两件事）。

4. **详情页懒触发**（**不做成 Job**：用户没发起任何动作，一次次打开详情页
   却在任务中心堆出一串条目作业既是噪音、也违背 persistent-jobs.md 的"Job 只
   承载用户可感知、需要追踪的异步业务"；重启把它丢了也无妨，下次打开原地再
   触发，已抓齐的文件靠台账不会重做）：`GET /libraries/{lib}/items/{id}` 发现有在位
   文件**没抓齐**（判据与整库作业同源）且库开关打开时，`asyncio.create_task(
   refresh_chapter_images(item_id))`，用 `_in_flight` 集合去重（与
   `trickplay.py:58` 同款）；响应里 `chapters_pending: true`，前端每 3 秒
   重拉详情、最多 20 次，图一张张补上。这是升级后第一次打开旧条目的体验
   保障——不用等整库 Job 排到它。

不放进 `ScanPhase.ASSETS`：那一阶段只覆盖本轮新挂锚条目，且一部电影
8～12 次 seek 乘以整库会把"扫描"拖长数倍；独立 Job 让扫描进度语义不变，
用户也能单独停掉它。

**库开关** `extract_chapter_images` 默认开：抓图在后台低优先级 Job 里，
网络挂载大库的用户可以关；关掉后已生成的图保留（与 Jellyfin 删图的做法
不同——删图是破坏性的，用户开关一次不该丢产物）。

### 4.6 控制台接口与前端

`LibraryFileView` 增字段：

```
chapters: list[ChapterView] | None    # null=尚未探测（ffprobe 缺失/文件不可达）
chapters_pending: bool                # 正在后台抓图（§4.5 懒触发），前端据此轮询
ChapterView = {index, start_ms, end_ms, frame_ms, title, synthetic, image_url}
image_url = "/images/assets/{path}?v={mtime}" | null
```

前端跳播、角标、灯箱标题都用 `frame_ms`（无图时退回 `start_ms`）。

`build_item_detail()` 不触发 ffprobe（保持"探测只在入库/扫描时做"），
只读两列做 join。`SeasonEpisodesView` 不动：分集横排选中某集后，前端从
`files` 里按 `file_ids` 找到该集文件的 `chapters`。

前端新组件 `chapter-strip.tsx`（§1.1），复用 `poster-card.tsx` 的横卡
比例逻辑与 `image-proxy.ts` 的 `imageUrl(path, "landscape-card")`。
Agent 工具无需改动：`spec.json` 重导出后 `library.items.get` 自动带出
章节（`scripts/export-spec.sh` 两份都要刷）。

### 4.7 Jellyfin 输出

- `movie_dto` / `episode_dto`：`options.has("Chapters")` 时输出
  `Chapters`，数据取该单元首文件的有效章节列表（与 `MediaSources[0]`、
  `Path`、`DateCreated` 同一个 `files[0]`）：

  ```
  {"StartPositionTicks": start_ms * 10_000,
   "Name": title or f"第 {i+1} 章",
   "ImageTag": _asset_tag(rel, file.updated_at),   # 仅有图时
   "ImageDateModified": ...}                        # 仅有图时
  ```

  合成章节同样输出（等价于 Jellyfin 打开了 DummyChapterDuration），
  这正是"让播放器捕捉到章节"的诉求；Infuse 的章节跳转会按这些点跳。
- `_list_load_columns()` 仅在 `has("Chapters")` 时加载两列，列表请求不
  多读 JSON。
- `routes/images.py:_resolve_asset` 增 `chapter` 分支：ITEM/EPISODE GUID +
  `image_index` → 单元首文件 → 有效列表 `[index].image`；越界或无图
  404 `Item does not have an image of type Chapter`。缩放参数与变体缓存
  沿用 `_maybe_scaled`。不需要新的 `EntityKind`。
- `routes/library.py` 的 `LibraryOptions.EnableChapterImageExtraction`
  改为反映库开关；`ExtractChapterImagesDuringLibraryScan` 保持 False。
- jellyfin-compat.md 更新：5.3 的 fields 门控清单加 `Chapters`；偏离清单
  追加 ⑫ "`ChapterInfo.ImagePath` 省略"；`Trickplay` 仍在绝不输出清单。

### 4.8 网页播放器（第四期）

`player-controls.tsx` 已有 trickplay hover 预览（`:260`），加章节刻度与
标题只是在同一个 hover 状态上多画一层；`[`/`]` 快捷键与 OSD 章节名
读同一份 `chapters`。播放页需要多拿一次当前文件的章节：沿用详情接口
或在 `PlaybackSession` 响应里带 `chapters`（后者少一次请求，倾向后者，
实现时定）。

## 5. 分期与验收

| 期 | 内容 | 验收 |
|---|---|---|
| 一 | 迁移（3 列）；`probe_media` 加 `-show_chapters` 与标题规范化；`MediaSpec.chapters`；5 处落库；`effective_chapters` + `synthesize` 纯函数 | 单测：ffprobe JSON 夹具解析、标题规范化（空/时间戳）、合成表每档位与边界（89s/90s/…）、≤1 章视为无；新入库文件 `chapters` 非 NULL |
| 二 | `chapters.py` 抓图（含 `frame_ms`、部分产物）；Job `library.chapter_images` 与四处入口（含详情页懒触发）；库开关（模型/schema/config/repo/表单）；`LibraryFileView.chapters`；`chapter-strip.tsx` + 灯箱起播 + 「上次看到这里」角标 | ffmpeg 合成测试片（lavfi `testsrc` + FFMETADATA 章节，见附录）跑完整链路：列表→图→详情接口→点击后 `start_ms` 正确；无 ffmpeg 环境按现有约定跳过 |
| 三 | Jellyfin `Chapters`、章节图路由、LibraryOptions、compat 文档 | `tests/jellyfin`：不传 fields 无 `Chapters`；传了有；单条目全开有；图路由 200/越界 404；ImageTag 仅有图时出现。Infuse 手工验证章节列表与跳转 |
| 四 | 播放器章节刻度/快捷键/OSD | 手工验证 |

一、二期合一个 PR 也可以；三期独立 PR 便于对照 compat 文档评审。

## 6. 风险与开放问题

- **首轮回填成本**：存量 1000 部电影 ≈ 8000～12000 次 seek，本地盘约
  20 分钟，NAS 可能一两小时。在低优先级 Job 里、可停、可关开关，可接受；
  发版说明要写明。
- **合成章节输出给 Jellyfin 客户端**：Infuse 的"下一章"会跳到等距点而
  不是真实场景切换。这与 Jellyfin 开虚拟章节的体验一致，且不输出就没有
  章节跳转可用。若评审倾向保守，改为"仅内嵌章节输出、合成章节只在控制台
  展示"只需在 `movie_dto` 里加一个条件——**待用户定夺，本文默认输出**。
- **`showinfo` 解析依赖 stderr 格式**：ffmpeg 各版本 `showinfo` 输出格式
  稳定（`pts_time:` 字段十多年未变），但仍是文本解析；解析失败退回
  `start_ms`，只损失几秒精度。
- **磁盘**：1000 部约 0.8GB（§4.4）。应用内更新的自动备份若包含
  `data/metadata`，备份时长随之增加——实现时确认备份范围。
- **多版本条目**：Jellyfin DTO 只能带一份章节，取首文件；控制台按选中
  版本切换，无此限制。
- **主图是否改从场景图里选**：`thumbnail=n=24` 在 10% 处的主图与场景图
  各抓各的，本方案不合并。后续可以让 LOCAL 条目的主图直接取方差最大的
  一张场景图，省一次抓帧——留作后续。

## 7. 守卫清单

| 守卫 | 位置 |
|---|---|
| 迁移只加列、带默认值，可回退 | `alembic/versions/…_video_chapters.py` |
| 不改运行时依赖，`docker/runtime-version` 不动 | 评审对照 |
| 原盘/strm/非在位文件不抓图 | `chapters.py` 资格判断 |
| ≤1 个内嵌章节 → 合成；时长未知 → 空 | `effective_chapters` 单测 |
| 章节 > 48 或平均间隔 < 1s → 只列表不抓图 | `chapters.py` 单测 |
| 重启后不重抓已完成的文件，进度不归零 | `test_library_job_resumes_after_restart` |
| 条目重抓是可恢复 Job，排队期间详情页仍轮询 | `test_item_regenerate_route_enqueues_resumable_job` |
| `Chapters` 受 fields 门控、单条目全开 | `tests/jellyfin` |
| 列表请求不加载章节 JSON 列 | `_list_load_columns` |
| 详情接口不触发 ffprobe | `build_item_detail` |

## 附录：测试片与实测

生成带章节的 60s 测试片（第 2 章标题故意写成时间戳，测规范化；第 3 章
无标题）：

```
;FFMETADATA1
[CHAPTER]
TIMEBASE=1/1000
START=0
END=20000
title=Opening
[CHAPTER]
TIMEBASE=1/1000
START=20000
END=45000
title=00:00:20.000
[CHAPTER]
TIMEBASE=1/1000
START=45000
END=60000
```

```
ffmpeg -f lavfi -i "testsrc=size=640x360:rate=25:duration=60" \
  -f lavfi -i "sine=frequency=440:duration=60" -i chapters.txt \
  -map 0 -map 1 -map_metadata 2 -c:v libx264 -preset ultrafast -g 50 -c:a aac -shortest test.mkv
ffprobe -v error -print_format json -show_chapters test.mkv
```

ffprobe 输出 `chapters[].{id,time_base,start,start_time,end,end_time,tags.title}`，
`start_time` 为秒字符串；无章节文件输出 `"chapters": []`。`-c copy` 转
mp4 章节保留；HLS 分片无章节。逐章 `-ss` + `-skip_frame nokey` 抓 3 张
0.33s，单次通读 `select` 0.18s（但要读完整个文件的关键帧）。
`-ss 20 -copyts … showinfo` 报 `pts_time:20`，`-ss 45` 报 `pts_time:52`
（`thumbnail=n=5` 在 5 个关键帧里选了最后一个）；不带 `-copyts` 报的是
`0.023` 这种相对偏移。
