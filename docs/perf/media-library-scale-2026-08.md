# 媒体库大体量性能评估报告（2026-08）

> 在 **79 个媒体库 / 6.98 万条台账 / 单库最大 9,500 个条目** 的真实体量下端到端
> 测量媒体库读路径，定位九个瓶颈，并把其中**三个无风险项落地验证**。
>
> 已落地：单库海报墙 486 ms → 46 ms（**10.6×**）、A-Z 索引条 418 ms → 41 ms
> （**10.3×**）、库列表 131 ms → 56 ms（**2.3×**）、海报墙滚动流量
> 21.0 MB → 7.6 MB（**−64%**）。
>
> **一条初版结论被后续验证推翻**：收窄 SQLite 连接池（B2）虽能把并发吞吐
> 提升 6 倍，但会在扫描期间饿死所有读请求，**不是无风险改造，已撤回**——
> 详见 §3 B2 与 §5。
>
> 复现脚本在 `scripts/perf/`，任何人都能在本机重跑本报告的全部数字。

---

## 1. 测试方法

### 1.1 环境

| 项 | 值 |
|---|---|
| CPU / 内存 | 4 vCPU Intel Xeon @ 2.10 GHz / 16 GB |
| 后端 | Python 3.11.15 + FastAPI + SQLAlchemy async + aiosqlite（WAL） |
| 前端 | Next.js 15 **生产构建**（`next build` 产物，非 dev server） |
| 浏览器 | Chromium headless，视口 1440×900，无网络节流 |
| 调度器 | `SCHEDULER_ENABLED=false`（隔离定时任务噪声，只测读路径） |
| 数据库 | SQLite 单文件 149 MB，结构由 alembic `upgrade head` 生成 |

### 1.2 数据集怎么造的

`scripts/perf/seed_library_dataset.py` 直接按**真实 schema** 批量灌库。不走入
库管线是因为管线要真实文件、ffprobe 与 TMDB，几万条根本跑不完；而读路径的
成本只取决于落库的行长什么样，只要列口径（`state` / `media_item_id` /
季集号 / `audio_streams` 三态 / `added_batch_id`）与扫描产出的一致，测出来的
就是真实部署的成本。

数据形态刻意贴近真实用户：剧集按「剧 × 季 × 集」展开（台账行爆炸的真实来
源）、标题中英混排（拼音排序键的真实分布）、掺入未识别 / 已忽略 / 缺失文件
（读路径的三条分支都要被走到）、15% 的文件未探测介质规格。

| 表 | 行数 |
|---|---|
| `library` | **79** |
| `library_file` | **69,798** |
| `media_item` | 37,363 |
| `media_episode` | 37,674 |
| `media_season` | 1,806 |
| `job`（90 天保留窗口内的扫描/整理/刷新历史） | 17,863 |
| 本地海报资产 | 37,363 张真实 JPEG（500×750，约 81 KB/张） |

规模最大的五个库：

| 库 | 类型 | 条目数 | 文件数 |
|---|---|---|---|
| 电影库·主库 | movie | **9,500** | 9,622 |
| 剧集库·主库 | tv | 246 部剧 | **9,325**（9,200 集） |
| 动漫库·主库 | tv | 213 部剧 | 7,705 |
| 电影库·4K 收藏 | movie | 5,400 | 5,472 |
| 纪录片库·主库 | movie | 5,100 | 5,167 |

海报资产是**真实存在的 JPEG 文件**（`scripts/perf/seed_poster_assets.py`）。
第一轮只灌库不落图，浏览器测出来的是一屏 404、图片解码与传输全部缺席，
那不是用户真实看到的页面——补齐后前端数字才可用。

### 1.3 三条测量线

1. **接口串行采样**（`scripts/perf/bench_library_api.py`）：每个场景预热一次
   再打 10 次，取 P50/P95/Max。
2. **接口并发形态**：媒体库首页的真实形态是「1 个库列表 + 79 个逐库条目请求
   同时飞出去」，单独测这个扇出的墙钟，并模拟浏览器 HTTP/1.1 的 6 并发上限。
