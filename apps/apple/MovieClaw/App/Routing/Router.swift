import SwiftUI

/// 底部标签页。前四个与 Web 银玻璃主题手机底栏同序；最右是当前用户的头像（「我的」页，
/// Instagram 式的个人页签）。搜索不占标签，在各标签根页右上角（见 MainTabView 的 AppTopBar）。
/// 标签栏只显示图标，`title` 给读屏与 UI 测试用。
enum MainTab: String, Hashable, CaseIterable {
    case discover, library, subscriptions, activity, more

    var title: String {
        switch self {
        case .discover: "发现"
        case .library: "媒体库"
        case .subscriptions: "订阅"
        case .activity: "活动"
        case .more: "我的"
        }
    }

    /// 页签图标；「我的」平时显示头像，这个图标只在头像位图还没画好时顶一下
    var systemImage: String {
        switch self {
        case .discover: "house"
        case .library: "play.square.stack"
        case .subscriptions: "bookmark"
        case .activity: "waveform.path.ecg"
        case .more: "person.crop.circle"
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

extension PlayRequest {
    /// 解析站内播放链接（同 Web `lib/player/play-links.ts` 的地址约定）：
    /// - `/play/{mediaItemId}[/sXXeYY][?t=秒]`
    /// - `/s/{slug}/play[/sXXeYY][?t=秒]`（访客播放；条目 id 要等分享页读到影片才知道，这里记 0）
    /// 不是播放链接返回 nil。
    init?(webPath raw: String) {
        guard let components = URLComponents(string: raw.hasPrefix("/") ? raw : "/\(raw)") else { return nil }
        let parts = components.path.split(separator: "/").map(String.init)
        var unitSegment: String?
        if parts.count >= 2, parts[0] == "play", let id = Int(parts[1]), id > 0 {
            self.init(mediaItemId: id)
            unitSegment = parts.count >= 3 ? parts[2] : nil
        } else if parts.count >= 3, parts[0] == "s", parts[2] == "play" {
            self.init(mediaItemId: 0, shareSlug: parts[1])
            unitSegment = parts.count >= 4 ? parts[3] : nil
        } else {
            return nil
        }
        // sXXeYY 之外的写法（含 s00e00 = 电影）一律当电影 / 由服务端定起点
        if let segment = unitSegment, let match = segment.lowercased().wholeMatch(of: /s(\d+)e(\d+)/),
           let season = Int(match.1), let episode = Int(match.2), season > 0 || episode > 0 {
            self.season = season
            self.episode = episode
        }
        // ?t= 只接受单个非负整数（同 Web queryNumber）
        if let t = components.queryItems?.first(where: { $0.name == "t" })?.value, t.wholeMatch(of: /\d+/) != nil, let seconds = Double(t) {
            startSeconds = seconds
        }
    }
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
    /// 正在播放的控制器：放在这里而不是播放器视图的 @State 里——iOS 26 标签栏在旋转时会重建容器，
    /// 连带全屏呈现的播放器视图被销毁重建；控制器挂在视图上会跟着重开会话、重载 mpv，
    /// 横屏后画面错位、又被旧视图的收尾转回竖屏（真机《抓特务》实测）。
    var activePlayback: PlaybackController?
    /// 全局弹层
    var sheet: AppSheet?

    /// 当前标签的导航栈（绑定给 NavigationStack）
    func path(for tab: MainTab) -> Binding<[AppRoute]> {
        Binding(
            get: { self.paths[tab] ?? [] },
            set: { self.paths[tab] = $0 }
        )
    }

    /// 当前账号的权限（由 MainTabView 写入），路由守卫据此把越权目标改道。
    /// nil = 还没写入（主界面刚挂载、启动深链抢在权限同步之前）：此时不拦，交给页面与后端兜底
    var permissions: Permissions?

    /// 路由守卫（同 Web `accessiblePathFor` 与设置页的越权回退）：
    /// - 成员打开仅管理员可见的设置分区 → 改去「个人信息」（Web settings-view 的 replace 到 /settings/profile）；
    /// - 其余越权页面（AI 会话、无能力的订阅/搜索、活动、媒体库管理）→ 落到媒体库首页。
    /// 界面上本就不给这些入口，守卫兜的是通知、AI 卡片、深链等「从别处跳过来」的情况。
    func guarded(_ route: AppRoute) -> AppRoute {
        guard let permissions, !permissions.allows(route) else { return route }
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
        // 切到路由归属的标签（该标签对当前账号不可见时——例如成员没有订阅页——留在当前标签）
        if let target = route.tab, availableTabs.contains(target) { selectedTab = target }
        // 设置分区的返回固定回设置列表（Web app-shell：/settings/[x] 的返回是 /settings）：
        // 从通知「去处理」、更多页「新版本」等处直达分区时，栈顶不是设置列表就先垫一层
        if case .settingsSection = route, paths[selectedTab]?.last != .settings {
            paths[selectedTab, default: []].append(.settings)
        }
        paths[selectedTab, default: []].append(route)
    }

    /// 当前账号可见的标签（由 MainTabView 按权限写入）
    var availableTabs: Set<MainTab> = Set(MainTab.allCases)

    /// 打开 Web 站内链接；解析失败返回 false
    @discardableResult
    func open(webPath: String) -> Bool {
        // 站内播放链接：/play/... 直接起播；/s/{slug}/play/... 先开分享页，读到影片后由分享页接着起播
        if let request = PlayRequest(webPath: webPath) {
            if let slug = request.shareSlug {
                pendingSharePlay = request
                open(.share(slug: slug))
            } else {
                play(request)
            }
            return true
        }
        guard var route = AppRoute(webPath: webPath) else { return false }
        // 「/」对成员同样收敛到媒体库（Web accessiblePathFor：成员的 / → /library）
        if let permissions, !permissions.isAdmin, URLComponents(string: webPath)?.path.split(separator: "/").isEmpty ?? false {
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

    /// 待起播的访客播放链接（`/s/{slug}/play/...`）：分享页读到影片（必要时先过密码）后取走并起播
    var pendingSharePlay: PlayRequest?

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
        case .my: .more
        default: nil
        }
    }
}
