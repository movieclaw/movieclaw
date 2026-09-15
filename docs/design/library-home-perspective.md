# 媒体库首页透视：让首页由用户定义

> 状态：已实现（2026-09-13）。实现与本文的差异见第 7 节。v2 相对初稿的三处改动：首页不露任何排序细节、
> 自定义改成独立页面（桌面与移动端同一个，纯列表不放海报）、排序改成一列自带推荐名的预设。
> 样稿：[mockups/library-home-perspective-demo.html](mockups/library-home-perspective-demo.html)
> （排序与命名推荐是真跑的）。
> 上游：[library-home-up-next.md](library-home-up-next.md)（首页现有三行的口径）、
> [library-filtering.md](library-filtering.md)（合集是「存好的筛选」，4.3 已定「由用户选哪几个合集上首页」）。
> 先例：侧栏导航排序 `NavUiPrefs`（`settings/schemas.py`），本文的数据形状照抄它。

## 0. 一句话

首页 = 每个成员一份**有序的行清单**；每一行 = **来源 × 排序 × 名字**。

OmniFocus 的透视由三部分组成：规则（筛哪些任务）、呈现（怎么排、叫什么）、
位置（在侧栏第几个、要不要显示）。搬到首页上：

| 透视的部分 | 首页对应 | 现状 |
|---|---|---|
| 规则 | 一行滚的是哪一批作品：**一个库**或**一个合集** | 已有。想要「8 分以上的日漫」，先筛、存成合集、再上首页，**不新造第二套条件语言**（铁律 3） |
| 呈现 | 排序 + 名字 | 排序是现有 `WallSort` 加一档随机；名字由用户定，系统按排序给推荐 |
| 位置 | 顺序、显隐 | **本期要做的**。每个成员一份，能藏、能挪、能加、能恢复默认 |

## 1. 行的类型与能力

| 行 | 来源 | 排序 | 名字 | 隐藏 | 删除 |
|---|---|---|---|---|---|
| 接下来继续 | 内置 | 固定：最近播放（up-next 文档已论证不能引入入库时间） | 固定 | ✓ | — |
| 我的媒体库 | 内置 | 固定：管理页的库顺序 | 固定 | ✓ | — |
| 我的收藏 | 内置 | 未看优先（默认）/ 收藏时间 / 片名 / 评分 / 上映 | **固定**，它就是收藏，改名反而找不着 | ✓ | — |
| 库行（默认） | 一个库，每库自动一条，id `lib:<library_id>` | 入库 / 上映 / 最近观看 / 评分 / 片名 / 随机 | 可改，留空跟随推荐 | ✓ | — |
| 库行（自加） | 一个库，同库可多条，id `row:<uuid>` | 同上 | 同上 | ✓ | ✓ |
| 合集行 | 一个合集，id `row:<uuid>` | 合集自身 / 入库 / 上映 / 评分 / 随机 | 可改，留空跟合集名走 | ✓ | ✓ |

合集行的名字初稿定的是「硬跟合集名走，一个东西一个名字，改名去合集页」，后来推翻了：
首页上这一行**叫什么**是呈现的事，与合集自身的身份是两回事。同一个合集摆在首页可以
叫「今晚看点轻松的」，合集页里它仍叫原名，两边互不影响；真想连合集一起改名，去合集页
改，这一行名字留空就会跟着变。库行早就是这个规矩，合集行没有理由例外。

「最近添加的 X 库」不再是一种特殊的行，只是库行的**默认排序**。用户把它改成
「上映时间」，它就变成「最近上映的电影」；再加一条同库、按评分的行，两行并存。

### 1.1 排序 = 一列预设，每个预设自带推荐名

初稿：用户选的不是「上映时间 + 倒序」，而是「最近上映的动漫」，方向合并进取值里，
没有单独的方向开关，`release_date_asc` 与系列合集用的同一档。

