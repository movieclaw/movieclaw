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
  name          str                     -- 展示名
  library_id    int FK → library.id nullable   -- NULL = 跨库合集（F4 才开入口）
  kind          str                     -- movie / tv / video，跟随 library.kind
  mode          str                     -- smart | manual | system
  rules         JSON                    -- 与 library.match_rules 同构；manual 恒为 []
  sort          str                     -- 合集内默认排序（沿用 WallSort 取值）
  visibility    str                     -- household | private
  member_id     int FK → member.id nullable    -- private 时的归属成员
  pinned        bool                    -- 顶栏 chip 行是否优先露出
  position      int                     -- 货架/网格顺序，与 library 排序同款
  cover_item_id int FK → media_item.id nullable  -- 不指定则取首个成员的海报
  created_at / updated_at

collection_item                          -- 仅 manual（含「固定当前命中」快照）
  collection_id int FK ON DELETE CASCADE
  media_item_id int FK ON DELETE CASCADE
  position      int
  UNIQUE(collection_id, media_item_id)
```

**索引**：`collection(library_id)`、`collection(member_id)`、
`collection_item(collection_id, position)`。

**外键与级联**：`library` 删除时 `collection.library_id` 置 NULL 还是级联删？
**级联删除**——库没了，"这个库里的一批片"这个定义也就没了意义，留一个指向空气的
合集只会变成幽灵数据。跨库合集（`library_id IS NULL`）本来就不受影响。
（与 `media_item.scrape_library_id` 的 `SET NULL` 处理不同：那一列是"归属推断"，
可以重新推断；合集是"用户定义的东西"，宿主没了就该没了。）

### 1.2 智能合集**不物化**（沿用 6.2 的同一条闸门）

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

**例外**：`mode=manual` 的「固定当前命中」本来就是名单，直接写 `collection_item`，
不涉及求值。

### 1.3 迁移（对照 CLAUDE.md 硬约束 3）

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

### 2.2 唯一的成员解析入口

```python
async def resolve_members(
    session, collection, *,
    member_id: int | None,                 # 观看者，决定「我收藏的」这类成员相关条件
    visible_library_ids: set[int] | None,  # 成员可见库；None = 不受限
    sort: WallSort | None = None,
    limit: int | None = None, offset: int = 0,
) -> list[int]:
    """合集成员的 media_item_id，按 sort 排好。

    smart  → rules_to_filter() → 复用 _wall_page_ids() 的候选集收窄
    manual → collection_item 按 position；再过一遍可见性与在位文件
    """
