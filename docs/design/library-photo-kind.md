# 媒体库「图片」类型：照片的入账、瀑布流展示与全屏查看

> 状态：**v1 已实施（2026-09-06）**。产品方向由交互 demo 确认：瀑布流
> （按月分组、组内最短列放置）、点击全屏查看。落点：`profile.py`（档案行与
> 三个能力位）、`media_probe.probe_image`、`thumbs._build_image_thumbnail`、
> `items.build_library_index(sort)`、`/libraries/files/{id}/original`、
> `watch.py` 按库扩展名判定、前端 `photo-wall.tsx` / `photo-lightbox.tsx`；
> 回归测试 `tests/api/test_library_photo_kind.py`。
> 实施时与定稿方案的两处偏离见第 6 节。
> 关联文档：[library-other-kind.md](library-other-kind.md)（形态 × 来源能力档案，
> 本文所有复用的地基）、[library.md](library.md)、[library-manage.md](library-manage.md)、
> [library-file-recycle.md](library-file-recycle.md)、[member-management.md](member-management.md)。

## 0. 目标与定位

用户诉求：媒体库支持图片类型——监听并扫描目录里的所有图片，进库后以
Pinterest 式的瀑布流展示，混合长宽比要排得好看，浏览中可点击放大全屏看。

定位一句话：**图片库是「其他」库的同构体——条目就是文件，只是文件不可播、
只可看。** 它复用「其他」类型那次改造铺好的全部地基（`(kind, source)` 能力
档案、本地来源条目、主图记真实宽高、`primary_aspect`、按内容时间排序），
新写的只有四块：Pillow 探测、Pillow 缩略图分支、原图路由、前端瀑布流墙。

两条产品原则（沿用 library-other-kind.md）：

- **文件级原生能力一律保持一致**：可见范围、回收站、缺失清单、库封面、
  实时监听、统计，图片库与其他库同等享有；只有作用于「播放」的能力
  （播放器、进度、最近观看、Trickplay、字幕）与它无关；
- **不引入目录层级**：库内平铺，按拍摄时间倒序、按月分组；目录只是磁盘上
  的组织方式。

一期不做：HEIC/HEIF（Pillow 原生不支持，引入 pillow-heif 要 bump
`runtime-version`，二期再议）、相册层级、Jellyfin 暴露、EXIF 面板之外的
元数据（GPS/镜头参数）、重复照片识别。

## 1. 复用清单：链路逐环核对

| 环节 | 现状 | 图片库 |
|---|---|---|
| 能力档案 | `profile.py` 按 `(kind, source)` 建表，消费方读能力位 | 加一行 `("photo","local")`，加两个能力位（2.1） |
| 数据模型 | `library.kind` / `media_item.kind` 裸字符串列 | **零迁移** |
| 扫描遍历 | `_walk_videos(plain=…)` 已有本地库忽略口径 | 扩展名集合改从档案取（2.2） |
| 探测 | `probe_media` → `MediaSpec`；预取、失败记忆、退避补探全围着它 | 前置一层按后缀分派到 `probe_image`，**返回同一个 `MediaSpec`**（2.3） |
| 本地身份 | `build_local_identity` 的 `_content_date` 已按 NFO → 容器 `tag_date` → `creation_time` → mtime 取内容时间，`_parse_date` 已能解析 EXIF 冒号日期 | EXIF 拍摄时间塞进 `tag_date`，下游零改动；三处 `kind is VIDEO` 字面比较改能力位（2.4） |
| 条目/元数据 | `ensure_local_item` + `media_metadata` | 零改动 |
| 缩略图与资产 | `ensure_local_assets`：资产目录、`poster_width/height` 回写、`generate_thumbnails` 开关、扫描收尾 ASSETS 阶段并发、失败自愈、「重新生成缩略图」按钮 | `build_thumbnail` 加图片分支，两处短路（2.5） |
| 列表接口 | `sort=release_date`、`primary_aspect`、带版本号 `poster_url`、200/页分页 | `LibraryItemView` 加 `release_date`；同日次序改按文件名（2.6） |
| 跳转索引 | `item-index` 按首字母分档 `(档, 条数, offset)` | 同一机制按月分档（2.6） |
| 原图 | 只有资产目录路由 | 新路由，照抄 `get_file_thumb`（2.7） |
| 实时监听 | `_make_handler(library_id, root)` 每库一个 handler，判定用全局扩展名 | handler 持有该库档案的扩展名集合（2.8） |
| 回收站 / 缺失 / 可见范围 / 统计 / 库封面 | 文件级或库级 | 零改动 |
| 首页最近添加 | `exclude_from_home` | 建库默认值按类型给（2.9） |
| Jellyfin | 库视图按可见性列出 | 按能力位排除，一期不暴露（2.10） |
| 前端 | 类型卡由 `library_kind_options` 驱动；非刮削库已走 timeline 模式；`ImageLightbox` 已有翻页/缩略条/预加载 | 新 `photo-wall.tsx`；灯箱向后兼容扩展（第 3 节） |

