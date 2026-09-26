import Nuke
import SwiftUI

/// 登录后的主界面：iOS 26 原生液态玻璃标签栏。
///
/// 页签只显示图标（参照 Instagram iOS 底栏，2026-09-26 用户要求）：发现 / 媒体库 / 订阅（有订阅权限）/
/// 活动（管理员）/ 头像。最右的头像页签是当前用户头像，点开「更多」页（账号、设置、会话），
/// 前四个与 Web 银玻璃主题手机底栏（components/glass-tab-bar.tsx）同序；下滑时标签栏自动收起。
///
/// 搜索不占页签，在各标签根页右上角（见 AppTopBar）：iPhone 标签栏最多放 5 个页签，管理员
/// 四个内容页签加头像已满，再放搜索页签会被系统收进「More」。标签栏的高度与玻璃质感是系统定的
/// （实测去掉文字仍是 62pt，控件尺寸 / 字号也改不动；背景色设置对液态玻璃不生效），不自绘——用户明确要
/// 原生标签栏。要压暗只能从玻璃身后的内容下手（TabBarScrim）；图标统一成正方形见 TabIcon。
///
/// 这里还负责注入全局依赖：Router（导航）、Feedback（提示/确认）、APIClient、权限，
/// 并在根部统一挂载全屏播放器与全局弹层。
struct MainTabView: View {
    @Environment(AppModel.self) private var model
    @State private var router = Router()
    @State private var feedback = Feedback()
    @State private var badges = ShellBadges()
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.displayScale) private var displayScale
    /// 首次落点只定一次（之后权限变化不再抢标签）
    @State private var landed = false
    /// 头像页签的图标（见 AvatarTabIcon）；nil = 还没画好，先用 SF Symbol 顶一下
    @State private var avatarIcon: UIImage?

    var body: some View {
        let session = model.session
        let permissions = session.map(Permissions.init(session:)) ?? .none
        let api = model.api ?? EnvironmentValues().api

        TabView(selection: $router.selectedTab) {
            Tab(value: MainTab.discover) {
                TabRoot(tab: .discover) { DiscoverView(kind: "movie") }
            } label: {
                iconLabel(.discover)
            }
            .accessibilityLabel(MainTab.discover.title)
            Tab(value: MainTab.library) {
                TabRoot(tab: .library) { LibraryHomeView() }
            } label: {
                iconLabel(.library)
            }
            .accessibilityLabel(MainTab.library.title)
            if permissions.canSubscribe {
                Tab(value: MainTab.subscriptions) {
                    TabRoot(tab: .subscriptions) { SubscriptionsView() }
                } label: {
                    iconLabel(.subscriptions)
                }
                .accessibilityLabel(MainTab.subscriptions.title)
            }
            if permissions.isAdmin {
                Tab(value: MainTab.activity) {
                    TabRoot(tab: .activity) { ActivityView() }
                } label: {
                    iconLabel(.activity)
                }
                .accessibilityLabel(MainTab.activity.title)
            }
            Tab(value: MainTab.more) {
                TabRoot(tab: .more) { MorePage() }
            } label: {
                Label {
                    Text(MainTab.more.title)
                } icon: {
                    Image(uiImage: avatarIcon ?? TabIcon.image(MainTab.more.systemImage))
                }
                .labelStyle(.iconOnly)
            }
            .accessibilityLabel(MainTab.more.title)
            .accessibilityIdentifier("open-more")
        }
        .tabBarMinimizeBehavior(.onScrollDown)
        // 活动页签（红 > 绿 > 蓝，同网页）与头像页签（有待安装的更新）的状态点：
        // SwiftUI 的 .badge 只能红底文字，下到 UIKit 画小圆点
        .background(TabBarDotBridge(
            tabs: Self.visibleTabs(permissions),
            dots: [MainTab.activity: badges.activityDot, .more: badges.moreDot].compactMapValues { $0 }
        ))
        // 头像位图：先出首字版（照片下载前不空着），照片到了再换；改昵称 / 换头像后重画
        .task(id: "\(session?.nickname ?? "")|\(session?.avatarUrl ?? "")|\(displayScale)") {
            avatarIcon = AvatarTabIcon.render(nickname: session?.nickname, photo: nil, scale: displayScale)
            guard let url = api.image(session?.avatarUrl) else { return }
            let request = ImageRequest(url: url, processors: [.resize(size: CGSize(width: AvatarTabIcon.size, height: AvatarTabIcon.size), contentMode: .aspectFill)])
            guard let photo = try? await ImagePipeline.shared.image(for: request) else { return }
            avatarIcon = AvatarTabIcon.render(nickname: session?.nickname, photo: photo, scale: displayScale)
        }
        .sheet(item: $router.sheet) { sheet in
            sheet.content.sheetFeedback()
        }
        .fullScreenCover(item: $router.player) { request in
            PlayerScreen(request: request)
        }
        .modifier(FeedbackHost(feedback: feedback))
        #if DEBUG
        .task {
            // 开发期：-mcRoute 直接打开某个站内路径（与网页同路由截图对照）
            guard let path = DebugLaunch.route else { return }
            router.permissions = permissions // 启动路由可能抢在 onChange 同步权限之前
            router.open(webPath: path)
        }
        #endif
        .onChange(of: permissions, initial: true) { _, value in
            let tabs = Set(Self.visibleTabs(value))
            router.availableTabs = tabs
            router.permissions = value
            // 权限被收回时（后台重新校验身份后），停在已不可见的标签上要落回媒体库
            if !tabs.contains(router.selectedTab) { router.selectedTab = .library }
            land(permissions: value)
        }
        .onDisappear {
            // 会话过期被打回登录页：记下此刻的位置，重新登录后回到这里（Web 401 → /login?next=原路径）
            model.captureResume(tab: router.selectedTab, path: router.paths[router.selectedTab] ?? [])
        }
        .onChange(of: scenePhase) { _, phase in
            // 墙位置的「久别回归」判定全站共用一个时刻（各面墙出现时比较，见 LibraryWallRecall）
            LibraryWallRecall.noteScenePhase(phase)
            // 回到前台：后台静默重新校验身份与权限（Web AuthGate 每次挂载重取 /auth/me），
            // 管理员顺带刷新待更新快照（Web 窗口获得焦点即刷新）
            guard phase == .active else { return }
            Task {
                if let fresh = try? await api.authMe(), fresh.username == session?.username {
                    model.update(session: fresh)
                }
                if permissions.isAdmin { await badges.refreshUpdate(api: api) }
            }
        }
        .task(id: permissions.isAdmin) {
            guard permissions.isAdmin else { return }
            await badges.run(api: api)
        }
        .environment(router)
        .environment(feedback)
        .environment(badges)
        .environment(\.api, api)
        .environment(\.permissions, permissions)
        .tint(Theme.accentStrong)
    }
}

