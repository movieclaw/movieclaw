# MovieClaw iOS App 与网页手机端·第二轮对等审计报告

- 审计对象：`feat/ios-app` @ `564c5710`（工作树 `/Users/yee/workspace/mc-ios-audit2`，分支 `audit/ios-parity-2`，只读，未提交；临时 UI 测试 `AuditR2UITests.swift` 审完已删除，工作树干净）
- 模拟器 MC-Audit2（iOS 27 模拟器，Debug 构建）；对照：NAS 正式服务 `http://192.168.1.10:3000`，网页用 Playwright iPhone 15 Pro 手机视口
- 日期：2026-09-26
- 方法：
  1. 807 条逐条重新核对，第一轮判「一致」的也重核。共 6 个子审计员，同时最多 2 个。每人对照 `apps/web` 与 App 当前代码，重点查修补改动过的文件和全局行为：背景层、Polling、fromNow、整数插值、路由守卫、深链参数。第一轮 177 条「差异/缺失/未验证」逐条给出修补状态。
  2. 补审第一轮跳过的 `/library/manage`、LibraryFormDialog、`/s/{slug}` 访客页，共 158 条，另外单独统计。
  3. 运行期验证：两端同路由截图 26 对（`/tmp/audit2/pair/*.jpg`）。临时 XCUITest 只读打开弹层、菜单、页签，覆盖发现页首点、成员编辑、更多、外观、访客页、墙返回。播放器共跑 7 次。另建了一个测试分享，审完已取消。
- 逐条核对表：`/tmp/audit2/r2-00-shell-09-agent.md`、`r2-02-06-discover-search.md`、`r2-04-library.md`、`r2-05-07-08-player-subs-activity.md`、`r2-10-settings.md`、`r2-manage-form-share.md`
- 证据目录：网页 `/tmp/audit2/web/`、`/tmp/audit2/webs/`（滚屏与 innerText）；App `/tmp/audit2/app/`、`/tmp/audit2/ui/`（XCUITest 截图，`*.txt` 为无障碍文本）；两端对照 `/tmp/audit2/pair/`；拼图 `/tmp/audit2/crop/`；崩溃报告 `/tmp/audit2/MovieClaw-2026-09-26-093610.ips`

## 1. 结论

**未达到「除已接受平台差异外全部一致」的验收标准。**

第一轮的 3 条 P0 情况如下：
- SA-1（成员保存抹授权）：已修复，运行期确认编辑弹窗里 AV、成人图片为选中态。
- S-1（Netflix）：按用户拍板归为已接受差异。外观页 Netflix 卡片已置灰，并注明「Netflix 主题仅在网页生效，App 固定使用银玻璃」。
- P-1（中央三键）：系统播放器已修复，运行期确认三键正常。MPV 播 4K 片时在模拟器上又复现两次「控制层可见、三键和转圈都不在、画面全黑」，暂列 P1，待真机判别。

807 条的结果：一致 733、差异 25、缺失 0、已接受差异 49。补审 158 条：一致 137、差异 11、缺失 3、已接受 7。

去重后剩余差异共 **40 条：P0 0 条、P1 10 条、P2 30 条**。P1 如下：

1. 访客页在 App 里没有生产入口（需要产品拍板）。
2. P-1 残留：MPV + 4K 下中央三键不可见（模拟器复现，待真机）。
3. MPV 起播时 CoreAudio RPC 超时导致 App 崩溃（模拟器复现，待真机）。
4. AVPlayer 把 -11828「格式无法识别」算成网络错误，同档重开没有次数上限，可能无限循环起停会话。
5. 详情页灯箱「设为背景」后，App 背景不刷新。
6. 首页行偏好单例在切换账号后不清空，可能覆盖新账号的偏好。
7. 从条目详情返回单库墙：整面重载、跳回墙首。运行期已确认。
8. 从条目详情返回合集墙和图廊：同上，合集墙运行期已确认。
9. 单库图廊「回到上次位置」后换排序，窗口不回墙首。
10. LD-1 元数据刷新状态只修了一半，而且出现回归：外部发起的刷新，App 看不到。

## 2. 总览

「已接受差异」包括三类：用户拍板的（S-1 Netflix、S-7 红底角标）、第一轮平台差异、修补组未修但理由复核成立的。第一轮的「未验证」有 2 条，本轮都已下结论：LI-3 降为 P2；6 搜索那条已核为一致。

### 2.1 清单 807 条