**2026-09-13 推翻**：方向拆成独立的维度。用户明确要每个指标都能自己定正倒序，而后端
三个取数接口（库条目 / 合集条目 / 收藏）本来就收 `order`，前端没理由替他把这个选项
藏起来。现在一行的排序 = **指标 × 是否反转自然方向**，与海报墙的 `WallSortState`
同一个模型、同一套方向文案（`lib/wall-sort.ts` 的 `SORT_DIRECTIONS`，两处逐字相同）。
推荐名按（指标, 方向）算，所以「添加时间 + 反转」推荐的是「最早添加的电影」。
`release_date_asc` 不再是一档：老偏好与合集表里存的这个值在读取时归一成
`release_date` + 反转，行为逐字不变，不需要迁移。

| `sort`（指标） | 自然方向 | 推荐名（自然 / 反转） | 方向文案 |
|---|---|---|---|
| `added_at` | 新→旧 | 最近添加的{库} / 最早添加的{库}（每库默认行） | 新→旧 / 旧→新 |
| `release_date` | 新→旧 | 最近上映的{库} / 最早上映的{库} | 新→旧 / 旧→新 |
| `last_played` | 近→远 | 最近观看的{库} / 很久没看的{库} | 近→远 / 远→近 |
| `rating` | 高→低 | 评分最高的{库} / 评分最低的{库} | 高→低 / 低→高 |
| `random` | — | 随便看看 · {库}（没有方向，不画开关） | — |
| `title` | A→Z | {库} A–Z / {库} Z–A | A→Z / Z→A |

收藏行同理：收藏时间 / 评分 / 片名 三档可反转，「未看优先」没有方向。
换档时方向回到新档的自然方向（与海报墙一致：从「片长 长→短」换到「评分」，用户要的
是"高分在前"，不是继承一个反向）。

名字留空即跟随默认，用户一旦手输就不再跟着变，清空即回到默认。库行的默认是选中预设
算出的推荐名，合集行的默认是合集自己的名字。
收藏行：未看优先（默认）/ 最近收藏 / 评分最高 / 片名 A–Z。
合集行：与库行同一组预设，没有「合集自身」这个特例；新加的合集行默认取合集表里
已有的 `sort` 列（它本来就是 `WallSort` 取值）。手动合集的拖拽顺序只在合集页生效，
首页不理它。预设按库的 kind 裁剪，见 4.2 第 4 条。

### 1.2 「只显示我没看过的」

库行多一个开关。按评分排的行没有它，永远是同一批 20 部，那是一张海报不是一行内容。
后端已有 `w=unwatched`，只是一条参数。它与「最近观看」互斥：那一行只要播过的（`w=seen`），
排序切到最近观看时开关作废并隐藏。

### 1.3 随机

随机种子**按天固定**：页面轮询刷新不换一批，明天再换。否则用户会以为坏了。

## 2. 交互

### 2.1 首页：只有一件事，看

行的骨架不变（横滚、海报、「查看全部」）。首页上**没有任何排序细节**：没有
「按评分」的标签，没有行菜单，没有悬停才出现的控件。名字就是这一行的全部说明，
所以名字才值得让用户自己起。

- 唯一的配置入口在页头：「自定义首页」（「管理媒体库」左侧；移动端为图标按钮），
  跳到独立页面。
- 空行照旧整段隐藏：偏好决定「想不想看」，数据决定「有没有」。
- 全部藏光时不出白页，给一个指回「自定义首页」的空态。

### 2.2 自定义首页：独立页面 `/library/customize`

不是抽屉、不是弹层，桌面与移动端**同一个页面、同一份实现**。纯列表，**不放海报**：
这里是配置页，效果回首页看，「‹ 媒体库」一步就回去。

- **收起时只有把手 · 名字 · 眼睛**，排序这类信息不露，也没有类型图标。整份清单是
  一块面板，行之间用分隔线；隐藏的行压暗并标「已隐藏」，留在原位。
- **点名字展开一条紧凑的设置区**（两列网格，「排序」「名字」标签对齐）：排序菜单 · 名字输入框
  （库行与合集行，占位文字就是各自的默认名，留空即跟随）· 只显示我没看过的（库行）·
  删除这一行（自加行）。排序菜单复用海报墙的 `WallSortControl`，菜单里每个有方向的指标
  列两条（「最近添加」「最早添加」），点一条同时定档位和方向——不用「选指标 + 方向开关」
  两个控件：iOS 上原生 `<select>` 收起的那一下会吞掉紧接着的点击，方向开关总要点两次。内置的
  「接下来继续」「我的媒体库」没有可设的，点了不展开。新加的行自动展开。