extension MainTabView {
    /// 当前账号能看到的页签，按标签栏上从左到右的顺序（与上面 TabView 的声明顺序一致，
    /// TabBarDotBridge 靠这个顺序找页签）
    static func visibleTabs(_ permissions: Permissions) -> [MainTab] {
        var tabs: [MainTab] = [.discover, .library]
        if permissions.canSubscribe { tabs.append(.subscriptions) }
        if permissions.isAdmin { tabs.append(.activity) }
        tabs.append(.more)
        return tabs
    }

    /// 只有图标的页签标签：标题留在 Label 里表明含义，显示时去掉（读屏名字由 Tab 的
    /// `.accessibilityLabel` 给——Label 里的标题不会传给系统标签栏）。图标统一成正方形，见 TabIcon
    private func iconLabel(_ tab: MainTab) -> some View {
        Label { Text(tab.title) } icon: { Image(uiImage: TabIcon.image(tab.systemImage)) }
            .labelStyle(.iconOnly)
    }

    /// 登录 / 切换账号 / 退出后自动切到下一个账号时的首个落点（只定一次）：
    /// - 会话过期前记下的位置（同一身份可进入时）优先还原；
    /// - 否则成员落「媒体库」（Web accessiblePathFor：成员的 / → /library），管理员落「发现」
    ///   （Web 银玻璃手机端 / → /discover/movie）。
    private func land(permissions: Permissions) {
        guard !landed else { return }
        landed = true
        // 已经被别处（深链、调试启动路由）导航过就不再抢落点
        guard router.selectedTab == .discover, router.paths.values.allSatisfy(\.isEmpty), router.rootParameter == nil else { return }
        if let resume = model.takeResume(), router.availableTabs.contains(resume.tab),
           resume.path.allSatisfy(permissions.allows) {
            router.selectedTab = resume.tab
            router.paths[resume.tab] = resume.path
            return
        }
        router.selectedTab = permissions.isAdmin ? .discover : .library
    }
}

