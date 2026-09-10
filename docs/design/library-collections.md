# 媒体库合集：技术方案（含 Jellyfin 兼容）

> 状态：技术方案 v1（2026-09-09），待评审。
> 上游：[library-filtering.md](library-filtering.md)（产品设计——本文是其 F3/F4 的技术落地，
> 产品判断以那份为准，本文不重复论证）。
> 关联：[jellyfin-compat.md](jellyfin-compat.md)（协议层、GUID 方案、可见性收口）、
> [library-routing.md](library-routing.md)（条件 DSL 的原产地）、
> [library-access.md](library-access.md)（成员可见库）、[library.md](library.md)（库与台账）。

## 0. 范围与第一性

**一句话**：合集是「一组 `media_item`」的持久化定义，有两种定义方式（规则 / 名单），
要同时出现在 movieclaw 自己的 web 和 Jellyfin 客户端里。

**贯穿全文的第一性约束**：

> **规则求值只能有一个实现。** movieclaw web 与 Jellyfin 兼容层必须调用同一个
> `resolve_members()`，协议层**不许自己写查询**。否则同一个智能合集在网页里 42 部、
> 在电视上 39 部——这类不一致查起来极贵，且用户会认定"这个功能坏了"。

这条约束也是 `jellyfin-compat.md` 8.3 已有的原则（"查询复用服务与仓储，不直接裸写
SQL 散落各处"）在合集上的延续。

**不在本期**：合集分享（复用 `media_share`）、跨库合集入口、TMDB 系列自动合集
——都排在 F4，模型上留好口子即可。

## 1. 数据模型

### 1.1 两张新表

```
collection
  id            int PK
  name          str
  library_id    int FK → library.id nullable   -- NULL = 跨库合集（F4 才开入口）
  rules         JSON  default []               -- 与 library.match_rules 同构
  sort          str                            -- 合集内默认排序（沿用 WallSort 取值）
  visibility    str                            -- household | private
  member_id     int FK → member.id nullable    -- private 时的归属成员
  builtin       str  nullable                  -- 内置合集标识；NULL = 用户创建（见 1.2）
  position      int                            -- 顶栏与网格的顺序
  cover_item_id int FK → media_item.id nullable
  created_at / updated_at

collection_item                                -- 手动名单；smart 合集没有行
  collection_id int FK ON DELETE CASCADE
  media_item_id int FK ON DELETE CASCADE
  position      int
  UNIQUE(collection_id, media_item_id)
```

**没有 `mode` 列**（初稿有，砍掉）。原因：`smart | manual | system` 这个三分法是假的
——前两者说的是"成员怎么来"，`system` 说的是"能不能改"，两个正交的东西挤进一列，
后面必然长出 `if mode == "system" and rules` 这种检查。改成两个各自独立的事实：

| 问题 | 答案来自 |
|---|---|
| 成员怎么来 | `rules` 非空 → 规则驱动；`collection_item` 有行 → 名单驱动 |
| 能不能改 | `builtin IS NULL` → 用户创建，可改；否则内置，只能改名/隐藏 |

形态是**推导出来的**，不是存出来的。副作用是"规则 + 手工增删"这种组合天然可表达
（Plex 的智能播放列表就是这个形态），但 v1 **不做**这个功能——只是模型没有把路堵死。

**顺带砍掉的两列**：`kind`（可从 `library.kind` 推，跨库合集本来就没有单一 kind，
存了就是一份要维护的冗余）、`pinned`（和 `position` 是同一件事的两种说法，
留 `position` 就够）。

**索引**：`collection(library_id)`、`collection(member_id)`、`collection(builtin)`、
`collection_item(collection_id, position)`。

**外键与级联**：`library` 删除时 `collection` **级联删除**——库没了，"这个库里的
一批片"这个定义也就没意义了，留一个指向空气的合集只会变成幽灵数据。跨库合集
（`library_id IS NULL`）不受影响。（与 `media_item.scrape_library_id` 的 `SET NULL`
处理不同：那一列是"归属推断"，可以重新推断；合集是用户定义的东西，宿主没了就该没了。）

### 1.2 内置合集：这个抽象要**吃掉**既有特例，而不是摆在它们旁边

只做加法的抽象是可疑的。合集这个抽象立得住，前提是它能收编已经存在的那些
"其实就是合集"的东西：

| 既有的东西 | 现状 | 登记为内置合集后 |
|---|---|---|
| 「我的收藏」 | `/library/favorites` 独立页 + `listFavorites` / `listFavoritesGallery` 一套并行的墙实现（`favorites-view.tsx` 632 行） | `builtin="favorites"`，规则 = 收藏条件（按成员求值） |
| TMDB 系列（漫威宇宙…） | 只在详情页「系列作品」露一行 | `builtin="tmdb_series:{id}"`，F4 落地 |
| 「最近添加」 | 首页的一个分区 | 可登记，但**不登记**——它是排序不是筛选，登记了反而要解释"这个合集为什么没有规则" |