```

- **web 与 Jellyfin 都只调它**，这是第 0 节那条约束的落点；
- 排序复用 `WallSort`：`sort=None` 时 manual 用 `position`、smart 用
  `collection.sort`；
- 分页参数留着，是因为 Jellyfin 的 `/Items?ParentId=…&Limit=&StartIndex=`
  会真的分页。

### 2.3 可见性收口（三层，缺一不可）

1. **合集本身可见吗**：`visibility=private` 且 `member_id` 不匹配 → 整个合集不下发；
   `library_id` 指向的库对该成员不可见 → 同样不下发。
2. **成员条目可见吗**：解析结果必须过 `visible_library_ids`——smart 走
   `_wall_page_ids` 时带上，manual 走 `item_ids_with_files(visible_library_ids=…)` 过滤。
3. **过滤后空了怎么办**：**不下发这个合集**。一个点进去空无一物的合集，
   在电视端是纯粹的死路。代价是 `/UserViews` 要为每个合集算一次成员数（见 2.4）。

### 2.4 计数与缓存

`ChildCount` 是 Jellyfin 客户端在视图列表里就要的字段，意味着**每次 `/UserViews`
都要为每个合集算一次数**。N 个合集 = N 次查询。

对策（够用即可，不上缓存中间件）：进程内缓存 `(collection_id, member_id) → count`，
失效条件跟着 `library.stats_refreshed_at` 走——库内容没变，成员数就不会变。
与库统计快照同一个 tick，不新增失效通道。

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
| POST | `/collections/{id}/items` | `collection.items.add`（manual，F4） |
| PUT | `/collections/{id}/order` | `collection.items.reorder`（manual，F4） |
| POST | `/collections/{id}/apply-to-library` | `collection.apply-to-library` |

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
| 合集内排序 | `SortBy` / 默认序 | manual 的默认序 = `position`（见 4.7） |

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
| `routes/library.py` | `user_views`（约 201） | 有可见合集时追加「合集」视图 |
| 同上 | `_entries_for_parent`（约 786） | 加两个分支：parent = 合集视图 → 列 BoxSet；parent = BoxSet → `resolve_members()` 后 `load_bundles` + `_build_entries` |
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

### 4.7 排序与手动序

- 客户端传了 `SortBy` → 交给现有的 `_sort_entries()`（约 457），与库浏览同一套；
- **没传 SortBy** → smart 用 `collection.sort`，manual 用 `position`。
  这里要注意：`resolve_members()` 返回的已经是有序 id 列表，
  `_build_entries` 之后**不能再无条件过一遍 `_sort_entries`**，否则手动拖出来的
  顺序会被 SortName 洗掉。这是本次唯一一处要小心的既有代码交互。

### 4.8 UserData：不做已看聚合

库视图现在是 `SupportsPlayedStatus = false`（`catalog.py` 里有注释说明）。
**合集保持一致**：BoxSet 不聚合"看了几部"。理由与库视图相同，且省掉一次
对全部成员的播放状态聚合——那正是 `/UserViews` 这种高频接口最不该背的成本。

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

| 路径 | 成本 | 对策 |
|---|---|---|
| `/UserViews` | N 个合集 × 1 次计数 | 2.4 的进程内缓存，跟 `stats_refreshed_at` 失效 |
| `/Items?ParentId=<boxset>` | 1 次筛选查询 + 1 次 `load_bundles` | 与海报墙翻页同量级，已知成本 |
| 手动合集 | 1 次 `collection_item` 主键查 + 可见性过滤 | 忽略不计 |

超过 1.2 节那条闸门时再上物化，届时本节的对策全部作废、换成一次主键 IN。

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

## 7. 分期与验收

对应 library-filtering.md 第 7 节的 F3 / F4：

| 步 | 内容 | 验收 |
|---|---|---|
| **F3.1** | 两张表 + 迁移 + `collections.py` 领域层 + 业务接口 | `/libraries/{id}/items` 与 `/collections/{id}/items` 在同一组条件下返回同一批 id |
| **F3.2** | web：合集 chip 行、2:3 网格视图、详情页、存为合集 | 筛选 → 存 → 新入库一部命中的片 → 数量自动 +1 |
| **F3.3** | **Jellyfin BoxSet**（本文第 4 节全部） | 4.10 清单逐条通过；Infuse / Jellyfin 官方客户端各验一遍 |
| **F4** | 手动合集拖拽、加入合集入口、跨库合集、TMDB 系列、合集分享 | 拖拽顺序在海报墙、Jellyfin、分享页三处一致 |

**F3.3 可以与 F3.2 并行**：两者都只依赖 F3.1 的领域层。

## 8. 决策记录

1. **规则求值只有一个实现**，协议层不写查询（第 0 节）——本方案的地基。
2. **智能合集不物化**，沿用 filtering 6.2 的同一条量化闸门（1.2）。
3. **协议侧顶层「合集」视图，产品侧仍挂库下**——两个受众的正确答案不同（4.3）。
4. **空合集不下发**，代价是客户端可能要刷新一次才看到新视图（4.3）。
5. **BoxSet 封面复用成员海报**，不做第二套资产（4.6）。
6. **BoxSet 不做已看聚合**，与库视图保持一致（4.8）。
7. **库删除时合集级联删除**，不 SET NULL（1.1）。

## 9. 风险

| 风险 | 应对 |
|---|---|
| 客户端缓存 `/UserViews`，新建第一个合集后电视端看不到入口 | 已知代价（4.3），文档写明；不常驻空视图 |
| 手动合集顺序被 `_sort_entries` 洗掉 | 4.7 点名了这处交互，测试里单独一条 |
| 智能合集在两端结果不一致 | 结构上堵死（唯一 `resolve_members`）+ 一条回归测试（第 6 节） |
| 成员数计算拖慢 `/UserViews` | 2.4 缓存；真扛不住就退成不下发 `ChildCount`（协议允许缺省） |
| 各家客户端对 BoxSet 的支持深浅不一 | 4.10 清单在 Infuse 与官方客户端各验一遍；只用协议原生字段，不发明扩展 |