/// 一个标签页的导航根：独立导航栈 + 路由映射 + 全局顶栏（右上角搜索）+ 底栏压暗
struct TabRoot<Root: View>: View {
    let tab: MainTab
    @ViewBuilder let root: () -> Root
    @Environment(Router.self) private var router
    /// 当前页藏起了标签栏（AI 会话页，见 hidesTabBar）：压暗层跟着撤掉，免得压在输入框上
    @State private var tabBarHidden = false

    var body: some View {
        NavigationStack(path: router.path(for: tab)) {
            root()
                .appTopBar(tab: tab)
                .navigationDestination(for: AppRoute.self) { route in
                    route.destination
                }
        }
        .onPreferenceChange(TabBarHiddenKey.self) { tabBarHidden = $0 }
        .overlay {
            if !tabBarHidden { TabBarScrim() }
        }
    }
}

/// 垫在标签栏底下的黑色渐变，让液态玻璃整体暗一些（2026-09-26 用户要求底栏压暗）。
///
/// 液态玻璃的背景色 / `.toolbarBackground` 都不生效（实测），玻璃显示的是它身后内容的模糊，
/// 所以从内容这一侧下手：在屏幕最底下铺一层黑，玻璃透出来的就是暗底，毛玻璃质地不变。
/// 系统的 `.scrollEdgeEffectStyle(.hard)` 也能压暗，但会在底栏上方切出一条硬边横带，没用。
/// 实色部分正好盖住底栏（62pt 高 + 离屏幕底边 21pt），上面再留一段渐变过渡，不出硬边。
struct TabBarScrim: View {
    private static let barCover: CGFloat = 83
    private static let fade: CGFloat = 24
    private static let opacity = 0.75

    var body: some View {
        let height = Self.barCover + Self.fade
        VStack(spacing: 0) {
            Spacer(minLength: 0)
            LinearGradient(
                stops: [
                    .init(color: .black.opacity(0), location: 0),
                    .init(color: .black.opacity(Self.opacity), location: Self.fade / height),
                    .init(color: .black.opacity(Self.opacity), location: 1),
                ],
                startPoint: .top, endPoint: .bottom
            )
            .frame(height: height)
        }
        .ignoresSafeArea(edges: .bottom)
        .allowsHitTesting(false)
    }
}

/// 页面是否藏起了标签栏（TabRoot 据此撤掉压暗层）
struct TabBarHiddenKey: PreferenceKey {
    static let defaultValue = false
    static func reduce(value: inout Bool, nextValue: () -> Bool) { value = value || nextValue() }
}

extension View {
    /// 藏起标签栏，并通知 TabRoot 撤掉垫在底栏下的压暗层
    func hidesTabBar() -> some View {
        toolbarVisibility(.hidden, for: .tabBar)
            .preference(key: TabBarHiddenKey.self, value: true)
    }
}

/// 标签栏的页签图标：SF Symbol 裁掉自带留白后，按真实字形外框等比放进同一个正方形。
///
/// 系统按字号排 SF Symbol，各图标外框宽窄不一（书签 15×24pt、房子 27×24、头像 26×26），
/// 并排在底栏里显得大小不齐（2026-09-26 用户反馈）。这里统一成最长边 = `side`，与头像页签
/// （AvatarTabIcon）同尺寸；交出去的是模板图，选中 / 未选中照常由系统染色。
enum TabIcon {
    static let side: CGFloat = AvatarTabIcon.size
    @MainActor private static var cache: [String: UIImage] = [:]

    @MainActor static func image(_ name: String) -> UIImage {
        if let cached = cache[name] { return cached }
        let image = render(name)
        cache[name] = image
        return image
    }