## 2. 后端设计

### 2.1 能力档案

`MediaKind` 加 `PHOTO = "photo"`（形态：单张图片，没有结构假设，只在本地
来源下出现，永不进 TMDB 请求）。`PROFILES` 加：

```python
(MediaKind.PHOTO.value, MediaSource.LOCAL): LibraryProfile(
    kind=MediaKind.PHOTO, source=MediaSource.LOCAL, label="图片",
    scraped=False, naming=False, ignore_rules=IgnoreProfile.PLAIN,
    write_nfo=False, subscribable=False,
    jellyfin_type="Photo", jellyfin_collection="photos",
    default_aspect=4 / 3,
    media_exts=IMAGE_EXTS, playable=False, jellyfin_exposed=False,
)
```

`LibraryProfile` 新增三个能力位，存量三行同步补值：

| 能力位 | movie/tv | video | photo | 消费方 |
|---|---|---|---|---|
| `media_exts: frozenset[str]` | `SCAN_VIDEO_EXTS` | `SCAN_VIDEO_EXTS` | `IMAGE_EXTS` = jpg jpeg png webp gif bmp avif | 扫描遍历、监听判定 |
| `playable: bool` | 真 | 真 | 假 | 前端卡片点击/`/play/` 守卫、Jellyfin `PLAYABLE_TYPES` |
| `jellyfin_exposed: bool` | 真 | 真 | 假 | Jellyfin 库视图与最新媒体查询 |

`capabilities_of` 导出 `playable`；`KIND_LABELS` 补「图片」。`IMAGE_EXTS`
落 `layout.py`，与 `VIDEO_EXTS` 并列。

**为什么不直接比 `kind == "photo"`**：profile.py 的 docstring 明令消费方读
能力位。本次核对发现 `local_identity.py` 已有三处 `kind is MediaKind.VIDEO`
（2.4），图片类型若不改会**静默按影视库的临时身份分组**——同目录所有照片
合成一张卡。这正是字面比较的代价，一并收掉。

### 2.2 扫描遍历

`_walk_videos` 现在硬编码 `SCAN_VIDEO_EXTS`，改为参数 `exts`，两个调用点
传 `profile.media_exts`（`local_episode_count` 等只服务影视库的调用点不动）。
`_SYSTEM_DIRS` 补 `.thumbnails`、`.picasaoriginals`（`@eadir` 已在）：相册
工具的缩略图目录，不挡就一张照片入账两次。

主循环其余环节（秒过、写入中延后、缺失标记、改名归并、批次号、进度桥、
停止请求）原样复用。非刮削库本就不走 ffprobe 预取窗口，图片探测在
`to_thread` 里顺序执行，Pillow 只读文件头，单张毫秒级。

### 2.3 探测：`probe_image` 返回同一个 `MediaSpec`

`media_probe.py` 的 `probe_media` 开头按后缀分派：`IMAGE_EXTS` → `probe_image`。
Pillow 实现（线程池内）：

