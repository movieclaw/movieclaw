# 元数据自足：结构化落库与刷新机制设计

> 状态：v1 已实施（2026-07-25，M1~M4 一次落地）。与草案的三处偏差见文末
> 「实施记录」。
> 关联文档：[library.md](library.md)（媒体库架构）、[subscription.md](subscription.md)
> （订阅架构，media_item 身份锚的由来）、subscription-p4.md 第 6 节（刷新分档）。

## 0. 定位与目标

**movieclaw 升级为自足媒体库**（用户决策，2026-07-25）：像 Emby/Plex 一样，
**一次入库刮削，本地即拥有完整展示数据**；简介/评分/演职员/分集详情等元数据
结构化存储在自己的表里，详情页断网可用。TMDB 从「每次展示的实时数据源」降级为
「刮削与刷新时才访问的上游」。

此前的取舍（media_item 只存匹配最小闭包，展示信息走 NFO 优先 + TMDB 实时兜底
+ SWR 响应缓存）是「伴生工具」定位下的正确选择；定位变更后它的缺口是：

- 断网/TMDB 故障且无本地 NFO 时，详情页退化到只剩标题年份和 ffprobe 规格；
- SWR 缓存是 URL 键的 JSON 响应副本，不可查询、不可控过期粒度、无法作为事实源；
- 没有「整库刷新/单条目刷新」的用户可控入口，数据新鲜度完全依赖访问触发。

**目标**（成功标准，可验证）：

1. 条目识别挂锚后自动完成一次全量刮削，此后**断开 TMDB，详情页信息完整**
   （简介/评分/片长/演职员/分集名与简介/图片全部可展示）；
2. 提供单条目刷新与整库刷新，加上定时分档保鲜，三个入口收敛到同一条刮削管线；
3. 暂不移动/改名/删除任何媒体目录中的既有文件；图片与 NFO 按 Kodi/Emby
   业界规范**只增不覆盖**地补充写出，反哺播放器生态。

**决策变更声明**：[library.md](library.md) 第 1 节曾决策「不建 season/episode
实体表」——该决策针对的是**引用维度**（`library_file`/`wanted_item` 用
`(season_number, episode_number)` 数字对引用，不设外键），此约定**不变**。
本设计为**展示维度**新建 `media_episode` 表，两者不冲突：引用靠数字对，
展示数据按 `(media_item_id, season, episode)` 唯一键查询。

**明确不抄**（重申 library.md 的教训）：moviebot 的 `media_metadata` 挂
`library_id`，同一部片两个库两份元数据。movieclaw 的元数据挂**全局
`media_item`**，与订阅、匹配、库存共享同一身份锚——一部片全局一份元数据，
无论它的文件散在几个库。

## 1. 分层原则：身份层不动，新增展示层

```
media_item        身份锚 + 匹配最小闭包（现状不动）
  ├─ media_metadata   条目展示元数据（新，1:1）
  ├─ media_season     季骨架（现有，扩展季级展示字段）
  └─ media_episode    分集展示元数据（新，1:N）
```

`media_item` 保持「匹配内核最小闭包」职责不变（外部 ID/标题/别名/年份/status/
海报路径），**刮削失败不影响身份与订阅**——展示层缺失只是详情页降级，订阅
追新、种子匹配、库存台账全部照常。这也是把展示字段放独立表而非拍进
`media_item` 的原因：两层的写入方、刷新节奏、失败语义都不同。

### episodes JSON 与 media_episode 的关系（权衡与决策）

现状 `media_season.episodes` JSON（`[{episode_number, name, air_date}]`）是订阅
wanted 生成的输入。新表 `media_episode` 与它在 name/air_date 上重复。三个方案：

- A. 只扩展 JSON 元素（加 overview/still 等）——查询与按集 upsert 都笨拙，
  单集简介可达数百字，几百集的剧一行 JSON 会膨胀到 MB 级，否掉；
- B. 新表并存，JSON 保留——同一次刷新事务里双写，数据同源不会漂移；
- C. 新表替代，订阅读路径（`expected_units` 等）迁移到 `media_episode`，
  删除 episodes JSON 字段。