    private static func render(_ name: String) -> UIImage {
        // 系统标签栏会把图标自动换成实心款；自己画的位图要显式取 .fill（没有实心款的用原款）
        let config = UIImage.SymbolConfiguration(pointSize: 200, weight: .medium)
        guard let symbol = UIImage(systemName: "\(name).fill", withConfiguration: config)
                ?? UIImage(systemName: name, withConfiguration: config)
        else { return UIImage() }
        // 先画一张大图，按不透明像素找出字形的真实外框
        let large = UIGraphicsImageRenderer(size: symbol.size).image { _ in
            symbol.withTintColor(.black).draw(at: .zero)
        }
        guard let cg = large.cgImage, let glyph = opaqueBounds(cg).flatMap(cg.cropping(to:)) else { return symbol }
        let width = CGFloat(glyph.width), height = CGFloat(glyph.height)
        let scale = side / max(width, height)
        let rect = CGRect(x: (side - width * scale) / 2, y: (side - height * scale) / 2,
                          width: width * scale, height: height * scale)
        return UIGraphicsImageRenderer(size: CGSize(width: side, height: side)).image { _ in
            UIImage(cgImage: glyph).draw(in: rect)
        }.withRenderingMode(.alwaysTemplate)
    }

    /// 位图里不透明像素的外框（像素坐标，原点左上）
    private static func opaqueBounds(_ image: CGImage) -> CGRect? {
        let width = image.width, height = image.height
        guard let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                                      bytesPerRow: width, space: CGColorSpaceCreateDeviceGray(),
                                      bitmapInfo: CGImageAlphaInfo.alphaOnly.rawValue),
              let data = context.data
        else { return nil }
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        let alpha = data.bindMemory(to: UInt8.self, capacity: width * height)
        var minX = width, minY = height, maxX = -1, maxY = -1
        for y in 0..<height {
            for x in 0..<width where alpha[y * width + x] > 8 {
                minX = min(minX, x); maxX = max(maxX, x)
                minY = min(minY, y); maxY = max(maxY, y)
            }
        }
        guard maxX >= minX else { return nil }
        return CGRect(x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1)
    }
}

/// 标签根页面的顶栏：右上角放大镜，在当前标签里压栈打开搜索首页（结果、详情接着压在同一个栈，
/// 返回一路退回来）。常见 App 的搜索都在右上角（2026-09-26 用户拍板）；原来左上角的头像
/// 挪进了标签栏最右的页签。网页右上角的「+」新建 AI 会话在手机 App 里按用户决定去掉了。
///
/// 从媒体库页签点进去预选「媒体库」模式（在哪个页签搜就先搜那里的内容，同 iOS 音乐的资料库）；
/// 其他页签沿用搜索页记住的模式。
///
/// 页面自己的按钮用 `.toolbar` 追加（发现页的筛选、媒体库的 ⋯ 菜单）。
/// 外层注入的 `.topBarTrailing` 会排到页面按钮前面，所以放 `.primaryAction`（固定在最右），
/// 再用固定间隔隔开：页面按钮在左边自成一组，搜索在每个标签根页都是同一位置的独立圆钮。
struct AppTopBar: ViewModifier {
    let tab: MainTab
    @Environment(Router.self) private var router
    @Environment(\.permissions) private var permissions

    func body(content: Content) -> some View {
        content.toolbar {
            if permissions.canSearch {
                ToolbarSpacer(.fixed, placement: .primaryAction)
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        router.push(.searchHome(mode: tab == .library ? .library : nil))
                    } label: {
                        Image(systemName: "magnifyingglass")
                    }
                    .accessibilityLabel("搜索")
                    .accessibilityIdentifier("open-search")
                }
            }
        }
    }
}

extension View {
    func appTopBar(tab: MainTab) -> some View { modifier(AppTopBar(tab: tab)) }
}

/// 头像徽标：有头像显示图片，否则显示昵称首字
struct AvatarBadge: View {
    let session: API.SessionView?
    var avatarUrl: String?
    var nickname: String?
    var size: CGFloat = 32
    @Environment(\.api) private var api

    var body: some View {
        let url = api.image(avatarUrl ?? session?.avatarUrl)
        ZStack {
            Circle().fill(LinearGradient(colors: [Theme.accentStrong, Theme.accent2], startPoint: .top, endPoint: .bottom))
            Text(initials)
                .font(.system(size: size * 0.38, weight: .bold))
                .foregroundStyle(Color.black.opacity(0.75))
            if url != nil {
                RemoteImage(url: url, placeholderSymbol: "person.fill")
            }
        }
        .frame(width: size, height: size)
        .clipShape(Circle())
    }