3. **浏览器端到端**（`scripts/perf/e2e_library_ux.py`）：真实 Chromium 走完
   登录 → 首页 → 单库墙 → 滚动 8 屏 → 条目详情 → 后退，采集 FCP/LCP、
   长任务、请求数与字节数，另跑一轮 4× CPU 节流模拟 NAS/老设备。

---

## 2. 现状基线

### 2.1 接口响应（79 库 / 6.98 万台账 / 1.79 万 Job 历史）

| 场景 | P50 | P95 | 响应体 |
|---|---:|---:|---:|
| 媒体库首页·库列表 | 130.9 ms | 136.3 ms | 63.9 KB |
| 媒体库首页·最近观看 | 7.9 ms | 9.8 ms | 0.1 KB |
| **单库海报墙·首屏（按标题, 60）** | **486.5 ms** | **689.6 ms** | 24.8 KB |
| 单库海报墙·翻到中段（按标题, 60） | 501.4 ms | 732.8 ms | 25.2 KB |
| 单库海报墙·翻到尾部（按标题, 60） | 464.4 ms | 648.3 ms | 25.2 KB |
| 单库海报墙·最近添加（60） | 18.8 ms | 19.7 ms | 25.1 KB |
| 单库海报墙·补探优先（60） | 469.9 ms | 641.7 ms | 24.9 KB |
| 剧集大库海报墙·首屏（按标题, 60） | 46.6 ms | 259.8 ms | 42.0 KB |
| 小库海报墙·首屏（按标题, 60） | 11.7 ms | 12.9 ms | 1.4 KB |
| **A-Z 索引条（电影大库）** | **417.5 ms** | **632.6 ms** | 1.0 KB |
| A-Z 索引条（剧集大库） | 7.7 ms | 8.5 ms | 0.7 KB |
| 已入库 id 集合（电影大库） | 20.0 ms | 222.9 ms | 45.4 KB |
| **媒体库搜索（关键词）** | **301.5 ms** | 500.2 ms | **300.4 KB** |
| 待识别清单（全部库） | 55.2 ms | 56.0 ms | 259.4 KB |
| 已忽略清单（全部库） | 59.7 ms | 62.2 ms | 65.5 KB |
| 条目详情（剧集大库） | 15.3 ms | 212.0 ms | 20.9 KB |

**并发形态**：

| 场景 | 墙钟 |
|---|---:|
| 首页扇出（1 + 79 请求，无并发限制） | **8,207 ms** |
| 同一批请求，限 6 并发（浏览器 HTTP/1.1 真实上限） | **6,713 ms** |
| 同一批请求，**完全串行** | **1,046 ms** |

> 串行比并发快 7 倍——这不是笔误，机理见 §3 B2（该项的「收窄连接池」
> 建议后来被推翻，原因同节）。

### 2.2 浏览器端到端（真实 Chromium，含真实海报）

| 步骤 | 指标 |
|---|---|
| 媒体库首页·冷加载 | 首张库卡片可见 **615 ms**；整页数据加载完 **3,922 ms** |
| 　FCP / LCP | 92 ms / 256 ms |
| 　长任务 | 5 个，合计 1,218 ms，最长 **598 ms** |
| 　接口请求 | **95 个 / 692 KB**，其中 `/libraries/{id}/items` 占 **79 个 / 645 KB** |
| 　页面结构 | 79 张库卡片 + 79 行「最近添加」，**16,620 DOM 节点**，1,337 张 img |
| 　页面高度 | **28,999 px = 32.2 屏** |
| 单库海报墙 | 首格海报可见 **1,743 ms**；LCP 1,688 ms |
| 滚动 8 屏 | 262 个请求 / **21.0 MB**；之后仍需等待 **615 ms** 才安静 |
| 条目详情打开 | **3,037 ms** |
| 后退回海报墙 | **1,340 ms** |
| 4× CPU 节流下首页 | 首卡片 1,524 ms，整页 2,551 ms，长任务合计 2,182 ms |

首页实拍（79 个库、36,456 部电影、1.14 PB）：

![媒体库首页](./assets/media-library-home.jpg)

---

## 3. 瓶颈分析

### B1 · 拼音排序键的 LRU 缓存**恒定 0% 命中**（P0，数量级）✅ 已落地