- `Image.open` 惰性打开，只读头：`size`、`format`；
- `getexif()` 取 `DateTimeOriginal`（0x9003，在 Exif IFD）→ `tag_date`，
  形如 `2024:05:01 10:20:30`，`_parse_date` 已能解析；
- `Orientation`（0x0112）为 5–8 时宽高对调；
- 返回 `MediaSpec(resolution=f"{w}x{h}", video_codec=None, hdr=None,
  bit_depth=None, duration_seconds=None, bit_rate=None, audio_streams=[],
  subtitle_streams=[], tag_date=…)`。

三个必须守住的点：

1. **`audio_streams` 必须是 `[]` 不是 `None`**：定期对账的补探条件是
   `audio_streams IS NULL`，写 `None` 会让每张照片每轮都被重探。
2. **Pillow 解压炸弹防护**：超过 `MAX_IMAGE_PIXELS` 抛
   `DecompressionBombError`，按探测失败处理（中文日志 + `note_probe_failure`），
   不让一张畸形图打断整轮。
3. **原图尺寸落 `library_file.resolution`**（`4032x3024`），不加列；灯箱
   信息面板从条目文件清单里读它。

下游零改动：`_ingest_file` 照常写台账行；`_content_date` 取到 `tag_date`
→ `release_date` = 拍摄日期 → 时间线排序、年份都现成；预取判定、失败记忆、
退避补探自动生效。

### 2.4 本地身份：三处字面比较改能力位

`local_identity.py` 中：

| 位置 | 现状 | 改为 |
|---|---|---|
| `local_external_id` | `if kind is MediaKind.VIDEO:` 一文件一条目，否则按目录分组 | `if not scraped:` |
| `build_local_identity` | `dirs = [] if kind is VIDEO else entry_dirs(…)` | 同上 |
| 同上 | `if year is None and kind is VIDEO and content_date` | 同上 |

三处语义都是「本地内容库（一文件一条目）」对「影视库的临时身份（按作品
分组）」，与形态无关、与来源有关；`build_local_identity` 与 `local_external_id`
的 `kind` 参数保留（`external_id` 里仍带形态），新增 `scraped: bool` 参数，
调用点从 `profile.scraped` 传入。图片的标题 = 文件名主干（`IMG_5123`），
sidecar NFO 逻辑保留但对照片几乎不会命中。

### 2.5 缩略图：`build_thumbnail` 加图片分支

`ensure_local_assets` 整体复用。`build_thumbnail` 开头：源文件后缀在
`IMAGE_EXTS` → `_build_image_thumbnail`（Pillow，线程池内）：

- `Image.open` → `ImageOps.exif_transpose`（ffmpeg 不认 EXIF 方向，这是
  不走 ffmpeg 的主因，另一半原因是几万张图起几万个子进程太慢）；
- `img.draft("RGB", (720, 720))`：JPEG 解码时直接降采样，大图快 4–16 倍；
- `thumbnail((720, 720))`：长边 ≤ 720（墙上最宽列 310px 的 2× 视网膜足够，
  比视频抓帧的 1280 省一半磁盘与流量）；
- 带透明通道的 PNG/WebP/GIF 合成到卡片底色 `#141824`（与前端 `bg-[#141824]`
  同源），GIF 取首帧；
- 存 JPEG 质量 85，返回 `(w, h)` 写 `poster_width/height`。

Pillow 存图默认不带 EXIF，缩略图天然不泄漏 GPS。

两处短路：

- `find_artwork` 会找 `<主干>.jpg` 当海报 sidecar——对图片文件就是它自己，
  会被 ffmpeg 当海报转码一遍；图片分支在 sidecar 查找之前返回；
- `build_backdrop` 对图片直接返回 `False`。

「探测读头 + 缩略图读全文」是两次打开，但探测只读前几十 KB，对网络挂载
的额外流量可忽略，不为此把缩略图耦合进扫描主循环。ASSETS 阶段
`_ASSET_CONCURRENCY = 3` 对 CPU 型的 Pillow 工作正合适，不改。