- **拖拽是唯一的换位方式**：左侧把手是标准的可拖提示，没有上下箭头。用指针事件实现
  而不是 HTML5 DnD，触屏也能拖；键盘 Alt+↑/↓ 换位。落点跟手实时重排。
- **隐藏的行留在原位、压暗**，不挪到底部，再打开时回到原来的位置。
- 「＋ 添加一行」只问一个问题：**从哪来**。选一个库得到「最近添加的 X」，选一个
  合集得到它本身；已在首页的合集置灰标「已在首页」。候选**只收用户自建的合集**
  （`kind=user`）：`builtin` 是「我的收藏」，首页已经有那一行；`series` 是刮削器按
  作品系列自动生成的，往往只有一两部、钉上首页滚不满一行，数量上却能把候选区淹掉。
  这只拦「添加」这一步——存过的 series 合集照常渲染，合集页 ⋯ 菜单的「显示在首页」
  也仍然管用。
- 没有保存键，改动即时生效（400ms 去抖后整体 PUT）。「恢复默认」是页面里唯一带确认
  的动作：回到出厂布局，自加行被移除。
- 合集页 `⋯` 菜单另有「显示在首页 / 从首页移除」，写同一份偏好。
- **这颗开关还有第二个作用**（2026-09-15）：钉了首页的合集会在 Jellyfin 兼容层
  额外伪装成一个顶层媒体库，电视端客户端当它是库（
  [library-collections.md](library-collections.md) 4.11）。改这份偏好会震动
  电视端的 `/UserViews` 缓存，客户端可能要手动刷新一次。
- 首页页头的「自定义首页」「管理媒体库」都是图标钮。

### 2.3 移动端布局与密度

同一个页面，窄一点：收起的行就是一行高；展开的设置行自动换行；把手常驻在左；
排序下拉是原生 `<select>`，手机上弹系统选单。

## 3. 数据

不新增表，不迁移。存在成员自己的 `member.ui_prefs`（超管走 `ui.preferences` 全局域，
与现有界面偏好同一条分流），`UiPreferencesSetting` 加一个 `home` 字段：

```jsonc
{ "home": { "rows": [
  { "id": "up-next" },
  { "id": "favorites", "sort": "unwatched_first" },
  { "id": "libraries" },
  { "id": "lib:1", "sort": "release_date" },             // 默认库行，改了排序、名字留空
  { "id": "row:8f2c", "library_id": 3, "sort": "rating",
    "unwatched": true, "name": "评分最高的动漫" },       // 自加
  { "id": "lib:2" },
  { "id": "row:a91e", "collection_id": 7, "sort": "random" },
  { "id": "lib:3", "hidden": true }
] } }
```

合并规则照抄 `NavUiPrefs`「存的是提示，不是契约」：

1. 存过的按存的顺序；没存过的内置行与每个可见库的默认行按出厂顺序追加在后。
   新库、新版本加的内置行，老用户一定看得到。
2. 认不出的 id 直接忽略：库被删、合集被删或被别人改成私有，那一行静默消失。管理员勾了
   「从首页排除」的库，默认行 `lib:<id>` 同样不出现（存过也一样），用户自加的行仍尊重。
3. `sort`、`order`、`name`、`unwatched` 全部可空，空即默认。`order` 只在反转自然方向时才存
   （与海报墙 `orderParam` 的约定一致），读到与自然方向相同的 `order` 视作没反转。
4. 上限 128 行，防脏数据，不是产品限制（自定义页会把合并后的整份清单存回来，
   上限必须留得比"家里有很多库"大得多）。

## 4. 接口与技术核对

### 4.1 接口

- 偏好读写：沿用 `GET/PUT /ui/preferences`，不加新路由。
- 库行数据：现有 `GET /libraries/{id}/items?sort=&w=&limit=20`；`WallSort` 加 `random`。
- 合集行数据：现有 `GET /collections/{id}/items`，加 `sort` 参数。
- 收藏行数据：现有 `GET /playback/favorites`，加 `sort` 参数，与 `unwatched_first` 并存。
- 前端按清单逐行取数，**隐藏的行不发请求**。行数多到影响首屏时再考虑聚合接口，
  v1 不做。