**决策：B 起步，C 收口**。M1 双写（改动面最小、订阅零风险），M4 把订阅读路径
迁到 `media_episode` 后删 JSON 字段。双写期间 `media_episode` 是集数据的唯一
事实源，JSON 只是订阅侧的兼容视图。

### 不建 person 表

演职员以 JSON 内嵌在 `media_metadata`（前 40 位，与现有 NFO 展示上限一致）。
Emby 建了 People 实体表是为了「按演员浏览/搜索」，movieclaw 暂无此需求，
建表属过度工程。未来若做演员页，届时从 JSON 迁移，成本可控。

## 2. 表结构

三态铁律沿用全仓约定：可缺失字段 NULL=未知，语义空值用空串/空列表。

### 2.1 media_metadata（新表，1:1 media_item）

```
id                  PK
media_item_id       FK media_item CASCADE，UNIQUE（一条目一行）

-- 文案
overview            Text        简介；NULL=TMDB 也没有
tagline             str|NULL    宣传语

-- 事实字段
genres              JSON [str]  类型（中文名，如 ["剧情", "科幻"]）
runtime_minutes     int|NULL    片长（电影）/单集常规时长（剧集）
release_date        date|NULL   上映日期（电影）/首播日期（剧集）
content_rating      str|NULL    分级（电影取 release_dates、剧集取 content_ratings，
                                优先 CN 无则 US）
original_language   str|NULL    原始语言
origin_countries    JSON [str]  制片国家/地区
studios             JSON [str]  电影=制作公司，剧集=播出网络

-- 评分
vote_average        float|NULL  TMDB 评分
vote_count          int|NULL    评分人数

-- 演职员（JSON 内嵌，见第 1 节决策）
directors           JSON [str]              电影=导演，剧集=创作者
cast                JSON [{name, character,
                          order, profile_path}]   前 40 位；profile_path 为
                                                  TMDB 相对路径

-- 本地图片资产（相对 data/metadata/images/，NULL=未下载/下载失败）
poster_file         str|NULL
backdrop_file       str|NULL

-- 刮削台账
scraped_at          datetime    本行最近一次成功刮削时间
scrape_language     str         刮削时使用的 TMDB language（如 zh-CN）
```

不设 `source` 字段：本表只有 TMDB 一个写入方（NFO 是读路径的另一层，
不写进本表，见第 5 节）。未来接第二数据源时再加。

### 2.2 media_episode（新表）

```
id                  PK
media_item_id       FK media_item CASCADE
season_number       int
episode_number      int
UNIQUE (media_item_id, season_number, episode_number)   -- 刷新 upsert 键
INDEX  (media_item_id, season_number)                   -- 详情页逐季读取

name                str         集名；语义空值空串
overview            Text|NULL   单集简介
air_date            date|NULL   播出日期
runtime_minutes     int|NULL    单集时长
vote_average        float|NULL
still_path          str|NULL    TMDB 剧照相对路径
still_file          str|NULL    本地剧照资产路径
```

### 2.3 media_season 扩展（现有表加列）

```
+ overview          Text|NULL   季简介
+ poster_path       str|NULL    季海报 TMDB 相对路径
+ poster_file       str|NULL    本地季海报资产路径
```

`episodes` JSON 保留至 M4（见第 1 节）。

### 2.4 library_file 不动

介质规格（ffprobe）、音轨字幕轨已完备，它是「文件本体真相」，与本设计的
「作品元数据」正交。

## 3. 刮削管线（media_scrape 服务）

新建 `movieclaw_api/services/media_scrape.py`，唯一入口：

```
async def scrape_media_item(media_item_id, *, force=False) -> ScrapeResult
```

**流程**：

1. 拉条目档案：`{kind}/{tmdb_id}?append_to_response=credits,content_ratings`
   （电影用 `release_dates` 取分级）——与现有 `fetch_media_profile` 合并为
   一次请求扩展，**同一份响应既喂身份层（别名/status）也喂展示层**，
   不重复打 TMDB；
2. 剧集逐季拉 `tv/{id}/season/{n}`（复用现有 `_fetch_season`，扩展解析
   overview/still/runtime/vote）；
3. 同一事务 upsert `media_metadata` + `media_season` + `media_episode`
   （+ 双写 episodes JSON）；