| 清单章节 | 条目数 | 一致 | 差异 | 缺失 | 已接受差异 |
|---|---|---|---|---|---|
| 0 外壳与导航 | 44 | 32 | 2 | 0 | 10 |
| 1 认证 | 18 | 18 | 0 | 0 | 0 |
| 2 发现 | 29 | 26 | 0 | 0 | 3 |
| 3 媒体详情与影人 | 20 | 18 | 1 | 0 | 1 |
| 4 媒体库（首页/自定义/收藏/合集/单库/待处理/条目详情） | 168 | 153 | 13 | 0 | 2 |
| 5 播放器 | 55 | 45 | 4 | 0 | 6 |
| 6 搜索 | 70 | 62 | 0 | 0 | 8 |
| 7 订阅 | 48 | 46 | 0 | 0 | 2 |
| 8 活动 | 40 | 40 | 0 | 0 | 0 |
| 9 AI 会话 | 32 | 29 | 2 | 0 | 1 |
| 10 设置（21 个分区） | 243 | 231 | 1 | 0 | 11 |
| 11 分享弹层（ShareDialog） | 6 | 5 | 0 | 0 | 1 |
| 12 其它通用行为 | 11 | 6 | 1 | 0 | 4 |
| 13 实时行为 | 23 | 22 | 1 | 0 | 0 |
| **合计** | **807** | **733** | **25** | **0** | **49** |

第 4 节比子审计员的统计多 1 条差异：4a #8 继续观看卡片的「已播 / 总时长」，运行期发现取整方式不同（N-RT-1）。

### 2.2 本轮补审（第一轮跳过，不计入 807）

| 范围 | 条目数 | 一致 | 差异 | 缺失 | 已接受差异 |
|---|---|---|---|---|---|
| /library/manage：媒体库页签与骨架 | 37 | 32 | 3 | 1 | 1 |
| /library/manage：回收站 | 24 | 22 | 1 | 0 | 1 |
| /library/manage：重复文件 | 23 | 23 | 0 | 0 | 0 |
| /library/manage：分享页签 | 10 | 8 | 0 | 0 | 2 |
| LibraryFormDialog 新建向导与编辑表单 | 39 | 34 | 3 | 1 | 1 |
| /s/{slug} 访客页与访客播放器 | 25 | 18 | 4 | 1 | 2 |
| **合计** | **158** | **137** | **11** | **3** | **7** |

访客页比子审计员的统计多 1 条差异（N-share-5），是运行期对照截图时发现的。

### 2.3 第一轮 177 条（差异 164 + 缺失 11 + 未验证 2）的修补状态

- **已修复（绝大多数）**：
  - 外壳：S-2~S-6、S-9~S-12、S-14~S-17、S-19、R-1、R-3~R-7、A-4
  - 发现：D-1~D-5、D-7~D-12、D-14
  - 媒体库：LH-1/2/3/5/6/7/8/9/11、LD-2/3/5/6/7/8/10/11/12、LI-1/2/4~9
  - 播放器：P-2、P-4~P-15（P-1 仅系统播放器路径修复）
  - 搜索：SR-1、SR-3、SR-5、SR-8、SR-10、SR-12~SR-15
  - 订阅：SB-2~SB-9
  - 活动：A-1~A-7
  - AI 会话：AI-1、AI-2、AI-3、AI-5、AI-6、AI-8
  - 设置：SA-1、SA-2、SA-4~SA-7、SA-9~SA-11、SBB-1~SBB-9、SBB-11，以及 SBB-10 的 9 个小项
- **部分修复**：P-1、P-3（顺带引入 N-05-1）、LH-4、LD-1（有回归）、LD-4、LD-9、S-13、S-18、SA-3（N-10-1）、SR-9（其余部分已接受）
- **未修复**：LI-3（由「P1 待确认」降为 P2）、R-8
- **修出新问题**：N-05-1（修 P-3 引入）、N-13-1（S-19 的连带回归）、N-00-2（R-1 背景模糊缓存）、N-03-1（R-1 修完后暴露）、N-04a-1（LH-7 修复路径上的单例）、LD-1 回归
- **未修且理由成立，归为已接受**：S-8、D-6、SR-2、SR-4、SR-6、SR-7、SR-11、AI-4、AI-7、LH-10、SA-8、SB-10、SBB-10 中 3 小项（配对第 3 步文案、无代理提示位置、占位符插入位置），以及 D-13 的播放方式（Safari 视图代替 iframe）
- **用户拍板**：S-1、SB-1、S-7

## 3. 剩余差异清单

编号规则：
- 沿用第一轮编号的，是未修或部分修复的条目。
- `N-<节>-<序>` 是本轮代码核对新发现的。
- `N-RT`、`R-9` 是本轮运行期新发现的。

表内按严重度排序。