- 首页布局只影响 movieclaw 网页。Jellyfin 客户端的首页是它们自己的，唯一共享的是合集本身。

### 4.2 对着代码核对后补的决定

按 `services/library/items.py`、`services/library/collections.py`、`api/routes/*.py`
逐条核对后，初稿有七处没想到或想错，这里定下来：

1. **「最近观看」行会被没看过的片灌满。** `_wall_page_ids` 的度量档（`rating` /
   `runtime` / `size` / `last_played`）把空度量 `NULLS LAST` 沉底而不是排除，取 20 条时
   看过的片排完就轮到从没播过的。墙上这样是对的（墙要全量），首页这一行不行。
   决定：`WatchFilter` 加一档 `seen`（= watching ∪ played，即「不是 unwatched」），
   库行 `sort=last_played` 时前端自动带 `w=seen`。`seen` 只是接口取值，不进筛选条 UI，
   facet 三档计数的划分不受影响。
2. **合集行不区分规则/手动，同一组预设。** `resolve_members` 的名单驱动分支目前
   按 `position` 返回、忽略 `sort`。首页不需要那个顺序：合集行的排序与库行同一组
   七个预设，默认取 `collection.sort`。实现是把 `_wall_page_ids` / `_wall_scope` 的
   `only_item_id: int | None` 推广成 `only_item_ids: set[int] | None`，名单分支算出
   alive ids 后交给它排序，两种合集走同一条查询。合集页上手动合集的拖拽顺序不受影响
   （不传 `sort` 仍按 `position`）；那个功能要不要留是另一个决定，本设计不依赖它。
3. **`random` 的实现放进度量档那个形状里。** `measure = (media_item_id * 2654435761
   + seed) % 2^32`，`seed` 取 UTC 日期序数；`_NATURAL_ASC["random"] = True`，`order`
   参数对它忽略；PostgreSQL 下 `media_item_id` 先 cast 成 bigint。`build_library_index`
   对 `random` 返回空索引；`_wall_count` 不受影响；墙页的排序下拉是 `sortOptions`
   显式列表，不会自动露出 `random`。同一天内分页稳定，跨过 UTC 零点会换一批，接受。
4. **预设按库的 kind 裁剪。** `photo` 库只有 `added_at` / `title` / `random`；`video`
   库多一个 `last_played`；`movie` / `tv` 全部七个。评分、上映对家庭录像没有意义，
   列出来只会让用户选到一行空的。裁剪逻辑放在 `lib/home-rows.ts`，前端与展开区共用。
5. **「全部合集 ›」入口不能随「我的媒体库」行一起消失。** 它目前挂在那一行的标题右侧，
   行被隐藏后首页就没有去合集列表的路。决定：该行隐藏且合集数大于 0 时，把
   「全部合集」作为文字链放到页头统计行末尾；不加第三个页头按钮。

   **2026-09-13 推翻。** 这两处文字链（分区标题右端 + 隐藏时的统计行末尾）全部撤掉，
   改成页头右上角的 **首页 / 合集 分段控件**（`components/library-section-switch.tsx`）。
   理由：「全部合集」挂在某个分区标题的右端，位置既不显眼、又会随那一行的显隐
   飘来飘去，得为它专门写一条兜底规则——这本身就说明位置选错了。合集是媒体库的
   一个**同级视角**，不是藏在某一行里的一个链接，那就该由视角切换器来表达。
   形态与窄屏位置照抄发现页的 TMDB / 豆瓣（docs/design/activity.md 已把这条定成规矩）：
   桌面端在页头右上角、图标钮左边，移动端挂进全局顶栏那一行。

   连带的两条：`/library/collections` 因此**不再挂 PageNav、也没有返回键**，页头与
   首页同一副长相（同一个「媒体库」大标题 + 同一个切换器），与发现、活动、订阅这些
   分区级页面一个规矩——挂了 PageNav，移动端全局顶栏会被撤掉，切换器就没地方待。
   切换器**一律常驻**，不再「有合集才露出」：两段的分段控件少一段会变成一颗孤零零的
   胶囊，比多一段更怪；一个合集都没有时，合集视角自己有一句教人怎么建合集的空态。