4. 下载图片到本地资产目录（第 6 节），失败保持 NULL 不阻断——下次刷新自愈；
5. 按库开关镜像写出媒体目录图片与完整 NFO（第 6 节），失败只告警。

**并发防护**：进程内 `_scraping: set[int]` 单飞（同扫描的 `_scanning` 模式），
同一条目并发刮削后到者直接返回。TMDB 限速依赖现有 `TmdbClient` 的限速与
`movieclaw_net` 出口层，不另建队列。

**语言开口**：`language=zh-CN` 时 TMDB 部分条目 overview/集名缺中文。v1 策略：
中文为主，条目级 overview 为空时补拉一次 `language=en-US` 兜底（只对条目级，
分集不做，控制请求量）。效果不满意再考虑 append `translations`。

## 4. 触发与刷新机制

三个入口 + 一个定时器，全部收敛到 `scrape_media_item`：

### 4.1 一次入库刮削（自动）

条目**识别挂锚**的所有路径都触发：扫描识别成功、入库管线（订阅/手动下载）、
待识别清单人工认领、候选点选、重新识别改锚。实现上收口在
`ensure_media_item` 之后的统一钩子：若该条目 `media_metadata` 不存在 →
投入后台刮削（`asyncio.create_task`，不阻塞识别主流程）。

扫描场景的批量优化：扫描中不逐文件触发（一部剧几百个文件会重复命中单飞锁），
`_scan` 收尾时收集本轮新挂锚的 `media_item_id` 去重集合，统一逐个刮削。

### 4.2 单条目手动刷新

```
POST /api/library/items/{media_item_id}/metadata/refresh
```

`force=True`：跳过新鲜度判断强制重刮，并重新下载图片（覆盖本地资产——
资产目录是应用私有的，可覆盖；媒体目录内的文件仍只增不覆盖）。
前端入口：条目详情页「刷新元数据」按钮（工具栏图标化，遵循密度偏好）。

### 4.3 整库刷新

```
POST /api/libraries/{library_id}/metadata/refresh        启动（后台执行）
POST /api/libraries/{library_id}/metadata/refresh/stop   停止
GET  库详情接口携带 refreshing / refresh_progress
```

遍历该库全部已识别条目（`library_file` 去重出 `media_item_id` 集合），逐个
串行刮削（TMDB 限速下并行无意义）。防重复与进度沿用扫描的三层模式：路由层
409、服务层单飞集合、进度字典 + 停止请求集合。与扫描互斥**不需要**：刮削
只写元数据表，不碰台账与文件。

### 4.4 定时分档保鲜（合流进现有 refresh_media_metadata）

现有定时任务已按 tick 拉取 TMDB 档案刷身份层与季集骨架。改造：`_refresh_one`
拉档案后**顺手调用刮削管线的 upsert**（复用同一份响应 + 逐季响应，零额外
请求），展示层随身份层同节奏保鲜。分档在现有基础上微调：

| 条目状态 | 间隔（现状） | 调整 |
|---------|------------|------|
| 有订阅，在播剧 | 8h | 不变 |
| 有订阅，未上映电影 | 24h | 不变 |
| 有订阅，已完结/已上映 | 7d | 不变 |
| 无订阅 | 30d | 无订阅**但在库** 30d；无订阅且不在库（历史建档残留）不刷 |

**存量回填**：升级后已识别条目的 `media_metadata` 为空。不做一次性迁移任务：
定时 tick 会按 `next_refresh_at` 逐步补齐（NULL=立即到期的语义现成）；
用户想立刻补齐就点整库刷新。文档与升级说明里写明这一点。

### 4.5 图片自愈

刮削成功但图片下载失败的条目，`poster_file` 等保持 NULL。详情页/海报墙读到
NULL 时回退现有 image-proxy 实时链路（不白屏），下次任一刷新入口重试下载。

## 5. 读路径改造（详情页与海报墙）

优先级从「NFO → TMDB 实时」改为：

```
1. 本地 NFO / 条目目录图片     （尊重用户既有刮削成果，Emby 同款惯例）
2. media_metadata / media_episode（本设计的主体，绝大多数条目的日常路径）
3. TMDB 实时                    （前两层皆空的兜底；命中时顺手触发后台刮削自愈）
```