| 编号 | 章节 | 条目 | 网页行为 | App 现状 | 严重度 | 涉及文件 | 建议修法 | 证据 |
|---|---|---|---|---|---|---|---|---|
| N-share-1 | 11 访客页（补审） | 访客页入口 | 任何人拿到 `/s/{slug}` 链接，浏览器直接打开，不需要账号 | SharePageView 只能经 `router.open(webPath:)` 到达。调用方只有三个：AI 会话里同主机的站内链接（AgentConversationView.swift:51）、通知 href（NoticeCenterView.swift:92）、DEBUG 启动参数 `-mcRoute`。App 没有 onOpenURL、URL scheme、Universal Links，也没有「粘贴分享链接」或「在 App 内预览」入口。页面挂在登录后的外壳里。**生产环境实际无法到达**，但功能本身完整：运行期用 DEBUG 路由验证过密码卡、错密码提示「密码不对，请重新输入」、影片页 | P1（按「入口没有」字面可升 P0；也可由产品拍板「访客链接一律走浏览器」，改记平台差异） | SharePageView.swift, AppRoute.swift, ManageShares（分享页签） | 分享页签行和分享弹层加「在 App 内预览」→ `router.open(.share(slug:))`；「我的」或管理页加「粘贴分享链接」；可选接 onOpenURL | 代码核对；/tmp/audit2/ui/share_gate.png、share_wrong.png、share_item_*.png 对照 /tmp/audit2/webs/share_*.png |
| P-1（残留） | 5 播放器 | #23 中央三键 | 控制层可见、不在转圈、不是报错或同意态时，中央一直显示三键（video-player.tsx:3331） | 系统播放器 1080p（6506）：三键正常（/tmp/audit2/crop/p6506b.jpg）。**MPV 播 4K 片（6434）在模拟器上两次复现**：顶栏、底栏、时间（53:00、52:58）都可见，中央三键和「正在缓冲…」都没有，画面全黑（/tmp/audit2/crop/player_a.png、p6434e.jpg）。代码推演（子审计员）：PlayerScreen.swift:240/164 的条件下，三键和转圈每一帧至少显示一个；视频层 EngineSurface 在 ZStack 最底层。所以更像是模拟器 GL 主线程逐帧绘制拖住了 SwiftUI 合成，与 R-8 同根 | P1（暂定；若真机不复现，降为模拟器问题，与 R-8 合并） | PlayerScreen.swift, MPVRenderViews.swift | 真机播同一部 4K 片判别：出现异常时一次快照同时查 `player-time`、`player-play-pause`、`player-busy`；另用 `-mcMPVBackend metal` 对照 | /tmp/audit2/ui/player_a.png、p6434_mpv_d.png |
| R-9 | 5 播放器（运行期） | MPV 起播时 App 崩溃 | — | MPV 播 6434（4K）约 1 分钟后 SIGABRT：`AudioToolboxCore _ReportRPCTimeout → AURemoteIO::Start → MPVCore ao_start → audio_start_ao → run_playloop`。原因是 CoreAudio RPC 超时，系统主动 abort。发生时模拟器内存与交换压力很大（本机交换区 4GB，另有两台其他会话的模拟器在跑） | P1（待真机确认；若只在模拟器高压下出现，降为 P2） | MPVCore（ao 配置）、PlaybackController | 真机复测。MPV 的 ao 可改在进入播放前先激活 AVAudioSession（异步 API），或捕获 ao 初始化失败后改用系统播放器 | /tmp/audit2/MovieClaw-2026-09-26-093610.ips |
| N-05-1 | 5 播放器 | 取流失败归因（修 P-3 时引入） | hls.js 网络错误先重试 3 次，再 onNetworkDead 同档重开；媒体错误走自救或降档（engine.ts:486-505） | `AVPlayerEngine.cause(of:)`（AVPlayerEngine.swift:284）把 AVFoundationErrorDomain **-11828** 算成 `.network`。已对照 SDK 头文件：-11828 就是 `AVErrorFileFormatNotRecognized`（格式无法识别）。`engineFailed` 遇到 `.network` 直接同档重开（PlaybackController.swift:665-671）：不计失败、不降档、没有次数上限。AVPlayer 放不了某档格式时，可能无限循环「正在准备视频流…」，反复起停服务端会话 | P1 | AVPlayerEngine.swift, PlaybackController.swift | 把 -11828 移出网络码表；同档网络重开加上限，比如连续 2 次没到 `.playing` 就按 decode 降档或报错 | 代码核对 + AVError.h:62 |
| N-03-1 | 3 媒体详情 | 剧照灯箱「设为背景」 | 上传后立即 `applyView`，全站背景当场换成这张（lib/backdrop.tsx:177-183；md:916-941） | MediaDetailView.swift:602-604 `_ = try await client.uploadBackdrop(...)`，丢掉了返回的 AppearanceView。提示「已设为背景」，App 背景却要重启或切换账号才变 | P1 | MediaDetailView.swift, AppBackdrop.swift | `let view = try await client.uploadBackdrop(...); await AppBackdropStore.shared.apply(appearance: view, api: client)` | 代码核对（写操作，未运行期执行） |
| N-04a-1 | 4 首页 | 首页行偏好单例跨账号串用 | UiPrefs 按当前会话加载（library-view.tsx:180-181） | `LibraryHomePrefs.shared`（LibraryHomeView.swift:9-24）只在 `rows == nil` 时拉一次（:319-321），切换账号不清空。新账号首页沿用旧布局；在新账号合集页点「显示在首页」，会以旧账号的行清单为底整份 PUT，覆盖新账号偏好（CollectionDetailView.swift:427-442） | P1 | LibraryHomeView.swift, CollectionDetailView.swift, AppModel.swift | 单例记 owner（同 SubscriptionIndex），账号变化即置 nil；或在 update(session:)/logout 时清空 | 代码核对 |
| N-04b-2 | 4 单库页 | 从条目详情返回，海报墙整面重载、跳回墙首 | 快照恢复，返回时只整窗对账、位置不动（library-detail-view.tsx:1096-1112，注释写明这是 2026-09-07 用户反馈过的问题） | `.task(id: wallKey){ reloadWall() }`（LibraryDetailView.swift:165）→ pager.reset（:783-790）。**运行期确认**：/library/19 下滑 5 屏 → 进《芭比》→ 返回，墙回到顶部「阿凡达」 | P1 | LibraryDetailView.swift, LibraryWall.swift | 首载判 `items == nil`；wallKey 变化改用 `.onChange`，返回只走 refresh（收藏页已这样修，FavoritesView.swift:105-113） | /tmp/audit2/crop/walls.jpg（wall_before/detail/after） |
| N-04a-2 | 4 合集 / 图廊 | 从详情返回，合集页海报墙和三处图廊整面重载 | 同上（favorites-view.tsx:471） | 合集页 `.task(id:sortKey…)` → pager.reset（CollectionDetailView.swift:108-112）。LibraryGalleryWall `.task(id:LoadKey)`（:272）每次出现都重载，:462 把多页窗口替换成一页。**运行期确认**：/library/19/c/563 下滑 5 屏 → 进《茶馆》→ 返回，回到墙首 | P1 | CollectionDetailView.swift, LibraryGalleryWall.swift | 同上；GalleryFeed 加整窗 refresh | /tmp/audit2/crop/colls.jpg |
| N-04b-3 | 4 单库页 | 图廊跳回上次位置后换排序或筛选，窗口不回墙首 | 换排序时 `galleryStart = 0`（library-detail-view.tsx:1106-1119） | galleryStart（LibraryDetailView.swift:67）只在 :261、:831 被改，reloadWall 不复位。结果是按新排序从旧 offset 起取，图廊不会向上补页，墙首那段看不到（收藏页已在 FavoritesView.swift:110 复位） | P1 | LibraryDetailView.swift | reloadWall 或 onChange(wallKey) 时把 galleryStart 置 0 | 代码核对 |
| LD-1（部分修复，有回归） | 4 单库页 | 元数据刷新状态来源 | 进页探一次 progress，刷新中每 2s 轮询；每轮 reload 还从库列表的 `metadata_refresh` 补种状态（:562-566） | progress 探测和 2s 轮询已实现（LDV:159-163、843-861）。但 `refreshingMeta` 只读 metaRefresh（:88），reload（:728-767）不再看 `library.metadataRefresh`。进页之后由首页、其他设备或定时任务发起的刷新：面板不出现，⋯ 菜单仍是「刷新元数据」，墙轮询停在 30 秒档 | P1 | LibraryDetailView.swift | reload 里如果远端在刷新、本地没有，就写入 metaRefresh | 代码核对 |
| N-10-1（SA-3 残留） | 10 设置·下载器 | 「去补映射」的预填只生效一次 | `suggestMapping` 是页面级状态，传给每一台下载器的编辑表单（downloader-config-section.tsx:97-111, 224, 455） | 只有自动打开的那一次带建议（DownloadersSettingsView.swift:160-163）；取消后从菜单再点「编辑配置」，不会预填 | P2 | DownloadersSettingsView.swift | 把 `routeQuery["suggest_mapping"]` 存成页面级 @State，所有编辑入口都带上 | 代码核对 |
| N-00-1 | 0 外壳 | 进设置分区后的返回链 | /settings/[x] 的返回固定回 /settings（app-shell.tsx:298-316） | 「更多 → 新版本 vX」只压 `.settingsSection(.app)`（MorePage.swift:84）；待处理事项「去处理」经 `router.open(webPath:)`（NoticeCenterView.swift:92）也只压分区页，返回直接回标签根或事项页。S-9 只修了「个人信息」那一行 | P2 | MorePage.swift, Router.swift | `Router.open` 打开 `.settingsSection` 时，栈里没有 `.settings` 就先补压一个 | 代码核对 |
| S-13（部分修复） | 0 外壳 | 会话重命名输入限长 | prompt `maxLength: 80`，输入时就挡住（more-page.tsx:81） | Feedback.prompt 没有 maxLength（Feedback.swift:84），提交时才静默截断（MorePage.swift:230） | P2 | Feedback.swift, MorePage.swift | prompt 加 maxLength，输入时截断 | 代码核对 |
| N-00-2 | 0 外壳 | 背景模糊成品缓存没有上限 | CSS 实时模糊，不存成品 | `blurCache` 按「尺寸 + 半径」存，只在换图时清空（AppBackdrop.swift:49-51、105、128-137）。把模糊滑杆从 0 拖到 40，最多约 41 张全屏 2x 图，约 230MB；detached 渲染不随取消 | P2 | AppBackdrop.swift | 每个尺寸只留 2–3 个半径（LRU）；渲染完成后检查是否仍需要这个半径 | 代码核对 |
| S-18（部分修复） | 12 通用 | 墙位置「久别回归」 | 所有记位置的墙（含收藏墙）都监听回归（library-wall-recall.ts:158-200） | 只有单库墙监听 scenePhase（LibraryDetailView.swift:821-838）；收藏墙（FavoritesView.swift:184）没有；也没有全局的回归时刻 | P2 | FavoritesView.swift | 根视图写全局 `returnedAt`，各面墙出现时比较 | 代码核对 |
| N-04a-3 | 4 收藏 | 收藏页挂后台 30 分钟回来不重新询问 | favorites-view.tsx:564 useWallRecall | FavoritesView 没有 scenePhase 处理 | P2 | FavoritesView.swift | 与 S-18 一起做 | 代码核对 |
| N-13-1 | 13 实时 | 媒体库首页扫描结束后可能一直 3 秒轮询 | 扫描结束 12 秒后 recentlyBusy 置 false，间隔回到 30 秒（library-view.tsx:340） | 间隔在 body 里按 `Date.now < busyUntil` 算（LibraryHomeView.swift:98-104）。Polling 改成传值后，只有 body 重算才会更新间隔；reload 去重赋值，数据不变就不重算，间隔可能一直停在 3 秒 | P2 | LibraryHomeView.swift | 用 `@State recentlyBusy` 加到期置 false 的 `.task(id: busyUntil)` | 代码核对 |
| N-09-1 | 9 AI 会话 | 技能快选展开时按回车 | Lexical typeahead 回车选中高亮技能（composer-editor.tsx:327-349） | 快选展开时 onKeyPress 返回 `.ignored`（AgentComposer.swift:105），外接键盘回车插入换行 | P2 | AgentComposer.swift | 快选展开时回车选中当前项，返回 `.handled` | 代码核对 |
| N-09-2 | 9 AI 会话 | Markdown 图片拆分的边界情况 | react-markdown 按语法树渲染 | `splitImages` 对段落原文做正则拆分（AgentMarkdown.swift:37-57）：`[![x](图)](链接)` 会剩下残文；行内代码里的 `![x](y)` 也会被拆成图片 | P2 | AgentMarkdown.swift | 正则跳过反引号区间，或只识别整行只有图片的情况 | 代码核对 |
| N-RT-1 | 4 首页 | 继续观看卡片「已播 / 总时长」取整 | `Math.round(ms/1000)`（up-next-row.tsx:54），6434 显示「52:55 / 2:28:10」 | `Formatters.clock` 向下取整（Theme.swift:66-71），显示「52:54 / 2:28:10」 | P2 | LibraryHomeView.swift:546 | 这一处改为四舍五入（播放器时钟保持向下取整） | /tmp/audit2/pair/library.jpg |
| N-04b-4 | 4 单库页 | 刷新结束后的收尾重拉被取消（疑似） | `if(!p.refreshing) reload()` | LDV:855-858 先写 metaRefresh 再 await reload；`task(id: refreshingMeta)` 随之被取消，新海报最多晚 30 秒出现 | P2 | LibraryDetailView.swift | 先 reload 再写状态，或把 reload 放进脱离的 Task | 代码核对 |
| N-04b-5 | 4 单库页 | 进页首次「回到上次位置」询问有竞态（疑似） | 数据齐了才问 | LDV:794 要 `libraries != nil` 才问，并发的首轮可能跳过，之后不会补问 | P2 | LibraryDetailView.swift | reload 成功后再检查一次 recallChecked | 代码核对 |
| LD-4（残留） | 4 单库页 | 久别回归复位没有滚回墙顶 | 复位到顶部再弹胶囊 | LDV:833 只做 `pager.jump(to:0)`，没有 scrollTo | P2 | LibraryDetailView.swift | 同时用 ScrollViewProxy 滚到顶 | 代码核对 |
| LD-9（残留） | 4 单库页 | 成员在合集视图的 ⋯ 菜单 | 只要有 ⋯、处于合集视图，就有「显示已隐藏的合集」（:1435、1452-1455） | 该项只给管理员（LDV:603-606）；成员开着图床偏好进合集视图时少这一项 | P2 | LibraryDetailView.swift | 去掉管理员限定 | 代码核对 |
| LI-3 | 4 条目详情 | 更换图片保存失败的表现 | 错误提示在网格上方，网格保留（artwork-picker-dialog.tsx:62-64、129-136、148） | 错误块整块替换网格（ArtworkPickerSheet.swift:90-98、208-210），另弹 toast。「候选图加载失败（TMDB 可能不可达）」这句误导文案两端都有 | P2 | ArtworkPickerSheet.swift | 加载失败与保存失败分成两个状态 | 代码核对 |
| N-05-3 | 5 播放器 | 诊断面板打开时钉住控制层 | chrome.ts 注释明确写「诊断面板不在此列（曾经在）」 | PlayerScreen.swift:200 `!controller.diagnosticsOpen` 时才自动收起 | P2 | PlayerScreen.swift | 去掉这个条件，并同步改 testCenterControlsWithMPV | 代码核对 |
| N-05-4 | 5 播放器 | 站内 /play 链接 | 站内链接可直达 `/play/{id}/{sXXeYY}?t=` | `AppRoute(webPath:)` 没有 play 分支，`Router.open(webPath:)` 返回 false；只有 MainTabView.swift:69 的 DEBUG 前缀特判能打开 | P2 | AppRoute.swift, Router.swift | 增加 play 解析 | 代码核对 |
| R-8 | 5 播放器（运行期） | MPV 模拟器画面 | — | MPVRenderViews.swift 没有改动。MPV 播 4K（6434）画面全黑；MPV 播 1080p mp4（6506）起播失败，提示「MPV 播放失败，已改用系统播放器」 | P2（待真机确认） | MPVRenderViews.swift | 真机复测；模拟器可默认走 Metal | /tmp/audit2/crop/plmpv.jpg、p6506c.jpg |
| N-share-2 | 11 访客页（补审） | 音轨/字幕行 | 复用 ReadOnlyTrackRows（media-track-rows.tsx:643）：语言组有排序、AI 字幕有语言名、芯片带格式标记、展开是分组 | ShareItemView.swift:429-564 私有简化版：不排序、没有 AI 语言名、字幕芯片不带格式、展开是简表 | P2 | ShareItemView.swift, MediaTrackSection.swift | MediaTrackSection 加只读模式，访客页复用 | 代码核对 |
| N-share-3 | 11 访客页（补审） | 续播信息 | 按钮下方显示进度条和「看到 hh:mm」 | 只把按钮改成「继续观看」（ShareItemView.swift:252-264） | P2 | ShareItemView.swift | 补上「看到 hh:mm」这一行 | 代码核对 |
| N-share-4 | 11 访客页（补审） | `/s/{slug}/play/sXXeYY?t=` 链接 | 直达访客播放器 | AppRoute.swift:270-273 只取 slug，丢掉 play 段 | P2 | AppRoute.swift | 解析 play 段后起播 | 代码核对 |
| N-share-5 | 11 访客页（补审，运行期） | 演职员角色与外链样式 | 「饰 Jiao San Ye」；「TMDB ↗」「IMDb ↗」 | 「Jiao San Ye」（没有「饰」）；「TMDB」「IMDb」（没有 ↗） | P2 | ShareItemView.swift | 对齐文案 | /tmp/audit2/crop/share.jpg |
| N-manage-1 | 4 管理（补审） | 「待处理」胶囊和 ⋯ 菜单项 | 跳 `/library/{id}?pending=1`，抽屉按 缺失→待识别→待复核→已忽略 选落点 | 在管理页直接弹 IssueDrawer，写死 `missing ? "missing" : "unidentified"`（LibraryManageView.swift:349-355）；只有待复核或已忽略的库会落到空页签 | P2 | LibraryManageView.swift | initialTab 传 nil，或 push `.library(id:, pending: true)` | 代码核对 |
| N-manage-2 | 4 管理（补审，缺失） | 库作业事件即时刷新 | JobsProvider SSE 指纹一变就立即重拉（library-manage-view.tsx:175-193） | 只靠轮询，空闲时最长 30 秒才看到外部发起的扫描 | P2 | LibraryManageView.swift | 订阅 ShellBadges.tasks.jobs 的库资源作业指纹 | 代码核对 |
| N-manage-3 | 4 管理（补审） | 带 ?tab= 进入后页签被重置 | 只在挂载时读一次 | `.task` 每次重新出现都执行 `tab = initialTab`（LibraryManageView.swift:74-75）；push 进单库页再返回，页签被拨回去 | P2 | LibraryManageView.swift | 只应用一次 | 代码核对 |
| N-manage-4 | 4 管理（补审） | 确认框取消键 | 回收站清理、重复文件四处确认的取消键都是「先不」 | Feedback.confirm 取消键固定为「取消」（ManageRecycleBin.swift:139、ManageDuplicateFiles.swift:152/179/215/232） | P2 | Feedback.swift, ManageRecycleBin.swift, ManageDuplicateFiles.swift | confirm 已有 cancelTitle 参数（SB-6 修补加的），传「先不」 | 代码核对 |
| N-form-1 | 4 表单（补审） | 新建第 2 步名称框回车 | 回车执行主按钮，排除输入法选词（library-form-dialog.tsx:658-661） | 没有 onSubmit（LibraryFormSheet.swift:221-224） | P2 | LibraryFormSheet.swift | `.onSubmit { if !primaryDisabled { primaryAction() } }` | 代码核对 |
| N-form-2 | 4 表单（补审） | 封面预览兜底 | 加载失败显示「暂无封面」 | 只显示 photo 图标（ManageFormParts.swift:437） | P2 | ManageFormParts.swift | 补上文案 | 代码核对 |
| N-form-3 | 4 表单（补审，缺失） | 刮削设置分组标题 | 「元数据 / 图片 / 命名与整理 / 目录写入」四个分节标题 | 七张卡平铺，没有分组（ManageScrapeOverrides.swift:97-101） | P2 | ManageScrapeOverrides.swift | 插入四个分组标题 | 代码核对 |
| N-form-4 | 4 表单（补审） | 新建后定位新库 | 新行平滑滚到视口中间 | 只刷新列表（LibraryManageView.swift:98） | P2 | LibraryManageView.swift | ScrollViewReader 滚到 saved.id | 代码核对 |

