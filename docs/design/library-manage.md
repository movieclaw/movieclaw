# 媒体库：预览与管理分离

> 状态：已实现（2026-09-05）；2026-09-06 按 §2.4 改版列表布局；浏览器端到端见 `tests/e2e/test_library_manage_browser.py`
>
> 样稿：`docs/design/mockups/library-manage-demo.html`（三屏：首页改后 / 管理页桌面 / 管理页手机）
>
> 相关：`library.md`（库模型）、`library-routing.md`（收藏范围）、`library-access.md`（可见范围）、
> `library-home-recently-watched.md`（首页「最近观看」）、`mockups/library-import-watch-demo.html`（自动入库标签样稿）

## 1. 问题

`/library` 首页的库卡片同时承担「内容入口」和「管理面板」两个角色：封面叠扫描进度环、
待识别徽标、入库中徽标、仅管理锁卡，右上角悬停 ⋯ 菜单里放编辑、扫描、整理、刷新、设默认、
左移/右移、删除。库数量增长后出现三类症状：

- 新建的库落在横滚可视区外。代码里为此专门加了「滚进视野并高亮 2.5 秒」的补丁
  （`highlightId`），这是布局不合适的信号，不是需要继续修补的功能。
- 排序靠菜单里「左移/右移」一次挪一格，十几个库时不可用。
- 没有全局视图：想知道哪些库在扫、哪些有待识别、哪些成员看不到，得逐张卡片滚过去读徽标。

## 2. 设计结论

**预览和管理分成两个页面。首页只做浏览入口，管理进独立页 `/library/manage`。**

| | 首页 `/library` | 管理页 `/library/manage` |
|---|---|---|
| 服务对象 | 所有成员 | 有库管理权限的账号（当前为管理员） |
| 布局 | 横滚保留：最近观看 → 我的媒体库 → 各库最近添加 | 纵向表格，一库一行；手机端一库一卡 |
| 库卡片上保留 | 封面拼图、库名、「默认」标、扫描进度环、「N 个新文件入库中」徽标 | — |
| 从首页撤走 | ⋯ 菜单、左移/右移、「待识别 N」徽标、仅管理锁卡、「添加媒体库」按钮、收藏范围重叠黄条 | 全部落到这里 |
| 新增 | 页头右侧「管理媒体库」按钮（仅管理权限可见） | 搜索、类型筛选、拖拽排序、状态列、单一 ··· 操作菜单 |

判断依据：卡片上留下的扫描进度环和入库中徽标是**预览信息**（它们预告你马上会看到新内容），
其余都是**管理信息**（要你做决定或动手）。

### 2.1 首页改动清单

1. 页头右侧「添加媒体库」换成「管理媒体库」（`canManageLibraries` 时渲染），跳 `/library/manage`。
2. `LibraryCard` 去掉 `LibraryCardMenu` 及其 `canMoveLeft / canMoveRight / onMove / onEdit /
   onOrganize / onError` 属性；封面上去掉「N 个待识别」徽标；`busy` 遮罩与「入库中」徽标保留。
3. `viewer_access === false` 的库（超管不在浏览范围内）不再出现在首页卡片区。
4. 首页不再渲染 `routingOverlapWarnings`。
5. 删除 `highlightId` 相关的滚动定位与高亮逻辑；新建库在管理页完成，管理页的新行天然在视野内
   （纵向列表末尾，创建后滚到该行即可）。
6. 空状态（一个库都没有）保留「创建第一个媒体库」按钮，落点改为 `/library/manage?create=1`。
7. `LibraryFormDialog`、`LibraryOrganizeDialog` 从首页移出。首页 `library-view.tsx` 只剩浏览逻辑。

### 2.2 管理页

地址 `/library/manage`。侧栏「媒体库」保持高亮（`navIdFromPath` 已按前缀匹配，不需改）。
页面结构自上而下：