**分寸**：v1 只把「我的收藏」**登记**为内置合集，让它出现在合集列表与 Jellyfin
BoxSet 里；`/library/favorites` 这个页面**本期不动**。那 632 行里有位置记忆、
图廊形态、会话快照等一堆已经调稳的东西，和新功能同期重写是拿存量稳定性换整洁，
不划算。等合集详情页跑稳了再合并（F4），届时是**净删代码**。

### 1.2.1 「合集」这个词在本仓库已经有四个意思

做 F4 之前先认清楚，否则很容易把不相干的东西接到一起：

| 叫法 | 是什么 | 落库吗 | 在哪 |
|---|---|---|---|
| `Collection` / `CollectionItem` | **本次新增**：媒体库合集 = 存好的筛选 | 是 | `movieclaw_db/models/collection.py` |
| `MediaCollection` | TMDB 电影**系列**（`belongs_to_collection`，如指环王三部曲） | **否** | `movieclaw_media/models.py`，发现页详情实时取 |
| discover 片单 | TMDB / 豆瓣的**榜单**（Top250、热门…） | 只进缓存 | `discover.list-collections` |
| `CollectionFolder` / `BoxSet` | Jellyfin **协议**里的容器类型 | 不适用 | `movieclaw_jellyfin/catalog.py` |

**入库刮削链完全不碰系列**（2026-09 核实）：`MediaMetadata` 没有任何系列字段，
NFO 写出也没有 `<set>`——而 Emby/Jellyfin 正是靠这个标签认合集的；读第三方 NFO
时同样不解析它。系列信息只在发现页详情那一条实时链路上出现，用完即弃。

**这一条已经单独成篇**：完整改造方案见
[library-series-collections.md](library-series-collections.md)（刮削取值、写读
NFO 的 `<set>`、自动建合集、协议侧要核什么、以及"别把用户淹没"那一整节）。

所以 1.2 表里那行「TMDB 系列 → `builtin="tmdb_series:{id}"`」到了 F4 是**从零开始**：
没有既有数据可以吃掉，得先让刮削那一侧把 `belongs_to_collection` 存下来
（一个字段就够：系列 id + 名字），合集这一层才有东西可接。顺带值得考虑的是
NFO 补写 `<set>`——那是"movieclaw 作为上游生产者"（library.md 1.5 第 5 条）
在合集这一层的自然延伸，能让不走我们协议层的播放器也认得这些系列。

### 1.3 智能合集**不物化**（沿用 filtering 6.2 的同一条闸门）

规则求值有两条路：查询时实时算，或物化进 `collection_item`。**v1 选实时算。**

| | 实时求值 | 物化 |
|---|---|---|
| web / Jellyfin 一致性 | 天然一致 | 要维护同步，窗口期内会不一致 |
| 写入点 | 无 | 入库、刮削刷新、条目删除、重锚、规则变更——**五个**都要重算 |
| 读取成本 | 一次带索引的筛选查询 | 一次主键 IN |

单库量级 10³~10⁴ 条目，那次筛选查询正是海报墙每次翻页都在跑的同一条，成本已知。
物化是"为不确定的性能问题先付确定的复杂度"，而且它多出来的五个写入点每一个都是
未来的 bug 源。

**量化闸门**（与 library-filtering.md 6.2 共用同一条）：单库条目数 > 50,000
或合集成员解析 p95 > 300ms 时再引入物化，届时 `collection_item` 表已经在，
只需给 smart 模式也写行 + 一个重算任务。

**例外**：「固定当前命中」本来就是名单，直接写 `collection_item`，不涉及求值。

### 1.4 迁移（对照 CLAUDE.md 硬约束 3）

一次 alembic 迁移，**纯新增两张表**，不动任何既有表。旧版本读不到即忽略，
应用内更新回退安全。不落 `data/` 新目录，不触发 `storage/registry.py` 登记。
运行时依赖无变化，`docker/runtime-version` 不需要 bump。

## 2. 领域层：`services/library/collections.py`（新增）

### 2.1 规则求值复用筛选的值对象

```python
def rules_to_filter(rules: list[dict]) -> LibraryFilter:
    """把 [{field, op, values}] 翻成筛选层的值对象。

    合集规则与筛选条件是同一套结构（library-routing.md 1.1 定的那套），
    所以这里是一次纯粹的形状转换，没有第二套语义。
    """
```

`LibraryFilter` 是 library-filtering.md F1 引入的值对象。**F3 依赖 F1 先落地**
——这是分期上唯一的硬顺序。

### 2.2 成员解析不是一个"新查询"，是给筛选传参