上表是**按问题去重**的清单，和第 2 节的逐条统计不是一一对应：
- 同一个问题可能影响多条清单条目，例如 N-04a-2 同时影响合集墙和三处图廊；
- 有的问题是跨条目的运行期发现（R-8、R-9、N-00-2）。

补审的 3 条「缺失」分别是 M12（N-manage-2）、F34（N-form-3）、G1（N-share-1）。

逐条对应关系见各分节核对表的「第二轮结论」列。

## 4. 已接受差异清单（含理由）

### 4.1 用户拍板
- **S-1 / SB-1 Netflix 主题**（第 0 节 0.32–0.36、第 12 节 12.6、第 7 节 #14）：App 固定银玻璃。已核实外观页 Netflix 卡片 `.disabled`、透明度 0.4，脚注写明「Netflix 主题仅在网页生效，App 固定使用银玻璃。主题跟随账号保存，当前设置的是移动端的主题，网页桌面端与移动端可分别设置。」（AppearanceSettingsView.swift:146-193；运行期截图 /tmp/audit2/ui/appearance_1.png）。
- **S-7 标签角标只能红底**：App 用「数字 / 在看 / 进行中」区分。

### 4.2 第一轮平台差异（复核仍然成立）
- **外壳**：
  - 原生标签栏带文字；
  - 搜索是 `Tab(role:.search)` 标签页；
  - 系统拖动切页签；
  - 原生导航栏；
  - 待处理用 push 页；
  - 滚动恢复靠 NavigationStack；
  - ProgressView 代替骨架屏；
  - 按 URLError 细分文案；
  - `.id(username)` 重建界面树，代替整页刷新。