`services/library/sort_key.py:34`

```python
@lru_cache(maxsize=8192)
def title_sort_key(title: str) -> tuple[int, str]:
```

海报墙按标题排序不走 SQL（SQLite 按码点排中文对用户无意义），改在 Python 里
用 pypinyin 算排序键，靠这个 lru_cache 摊薄成本。设计是对的，**容量选错了**：
一个 9,500 条目的库每次翻页都要按同样的顺序遍历同样的 9,500 个标题，而缓存只
装得下 8,192 个——LRU 每次淘汰的恰好是下一次马上要用的那一个，命中率**恒为
零**。这是 LRU 最经典的失效形态（顺序扫描的工作集大于缓存容量）。

实测（同一批 9,500 个标题连排三轮）：

```
round0: 406.1ms  CacheInfo(hits=0, misses=9500, maxsize=8192, currsize=8192)
round1: 392.2ms  CacheInfo(hits=0, misses=19000, maxsize=8192, currsize=8192)
round2: 454.1ms  CacheInfo(hits=0, misses=28500, maxsize=8192, currsize=8192)

改成 maxsize=None：
round0: 423.8ms  ← 首轮照样要算
round1:   2.2ms  CacheInfo(hits=9500, ...)   ← 190×
round2:   2.1ms
```

拆解一次 `/libraries/1/items?sort=title&limit=60`（P50 486 ms）：

```
SQL 取 (id, title) 两列   18.0 ms
Python 拼音排序          423.8 ms   ← 87%
本页 60 个条目的聚合       ~45 ms
```

**最危险的是它是阶跃劣化**：8,192 个条目以下秒开，越过就整体崩塌。用户视角
是「昨天还好好的，今天加了几十部片突然全线变卡」，而且怎么翻页都一样慢——
因为慢的不是翻页，是每次翻页都把整库标题重排一遍。

同一份排序被 `/items?sort=title`、`/items?sort=probing`、`/item-index` 三个
端点各自独立地算一遍，这三个数字（486 / 470 / 418 ms）因此高度一致。

**已落地**：`maxsize=8192` → `maxsize=None`，并在函数上方写清了「为什么
必须无界」（否则下一个人会当成疏忽改回去）。实测 486 ms → 45.7 ms。定义域
是有界的（media_item 的标题集合），实测 37,363 个条目占 2 MB。
`tests/api/test_library_sort_key.py` 把这个不变量钉成了守护测试。

---

### B2 · SQLite 连接池并发劣化——**有真实收益，但不能这么改**（已撤回）

> ⚠️ **本节结论在初版报告里是「P0，改一行 `pool_size=1`」。后续验证表明那个
> 建议是错的，此处保留完整推理与推翻它的证据。**

`movieclaw_db/engine.py:71` 用 SQLAlchemy 默认池（`pool_size=5` +
`max_overflow=10`）。**现象确实存在**：同一批 79 个海报墙请求，只改并发度：

| 并发 | 墙钟 | 单请求 P50 |
|---:|---:|---:|
| 1 | **1,046 ms** | 11.2 ms |
| 4 | 4,559 ms | 81.2 ms |
| 8 | 6,707 ms | 195.7 ms |
| 80 | 7,444 ms | 4,605 ms |

按池大小扫描（固定并发 80）：`pool_size=1` 740 ms、2 → 1,251 ms、
3 → 1,384 ms、4 → 4,595 ms、5 → 5,327 ms、默认 5+10 → 7,104 ms。
**单调劣化，1 条连接比默认配置快 9.6 倍。**

机理：aiosqlite 的每个连接跑在自己的 OS 线程里，海报墙一次请求要走 6 次
左右 DB 往返，每次都是一轮「事件循环线程 ↔ 连接线程」交接；而聚合、ORM
装配、序列化又都在事件循环线程里持 GIL。4 个 vCPU 上十几个线程为一堆
微秒级操作互抢 GIL，线程切换开销压过了并发收益。单条轻查询不受影响
（200 次查询，并发 1 是 221 ms、并发 80 是 269 ms），只有「多次往返 +
大量 Python 侧聚合」的接口才踩中。