    private var initials: String {
        let name = (nickname ?? session?.nickname ?? "").trimmingCharacters(in: .whitespaces)
        guard let first = name.first else { return "?" }
        if first.unicodeScalars.first.map({ (0x4E00 ... 0x9FFF).contains($0.value) }) == true { return String(first) }
        return String(name.prefix(2)).uppercased()
    }
}

/// 标签栏最右「头像」页签的图标位图。
///
/// 系统标签栏的页签图标只收位图，并且默认当模板图、按选中色染成单色——头像照片要先画成
/// 圆形位图、以原色（`.alwaysOriginal`）交出去才保得住颜色。画法直接复用 `AvatarBadge`
/// （渐变底 + 昵称首字，没设头像或照片还在下载时就是这一版），照片到手后盖在上面。
enum AvatarTabIcon {
    /// 边长：与其他页签图标的正方形同尺寸（TabIcon.side 取的就是它），与 Instagram 底栏里头像和图标的比例相当
    static let size: CGFloat = 26

    static func render(nickname: String?, photo: UIImage?, scale: CGFloat) -> UIImage? {
        let face = AvatarBadge(session: nil, nickname: nickname, size: size)
            .overlay {
                if let photo {
                    Image(uiImage: photo).resizable().scaledToFill()
                }
            }
            .clipShape(Circle())
        let renderer = ImageRenderer(content: face)
        renderer.scale = scale
        return renderer.uiImage?.withRenderingMode(.alwaysOriginal)
    }
}

extension Theme {
    /// 银蓝暗侧 --accent-2
    static let accent2 = Color(red: 0x9F / 255, green: 0xB0 / 255, blue: 0xC9 / 255)
}

/// 外壳上的角标状态（管理员）：待处理更新（头像页签蓝点，10 分钟轮询）、
/// 活动标签提示（需处理任务 > 有人在看 > 任务进行中，同 Web glass-tab-bar `pickActivityDot`）。
///
/// 任务与观看两份数据源（`tasks` / `media`）也挂在这里、由外壳常驻运行：活动页直接读同一个实例，
/// 全 App 只有一条 `/jobs/stream` SSE、一路下载器轮询、一路播放活动轮询（Web 同样是全站 Provider）。
/// 活动标签的状态点与网页一样按状态换色（红 / 绿 / 蓝），画法见 TabBarDotBridge。
@Observable
final class ShellBadges {
    /// 待更新快照（管理员）；nil 表示没有可用更新
    var pendingUpdate: API.PendingUpdateView?
    var updatePending: Bool { pendingUpdate != nil }
    /// 「更多」里更新行的文案（Web app-update-entry）：应用与模型都有更新时只说应用版本
    var updateLabel: String? {
        guard let pendingUpdate else { return nil }
        if let version = pendingUpdate.appVersion { return "新版本 v\(version)" }
        return pendingUpdate.modelTag.map { "新识别模型 \($0)" }
    }
    /// 任务活动（Job SSE + 下载器快照），活动页任务视角共用
    let tasks = TaskActivityStore()
    /// 媒体库实时活动（8 秒轮询），活动页观看视角共用
    let media = MediaActivityStore()

    /// 需要处理的任务数（红）
    var needsAction: Int { tasks.activity.attentionTotal }
    /// 此刻在播 / 在下载的设备数（绿）
    var watching: Int { media.liveCount }
    /// 进行中的任务数（蓝）
    var running: Int { tasks.activity.activeTotal }

    /// 活动标签的状态点（同网页 glass-tab-bar 的优先级）：有需要处理的任务红、有人在看绿、
    /// 只有进行中的任务蓝；都没有不显示。按优先级只表达当前最该被看见的那一件事。
    /// 用户觉得红底「在看」文字太重，改成小圆点；`label` 给读屏用
    var activityDot: TabBarDotBridge.Dot? {
        #if DEBUG
        // 开发期：-mcActivityDot red|green|blue 强制显示状态点（截图核对用）
        switch UserDefaults.standard.string(forKey: "mcActivityDot") {
        case "red": return .init(color: UIColor(Theme.danger), label: "有需要处理的任务")
        case "green": return .init(color: UIColor(Theme.success), label: "有人正在观看")
        case "blue": return .init(color: UIColor(Theme.info), label: "有任务进行中")
        default: break
        }
        #endif
        if needsAction > 0 { return .init(color: UIColor(Theme.danger), label: "有需要处理的任务") }
        if watching > 0 { return .init(color: UIColor(Theme.success), label: "有人正在观看") }
        if running > 0 { return .init(color: UIColor(Theme.info), label: "有任务进行中") }
        return nil
    }