- **发现与搜索**：
  - 电影/剧集分段放在顶栏（S-8，理由见 4.3）；
  - 数据源用 Menu；
  - 筛选不写网址（深链反向恢复已做）；
  - 预告片用 SFSafariViewController，不内嵌 iframe；
  - 灯箱点「下载」时先收起灯箱再弹窗（iOS 同时只能显示一个弹层）；
  - 没有 ⌘K 快捷键。
- **媒体库**：
  - 海报卡附加信息用长按菜单；
  - 自定义页用系统拖动把手；
  - 生成章节用三按钮 alert；
  - IssueDrawer 用 sheet；
  - 筛选面板用原生 sheet；
  - 下载原图走系统分享面板；
  - returnTo 由返回栈代替；
  - 分享链接以当前连接的服务器地址补全。
- **播放器**：
  - AVPlayer + libmpv 双引擎，外加「播放引擎」菜单；
  - 不用 sendBeacon；
  - 没有全屏键；
  - 没有「自动播放被拦截」状态；
  - 字幕自有样式；
  - MPV 没有画中画，另多 AirPlay；
  - 手动选「系统播放器」时 ASS 只显示纯文本（AVPlayer 没有这类渲染能力；默认「自动」模式已改走 MPV）。
- **订阅**：
  - 海报墙信息常显，单击进详情（SB-10）；
  - Picker、sheet、系统 alert 代替网页控件。