#### 为什么不能收窄连接池

扫描把**一个 session 持有整轮**（`services/library/scan.py:799`，
`async with db.session()` 包住了整个入库循环）。连接池一旦收窄，扫描期间
就没有连接留给读请求。实测（模拟一个 2 秒的写事务，期间持续发读请求）：

| 池配置 | 期间读完成次数 | 读 P50 | 读 Max |
|---|---:|---:|---:|
| `pool_size=1` | **1 次** | **2,071 ms** | 2,071 ms |
| `pool_size=2` | 71 次 | 1.8 ms | 9.5 ms |
| `pool_size=5+10` | 71 次 | 1.7 ms | 5.3 ms |

真实扫描是**分钟级**的——`pool_size=1` 会让整个界面在每次扫描期间彻底冻住。

再看并发扫描（库锁是按库的 `library:{id}`，不同库可以同时扫描，79 个库
开着实时监控时很常见）。规律是**池大小 N 只扛得住 N−1 个并发扫描**，
每超一个就多一次 ~1.5 s 的读停顿：

| | 1 个库在扫 | 2 个库在扫 | 3 个库在扫 |
|---|---:|---:|---:|
| `pool_size=2` 读 Max | 9.5 ms | **1,527 ms** | **3,078 ms** |
| `pool_size=3` 读 Max | 6.4 ms | 16.3 ms | **1,528 ms** |
| `pool_size=5+10` 读 Max | 5.3 ms | 6.7 ms | 7.1 ms |

任何固定的小池都能被同样数量的并发扫描打穿。**结论：现在的默认池是在为
「扫描期间界面还能用」买单，收窄它等于拿一个用户天天遇到的卡顿去换一个
只在冷启动瞬间出现的扇出问题。**

正确的解法不是调池大小，而是**让扫描别整轮持有连接**（分批提交、每批用完
就释放），之后再谈池的收窄。那是写路径改造，不在本次无风险范围内。

### B3 · 媒体库首页的 1 + N 扇出（P0，结构性）

`apps/web/components/library-view.tsx:264`

```js
const entries = await Promise.all(
  libs.map(async (lib) => [lib.id,
    await listLibraryItems(lib.id, { sort: "added_at", limit: RECENT_COUNT })]),
);
```

每个库一个请求。79 个库 = **79 个请求**，全部在打开页面的瞬间并发飞出——
正好撞进 B2 描述的并发劣化区间（而 B2 本身不能靠收窄连接池来解，见该节）。

页面本身的结构同样是问题：

| 指标 | 值 |
|---|---:|
| 库卡片 | 79 |
| 「最近添加」横滚行 | 79 |
| DOM 节点 | 16,620 |
| `<img>` 元素 | 1,337（1,327 已 lazy，这点做得对） |
| 页面高度 | 28,999 px = **32.2 屏** |
| 首屏可见的「最近添加」行 | 约 1.5 行 |

也就是说：**为了渲染用户能看见的 1.5 行内容，打了 79 个请求、拉了 645 KB、
建了 1.6 万个 DOM 节点、铺了 32 屏页面。** 97% 的内容在首屏之下，绝大多数
永远不会被看到。

值得肯定的是**空闲轮询做得很克制**：`lastLibsSnapshot` 快照比对生效，实测
首页空闲 65 秒只有 21 个请求（2 次 `/libraries` + 状态类轮询），并**没有**
每 30 秒把 79 个库的条目重拉一遍。问题只在**首次加载**这一下。

（注：`useVisiblePolling` 的 `document.hidden` 守卫经代码与实测双重确认是
正确的；headless 环境不上报 hidden，是测试工具的限制，不是产品缺陷。）

---

### B4 · 单库页轮询包重，且内含重复计算（P1）

单库页每轮刷新固定打 9 个请求。library 1（9,500 条目）实测：

| 耗时 | 大小 | 接口 |
|---:|---:|---|
| 681.7 ms | 24.8 KB | `/libraries/1/items?sort=title&limit=60&offset=0` |
| 663.0 ms | 1.0 KB | `/libraries/1/item-index` |
| 72.8 ms | 43.4 KB | `/libraries` |
| 25.0 ms | 45.4 KB | `/libraries/1/item-ids` |
| 其余 5 个 | | 合计 < 60 ms |
| **并发墙钟 1,483 ms** | 155 KB | |