```text
‹ 返回媒体库
媒体库管理                                                 [＋ 创建媒体库]
18 个媒体库 · 4896 个条目 · 31.6 TB  [● 1 个在跑任务] [● 3 个库有待处理文件]
[媒体库 18] [回收站 14]
[⌕ 按库名或根目录搜索] [全部 18][电影 4][剧集 6][其他 7][图片 1]
⚠ 收藏范围重叠提示（原首页黄条，逐条渲染）
┌───────────────────────────────────────────────────────────────────────────┐
│ ⠿ [图] 大陆电影 默认                     100 部   ● 12 个待识别          ··· │
│        电影 · /remote/media/电影/大陆    100 个文件  最近扫描 3 分钟前          │
├───────────────────────────────────────────────────────────────────────────┤
│ ⠿ [图] 欧美电影  🔒指定成员 2            331 部   ● 正在扫描 42% ▓▓▓░░       ··· │
│        电影 · 未声明收藏范围 · /remote/media/电影/欧美   334 个文件  128 / 300 │
└───────────────────────────────────────────────────────────────────────────┘
把指针停在行上，拖动行首的把手调整首页「我的媒体库」的展示顺序，松手即保存
```

**行的四个区**（数据全部来自现有 `MediaLibrary`，无新接口）：

| 区 | 内容 | 来源字段 |
|---|---|---|
| 拖拽柄 | ⠿，指针停在行上或键盘聚焦时才现形，拖拽改顺序 | — |
| 库（弹性宽） | 缩略图（服务端拼贴图；无海报用类型图标，你不在浏览范围内时是锁）、**库名**（链接到 `/library/{id}`，一行里唯一的白色加粗）、「默认」标、**偏离默认时**的可见范围胶囊（全部成员且自己能看 = 默认，不挂）；第二行小字，顺序是「类型 · 备注 · 根目录」：备注只念**偏离默认或有待办的配置**（警示色「未声明收藏范围」、「从首页排除」、「实时监控关」）；主根目录等宽字体放最后（多根折成 `+N 个根目录`，悬停展开），窄屏下它整体换到下一行而不是把备注拆散；手机端不在这行里，而是在库名块下方独占卡片一整行 | `name` `kind` `is_default` `access_mode` `member_ids` `admin_visible` `viewer_access` `root_paths` `match_rules` `exclude_from_home` `realtime_watch` |
| 库存（定宽右对齐） | 两行安静的数字：`N 部`（图片库「张」、其他库「个条目」）/ `M 个文件`；手机端并入库名下那行小字 | `stats.item_count` `stats.file_count` |
| 状态（定宽状态道） | 见下表；上下行对齐成一条竖道，眼睛顺着往下扫 | `scanning` `scan_progress` `organizing` `organize_progress` `metadata_refresh` `last_scan` `stats.unidentified_count` `stats.missing_count` |
| 操作 | 唯一一个 ··· 按钮 | — |

**状态列**按优先级取第一个命中的：

| 优先级 | 条件 | 圆点色 | 第一行 | 第二行 |
|---|---|---|---|---|
| 1 | `scanning` | 蓝（进行中） | `扫描 · {阶段词} {pct}%` + 进度条 | `已处理 / 总数 · 开始于 X 前` |
| 2 | `organizing` | 蓝 | `整理文件名 {pct}%` + 进度条 | 同上 |
| 3 | `metadata_refresh.refreshing` | 蓝 | `刷新元数据 {pct}%` + 进度条 | `正在处理「片名」· 阶段` |
| 3.5 | `chapter_job` 非空（生成章节，低优先级作业常排在前三种后面） | 蓝 | `生成章节排队中` / `正在生成章节 {pct}%` + 进度条 | `等前面的任务跑完再开始` / `已处理 / 总数 · N 个失败` |
| 4 | `last_scan.deferred > 0` | 蓝 | `N 个新文件入库中` | `等文件写完自动补扫` |
| 5 | `stats.missing_count > 0` | 红胶囊 | `N 个待识别 · M 个缺失`（待识别为 0 时只写缺失）；刮削型库的胶囊是链接，点开 `/library/{id}?pending=1` | `最近扫描 X 前` |
| 6 | `stats.unidentified_count > 0` | 黄胶囊 | `N 个待识别`，同上可点 | `最近扫描 X 前` |
| 7 | 其余 | 无圆点、无状态词（「空闲」不是需要看的状态） | — | `最近扫描 X 前 · 结论`：结论是「新增 N 个文件」「标记缺失 M」「手动停止」，都没有写「无新文件」——扫描常毫秒级完成，行内只有这句能证明"点了有反应"；实时监控关了才作为配置备注出现在库名下 |

阶段词复用 `SCAN_PHASE_LABELS`；进度百分比复用单库页 `busyText` 的算法。

**··· 菜单**（Radix DropdownMenu，与单库页 `LibraryActionsMenu`、站点配置一致）：