- **活动**：
  - Modal 换成系统确认框或 sheet；
  - openURL 代替新标签页；
  - 点按柱子看读数；
  - 自行解析 SSE；
  - 按 applicationState 暂停轮询。
- **AI 会话**：
  - 技能 chip 放在托盘行；
  - 不能粘贴或拖入图片；
  - 外链交给系统浏览器；
  - 用户气泡多一个长按菜单。
- **设置**：
  - sheet 代替就地展开；
  - 拖动把手调整顺序；
  - 背景图瓷砖的 × 常显；
  - 手工令牌地址用 App 当前连接的服务器地址；
  - 浏览器扩展检测固定显示「未检测到」；
  - 「播放引擎（本机）」是 App 独有设置；
  - 更新或重启完成后 App 就地重拉，不整页刷新；
  - 端口切换完成页引导到登录页改服务器地址；
  - 日志全屏用 fullScreenCover。
- **补审部分**：
  - 章节确认的勾选框改成第二个按钮；
  - 回收站批量操作放在列表表头；
  - 可见成员改为带搜索的勾选列表；
  - 访客页开在当前标签的导航栈里。

### 4.3 修补组未修、复核后理由成立
| 编号 | 理由 |
|---|---|
| S-8 电影/剧集切换位置 | iOS 26.0 的 `tabViewBottomAccessory` 挂在整个标签栏上，所有标签都会显示（26.1 起才能按标签关闭）。放在顶栏中间，功能等价 |
| D-6 筛选弹层副标题 | App 没有网址可分享，照搬网页「筛选会写入网址」反而说错 |
| SR-2 老快照解码 | 后端 `/search/history/{id}/results` 用 pydantic `list[TorrentHit]` 重新校验，缺省字段一定会补齐（routes/search.py:405-446），App 不会解码失败 |
| SR-4 影视模式说明 | 两端实际都同时搜豆瓣和 TMDB，是网页文案过时，建议改网页 search-command.tsx:534 |
| SR-6 抢种模式下切范围 | 网页会丢掉 for_sub，悄悄退出选种模式，属网页缺陷；App 保留更合理 |
| SR-7 按发布时间升序 | 网页注释写「升降序都垫底」，但实现只在降序生效；App 符合设计意图 |
| SR-11 提交 toast | 网页走确认条时本来就弹同一句，App 让两条路径一致 |
| SR-9（剩余部分） | 手机列表行 App 常显指标，信息多于网页；做种 ≥100 加粗已补 |
| AI-4 停止失败提示 | 网页静默，App 弹 toast，信息只多不少 |
| AI-7 代码块复制键常显 | 功能相同，触屏上更容易发现；代码注释写明是有意为之 |
| LH-10 全部合集没有「首页/合集」分段 | 用导航栈返回，信息等价 |
| SA-8 分区描述位置 | 描述放在列表行，信息相同 |
| SBB-10 三小项 | 配对第 3 步：App 去掉「网页的」三个字更准确；「当前无可用代理」文案逐字一致，只是位置不同；刮削占位符插在模板末尾，更合理 |
| S-4 延伸 | 成员打开 /activity、/library/manage 时，App 改道到媒体库；网页会显示接口全部 403 的空壳。App 没有地址栏，改道更合理 |
| 补审 S3、S5、G24 | 分享列表首载失败时网页一直停在「正在读取分享…」；合集分享行网页拼出 /library/null 死链；合集分享播放网页疑似被弹回名单。这三处 App 更合理，属网页缺陷 |

