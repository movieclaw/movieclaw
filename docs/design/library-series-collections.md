# 系列合集：让 TMDB / NFO 里的「系列」自动成集

> 目标（用户 2026-09-10 提出）：
> 1. 刮削时 TMDB 有系列 → 按 NFO 规范写出，同时我们这边自动建合集；
> 2. Jellyfin 播放器接口正确输出；
> 3. NFO 里读得出系列 → 入库时我们的合集也要有记录可见。
>
> 前置：合集本身的模型与接口见 [library-collections.md](library-collections.md)，
> 筛选见 [library-filtering.md](library-filtering.md)。本文只讲「系列」这一层。

## 0. 现状（2026-09 核实过的，不是印象）

| 事 | 现状 |
|---|---|
| 刮削存系列吗 | **不存**。`MediaMetadata` 没有任何系列字段 |
| TMDB 给了吗 | **给了，而且免费**：`belongs_to_collection` 在 `/movie/{id}` 的基础响应里，`fetch_media_profile` 已经拿到了 `data`，只是没读它 |
| NFO 写系列吗 | **不写**。`write_full_nfo` 的标签是 title/originaltitle/year/tmdbid/uniqueid/plot/tagline/runtime/mpaa/premiered/ratings/genre/studio/country/director/actor，**没有 `<set>`**——而 Emby/Jellyfin/Kodi 认合集正是靠它 |
| NFO 读系列吗 | **不读**。`read_entry_metadata` / `read_local_sidecar` 都没有解析 `<set>` |
| 唯一碰系列的地方 | 发现页详情的实时链路（`movieclaw_media/service.py::_movie_collection`），拉 `collection/{id}` 渲染「系列电影」一行，**用完即弃** |

结论：这三件事**全部是新增**，没有既有实现可以复用，也没有既有数据可以迁移。

## 1. 第一性判断：系列合集不是新实体

最容易走错的一步，是给「系列」单开一张表（`media_series` + `series_item`），
然后写一套同步逻辑把它和 `collection` 对起来。**不要。**

系列合集就是**规则驱动的合集**，规则是「属于系列 X」：

```
rules = [{"field": "collection_key", "op": "any_of", "values": ["tmdb:1241"]}]
```

这一句话决定了后面所有事都不用写：

| 想要的行为 | 谁做的 |
|---|---|
| 新入库一部《哈利·波特》续集，自动进合集 | 不用做。规则驱动的合集本来就是查询时求值（collections.md 1.3 不物化） |
| 网页与 Jellyfin 成员一致 | 不用做。两边都走 `resolve_members()` → `_wall_page_ids()` |
| Jellyfin 里出现一个 BoxSet | 不用做。`visible_collections()` 已经把所有合集下发成 BoxSet |
| 合集详情页、chip、封面 | 不用做。F3.2 那套 UI 不区分合集从哪来 |

**这正是 collections.md 1.2「这个抽象要吃掉既有特例」的第二次兑现**——第一次是
「我的收藏」。真要新开一张表，就说明抽象没立住。

## 2. 数据模型：一列 + 一个规则字段

### 2.1 `media_metadata` 加两列

```python
collection_key: str | None   # 成员判定的唯一依据："tmdb:1241" / "name:哈利波特系列"
collection_name: str | None  # 系列展示名，建合集时用；不参与判定
```

**为什么是一个 `collection_key` 而不是 `tmdb_collection_id` + `set_name` 两列**：
两列意味着两个规则字段，而一部片可能两者都有（TMDB 给了 id，NFO 还写着名字），
于是它同时落进两个合集，用户看到两个几乎一样的系列。一个规范化的 key 从构造上
杜绝这件事——写入时定优先级：**有 TMDB id 就是 `tmdb:{id}`，否则才是 `name:{规范化名}`**。

`name:` 那一支只会出现在**没有 TMDB 身份的条目**上（本地库、认不出的片），
与 `tmdb:` 天然不相交。

### 2.2 筛选加一维

`items.py` 的 `LibraryFilter` 加 `collection_keys: tuple[str, ...]`，
`_filter_subquery()` 加一条 `MediaMetadata.collection_key.in_(...)`。
`collections.py` 的 `rules_to_filter()` 认 `collection_key` 字段。

一维、一条 WHERE、一个字段名——**筛选的唯一收窄点不变**。

### 2.3 迁移

纯新增两列，向前兼容（CLAUDE.md 硬约束 3）。旧版本回退后不认识这两列，读写都不碰。

## 3. 刮削侧：写 NFO + 建合集

### 3.1 取值（零额外请求）

`fetch_media_profile` 已经有 `data`，加两行：