前两项加起来 1.34 s，**算的是同一份 9,500 个标题的拼音排序，算了两遍**
（`build_library_wall` 与 `build_library_index` 各调一次 `_titles_sorted`）。

轮询间隔空闲时 30 s、**扫描中 3 s**。扫描期间一个开着的标签页就要 1.48 s／3 s
≈ **持续占用半个核**——而扫描恰恰是最需要 CPU 的时候。

---

### B5 · 海报墙不请求 `poster-card` 派生图（P1）✅ 已落地

项目已经有完整的派生图机制（`services/image_variants.py`，`poster-card` =
328×492 WebP q80），`recent-watch-row.tsx` 也正确用上了：

```js
const src = imageUrl(posterUrl, "poster-card");
```

但**全站最大的两张海报墙都漏了变体参数**：

- `apps/web/components/library-view.tsx:586` → `imageUrl(item.poster_url)`
- `apps/web/components/library-detail-view.tsx:1104` → `imageUrl(item.poster_url)`

实测同一张海报：

| | 大小 |
|---|---:|
| 原图直出 | 82,188 B |
| `?variant=poster-card` | **29,264 B（−64%）** |

端到端影响：海报墙滚动 8 屏实测拉取 **21.0 MB**；用上变体约 7.5 MB。
一个 60 格的首屏从 4.9 MB 降到 1.7 MB。

**已落地**：两处各加一个参数。落地前实测了海报的真实渲染宽度——1280~3440 px
视口下均为 150~170 CSS px（`auto-fill` 把列宽控住了），确认 328px 的预设覆盖
2× 屏，不是拿清晰度换流量。实测滚动 8 屏 **21.0 MB → 7.6 MB（−64%）**。

---

### B6 · 库封面过大，且每次访问都强制回源校验（P2）

`/libraries/{id}/cover` 返回 **1260×600 JPEG，195 KB**，而卡片实际显示宽度
约 340 px——按面积算浪费了约 13 倍像素。

响应头 `Cache-Control: no-cache` + ETag：语义上是对的（换图要立刻生效），
代价是**每次进首页，79 张封面各要一次条件请求**，换回 79 个 304。首页实测
里 6 张进入视口的封面就吃掉了 1.15 MB。

---

### B7 · `asset_version()` 每个条目一次同步 `stat()`（P2，NAS 上会放大）

`services/library/items.py:513`

```python
poster_url = f"/images/assets/{rel}?v={asset_version(rel)}"
```

`asset_version()` 里是 `(assets_root() / rel_path).stat().st_mtime`——**同步
阻塞调用，在 async 事件循环里，每个条目一次**。

本地 SSD 上实测 60 次共 0.48 ms（热）/ 3.77 ms（冷），可以忽略。但 movieclaw
的目标用户大量把 `data/` 放在**网络挂载**上（SMB/CIFS/NFS，项目自己在
`Library.realtime_watch` 的注释里就点名了这个场景）。那里每次 `stat` 是一次
网络往返，按 5 ms 算，一页 60 格就是 **300 ms，而且是阻塞事件循环的 300 ms**
——整个进程在这期间处理不了任何其他请求。

---

### B8 · 媒体库搜索：全表 `LIKE` + 结果无上限（P2）

`services/library/items.py:556`

```python
pattern = f"%{keyword.strip().lower()}%"
... or_(func.lower(MediaItem.title).like(pattern),
        func.lower(MediaItem.original_title).like(pattern))
```

前缀通配符让任何索引都用不上，必然全表扫 37k 行；命中集**不设上限**，且对
每个命中库都完整跑一次 `_aggregate_wall_views`。

实测 `keyword=长安`：P50 **301 ms**，响应体 **300 KB**。搜索是输入即触发的
交互，300 ms + 300 KB 已经能被明显感知；用户打一个高频单字会更糟。

---

### B9 · `/libraries` 里的 79 次 `list_jobs`（P2，随时间劣化）✅ 已落地

`api/routes/libraries.py:495`