### 2.6 列表与索引

- `LibraryItemView` 加 `release_date: date | None`：前端按月分组、悬停
  日期、灯箱信息用。查询已联 `media_metadata`，零额外开销。
- `_wall_page_ids` 的 `release_date` 排序第二键从 `id` 改为 `title`
  （`func.max(MediaItem.title).desc()`）：`release_date` 是日期无时分，同一天
  的照片按文件名（`IMG_5123` 序号单调）排比按入账顺序稳定得多。多设备
  同一天交错仍按文件名，精确到秒要加 `captured_at` 列，留待有需求再做
  （单向迁移可接受）。其他库的家庭录像顺带受益。
- `build_library_index` 加 `sort` 参数：`release_date` 时按 `YYYY-MM`
  分档，返回同样的 `[(档, 条数, offset)]`；`item-index` 路由透传
  `?sort=`。前端现有的字母跳转轨道直接变成月份轨道（3.2）。

### 2.7 原图路由

`GET /api/libraries/files/{file_id}/original`，照抄 `get_file_thumb`：

- 路径完全由台账行推导（客户端只给 id，无路径注入面）；
- `assert_library_visible` 按文件归属库做成员可见性判定；
- `FileResponse`（自带 `Last-Modified` / `ETag` / Range），`media_type`
  由 `mimetypes.guess_type` 给，`Cache-Control: private, max-age=3600`；
- `?download=1` 加 `Content-Disposition: attachment; filename*=UTF-8''…`，
  「下载原图」就是它。

原图含完整 EXIF（含 GPS）；库的可见范围就是它的访问边界，与视频直连一致。

### 2.8 实时监听

`_make_handler(library_id, root)` 每库一个 handler，在这里把
`profile_of(library).media_exts | SUBTITLE_EXTS` 作为判定集合交给
`_is_relevant_event(event, watched)`。**不能取所有档案的并集**：刮削器往
影视库写 `poster.jpg` 会触发范围扫描，「正在扫描」在刷新期间反复闪现，
正是该函数注释里第 2 条防的自激。`_modified_video_path` 只认视频扩展名
不动——图片内容被修改（原地编辑）走 mtime 变化的秒过重探即可。

### 2.9 建库默认值

`LibraryPayload` 不变；前端建库时按类型给默认值，图片库
`exclude_from_home=True`（一次导入几百张会把首页「最近添加」刷满），
其余（监控开、自动清理关、缩略图开）与其他库一致。后端不为此加逻辑。

### 2.10 Jellyfin：一期不暴露

- `item_type_of` 映射补 `"photo": "Photo"`（有人问到不至于报错）；
- 库视图与最新媒体查询按 `jellyfin_exposed` 排除：从 `PROFILES` 聚合出
  `kind` 集合，`Library.kind.not_in(...)`；`routes/library.py:1162` 的
  `library.kind == "video"` 顺手改读能力位；
- `PLAYABLE_TYPES` 不含 `Photo`。

Infuse 不支持照片库，Jellyfin 官方客户端的 `photos` 视图留二期。

### 2.11 守卫清单

| 守卫 | 落点 |
|---|---|
| 图片库零 ffprobe、零 TMDB 请求 | 回归测试打桩 `subprocess.run` 与 TMDB 客户端 |
| 影视库目录里的 jpg 不入账 | `_walk_videos(exts=profile.media_exts)` |
| 图片库不能做订阅/路由/导入目标 | 既有 `subscribable=False`；导入规则的 kind 枚举不含 photo |
| `/play/{id}`、播放器、进度 | 前端 `playable` 守卫；后端播放接口对无视频流文件本就返回错误，不加逻辑 |
| 补探不重复 | `audio_streams=[]` |

## 3. 前端设计

### 3.1 建库 / 编辑 / 管理页

- `LibraryKind` 联合类型加 `"photo"`，typecheck 会逼着 `KIND_CARDS`、
  `LIBRARY_KIND_META` 补项（文案见 library-manage.md 的卡片口径：
  「不识别、不刮削，扫描目录里的所有图片，按拍摄时间排成相册墙」，
  特性「不识别 · 全屏查看 · 按拍摄时间」，默认名「相册」）；