## 5. NAS 写操作与恢复核对

### 5.1 测试分享
- **创建**：2026-09-26 09:40:01。`POST /libraries/19/items/6506/share`，请求体 `{"expires_in_days":1,"password":"aud123"}`，返回 id=2、slug `zZAUQXy7Xji0ANF3`，影片《百鸟朝凤》。
- **使用**：网页访客端和 App 各输过一次错密码、一次正确密码（解锁接口），分享 view_count 到 2。
- **取消**：10:07 `DELETE /shares/2`，返回「分享已取消」。
- **核对**：
  - `GET /shares` → `[]`
  - `GET /share/zZAUQXy7Xji0ANF3` → **404** `SHARE_NOT_FOUND`「分享不存在或已取消」
  - `GET /libraries/19/items/6506/share` → `null`

### 5.2 续播点

审计前在 08:55 记录了基线（/tmp/audit2/resume-baseline.txt）。恢复一律用 `POST /playback/progress`，请求体：
```
{"media_item_id":..,"season_number":0,"episode_number":0,"event":"stop","position_ms":<原值>,"device_id":"ui-test"}
```
这个请求体与仓库 PlayerUITests.restoreResume 相同，字段按后端 `PlaybackProgressRequest` 核过：不传 audio_track、subtitle_track 时服务端保持原值。