```python
scan_views = {
    row.id: await _persistent_scan_views(session, row.id) for row in rows ...
}
```

字典推导里 `await`——**逐库串行**，每个库一次 `job` ⋈ `job_resource` 查询。

| `job` 表规模 | `/libraries` P50 |
|---|---:|
| 0 行（全新部署） | 70 ms |
| 17,863 行（90 天正常使用） | **130.9 ms** |

`job` 表保留 90 天，会随使用持续增长；而 `/libraries` 被首页和单库页双双
每 30 秒轮询。另外 `job` 表上**没有 `created_at` 索引**，而 `list_jobs` 正是
`ORDER BY created_at DESC LIMIT n`。

**已落地**：新增 `jobs.list_jobs_by_resource()`（窗口函数按资源分区取前 N），
`list_libraries` 一次查完再逐库套用。取数与判定拆开：单库详情页仍走原来的
逐个查询，两条路径共用 `_scan_views_from_jobs`，口径不会分叉。同时补上
`job.created_at` 索引——这顺带修了个**漂移**：模型 `job.py:171` 一直声明
`index=True`，历史迁移只给 `job_event` 建了索引、漏了 `job` 本身。
实测 **130.9 ms → 56.1 ms**，且接口响应与改动前**逐字节一致**
（sha256 相同），等价性另有 `tests/api/test_jobs_batch_lookup.py` 守护。

---

## 4. 已落地的改动与实测效果

三个无风险项已落地并合入本分支。测量在**同一份数据、同一台机器**上做前后
两轮（基线轮把改动 stash 掉、数据库降回改动前的 revision，确保对比公平）。

### 4.1 落地清单

| # | 改动 | 文件 | 风险论证 |
|---|---|---|---|
| B1 | 拼音排序缓存 `maxsize=8192` → `None` | `services/library/sort_key.py` | 纯记忆化，定义域有界（实测 37,363 条标题占 2 MB）；输出完全不变 |
| B5 | 海报墙补 `poster-card` 派生图参数 | `library-view.tsx`、`library-detail-view.tsx` | 后端早已支持该预设、`recent-watch-row` 已在用；实测海报渲染宽度 150~170 CSS px，328px 覆盖 2× 屏 |
| B9 | 库列表批量查作业 + `job.created_at` 索引 | `services/jobs.py`、`api/routes/libraries.py`、新增迁移 | 接口响应逐字节比对一致；索引是**补上模型早已声明却漏建**的那一个 |

配套的守护测试：

- `tests/api/test_library_sort_key.py`：断言整库标题重排第二轮**必须全部命中
  缓存**，并直接断言 `maxsize is None`。已验证它在改回 `8192` 时确实失败
  （`hits=0, misses=24000`）——守护测试自己也要被守护。
- `tests/api/test_jobs_batch_lookup.py`：批量查询与逐个查询的**等价性**、
  「每个资源各取前 N 而不是总共 N」（改批量最容易踩的坑）、按类型过滤、
  空输入与不存在资源的边界。

`job.created_at` 索引这一条顺带修了个**模型与表结构的漂移**：
`models/job.py:171` 一直写着 `index=True`，但历史迁移只给 `job_event` 建了
索引、漏了 `job` 本身，而 `list_jobs` 恰恰一直在 `ORDER BY created_at DESC`。

### 4.2 接口层（P50，79 库 / 6.98 万台账 / 1.79 万 Job）

| 场景 | 改动前 | 改动后 | 变化 |
|---|---:|---:|---:|
| **单库海报墙·首屏（按标题, 60）** | 486.5 ms | **45.7 ms** | **10.6×** |
| 单库海报墙·翻到中段 | 501.4 ms | **47.3 ms** | **10.6×** |
| 单库海报墙·翻到尾部 | 464.4 ms | **48.0 ms** | **9.7×** |
| 单库海报墙·补探优先 | 469.9 ms | **55.5 ms** | **8.5×** |
| **A-Z 索引条（电影大库）** | 417.5 ms | **40.7 ms** | **10.3×** |
| **媒体库首页·库列表** | 130.9 ms | **56.1 ms** | **2.3×** |
| 剧集大库海报墙·首屏 | 46.6 ms | 49.1 ms | 持平 |
| 媒体库搜索 | 301.5 ms | 274.8 ms | 持平（B8 未做） |
| 首页扇出墙钟（1+79 并发） | 8,207 ms | 8,097 ms | 持平（B3 未做） |