NFO 仍居首是行为连续性的选择：TMM/Emby 人工刮削的中文数据可能优于 TMDB
自动拉取，且现有用户已依赖此行为。字段级合并：NFO 有的字段用 NFO，
缺的字段用 DB 补（现有 `local_meta` 合并逻辑扩展，DB 层替换 `_tmdb_fallback_meta`
成为第二层）。

第 2 层命中后，现有 SWR 响应缓存（`meta:*` / `season:*` 键）不再被详情页
依赖，自然冷却；缓存层保留给发现页/搜索等非库内场景。

**图片服务**：新路由 `GET /api/metadata/assets/{path}`，FileResponse 直出本地
资产（带长缓存头）。前端海报 URL 生成逻辑：`poster_file` 非空 → 资产路由；
NULL → 现有 image-proxy。

## 6. 图片落盘规范

### 6.1 应用托管资产（事实源，前端消费）

```
data/metadata/images/{media_item_id}/
  poster.jpg              w500
  backdrop.jpg            w1280
  season-{n}.jpg          w500
  s{ss}e{ee}.jpg          w300（分集剧照）
```

与 SQLite/uploads/图片缓存同在 `data/` 卷下，Docker 挂载一个卷整体持久化。

**尺寸档位**（2026-07-26 调整，自足媒体库偏画质）——可经 `TMDB_*_SIZE`
配置，默认：

| 资产 | 档位 | 理由 |
|------|------|------|
| 背景 | `original` | 全屏沉浸底图，最显眼的一张；w1280 在 2K/4K 屏上是放大糊图 |
| 海报 | `w780` | 详情页 186px、墙 148px，2 倍屏下足够锐利 |
| 分集剧照 | `w300` | 小卡片，且一部剧动辄几百集 |

估算：1000 部电影 ≈ 1000×(0.3+2)MB ≈ **2.3GB**；分集剧照 7200×30KB ≈ 220MB。
比原档位（w500/w1280/w300，约 550MB）大一个量级，但相对媒体文件本身
（TB 级）可忽略；磁盘吃紧的用户调低配置即可。

**资产溯源与追新**（2026-08-04 完整性决策，取代已废弃的
`media_metadata.asset_profile` 列方案）：每个条目资产目录写 `sources.json`
（资产键 → 下载时的「档位+TMDB 路径」）。普通刷新时溯源与当前来源不符
即重下——**TMDB 换图**（poster_path 变更）与**档位配置调整**都被覆盖，
资产随任一刷新入口保持最新；没有这份记录时无从判断新旧，视同过期重下一次。
选图锁定的海报/背景不受溯源触发（锁的语义是"这张图就是要的"）；档位升级
同理经溯源生效，锁保护的是"用哪张图"，不是"用什么分辨率"。

### 6.2 媒体目录镜像（每库开关 `write_media_assets`，默认开）

按 Kodi/Emby/Jellyfin 共同识别的命名规范，写入条目目录：

```
电影：  Title (Year)/poster.jpg、fanart.jpg、movie.nfo
剧集：  Title (Year)/poster.jpg、fanart.jpg、tvshow.nfo、season{NN}-poster.jpg
分集：  <视频文件名>-thumb.jpg、<视频文件名>.nfo（<episodedetails>）
```

**铁律**（2026-08-04 完整性决策改版——原「只增不覆盖」翻转为「随档案
保持更新」，用户拍板：入库与刷新两个动作必须让本地数据完整且最新）：

- **图片**：资产比镜像新（或单条目 force 刷新）才覆盖——资产因上游换图/
  档位调整重下后，镜像随之更新；用户手放进目录的图（mtime 比资产新）
  不会被冲掉，想让手选图长期生效走详情页选图锁（6.3，锁会覆盖镜像并
  冻结资产）；
- **NFO**：每次刮削/刷新按 `media_metadata` 重写为完整 NFO，**内容比对，
  无变化不落盘**（不动 mtime，免得 watchdog 与播放器无谓重扫）。三条
  保护：既有 NFO 声明不同 tmdbid 不写（留给认领纠错链路）；无刮削档案
  不覆盖既有内容（不拿身份档降级富 NFO）；分集档案行只有骨架（无简介
  无日期）时不覆盖第三方成果；
