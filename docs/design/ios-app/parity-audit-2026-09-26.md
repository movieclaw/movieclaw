# MovieClaw iOS App 与网页手机端对等审计报告

- 审计对象：`feat/ios-app` 合并结果（工作树 `/Users/yee/workspace/mc-ios-audit`，HEAD `470206e0`），模拟器 MC-Audit（iOS 27 模拟器，Debug 构建）
- 对照基准：NAS 正式服务 `http://192.168.1.10:3000`（账号 yee，超级管理员）网页手机视口（Playwright iPhone 15 Pro）；清单 `docs/design/ios-app/parity-inventory.md` 第 0–13 节，清单与网页源码不符处以 `apps/web/` 源码为准
- 不在范围：`/library/manage`、LibraryFormDialog（编辑库表单）、`/s/{slug}` 访客页
- 日期：2026-09-25 ~ 26

## 方法与局限

1. **逐条代码核对**：12 个分节审计员按清单逐条读网页源码与 App 源码（入口、接口与参数、确认框文案、字段、权限裁剪、轮询、空态/错误态），各分节全文见 `/tmp/audit/sec-*.md`（每份含 A 逐条核对表，本报告只汇总 B/C 两部分）。
2. **运行期对照**：主审计员对 30 余个路由做两端同路由截图（网页 `/tmp/audit/web/`、App `/tmp/audit/app/`，缩略图 `/tmp/audit/sm/`），并用临时 XCUITest（`MovieClawUITests/AuditParityUITests.swift`，未提交）只读打开菜单/弹层/长按/筛选（`/tmp/audit/ui/`），网页交互截图在 `/tmp/audit/webui/`。
3. **真实数据**：主审计员用 NAS 只读 GET 核对了库、合集、订阅、会话、搜索快照、成员、facets 等数据形状。部分分节审计员的 NAS GET 被本机权限分类器拦截，那几节（条目详情、搜索、AI 会话、设置下半）的解码结论依据网页类型与后端源码。
4. **局限**：
   - NAS 上没有图片库、待处理四个清单都为空、当前账号收藏为 0、无人在播，这些条目只做了代码核对；
   - 写操作（保存/删除/订阅/下载）一律没有真实执行，操作结果按代码与接口核对；
   - 网页截图取自 NAS 正在运行的版本（v0.27.1-dev），与工作树 `apps/web` 可能有细小出入，出现分歧时以截图为准；
   - Netflix 主题没有切换验证（需要写 `theme_mobile`），结论来自代码。
5. **播放进度副作用（需告知用户）**：播放器 XCUITest 第一次运行时 XCUITest 等不到界面空闲而挂住，影片多播了约 5 分钟。另外为判别 P0 又两次各起播约 10–15 秒。结果《蜘蛛侠：英雄无归》（media_item 6434）的续播点从 **47:14（2834473 ms）** 推进到约 **52:54（3174771 ms）**。按只读要求，主审计员没有回写，是否恢复请用户决定。

## 1. 总览

「一致」含部分分节标注为平台差异、但功能等价的条目。「未验证」指源码读取被拦截或缺数据、未能下结论的条目。差异清单的行数与「有差异+缺失」不完全相等，原因有三：同一根因的多个条目合并成一行；平台差异单列在第 3 部分；另外补了 8 条跨节的运行期发现（R 开头）。

| 清单章节 | 条目数 | 一致 | 有差异 | 缺失 | 未验证 | 其中 P0 |
|---|---|---|---|---|---|---|
| 0 外壳与导航（银玻璃 + Netflix 手机端） | 44 | 26 | 13 | 5 | 0 | 1（Netflix 整体缺失） |
| 1 认证 | 18 | 11 | 7 | 0 | 0 | 0 |
| 2 发现 | 29 | 19 | 10 | 0 | 0 | 0 |
| 3 媒体详情与影人 | 20 | 14 | 6 | 0 | 0 | 0 |
| 4 媒体库（首页/自定义/收藏/合集/单库/待处理/条目详情） | 168 | 136 | 30 | 1 | 1 | 0 |
| 5 播放器 | 55 | 44 | 9 | 2 | 0 | 1（中央三键缺失） |
| 6 搜索 | 70 | 51 | 18 | 0 | 1 | 0 |
| 7 订阅 | 48 | 37 | 10 | 1 | 0 | 0 |
| 8 活动 | 40 | 32 | 8 | 0 | 0 | 0 |
| 9 AI 会话 | 32 | 21 | 11 | 0 | 0 | 0 |
| 10 设置（21 个分区） | 243 | 205 | 37 | 1 | 0 | 1（成员保存抹授权） |
| 11 分享弹层 | 6 | 5 | 1 | 0 | 0 | 0 |
| 12 其它通用行为 | 11 | 8 | 2 | 1 | 0 | 0 |
| 13 实时行为 | 23 | 21 | 2 | 0 | 0 | 0 |
| **合计** | **807** | **630** | **164** | **11** | **2** | **3** |

差异清单严重度分布（去重后）：**P0 3 条，P1 33 条，P2 114 条**。

**P0 速览**
1. **SA-1 在 App 里编辑成员并保存，会静默清空该成员「指定成员」库的授权**：用 NAS 真实数据复核过，xxx 的 [18,21]、nnn 的 [21] 都会被清成 []。修复前不要在 App 里保存成员。
2. **P-1 播放器中央的「后退 10s / 播放暂停 / 前进 10s」不出现**：两次复现，正常播放中无障碍树里没有 `player-play-pause`。
3. **S-1 Netflix 手机端整体缺失**：外观页却能选 Netflix 并提示「切换立即生效」。需要产品决定：实现，还是在 App 里禁用该选项。

**已撤销的疑似问题（运行期核验后判为一致）**
- 「更多 → 切换账号」弹不出：实测能正常弹出，证据 `/tmp/audit/ui/more_switch_account.png`。
- 老的种子搜索快照解码失败：扫了 NAS 上全部带快照的资源搜索历史，没有缺字段，降为 P2 健壮性问题（SR 表里那一行）。
- 活动任务徽标红/蓝不一致：两端都是红色 1，确有一条待处理的换种任务。网页观看页的初始蓝色，是任务数据还没加载完。

## 2. 差异清单

编号前缀：S 外壳/认证/通用/实时，D 发现，LH 媒体库首页组，LD 单库页，LI 条目详情/分享，P 播放器，SR 搜索，SB 订阅，A 活动，AI 会话，SA 设置上，SBB 设置下，R 运行期跨节发现。表按严重度排序。「证据」一栏写「代码核对」的，具体行号见「App 现状」。

