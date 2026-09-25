import SwiftUI

/// 登录后的主界面：iOS 26 原生液态玻璃标签栏。
///
/// 对应 Web 银玻璃主题手机底栏（components/glass-tab-bar.tsx）：
/// 发现 / 媒体库 / 订阅（有订阅权限）/ 活动（管理员），搜索是尾部独立的圆形按钮
/// ——正好是 iOS 26 `Tab(role: .search)` 的原生形态；下滑时标签栏自动收起。
///
/// 这里还负责注入全局依赖：Router（导航）、Feedback（提示/确认）、APIClient、权限，
/// 并在根部统一挂载全屏播放器、全局弹层与「更多」面板。
struct MainTabView: View {
    @Environment(AppModel.self) private var model
    @State private var router = Router()
    @State private var feedback = Feedback()
    @State private var badges = ShellBadges()

    var body: some View {
        let session = model.session
        let permissions = session.map(Permissions.init(session:)) ?? .none
        let api = model.api ?? EnvironmentValues().api

        TabView(selection: $router.selectedTab) {
            Tab(MainTab.discover.title, systemImage: MainTab.discover.systemImage, value: MainTab.discover) {
                TabRoot(tab: .discover) { DiscoverView(kind: "movie") }
            }
            Tab(MainTab.library.title, systemImage: MainTab.library.systemImage, value: MainTab.library) {
                TabRoot(tab: .library) { LibraryHomeView() }
            }
            if permissions.canSubscribe {
                Tab(MainTab.subscriptions.title, systemImage: MainTab.subscriptions.systemImage, value: MainTab.subscriptions) {
                    TabRoot(tab: .subscriptions) { SubscriptionsView() }
                }
            }
            if permissions.isAdmin {
                Tab(MainTab.activity.title, systemImage: MainTab.activity.systemImage, value: MainTab.activity) {
                    TabRoot(tab: .activity) { ActivityView(initialView: nil) }
                }
                .badge(badges.activityBadge)
            }
            if permissions.canSearch {
                Tab(MainTab.search.title, systemImage: MainTab.search.systemImage, value: MainTab.search, role: .search) {
                    TabRoot(tab: .search) { SearchHomeView() }
                }
            }
        }
        .tabBarMinimizeBehavior(.onScrollDown)
        .sheet(item: $router.sheet) { sheet in
            sheet.content.sheetFeedback()
        }
        .sheet(isPresented: $router.showsMore) {
            NavigationStack {
                MorePage()
                    .navigationDestination(for: AppRoute.self) { $0.destination }
            }
            .sheetFeedback()
        }
        .fullScreenCover(item: $router.player) { request in
            PlayerScreen(request: request)
        }
        .modifier(FeedbackHost(feedback: feedback))
        #if DEBUG
        .task {
            // 开发期：-mcRoute 直接打开某个站内路径（与网页同路由截图对照）
            guard let path = DebugLaunch.route else { return }
            if path.hasPrefix("/play/"), let id = Int(path.split(separator: "/")[1]) {
                router.play(PlayRequest(mediaItemId: id))
            } else {
                router.open(webPath: path)
            }
        }
        #endif
        .onChange(of: permissions, initial: true) { _, value in
            var tabs: Set<MainTab> = [.discover, .library]
            if value.canSubscribe { tabs.insert(.subscriptions) }
            if value.isAdmin { tabs.insert(.activity) }
            if value.canSearch { tabs.insert(.search) }
            router.availableTabs = tabs
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

/// 一个标签页的导航根：独立导航栈 + 路由映射 + 全局顶栏（左上头像、右上新会话）
struct TabRoot<Root: View>: View {
    let tab: MainTab
    @ViewBuilder let root: () -> Root
    @Environment(Router.self) private var router

    var body: some View {
        NavigationStack(path: router.path(for: tab)) {
            root()
                .appTopBar()
                .navigationDestination(for: AppRoute.self) { route in
                    route.destination
                }
        }
    }
}

/// 标签根页面的顶栏：左上头像（打开「更多」，有待处理更新时带蓝点）、
/// 右上「+」新建 AI 会话（管理员）。页面自己的操作按钮用 `.toolbar` 追加在它们之间。
struct AppTopBar: ViewModifier {
    @Environment(AppModel.self) private var model
    @Environment(Router.self) private var router
    @Environment(ShellBadges.self) private var badges
    @Environment(\.permissions) private var permissions
    @Environment(\.api) private var api

    func body(content: Content) -> some View {
        content.toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Button {
                    router.showsMore = true
                } label: {
                    AvatarBadge(session: model.session, size: 30)
                        .overlay(alignment: .topTrailing) {
                            if badges.updatePending {
                                Circle().fill(Theme.info).frame(width: 9, height: 9).offset(x: 2, y: -2)
                            }
                        }
                }
                .accessibilityLabel("更多")
                .accessibilityIdentifier("open-more")
            }
            if permissions.isAdmin {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        router.push(.newSession)
                    } label: {
                        Image(systemName: "plus")
                    }
                    .accessibilityLabel("新会话")
                }
            }
        }
    }
}

extension View {
    func appTopBar() -> some View { modifier(AppTopBar()) }
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

extension Theme {
    /// 银蓝暗侧 --accent-2
    static let accent2 = Color(red: 0x9F / 255, green: 0xB0 / 255, blue: 0xC9 / 255)
}

/// 外壳上的角标状态（管理员）：待处理更新（头像蓝点，10 分钟轮询）、
/// 活动标签提示（需处理任务 > 有人在看 > 任务进行中，同 Web glass-tab-bar `pickActivityDot`）。
///
/// 任务与观看两份数据源（`tasks` / `media`）也挂在这里、由外壳常驻运行：活动页直接读同一个实例，
/// 全 App 只有一条 `/jobs/stream` SSE、一路下载器轮询、一路播放活动轮询（Web 同样是全站 Provider）。
///
/// 与 Web 的差异：iOS 标签角标只能是系统红底的数字/文字，不能按状态换色。于是用文字区分三档：
/// 需要处理显示数量（红底数字，语义与 Web 红点一致）、有人在看显示「在看」、只有进行中显示「进行中」。
@Observable
final class ShellBadges {
    var updatePending = false
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

    /// 活动标签角标：按优先级只表达当前最该被看见的那一件事
    var activityBadge: Text? {
        if needsAction > 0 { return Text("\(needsAction)") }
        if watching > 0 { return Text("在看") }
        if running > 0 { return Text("进行中") }
        return nil
    }

    func run(api: APIClient) async {
        await withTaskGroup(of: Void.self) { group in
            group.addTask { await self.tasks.run(api: api) }
            group.addTask { await self.media.run(api: api) }
            group.addTask {
                while !Task.isCancelled {
                    if let pending = try? await api.appUpdatePending() {
                        await MainActor.run { self.updatePending = pending.appVersion != nil || pending.modelTag != nil }
                    }
                    try? await Task.sleep(for: .seconds(600))
                }
            }
        }
    }
}