```python
summary = data.get("belongs_to_collection") or {}
profile.collection_tmdb_id = summary.get("id")
profile.collection_name = summary.get("name")
```

`MediaProfile` 加这两个字段，`apply_display_profile()` 落到 `media_metadata`
（顺手把 `collection_key` 拼出来）。

> 系列自己的海报/背景要另拉 `collection/{id}`——**一期不做**，封面用现成的
> 「首位成员海报叠放」（F3.2 已实现，实机看下来够用）。见第 9 节开放问题。

### 3.2 写 NFO

`write_full_nfo()` 加 `<set>`，按 Kodi v18+ 的嵌套写法：

```xml
<set>
  <name>哈利·波特系列</name>
</set>
```

三条硬约束：

- **必须走现有闸门**：`effective_mirror_flags(library)` 的 `write_nfo` 与
  `trusted_entries`。加了 `<set>` 不等于可以绕过"要不要动用户媒体目录"这一问。
- **不发明非标标签**。不往 `<set>` 里塞 `tmdbid`——那不是通行写法；重装后
  重新刮削一次就能把 id 拿回来，不值得为它污染别人的库。
- **内容不变则不改 mtime**：现有 `write_full_nfo` 已经这么做，加字段后仍成立。

### 3.3 建合集（幂等）

刮削落库后，若 `collection_key` 非空，就 ensure 一个合集：

```
builtin = f"series:{collection_key}:{library_id}"     # builtin 上有唯一约束
name    = collection_name
rules   = [{"field": "collection_key", "op": "any_of", "values": [collection_key]}]
sort    = "release_date"                              # 系列要按上映顺序看，不是按标题
```

与 `ensure_builtin_collections()` 同一手法（先查后插，幂等），放在
`services/library/collections.py`，不要写在刮削里——刮削只管落列。

## 4. 读 NFO 侧：第三方整理过的库

这一支服务的是「用 TMM/Kodi 整理过、直接挂进来」的用户。

- `read_entry_metadata()` 与 `read_local_sidecar()` 加 `<set>` 解析，**两种写法都认**：
  `<set>名字</set>`（Kodi v17-）与 `<set><name>名字</name></set>`（v18+）。
- 扫描入库时，若该条目**没有 TMDB 身份**（本地库 / 认不出），把 `name:{规范化名}`
  写进 `collection_key`，并 ensure 同样一个合集。
- 若有 TMDB 身份，NFO 里的 set **只作参考不落 key**——以 TMDB 为准（2.1 的优先级）。

> **注意现状**：`read_entry_metadata` 今天只在**详情页降级展示**时被调用
>（`items.py:1891`），不在入库路径上。所以第 4 节要在扫描侧**新接一处**读取，
> 不是改个现成的调用点。这是本次改造里最容易低估的一块。

## 5. Jellyfin：不需要新代码，但需要核

系列合集就是 `Collection` 行，`/UserViews` 的「合集」视图与
`/Items?ParentId=<合集视图>` 的 BoxSet 列表**自动包含它们**（F3.3 已实现）。

要核的是**客户端观感**，按 collections.md 4.10 那张清单：

- BoxSet 数量从个位数涨到几十个之后，Infuse / Jellyfin 官方端的合集视图还好用吗；
- BoxSet 封面用成员海报叠放，在电视端 10 尺距离下认不认得出；
- 一部片同时属于系列合集与用户合集时，两个 BoxSet 都列出它——协议上合法，
  但要确认客户端不会显示成"重复条目"。

**协议层还有一个待核项**：Jellyfin 的 `BaseItemDto` 上没有"我属于哪个 BoxSet"
的标准字段，客户端（如 Infuse）在影片页显示系列信息时读的是什么，我**没有核实过**，
不能假设我们输出 BoxSet 就自动有。列为验收项，不是已知能力。

## 6. 产品面：怎么不把用户淹没

这是本次最容易做砸的地方。一个 300 部的电影库可能有 40+ 个系列，
如果它们和用户自己存的三五个筛选混在一起平铺，**用户存的那几个就没了**。

| 问题 | 对策 |
|---|---|
| 一部片也算"系列" | 成员 **< 2 不下发**（列表侧按 builtin 前缀推导阈值，与"空合集不列"同一处闸门） |
| 系列淹没自建合集 | 合集视图**分组**：「我的合集」在前，「系列」在后；chip 行只放自建的 |
| 用户不想要某个系列 | **需要一个隐藏态**——见下方缺口 |
| 完全不想要这功能 | `MetadataScrapeSetting` 加一个可按库覆盖的开关（与 `mirror_nfo` 同一组），默认开 |
| 看《哈利·波特》要按顺序 | 系列合集 `sort = release_date`（3.3 已定） |
| 从影片页找到系列 | 影片详情页加一行「所属系列」chip，点进合集详情页（filtering.md 4.3 提过的「所属合集」） |
| 分不清合集从哪来 | 卡片副行：自建写「自动收录」，系列写「系列 · 8 部」 |