| 条目 | 基线 | 审计后（10:08 核对） | 说明 |
|---|---|---|---|
| 6434《蜘蛛侠：英雄无归》 | position 3174771，played false，audio embedded:1，subtitle off，play_count 10 | position **3174771**，played false，轨道不变，play_count **16** | 共起播 5 次，每次都已写回原位置。**有两次超过 30 秒上限，需要告知用户**，见下方 |
| 6506《百鸟朝凤》 | position 0，played false，play_count 25 | position **0**，played false，play_count **28** | 起播 3 次，每次约 20–30 秒；位置没有前移，无需写回 |
| 6335、6633 | 未触碰 | 与基线一致 | — |

6434 超过 30 秒的两次：
- **第 1 次**：XCUITest 在 MPV 播放时查询控制层，被界面空闲等待拖住，实际播放约 70 秒，位置推到 3244299，已写回。
- **第 4 次**：MPV 播 4K 时 App 崩溃（R-9），测试进程挂了约 2.5 分钟，但位置只前进约 13 秒（3188391），已写回。

另有一次误操作：磁盘写满导致步骤文件没有写入成功，重跑时误用了上一份 MPV 步骤，多播了一次约 25 秒（3214895），已写回。

play_count 递增无接口可回退：6434 为 10→16，6506 为 25→28。

### 5.3 其他
- 除上面两项外，没有执行任何写操作。成员编辑、外观页等弹层只打开后点「取消」，没有点保存、确认、删除。
- 审计期间观看活动里有一路「yee · Safari · iPhone 在看《我不是大师》」，不是审计产生的。

### 5.4 环境问题（需告知用户）
- 审计中途本机磁盘一度写满：可用空间从 6.9GB 掉到 117MB，Bash 报 ENOSPC。
- 主要原因：
  - App 崩溃后 xcodebuild 自动触发 `simctl diagnose`，这是本审计造成的，已终止并删除本审计的 xcresult；
  - 系统交换区增至 4GB；
  - 另有两台其他会话的模拟器（iPhone 17、MC-FixB）在跑。
- 现已恢复到约 5.7GB 可用。
