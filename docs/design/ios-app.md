# iOS 原生 App 设计

> 状态：开发中（分支 `feat/ios-app`）。验收标准：浏览器与 iOS App 同时打开同一台服务器，
> `docs/design/ios-app/parity-inventory.md` 列出的全部功能两端一致。

## 1. 决策

| 事项 | 结论 | 理由 |
|---|---|---|
| 技术栈 | SwiftUI 全原生，iOS 26+ | 液态玻璃标签栏/工具栏/浮层系统自带；网页 PWA 的画中画、全屏、字幕等受 WebKit 所限 |
| 范围 | Web 手机端全部功能原生重写（不内嵌网页） | 用户决定 |
| 播放 | Swiftfin 式多引擎：AVPlayer + libmpv（MPVKit，LGPL 构建） | 用户决定；AVPlayer 管画中画/AirPlay/杜比视界，mpv 管 MKV/ASS/PGS 直出 |
| 认证 | 复用 Web 同一套会话 Cookie | 后端零改动，多账号切换、吊销、改密下线与网页一致 |
| 接口层 | 脚本生成（`apps/apple/scripts/gen_api.py`） | 340 个接口、459 个模型手写不可维护 |
| 工程 | XcodeGen（`project.yml`，同步文件夹） | 不提交 .pbxproj，并行加文件不冲突 |
| 外观 | 不提供网页的「外观」设置（主题、背景图、界面质感、导航顺序都只作用于网页），设置里没有这一页；底色固定纯黑（同 Apple Music），App 强制暗色（含启动屏），剧照灯箱不带「设为背景」；账号在网页的外观设置原样保留 | 用户决定（2026-09-26），列为已接受差异 |
| 活动页 | 一页总览，不做网页的「观看 / 任务」分段与二级切片胶囊：大标题下一行实时摘要，分组按紧急程度排（需要处理 → 正在播放 → 正在下载 → 进行中 → 最近播放 → 观看统计 → 最近完成），空分组不出现；历史与统计各露一小段，「查看全部」压栈到二级页（`AppRoute.activityPage`），成员 / 周期 / 范围筛选在二级页右上角；浏览范围只在确有隐藏内容时以分组脚注出现；设备处置走左滑与长按。`/activity?view=plays·stats·history·active` 仍可直达对应二级页 | 用户决定（2026-09-26），列为已接受差异；系统分组列表的左滑、长按、下拉刷新比自绘胶囊更贴 iOS |

## 2. 目录

原生 App 按平台生态放在 `apps/` 下，与 `apps/web`、`apps/extension` 并列：`apps/apple/` 是一个 Xcode 工程，
现在只有 iPhone/iPad 目标，将来的 Apple TV（tvOS）版作为同一工程的另一个目标，共用接口层、播放器与 MPV
（届时工程内再拆 `Shared/`、`iOS/`、`tvOS/`）；将来的 Android 版放 `apps/android/`（一个 Gradle 工程，手机与
Android TV 两个模块）。各平台共用 Bundle ID / 包名 `io.movieclaw.app`，请求标识为 `MovieClaw-<iOS|tvOS|Android>/<版本>`，
活动页据此显示「MovieClaw iOS / Apple TV / Android」。`pnpm-workspace.yaml` 因此只列 JS 项目、不用 `apps/*` 通配。

```
apps/apple/
  project.yml                 工程定义（xcodegen generate 生成 .xcodeproj，不入库）
  scripts/gen_api.py          生成接口层；后端改了接口就重跑
  scripts/test.sh             跑测试（兜住 xcodebuild 不退出）
  MovieClaw/
    App/                      入口、AppModel（连接/登录状态机）、Routing（路由/导航/全局弹层）
    Core/API/Generated/       生成的模型（命名空间 API.*）与接口函数（APIClient 扩展）——勿手改
    Core/API/*.swift          少量手写补充（multipart 上传、SSE 等生成器跳过的接口）
    Core/Networking/          APIClient、Cookie 钥匙串备份、SSE、服务器地址
    Core/Session/             权限、环境值
    DesignSystem/             主题令牌、反馈中心、三态加载、远程图片、占位页、通用组件
    Features/<模块>/           各功能模块
  MovieClawTests/             单元测试 + Generated/LiveDecodeTests（对真实服务器的解码冒烟）
  MovieClawUITests/           UI 自动化（端到端验收）
```

## 3. 约定（所有模块必须遵守）

### 3.1 接口
- 一律用生成的函数：`@Environment(\.api) private var api` → `try await api.librariesList()`。
  函数名 = 后端 operation_id 驼峰化，文档注释里有 HTTP 方法与路径，找接口用
  `grep -n '/libraries/{library_id}/items' Core/API/Generated/Endpoints.swift`。
- 生成器跳过的接口（文件流、SSE、multipart）见 `Endpoints.swift` 末尾清单，在模块内手写
  `nonisolated extension APIClient`，复用 `raw/send/upload/events/perform`。