- 编辑面板已按 `capabilities.scraped` / `naming` 分叉；缩略图开关文案按
  类型：「生成缩略图」，说明写清关掉后墙会直接加载原图；
- 管理行「刷新元数据」按钮对非刮削库已显示「重新生成缩略图」，零改动。

### 3.2 库内页：`photo-wall.tsx`

`library-detail-view.tsx` 对非刮削库已走 timeline 模式（`sort=release_date`、
无限滚动、`item-index` 跳转轨道）。按 `capabilities.playable === false`
换墙组件：

- **布局**：每档密度定目标列宽、最少列数与间距（紧凑 150 / 3 列 / 6px，
  标准 230 / 2 列 / 12px，宽松 340 / 1 列 / 18px），列数 = 容器宽按目标列宽
  取整、不低于最少列数；每张按 `primary_aspect` 夹到 `[0.5, 2]` 算高度，放入
  最短列；`position: absolute` + `transform`，重排走 CSS 过渡。比例在渲染前
  已知，布局零抖动。只按目标列宽算列数在 600px 以下三档都落到两列、点了没
  变化，桌面上也只差一列，所以最少列数与间距也随档走：任何宽度下三档都是
  三种明显不同的画面（1440px 窗口 6 / 4 / 2 列，820px 窗口 3 / 2 / 1 列）。
- **按月分组**：用月份索引的 `(档, 条数, offset)` 切段，每段一个标题 +
  一面墙；索引给出全库月份与条数，未加载的月份也能显示标题与占位高度，
  滚动条比例准确。
- **时间刻度**（Google Photos 式）：不占宽度的悬浮刻度条叠在墙的右缘，
  静止时隐去、滚动中或鼠标移到右缘时浮现；刻度按每月张数按比例分布，年份
  标签立在该年第一个月处，悬停浮出「2026 年 8 月 · 16 张」气泡，点击即
  `offset` 跳转，与影视库同一套 `wallStart` 逻辑。最初做成常驻的月份列表
  放在墙旁边，占 56px 宽还一直亮着，评审认为浏览照片时是个干扰，改为此。
- **密度**收在页头右上角的「⋯」菜单里（三档单选，记住偏好），墙上不摆
  任何控件。
- **虚拟化**：离视口三屏以外的瓦片只保留定位盒不挂 `<img>`；瓦片总数
  以万计时 DOM 仍可控。
- **极端比例**：夹紧后 `object-fit: cover` 裁边，角标「全景 / 长图」，
  全屏看完整原图。
- **稀疏月份**：照片数少于列数的月份（零星截图、单张转存，没有 EXIF 的图按
  修改时间归到当月往往就是一两张）不走最短列——那会退化成一根孤柱，一张
  1:2 的长图撑出近 500px 高、右边几列全空。改成一行等高、限高（目标列宽 ×
  1.2），每张按比例给宽度（`layoutSparseRow`）。端到端模拟时暴露的问题。
- **悬停层**：文件名、拍摄日期、原图尺寸、格式角标。

### 3.3 灯箱：扩展 `ImageLightbox`，向后兼容

`images: string[]` 之外新增可选 `entries: LightboxEntry[]`
（`{ thumb, full, title, meta }`）与 `onReachEnd`；搜索页与详情页的既有
调用方不动。新增行为：

- 渐进：先显示墙上的缩略图（模糊），原图 `onload` 后替换；
- 滚轮 / 双击缩放，拖拽平移，`0` 复位；触屏左右滑动翻页；
- 翻到已加载列表末尾时 `onReachEnd` 拉下一页；
- 底部缩略条只渲染当前位置 ±30 张（万张库不铺满 DOM）；
- 信息面板（`i`）：文件名、拍摄时间、尺寸、大小、格式、路径，数据来自
  既有条目文件清单接口，按需拉取；