- **绝不删除**媒体目录中的任何文件；
- 写失败（只读挂载/权限）只告警不阻断刮削事务。

翻转的代价要明说：TMM/Emby 刮的富 NFO 从此会被我们的 TMDB 口径档案覆盖
（内容以 movieclaw 库内档案为准）。这是「本地数据的唯一事实源是库内档案，
NFO 是它的镜像」这一定位的必然结果——两套刮削成果并存只会重演"NFO 挡住
新数据"的陈旧问题（2026-08-04 Infuse 联调实测：NFO 层的 78 位演员遮蔽了
库内新档案）。

扫描器对镜像文件天然免疫：扫描只认视频扩展名，图片与 NFO 不入台账。
但需确认 watchdog 去抖不会因图片写出触发无谓补扫（写出集中在刮削收尾，
落在同目录的事件会被现有去抖合并，可接受；实测有噪音再加扩展名过滤）。

## 6.3 选图：自动策略 + 手动选定（2026-07-26 补充实施）

TMDB 详情里的 `backdrop_path` / `poster_path` 是"按投票排序的第一张"，
直接用有两个通病：① 票王常是**烧了片名文字的横图**，铺全屏做沉浸背景很脏；
② 少量投票就能把一张低分辨率图顶上去。分两层解决：

**自动策略**（`movieclaw_media.library` 的 `pick_backdrop` / `pick_poster`，
纯函数、可单测）——详情请求的 `append_to_response` 加 `images` +
`include_image_language=null,zh,en`，**零额外请求**拿到全量候选：

- **背景**：无文字（`iso_639_1 is null`）优先 → 宽度 ≥1920 → 加权分
  `vote_average × log(vote_count+1)` 降序（防"1 票 10 分"冒顶）；
- **海报**：以 **TMDB 默认 `poster_path` 为准**（2026-07-29 调整：发现页
  列表接口给的就是默认海报，建档再按策略重选会导致"订阅前后海报跳变"，
  用户反馈不一致比选图质量更伤）；`pick_poster`（本地化语言优先 →
  宽度 ≥500 → 同上加权分）仅在默认缺失时兜底，并继续为换图弹层排序候选；
- 门槛把候选滤空时不设门槛重来，全无候选回落 TMDB 默认字段——宁可给小图，
  不要没有图。

**手动选定 + 锁定**（口味问题最终只能交给用户）：条目详情页海报**悬浮**浮出
「更换图片」（工具栏已有三个按钮，不再加控件——选图入口放在它作用的对象上，
Emby/Plex 同款位置），弹层两个 tab 铺候选缩略图，排序与自动策略一致，
**首张即系统当前用的那张**并标「当前」。

- 选定即 `poster_locked` / `backdrop_locked` 置位：自动策略与 force 刷新
  **都不再覆盖**——精挑的图被下次刷新冲掉，比选不了图更伤（Emby/TMM
  "改过即锁"同款）；
- 选定后当场下载资产 + **覆盖**镜像到媒体目录，Emby 那侧下次扫描即得新图；
- 弹层顶部给「恢复自动选图」：解锁后下次刷新按策略重选。

## 7. 分期实施

- **M1 结构与管线**：三表迁移（alembic）+ `media_scrape` 服务 + 4.1 入库触发
  + 详情页第 2 层读路径 + 图片资产下载与服务路由。
  验证：新识别一部剧 → 断网 → 详情页简介/演职员/分集/图片完整。
- **M2 手动刷新**：单条目刷新 + 整库刷新（API/进度/停止/前端按钮）。
  验证：整库刷新后存量条目 `media_metadata` 全量回填；重复触发 409。
- **M3 定时合流**：`refresh_media_metadata` 改造双写展示层 + 分档微调。
  验证：在播剧新集播出后 8h 内 `media_episode` 出现新行。
- **M4 镜像写出与收口**：完整 NFO + 媒体目录图片镜像（每库开关）+
  订阅读路径迁 `media_episode`、删 episodes JSON。
  验证：Emby 指向同一目录能零刮削建库；订阅回归测试全绿。

## 8. 风险与开口