```text
扫描库                  ← scanning 时变「停止扫描 42%」
待处理 · 14 个文件      ← 跳 /library/{id}?pending=1；库快照只有文件数，写明单位，与单库页按「组」数的待处理区分
整理文件名              ← 打开 LibraryOrganizeDialog（busy 时置灰）
刷新元数据              ← refreshing 时变「停止刷新」；无刮削能力的库不显示
──────
编辑库                  ← 打开 LibraryFormDialog（scanning / organizing 时置灰，原因同现有）
设为默认库              ← 已是默认时置灰
从首页排除 / 在首页展示 ← 切 exclude_from_home，走 updateLibrary
──────
删除库  不动磁盘        ← 二次确认，文案沿用现有 useConfirm
```

菜单项与现有 `LibraryCardMenu` + 单库页 `LibraryActionsMenu` 的并集一致，**不新增功能**。
「左移/右移」被拖拽取代，删除。

**页头摘要**（副标题位置，`summarizeLibraries`）：

- 规模事实一句：`N 个媒体库 · M 个条目 · X TB`。
- 「● N 个在跑任务」：`libraryIsBusy` 的库数；「● N 个库有待处理文件」：`libraryNeedsAttention`
  的库数（有待识别或缺失，且没有任务在跑、没有文件在入库——与状态列的优先级同一口径，
  否则摘要说 3 个待处理、筛出来的行却显示扫描进度），含缺失时圆点用红。两枚胶囊都是筛选
  （`filter.focus`），再点一次取消；在回收站标签上点则先切回库列表。都为 0 时写「一切正常」。

**工具栏**：

- 搜索：客户端过滤，匹配 `name` 与任一 `root_paths`（库列表一次全拉，几十个库不需要服务端搜索）。
- 类型筛选胶囊：全部 / 电影 / 剧集 / 其他 / 图片，胶囊上带计数。
- 筛选或搜索生效时拖拽柄隐藏（顺序是全量的，局部列表上拖没有意义），底部提示改为「清除筛选后可拖拽排序」。

**拖拽排序**：

- HTML5 原生拖放（`draggable` + `onDragStart/onDragOver/onDrop`），不引入新依赖；表格行拖拽只需
  同列表内上下换位，原生 API 够用。指针设备 hover 到 ⠿ 才显示抓手光标。
- 落点按指针在目标行的上半/下半判定「之前/之后」，提示线画在对应边沿；拖影用整行。
- 松手后乐观更新本地顺序，调 `reorderLibraries(全部 id)`；失败回滚并 toast 报错。这与首页现有
  `moveLibrary` 的提交模型一致，后端接口不变。
- 键盘可达：⠿ 聚焦后 `Alt+↑ / Alt+↓` 换位，读屏用户不依赖拖拽。
- 手机端（`max-md`）不做长按拖拽：··· 菜单里多一项「调整顺序」，进入一个只列库名的
  上下箭头列表弹窗，确认后一次提交。

**轮询**：复用首页那套 `useVisiblePolling` 与节奏（busy 3s → 刷新 5s → 入库中 10s → 空闲 30s）
和 `reloadSeq` 乱序守卫。管理页只打 `listLibraries` 一个接口：缩略图直接用服务端拼贴图
`/libraries/{id}/cover`（与首页卡片封面同源），不逐库拉条目。
不是从这页发起的任务（实时监控触发的自动扫描、CLI、另一台设备）只靠轮询要等到下个周期，
空闲时最长 30 秒——所以另外盯着 `JobsProvider`（活动页那条 SSE）里库相关活跃作业的
「id + 状态」指纹，作业出现、状态变化、结束都立即重拉一次；进度更新仍交给轮询，避免每条进度都打一次接口。

**创建 / 编辑**：`LibraryFormDialog` 搬到管理页。`?create=1` 进入页面即打开创建弹窗（首页空状态
的落点）。创建成功后新行滚进视野（`scrollIntoView`），不再需要高亮。

**手机端**：同一行组件按断点切成卡片：缩略图右侧只放两行——库名 + 「默认」标 + 可见范围胶囊 + ···，
小字（类型 · 库存主数字 · 配置备注）——两行正好与缩略图等高，图不再上下露边；根目录在其下方独占卡片一整行
（拿到整个卡片宽度，长路径不必早早截尾）；最后是状态，状态格跨整行，进度条通栏填满卡片（桌面端受状态道宽度约束）。
「创建媒体库」在页头标题右侧。