    /// 头像页签的状态点：有待安装的新版本 / 识别模型时亮蓝点（原先画在左上角头像上，同 Web 头像圆点）
    var moreDot: TabBarDotBridge.Dot? {
        updatePending ? .init(color: UIColor(Theme.info), label: "有可用更新") : nil
    }

    func run(api: APIClient) async {
        await withTaskGroup(of: Void.self) { group in
            group.addTask { await self.tasks.run(api: api) }
            group.addTask { await self.media.run(api: api) }
            group.addTask {
                while !Task.isCancelled {
                    await self.refreshUpdate(api: api)
                    try? await Task.sleep(for: .seconds(600))
                }
            }
        }
    }

    /// 拉一次待更新快照；失败保留上次结果（离线/后端重启时下轮自愈，同 Web）
    func refreshUpdate(api: APIClient) async {
        guard let pending = try? await api.appUpdatePending() else { return }
        await MainActor.run {
            self.pendingUpdate = (pending.appVersion != nil || pending.modelTag != nil) ? pending : nil
        }
    }
}

/// 给系统标签栏的页签挂彩色小圆点（与活动页顶部「观看」旁的状态点差不多大）。
///
/// SwiftUI 的 `.badge` 只能是红底数字/文字；UIKit 的系统空角标（`badgeValue = ""`）是约 18pt 的实心圆，
/// 挂在图标右上角太抢眼（用户反馈），而角标尺寸没有公开接口可调。这里借用系统角标的位置、换掉画法：
/// 角标底色设为透明，角标文字是一个小字号的「●」、文字颜色即状态色——画出来就是图标右上角的一颗小圆点，
/// 标签栏收起/展开、横竖屏时的位置仍由系统排布，不碰任何私有视图。
/// 从视图所在窗口找到标签栏控制器后逐个页签设置。页签只显示图标、系统页签对象上没有标题，
/// 也拿不到 SwiftUI 设的读屏名字，所以按先后顺序对应：`tabs` 必须与 TabView 的声明顺序一致。
struct TabBarDotBridge: UIViewRepresentable {
    struct Dot: Equatable {
        var color: UIColor
        /// 读屏念的状态说明（否则会把「●」念出来）
        var label: String
    }

    /// 标签栏上的页签，从左到右（见 MainTabView.visibleTabs）
    let tabs: [MainTab]
    /// 要挂圆点的页签；不在表里的页签清掉圆点
    let dots: [MainTab: Dot]

    /// 「●」的字号：约合 6pt 直径的圆点（活动页顶部状态点是 6pt）
    private static let dotFontSize: CGFloat = 7.5

    func makeUIView(context: Context) -> UIView {
        let view = UIView(frame: .zero)
        view.isUserInteractionEnabled = false
        return view
    }

    func updateUIView(_ view: UIView, context: Context) {
        let tabs = tabs, dots = dots
        // 等视图进窗口、标签栏建好之后再设（首次更新时窗口可能还是 nil）
        DispatchQueue.main.async {
            guard let root = view.window?.rootViewController,
                  let tabBarController = Self.findTabBarController(from: root),
                  let items = tabBarController.tabBar.items
            else { return }
            for (item, tab) in zip(items, tabs) {
                guard let dot = dots[tab] else {
                    item.badgeValue = nil
                    item.accessibilityValue = nil
                    continue
                }
                let attributes: [NSAttributedString.Key: Any] = [
                    .foregroundColor: dot.color,
                    .font: UIFont.systemFont(ofSize: Self.dotFontSize),
                ]
                item.setBadgeTextAttributes(attributes, for: .normal)
                item.setBadgeTextAttributes(attributes, for: .selected)
                item.badgeColor = .clear
                item.badgeValue = "●"
                item.accessibilityValue = dot.label
            }
        }
    }

    private static func findTabBarController(from controller: UIViewController) -> UITabBarController? {
        if let tabs = controller as? UITabBarController { return tabs }
        for child in controller.children {
            if let found = findTabBarController(from: child) { return found }
        }
        return nil
    }
}