```python
async def resolve_members(
    session, collection, *,
    member_id: int | None,                 # 观看者：决定「我收藏的」这类成员相关条件
    visible_library_ids: set[int] | None,  # 成员可见库；None = 不受限
    sort: WallSort | None = None,
    limit: int | None = None, offset: int = 0,
) -> list[int]:
    """合集成员的 media_item_id，按 sort 排好。

    规则驱动 → rules_to_filter() 后原样交给 _wall_page_ids()
    名单驱动 → collection_item 按 position，再过一遍可见性与在位文件
    """
```

第 0 节那条约束——"规则求值只能有一个实现"——**初稿是把它当纪律写的**
（"两边都要记得调同一个函数"）。纪律是会松的。真正让它成立的是这件事：

> **合集根本没有自己的查询。** `resolve_members()` 是 `_wall_page_ids()` 的一层
> 薄适配：把 `rules` 翻成 `LibraryFilter`，连同 `library_id` / `sort` / 分页一起传进去。
> 海报墙翻页跑的是它，合集列表跑的也是它，Jellyfin 要成员时跑的还是它。

于是"两端不一致"不再靠人记得，而是**没有第二条路可走**。协议层想自己写一条查询，
得先把 `_wall_page_ids` 抄一遍——那种代码在 review 里是藏不住的。

排序：`sort=None` 时，名单驱动用 `position`，规则驱动用 `collection.sort`。
分页参数留着，因为 Jellyfin 的 `/Items?ParentId=…&Limit=&StartIndex=` 会真的分页。

### 2.3 可见性收口（三层，缺一不可）

1. **合集本身可见吗**：`visibility=private` 且 `member_id` 不匹配 → 不下发；
   `library_id` 指向的库对该成员不可见 → 不下发。**这一层只读 `collection` 表，
   不解析成员**——是一条便宜的 SQL。
2. **成员条目可见吗**：解析结果必须过 `visible_library_ids`——规则驱动走
   `_wall_page_ids` 时带上，名单驱动走 `item_ids_with_files(visible_library_ids=…)` 过滤。
3. **过滤后空了**：**不下发这个合集**。点进去空无一物的合集在电视端是纯粹的死路。

### 2.4 成员数：不缓存，改把工作挪到该做它的那个请求

初稿在这里写了个进程内缓存，跟着 `library.stats_refreshed_at` 失效。**那是错的**：
「我的收藏」这类成员相关的合集，用户点一下心成员数就变，而点心**不会**碰
`stats_refreshed_at`——缓存会一直脏到下次扫描。为一个高频接口引入一条会脏的缓存，
比不缓存糟得多。

正确的做法是**别在 `/UserViews` 里算**：

| 请求 | 要不要成员数 | 做什么 |
|---|---|---|
| `GET /UserViews` | **不要** | 只判断"有没有至少一个元数据可见的合集"——2.3 第 1 层那条便宜 SQL，与合集数量无关 |
| `GET /Items?ParentId=<合集视图>` | 要 | 这本来就是"把合集列出来"的请求，N 次解析发生在用户真的打开合集列表时，天经地义 |

`ChildCount` 只在后者输出。协议允许缺省该字段，客户端不会因此出错。

**v1.1 修订**：上表第一行原本写的是"只判元数据存在"，理由是别让高频接口背
N 次解析。**F3.4 把「我的收藏」登记为内置合集之后，这个前提没了**——每个库都
常驻一行空合集，只判元数据存在等于**永远**下发这个视图，新用户点进去一片空白，
正是本节要避免的那件事。

改成逐个探"有没有第一个成员"（`has_any_member`，`LIMIT 1`，命中即停）：通常
第一个合集就命中，一次查询；全空时是 N 次 `LIMIT 1` 查询，与"解析 N 个合集的
全部成员"不是一个量级。

这是一条值得记下来的教训：**一个抽象吃掉既有特例时，会顺带改变别处的性能
前提**。「登记内置合集」看起来只是加一行数据，实际把"有没有合集"这个判断从
"多数时候为假"翻成了"恒为真"。

**v1.2 修订（2026-09-10，系列合集上线时）**：上面那句"N 次解析发生在用户真的
打开合集列表时，天经地义"，在合集是**自动生成**的之后不再天经地义——一个 300 部
的电影库有 40+ 个系列，"打开一次合集页"就是 40 次完整的墙查询，而电视端会反复
拉这个视图。做了两件事：

- **数数不再把成员取出来**：`count_members()` 走 `items._wall_count()`，一条
  `COUNT(DISTINCT)`。按标题排序的合集尤其受益——`_wall_page_ids` 在那一档要把
  全库标题取出来在 Python 里做一次拼音排序，几十个合集叠起来就是那道悬崖；
- **封面整页只取一次**：`items.poster_facts_many()`，一条查询覆盖本页全部合集的
  封面。此前是每个合集调一次完整的墙聚合（十条查询）。