**反馈**：动作成功/失败都走全站 toast（开始扫描、设默认、首页展示开关、删除、排序有一句回执），不用页顶
横条——列表长时用户在底部操作，看不到顶部。开始扫描必须有回执：已是最新的库扫描毫秒级就结束，
行内只会从「最近扫描 3 分钟前」变成「几秒前 · 无新文件」，没有回执用户会以为没点上。列表带 `role="list"/listitem` 语义（`aria-label="媒体库列表"`）。

### 2.4 列表布局改版（2026-09-06）

首版是七列表格（拖拽柄 / 库 / 根目录 / 库存 / 状态 / 可见范围 / 操作）。上线后的问题：

- **挤**：笔记本宽度下每列都只剩截断，根目录、状态文案各自截一半；行内三处等重的文字块
  （加粗的库名、大号的库存数、状态句）互相抢眼。
- **没有重点**：库存数字是最大最亮的元素，但它不是管理员来这页要看的东西；真正要看的
  「N 个待识别」是一行灰字；「仅自己」「收藏范围 1 项条件」「实时监控开」逐行重复，
  等于什么都没说。
- **不知道该看什么**：页面没有回答「有没有事要我管」——唯一的信号是工具栏最右一枚「N 个在跑任务」。

改版原则：**一行两个重心（库名、状态），其余降为小字；页头先回答「有没有事」。**

| | 首版 | 改版 |
|---|---|---|
| 副标题 | 一句页面说明 | 活的摘要：规模事实 + 「在跑任务」「有待处理文件」两枚可点胶囊，或「一切正常」 |
| 列 | 七列表格，带表头与颜色图例 | 四区列表（库 / 库存 / 状态 / 操作），无表头无图例——胶囊自带文字 |
| 根目录、可见范围 | 各占一列 | 根目录并入库名下的小字；可见范围只在偏离默认时挂胶囊 |
| 配置备注 | 「收藏范围 N 项条件」「实时监控开/关」逐行写 | 只写偏离默认或有待办的：「未声明收藏范围」「从首页排除」「实时监控关」 |
| 待识别 / 缺失 | 灰字 + 彩色圆点，动手要进 ··· 菜单 | 带色胶囊，点它直达待处理清单（菜单里的「待处理」保留） |
| 库存 | 大号加粗 | 定宽右对齐的两行安静数字 |
| 拖拽柄 | 常显 | 指针停在行上或键盘聚焦时现形 |
| 工具栏「N 个在跑任务」 | 工具栏最右 | 上移到页头摘要，与「待处理」并列 |

纯函数层的对应改动：`LibraryFilter.busyOnly` 改为 `focus: "busy" | "attention" | null`；新增
`libraryNeedsAttention`、`summarizeLibraries`、`configNotes`、`accessRestricted`；`libraryStatus`
的空闲态第二行不再带实时监控开关。

### 2.3 权限

- `/library/manage` 页面组件挂载时若 `!canManageLibraries`，渲染「没有管理权限」空状态并给
  「返回媒体库」链接；不做路由级重定向（与设置页处理管理分区的方式一致，后端本就 403）。
- 首页「管理媒体库」按钮和空状态的创建按钮都受 `canManageLibraries` 控制。

## 3. 文件改动

### 新增

| 文件 | 内容 |
|---|---|
| `apps/web/app/(app)/library/manage/page.tsx` | 路由壳，`metadata.title = "媒体库管理"`，渲染 `LibraryManageView` |
| `apps/web/components/library-manage-view.tsx` | 管理页主组件：加载/轮询、搜索筛选、表格与手机卡片、拖拽、弹窗挂载 |
| `apps/web/components/library-manage-row.tsx` | 表格行与手机卡片（同一组件按断点切模板），含状态列与 ··· 菜单 |
| `apps/web/lib/library-manage.ts` | 纯函数：`libraryStatus(lib)`（§2.2 状态表）、`filterLibraries(libs, query, kind, busyOnly)`、`moveInList(ids, from, to)` |
| `apps/web/test/library-manage.test.mjs` | 上述纯函数的单测（`node --test`，与现有 `test/*.test.mjs` 同一套） |

### 修改