6. **超管的观看状态按 `member_id=0` 算。** `list_library_items` 与 `_scope` 都是
   `principal.member_id or 0`，`w=unwatched`、`last_played` 对超管的口径与现在海报墙
   一致，沿用，不在本期处理。
7. **整体 PUT 的覆盖。** 自定义页 PUT 的是整个 `UiPreferences`，与外观设置页相同：
   基于 `useUiPrefs` 里最新的 `prefs` 只替换 `home` 再写回；跨标签页最后写入者赢，接受。

顺带记两条现状：前端 `LibraryItemSort` 缺后端已有的 `release_date_asc`，第二步补上；
名字为空的库行在库改名后自动跟着变，合集行同理；这是「留空跟随默认」额外赚到的。

## 5. 实施计划

五步，每步单独可合并、单独可发。前三步不碰排序后端，就能把「显隐、顺序、改名、
换排序」交付出去；第四步补齐首页独有的排序；第五步收尾。

### 5.1 偏好模型与合并逻辑（不改任何界面）

**后端** `src/movieclaw_api/settings/schemas.py`

- 新增 `HomeRowPref`：`id`、`sort`、`name`、`unwatched`、`hidden`、`library_id`、
  `collection_id`，除 `id` 外全部可空。`id` 只认四种形状：`up-next` / `favorites` /
  `libraries` / `lib:<int>` / `row:<slug>`；`row:` 必须且只能带 `library_id` 或
  `collection_id` 之一；`name` 上限 40 字。校验放在 Pydantic 里，坏数据在 PUT 时拒掉，
  而不是读的时候兜底。
- 新增 `HomeUiPrefs(rows: list[HomeRowPref], max_length=48)`，`UiPreferencesSetting`
  加 `home` 字段。基类 `extra="ignore"`，老数据原地生效，**不需要迁移**。
- 路由 `api/routes/ui.py` 不动：成员走 `member.ui_prefs`、超管走全局域的分流已经在。

**前端** `apps/web/lib/api/ui.ts`：类型、`DEFAULT_UI_PREFS.home`、`normalizeUiPreferences`
里对 `home.rows` 加数组守卫（与 `nav.order` 同款）。

**前端** 新建 `apps/web/lib/home-rows.ts`（不用 `@/` 别名，让 `node --test` 直接导入）：

- `SORT_PRESETS`：1.1 那张表，取值 → 推荐名函数 + 规则说明。
- `buildHomeRows(saved, libraries, collections)`：合并规则（§3）。存过的按存的顺序；
  没存过的内置行按出厂顺序补；每个当前成员可见、且清单里没有 `lib:<id>` 的库，
  在最后一条库行之后补一条默认行；`row:` 指向不可见库或不可见合集的直接丢弃。
- `rowTitle` / `rowMeta`：名字为空时跟随默认（库行按预设推荐，合集行用合集名）；
  小字「来源 · 排序 · 只看没看过的」。
- `DEFAULT_ROWS()`：出厂布局。**空清单即默认**，「恢复默认」就是存一个空列表，
  与 `nav.order` 同一约定。

**验证**

- `tests/api/test_ui_preferences.py` 照 nav 那组加：默认值、坏 id 被拒、`row:` 缺来源
  被拒、超过 48 行被拒、成员与超管各存各的。
- `apps/web/test/home-rows.test.mjs`：存过的排前、新库追加在库行之后、删库的行消失、
  隐藏保留位置、名字为空跟随预设。

### 5.2 首页按清单渲染

`apps/web/components/library-view.tsx`

- `reload()` 先拿 `listLibraries` + `listCollections`，用 `buildHomeRows` 得到清单，
  再**按清单逐行取数**，隐藏的行不发请求：
  - `up-next` → `listUpNext(20)`，渲染仍是 `UpNextRow`；
  - `favorites` → `listFavorites(20, 0, true)`（本步只支持默认的未看优先）；
  - `libraries` → 现有库卡片 `HScroller`（「全部合集 ›」原本留在这一行，已按 4.2 第 5 条撤掉）；
  - `lib:` / 库行 → `listLibraryItems(id, { sort, limit: 20, filter })`，`filter.watch` 为
    `unwatched`（开关开着）或 `seen`（`sort=last_played`，见 4.2 第 1 条；本步后端还没有
    `seen`，先在前端把空度量的条目截掉，5.4 补上后去掉这段截断）；
  - 合集行 → `listCollectionItems(id, { limit: 20 })`（本步还没有 `sort` 参数，先按合集页现状的顺序）。