两条都**没有**给合集增加自己的查询：`_wall_count` 与 `_wall_page_ids` 共用
`_narrow` + `_wall_scope`，换的只是投影；`poster_facts_many` 是海报墙自己也在用
的那一处实现（"合集卡片上的图与墙上的图是同一张"因此变成结构性保证）。
口径一致由回归用例压着：`_wall_count == len(_wall_page_ids)`。

**成员数仍然不缓存**，但 2.4 这条规则可以说得更准：不能缓存的是**看的人**决定
的合集（收藏、观看态——点一下心就变，且不碰任何库级时间戳）；**库里有什么**
决定的合集（系列、类型、年代、画质）只在已经调 `refresh_stats` 的写路径上变，
将来要缓存是可行的。这一期没做——`_wall_count` 已经把大头削掉了。

## 3. movieclaw 业务接口

### 3.1 端点

沿用业务接口规范（成功 `success/code/message/data`，错误 `success/code/message/details`）：

| 方法 | 路径 | operation_id |
|---|---|---|
| GET | `/collections?library_id=&scope=` | `collection.list` |
| POST | `/collections` | `collection.create` |
| GET | `/collections/{id}` | `collection.get` |
| PUT | `/collections/{id}` | `collection.update` |
| DELETE | `/collections/{id}` | `collection.delete` |
| GET | `/collections/{id}/items?sort=&limit=&offset=` | `collection.items.list` |
| POST | `/collections/{id}/items` | `collection.items.add`（仅手动合集） |
| DELETE | `/collections/{id}/items/{item_id}` | `collection.items.remove`（仅手动合集） |
| PUT | `/collections/{id}/order` | `collection.items.reorder`（仅手动合集） |
| GET | `/collections/{id}/series` | `collection.series.get`（系列合集的缺片补齐） |
| GET/POST/DELETE | `/collections/{id}/share` | `collection.share.get/create/revoke` |
| POST | `/collections/{id}/apply-to-library` | `collection.apply-to-library` |

「仅手动合集」由 `_guard_manual` 统一拦截：规则驱动的合集拒绝手工增删，
否则下一次规则求值就会把手工结果冲掉——那是一种用户改了、看着生效了、
过一会儿又变回去的失败，比直接报错难查得多。

新文件 `src/movieclaw_api/api/routes/collections.py`。

### 3.2 列表复用海报墙的聚合

`collection.items.list` 拿到 id 列表后走 `items.py` 的 `_aggregate_wall_views()`
——与单库墙**同一份聚合**（库存概况、缺集数、海报本地资产优先级）。
合集页的卡片因此和主墙长得一模一样，不会出现"同一部片两处显示不同"。

### 3.3 CLI

按仓库惯例，新端点进命令树（`cli/internal/spec` 的 operation_id 映射）。
`mclaw` 随 app-backend 产物发布，不要求重发镜像。

## 4. Jellyfin 兼容层

### 4.1 协议映射决策表

| movieclaw | Jellyfin | 说明 |
|---|---|---|
| 一个合集 | `BaseItemDto.Type = "BoxSet"` | 协议原生概念，客户端普遍支持 |
| 「合集」聚合视图 | `CollectionFolder`，`CollectionType = "boxsets"` | 与库视图同型，只是 CollectionType 不同 |
| 合集成员 | BoxSet 的子级（`ParentId = <boxset guid>`） | **只能是条目**（Movie / Series），不能是季/集 |
| 合集内排序 | `SortBy` / 默认序 | 无需特殊处理：没传 `SortBy` 时现有代码本就保持容器给的顺序（见 4.7） |

### 4.2 GUID 扩展（`movieclaw_jellyfin/ids.py`）

沿用现有的结构化编码，加一个类型字节和一个固定实体常量：

```python
class EntityKind(IntEnum):
    ...
    MEMBER_USER = 0x07
    COLLECTION  = 0x08     # 载荷 = collection.id (8B)

FIXED_USER = 1
FIXED_ROOT = 2
FIXED_COLLECTIONS = 3      # 「合集」聚合视图，走 FIXED 类型即可，不必新开一类

def collection_guid(collection_id: int) -> str:
    return _pack(EntityKind.COLLECTION, collection_id)

def collections_view_guid() -> str:
    return _pack(EntityKind.FIXED, FIXED_COLLECTIONS)
```

`collection.id` 是自增主键，删除不回收，GUID 天然稳定——与现有 library/item
的处理同源，不需要映射表。

### 4.3 视图层级：协议侧顶层，产品侧挂库下

产品设计（library-filtering.md 4.3）定的是"合集挂在库下面，不做侧栏一级入口"。
**协议侧不照搬这个结论**，而是走 Jellyfin 的惯例：**一个顶层「合集」视图**。

