# 媒体库大体量性能评估报告（2026-08）

> 结论先行：在 **79 个媒体库 / 6.98 万条台账 / 单库最大 9,500 个条目** 的真实
> 体量下，媒体库读路径存在两个**数量级级别**的瓶颈和一个**结构级别**的瓶颈。
> 两个数量级瓶颈各改一行代码即可修复，实测单库海报墙 486 ms → 50 ms
> （9.7×）、媒体库首页扇出 8.2 s → 1.3 s（6.2×）。
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

> 串行比并发快 7 倍——这不是笔误，是 §3.2 的瓶颈。

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

### B1 · 拼音排序键的 LRU 缓存**恒定 0% 命中**（P0，数量级）

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

**修复**：`maxsize=8192` → `maxsize=None`。一行。实测 486 ms → 50 ms。
条目上限就是媒体库条目总数，37k 条标题的键约占几 MB，可接受。若担心无界，
把缓存改成挂在 `media_item_id` 上的进程级 dict、随条目改名失效，效果相同且
容量可控。

---

### B2 · SQLite 连接池并发**反向劣化**（P0，数量级）

`movieclaw_db/engine.py:71` 用 SQLAlchemy 默认池（`pool_size=5` +
`max_overflow=10`）。对 SQLite 这是**负优化**。

同一批 79 个海报墙请求，只改并发度：

| 并发 | 墙钟 | 单请求 P50 | Max |
|---:|---:|---:|---:|
| 1 | **1,046 ms** | 11.2 ms | 26.9 ms |
| 2 | 1,397 ms | 22.8 ms | 105 ms |
| 4 | 4,559 ms | 81.2 ms | 932 ms |
| 6 | 5,975 ms | 137.3 ms | 1,956 ms |
| 8 | 6,707 ms | 195.7 ms | 2,662 ms |
| 16 | 7,330 ms | 560.5 ms | 6,266 ms |
| 80 | 7,444 ms | 4,605 ms | 7,439 ms |

按连接池大小扫描（固定并发 80）：

| 池配置 | 墙钟 |
|---|---:|
| `pool_size=1, overflow=0` | **740 ms** |
| `pool_size=2` | 1,251 ms |
| `pool_size=3` | 1,384 ms |
| `pool_size=4` | 4,595 ms |
| `pool_size=5` | 5,327 ms |
| `pool_size=8` | 6,624 ms |
| `pool_size=5, overflow=10`（当前默认） | 7,104 ms |

**单调劣化：连接越多越慢，1 条连接比默认配置快 9.6 倍。**

机理：aiosqlite 的每个连接跑在**自己的 OS 线程**里。海报墙一次请求要走 6 次
左右的 DB 往返，每次往返都是一次「事件循环线程 ↔ 连接线程」的交接；而聚合、
ORM 行装配、pydantic 序列化又都在事件循环线程里持 GIL。4 个 vCPU 上，十几个
线程为了一堆微秒级的 SQLite 操作互相抢 GIL，线程切换的开销彻底压过了并发收益。

对照组证明这不是「SQLite 天生不能并发」：**单条轻查询完全不受影响**——
200 次 `SELECT id FROM library LIMIT 1`，并发 1 墙钟 221 ms，并发 80 墙钟
269 ms，吞吐几乎持平。只有「每请求多次往返 + 大量 Python 侧聚合」的接口
才会踩中。媒体库读路径恰好全是这种形态。

**修复**：`pool_size=1~2, max_overflow=0`。同一个 SQLite 文件本来就不存在
真并行写，读也已经被 WAL 保护，多连接换不来任何并行度，只换来线程抖动。

---

### B3 · 媒体库首页的 1 + N 扇出（P0，结构性）

`apps/web/components/library-view.tsx:264`

```js
const entries = await Promise.all(
  libs.map(async (lib) => [lib.id,
    await listLibraryItems(lib.id, { sort: "added_at", limit: RECENT_COUNT })]),
);
```

每个库一个请求。79 个库 = **79 个请求**，全部在打开页面的瞬间并发飞出——
正好撞进 B2 的最坏区间。

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

### B5 · 海报墙不请求 `poster-card` 派生图（P1）

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

### B9 · `/libraries` 里的 79 次 `list_jobs`（P2，随时间劣化）

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

---

## 4. 验证实验：改两行的效果

为了让优化方案不停留在推理，把 B1 与 B2 各改一行，用完全相同的脚本重测
（改动仅用于实验，**未合入本分支**，源码已还原）：

```diff
- @lru_cache(maxsize=8192)          # sort_key.py:34
+ @lru_cache(maxsize=None)

  create_async_engine(              # engine.py:71
      database_url, echo=echo,
      connect_args={"check_same_thread": False},
+     pool_size=1, max_overflow=0,
  )
```

### 4.1 接口层

| 场景（P50） | 基线 | 仅改 B1 | B1 + B2 | 提升 |
|---|---:|---:|---:|---:|
| 单库海报墙·首屏（按标题, 60） | 486.5 ms | 56.0 ms | **50.5 ms** | **9.6×** |
| 单库海报墙·翻到中段 | 501.4 ms | 49.2 ms | **47.4 ms** | 10.6× |
| 单库海报墙·补探优先 | 469.9 ms | 61.6 ms | **56.6 ms** | 8.3× |
| A-Z 索引条（电影大库） | 417.5 ms | 42.3 ms | **39.5 ms** | 10.6× |
| **首页扇出墙钟（1+79 并发）** | 8,207 ms | 7,822 ms | **1,320 ms** | **6.2×** |
| **限 6 并发墙钟** | 6,713 ms | 6,920 ms | **1,124 ms** | **6.0×** |