- 模型字段不对（解码失败）先查后端 schema，**不要改 Generated/**；需要改生成规则告诉集成方。
- 图片：`api.image(item.posterUrl, .posterCard)` → `RemoteImage(url:)`；远程图自动走后端缓存代理。

### 3.2 页面骨架
- 三态：`@State var state: Loadable<T> = .loading` + `AsyncContent(state, retry:) { … }`，
  加载用 `await Loadable.load(into: $state) { try await api.xxx() }`（已有数据时静默刷新，不闪）。
- 空态 `EmptyState`，失败 `ErrorState`（后端中文原因原样显示）。
- 轮询：`.polling(every: 秒) { await reload() }`，自动随页面可见性与前后台启停；间隔同 Web（清单第 13 节）。
- SSE：`for try await event in api.events("/jobs/stream") { … }` 放在 `.task` 里，离开页面自动断开。
- **弹层**：所有 `.sheet` / `.fullScreenCover` 的内容必须调用 `.sheetFeedback()`——根部的确认框/输入框被 sheet 盖住时弹不出，
  它给弹层配独立的反馈中心，关窗时未消失的 Toast 转交回根部（全局弹层已自动挂上）。
- **命名**：同一个 App target 里 `private` 类型也会和别处的同名类型冲突，模块内新类型一律带模块前缀
  （如 `PlayerUpNextCard`、`LibraryWallCell`），通用名（`UpNextCard`、`Row`、`Header`）禁止使用。
- **模型一致性**：给 `API.*` 模型加 `Identifiable` 等协议一律写在 `Core/API/ModelConformances.swift`（先 grep，别在模块里重复声明）。
- 反馈：`@Environment(Feedback.self)`：`feedback.success/error`、`await feedback.confirm(…)`、`await feedback.prompt(…)`，
  文案照搬 Web。
- 导航：`@Environment(Router.self)`：`router.push(.libraryItem(…))`、`router.open(webPath:)`、
  `router.play(PlayRequest(…))`、`router.present(.subscribe(…))`。
  页面入口类型与参数固定在 `App/Routing/Destinations.swift`，模块只替换自己的占位文件，**不改路由表**。
- 权限：`@Environment(\.permissions)`，入口裁剪口径同 Web `lib/permissions.ts`。
- 视觉：深色，`Theme` 令牌取自 Web 银玻璃主题；列表/按钮/工具栏用系统液态玻璃（`.glassEffect`、`.buttonStyle(.glass)`），
  页面根视图加 `.appBackground()`。不必像素级复刻网页，但**信息与操作必须一致**。
- 中文：所有文案、错误提示用中文；关键类写中文设计注释（CLAUDE.md「注释和日志」）。

### 3.3 文件归属（并行开发的硬规则）
- 只改自己模块目录 `Features/<模块>/` 下的文件；需要新的通用组件放 `DesignSystem/<模块前缀>*.swift` 新文件。
- 不改：`Core/API/Generated/`、`App/Routing/`、其它模块目录、已有 DesignSystem 文件。确需改动写进交付说明，由集成方处理。
- 播放器模块可改 `project.yml` 的 packages（引入 MPVKit）。

## 4. 播放器架构（多引擎）

```
PlayerScreen（控制层 UI、手势、字幕叠加、选轨、诊断）
  └─ PlaybackController（会话协议：/playback/sessions、ping 15s、progress 10s、降档重试、下一集）
       └─ PlayerEngine 协议
            ├─ AVPlayerEngine   HLS / MP4 直出；画中画、AirPlay、杜比视界、全景声、系统字幕
            └─ MPVEngine        libmpv（MPVKit LGPL）：MKV/HEVC/TrueHD/DTS 直出，ASS/PGS 由 libass 渲染
```
- 引擎选择全自动，用户不选（2026-09-26 用户决定，同 Infuse）：系统播放器优先——服务端能直出或只换封装/转音频
  （画面不重编码）就用 AVPlayer，画中画、隔空播放、系统字体字幕都可用；只有选了图形字幕（PGS）、或服务端要为
  AVPlayer 重新编码画面时才用 MPV 在本机直接放原文件。AVPlayer 在不重编码的档位放不出来时自动改用 MPV；MPV 失败回落服务端 HLS + AVPlayer。
- 字幕：文字字幕（SRT/ASS）两个引擎都由 SwiftUI 叠加层用系统字体画（iOS 上 libass 用不了系统中文字体，会画成方框）；
  MPV 只画图形字幕。MPV 播放中点画中画：在当前位置换成系统播放器，就绪后自动进画中画。
- 开发期可用启动参数 `-movieclaw.player.engine system|mpv` 强制引擎、`-mcSubtitle <轨>` 指定起播字幕、`-mcAutoPiP <秒>` 自动点画中画。
- 会话参数（capability、failed_tiers、audio/subtitle track、max_height、downlink_bps）按引擎能力申报。
- LGPL 合规：MPVKit 动态库形式链接；关于页列出 libmpv/FFmpeg 许可与源码地址。
- MPV 真机渲染走 Metal（MoltenVK + gpu-next）：黑底容器铺满播放区，渲染面按视频比例居中摆放，
  横竖屏切换时渲染面随系统旋转动画等比缩放，全程不变形、不黑屏，不重建视频输出。依赖 libmpv 的两个补丁
  （`Vendor/MPVKit/patches/`：渲染面尺寸一变就重排、每帧以交换链实际尺寸为准）。构建产物 `Libmpv.xcframework`
  直接入库，平时无需构建；改补丁或升级 mpv 时用 `scripts/build-libmpv.sh` 重建并提交，
  设计细节见 `MPVCore/MPVRenderViews.swift` 的注释。

## 5. 验收方法

每个模块交付前自证：
1. `xcodebuild build` 通过、无新增警告级错误；
2. **对照截图**：同一路由分别截网页（`/tmp/mc-shots/shoot.mjs`，iPhone 视口）与 App（Debug 启动参数
   `-mcServer … -mcUser … -mcPass … -mcRoute /web/path` 直达页面后 `simctl io screenshot`），逐项核对清单条目；
3. **交互**：清单里的每个操作在 App 里实际点一遍（XCUITest 放 `MovieClawUITests/<模块>UITests.swift`），
   结果与网页一致（同一台服务器，后端状态是唯一事实来源）；
4. 交付说明列出：已实现条目、未实现/有差异条目及原因。

本地联调环境：`http://localhost:3000`（admin / mclaw-dev-2026，数据为开发副本，可放心增删测试数据，
但不要删除已有媒体库、成员与媒体文件）。