理由：Jellyfin 客户端对库视图的子级有固定预期（Movie / Series / Season / Episode），
把 BoxSet 塞进某个库的子级，各家客户端的表现不可预期——有的不渲染，有的渲染成
空文件夹。协议兼容的价值恰恰在于"表现可预期"，为了 IA 上的一致性去赌客户端行为
是本末倒置。

**这不是不一致，是两个受众的正确答案不同**：网页端的用户在管理自己的库，
心智是"我的电影库里有一批诺兰"；电视端的用户在找片看，心智是"有哪些片单"。
库归属信息并不丢失——它决定合集对谁可见（2.3 第 1 层），只是不体现为协议层级。

**空视图不下发**：一个可见合集都没有时，`/UserViews` 不返回这个视图。
配套把 `identity.py` 的 `user_configuration()` 里
`"DisplayCollectionsView": False` 改为 `True`。

> ⚠️ **已知代价**：不少客户端会缓存 `/UserViews`。用户建了第一个合集后，
> 电视端可能要手动刷新一次才看得到「合集」入口。这是"不给空视图"的代价，
> 记录在案，不做额外补偿（补偿手段只有常驻空视图，那更糟）。

### 4.4 改动点（文件级，全部是已有的分发点上加分支）

| 文件 | 位置 | 改动 |
|---|---|---|
| `ids.py` | `EntityKind` | 加 `COLLECTION = 0x08`、`FIXED_COLLECTIONS`、两个构造函数 |
| `catalog.py` | `library_view_dto` 近旁 | 新增 `collections_view_dto()`（CollectionFolder + `CollectionType="boxsets"`）与 `boxset_dto()` |
| `routes/library.py` | `_entries_for_parent`（约 786） | **先抽 `_container_entries()`**：LIBRARY 分支现有的"取 id → `load_bundles` → `_build_entries`"就是容器语义，抽出来后 COLLECTION 分支只剩"换一个 id 来源"，约五行 |
| 同上 | `user_views`（约 201） | 有元数据可见的合集时追加「合集」视图（不解析成员，见 2.4） |
| 同上 | 同上 | 两个新分支：parent = 合集视图 → 列 BoxSet（带 `ChildCount`）；parent = BoxSet → `resolve_members()` 走容器分支 |
| 同上 | `get_item`（约 1285） | 加 `EntityKind.COLLECTION` 分支，返回 `boxset_dto` |
| 同上 | `_query_items` 的 `includeItemTypes`（约 506） | 认识 `"BoxSet"`；根级 `IncludeItemTypes=BoxSet` 返回全部可见合集 |
| 同上 | `items_counts`（约 1108） | 补 `BoxSetCount` |
| 同上 | `library_virtual_folders`（约 261） | 一并列出合集视图（部分客户端据此建库列表） |
| `routes/images.py` | EntityKind 分发（约 103） | 加 COLLECTION 分支（见 4.6） |
| `identity.py` | `user_configuration()` | `DisplayCollectionsView` → `True` |

**协议层不新增任何查询**：上面每一处拿成员都调 `resolve_members()`，
渲染都走既有的 `load_bundles` / `_build_entries` / `_entry_dto`。

### 4.5 BoxSet DTO 关键字段

```
Id            = collection_guid(id)
Name          = collection.name
Type          = "BoxSet"
ParentId      = collections_view_guid()
IsFolder      = true
ChildCount    = 成员数（2.4 的缓存）
CollectionType 不设            —— 那是 CollectionFolder 的字段，BoxSet 不该有
ImageTags     = {"Primary": <tag>}   见 4.6
UserData      见 4.8
```

### 4.6 图片：不做第二套资产

BoxSet 的 Primary 图**直接复用首个成员条目的海报**（`cover_item_id` 指定时用它）：
`routes/images.py` 的 COLLECTION 分支解析出成员条目后，转交现有的 ITEM 图片路径。

理由：合集封面在网页端本来就是"首个成员海报 + 背后露两片边"，那两片边是 CSS，
不是图片资产。为协议侧单独生成拼贴图（像 `services/library/cover.py` 给库做的那样）
要多一套资产、多一个失效通道，换来的只是电视端封面好看一点点。
`ImageTags.Primary` 取成员条目的既有 tag 派生，内容变则 tag 变，缓存自然失效。

### 4.7 排序：不需要任何特殊处理

初稿在这里记了一条风险："手动合集的 `position` 序会被 `_sort_entries()` 洗掉，
这是唯一要小心的既有代码交互"。**核对源码后确认这条风险不存在**——
`_sort_entries()` 开头就是 `if not sort_by: return entries`，客户端没传 `SortBy`
时它原样返回。

所以正确的描述是：现有代码已经遵循**"没给排序就保持容器给的顺序"**，
而这恰好就是手动合集需要的语义。合集分支什么都不用做：