库列表 56.1 ms 比**空 job 表时的 70 ms 还快**——批量化省掉了 78 次往返，
索引又让排序不必扫全表。

剧集大库、小库、搜索等场景持平是**预期内**的：它们的条目数没到让缓存颠簸
的量级，本来就不慢。

### 4.3 浏览器层（真实 Chromium，同一数据状态前后对比）

| 步骤 | 改动前 | 改动后 | 变化 |
|---|---:|---:|---:|
| **单库墙·首格海报可见** | 2,405 ms | **887 / 1,417 ms**（两轮） | **~2.1×** |
| **单库墙·LCP** | 2,340 ms | **996 / 1,520 ms** | **~1.9×** |
| **单库墙·加载字节** | 3,598 KB | **1,438 KB** | **−60%** |
| **滚动 8 屏拉取** | 20,970 KB | **7,606 KB** | **−64%** |
| **媒体库首页·加载字节** | 5,073 KB | **3,051 KB** | **−40%** |
| 媒体库首页·整页加载 | 5,438 ms | 5,504 / 4,541 ms | 持平（B3 未做） |
| 后退回海报墙 | 1,972 ms | 1,707 / 998 ms | ~1.5× |

字节数在两轮里完全一致（3,051 / 1,438 / 7,606 KB），是确定性的硬数字；
耗时有正常抖动，所以两轮都列出来。

**关于「条目详情打开」这一项**：前后测得 2,438 ms vs 2,645/3,321 ms，看似
变慢，但查证后与改动无关——详情页那 13 个图片请求全部是打向 TMDB 的代理
请求（演员头像、背景图），而压测数据里的 TMDB 路径是合成的，每个请求都要
等一次外网往返再 404。这一项测的是 TMDB 超时，不是应用本身，不应计入。

### 4.4 回归验证

全量 `pytest -m "not integration"` 在改动前后各跑一遍，**失败集合完全相同
（39 个）**，零新增失败。这 39 个都是本机环境缺失导致的既有失败：

| 数量 | 模块 | 原因 |
|---:|---|---|
| 20 | `tests/enrich/test_enrich.py` | NER 模型未下载 |
| 9 | `tests/docker/test_entrypoint_supervision.py` | 需要 docker |
| 4 | `tests/api/test_image_proxy.py` | 需要外网 |
| 6 | 其余（backfill / egress / search / library_scan） | 同样依赖 NER 模型或外网 |

---

## 5. 剩余优化方案

### 已撤回：收窄连接池（原 P0 #2）

初版报告把它列为「改一行、风险低」。§3 B2 的后续验证推翻了这个判断：
扫描整轮持有一个 session，收窄池会让扫描期间的读请求被饿死
（`pool_size=1` 下 2 秒写事务里只有 1 次读完成、耗时 2,071 ms）。
**真正该做的是让扫描分批提交、用完即释放连接，之后再谈池。**

### P1 — 结构性，收益最大的一块

**1. 拆掉首页的 1+N 扇出（B3）**

现在仍是 79 个库 = 79 个请求 / 645 KB，页面 32.2 屏、16,620 个 DOM 节点，
而首屏只看得见约 1.5 行内容。两条路建议都做：

- 服务端加批量端点 `GET /libraries/recent-items?limit=20`，一条窗口函数 SQL
  （`ROW_NUMBER() OVER (PARTITION BY library_id ORDER BY created_at DESC)`）
  定页后走一次 `_aggregate_wall_views`，79 个请求压成 1 个。参考地板值：
  进程内串行循环跑完 79 个库是 743 ms，真正的批量 SQL 应显著低于它。
- 前端对「最近添加」行上 `IntersectionObserver` 懒加载。这条即使不做批量
  端点也能独立生效，且对超过 100 个库的用户是唯一可扩展的解。