- `lib/api/libraries.ts` 的 `LibraryItemSort` 补上后端已有的 `release_date_asc`。
- 行标题用 `rowTitle`，`moreHref` 库行指向 `/library/{id}`、合集行指向合集页。
  `MediaRow` 不需要改：它本来就只有标题和一个「查看全部」。
- 页头「管理媒体库」左侧加「自定义首页」，链到 `/library/customize`；全部隐藏时渲染
  指回该页的空态。页头右上角另有 首页 / 合集 分段控件（4.2 第 5 条）。
- 轮询与 stale-while-error 沿用 `useVisiblePolling` 现有分档，不动。

**验证**：现有 `library-view` 相关测试跑绿；手工核对默认布局与改造前逐行一致
（这是本步的成功标准：**没有偏好的用户看不出任何变化**）。

### 5.3 自定义首页页面

- `apps/web/app/(app)/library/customize/page.tsx`：与 `favorites/page.tsx` 同样的壳。
- `apps/web/components/library-customize-view.tsx`：
  - 数据来自 `useUiPrefs()`；每次改动 `savePrefs` 整体 PUT，去抖 500ms；没有保存键。
  - 列表：复用 `library-manage-row.tsx` 的拖拽模式（`draggable` + before/after 落点 +
    键盘上下移），不引入拖拽库；上下箭头两端同时给，手机上只有箭头（§2.4）。
  - 点行头就地展开：排序单选（`SORT_PRESETS` / 收藏与合集各自的短表）、「只显示我没看过的」
    开关（仅库行）、名字输入框（库行与合集行都有；占位 = 各自的默认名，`onBlur` 才
    写入，避免每个键入都 PUT）。
  - 「＋ 添加一行」：库来自 `listLibraries`（只列 `viewer_access` 的），合集来自
    `listCollections()`；新行 id 为 `row:` + 6 位 base36 随机串，添加后自动展开。
  - 「恢复默认」带确认，写空列表。
  - 布局与密度按 §2.4；桌面列表最大宽 640px。
- 侧栏与首页之间的返回：页头「‹ 媒体库」。

**验证**：`apps/web/test` 加纯逻辑用例（新增行 id 生成、移动、显隐不改顺序）；
手机宽度 375px 下逐项核对 §2.4 的规格；改动 300ms 内落库、刷新后保留。

### 5.4 首页独有的排序补齐（后端）

- `random`：`services/library/items.py` 的 `WallSort` / `_NATURAL_ASC` / `_wall_page_ids`
  各加一档；顺序用算术哈希 `(media_item_id * 2654435761 + seed) % 2^32` 做 `ORDER BY`，
  `seed` 取当天日期整数，SQLite 与 PostgreSQL 都不需要扩展；分页在同一天内稳定。
  `api/routes/libraries.py` 里重复的 `Literal` 与前端 `LibraryItemSort` 同步。
- `WatchFilter` 加 `seen`：`_watch_clause` 返回 `or_(watching, played)`；`_filter_params`
  的 Literal 与前端 `WatchFilter` 类型同步，筛选条 UI 不列它。
- 合集行排序：`GET /collections/{id}/items` 加 `sort`（`WallSort`），不给即现状（规则合集
  按 `collection.sort`，手动合集按 `position`）。`_wall_page_ids` / `_wall_scope` 的
  `only_item_id` 推广成 `only_item_ids`，名单分支算出 alive ids 后交给它排序（4.2 第 2 条）。
- 收藏行排序：`GET /playback/favorites` 加 `sort`（`favorited_at` / `rating` / `title`），
  与 `unwatched_first` 并存。
- 前端三个 API 客户端补参数；`library-view.tsx` 把行的 `sort` 透传。

**验证**：`tests/api/test_library_items.py` 加随机档「同日稳定、跨日变化、分页不重不漏」
与 `w=seen`「只含播过的」；
`test_collections_api.py` / `test_playback_favorites.py` 各加排序用例。