- 客户端传了 `SortBy` → `_sort_entries()` 照常生效，与库浏览完全一致；
- 没传 → `resolve_members()` 给的顺序原样保留（名单序或 `collection.sort`）。

**一处编造出来的"要小心"，换来的是一段本不必写的特殊处理。** 记在这里，
是因为"技术方案里凭印象写风险"本身就是要防的事——每条风险都该有源码或实测支撑。

**v1.2 更正（2026-09-10）**：上面这段只对**一半**——它说的是"打开一个 BoxSet
之后，里面那些片怎么排"，那条路径确实什么都不用做。但**把合集本身列出来**的
那条路径（`?ParentId=<合集视图>`）压根没经过 `_sort_entries`：它直接
`query_result(page)`，客户端给的 `SortBy` 对合集列表**完全无效**，恒按 `position`。
一两个合集时没人察觉，几十个系列合集时用户会觉得"排序坏了"。

现在有 `_sort_boxsets()`（合集版的 `_sort_entries`，认 SortName/Name/DateCreated
与 Random，未知键同样静默忽略、回落 position 序）。**教训与上一段其实是同一条的
反面**：核对源码时只核对了自己想到的那条路径，另一条同名的事就漏掉了。

**v1.3 更正（F4 端到端跑出来的）**：连"打开一个 BoxSet 之后里面那些片怎么排"
也不是什么都不用做。`_sort_entries` 确实原样返回，但它拿到的 entries 已经**不是**
`resolve_members()` 给的顺序了——中间隔着 `load_bundles()`，它按查询顺序回一个
dict，人手拖出来的 `position` 序在那里就没了。表现是：网页与分享页顺序对，
电视端仍是入库序。合集分支现在按 `resolve_members()` 的返回重排 bundles 再建
entries。

这一处**只有 F4 那条验收标准抓得到**：三处各自的用例都是绿的——网页对、分享页对、
电视端"有这几部片"也对，只有把三处摆在一起比顺序时才露馅
（`tests/e2e/test_library_filtering_browser.py` 第 24 段）。这也是把"三处一致"
写成验收标准、而不是写成三条各自的断言的原因。

### 4.8 UserData：不做已看聚合

库视图现在是 `SupportsPlayedStatus = false`（`catalog.py` 里有注释说明）。
**合集保持一致**：BoxSet 不聚合"看了几部"。理由与库视图相同，且省掉一次
对全部成员的播放状态聚合——那正是 `/UserViews` 这种高频接口最不该背的成本。

### 4.8.1 库范围：`IncludeItemTypes=BoxSet` 也要看 `ParentId`（v1.2 修正）

`_wants_boxsets()` 初版对 `IncludeItemTypes=BoxSet` 直接短路返回真，**完全不看
`ParentId`**。于是 `?ParentId=<电影库>&IncludeItemTypes=BoxSet` 会把剧集库的合集
一起返回——合集都在同一个库时看不出来，自动生成系列合集之后就是明显的串库。

现在是 `_boxset_query_scope()`，回答两件事："要的是合集吗"以及"限定在哪个库"：

| 问法 | 给什么 |
|---|---|
| `ParentId=<合集视图>` | 全部可见合集 |
| `IncludeItemTypes=BoxSet`（无 ParentId） | 全部可见合集（根级问法） |
| `ParentId=<某个库>&IncludeItemTypes=BoxSet` | **只有这个库的** |
| `ParentId=<别的什么>&IncludeItemTypes=BoxSet` | 不是合集请求 |

### 4.9 私有合集与成员

`ViewerScope` 已经带了 `member_id` 与 `visible`（见 `routes/library.py` 的
`viewer_scope`），2.3 的三层过滤全部落在它上面，**不新增可见性通道**。
`private` 合集对非归属成员**返回 404 而不是空列表**——与现有条目可见性的处理一致
（GUID 可枚举，空列表等于确认存在）。

### 4.10 客户端核对清单（验收时逐条走）

| 请求 | 期望 |
|---|---|
| `GET /UserViews` | 有可见合集时多出一个「合集」视图，`CollectionType="boxsets"` |
| `GET /Items?ParentId=<合集视图>` | 返回全部可见 BoxSet，带 `ChildCount` |
| `GET /Items?ParentId=<boxset>` | 返回成员条目，分页与排序生效 |
| `GET /Items/{boxset}` | 单条 BoxSet 全字段 |
| `GET /Items/{boxset}/Images/Primary` | 首个成员的海报，ETag 生效 |
| `GET /Items?IncludeItemTypes=BoxSet&Recursive=true` | 根级返回全部可见 BoxSet |
| `GET /Items/Counts` | 含 `BoxSetCount` |
| 私有合集 + 非归属成员 | 上述全部 404 / 不出现 |
| 成员不可见库中的合集 | 不出现 |

## 5. 性能

