import SwiftUI

/// 底部标签页。顺序与 Web 银玻璃主题手机底栏一致；搜索是独立的圆形按钮（iOS 26 search 角色标签）。
enum MainTab: String, Hashable, CaseIterable {
    case discover, library, subscriptions, activity, search

    var title: String {
        switch self {
        case .discover: "发现"
        case .library: "媒体库"
        case .subscriptions: "订阅"
        case .activity: "活动"
        case .search: "搜索"
        }
    }

    var systemImage: String {
        switch self {
        case .discover: "house"
        case .library: "play.square.stack"
        case .subscriptions: "bookmark"
        case .activity: "waveform.path.ecg"
        case .search: "magnifyingglass"
        }
    }
}

/// 播放请求：对应 Web `/play/{mediaItemId}/{sXXeYY}?t=` 与分享页 `/s/{slug}/play/...`。
struct PlayRequest: Identifiable, Hashable {
    var mediaItemId: Int
    var season: Int?
    var episode: Int?
    /// 指定起播秒数（仅对第一个播放单元生效，同 Web `?t=`）
    var startSeconds: Double?
    /// 访客分享播放：走 `/share/{slug}/playback` 接口族，进度只存本地
    var shareSlug: String?
    /// 播放器内切换文件版本时指定
    var fileId: Int?

    var id: String { "\(shareSlug ?? "")-\(mediaItemId)-\(season ?? -1)-\(episode ?? -1)" }
}

/// 全局弹层：多个模块都会唤起的对话框放这里，由根视图统一呈现，避免各页面重复挂载。
enum AppSheet: Identifiable, Hashable {
    /// 订阅对话框（发现海报、详情页、搜索结果、AI 卡片、媒体库「洗版」都会用）
    case subscribe(SubscribeRequest)
    /// 账号切换
    case accountSwitcher

    var id: String {
        switch self {
        case let .subscribe(request): "subscribe-\(request.hashValue)"
        case .accountSwitcher: "account-switcher"
        }
    }
}

/// 唤起订阅对话框所需的信息（对应 Web SubscribeDialog 的入参）
struct SubscribeRequest: Hashable {
    /// 作品引用：`tmdb:movie:123` / `tmdb:tv:456` / `douban:tv:789`
    var titleRef: String
    /// 展示用标题（预览接口返回前先显示）
    var title: String?
    /// 洗版模式：只列有洗版目标的规则组，建好后立刻跑一轮洗版
    var upgrade: Bool = false
    /// 洗版模式下的库内作品（用于回跳）
    var libraryItemId: Int?
}

/// 全局导航状态。页面通过 `@Environment(Router.self)` 拿到它来跳转。
///
/// 每个标签页有独立的导航栈（切标签不丢各自的浏览位置）；
/// `push` 压到当前标签，`open(_:)` 按路由归属切到对应标签再压栈。
@Observable
final class Router {
    var selectedTab: MainTab = .discover
    var paths: [MainTab: [AppRoute]] = [:]
    /// 全屏播放器
    var player: PlayRequest?
    /// 全局弹层
    var sheet: AppSheet?
    /// 「更多」面板（左上角头像）
    var showsMore = false

    /// 当前标签的导航栈（绑定给 NavigationStack）
    func path(for tab: MainTab) -> Binding<[AppRoute]> {
        Binding(
            get: { self.paths[tab] ?? [] },
            set: { self.paths[tab] = $0 }
        )
    }

    /// 当前账号的权限（由 MainTabView 写入），路由守卫据此把越权目标改道
    var permissions: Permissions = .none

    /// 路由守卫（同 Web `accessiblePathFor` 与设置页的越权回退）：
    /// - 成员打开仅管理员可见的设置分区 → 改去「个人信息」（Web settings-view 的 replace 到 /settings/profile）；
    /// - 其余越权页面（AI 会话、无能力的订阅/搜索、活动、媒体库管理）→ 落到媒体库首页。
    /// 界面上本就不给这些入口，守卫兜的是通知、AI 卡片、深链等「从别处跳过来」的情况。
    func guarded(_ route: AppRoute) -> AppRoute {
        guard !permissions.allows(route) else { return route }
        if case .settingsSection = route { return .settingsSection(.profile) }
        return .libraryHome
    }

    /// 在当前标签内压栈
    func push(_ route: AppRoute) {
        let route = guarded(route)
        if let root = Self.tabRoot(of: route) {
            selectedTab = root
            paths[root] = []
            rememberRootParameter(of: route)
            return
        }
        paths[selectedTab, default: []].append(route)
    }

    /// 切到路由归属的标签后压栈（通知、AI 卡片等「从别处跳过来」的场景）
    func open(_ route: AppRoute) {
        let route = guarded(route)
        if let root = Self.tabRoot(of: route) {
            selectedTab = root
            paths[root] = []
            rememberRootParameter(of: route)
            return
        }
        showsMore = false
        // 切到路由归属的标签（该标签对当前账号不可见时——例如成员没有订阅页——留在当前标签）
        if let target = route.tab, availableTabs.contains(target) { selectedTab = target }
        paths[selectedTab, default: []].append(route)
    }

    /// 当前账号可见的标签（由 MainTabView 按权限写入）
    var availableTabs: Set<MainTab> = Set(MainTab.allCases)

    /// 打开 Web 站内链接；解析失败返回 false
    @discardableResult
    func open(webPath: String) -> Bool {
        guard var route = AppRoute(webPath: webPath) else { return false }
        // 「/」对成员同样收敛到媒体库（Web accessiblePathFor：成员的 / → /library）
        if !permissions.isAdmin, URLComponents(string: webPath)?.path.split(separator: "/").isEmpty ?? false {
            route = .libraryHome
        }
        open(route)
        return true
    }

    func pop() {
        _ = paths[selectedTab]?.popLast()
    }

    func popToRoot() {
        paths[selectedTab] = []
    }

    func play(_ request: PlayRequest) {
        player = request
    }

    func present(_ sheet: AppSheet) {
        self.sheet = sheet
    }

    /// 切到标签根时附带的参数（活动页的 view、发现页的电影/剧集），由对应根页面读取后清空
    struct RootParameter: Equatable {
        var tab: MainTab
        var value: String
        let id = UUID()
    }

    var rootParameter: RootParameter?

    /// 记下标签根路由携带的参数
    private func rememberRootParameter(of route: AppRoute) {
        switch route {
        // 活动页不带 view 也要下发（空串）：Web 缺省/非法 view 一律回到「观看 · 正在播放」
        case let .activity(view): rootParameter = RootParameter(tab: .activity, value: view ?? "")
        case let .discover(kind): rootParameter = RootParameter(tab: .discover, value: kind)
        default: break
        }
    }

    /// 标签根页面对应的路由不压栈，而是切标签
    private static func tabRoot(of route: AppRoute) -> MainTab? {
        switch route {
        case .discover: .discover
        case .libraryHome: .library
        case .subscriptions: .subscriptions
        case .activity: .activity
        default: nil
        }
    }
}