| 编号 | 清单章节 | 条目 | 网页行为 | App 现状 | 严重度 | 涉及文件 | 建议修法 | 证据 |
|---|---|---|---|---|---|---|---|---|
| S-1 | 0/1/12/13 外壳·认证·通用·实时 | 0.32–0.36、12.6 Netflix 手机端整体缺失 | theme_mobile=netflix 时：带文字的停靠底栏（发现 / 媒体库 / 订阅 / 我的，themes/netflix/chrome/tab-bar.tsx:22-33）、NetflixMyPage（themes/netflix/pages/my-page.tsx:106-163）、M 标顶栏（components/app-shell.tsx:348-370、571-590）、/ → /library + Billboard（app/(app)/page.tsx:25-32）、NetflixSubscriptionsPage | 外壳从不读 GET /ui/preferences，永远是银玻璃形态；外观页可以选 Netflix 并写 theme_mobile（AppearanceSettingsView.swift:166-176），脚注却称「切换立即生效」（:161） | P0（需产品决定：实现，或列为平台差异并在外观页去掉 / 置灰 Netflix） | AppearanceSettingsView.swift | 至少改脚注并禁用 Netflix 选项、写明「App 固定银玻璃」；要对等就在 MainTabView 按 themeMobile ?? theme 切换外壳分支 | 代码核对；App 全部截图均为银玻璃（/tmp/audit/app/*.png） |
| P-1 | 5 播放器 | B-1 #23 中央三键 | 控制层可见、不在转圈、不是报错/同意状态时，中央一直显示三键（video-player.tsx:3328-3334） | 运行期截图 /tmp/audit/ui/player_1.png：在播（47:17）、顶栏和底栏都在，**中央三键没有显示，转圈的 PlayerBusyView 也没有显示**。静态推演：MPV 路径的 playbackRestart→reportState→`.playing`（MPVEngine.swift:204-206, 251-259）和 AVPlayer 路径的 timeControlStatus→`.playing`（AVPlayerEngine.swift:235-240）都能把 phase 推到 `.playing`（PlaybackController.swift:545-550）。如果 phase 卡在 isBusy，画面正中会有转圈和「正在缓冲…」（PlayerScreen.swift:162-164）——截图里也没有。所以更可能**不是状态卡住，而是画面中心那一块的 SwiftUI 内容没画出来或被挡住**（中央三键和转圈都在视频矩形 y≈807–1193 内；截图里视频只露出上面一条，下面是黑的，也像是渲染层异常）。条件本身（PlayerScreen.swift:273）没有逻辑错误<br>**运行期核验**：两次复现：正常播放中（47:17、52:46）、无缓冲转圈、顶/底栏可见，但无障碍树中 `player-play-pause` 不存在（exists=false），说明是显示条件/状态机问题而非被画面层遮挡；缓冲中截图 /tmp/audit/ui/player_center_1.png 转圈正常。 | P0 | AVPlayerEngine.swift, MPVEngine.swift, MPVRenderViews.swift, PlaybackController.swift, PlayerScreen.swift | 先按 D-1 做判别：①用无障碍查询 `player-play-pause` 存不存在、能不能点；②在「⋯→播放引擎」切到「系统播放器」再截图。如果按钮存在但看不见 → 查 MPVGLView/MPVMetalView 的图层合成（MPVCore/MPVRenderViews.swift）；如果按钮不存在 → 在 PlaybackController 的 phase didSet 里打日志，看引擎事件 | /tmp/audit/ui/player_1.png、/tmp/audit/ui/player_center_2.png（两次复现） |
| SA-1 | 10 设置（上） | #25/#28 成员编辑保存会抹掉「指定成员」库的授权 | `listLibraries()` 固定带 `scope=all`（lib/api/libraries.ts:448-451），selectedModeIds 覆盖全部 selected 库，保存时保留 xxx 的 [18,21]（members-section.tsx:485-497） | MembersSettingsView.swift:86 `api.libraryList()` 没带 scope，只拿到 19、20；:443 的 selectedModeIds = [19,20]；:546 `libraryIds.filter { selectedModeIds.contains($0) }` 把 [18,21] 过滤成 []，PUT 后 **AV、成人图片的授权被清空**（只改昵称也会触发）。编辑弹窗里也看不到、无法勾选这两个库<br>**运行期核验**：主审计员已用 NAS 真实数据复核：两名成员的「指定成员」库授权（18 成人图片、21 AV）都不在 App 拿到的库列表里，任何一次保存都会被过滤成 []。修复前请勿在 App 里保存成员。 | **P0** | MembersSettingsView.swift | :86 改成 `api.libraryList(scope: "all")`。建议再加保险：allLibraries 时用 `member.libraryIds` 中不在 `libraries` 里的 id 兜底并入（未知库一律保留） | 代码 MembersSettingsView.swift:86/443/546 + NAS 只读数据 GET /members：xxx all_libraries=true library_ids=[18,21]、nnn [21]；GET /libraries 仅返回 19、20（未实际点保存） |
| S-2 | 0/1/12/13 外壳·认证·通用·实时 | 0.26 会话行 ⋯ 菜单 | more-page.tsx:169-177 行尾常驻 ConversationMenu（⋯）按钮 | MorePage.swift:140-148 `.contextMenu`，只能长按，界面上没有任何提示 | P1 | MorePage.swift | 行尾加 `Menu { … } label: { Image(systemName: "ellipsis") }`，四个动作不变 | /tmp/audit/ui/more_sheet.png 对照 /tmp/audit/web/my.png（网页行尾有 ⋯，App 无） |
| S-3 | 0/1/12/13 外壳·认证·通用·实时 | 0.21 / 12.5 更新入口文案 | app-update-entry.tsx:86-88：「新版本 v{app_version}」或「新识别模型 {model_tag}」 | MorePage.swift:68 固定「有可用更新」 | P1 | MorePage.swift | ShellBadges 保存 PendingUpdateView，按同一规则拼标签 | 代码核对 |
| S-4 | 0/1/12/13 外壳·认证·通用·实时 | 1.16 / 0.23 成员落点与路由守卫 | lib/permissions.ts:44-62：成员的 / /new /sessions → /library；登录（login/page.tsx:21-30）、切换（account-switcher-dialog.tsx:70）、退出（more-page.tsx:76）都经过 accessiblePathFor | App 重建后固定选中 `.discover`（Router.swift:76）；`Permissions.allows`（Permissions.swift:28-37）没有调用方 | P1 | Permissions.swift, Router.swift | MainTabView 首次出现时成员 `router.selectedTab = .library`；router.open 前调 `permissions.allows(route)`，不通过就落媒体库 | 代码核对 |
| S-5 | 0/1/12/13 外壳·认证·通用·实时 | 1.7 初始化 409 | setup/page.tsx:66-69：409 → router.replace("/login") | LoginView.swift:128-130 只显示错误，停在初始化页 | P1 | LoginView.swift | 在 createAdmin 捕获 `APIError.status == 409`，把 `phase` 设为 `.needsLogin` | 代码核对 |
| D-1 | 2/3 发现·详情·影人 | 电影 / 剧集切换时的筛选状态 | 切类型走 `router.push('/discover/{next}?source=')`，筛选全部清空（dv:246-252）。电影和剧集的 TMDB 类型 ID 是两套（电影 28=动作，剧集 10759=动作冒险） | `filters` 是 DiscoverView 的 `@State`，切类型不清空（DV:21，DV:125）。筛选结果网格按 `currentType` 重建（DV:35-38），拿电影的 genre_ids 去查剧集：结果不对，chips 退化成「N 个类型」<br>**运行期核验**：已复现：电影下选「动作」后切到剧集，App 显示「没有符合条件的影片」，标签变成「1 个类型」。 | P1 | — | 在 Picker 的 set 里（DV:125）类型真的变了就执行 `filters = .empty`，与网页一致 | /tmp/audit/ui/discover_filtered_movie.png → /tmp/audit/ui/discover_filtered_tv.png |
| D-2 | 2/3 发现·详情·影人 | 「已订阅」点击的落点（Hero、海报卡、详情页） | Hero（dv:823,940-942）、卡片（pc:530）、详情页（md:516）都调用 `openSubscribe` → SubscribeDialog 预检发现 `existing_subscription_id` → 进入管理态：「该电影/剧集已在订阅中，movieclaw 正在持续追踪资源。」+「好的」+「取消订阅」（sd:440-481） | 三处都 `router.push(.subscription(id:))`，直接跳订阅详情页（DV:377-378，PC:219-225，MD:204-205） | P1 | SubscribeSheet.swift | 二选一：① 改成 `router.present(.subscribe(...))`，由 SubscribeSheet 的管理态接手（SubscribeSheet.swift:105/143 已有 existingSubscriptionId 分支），与网页完全一致；② 若产品认可跳详情页更好，在清单里登记为有意差异 | 代码核对 |
| D-3 | 2/3 发现·详情·影人 | 海报卡「首点展开信息层」 | 触屏且卡片有订阅类动作、用户有订阅权限时，第一下只展开信息层（类型、简介、「订阅影片」按钮），第二下才进详情；点卡片外收起（pc:201-235，信息层 pc:450-490） | 点一下直接进详情；信息层做成长按上下文菜单的预览，菜单里放订阅动作和「查看详情」（PC:134-168，PC:214-239，PC:251-294）。订阅入口从「点一下可见」变成「长按才有」 | P1 | — | 需要产品拍板。要严格一致，就给 DiscoverPosterCard 加 `revealed` 状态：第一下在海报底部叠信息层和按钮，第二下 `open()`。若接受原生长按，则登记为平台差异，并考虑给首次使用加提示 | /tmp/audit/ui/discover_longpress.png（App 长按菜单：订阅影片 / 查看详情） |
| D-4 | 2/3 发现·详情·影人 | 上游不可达时错误态的 hint | `toErrorInfo` 从 `details[0].hint` 取出后端给的下一步提示（dv:79-91），不可达时显示在消息下方（dv:615-617） | `APIErrorBody` 只解码 `message/code`（Core/Networking/APIClient.swift:47-50），`DiscoverFeed.Failure` 里没有 hint（DV:198-201），错误视图只显示 message（DV:466-468） | P1 | APIClient.swift | APIErrorBody 增加 `details: [{hint?}]?`，APIError.http 带出 hint，DiscoverErrorView 在 unreachable 时补一行 `Text(hint)`（textFaint） | 代码核对 |
| LH-1 | 4 媒体库首页·自定义·收藏·合集 | 自定义页读取失败（#35） | 库或合集读取失败时 `libraries` 保持 null，**整个行清单不渲染**，只挂「读取媒体库与合集失败，行清单可能不完整；刷新页面重试。」，用户无法在残缺清单上编辑（library-customize-view.tsx:84-97, 351-355, 367） | 三个请求任一失败都走 `libraries = libraries ?? []`（LibraryCustomizeView.swift:108-112），清单照常渲染且可编辑；此时 build 找不到库/合集，所有 `lib:*`、带 library_id/collection_id 的 `row:*` 行都被丢弃（LibraryHomeRows.swift:227-245）。用户只要点一下任意眼睛/排序，400ms 后 toPrefs 整份 PUT，**用户自建的库行、合集行、改名、排序全部永久丢失** | P1 | LibraryCustomizeView.swift, LibraryHomeRows.swift | 读取失败时不渲染可编辑清单（或整页禁用编辑，只保留重试）；或保存时把 build 没认出的原始 pref 行原样并回 | 代码核对 |
| LH-2 | 4 媒体库首页·自定义·收藏·合集 | 收藏墙格子信息（#42） | 与单库墙同一个 InventoryCell（favorites-view.tsx:696-701 → poster-wall.tsx:128-190）：剧集显示库存概况（「第 N 季 · x/y 集」等）信息层、带「补齐缺集 / 自动续订」动作（libraryCardAction）、心、缺失提示；**不显示收藏层级** | 用的是裸 LibraryPosterCell（FavoritesView.swift:137-148）：没有库存概况、没有补齐缺集/自动续订入口、没有评分信息层（单库/合集墙的 LibraryInventoryCell 用长按菜单提供这些，LibraryWall.swift:277-290）；反而多出网页墙上没有的左上角「收藏了第 N 季 / S01E02」角标 | P1 | FavoritesView.swift, LibraryWall.swift | 改用 LibraryInventoryCell（FavoriteItemView 与 LibraryItemView 字段同构，可转成 LibraryItemView 或给 cell 加泛型入口），去掉收藏层级角标（或保留但视为有意增强需拍板） | 代码核对 |
| LH-3 | 4 媒体库首页·自定义·收藏·合集 | 图廊模式的「回到上次位置」（#44） | 图廊模式同样记录/提示，按瓦片所属作品算 offset，点胶囊 `jumpGalleryTo(offset)` 换图廊窗口（favorites-view.tsx:548-572, 717-723） | 胶囊在图廊模式照样弹出，但点了执行的是 `pager.jump`（海报墙分页器，图廊下不可见），图廊不动，看起来点了没反应（FavoritesView.swift:53-60）；图廊模式下也不写位置记录（trackVisible 只认海报格 id，FavoritesView.swift:50-52, 177-186） | P1 | FavoritesView.swift | 图廊模式下要么不弹胶囊，要么给 LibraryGalleryWall 增加 jump(offset) 与可见分组回报，按作品 offset 读写同一条记录 | 代码核对 |
| LH-4 | 4 媒体库首页·自定义·收藏·合集 | 从条目详情返回（#45，需运行验证） | 模块级快照 + 滚动恢复：返回时整窗对账（reload(loadedCount)），不动滚动位置（favorites-view.tsx:90-119, 254-283, 377-386） | `.task(id: sort)` 在视图每次重新出现时都会重跑 → `reload()` → `pager.reset`（`items = nil`，LibraryWall.swift:35-39），同时 `.onAppear` 的 `pager.refresh()` 因 generation 变化被作废（FavoritesView.swift:93-97）。预期结果：从详情返回后墙被清空重载、跳回墙首，已加载的深处位置丢失 | P1（疑似） | FavoritesView.swift, LibraryWall.swift | 首载与换排序分开：用 `.onChange(of: sort)` 触发 reset，`.task` 只在 `pager.items == nil` 时首载；返回时只走 refresh | 代码核对 |
| LH-5 | 4 媒体库首页·自定义·收藏·合集 | 收藏图廊分组标题（#48） | 分组标题是链接，点进 `/library/{lib}/item/{id}`（video-gallery.tsx:365-370） | FavoritesView 创建 LibraryGalleryWall 时没传 `onOpenItem`（FavoritesView.swift:126-128），默认空闭包（LibraryGalleryWall.swift:240），标题是个点了没反应的按钮（:315）；合集页已正确传入（CollectionDetailView.swift:260-262） | P1 | CollectionDetailView.swift, FavoritesView.swift, LibraryGalleryWall.swift | 与合集页相同，传 `onOpenItem: { router.push(.libraryItem(libraryId: $0.libraryId, itemId: $0.mediaItemId)) }` | 代码核对 |
| LD-1 | 4 单库页·待处理 | 元数据刷新状态来源 | 进入页面先 GET `/libraries/{id}/metadata/refresh/progress` 探一次（web LDV:804-812）；「开始刷新」之后也立刻 GET 一次（1516-1522）；刷新中每 2s 用 progress 接口更新 `metaRefresh`（818-832）。菜单、面板、墙轮询都读这份状态。后端的 progress 接口对「已排队、还没开跑」的持久化作业同样返回 `refreshing:true`（src/movieclaw_api/api/routes/libraries.py:1734-1755） | `refreshingMeta` 只读库列表里的 `metadata_refresh`（LDV:64），而库列表用的 `_metadata_refresh_view` 只看进程内状态，没有持久化作业的兜底（routes/libraries.py:529-540,718）。作业排队期间（例如排在扫描后面，或因 JobRetry 等待）以及刚点「开始」到 worker 接手之间，App 都不显示进度面板（LDV:273），⋯ 菜单仍是「刷新元数据」而不是「停止刷新」（LDV:583-588），墙轮询还停在 30s 档（LDV:168-172） | P1 | — | 在 LDV 里另存一份 `metaRefresh`：`.task` 和 `toggleMetaRefresh` 成功后各调一次 `libraryMetadataGetRefreshStatus`，`refreshingMeta` 改为「列表状态 或 progress 状态」；或者让面板一直挂着（只对管理员），把自己的 refreshing 状态回传给宿主 | 代码核对 |
| LD-2 | 4 单库页·待处理 | 照片墙不随轮询/下拉刷新更新 | 照片墙直接用页面的 `items`，30s/3s 轮询的 `reload()→fetchWall` 会整窗重拉（web LDV:475-573,1871-1882）。扫描入库的新照片、删除的照片会自动出现或消失 | PhotoWallView 有自己的 Feed，只在 `reloadKey` 变化时重载（PhotoWallView.swift:41）；LDV 的 `reload()` 只刷新 `pager`（LDV:701），下拉刷新（LDV:233）也碰不到照片 Feed。扫描过程中照片墙不变，只能退出再进来 | P1 | PhotoWallView.swift | 给 PhotoWallView 加一个 `refreshToken` 参数（例如 LDV 每轮 reload 递增一次），Feed 增加 `refresh()`，按已加载窗口整窗重拉（参照 LibraryWallPager.refresh），不动滚动位置 | 代码核对 |
| LI-1 | 4/11 条目详情·分享弹层 | 修正识别结果后条目页状态 | 拍板只 `setReidentifyDirty(true)`，关窗才 `reload()`；reload 失败 `setFailed(true)`，显示「未能加载该条目」和返回键（library-item-detail-view.tsx:302-313, 1085-1097） | 每组拍板后 `onApplied` 立刻 `reload()`（LibraryItemDetailView.swift:605），`reload()` 的 catch 只在 `detail == nil` 时置 failed（:626-629）。文件全部改挂或标为非独立作品后，条目页继续显示已不存在的旧条目，下拉刷新也静默失败 | P1 | LibraryItemDetailView.swift | 在 `.sheet(item:)` 的 onDismiss 里统一 reload；reload 返回 404 时不论已有 detail 都置 `failed = true`（或区分 404 与网络错误） | 代码核对 |
| LI-2 | 4/11 条目详情·分享弹层 | 重复文件入口 | `/library/manage?tab=duplicates&item={mediaItemId}`，管理页按本条目筛选（:540） | `router.push(.libraryManage(tab: "duplicates"))`（:486），路由没有 item 参数（AppRoute.swift:38）；目标 LibraryManageView 目前是占位页（LibraryManageView.swift:3-10） | P1 | AppRoute.swift, LibraryManageView.swift | AppRoute.libraryManage 加 `item: Int?`，深链解析 `query["item"]`，管理页重复文件标签按它筛选；管理页上线前这个入口其实是死路 | 代码核对 |
| LI-3 | 4/11 条目详情·分享弹层 | 更换图片保存失败的表现 | 未能比对（源码读取被拦） | `apply` 出错时 `failed = true`，网格整块换成「候选图加载失败（TMDB 可能不可达）」（ArtworkPickerSheet.swift:208-210, 90-98） | P1（待确认） | ArtworkPickerSheet.swift | 保存失败只弹 feedback.error，不要置 failed；请主审计员对照 artwork-picker-dialog.tsx 确认 | 代码核对 |
| P-2 | 5 播放器 | B-2 #13 掉帧转码 | 直通档连续掉帧超过阈值（FRAMEDROP_RATIO 0.1、10 次采样、至少 100 帧，framedrop.ts:30-34）→ `failed`「直通播放持续掉帧（x%），正在换转码重试」（video-player.tsx:1596-1622） | 没有；`droppedFrames` 只进诊断和指标（PlaybackController.swift:890-902） | P1 | PlaybackController.swift | tickSecond（:932）里按同样的窗口和阈值对 `engine.stats()` 做判定：只在 `video.action == "copy"` 且不暂停、不在 seek 时判，命中就走 engineFailed(.decode) | 代码核对 |
| P-3 | 5 播放器 | B-3 #14 卡顿恢复 | 三种判定：解码卡死 8s（前方缓冲 ≥3s 却不动）先推两次再降档；缺粮 45s，直出 15s；取流持续失败 → `onNetworkDead` 同档原地重开，不降档（stall.ts:18-44、engine.ts:196-250、video-player.tsx:1129-1139） | 两个引擎都只有「缓冲 45s → .starved」（AVPlayerEngine.swift:34, 252-256、MPVEngine.swift:42, 272-276）；AVPlayer 的 failedToPlayToEnd 和 item.failed 一律 `.decode` → 降档（AVPlayerEngine.swift:201-205, 223-224） | P1 | AVPlayerEngine.swift, MPVEngine.swift | 对 NSURLErrorDomain / AVFoundationErrorDomain 里的网络类错误（-1005、-1009、-11863 等）改成同档 `request(startMs:positionMs, phase:.sessionStarting)`，不累加 failedTiers；档 0 直出的缺粮上限改成 15s | 代码核对 |
| P-4 | 5 播放器 | B-4 #38 AVPlayer 模式字幕 | ASS 用 jassub 加内封字体原样渲染；PGS 不烧录时用 libbitsub canvas 渲染；iOS 画中画和原生全屏靠 `master_url` 或 VTT track 把文本字幕带进系统层（video-player.tsx:3268-3319，pipSubtitleUrl） | 叠加层对 ASS 请求 `&format=vtt` 得到纯文本（PlaybackController.swift:781-785）；PGS 在烧录被撤回（consent 回退 :388-392）后菜单里是选中状态，画面上却什么都没有；SwiftUI 叠加层进不了画中画和 AirPlay，文本字幕在那两处消失 | P1 | PlaybackController.swift | ①自动引擎模式下，当前选中的字幕是 ass/pgs 时优先选 MPV；②AVPlayer 放 VOD 时用 `session.masterUrl`，让画中画和 AirPlay 由系统渲染字幕（进入画中画时关掉叠加层）；③PGS 被撤回烧录时把 selectedSubtitle 置回 nil，或者弹出提示 | 代码核对 |
| SR-1 | 6 搜索 | 51 图览灯箱 | 灯箱顶栏带 详情/投给订阅/下载，舞台用 photo-screen，缩略条用 photo-tile，放大到 1:1 时取原图（search-results.tsx:2554-2566,2738-2772） | 只传 urls（photoScreen）、title、brokenHint，没有操作按钮（TorrentActions.swift:126-135） | P1 | TorrentActions.swift | 给 DiscoverLightbox 加 actions 插槽，放「查看详情/投给订阅/下载」（复用 TorrentActionsState.startDownload/grab）；缩略条与原图按需补上 | 代码核对 |
| SB-1 | 7 订阅 | #14 Netflix 主题订阅页 | theme_mobile=netflix 时 /subscriptions 渲染 `NetflixSubscriptionsPage`：预告行（横版卡）+ 四分区「追更中的剧集 / 订阅的电影 / 已收齐 / 已暂停」（components/subscriptions-page.tsx:16-19；themes/netflix/pages/subscriptions-page.tsx:162-196） | 外观设置允许选 Netflix（Settings/Sections/AppearanceSettingsView.swift:102），但订阅页只有银玻璃布局<br>**运行期核验**：与 S-1「Netflix 手机端整体缺失」同一根因，随 S-1 的产品决定一并处理。 | P1（如果 App 定位就是只做银玻璃，改记为设计取舍，需主审计员统一定性） | AppearanceSettingsView.swift | 要么实现 Netflix 订阅页四分区，要么在 App 外观设置里说明「Netflix 仅网页生效」 | 代码核对 |
| A-1 | 8 活动 | 结束播放 / 注销此设备：路径里的 deviceId | `encodeURIComponent(deviceId)`（lib/api/playback.ts:313、:462） | 直接插进路径：`"/playback/activity/sessions/\(deviceId)/end"`、`"/playback/devices/\(deviceId)"`（Features/Activity/WatchPanel.swift:195、:201）。`APIClient.url` 用 `appending(path:)`（Core/Networking/APIClient.swift:95-101），遇到 `/` 会当成路径分隔、遇到 `?` 会当成查询串切开。Jellyfin Web 客户端的 DeviceId 是 base64，可能带 `/`，这类设备上两个操作会 404，只报「失败」 | P1（有条件触发；当前 NAS 没有在播会话，无法用真实数据复现） | APIClient.swift, WatchPanel.swift | 两处都先把 deviceId 百分号编码再拼：`deviceId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(["/","?","#"]))`；生成的 `playbackActivityEnd` 同样有这个问题，可以在生成器里统一处理字符串类型的路径参数 | 代码核对 |
| AI-1 | 9 AI 会话 | 6 添加图片的来源 | `<input type=file accept="image/jpeg,image/png,image/webp,image/gif" multiple>`（composer.tsx:228-238），iPhone 上弹「照片图库 / 拍照 / 选取文件」 | 只有 `.photosPicker(... matching: .images)`（AgentComposer.swift:112），不能拍照、不能从「文件」选图 | P1 | AgentComposer.swift | 「+」菜单拆成「照片图库 / 拍照 / 选取文件」三项：分别用 PhotosPicker、UIImagePickerController(.camera)、fileImporter([.jpeg,.png,.gif,.webP])，都走 addPhotos 同一条压缩上传链 | 代码核对 |
| SA-2 | 10 设置（上） | #21 成员列表的媒体库范围摘要 | 「全部共享库 + 指定成员的库：AV、成人图片」（members-section.tsx:265-278） | 「全部共享库」（MembersSettingsView.swift:182-189，根因同上） | P1 | MembersSettingsView.swift | 同上，一处修好两处生效 | /tmp/audit/web/settings_members.png 对照 /tmp/audit/app/settings_members.png |
| SA-3 | 10 设置（上） | #7 体检修复卡丢了 fix_params 预填 | 「去补映射」跳 `/settings/downloaders?suggest_mapping=<anchor>`，自动展开默认下载器的编辑表单并预填一行映射、显示「已按体检建议预填…」提示（downloader-config-section.tsx:97-111,1003-1013,1167-1172）；「去建规则」跳 `/settings/import-watch?suggest=auto&kinds=movie,tv`，自动打开新建规则弹窗、预选自动路由，保存一条后接着预填下一类型（import-watch-section.tsx:91-105,178-196） | OverviewSettingsView.swift:159-168,252-255 只 push 到分区；AppRoute 没有携带参数的分区路由；两个分区页也没有对应的预填入口 | P1 | OverviewSettingsView.swift | 给 `.settingsSection` 加可选参数（如 `suggestMapping: String?`、`suggestKinds: [String]`），DownloadersSettingsView 落地后打开默认下载器的 EditorSheet 并预填映射行，ImportWatchSettingsView 落地后打开 EditorSheet，initial target 设为 `.auto(kind)` 并按队列连建 | 代码核对 |
| SA-4 | 10 设置（上） | #59 下载器测试连接轮询可能延迟 30s | 状态一进入 pending/verifying 就按 2000ms 轮询（downloader-config-section.tsx:122-131） | DownloadersSettingsView.swift:64 用 `hasInProgress ? 2 : 30`，Polling.swift:22-23 在 sleep 前取间隔，空闲时正在睡的 30s 不会被打断，新增 / 编辑 / 重新测试后最长要等 30s 才看到状态变化 | P1 | DownloadersSettingsView.swift, Polling.swift | 与站点页相同：固定 `.polling(every: 2)`，回调里 `guard hasInProgress`；或在 upsert、verify 之后主动启动一次短轮询 | 代码核对 |
| SBB-1 | 10 设置（下） | 刮削页的「N 个库已覆盖」统计 | `listLibraries()` 固定带 `scope=all`，统计全部库的 `scrape_overrides`（scrape-settings-section.tsx:1338-1352；lib/api/libraries.ts:449） | `api.libraryList()` 没带 scope，只返回当前身份可浏览的库（ScrapeSettingsView.swift:245） | P1（根因与总表同一条） | ScrapeSettingsView.swift | 改成 `api.libraryList(scope: "all")` | 代码核对 |
| SBB-2 | 10 设置（下） | 定时任务页的网络挂载对账建议 | 同上，`scope=all` 后看 `network_mount`（scheduled-tasks-section.tsx:37-40） | 同样没带 scope（ScheduledTasksPanel.swift:57-59） | P1（同一根因） | ScheduledTasksPanel.swift | 同上 | 代码核对 |
| SBB-3 | 10 设置（下） | 系统日志：自动刷新请求在途时点「刷新」或切换日期 | inFlight 守卫直接 return，而 loading 已经置为 true、不会复位，界面卡在「加载中…」，切日期后内容还是旧日期的（system-logs-section.tsx:173,197,208） | App 照搬了同样的守卫（LogsSettingsView.swift:114,121,138,171） | P1（两端同时存在，不计入对等差异） | LogsSettingsView.swift | 非静默请求绕过 inFlight 守卫，或者等在途请求结束后再发；守卫提前返回时复位 loading | 代码核对 |
| R-1 | 运行期新增（跨节） | 10 外观·背景图/界面质感在 App 不生效（0 节壳层背景） | 银玻璃主题所有页面以用户选定的背景图（GET /appearance 的 active）作底，叠界面质感（scrim blur/dark、侧栏透明度）；用户当前启用了「自定义背景」火车海景 | 所有页面背景固定为 `Theme.background` + 渐变（DesignSystem/Theme.swift:40-51 appBackground），全 App 不读 /appearance、不读 ui.preferences.scrim；外观页可上传/切换/删背景、调质感并保存成功，但 App 内看不到任何效果 | P1 | Theme.swift | appBackground 改为读取 AppearanceStore（GET /appearance active 背景 + scrim 参数），模糊/压暗后铺底；外观页保存后广播刷新 | /tmp/audit/app/settings_appearance.png（已启用自定义背景）对照 /tmp/audit/app/library.png、/tmp/audit/web/library.png |
| R-2 | 运行期新增（跨节） | 管理类页面拿不到「管理员不在浏览范围内」的库 | 网页 `listLibraries()` 固定带 `scope=all`（apps/web/lib/api/libraries.ts:448-451），需要按可浏览过滤的页面再用 `viewer_access` 自己过滤 | 多处 `api.libraryList()` 不带 scope：MembersSettingsView.swift:86、ScrapeSettingsView.swift:245、ScheduledTasksPanel.swift:57（另有 LibraryCustomizeView.swift:99、AllCollectionsView.swift:96、LibraryHomeView.swift:320、CollectionDetailView.swift:432、LibraryDetailView.swift:681、SearchScope.swift:218 用的也是默认范围，浏览类页面结果恰好与网页过滤后一致）。NAS 实测：默认范围 2 个库（电影/剧集），scope=all 4 个库（+AV、成人图片）。成员页因此把「全部共享库 + 指定成员的库：AV、成人图片」显示成「全部共享库」，编辑成员的库勾选里也缺这两个库 | P1 | AllCollectionsView.swift, CollectionDetailView.swift, LibraryCustomizeView.swift, LibraryDetailView.swift, LibraryHomeView.swift, MembersSettingsView.swift, ScheduledTasksPanel.swift, ScrapeSettingsView.swift, SearchScope.swift | 管理/设置类调用一律 `libraryList(scope: "all")`；浏览类保持默认或改 scope=all + `viewerAccess` 过滤，与网页同口径 | /tmp/audit/web/settings_members.png 对照 /tmp/audit/app/settings_members.png；NAS GET /libraries 与 ?scope=all 对比 |
| S-6 | 0/1/12/13 外壳·认证·通用·实时 | 0.3 / 13.8 待更新回焦刷新 | app-update-entry.tsx:56-57 window focus 立即 refresh | MainTabView.swift:224-229 只有 600 秒循环 | P2 | MainTabView.swift | 加 didBecomeActive 通知触发一次 | 代码核对 |
| S-7 | 0/1/12/13 外壳·认证·通用·实时 | 0.11 活动角标呈现 | glass-tab-bar.tsx:98-108：单颗圆点，红 / 绿 / 蓝 | MainTabView.swift:212-217：红底「N」/「在看」/「进行中」 | P2（受平台限制） | MainTabView.swift | 可接受；或只在需处理时显示数字、其余不显示，避免红底误导成告警 | 代码核对 |
| S-8 | 0/1/12/13 外壳·认证·通用·实时 | 0.14 电影 / 剧集切换位置 | 底栏 bottom accessory（glass-tab-bar.tsx:395-410） | DiscoverView.swift:124-128 放在顶栏 principal | P2 | DiscoverView.swift | 可改用 iOS 26 `.tabViewBottomAccessory` | 代码核对 |
| S-9 | 0/1/12/13 外壳·认证·通用·实时 | 0.18 个人信息返回链 | app-shell.tsx:312-315：/settings/[x] → /settings | 从「更多」直接压 settingsSection(.profile)，返回回到标签根 | P2 | — | 可先压 `.settings` 再压 `.settingsSection(.profile)` | 代码核对 |
| S-10 | 0/1/12/13 外壳·认证·通用·实时 | 0.19 待处理行 | notice-center.tsx:232-235：「待处理」+ 红底计数；30 秒轮询 | MorePage.swift:52-54：「待处理事项」+ 灰字计数；只拉一次 | P2 | MorePage.swift | 改文案与徽标色；「更多」页加 `.polling(every: 30)` | 代码核对 |
| S-11 | 0/1/12/13 外壳·认证·通用·实时 | 0.24 最近会话 | more-page.tsx:181-197 可「收起」；limit 20（agent-conversations.tsx:199、568）；行只有标题 | 只能展开不能收起；limit 50（MorePage.swift:159）；行下多一行相对时间 | P2 | MorePage.swift | 加「收起」切换；limit 对齐 20 | 代码核对 |
| S-12 | 0/1/12/13 外壳·认证·通用·实时 | 0.25 运行中圆点 | more-page.tsx:255-257：`--info` 蓝 + animate-pulse | MorePage.swift:129-130：Theme.success 绿；`.symbolEffect(.pulse)` 对 Circle 不生效 | P2 | MorePage.swift | 换 Theme.info，用 `.opacity` 动画或 SF Symbol `circle.fill` 做脉冲 | 代码核对 |
| S-13 | 0/1/12/13 外壳·认证·通用·实时 | 0.27–0.31 会话菜单文案 | 复制：「会话 ID 已复制」；删除确认 标题「彻底删除会话「{title}」？」、说明「服务器上的完整对话记录将一并删除，此操作不可恢复。」、按钮「彻底删除」；fork 失败「创建续接会话失败：…」；重命名初值是显示标题，没变化就跳过；空态「还没有会话，点上方的「新会话」开始。」（more-page.tsx:80-116、160） | 「已复制会话 ID」；「删除这个会话？」/「会话记录将被永久删除，无法恢复。」/「删除」；原始错误；初值 `item.title ?? ""`，不判断是否有变化；「还没有会话。点右上角「+」开始一个新任务。」（MorePage.swift:96、144、176-188） | P2 | MorePage.swift | 逐字对齐 | 代码核对 |
| S-14 | 0/1/12/13 外壳·认证·通用·实时 | 1.2 记住我默认值 | login/page.tsx:57 `useState(false)` | LoginView.swift:24 `remember = true` | P2（可能是有意为之） | LoginView.swift | 确认产品意图；要对等就改成 false | 代码核对 |
| S-15 | 0/1/12/13 外壳·认证·通用·实时 | 1.6 初始化文案 | 标题「初始化」，副标题「欢迎使用。请设置超级管理员账号——它是本站唯一的管理身份，此流程仅在首次部署时出现。」，按钮「创建账号并进入」（setup/page.tsx:77-110） | 组头「创建管理员账号」，脚注「这台服务器还没有初始化。创建的账号将成为超级管理员。」，按钮「创建并进入」（LoginView.swift:52-56、104） | P2 | LoginView.swift | 对齐文案 | 代码核对 |
| S-16 | 0/1/12/13 外壳·认证·通用·实时 | 1.9 / 1.10 / 1.12 / 1.14 切换账号弹窗 | 副标题「本浏览器已登录的账号，点击即可切换，不用再输密码。」；当前账号「✓ 当前」；行尾可见 × 移除；满额提示「最多同时保存 5 个账号，移除一个后可再添加」；退出全部说明「本浏览器里的 {N} 个账号都会退出登录，再回来需要逐个重新输入密码。共用设备时建议这样做。」（account-switcher-dialog.tsx:113-118、139-141、186-189、240-256） | 无副标题；只有勾；左滑移除；满额不提示；「本机保存的所有账号都将退出登录。」（AccountSwitcherSheet.swift:26-44、73-75、121） | P2 | AccountSwitcherSheet.swift | 加 Section header / footer 文案，行尾加 × 按钮或保留左滑并加说明 | 代码核对 |
| S-17 | 0/1/12/13 外壳·认证·通用·实时 | 1.1 / 1.15 会话过期与后台重新校验 | http.ts 401 → /login?next=原路径；AuthGate 每次挂载后台重新取 /auth/me（auth-gate.tsx:77-95） | 401 → `.needsLogin`，再登录后回到标签根（AppModel.swift:152-154）；权限快照只在冷启动 / 登录 / 切换时刷新 | P2 | AppModel.swift | 回前台时静默调用 authMe 并执行 `model.update(session:)` | 代码核对 |
| S-18 | 0/1/12/13 外壳·认证·通用·实时 | 12.8 久别回归触发位置胶囊 | library-wall-recall.ts:158-205：后台 ≥30 分钟回来，算重新进入 | 只在页面出现时判断 | P2 | — | 监听 scenePhase，离开 ≥30 分钟回到 active 时重新读取 recall | 代码核对 |
| S-19 | 0/1/12/13 外壳·认证·通用·实时 | 13.17 验证中快轮询的延迟 | useVisiblePolling 在 interval 变化时立即重启（2 / 2.5 秒） | Polling.swift:22-25 按上一轮的间隔睡完才重新取值，空闲 30 / 10 秒 | P2 | Polling.swift | PollingModifier 的 `.task(id:)` 带上当前间隔，间隔变了就重启 | 代码核对 |
| D-5 | 2/3 发现·详情·影人 | 错误态图标 | 只有不可达时显示地球图标，普通失败不显示图标（dv:606-610） | 普通失败也有 `exclamationmark.triangle`（DV:464-465） | P2 | — | 可接受；要一致就在非 unreachable 时去掉图标 | 代码核对 |
| D-6 | 2/3 发现·详情·影人 | 筛选弹层副标题 | 标题下：「筛选会写入网址，可直接分享或收藏」（fd:133） | 表单页脚：「筛选仅 TMDB 数据源支持」（DF:67-69） | P2 | — | App 没有网址，保留现文案即可，登记为有意差异 | 代码核对 |
| D-7 | 2/3 发现·详情·影人 | 切数据源时的筛选状态、深链参数 | 切源走 `/discover/{type}?source=`，筛选清空（dv:230-236）。深链 `/discover/tv?source=douban&genres=18` 能完整恢复视角（app/(app)/discover/[type]/page.tsx） | 切源不清筛选（切回 TMDB 又回到筛选结果）；`AppRoute(webPath:)` 只取类型，丢掉 source 和筛选参数（AR:201），`router.rootParameter` 也只带类型（DV:49-53） | P2 | — | 切源时 `filters = .empty`。`.discover` 路由加上 source（和可选的筛选）参数，DiscoverView 从 rootParameter 读取 | 代码核对 |
| D-8 | 2/3 发现·详情·影人 | 筛选结果、片单全列表的手机列数 | `grid-cols-2`，手机 2 列（fv:154，cg:361） | `GridItem(.adaptive(minimum: 104))`，393pt 宽时 3 列（PC:422-424） | P2 | — | 给这两处单独用两列定义，例如 `[GridItem(.flexible()), GridItem(.flexible())]`；影人页网页本来就是 3 列，保持现状 | /tmp/audit/web/discover_movie_collections_tmdb_trending-day.png 对照 /tmp/audit/app/同名.png |
| D-9 | 2/3 发现·详情·影人 | 发现页海报的「已订阅」蓝斜标 | `resolveRibbon` 只从 libraryStatus 派生「已入库」，发现页、筛选结果、片单、详情页相似推荐都没有「已订阅」斜标（pc:133-145）；只有 `/discover/people` 显式传入（dp CreditCard） | `showsSubscribedRibbon` 默认 true，所有 DiscoverPosterCard 未入库但已订阅时都打「已订阅」（PC:115，PC:127-132） | P2 | — | 信息比网页多。要一致就把默认值改成 false，只在 DiscoveredPersonView 传 true；或者登记为 App 增强 | 代码核对 |
| D-10 | 2/3 发现·详情·影人 | 详情页「列表字段先显示」 | 从卡片进来时用 `getMediaSeed` 预存的标题、海报、简介秒开（md:108）；有预存数据时详情接口失败也不打断页面 | 一律先显示「正在加载详情…」转圈，等接口返回（MD:29-35）；接口失败直接进错误页 | P2 | — | Router 推 `.mediaDetail` 时捎带 DiscoverPosterItem（或放一个全局预存缓存），MediaDetailView 先用它渲染头部 | 代码核对 |
| D-11 | 2/3 发现·详情·影人 | 详情页手机 Hero 用哪张图 | 手机（物理宽 ≤1280）上 `useWantsOriginalImage` 为 false，用 w1280 的 backdropUrl（md:224） | TMDB 条目用 `backdropOriginalUrl` 原图（MD:137-139），每次进详情多下载 1–3 MB | P2 | — | 手机上改用 `detail.title.backdropUrl ?? posterUrl`，与网页一致 | 代码核对 |
| D-12 | 2/3 发现·详情·影人 | 简介「展开全文」的出现条件 | 实测 `scrollHeight > clientHeight` 真溢出才显示（md:649-700） | `text.count > 88` 粗估（MD:279-280）：长英文简介不到 4 行也会出按钮，多段换行的短简介超过 4 行却可能没有 | P2 | — | 用 `ViewThatFits`，或测量完整高度与 4 行高度（onGeometryChange）来判断截断 | 代码核对 |
| D-13 | 2/3 发现·详情·影人 | 预告片播放 | 点卡片 → 弹层里用 iframe 内嵌 `embed_url?autoplay=1&rel=0`；同时拿 i.ytimg.com 探测本机能否连 YouTube，连不上就换成说明：「当前浏览器无法直连 YouTube …」+「在 YouTube 打开 ↗」（md:820-890）。行下方平时没有提示 | 点卡片 → SFSafariViewController 打开 `watch_url`（MD:320-322）；行下方一直显示「预告片由 YouTube 提供，需要本机能访问 YouTube 才能播放。」（MD:353-356）；不探测可达性 | P2（部分平台差异） | — | 可接受 Safari 打开。若要对齐：探测 `i.ytimg.com/vi/{key}/default.jpg`，不可达时改弹 Alert 说明，不再常驻那行提示 | 代码核对 |
| D-14 | 2/3 发现·详情·影人 | 剧照灯箱缩略图条 | ImageLightbox 底部有缩略图条，剧照 100px 横图、海报竖图（md:1044，image-lightbox.tsx:287） | DiscoverLightbox 只能左右滑，显示「序号 / 总数」，没有缩略图条（LB:27-119） | P2 | — | 可接受；要对齐就在底部加一个横滚缩略图条，放在动作键上方 | 代码核对 |
| LH-6 | 4 媒体库首页·自定义·收藏·合集 | 继续观看卡片图片加载失败兜底（#8） | 剧照 URL 存在但加载失败时：剧集回退印集号 `S01E02`，电影回退 MoviePosterFill（海报模糊铺底）（up-next-row.tsx:165-183） | URL 非空时只传 `fallbackText: code`，电影为 nil；加载失败只剩 RemoteImage 的 film 占位图标（LibraryHomeView.swift:605-606；LibraryPosterCell.swift:22-44 仅在 url==nil 时印字） | P2 | LibraryHomeView.swift, LibraryPosterCell.swift | LibraryArtwork 在 RemoteImage 失败时也显示 fallbackText；电影失败时回退到海报铺底 | 代码核对 |
| LH-7 | 4 媒体库首页·自定义·收藏·合集 | 首页行偏好读取（#23） | 偏好由全站 UiPrefs 上下文统一加载/同步（library-view.tsx:180-181） | `prefs.rows == nil` 才拉一次，`try?` 失败即写成 `[]` 且本进程不再重试（LibraryHomeView.swift:317-319），首页退回出厂布局；随后在合集页点「显示在首页」会以 `homePrefs.rows ?? []`（=[]）合并默认行后整体 PUT（CollectionDetailView.swift:357, 435），把用户原来的自定义行清单覆盖掉 | P2（边缘，但有覆盖用户偏好的后果） | CollectionDetailView.swift, LibraryHomeView.swift | 失败时保持 nil 并在下一轮轮询重试；toggleOnHome 在 rows 不可信时先强制重拉 /ui/preferences | 代码核对 |
| LH-8 | 4 媒体库首页·自定义·收藏·合集 | 改名失焦 trim（#27） | 输入框 onBlur 时 `name.trim()`（library-customize-view.tsx:721-727） | 只截 40 字，不 trim（LibraryCustomizeView.swift:347-361）；输入纯空格会存成「  」，草稿态行名显示为空白（Row.title 只判 isEmpty，LibraryHomeRows.swift:118-121）；重读时 build 会 trim，所以只影响当次显示与存储内容 | P2 | LibraryCustomizeView.swift, LibraryHomeRows.swift | 保存前（toPrefs 或 set 时）对 name 做 trimmingCharacters(in: .whitespacesAndNewlines) | 代码核对 |
| LH-9 | 4 媒体库首页·自定义·收藏·合集 | 系列缺片格（#60） | 「追踪中」= `part.subscribed` **或** 本地订阅表里已有该 tmdb 电影（library-collection-detail-view.tsx:942-951）；卡片自带订阅动作，已追踪时变「管理订阅」；只压暗海报图、片名不压暗（:1022-1026） | 只看 `part.subscribed`（CollectionDetailView.swift:498, 505）；订阅入口只在长按菜单、已追踪时没有「管理订阅」；整格（含片名）opacity 0.55（:501） | P2 | CollectionDetailView.swift | 追踪判定并入 App 的订阅缓存；已追踪时长按给「管理订阅」；只对海报层降透明度 | 代码核对 |
| LH-10 | 4 媒体库首页·自定义·收藏·合集 | 全部合集的视角切换（#55） | 手机端顶栏挂「首页 / 合集」分段控件（all-collections-view.tsx:40-48；library-section-switch.tsx） | 无分段控件，导航标题「全部合集」，靠返回键回首页（AllCollectionsView.swift:89） | P2 | AllCollectionsView.swift | 可接受（导航栈返回等价），如需一致可在 toolbar 放 Picker 做 replace 导航 | 代码核对 |
| LH-11 | 4 媒体库首页·自定义·收藏·合集 | 合集墙框比例（#71） | PosterWall 不传 frameAspect，按每格主图比例取 2:3 或 16:9（poster-wall.tsx:149） | 强制 Theme.posterAspect（CollectionDetailView.swift:272），横版封面变成 2:3 框内模糊铺底 | P2 | CollectionDetailView.swift, LibraryWall.swift | 传 `frameAspect: nil` 让 LibraryInventoryCell 自选（它已支持，LibraryWall.swift:264） | 代码核对 |
| LD-3 | 4 单库页·待处理 | A–Z 条交互细节 | 滑动时气泡显示字母和「N 部 / 无作品」（web LDV:2462-2471）；滚动时当前字母高亮（activeWallInitial，1247-1292）；空字母不能跳（2431-2432） | 气泡只显示字母（LDV:801-809）；没有当前字母高亮；空字母会跳到邻近有内容的字母（LDV:795,820-825） | P2 | — | 气泡里补上 `entry.count 部`/「无作品」；用 trackVisible 算出的 offset 反查 index，高亮当前档；空字母改为不跳，或保留现在的行为并注明是有意为之 | 代码核对 |
| LD-4 | 4 单库页·待处理 | 图廊 / 照片墙的「回到上次位置」 | 图廊和照片墙也记录位置、弹胶囊并能跳回（web LDV:1160-1194,2032-2041 调 jumpGalleryTo/jumpTo）；另外挂后台 30 分钟以上再回来算重新进入，会复位并重新询问（lib/library-wall-recall.ts:150-206） | 只有海报墙网格参与 `onScrollTargetVisibilityChange`（LDV:210,486）；胶囊固定调 `pager.jump`（LDV:223-229），在图廊/照片墙下跳不过去；没有长时间离开后重新进入的逻辑 | P2 | — | LibraryGalleryWall 和 PhotoWallView 各暴露首个可见条目 offset 和 jump(to:)，胶囊按当前形态分派；长离开的逻辑可以用 scenePhase 加时间戳实现 | 代码核对 |
| LD-5 | 4 单库页·待处理 | 照片瓦片的后台处理标签 | PhotoTile 底部显示 workingLabel（整库刷新阶段 / 后台任务 / 正在读取规格）（photo-wall.tsx:711-716） | PhotoTile 没有 working 参数（PhotoWallView.swift:240-293） | P2 | PhotoWallView.swift | 把 LDV 的 `workingLabel(_:)` 作为闭包传给 PhotoWallView | 代码核对 |
| LD-6 | 4 单库页·待处理 | 下载原图失败没有提示 | iOS 独立 App 下失败时显示「下载失败，请在 Safari 中打开本站后下载」（photo-lightbox.tsx downloadOriginal） | ShareLink 的 FileRepresentation 抛错后什么都不显示（PhotoLightbox.swift:177-190） | P2 | PhotoLightbox.swift | 改成按钮：先下载，失败时在灯箱的 `note` 里显示原因，成功后再弹出分享面板 | 代码核对 |
| LD-7 | 4 单库页·待处理 | 章节作业 cancelling 状态的文案 | `chapterJobRunning` 把 running 和 cancelling 都算运行中（apps/web/lib/library-manage.ts:173-175） | `job.status != "running"` 就显示「生成章节排队中」（LDV:593）；cancelling 但 stopping=false 时会显示成排队中 | P2 | — | 改成 `!["running","cancelling"].contains(job.status)` | 代码核对 |
| LD-8 | 4 单库页·待处理 | 「停止刷新 x/y」和墙上各格的刷新阶段更新慢 | 菜单计数和格子上的阶段（refreshPhaseById）读的是每 2s 更新的 progress（web LDV:890-893,1503-1507） | 菜单计数和 workingLabel 读的是库列表（LDV:520,584-586），只随 10s 墙轮询更新 | P2 | — | 和第一条 P1 一起改：统一改读 progress 状态 | 代码核对 |
| LD-9 | 4 单库页·待处理 | 非管理员在合集视图下的 ⋯ 菜单 | `hasMenuItems = canManage \|\| photoWall \|\| gallery \|\| galleryAvailable`（web LDV:1435）：普通成员在合集视图、图床偏好关闭时，整个 ⋯ 按钮不渲染，看不到「显示已隐藏的合集」 | 只要库存在就显示 ⋯（LDV:188-190）；合集视图下所有人都能看到「显示已隐藏的合集」（LDV:556-558） | P2 | — | 与 web 对齐：非管理员在合集视图下隐藏这一项，或者整个菜单按 hasMenuItems 判断 | 代码核对 |
| LD-10 | 4 单库页·待处理 | 元数据刷新面板「停止」失败的提示方式 | 失败时显示在页面的 notice 横幅（web LDV:1691-1699） | 用 toast（MetadataRefreshPanel.swift:53-60） | P2 | MetadataRefreshPanel.swift | 可以接受；如果要一致，就把错误回传给宿主，放进 `notice` | 代码核对 |
| LD-11 | 4 单库页·待处理 | 抽屉数据刷新节奏 | 抽屉直接用父页的四份清单，跟着父页轮询（忙时 3s / 10s / 30s） | 抽屉自己每 30s 拉一次（IssueDrawer.swift:59-61） | P2 | IssueDrawer.swift | 可以接受；或者让父页把当前轮询间隔传给抽屉 | 代码核对 |
| LD-12 | 4 单库页·待处理 | 带参数进入 | `?view=collections` 直接落在合集视图（web LDV:270-275；来源 library-item-detail-view.tsx:848、library-filter-bar.tsx:911）；`?pending=1` 数据到齐后自动打开抽屉（382-392,880-886） | `.library(id:)` 路由没有参数（AppRoute.swift:34，Destinations.swift:24）。筛选栏内部的「全部合集 ›」已经直接切视图（LDV:439），不受影响；条目详情跳过来会落在作品视图 | P2 | AppRoute.swift, Destinations.swift | 给 `.library` 加上可选的 `view` / `openPending` 参数，初始化 `view` 和 `issueTab` | 代码核对 |
| LI-4 | 4/11 条目详情·分享弹层 | 删除影片后去向 | `router.replace(/library/{libraryId})`（:1047-1050） | `router.pop()`（LibraryItemDetailView.swift:611）；DeleteFileSheet 删到最后一个文件也是 pop（:108） | P2 | LibraryItemDetailView.swift | 先 pop 当前条目，再在上一屏不是该库页时 push `.library(id:)`；或者直接把栈顶换成 `.library(id: libraryId)` | 代码核对 |
| LI-5 | 4/11 条目详情·分享弹层 | 合集行「还有 N 个」 | 跳 `/library/{id}?view=collections`（:846-852） | `.library(id: libraryId)`（:257），落到海报墙而不是合集视图 | P2 | — | 给 `.library` 路由加 view 参数，或跳 `.allCollections` | 代码核对 |
| LI-6 | 4/11 条目详情·分享弹层 | 章节轮询计数复位 | effect：`chapters_pending` 一变 false 就把计数清零（:433-435） | 只在 reload() 里清零（:625）；轮询自己把 pending 拉成 false 时不复位，之后再点「重新生成章节」剩下的轮数会变少 | P2 | — | 轮询回调里拿到 `!next.chaptersPending` 时也把 `chapterPolls = 0` | 代码核对 |
| LI-7 | 4/11 条目详情·分享弹层 | 演职员首字占位 | initialsOf：中日韩取首字，拉丁名取首尾单词首字母（大写）（cast-row.tsx） | `String(person.name.prefix(1))`（:459） | P2 | — | 移植 initialsOf | 代码核对 |
| LI-8 | 4/11 条目详情·分享弹层 | 标记接口 device_id | 带 `device_id: getPlayerDeviceId()`（playback.ts:939） | `deviceId: nil`（LibraryShared.swift:180-185） | P2 | LibraryShared.swift | 传 App 播放器用的设备 id，让活动页和 Jellyfin 的设备归属一致 | 代码核对 |
| LI-9 | 4/11 条目详情·分享弹层 | 失败态返回按钮文案 | 「返回发现详情 / 媒体库 / {库名} / 库存」（:451-455, 483） | 「返回{库名 ?? 库存}」（:69） | P2 | — | 平台返回栈可以接受，也可按来路补文案 | 代码核对 |
| P-5 | 5 播放器 | B-5 #1 条目信息失败 | 整页显示错误原因 +「返回」（player-page.tsx:180-195） | `infoError` 写了但从不显示（PlaybackController.swift:51, 180） | P2 | PlaybackController.swift | 在 PlayerContent 里 `infoError != nil && session == nil` 时复用 PlayerErrorView | 代码核对 |
| P-6 | 5 播放器 | B-6 #41 诊断面板字段 | 执行区有「Worker 版本 · ffmpeg 版本」「平台：…」；传输区有「上次跳转 x 秒 · 卡顿 N 次 · 累计 x 秒」；供片区有「历史失败 segXXXXX …」「最近上传 …」（diagnostics-panel.tsx:366-376, 452-470, 515-532）；每 2s 轮询 | 这些行都没有（PlayerDiagnosticsPanel.swift:87-167），尽管模型里有 workerVersion/ffmpegVersion/workerPlatform/workerArch/historicalFailedSegments/recentUploads（Models.swift:5763-5787）；每 1s 轮询 | P2 | Models.swift, PlayerDiagnosticsPanel.swift | 按 Web 补上这些行；QoE 行用 controller.qoe 的 rebufferCount/rebufferMs；轮询间隔两边统一（清单写的 1s，Web 源码是 2s，请主审计员定口径） | 代码核对 |
| P-7 | 5 播放器 | B-7 #43 暂停时的控制层 | 暂停、菜单打开、拖动、等待用户操作时控制层钉住，点击也收不起来（chrome.ts `chromeMustStayVisible`；video-player.tsx:3142-3151） | 暂停时不会自动隐藏，但单击仍能收起（PlayerScreen.swift:372-377） | P2 | PlayerScreen.swift | handleTap 里 `controller.paused && !isBusy` 时不执行 toggle | 代码核对 |
| P-8 | 5 播放器 | B-8 #23 菜单打开时的中央三键 | 菜单打开时中央三键照常显示（video-player.tsx:3331） | `menu == .none` 才显示（PlayerScreen.swift:273） | P2 | PlayerScreen.swift | 去掉 `menu == .none` 条件，或者确认这是有意的（菜单会压住三键） | 代码核对 |
| P-9 | 5 播放器 | B-9 #33 拖动时画面跟随 | 直出/VOD 这类 seek 便宜的场景，拖动中画面跟着跳（lib/player/scrub-follow.ts） | 只在松手时 seek（PlayerControls.swift:240-252） | P2 | PlayerControls.swift | MPV 直出或 timeline=file 时，onChanged 里节流做 `engine.seek(exact:false)` | 代码核对 |
| P-10 | 5 播放器 | B-10 #52 暂停大字 | 高度 ≤480 的横屏里隐藏片名大字（video-player.tsx:3714） | 横屏也显示（PlayerScreen.swift:240-244） | P2 | PlayerScreen.swift | `if showPaused, menu == .none, !landscape` | 代码核对 |
| P-11 | 5 播放器 | B-11 #47 暂停时长按 | 暂停时不启动长按计时，松手当成一次轻点（hold-speed.ts `canHoldSpeed`） | 手势层 500ms 后判为 hold（PlayerGestureLayer.swift:61-67），控制器拒绝加速（PlaybackController.swift:711-715），这次触摸没有任何效果 | P2 | PlaybackController.swift, PlayerGestureLayer.swift | 在 GestureView 里通过 config 读能不能加速；不能的话，松手时按 tap 处理 | 代码核对 |
| P-12 | 5 播放器 | B-12 #9 卡顿口径 | seek 造成的等待不算卡顿（qoe.ts） | 在播时每次 `.buffering` 都算卡顿，seek 也算（PlaybackController.swift:568-572） | P2 | PlaybackController.swift | seek 后到下一次 `.playing` 之间的缓冲不计入 | 代码核对 |
| P-13 | 5 播放器 | B-13 #45/#46 手势灵敏度和排除带 | 亮度/音量按屏高的 60% 满量程；排除上 12%、下 24%、左右各 32px（touch-adjust.ts:30-43） | 满屏高为 100%；只排除上下各 32pt（PlayerGestureLayer.swift:49, 56；PlayerScreen.swift:420） | P2 | PlayerGestureLayer.swift, PlayerScreen.swift | 对齐常量 | 代码核对 |
| P-14 | 5 播放器 | B-14 潜在问题 | Web 不发 file_id | `sessionBody` 对同一 mediaItemId 的所有单元（包括下一集）都带 `request.fileId`（PlaybackController.swift:317）。现在没有调用方设置 fileId，所以还没触发；一旦「播放器内切换版本」接入，切到下一集会继续请求上一集的文件 | P2（潜在） | PlaybackController.swift | 只在 `unit == 初始单元` 时带 fileId | 代码核对 |
| P-15 | 5 播放器 | B-15 潜在问题 | timeline=session 时，在已转码区间内 seek 走原生 seek，只有越界才换会话（timeline.ts:86 planSeek） | timeline=session 下任何 seek 都重开会话（PlaybackController.swift:698-703）。服务端现在 VOD 基本都是 file（routes/playback.py:1175），影响面小 | P2 | PlaybackController.swift | 用 engine.bufferedEnd / 可 seek 范围判断越界后再重开 | 代码核对 |
| SR-2 | 6 搜索 | 70 老快照解码 | `TorrentAttrs` 的 `subtitle_carriers?`/`audio_languages?`/`platforms?` 是可选字段（lib/api/search.ts:57-63）。老快照缺这些字段也能正常渲染 | Models.swift:8776,8789-8791 里 `titleCandidates`/`subtitleCarriers`/`audioLanguages`/`platforms` 是非可选数组，TorrentHit 的 `uploader: String` 也是非可选。快照走 `value.decode(as:)`（TorrentSearchAPI.swift:121），只要有一条缺字段，整个快照就解码失败，页面显示「搜索出错」<br>**运行期核验**：主审计员扫描了 NAS 上全部带快照的资源搜索历史，所有 hits 的 uploader 与 attrs 四个数组字段都在，当前数据不会解码失败；仍建议把这几个字段改为缺省空值以防老数据。 | P2（健壮性；NAS 现有快照未复现） | Models.swift, TorrentSearchAPI.swift | 先用老的种子搜索历史（has_snapshot=true）取 /search/history/{id}/results 核对字段。如果后端返回的是原始存档 JSON，就把这几个字段改成 `decodeIfPresent ?? []`（`uploader` 缺省为 ""） | NAS 全部种子快照 GET /search/history/{id}/results 扫描：无缺字段 |
| SR-3 | 6 搜索 | 17 相对时间 | formatRelativeTime：「11 天前」 | Formatters.relative：「1周前」（运行期已见）<br>**运行期核验**：这是全 App 共性：凡用 Formatters.relative 的地方（搜索历史、成员最近活动、会话列表等）都与网页口径不同，建议统一修 Formatters.relative。 | P2 | — | 让 Formatters.relative 按网页的天数分档输出，历史、快照药丸、行时间、确认条都会受益 | /tmp/audit/ui/search_palette.png 对照 /tmp/audit/webui/search_palette.png；另见成员页 /tmp/audit/app/settings_members.png「1周前」对网页「11 天前」 |
| SR-4 | 6 搜索 | 11 影视模式说明 | 「在豆瓣中搜索影视条目」（search-command.tsx:534） | 「在豆瓣与 TMDB 中搜索影视条目」（SearchHomeView.swift:108） | P2 | SearchHomeView.swift | 建议改网页文案（App 与实际双来源一致）；也可以把 App 改回网页原文 | /tmp/audit/ui/search_palette.png 对照 /tmp/audit/webui/search_palette.png |
| SR-5 | 6 搜索 | 26 切垂直时的快照 | 切走会去掉 snapshot，切回来实时搜索（page.tsx:76-82） | 快照保留（SearchResultsView.swift:136-140） | P2 | SearchResultsView.swift | 要严格对齐的话，switchTo 时把另一垂直的 snapshot 置 nil 并重建 | 代码核对 |
| SR-6 | 6 搜索 | 27 抢种模式下切范围 | 丢掉 for_sub、退出抢种（page.tsx:93-95） | 保留 grabTarget | P2 | — | 建议保留 App 行为，并把网页 switchScope 改为带上 for_sub | 代码核对 |
| SR-7 | 6 搜索 | 42 按发布时间升序 | 无时间的条目排最前 | 始终垫底（TorrentSearchLogic.swift:176-180） | P2 | TorrentSearchLogic.swift | 以 App 为准，修网页（让 -Infinity 在升序也垫底） | 代码核对 |
| SR-8 | 6 搜索 | 44/45 手机工具栏 | 手机只显示 2 个分辨率 chip，年份/季/压制组下拉隐藏（search-results.tsx:1543,1560） | 显示 3 个 chip 和三个下拉 | P2 | — | 可以接受（App 更强）；如果要求严格一致，就按网页裁掉 | 代码核对 |
| SR-9 | 6 搜索 | 53 行内指标 | 手机上隐藏指标列（search-results.tsx:3188）；做种数 ≥100 时加粗绿色 | 常显指标行；没有 ≥100 档（TorrentResultsView.swift:673-677） | P2 | TorrentResultsView.swift | 保留指标行，补上 ≥100 加粗档 | 代码核对 |
| SR-10 | 6 搜索 | 54 操作面板「浏览图片」 | 只有图览卡片的抽屉有 | 有图的行都有 | P2 | — | 可以接受 | 代码核对 |
| SR-11 | 6 搜索 | 65 提交 toast | 弹窗提交后没有 toast | 有 toast | P2 | — | 可以接受 | 代码核对 |
| SR-12 | 6 搜索 | 37 不定进度 | 扫光动画（progress-sweep） | 静止的 40% 条（TorrentResultsView.swift:348） | P2 | TorrentResultsView.swift | 站点数未知时加扫光动画 | 代码核对 |
| SR-13 | 6 搜索 | 28 无权限态 | 只有一行文案 | 多一个标题「无法搜索」和图标 | P2 | — | 去掉 title，或保留 | 代码核对 |
| SR-14 | 6 搜索 | 60 确认条高度 | 路径 break-all 自动换行 | `.presentationDetents([.height(250)])` | P2 | — | 改成自适应高度（fitted 或 `.medium`） | 代码核对 |
| SR-15 | 6 搜索 | 54 无链接的行 | 没有 detail_url/download_url 时整行不可点 | 无条件打开空面板 | P2 | — | 两个链接都没有时不打开面板 | 代码核对 |
| SB-2 | 7 订阅 | #21 路由预览在选定候选后可能缺失 | 预览随 `prepared.media` 与 libraryId 两者变化重拉（subscribe-dialog.tsx:103-124 依赖含 prepared?.media） | `.task(id: libraryId)`（SubscribeSheet.swift:96）只盯 libraryId。豆瓣歧义 → 选定候选后，如果算出的库 id 和第一次相同，task 不会重跑，「将直接下载到库内目录…」这行就不出现 | P2 | SubscribeSheet.swift | task id 改成 `"\(libraryId ?? -1)-\(prepared?.media?.tmdbId ?? -1)"`，或在 runPrepare 末尾显式调用 refreshDispatchPreview | 代码核对 |
| SB-3 | 7 订阅 | #13 错误态 | 只显示「订阅列表加载失败」+ 重试（subscriptions-view.tsx:286-297）；每次 refresh 失败都进错误态 | ErrorState 多一句 message「请检查网络后重试」；已有旧快照时不进错误态（SubscriptionsView.swift:108,203） | P2 | SubscriptionsView.swift | 去掉额外 message，保持文案一致（保留旧快照属于改进，可以接受） | 代码核对 |
| SB-4 | 7 订阅 | #24 管理态按钮顺序 | 右对齐：好的 →（洗版时）去洗一轮版 → 取消订阅（subscribe-dialog.tsx:448-477） | 竖排：去洗一轮版（主按钮）→ 好的 → 取消订阅（SubscribeSheet.swift:187-215） | P2 | SubscribeSheet.swift | 可以接受（手机竖排主按钮置顶），或者调成与网页同序 | 代码核对 |
| SB-5 | 7 订阅 | #30 活跃下载首拉延迟 | `useVisiblePolling(..., hasInFlight ? 5000 : null, { leading: true })`，在途一出现就立刻拉（subscription-inspector-view.tsx:173-186） | `.polling(every: 5, immediately: true)` 在出现时就跑，那时详情还没加载，hasInFlight 为假直接返回，真正第一次拉要等 5s（SubscriptionDetailView.swift:101,133-134） | P2 | SubscriptionDetailView.swift | reload() 成功且 hasInFlight 为真时顺手调用一次 refreshDownloads() | 代码核对 |
| SB-6 | 7 订阅 | #38/#45 确认框取消键 | 立即搜索、暂停/恢复的 cancelLabel 都是「返回」（subscription-inspector-view.tsx:273,314） | 全局 Feedback.confirm 的取消键写死「取消」（DesignSystem/Feedback.swift:75,119） | P2 | Feedback.swift | 给 Feedback.confirm 加 cancelTitle 参数，按网页传「返回」 | 代码核对 |
| SB-7 | 7 订阅 | #46 成员取消订阅确认 | 取消键「先不」、确认「取消订阅」（subscription-inspector-view.tsx:343-349） | 取消键「取消」+ 确认「取消订阅」（SubscriptionDetailView.swift:445-450），两个按钮都以「取消」开头，容易看混 | P2 | SubscriptionDetailView.swift | 同上，传 cancelTitle「先不」 | 代码核对 |
| SB-8 | 7 订阅 | #41 调整订阅路由预览文案 | 调整弹窗 watch 模式只有「将投递到自动入库的监听目录 X，下载完成后自动整理入库」，不带 staging；库内模式不去尾斜杠（subscription-adjust-dialog.tsx:~270-277） | 复用 DispatchPreviewNote，watch 模式有 staging 时多「下载完成后整理到 Y，文件进入媒体库根目录后自动入账」、并去尾斜杠（SubscriptionDialogs.swift:76-88） | P2 | SubscriptionDialogs.swift | 可以接受（App 信息更全，与网页订阅弹层同口径）；要严格一致就给调整弹层单独传一个不带 staging 的变体 | 代码核对 |
| SB-9 | 7 订阅 | #42 洗一轮版报告态关闭 | 报告态下 Modal 任意关闭方式都走 onFinished，父页会刷新（upgrade-run-dialog.tsx:123） | 只有「完成」按钮走 finish()；在报告态下滑关闭 sheet 时不调用 onFinished，详情页要等下一次 30s 轮询或手动下拉才刷新（UpgradeRunSheet.swift:40,54,60,71-74） | P2 | UpgradeRunSheet.swift | 报告态加 `.interactiveDismissDisabled(true)`，或在父页 `.sheet(onDismiss:)` 里对 upgradeRun 统一 reload | 代码核对 |
| SB-10 | 7 订阅 | #9 规则组→库 流向展示 | 手机端首点展开信息层（季范围 + 流向），再点进详情（poster-card.tsx:202-204,235-242） | 季范围与流向常显在海报下方，单击直接进详情；另有长按菜单（SubscriptionsView.swift:292-319） | P2 / 平台差异 | SubscriptionsView.swift | 建议接受 | 代码核对 |
| A-2 | 8 活动 | 会话卡规格行 | 手机端隐藏规格串（media-activity-section.tsx:425-428，`max-md:hidden`，注释说明「窄屏会折成孤字行，移动端交给详情页」） | 始终显示「分辨率 · 编码 · HDR · 码率 · 体积」（WatchPanel.swift:386-388） | P2 | WatchPanel.swift | 想对齐网页手机端就去掉这一行；想保留就在验收记录里标为「App 多显示的信息」 | 代码核对 |
| A-3 | 8 活动 | 写操作后刷新下载快照 | `refresh()` 带排队：请求进行中再调用会记一次 `queued`，结束后再拉一次（lib/download-tasks.tsx refresh 中的 `queued.current` 循环） | `refreshDownloads()` 在请求进行中时直接 return（ActivityStores.swift:89-91）。删除或换种后如果正好撞上一次 10 秒轮询，刚删的任务最多会再显示 10 秒 | P2 | ActivityStores.swift | 仿照 `refreshJobs` 的 `jobsQueued` 改成排队串行（ActivityStores.swift:62-86 已有现成写法） | 代码核对 |
| A-4 | 8 活动 | view 缺省或非法时的落点 | view 缺省或非法 → 观看·正在播放（lib/task-center.ts `activityScopeFromQuery`/`watchViewFromQuery`）；徽标在无事可做时的 href 就是 `/activity` | `Router.rememberRootParameter` 只在 view 非空时下发参数（App/Routing/Router.swift:157）；`apply(view:)` 遇到非法值什么都不做（ActivityView.swift:35-43）。已经停在「任务」时点一个 `/activity` 链接，不会回到观看 | P2 | ActivityView.swift, Router.swift | Router 在 `.activity(nil)` 时也下发参数（例如空串），`apply` 遇到未知值时回落到 `.media/.playing` | 代码核对 |
| A-5 | 8 活动 | 忽略任务失败时的提示 | 在卡片内显示 `actionError`：「忽略失败，请稍后重试」（components/job-center.tsx:493），弹窗保持打开 | 弹一个 toast（JobCard.swift:333），不写卡片内的错误文字 | P2 | JobCard.swift | 改为设置 `actionError`，与卡片上其他动作的错误展示方式一致 | 代码核对 |
| A-6 | 8 活动 | 任务视角的加载态 | 只看下载快照的 `loading`；下载快照回来且没有内容就显示空态（task-center-view.tsx `visibleCount === 0 && !loading`） | 条件是 `downloadsLoading \|\| !jobsLoaded`（TaskCenterPanel.swift:58）。`/jobs` 持续失败时 `jobsLoaded` 一直是 false，页面会一直显示「正在汇总任务…」，也没有错误提示 | P2（边缘情况） | TaskCenterPanel.swift | Job 首次加载失败也要把 `jobsLoaded` 置 true（或记录错误），与网页一样落到空态 | 代码核对 |
| A-7 | 8 活动 | 刷新时刻行 | 手机端隐藏（task-center-view.tsx:268） | 选项卡下多一行「● 实时 / 定时刷新 · 几秒前更新」（TaskCenterPanel.swift:104-131） | P2 | TaskCenterPanel.swift | 两种做法都可以：保留则记为 App 增强；要求严格一致则移除 | 代码核对 |
| AI-2 | 9 AI 会话 | 6 上传文件名 | 保留原文件名，压缩后改扩展名为 .jpg（agent-attachments.ts:48-49），托盘 chip 与气泡替代文本显示原名 | 恒为「图片.jpg / 图片.png / …」（AgentComposer.swift:377-398） | P2 | AgentComposer.swift | 用 PhotosPickerItem 的 `suggestedName`（或 itemIdentifier 对应资源名）作文件名，缺失时再兜底「图片」 | 代码核对 |
| AI-3 | 9 AI 会话 | 4 回车行为 | 回车发送、Shift+回车换行（composer-editor.tsx:197-213） | TextField(axis: .vertical) 回车换行，只能点发送键（AgentComposer.swift:92） | P2 | AgentComposer.swift | 可接受为平台差异（见 C）；若要一致，给 TextField 加 `.submitLabel(.send)` + `onSubmit` 且硬件键盘 Shift+Return 换行 | 代码核对 |
| AI-4 | 9 AI 会话 | 10 停止失败提示 | 静默，只 console.warn（agent-conversations.tsx:908-914） | toast「停止失败，请稍后重试：…」（AgentConversationView.swift:263） | P2 | AgentConversationView.swift | App 更好，建议保留，或反向补到 Web | 代码核对 |
| AI-5 | 9 AI 会话 | 17 重进运行中会话的计时 | 末轮保留转录里用户消息时间戳，页脚显示真实已运行时长（agent-conversations.tsx:342-361） | 重置 `turns[i].startedAt = Date.now`（AgentConversationStore.swift:76），从 0s 重新计 | P2 | AgentConversationStore.swift | 删掉 :76 这一行（AgentTimeline.turns 已按消息时间戳填 startedAt） | 代码核对 |
| AI-6 | 9 AI 会话 | 20 正文代码块着色 | react-markdown 默认 `<pre>`，不着色（markdown.tsx:24-31,62-79）；Shiki 只用于工具参数 | 围栏 bash/json 块按 github-dark 着色（AgentMarkdown.swift:474-486,550-584） | P2 | AgentMarkdown.swift | 二选一对齐：App 正文代码块传 `.plain`，或 Web 正文也接高亮 | 代码核对 |
| AI-7 | 9 AI 会话 | 20 代码块复制键 | 触屏上点按代码块才浮现（REVEAL_CLASS + useTapReveal，markdown.tsx:52-60） | 右上角常显（AgentMarkdown.swift:490-507） | P2 | AgentMarkdown.swift | 可接受；App 注释里写了「常驻复制键」是有意为之，需产品确认 | 代码核对 |
| AI-8 | 9 AI 会话 | 20 Markdown 图片 | react-markdown 渲染 `<img>` | 行内解析用 `inlineOnlyPreservingWhitespace`，`![]()` 不出图（AgentMarkdown.swift:294-311） | P2 | AgentMarkdown.swift | 块级解析识别单独成行的 `![alt](url)`，用 RemoteImage 渲染 | 代码核对 |
| SA-5 | 10 设置（上） | #55 拥堵提示「去调整」 | 跳 `?limits=<id>`，自动打开那台下载器的「限速与队列」弹窗（site-config-section.tsx:1076-1082；downloader-config-section.tsx:113-120,293-296） | SitesSettingsView.swift:186 只 push `.settingsSection(.downloaders)` | P2 | SitesSettingsView.swift | 与 #7 一起给路由加参数（limitsDownloaderId），落地后设置 `limitsTarget` | 代码核对 |
| SA-6 | 10 设置（上） | #4 成员越权访问管理分区 | 替换到 /settings/profile（settings-view.tsx:154-165） | 原地显示「无权访问」（SettingsSectionView.swift:11-15） | P2 | SettingsSectionView.swift | 可以接受；如果要一致，就在 Router 解析 webPath 时把成员落到 `.settingsSection(.profile)` | 代码核对 |
| SA-7 | 10 设置（上） | #6 / #7 媒体库去向 | 「去创建」「去媒体库」都跳 `/library` | `.libraryManage(create: true)` 和 `.libraryManage()`（OverviewSettingsView.swift:165,182） | P2 | OverviewSettingsView.swift | 可以接受（App 更直接）；要严格对齐就改成 `.libraryHome` | 代码核对 |
| SA-8 | 10 设置（上） | #1 分区描述位置 | 列表只有名称；分区页顶部有「图标 + 名称 + 描述」头（银玻璃手机端可见） | 列表每行多一行描述；分区页没有描述头 | P2 | — | 可以接受；要对齐就把描述移到 SettingsSectionView 顶部 | 代码核对 |
| SA-9 | 10 设置（上） | #18 质感页状态文案 | 「调节实时预览中，保存后对所有设备生效」 | 「有未保存的调整，保存后对所有设备生效」+ 额外脚注（AppearanceSettingsView.swift:369,388） | P2 | AppearanceSettingsView.swift | App 确实没有预览，现在的文案更诚实；保持即可 | 代码核对 |
| SA-10 | 10 设置（上） | #21 最近活动前缀 | 只显示相对时间 | 「最近活动 X」（MembersSettingsView.swift:216） | P2 | MembersSettingsView.swift | 可去掉前缀 | 代码核对 |
| SA-11 | 10 设置（上） | #47/#62 数字输入越界崩溃（运行期风险） | JS Number 不会溢出 | SettingsBSiteSheets.swift:360-362 的 `Int(gibRaw.rounded())`、`Int(daysRaw.rounded())`，:388 的 `gib * SettingsBSiteFormat.gib`；SettingsBDlLimitsSheet.swift:130-131 的 `Int(value.rounded())`、`kib * Self.kib`：粘贴 20 位以上的数字（numberPad 也能粘贴）会让 Int 转换或乘法溢出，**直接闪退** | P2 | SettingsBDlLimitsSheet.swift, SettingsBSiteSheets.swift | 先判断 `value < Double(Int.max / 1024^3)` 再转换，或用 `Int(exactly:)` / `multipliedReportingOverflow`，越界时显示已有的校验错误文案 | 代码核对 |
| SBB-4 | 10 设置（下） | 播放页策略错误的显示位置 | 两张卡各自显示错误（trickplay-toggle-section.tsx:60；transcode-cache-toggle-section.tsx:59） | 只在「进度条预览」节显示；「转码缓存」加载失败时整节空白（PlaybackSettingsView.swift:73,89-104） | P2 | PlaybackSettingsView.swift | 两节都显示 `policyError`，或按字段拆成两个错误状态 | 代码核对 |
| SBB-5 | 10 设置（下） | 远程转码 Worker 摘要里的空字段 | `filter(Boolean)` 滤掉空字符串（RTS:307-316） | `compactMap` 只滤 nil，会出现「ffmpeg 」（PlaybackSettingsView.swift:361-366） | P2 | PlaybackSettingsView.swift | 先把空字符串转成 nil | 代码核对 |
| SBB-6 | 10 设置（下） | 推送绑定的输入去空白 | `trim()` 会去掉换行 | `.whitespaces` 不去换行（SettingsBPushBindSheet.swift:179,231,261,293,299,319,366） | P2 | SettingsBPushBindSheet.swift | 改为 `.whitespacesAndNewlines` | 代码核对 |
| SBB-7 | 10 设置（下） | 改了模型供应商或 AI 设定后，对话框的模型清单 | 立即失效（llm-config-section.tsx:103-107；ai-settings-section.tsx:81） | `AgentCatalog` 缓存 60 秒且没有失效入口（Features/Agent/AgentSkills.swift:76-86） | P2 | AgentSkills.swift | 加 `AgentCatalog.invalidateModels()`，在 LLM 增删改和 AI 设定保存成功后调用 | 代码核对 |
| SBB-8 | 10 设置（下） | 更新或重启等待超时后点「刷新页面」 | 整页刷新（app-update-section.tsx:362） | 调 `finishRestart`，无论结果如何都提示「应用已恢复」（AppUpdatePanel.swift:177-185,209） | P2 | AppUpdatePanel.swift | 超时时只重拉，不弹成功提示；或者先确认 /health 成功再提示 | 代码核对 |
| SBB-9 | 10 设置（下） | 离开更新页后的 /health 探测 | 有 unmounted 守卫 | rollback、restart、restarting 恢复用的 `Task {}` 没保存引用，离开页面后最长还会继续探测约 3 分钟（AppUpdatePanel.swift:74,78,99） | P2 | AppUpdatePanel.swift | 存进一个可取消的 @State Task，`onDisappear` 时取消 | 代码核对 |
| SBB-10 | 10 设置（下） | 页签蓝点、changelog、回退说明位置、「档位可控」短标、配对第 3 步文案、远程转码介绍位置、「正在获取 Worker 状态…」、网络页空地址提示和无代理提示的位置、网络加载文案、日志回前台补刷、占位符插入位置 | 见 A 表各行 | 见 A 表各行 | P2 | — | 按 A 表备注逐项处理，或接受 | 代码核对 |
| SBB-11 | 10 设置（下） | 设置分区内的查询参数深链（im-push/app 的 `?tab=`，mcp 的 `?endpoint=&tab=`） | useTabParam 读写地址栏 | `AppRoute(webPath:)` 在 settings 分支只保留 `app?tab=remote`，其它查询参数一律丢弃（AppRoute.swift:253-262） | P2 | AppRoute.swift | 站内和服务端代码都没有生成这类链接（grep 只找到 `/settings/downloaders?limits=`），可以接受；要对齐就给 `.settingsSection` 加上 tab 参数 | 代码核对 |
| R-3 | 运行期新增（跨节） | 相对时间口径全局不一致 | 网页 formatRelativeTime / dayjs：「11 天前」「13 天前」「1 小时前」（向下取整） | `Formatters.relative`（系统 RelativeDateTimeFormatter）出「1周前」「2周前」；LibraryShared.swift:126 libraryFromNow 按四舍五入（1h47m →「2 小时前」，网页「1 小时前」）；设备页又是「25 天前」——App 内部三套口径 | P2 | LibraryShared.swift | 统一一个与网页 formatRelativeTime 同算法的格式器，全 App 替换 | /tmp/audit/app/library.png「2 小时前」对 /tmp/audit/web/library.png；/tmp/audit/ui/search_palette.png |
| R-4 | 运行期新增（跨节） | 数字千分位 | 「10481 个文件」 | 「10,481 个文件」（LibraryDetailView 库头统计） | P2 | — | 统计数字不加分组分隔符，或与网页统一 | /tmp/audit/app/library_20.png 对照 /tmp/audit/web/library_20.png |
| R-5 | 运行期新增（跨节） | 单库筛选面板可读性 | 筛选 sheet 为不透明深色底（/tmp/audit/webui/library_filter.png） | 筛选 sheet 为透明液态玻璃，底下海报透出，筛选项文字对比度差（/tmp/audit/ui/library_filter.png） | P2 | — | sheet 加 `.presentationBackground(Theme.surface)` 或加厚材质 | /tmp/audit/ui/library_filter.png 对照 /tmp/audit/webui/library_filter.png |
| R-6 | 运行期新增（跨节） | 条目详情 Hero 被导航栏截断 | 手机 Hero 全出血到状态栏，返回/⋯ 浮在图上 | 顶部是带标题「交锋」的不透明导航栏，Hero 从栏下开始（/tmp/audit/app/library_20_item_6269.png） | P2 | — | 导航栏 `.toolbarBackground(.hidden)` + Hero `.ignoresSafeArea(edges: .top)`，滚动后再显示标题 | /tmp/audit/app/library_20_item_6269.png 对照 /tmp/audit/web/library_20_item_6269.png |
| R-7 | 运行期新增（跨节） | /my 页双关闭入口 | /my 页只有页面本身（壳层顶栏头像） | 同时出现左上「返回」和右上「完成」（/tmp/audit/app/my.png） | P2 | — | 以路由压栈打开时去掉「完成」，以 sheet 打开时去掉返回 | /tmp/audit/app/my.png 对照 /tmp/audit/web/my.png |
| R-8 | 运行期新增（跨节） | 播放器画面渲染残缺（模拟器） | —— | MPV 引擎在模拟器上两次截图画面只渲染出部分区域、有斜向撕裂（/tmp/audit/ui/player_center_2.png） | P2（待真机确认） | MPVRenderViews.swift | 真机复测；若复现检查 MPVCore/MPVRenderViews.swift 的 GL 帧提交 | /tmp/audit/ui/player_center_2.png |


## 3. 平台造成、建议接受的差异

以下各节审计员判为平台差异（iOS 原生控件或系统能力替代网页实现，信息与操作等价）。主审计员运行期另补：

- 标签角标只能用系统红底，不能按网页的红/绿/蓝三色区分；App 用「数字 / 在看 / 进行中」文字区分（S 表 0.11 有改进建议）。
- 在 /my、/settings 页，iOS 标签栏必须有一个选中项，App 停在「发现」；网页这时不高亮任何标签。
- iOS 自动在中文与数字/拉丁字母之间加间距：数据是「玩具总动员2」，App 显示成「玩具总动员 2」（/tmp/audit/app/library_19_c_287.png）。
- 片单全列表的「搜索已加载片名」，App 用 `.searchable` 实现，下拉才出现，网页是常驻输入框。
- 搜索用底栏独立的「搜索」标签页（iOS 26 search role），代替网页的浮层面板；内容等价（模式三段、最近搜索、快照徽标、删除/清空），见 /tmp/audit/ui/search_palette.png 与 /tmp/audit/webui/search_palette.png。

### 0/1/12/13 外壳·认证·通用·实时

- 0.8 iOS 26 原生标签栏带文字标签，网页只有图标。
- 0.10 搜索是 `Tab(role: .search)` 标签页，不是网页的 SearchCommand 浮层面板。
- 0.11 标签角标只能是系统红底文字，三色圆点用「数字 / 在看 / 进行中」区分（建议见 B）。
- 0.13 拖动切页签、按压辉光由系统标签栏提供。
- 0.4 / 0.7 顶栏标题、返回、详情页接管都用原生导航栏。
- 12.2 待处理事项用 push 页代替 modal。
- 12.7 滚动恢复靠 NavigationStack / TabView 保留视图状态，不需要 sessionStorage 方案。
- 12.9 骨架屏换成 ProgressView；12.10 网络错误按 URLError 细分，文案比网页兜底文案更具体。
- 1.3 / 1.11 「整页刷新」用 `.id(session.username)` 重建界面树实现，效果等价（不会串上一个账号的数据）。
- 1.5 AuthGate 首帧快照缓存（lib/session-snapshot.ts）App 不需要：冷启动有 `.launching` 态。

### 2/3 发现·详情·影人

1. 电影 / 剧集分段放在导航栏中间（DV:124-132），网页放在液态玻璃底栏附件位（dv:309-322）。App 不改 MainTabView，代码注释已说明原因。
2. 数据源用 Menu 选择器（DV:134-144），网页是两段胶囊（dv:443-476）。功能相同，只是样式不同。
3. 筛选状态不写网址（没有地址栏，没有「分享链接」的场景）。但切源和切类型时清空筛选的行为仍应对齐（B-1、B-7）。
4. 预告片用 SFSafariViewController 打开 watch_url，不在应用内内嵌 iframe（B-13）。
5. 灯箱「设为背景」只把图片上传到账号背景图库（`POST /appearance/backdrops` 成功）。App 自己的 `appBackground()` 是固定底色（DesignSystem/Theme.swift:40-52），不渲染用户背景，所以在 App 里看不到效果，要去网页才能看到。属于壳层 / 外观范围，建议由负责第 0、10 节的审计员确认是否登记。
6. 豆瓣外链：网页手机把链接换成豆瓣 App 直跳地址（md:636 `doubanAppHref`）；App 用 `openURL` 打开 https 词条页，装了豆瓣 App 时 iOS 通用链接一般也能拉起。
7. 影人页、片单页、详情页的加载 / 失败态，App 多给了「重试」按钮（PD:123，DPV:22-24，DC:82，MD:42）。这是超出网页的增强，不算差异。

---

### 4 媒体库首页·自定义·收藏·合集

- 首页/收藏行/库行海报卡的附加信息（入库时间、本批季集范围、收藏层级）：网页「悬停 / 触屏首点展开信息层」，App 改为长按 contextMenu（LibraryHomeView.swift:288-290）。
- 自定义页换位：网页指针拖拽 + Alt+↑/↓，App 为 List 编辑模式的系统拖动把手（onMove），没有键盘换位。
- 自定义页「恢复默认」：网页在标题行右端，App 在导航栏右上；确认框由 window.confirm 变原生确认弹窗，文案相同。
- 全部合集页没有「首页/合集」分段控件，用导航栈返回（B-10）。
- 继续观看卡片 `from=recent`：只决定网页详情页的「返回」兜底目标，App 由导航栈天然返回首页。
- 自定义页离开时 App 会立即保存防抖窗口里的改动（网页卸载即丢弃），属于 App 更稳的差异。

### 4 单库页·待处理

- 生成章节的确认：web 是 confirm 对话框加「已有的章节也重新生成」复选框；App 是三按钮 alert（开始生成 / 已有的章节也重新生成 / 取消），语义等价（LDV:155-165）。
- 待识别状态徽标：web 悬停看完整原因，App 点击弹出 popover（IssueDrawer.swift:466-486）。
- IssueDrawer：web 是右侧全高抽屉 + 胶囊页签 + 输入框；App 是 sheet + 分段控件 + `.searchable`；批量按钮放在导航栏右上。
- 筛选面板：web 手机端是自己画的 70dvh 底部抽屉；App 是原生 sheet（0.7 / large 两档），背景可交互。
- 下载原图用 ShareLink / 系统分享面板（web 在 iOS 独立 App 下也走 navigator.share）。
- App 额外有的：下拉刷新（LDV:233）、海报格长按菜单（库存概况 / 评分 / 订阅动作，LibraryWall.swift:277-290；web 约定没有长按菜单）、照片墙「按月份跳转」菜单（手机 web 没有）、排序菜单里单独一项「方向：…」。都是在 web 之外加的，不影响对等，但需要主审计员确认是否保留。
- 灯箱：翻页/缩放用 UIScrollView + TabView，手势行为与 web 描述一致。

### 4/11 条目详情·分享弹层

1. `returnTo` / `from=recent` 查询参数：网页靠它决定左上角返回到哪里；App 用 NavigationStack 返回栈，自然回到来路。
2. 分享相对链接的补全基准：网页用浏览器 origin，App 用当前连接的服务器 origin，提示文案也相应改了（LibraryShareSheet.swift:184）。
3. 版本选择、季选择、语言选择、参考字幕：网页原生 `<select>`，App 用 Menu + Picker。
4. 轨道列表：网页手机是 Modal 抽屉，App 是 `.sheet` + detents；点芯片打开，不做网页那种「再点同一枚收起」。
5. 播放入口：网页 push `/play/...`，App `router.play(PlayRequest)` 全屏播放器。
6. 桌面专属的 hover 播放键、Tooltip、Netflix 主题滚动退场，App 都没有，符合只对齐手机端。
7. 清除观看记录后 App 还会刷新续播点与分集（网页不刷新），属于合理增强。

### 5 播放器

1. 引擎：hls.js/原生 HLS → AVPlayer + libmpv（ios-app.md §4）；App 多一个「播放引擎」菜单，档 4 失败的建议改成「换 MPV」。
2. 没有 sendBeacon/keepalive：退出时用 Task 补发 stop 和 DELETE，进程被杀时靠服务端超时回收。
3. 没有全屏按钮（App 本身是全屏）；横屏用 requestGeometryUpdate。
4. 没有「自动播放被拦截」这种状态。
5. 字幕不交给 iOS 系统渲染，所以不需要「去系统设置改样式」的提示，一直显示自有样式编辑器；MPV 下 ASS/PGS 由 libass 原样渲染（等价于 jassub/libbitsub）。
6. 音量手势真的改系统音量（MPVolumeView），只在不能改时显示「音量由系统侧键控制」。
7. MPV 引擎没有画中画；App 多了 AirPlay 按钮。
8. 换音轨在 MPV 直出时原地切换，所以「需要重新起流」的提示只在需要时显示。
9. 退出落点：fullScreenCover 关闭后回到原页面，等价于 Web 的 sessionStorage 返回路径。

### 6 搜索

1. 搜索入口是 iOS 26 原生独立「搜索」标签页（`Tab(role:.search)`），不是网页那种圆钮弹出的浮层面板。模式、分类与历史的功能都对齐；关键词提交后不清空，与网页面板关闭即复位不同。
2. 没有 ⌘K/Tab/↑↓/esc 快捷键和页脚快捷键提示，占位文案也没有「下次按 ⌘K 唤醒」。
3. 排序、视图、垂直不写 URL，不可分享；网页切垂直会产生浏览器历史，App 切页签不会。
4. iOS 同时只能展示一个弹层：操作面板点「下载」后先收起面板，约 450ms 后再弹目标弹窗或确认条（TorrentActions.swift:25-35）。网页可以叠在抽屉上。
5. 「查看详情」用系统 openURL 打开站点页，对应网页的新标签页。
6. 排序用系统 Menu、下拉用带勾选的 Menu、视图切换用 SF Symbols 图标，属于视觉实现差异。

### 7 订阅

1. 海报墙交互：网页触屏「首点展开信息层」换成了 App 常显元信息 + 长按上下文菜单（查看订阅详情 / 查看影片详情）。
2. 下拉 `<select>` 换成 SwiftUI `Picker(.menu)`；复选框换成圆形勾选；Modal 换成 sheet（导航栏左上「取消 / 先不 / 保留内容 / 完成」）。
3. 顶栏胶囊换成导航栏中间的分段控件；分区吸顶用 LazyVStack pinnedViews 实现。
4. App 额外支持下拉刷新（列表、详情）；详情页刷新失败时保留旧内容（网页会整页进失败态）。
5. 确认框用系统 alert；toast 走全局 FeedbackHost。
6. 「更多」用系统 List sheet（带 SF Symbols 图标、medium/large detents），网页是自绘底部抽屉 + 「关闭」按钮。

---

### 8 活动

1. 确认框：网页用 Modal，App 用 `Feedback.confirm`（结束播放 / 注销设备）和底部 sheet（删除种子任务、忽略任务）；文案一致。「同时删除数据文件」在网页是复选框，App 是 Toggle。
2. 打开种子页 / 刷流行点名称：网页开新标签页，App 用系统 `openURL`。
3. 趋势图悬停：网页鼠标悬停看读数，App 改为点按柱子（Swift Charts）。
4. 实时通道：网页用 EventSource（自动重连），App 自己解析 SSE 流，断线 3 秒后重连。
5. 轮询暂停：网页按页面可见性暂停，App 按 `applicationState == .active` 暂停。
6. 观看数据：网页的活动页和底栏各自轮询一路，App 合成一个常驻的 store，数据一致且请求减半。
7. LLM 接入探测：App 缓存 5 分钟（ActivityHandoff.swift:27），网页在 Provider 挂载时探测一次。

### 9 AI 会话

1. 技能 chip 位置：Web Lexical 在输入框内插行内原子 chip；原生 TextField 做不了行内原子节点，App 放在输入框上方的托盘行，发送时拼成同样的 token 形态（AgentComposer.swift:5-31）。改写重问回填时，正文中间的 token 会被挪到最前（服务端展开结果一致）。
2. 回车行为（B 中条目 4）：iOS 原生多行输入回车换行、发送靠按钮，符合 iOS 习惯。
3. 粘贴截图 / 拖图进输入框：Web 支持（composer-editor.tsx PasteFilesPlugin），原生 TextField 收不到图片粘贴，App 没有。
4. 回答里的链接：Web 一律新窗口打开（markdown.tsx:24-29）；App 站内链接走原生路由、外链交给系统浏览器（AgentConversationView.swift:49-55）。App 行为更合理。
5. 用户气泡操作：App 在点按浮现之外还有长按系统菜单（复制 / 改写这条提问）。
6. Netflix 主题的 /new 居中版式与「我的→新任务」入口：App 只做银玻璃，不适用。
7. 页脚耗时上悬停的供应商/模型/token 提示：Web 只有桌面悬停可见，手机不可见，App 不显示，两端手机上一致。

### 10 设置（上）

1. 设置页返回用系统 NavigationStack；网页返回条右侧的搜索键 App 不提供（设置页里没有全局搜索入口）。
2. 站点「添加 / 编辑授权」、下载器「编辑配置」、规则组编辑器从网页的就地展开或弹窗改成 sheet（手机表单长、要避让键盘）。
3. 搜索分类排序：网页用指针拖拽手柄，App 用「调整顺序」编辑模式拖动把手；预设行的「编辑 / 删除」收进 ⋯ 菜单。
4. 导航顺序只影响网页桌面端侧栏，App 保留完整的读写与合并规则，说明文字按平台改写。
5. 背景图瓷砖的 × 在触屏上常显（网页靠 hover/touch-reveal）；站点「资料更新于」从悬停提示改成一行小字。
6. 手工令牌地址未配置时，用「App 当前连接的服务器地址」代替「浏览器地址栏」。
7. 浏览器扩展安装检测在 App 内固定显示「未检测到」（iOS 没有 Chromium 扩展），与 iOS Safari PWA 下网页的实际表现一致。
8. 目录字段 App 允许手输路径（网页只能浏览选择），属超集。

---

### 10 设置（下）

1. 播放页的「播放引擎（本机）」是 App 独有的设置（PlaybackSettingsView.swift:50-66）。
2. 刮削芯片的特殊项说明、「X 不跟随此处的设置」、Webhook 里 jellyfin 不支持的事件说明、网络测试的 message：网页用悬停提示，App 直接写出或放 ⓘ。
3. 「更多语言/地区」、LLM 表单、MCP 新建、Webhook 编辑、回退选择器：网页是页面内面板或居中弹窗，App 是 sheet；MCP 详情由 `?endpoint=` 原地切换改成 push。
4. 控件替换：分段控件代替胶囊页签，系统 Menu 代替下拉，Toggle 代替液态玻璃开关或复选框，Stepper/DatePicker 代替 number/time 输入，SecureField 代替 CSS 圆点遮罩。
5. 更新或重启恢复后，网页整页刷新，App 就地重拉并提示「应用已恢复」。
6. 端口切换完成页：App 提示退出登录、在登录页改服务器地址；「使用」按钮和端口映射判断取 App 当前连接的服务器地址。
7. 系统日志全屏：网页 Modal，App fullScreenCover；日期选择用 Menu。
8. 加载态：骨架屏换成转圈或 SettingsLoadingRow（消息推送、AI 设定、MCP 等）。

---


## 附：各分节全文与证据目录

- 分节全文（含 A 逐条核对表、D 未验证点）：`/tmp/audit/sec-00-shell.md`、`sec-02-discover.md`、`sec-04a-library-home.md`、`sec-04b-library-detail.md`、`sec-04c-item-detail.md`、`sec-05-player.md`、`sec-06-search.md`、`sec-07-subscriptions.md`、`sec-08-activity.md`、`sec-09-agent.md`、`sec-10a-settings.md`、`sec-10b-settings.md`、`sec-99-runtime.md`
- 截图：网页 `/tmp/audit/web/`（首屏与整页）、`/tmp/audit/webui/`（交互）；App `/tmp/audit/app/`（路由直达）、`/tmp/audit/ui/`（XCUITest 交互）；缩略图 `/tmp/audit/sm/`
- 临时 UI 测试：`/Users/yee/workspace/mc-ios-audit/ios/MovieClaw/MovieClawUITests/AuditParityUITests.swift`（未提交，修补阶段可删）
- 清单本身的错误（以源码为准，修补时别照清单改）：相关链接只有 TMDB 和豆瓣，没有 IMDb；发现详情里的演职员一律跳 /discover/people；AI 字幕阶段顺序是 准备 → 统一人名 → 翻译 → 质检 → 保存；「继续观看」行 ⋯ 菜单实际有 4 项（今天/最近一周/全部/某个库）；收藏页没有库筛选；规则组适用范围是 作品类型/区域/类型；Webhook 格式是 movieclaw/jellyfin；飞书只需 Webhook 地址和签名密钥；日志级别顺序是 全部/错误/警告/信息/调试；缓存页 2 秒轮询只在后台统计时进行。