- 操作：「下载原图」（2.7）、「移到回收站」（既有文件回收）。

### 3.4 渐进式加载

墙和灯箱都不等"完整的图"才上屏，按四级递进，每一级都比上一级早得多、小得多：

| 级 | 内容 | 来源 | 体量（4000×3000 相机直出，7.4 MB） | 何时可见 |
|---|---|---|---|---|
| 0 | 布局 | 扫描入账时探到的原图尺寸（`library_file.resolution`）定 `primary_aspect` | 0 字节 | 扫描一结束，缩略图还没生成就是最终布局，之后不重排 |
| 1 | 微缩占位 | 生成缩略图时顺手算的 16px 宽 JPEG data URI（`media_metadata.poster_blur`），随列表接口下发 | ≈ 400 字节 / 张 | 列表到达即铺出模糊色块 |
| 2 | 瓦片 | `?variant=photo-tile`：长边 480 的 WebP 派生图（紧凑/标准密度）；宽松密度直接用 720 缩略图 | 几十 KB | 懒加载，进视口才请求 |
| 3 | 灯箱 | 先缩略图（已在缓存里，模糊放大）→ `?size=screen` 长边 2048 的 WebP（按原图惰性派生、磁盘缓存）→ **放大到超过 1:1 时才拉原图** | 屏幕适配图 ≈ 原图的 8% | 打开即有图，几百 KB 后清晰，几 MB 的原图只为放大与下载 |

派生图走既有的 `ImageVariantService`（singleflight + 磁盘 LRU），新增 `fit`
语义的预设（等比装进盒子、不裁切）：照片的比例本身就是内容，不能像海报卡那样
按盒子裁。相邻两张预加载的也是屏幕适配图，不预拉原图。

`poster_blur` 是本方案唯一的迁移（`d1e2f3a4b5c6`，接在 main 的章节迁移 `d4e5f6a7b8c9` 之后，纯新增可空列，向前兼容）。

## 4. 实施计划

单个发布周期，一期即完整功能；无迁移、无新依赖、不 bump `runtime-version`。

| # | 事项 | 落点 |
|---|---|---|
| 1 | `MediaKind.PHOTO`；`IMAGE_EXTS`；档案加 `media_exts` / `playable` / `jellyfin_exposed`；`capabilities_of` 导出 `playable`；`KIND_LABELS` | `movieclaw_media/models.py`、`layout.py`、`profile.py`、`schemas/library.py` |
| 2 | `probe_image` + `probe_media` 分派；炸弹防护；`resolution` 口径 | `media_probe.py` |
| 3 | `local_identity.py` 三处改 `scraped`；调用点传档案 | `local_identity.py`、`scan.py:3069-3110` |
| 4 | `_walk_videos(exts=…)`；`_SYSTEM_DIRS` 补两项 | `scan.py` |
| 5 | `_build_image_thumbnail` + 两处短路 | `thumbs.py` |
| 6 | `LibraryItemView.release_date`；同日次序按 `title`；`build_library_index(sort)` 与路由透传 | `items.py`、`schemas/library.py`、`api/routes/libraries.py` |
| 7 | 原图路由 | `api/routes/libraries.py` |
| 8 | 监听 handler 按库档案判定 | `watch.py` |
| 9 | Jellyfin 排除与映射 | `movieclaw_jellyfin/catalog.py`、`routes/library.py` |
| 10 | 前端：类型与文案、`photo-wall.tsx`、月份轨道、灯箱扩展、`playable` 守卫、建库默认值 | `lib/media-types.ts`、`library-kind-meta.ts`、`library-form-dialog.tsx`、`library-detail-view.tsx`、新 `photo-wall.tsx`、`image-lightbox.tsx` |
| 11 | OpenAPI 基线与 CLI spec 用仓库脚本重生成；README 补「图片库」一节 | `export_openapi.py`、`cli/internal/spec/data/spec.json`、`docs/` |

**验收**（新建 `tests/api/test_library_photo_kind.py`，仿
`test_library_video_kind.py`；影视与其他库既有用例零改动全绿）：