之所以没在本轮做：它改变首页的加载时序（行随滚动出现），是可感知的行为
变化，不属于「无风险」。

**2. 单库页轮询瘦身（B4）**

B1 落地后单次成本已降一个数量级（`/items?sort=title` 682 → 46 ms，
`/item-index` 663 → 41 ms），轮询包的绝对开销大幅缓解。剩下的两件事：
轮询分档（进度类高频、清单类低频或按面板展开拉取）；两个端点共用一次
排序结果，省掉每轮重复的那一遍。

### P2 — 随后跟进

**3. `asset_version` 去 syscall（B7）**：把海报文件的 mtime 随 `poster_file`
一起落 `media_metadata`，读路径零 `stat()`。本地 SSD 上可忽略，但网络挂载
（SMB/NFS，NAS 常态）上每页 60 格是 60 次网络往返、约 300 ms **阻塞事件
循环**。对 NAS 用户这一条的优先级要往上提。

**4. 库封面（B6）**：现在是 1260×600 / 195 KB 服务一个约 340 px 宽的卡片。
按显示尺寸出图（复用 `image_variants` 预设）；`no-cache` 换成
`max-age=60, stale-while-revalidate` 并保留 ETag——换图最多晚一分钟生效，
换来首页少 79 次条件请求。需要确认与 Jellyfin 兼容层共用同一张图的影响。

**5. 搜索加上限并上 FTS5（B8）**：现在 P50 275 ms / 300 KB 响应体。先加硬
上限（每库 Top 20 + 全局 200），中长期给标题与别名建 FTS5 虚表把
`LIKE '%kw%'` 换成 `MATCH`。加上限会改变用户看到的结果集，属产品决策，
故未纳入本轮。

**6. 让扫描别整轮持有连接**：这是解锁 B2 那 6 倍并发吞吐的前置条件，
本身也能改善扫描期间的整体响应。属写路径改造，需配套并发回归。

---

## 6. 怎么复现

```bash
# 1. 建库并灌数据（约 6 秒）
python -c "import asyncio; from movieclaw_db.engine import init_db; \
  from movieclaw_db.migrations import run_migrations; \
  init_db('sqlite+aiosqlite:///./data/movieclaw.db'); \
  asyncio.run(run_migrations())"
python scripts/perf/seed_library_dataset.py --db data/movieclaw.db
python scripts/perf/seed_poster_assets.py --db data/movieclaw.db

# 2. 起后端 + 前端（前端要 next build 产物，dev server 的数字没有参考价值）
python -m movieclaw_api.main &
(cd apps/web && npm ci && npx next build && npx next start &)

# 3. 初始化管理员后跑两条测量线
python scripts/perf/bench_library_api.py --password '<密码>' --repeat 10
python scripts/perf/e2e_library_ux.py    --password '<密码>' --library 1
```

要复现 §4 的前后对比，基线轮必须把**代码和数据库**一起退回改动前——只 stash
代码而库里还留着 `ix_job_created_at`，量出来的「基线」是偏快的：

```bash
git stash push -u -- src/ apps/ alembic/ tests/
python -m alembic downgrade b9c2d5e8f014      # 退掉索引迁移
# …跑基线轮，跑完再 git stash pop 并重新 next build
```

两轮之间还要先把 79 张库封面预热一遍（`/libraries/{id}/cover` 首次访问会
服务端渲染拼贴），否则封面渲染的开销会落在先跑的那一轮头上。

## 7. 本报告没有覆盖的部分

- **写路径**：扫描、整理、入库管线未测——需要真实文件与 ffprobe，
  是独立的一轮工作。B2 的连接池改动落地前必须补写路径的并发回归。
- **多用户并发**：只测了单用户的请求形态。成员账号的可见性过滤
  （`visible_library_ids`）在多成员下的成本未评估。
- **Jellyfin 兼容层**：`/Items`、`/Items/Counts` 等兼容端点共用同一套聚合，
  推测同样受 B1/B2 影响，但未单独采样。
- **图片子系统的真实上限**：测试用的是 81 KB 的合成海报，真实 TMDB 海报
  的体积分布更宽；`/images/proxy` 的回源路径（本次因合成数据全部 404）
  未纳入测量。