| 文件 | 改动 |
|---|---|
| `apps/web/components/library-view.tsx` | 删 `LibraryCardMenu`、`highlightId`、`moveLibrary`、`routingWarnings` 渲染、弹窗挂载；`LibraryCard` 瘦身；页头换「管理媒体库」；卡片与页头统计同用过滤掉 `viewer_access === false` 后的列表。从 2320 行减到约 610 行 |
| `apps/web/components/library-form-dialog.tsx`（新拆） | 把 `LibraryFormDialog / CreateLibraryDialog / EditLibraryDialog / RootsEditor / ScopeEditor / AccessScopeEditor / ViewerCombobox / SwitchRow` 等表单组件从 `library-view.tsx` 拆出，首页不再 import 它们 |
| `apps/web/components/library-kind-meta.ts`、`apps/web/lib/library-routing-warnings.ts`（新拆） | `LIBRARY_KIND_META` 与 `routingOverlapWarnings` 从首页模块抽出，管理页、表单弹窗、单库页从这里导入，不再为一张表把首页整个模块图拖进依赖 |
| `apps/web/components/library-detail-view.tsx` | 挂载时读 `?pending=1` 打开待处理抽屉（落在第一个有内容的 tab，与 ⋯ 菜单进抽屉同一规则）；头部不另加管理入口，⋯ 菜单里的编辑/扫描/整理/刷新原样保留 |
| `docs/design/library.md` | 「页面」一节补一行指向本文 |

### 不动

- 后端：所有接口已有（`listLibraries / reorderLibraries / updateLibrary / setDefaultLibrary /
  deleteLibrary / startLibraryScan / stopLibraryScan / startLibraryOrganize /
  startLibraryMetadataRefresh / stopLibraryMetadataRefresh`）。无迁移，无 `runtime-version` 变更。
- `LibraryOrganizeDialog`、`LibraryScrapeSettings`、`IssueDrawer`：原样复用。
- 侧栏导航项：不新增；「媒体库」项按 `/library` 前缀高亮已覆盖 `/library/manage`。

## 4. 实施步骤与验证

```text
1. 拆表单弹窗到 library-form-dialog.tsx，首页行为不变
   → 验证：pnpm lint 通过；首页创建/编辑库流程手工走一遍无回归
2. 写 lib/library-manage.ts 纯函数 + 单测
   → 验证：node --test test/library-manage.test.mjs 全绿（状态优先级 7 档各一例、筛选、换位）
3. 管理页骨架：路由 + 表格 + 状态列 + 轮询，··· 菜单接现有 API
   → 验证：12 个库（含扫描中、刷新中、仅管理、多根）的本地数据下逐行核对状态列与菜单可用态
4. 拖拽排序 + 键盘换位 + 筛选时禁用
   → 验证：拖后刷新页面顺序保持；断网拖拽回滚并出错误条；筛选中 ⠿ 不显示
5. 手机端卡片布局 + 「调整顺序」列表
   → 验证：375px 宽下无横向滚动；顺序提交成功
6. 首页瘦身：撤菜单/徽标/黄条/高亮，加「管理媒体库」入口，过滤仅管理库
   → 验证：非管理员看不到入口；空状态按钮落到 ?create=1 并自动弹窗
7. 详情页 ?pending=1 打开抽屉；文档收尾
   → 验证：从管理页「待处理」跳过去抽屉已打开在正确 tab
```

每步一个 commit，全部在 `claude/media-library-preview-management-5zivn1` 分支上，最终一个 PR。
CI 门禁为 `pytest -m "not integration"`（后端无改动）与前端 `pnpm lint`；前端单测本地跑。

## 5. 明确不在本期范围

- **「自动入库」「待处理」两个标签**：样稿保留了三段标签栏，但本期只实现「媒体库」一段，标签栏
  暂不渲染（避免出现点不动的标签）。自动入库从设置搬到这里、跨库待处理汇总列表，按
  `mockups/library-import-watch-demo.html` 另开设计与 PR。届时管理页只需在标题下插入标签栏。
  （2026-09-06：标签栏已随「回收站」标签落地，见 `library-recycle-bin.md`；「自动入库」「待处理」仍待后续。）
- **服务端搜索 / 分页**：库列表规模在几十以内，客户端处理即可。
- **批量操作**（多选后一起扫描/删除）：没有明确需求，不做。
- **侧栏挂库列表**（Plex 式）：首页胶囊条都嫌挤时再考虑。