### 6.1 已发现的缺口：内置合集删不掉也藏不住

`delete_collection` 现在对 builtin 报「内置合集不能删除（**可以隐藏**）」，
但**代码里没有任何隐藏机制**——`visibility` 只有 household/private，没有 hidden。

只有「我的收藏」一个内置合集时这句话不疼不痒（没人想删它，空了还会自动不列）。
一旦自动生成几十个系列合集，"删不掉也藏不住"立刻变成真问题。

**必须一起做**：`collection` 加 `hidden: bool`（或 `dismissed_at`），
`visible_collections()` 过滤掉，接口给一个「隐藏」动作，那句提示才不再是空头支票。

## 7. 存量库怎么办

新列默认 NULL，**已经刮削过的库一部都不会自动成系列**。不能要求用户整库重刮
（大库要跑很久，还会重下图）。

方案：一个轻量回填作业，只对 `tmdb_id 非空 且 collection_key 为空` 的电影
发一次 `GET /movie/{id}`（**不带 append_to_response**，比完整刮削便宜一个量级），
只取 `belongs_to_collection` 落列 + ensure 合集。可中断可重入，
按现有作业中心的惯例给进度。

> NFO 那一支的回填同理：扫描时顺带补，不单开作业。

## 8. 分期与验收

| 期 | 内容 | 验收 |
|---|---|---|
| **S1** | 两列 + 迁移 + 筛选维度 + `rules_to_filter` | 手写一条 `collection_key` 规则，海报墙与 `/collections/{id}/items` 返回同一批 id |
| **S2** | 刮削取值落库 + ensure 合集 + `sort=release_date` | 刮一部《哈利·波特》，合集自动出现且按上映排序；再刮一部续集，数量自己 +1 |
| **S3** | `write_full_nfo` 写 `<set>` | 写出的 NFO 被 Kodi / Emby 认成合集（**真机验，不靠读代码**）；`write_nfo` 关闭时一个字节都不写 |
| **S4** | 读 NFO 的 `<set>`（含扫描侧新接的读取点） | 拿 TMM 整理过的目录挂进来，不联网也能成集 |
| **S5** | 产品面：分组、隐藏态、`< 2` 不下发、影片页「所属系列」、刮削开关 | 40 个系列的库里，用户自建的合集仍在第一屏 |
| **S6** | 存量回填作业 | 老库跑完之后系列齐了，且没有重复刮削整份档案 |

**S1 与 S2 之间不要跳**：S1 的验收（同一批 id）是整条链路的地基，
它一旦不成立，后面每一层都会各写一套查询。

## 9. 开放问题（需要你拍板）

1. **系列封面**：要不要拉 `collection/{id}` 取 TMDB 官方系列海报？多一次请求
  （每个系列一次，可缓存），换来的是电视端好认得多的封面。我倾向 S5 再做。
2. **NFO 里要不要带 id**：现在定的是不带（不发明非标）。若你更看重"换个软件也能
   认出是同一个系列"，可以考虑写 `<tmdbid>` 进 `<set>`——但那不是通行写法。
3. **剧集怎么办**：TMDB 的 `belongs_to_collection` 只有电影有。剧集的"系列"
  （如《王冠》各季）本来就是同一个条目，不需要合集；跨剧的宇宙（漫威剧）
   TMDB 没有结构化数据。**建议明确不做**，免得用户以为漏了。
4. **`name:` 那一支要不要做**：它只服务"本地库 + 第三方 NFO"这一小撮场景，
   却带来规范化命名（大小写、空格、繁简）的一堆麻烦。可以 S4 再评估要不要收窄
   到"只认 TMDB id"。

## 10. 风险

| 风险 | 应对 |
|---|---|
| 写 `<set>` 动了用户的媒体目录 | 沿用 `write_nfo` 闸门与 `trusted_entries`，不新开旁路；内容不变不改 mtime |
| 自动合集淹没自建合集 | 第 6 节整节都是为这件事；`< 2` 不下发 + 分组 + 可隐藏 + 可关 |
| 系列名多语言漂移（重刮换语言 → 合集改名） | 成员判定只认 `collection_key`（语言无关的 id），名字变了只是合集改名，成员一个不动 |
| 回填打爆 TMDB 限流 | 只发轻量详情、走现有作业中心的并发与退避 |
| 「内置合集可以隐藏」是空头支票 | 6.1 一起补上 hidden，否则这个功能一上线就有一堆删不掉的合集 |