两个修复**完全正交**：B1 治单请求延迟，B2 治并发吞吐。只改一个都只能拿到
一半收益——这也解释了为什么只看串行采样会漏掉 B2，只看并发压测会漏掉 B1。

### 4.2 用户可感知层（真实浏览器）

| 步骤 | 基线 | B1+B2 | 提升 |
|---|---:|---:|---:|
| 单库海报墙·首格海报可见 | 1,743 ms | **729 ms** | 2.4× |
| 单库海报墙·整页加载完成 | 1,805 ms | **1,048 ms** | 1.7× |
| 滚动 8 屏后的等待 | 615 ms | **10 ms** | 61× |
| 后退回海报墙 | 1,340 ms | **1,030 ms** | 1.3× |
| 媒体库首页·整页加载完成 | 3,922 ms | **3,142 ms** | 1.2× |
| 条目详情打开 | 3,037 ms | **2,825 ms** | 1.1× |

首页只快了 1.2×，因为它的瓶颈是 B3 的 79 个请求本身（结构问题），不是单个
请求的耗时——这正好印证了 B3 需要单独治。

---

## 5. 优化方案

### P0 — 两行改动，先做

| # | 改动 | 位置 | 预期 |
|---|---|---|---|
| 1 | `lru_cache(maxsize=8192)` → `maxsize=None` | `services/library/sort_key.py:34` | 海报墙/索引条 **9~10×** |
| 2 | 引擎加 `pool_size=1, max_overflow=0` | `movieclaw_db/engine.py:71` | 并发吞吐 **6×** |

两处都要补注释说明**为什么**（LRU 顺序扫描失效、SQLite 多连接的线程抖动），
否则下一个人会以为「缓存无界」和「池只有 1」是疏忽，顺手改回去。

配套护栏：加一个回归测试，断言在 > 10,000 条目的库上 `_titles_sorted` 二次
调用的 `title_sort_key.cache_info().hits > 0`——把「缓存必须装得下整库」这个
不变量钉死在测试里，而不是靠注释提醒。

### P1 — 结构性改动

**3. 媒体库首页的 1+N 扇出（B3）** — 两条路，建议都做：

- 服务端加批量端点 `GET /libraries/recent-items?limit=20`，一次返回所有库的
  最近添加。一条窗口函数 SQL（`ROW_NUMBER() OVER (PARTITION BY library_id
  ORDER BY created_at DESC)`）定页，再走一次 `_aggregate_wall_views`。
  79 个请求压成 1 个。参考值：进程内串行循环跑完 79 个库是 743 ms，这是
  「什么都不优化」的地板，真正的批量 SQL 应显著低于它。
- 前端对 `RecentRow` 上 `IntersectionObserver` 懒加载：32 屏的页面，只给进入
  视口的库拉数据。这条即使不做批量端点也能独立生效，且对超过 100 个库的
  用户是唯一可扩展的解。

**4. 海报墙补 `poster-card` 变体（B5）** — 两行：

```diff
- posterUrl: imageUrl(item.poster_url),
+ posterUrl: imageUrl(item.poster_url, "poster-card"),
```
（`library-view.tsx:586`、`library-detail-view.tsx:1104`）
流量 −64%，滚动 8 屏 21 MB → 7.5 MB。

**5. 单库页轮询瘦身（B4）** — 分两档：进度类（`/libraries`、扫描进度）保持
高频；清单类（待识别 / 已忽略 / 复核 / 缺失 / 订阅）降到低频或改为按面板
展开时拉取。另外让 `/items?sort=title` 与 `/item-index` 共用一次
`_titles_sorted` 的进程级结果（按 `library_id` + 库存 revision 失效），
省掉每轮重复的那一遍排序。

### P2 — 随后跟进

**6. `asset_version` 去 syscall（B7）**：把海报文件的 mtime 随 `poster_file`
一起落 `media_metadata`（刮削与换图时更新），读路径零 `stat()`。网络挂载上
每页省 300 ms 的事件循环阻塞。

**7. 库封面（B6）**：按显示尺寸出图（复用 `image_variants` 的预设机制），
`no-cache` 换成 `max-age=60, stale-while-revalidate=3600` 并保留 ETag——
换图最多晚 60 秒生效，换来首页少 79 次条件请求。

**8. 搜索加上限（B8）**：每库 Top 20 + 全局 200 的硬上限，先把 300 KB 的
响应体压下来。中长期给 `media_item.title/original_title/aliases` 建 FTS5
虚表，把 `LIKE '%kw%'` 换成 `MATCH`。

**9. `/libraries` 的 79 次 `list_jobs`（B9）**：一次查出所有 `library` 资源
的近期 job（`resource_type='library'` + 按 `resource_id` 分组取前 N），
内存里分组；顺手给 `job` 表补 `created_at` 索引（`list_jobs` 一直在
`ORDER BY created_at DESC`）。

### 优先级依据

| # | 用户感知 | 改动成本 | 风险 |
|---|---|---|---|
| 1 · LRU 容量 | 极高（阶跃式变卡的根因） | 1 行 | 极低 |
| 2 · 连接池 | 极高（并发全线劣化） | 1 行 | 低（需覆盖写路径回归） |
| 3 · 首页扇出 | 高 | 中（新端点 + 前端改造） | 中 |
| 4 · 海报变体 | 高（流量与移动端体验） | 2 行 | 极低 |
| 5 · 轮询瘦身 | 中（扫描期间尤其明显） | 中 | 低 |
| 6~9 | 中低 / NAS 用户高 | 中 | 低 |

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