1. **TMDB 限速与整库刷新时长**：大库（数千条目、剧集逐季请求）整库刷新可能
   跑数小时。串行 + 可停止 + 进度可见已覆盖体验；刮削幂等（upsert），停了
   再点从头跑也只是浪费请求不是重复数据。开口：按 `scraped_at` 跳过近期
   刷过的条目做断点续刷。
2. **中文元数据缺失**：TMDB 冷门条目中文简介/集名常空。v1 条目级英文兜底
   （第 3 节）；开口：接豆瓣简介作第二数据源（届时 media_metadata 加 source）。
3. ~~**磁盘占用**：图片资产 GB 级下限可控；开口：设置页加「清理无引用资产」~~
   已实现（2026-07-26）：`cleanup_orphan_items` 在**删库/删条目**后自动跑——
   条目在所有库都没文件、也没订阅时，连同 `media_metadata` / 季 / 集
   （外键级联）与 `data/metadata/images/{id}/` 一并删除。仍被别的库或订阅
   引用的条目原样保留（同一部剧的集分散在两库是常态）。磁盘删除放后台，
   删不掉只记日志——宁可留点垃圾，也不让删库这类操作半途失败。

### 长任务的状态归属（2026-07-26）

扫描 / 整理 / 整库刷新 / 单条目刮削都是**分钟级**任务，状态一律放在
**服务端进程内**、前端轮询，绝不用"发起时在前端起个计时器"的写法——
那样用户切走页面、刷新浏览器、换台设备打开就全丢了。四者现状一致：

| 任务 | 状态源 | 前端 |
|------|--------|------|
| 扫描 / 整理 | `_scanning` / `_progress` / `_last_scans` | 库列表接口自带，轮询 |
| 整库刷新 | `_refresh_states`（进度 + 正在处理哪几部 + 阶段） | 进入页面先探一次，进行中 2s 轮询 |
| 单条目刮削 | `_scraping` 单飞集合 → 详情接口 `scraping` | 同上，进行中 2s 轮询 |

**已知限制**：状态是进程内的，后端重启会丢——任务本身也随之中断。刮削与
扫描都是幂等的（upsert / 增量），重启后重新点一次即可，数据不会脏。要做到
"重启续跑"需要把任务落库（`scheduled_task` / `ingest_entry` 那种模式），
当前规模不值得，留作开口。
4. **episodes JSON 双写期一致性**：同事务双写保证不漂移；M4 收口前订阅侧
   任何新读取需求一律读 JSON（老路径），避免混用。
5. **刮削与重识别竞态**：重识别改锚后旧 `media_metadata` 随旧锚保留（锚是
   全局的，别的文件可能还挂着），新锚走 4.1 触发刮削；无互斥需求。

## 9. 实施记录（2026-07-25，与草案的偏差）

1. **episodes JSON 一步收口**（用户决策：dev 阶段可删库重来，不做数据
   迁移）：跳过 M1 双写过渡，`media_season.episodes` 直接删列，订阅读路径
   （`expected_units`、季选择器已播统计、海报墙缺集统计、分集区）一次性
   迁到 `media_episode` 表。
2. **NFO 镜像加身份置信门槛**（实施期发现的反馈回路）：扫描名称收敛
   （identity_source=RESOLVED）的条目**不写** NFO——机器低置信身份一旦写成
   NFO，下次识别会把它当权威读回，错挂自我固化、重识别翻案通道失效。
   只有人工认领 / 目录 tmdbid 标记 / 既有 NFO / 入库管线锚定的身份才镜像
   完整 NFO（含把我们自己的最小身份 NFO 原地升级为完整版，tmdbid 相同才
   升级）。图片镜像不受此限（识别链不读图片）。
3. **一次入库刮削的文本部分零额外请求**：`ensure_media_item` 建档时手里
   就有完整 TMDB 档案（fetch_media_profile 已扩展出展示层字段），三表随
   建档事务同写；刮削管线（`media_scrape.scrape_media_item`）只在刷新类
   入口全量执行，图片与媒体目录镜像由 `ensure_assets` 在挂锚后异步补齐。
   原 `media_refresh` 的工单生长逻辑随管线合流迁入 `media_scrape`
   （任何入口写新集都必须走同一段 diff，否则先写库的入口会吃掉新集信号）。