| 路径 | 成本 | 说明 |
|---|---|---|
| `GET /UserViews` | 1 次 EXISTS | 与合集数量无关（2.4） |
| `GET /Items?ParentId=<合集视图>` | N 次成员解析 | N = 可见合集数，量级个位数到几十；发生在用户打开合集列表时 |
| `GET /Items?ParentId=<boxset>` | 1 次筛选查询 + 1 次 `load_bundles` | 与海报墙翻页同量级，成本已知 |
| 名单驱动的合集 | 1 次主键查 + 可见性过滤 | 忽略不计 |

**没有缓存层**。初稿有一个，因为失效逻辑是错的（2.4）而删掉——
删掉之后这张表反而更简单，这通常是个好兆头。

超过 1.3 节那条闸门时再上物化，届时前两行都退化成主键 IN。

## 6. 测试

`tests/jellyfin/` 已有的组织方式直接套用：

- `test_protocol_units.py` → GUID 编解码往返（含 COLLECTION 与 FIXED_COLLECTIONS）、
  BoxSet DTO 字段形状；
- `test_library_access.py` → 私有合集、不可见库中的合集、过滤后为空的合集
  三种情况都不下发；
- `test_http_flow.py` → 4.10 那张表逐行走一遍；
- `tests/api/` → 合集增删改查、规则求值与筛选结果一致（**同一组条件，
  `/libraries/{id}/items` 与 `/collections/{id}/items` 必须返回同一批 id**
  ——这是第 0 节那条约束的回归测试）。

F4 落地后补的两处：

- `tests/api/test_share.py` → 合集分享的范围就是它此刻的成员：名单外的条目
  猜到 id 也打不开，被移出去的立刻打不开（不需要再来撤销一次）。
- `tests/e2e/test_library_filtering_browser.py` 23–25 段 → **F4 的验收只能在
  这里证**：把一部片顶到最前、移出另一部，然后在站内合集详情、Jellyfin 的
  BoxSet 孩子、公开分享页三处核同一份顺序。三处走的是不是同一份名单，
  单元测试各自绿着也说明不了。

## 7. 分期与验收

对应 library-filtering.md 第 7 节的 F3 / F4：

| 步 | 内容 | 验收 |
|---|---|---|
| **F3.1** ✅ | 两张表 + 迁移 + `collections.py` 领域层 + 业务接口 | 同一组条件下 `/libraries/{id}/items` 与 `/collections/{id}/items` 返回同一批 id |
| **F3.2** ✅ | web：合集 chip 行、2:3 网格视图、详情页、存为合集 | 筛选 → 存 → 新入库一部命中的片 → 数量自动 +1 |
| **F3.3** ✅ | **Jellyfin BoxSet**（第 4 节全部，含 `_container_entries` 抽取） | 4.10 清单逐条通过；Infuse 与 Jellyfin 官方客户端各验一遍 |
| **F3.4** ✅ | 「我的收藏」登记为 `builtin="favorites"` | 它出现在合集列表与 Jellyfin BoxSet 里；`/library/favorites` 页面**行为零变化** |
| **F3.5** ✅ | **系列合集**（另见 [library-series-collections.md](library-series-collections.md)）+ 合集隐藏机制 + 列表/BoxSet 的代价整治 | 40 个合集时列表接口的查询数不随合集数线性涨；自动合集点「删除」后不再长回来，且找得回来 |
| **F4.1–F4.4** ✅ | 手动合集成员增删与拖拽、「加入合集」入口、跨库合集与 `/library/collections` 总览页、合集分享 | 拖拽顺序在海报墙、Jellyfin、分享页三处一致 |
| **F4.6** ✅ | 合集详情页的规则条从只读升级为可编辑 | 改完规则立即重算成员数 |
| **F4.5** ⛔ | `/library/favorites` 并入合集详情页 | **未做**，两处前置未建，见 8.10 |

**F3.3 可以与 F3.2 并行**——两者都只依赖 F3.1 的领域层。
**F3.4 必须在 F3.3 之后**：它的验收要看 Jellyfin 侧，前面没做完验不了。

F3 四期已实施（✅）。F3.4 落地时发现它会改变 `/UserViews` 的性能前提，
修订见 2.4；`/library/favorites` 页面按计划未动，F4 并页。

F3.5（系列合集）把"合集会自动生成"这件事变成现实，随之暴露了三处只在
合集数量少时藏得住的问题：列表接口的 `11 × N`、`IncludeItemTypes=BoxSet`
不看 `ParentId`、BoxSet 列表绕过排序。修订分别见 2.4（v1.2）、4.8.1、4.7（v1.2）。
「TMDB 系列」原本挂在 F4，现在提前到 F3.5 单独成篇。

## 8. 决策记录

1. **合集没有自己的查询**：`resolve_members()` 是 `_wall_page_ids()` 的薄适配，
   两端一致是结构性的而不是纪律性的（2.2）。