1. 档案表：`photo` 行能力位齐全，`library_kind_options` 含「图片」，建库
   默认 `source=local`；
2. 扫描：目录放入带 EXIF 的 JPEG（含方向标签 6）、PNG、WebP、
   `.thumbnails/x.jpg`、`sample.jpg`、一个 `.mp4` → JPEG/PNG/WebP 各成一条
   `source=local` 条目（**一文件一条目**）、`release_date` = EXIF 拍摄日、
   `resolution` 已按方向对调、`.thumbnails` 与 `sample.jpg` 与 mp4 不在账、
   全程零 `ffprobe` 子进程、零 TMDB 请求；
3. 缩略图：`poster.jpg` 长边 ≤ 720、方向已纠正、`poster_width/height` 回写；
   `generate_thumbnails=False` 时不生成；透明 PNG 合成到底色；
4. 列表：`sort=release_date` 倒序且同日按文件名；`item-index?sort=release_date`
   返回月份档；`primary_aspect` 为真实比例；
5. 原图路由：可见成员 200 且带 `Last-Modified`，不可见成员 404，`?download=1`
   带 `Content-Disposition`；
6. 影视库目录里的 jpg 不入账；图片库目录里的 mp4 不入账；
7. 对账补探：图片行不被重探；
8. Jellyfin：库视图与最新媒体不含图片库；
9. 前端 typecheck 全绿；瀑布流墙对 aspect 0.1 与 10 的输入夹紧到 0.5 与 2。

## 5. 决策记录

| 决策 | 结论 | 理由 |
|---|---|---|
| 布局 | 瀑布流（按月分组、最短列） | demo 确认；比例已知所以零抖动、可虚拟化 |
| 等高行 | 不做 | 简洁优先；有需求再加开关 |
| HEIC | 一期不做 | 新依赖 + runtime-version bump；先验证 jellyfin-ffmpeg / pillow-heif 可用性 |
| Jellyfin | 一期不暴露 | Infuse 不支持照片库 |
| 首页展示 | 图片库默认关 | 批量导入刷屏 |
| 缩略图开关 | 保留、默认开 | 与其他库同一网络挂载顾虑 |
| 缩略图尺寸 | 长边 720 | 最宽列 2× 足够，省一半磁盘 |
| 同日次序 | 文件名 | 不加列；精确到秒留待 `captured_at` |
| 原图尺寸 | `library_file.resolution` 存 `WxH` | 不加列 |
| 探测/缩略图两次打开 | 接受 | 探测只读头，网络挂载额外流量可忽略 |

## 6. 实施记录（2026-09-06）

按第 4 节的 11 项全部落地，与定稿方案的偏离只有两处：

1. **灯箱是独立组件，没有扩展 `ImageLightbox`**。搜索页的灯箱是"一组外链 URL
   的浏览器"，图片库翻的是分页加载的条目列表、每张有缩略图与原图两级、有台账
   信息，两种数据模型硬塞进一个组件只会让搜索页那份变复杂。`photo-lightbox.tsx`
   沿用同一视觉与层叠约定，搜索页与详情页的调用方零改动。
2. **灯箱一期没有「移到回收站」**。核实后发现现有的文件级删除
   （`delete_single_file`）是从磁盘删除并连带清掉同主干的 sidecar 图片
   （`IMG_001.jpg` 会带走 `IMG_001-edited.jpg`），用户可见的回收站只服务于
   订阅洗版的替换文件，没有"把一个文件移进回收站"的接口。给照片做一条安全的
   删除路径（不碰同主干文件、进回收站可恢复）是独立的一小步，留待下一期；
   一期灯箱只有「下载原图」。

顺手收掉的旧债：`primary_aspect` 的兜底比例改读能力档案的 `default_aspect`
（此前写死 TMDB 2:3 / 本地 16:9），`local_identity.py` 三处 `kind is VIDEO`
改为按来源分叉，`_wall_page_ids` 的内容时间排序第二键改为标题。