### 5.5 收尾

- 合集详情页头部加「显示在首页」开关，写同一份偏好（找到就切 `hidden`，找不到就追加一行）。
- `docs/design/library-home-up-next.md` 第 2 节的信息层级标注「已由首页透视取代，默认布局不变」。
- 样稿与本文按最终实现回写差异。

### 5.6 改动面清单

| 层 | 文件 | 改动 |
|---|---|---|
| 后端偏好 | `settings/schemas.py` | `HomeRowPref` / `HomeUiPrefs` / `UiPreferencesSetting.home` |
| 后端排序 | `services/library/items.py`、`api/routes/libraries.py` | `random` 档、`seen` 筛选 |
| 后端接口 | `api/routes/collections.py`、`services/library/collections.py`、`api/routes/playback.py` | `sort` 参数、名单合集走同一条排序查询 |
| 前端偏好 | `lib/api/ui.ts`、`lib/home-rows.ts`（新） | 类型、默认、合并、命名 |
| 前端 API | `lib/api/libraries.ts`、`collections.ts`、`playback.ts` | 补 `release_date_asc`、`sort` |
| 首页 | `components/library-view.tsx` | 按清单渲染、入口、空态 |
| 自定义页 | `app/(app)/library/customize/page.tsx`、`components/library-customize-view.tsx`（新） | 列表、展开编辑、添加、恢复默认 |
| 合集页 | 合集详情视图 | 「显示在首页」开关 |
| 测试 | `tests/api/test_ui_preferences.py`、`test_library_items.py`、`test_collections_api.py`、`test_playback_favorites.py`；`apps/web/test/home-rows.test.mjs` | 见各步 |

不在清单里的：数据库迁移（没有）、`docker/runtime-version`（没动依赖）、Jellyfin 兼容层（不受影响）。

## 6. 刻意不做

- **多套具名首页**（「晚上追剧」「陪娃」切换）：多成员切换已经是一层视角，合集又是
  一层，单成员多套首页是同一件事的第三层。`rows` 套进 `views[]` 即可扩展，不现在做。
- 行的样式（卡片大小、横竖形态）：行的形态由来源决定，不是偏好。
- 在首页上直接编辑合集规则：去合集页，首页只覆盖排序。
- 跨库合并的「最近添加」：可作为后续的内置行 `recent:all`，不进 v1。

## 7. 实现记录

- 偏好：`settings/schemas.py` 的 `HomeRowPref` / `HomeUiPrefs`，PUT 时校验形状；
  前端 `lib/home-rows.ts` 负责合并、命名、预设裁剪（`node --test` 直接跑）。
- 首页：`components/library-view.tsx` 按清单逐行取数，隐藏的行不发请求；同一个库同一种
  排序只请求一次，库卡片封面与默认的「最近添加」行共用 `added_at` 那一份。
- 自定义页：`app/(app)/library/customize` + `components/library-customize-view.tsx`。
  改动进本地草稿立即呈现，400ms 去抖后整体 PUT；换位只有把手拖拽（指针事件，触屏可用）。
- 后端排序：`random` 用 `(media_item_id × 当日乘数) % 2^32`，当日乘数 = 黄金比例常数 ×
  (2 × UTC 日期序数 + 1)。种子必须进乘数：加在后面只是整体平移，换天换不了批（实现时
  测出来的）。`WatchFilter` 加 `seen`。合集与收藏的 `sort` / `order` 参数、名单合集按度量
  排序（`sort_item_ids`）在合并前已由主分支落地（收藏页与合集页排序对齐），本功能直接复用；
  合集接口没有 `release_date_asc` 档；方向拆成开关之后这一档整个消失，取数统一走 `sort + order`。
- 合集页 `⋯` 菜单加「显示在首页 / 从首页移除」，写同一份偏好。
- 与初稿的差异：合集行不区分规则 / 手动，同一组预设；自定义页不放海报；收起只留名字与
  眼睛、展开是一条紧凑设置行（排序下拉框）、无类型图标；去掉上下箭头只留把手拖拽；页头两个入口都是图标钮
  （均为用户评审后的决定）。样稿 `mockups/library-home-perspective-demo.html` 停留在
  展开态那一版，以实现为准。