2. **不存 `mode` 列**：形态由 `rules` / `collection_item` / `builtin` 推导；
   `smart|manual|system` 是把"成员怎么来"和"能不能改"两件正交的事挤进一列（1.1）。
3. **内置合集要吃掉既有特例**：先登记「我的收藏」，`/library/favorites` 页面
   等合集详情页跑稳后再并（1.2）——分两步，是因为那 632 行里有已经调稳的东西。
4. **智能合集不物化**，沿用 filtering 6.2 的量化闸门（1.3）。
5. **协议侧顶层「合集」视图，产品侧仍挂库下**——两个受众的正确答案不同（4.3）。
6. **成员数不缓存**，把 N 次解析挪到"列出合集"那个请求（2.4）。
7. **BoxSet 封面复用成员海报**，不做第二套资产（4.6）。
8. **BoxSet 不做已看聚合**，与库视图保持一致（4.8）。
9. **库删除时合集级联删除**，不 SET NULL（1.1）。
10. **合集分享的成员每次访问重算**，不在创建分享时固化名单——规则驱动的合集
   本来就随入库变化，固化等于分享出去一张会过期的快照（F4.4）。
11. **跨库合集不进单库页的 chip 行**，只在 `/library/collections` 露出：
   跨库合集出现在单库筛选条上，点进去会看到本库没有的片，比"找不到入口"更难解释（F4.3）。

### 8.10 F4.5 为什么没做

原计划「`/library/favorites` 下线，跳内置「我的收藏」合集」，验收是
**净删代码** 且 **行为零变化**。实现时发现这两条当前无法同时成立，两处前置未建：

1. **口径对不上**：`/library/favorites` 是**跨库**的（当前账号收藏的全部作品），
   而内置收藏合集是**按库**的（`favorites:{library_id}`）。规则驱动的合集目前
   只在单库内求值——`resolve_members()` 需要一个 `library_id`。要并页，得先让
   规则求值支持跨库，那不是并页的一部分，是 F4.3 之上的又一层。
2. **能力对不上**：`components/favorites-view.tsx` 有 632 行，带着墙位置召回、
   滚动恢复、画廊模式与密度偏好；合集详情页没有这些。照当前的合集详情页并过去，
   删掉的是代码，同时删掉的还有行为——"净删代码"成立了，"行为零变化"不成立。

结论：**不半做**。半做的代价落在一个每天都在用的页面上。要做，前置是
（a）跨库规则求值 +（b）合集的画廊模式接口；两项都不在 F4 的范围内，
留待后续单独立项。在那之前 `/library/favorites` 原样保留，
它已在 F3.4 登记为 `builtin="favorites"`，合集列表与 Jellyfin BoxSet 两处都能看到它。

### 8.1 初稿改了什么（留痕）

| 初稿 | 现在 | 为什么 |
|---|---|---|
| `mode: smart\|manual\|system` | 无此列，推导 | 三分法把正交的两件事挤进一列 |
| `kind` / `pinned` 列 | 砍掉 | 可推导 / 与 `position` 重复 |
| 成员数进程内缓存，跟 `stats_refreshed_at` 失效 | 不缓存 | **失效逻辑是错的**：点心不碰 `stats_refreshed_at` |
| "手动序会被 `_sort_entries` 洗掉"（风险） | 删除 | 核对源码：`if not sort_by: return entries`，风险不存在 |
| "两边都要记得调同一个函数" | "合集没有自己的查询" | 纪律会松，结构不会 |
| `/UserViews` 只判元数据存在 | 探到第一个有成员的合集为止 | 登记内置合集后"有没有合集"恒为真，只判存在等于永远下发一个空视图（2.4） |
| 内置合集靠一次性迁移补齐存量库 | 启动时逐库核一遍（幂等） | 迁移只补得到"迁移那一刻已有"的库；此后绕过建库接口写进来的库仍会缺行 |

## 9. 风险

| 风险 | 应对 |
|---|---|
| 客户端缓存 `/UserViews`，新建第一个合集后电视端看不到入口 | 已知代价（4.3），不常驻空视图 |
| 视图出现但点进去是空列表 | 已堵（2.4 修订）：`/UserViews` 探到第一个有成员的合集才下发 |
| 智能合集在两端结果不一致 | 结构上堵死（2.2）+ 一条回归测试（第 6 节） |
| 各家客户端对 BoxSet 的支持深浅不一 | 4.10 清单在 Infuse 与官方客户端各验一遍；只用协议原生字段，不发明扩展 |
| F4 合并 `/library/favorites` 时打破已调稳的行为 | **已按此风险停手**：F4.5 未做，理由与前置见 8.10 |
| 手工排好的顺序被规则求值冲掉 | `_guard_manual` 从接口层堵死：规则驱动的合集不接受手工增删（3.1） |
