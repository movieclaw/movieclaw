import SwiftUI

/// App 内所有可压栈的页面，与 Web 路由一一对应（见 docs/design/ios-app/parity-inventory.md）。
///
/// 设计要点：
/// - 路由是**纯数据**（Hashable），放进 NavigationStack 的 path；页面由 `AppRoute.destination` 统一映射，
///   各模块只实现自己的 View，不改这里的枚举——并行开发时互不冲突；
/// - `init?(webPath:)` 能把后端/网页里的站内链接（通知跳转、AI 卡片、订阅详情里的链接）
///   解析成原生路由，避免到处手拼；
/// - 标签页根页面（发现、媒体库首页、订阅列表、活动、更多）不作为路由压栈，由 MainTabView 直接承载；
///   但它们也有对应 case，便于从别处「切到某个标签并定位」。
enum AppRoute: Hashable {
    // MARK: 发现
    /// /discover/{movie|tv}（标签根）
    case discover(kind: String = "movie")
    /// /discover/{type}/collections/{provider}/{collectionId}
    case discoverCollection(kind: String, provider: String, collectionId: String)
    /// /media/{type}/{id} 或 /media/douban/{id}；titleRef 形如 `tmdb:movie:123` / `douban:tv:456`
    case mediaDetail(titleRef: String)
    /// /people/{id}：库内影人
    case person(tmdbId: Int)
    /// /discover/people/{id}：TMDB 影人
    case discoveredPerson(tmdbId: Int)

    // MARK: 媒体库
    /// /library（标签根）
    case libraryHome
    case libraryCustomize
    case favorites
    case allCollections
    /// /library/{id}/c/{cid} 或 /library/c/{cid}
    case collection(libraryId: Int?, collectionId: Int)
    /// /library/{id}?view=collections&pending=1：view 为合集视图，pending 为进页即开待处理抽屉
    case library(id: Int, view: String? = nil, pending: Bool = false)
    /// /library/{id}/item/{mediaItemId}?season=&episode=
    case libraryItem(libraryId: Int, itemId: Int, season: Int? = nil, episode: Int? = nil)
    /// /library/manage?create=1&tab=duplicates&item={mediaItemId}
    case libraryManage(create: Bool = false, tab: String? = nil, item: Int? = nil)

    // MARK: 搜索
    /// 搜索首页（输入框 + 模式 + 最近搜索，对应 Web 的搜索命令面板，没有网页地址）：
    /// 各标签根页右上角的放大镜压栈打开，结果页接着压在同一个栈里
    case searchHome
    /// /search?q=&tab=&scope=&snapshot=&for_sub=
    case search(SearchQuery)

    // MARK: 订阅
    /// /subscriptions（标签根）
    case subscriptions
    /// /subscriptions/{id}?upgrade-run=1
    case subscription(id: Int, upgradeRun: Bool = false)

    // MARK: 活动（管理员）
    /// /activity?view=（标签根）
    case activity(view: String? = nil)

    // MARK: AI 会话（管理员）
    case newSession
    case session(id: String)

    // MARK: 我的 / 设置
    case my
    case settings
    /// /settings/{section}?…：query 原样透传给分区页（如 /settings/app?tab=storage、
    /// /settings/downloaders?limits=… 的预填与直达），分区页经 `@Environment(\.routeQuery)` 读取
    case settingsSection(SettingsSection, query: [String: String] = [:])

    // MARK: 分享（访客页）
    case share(slug: String)

    /// 搜索页参数（对应 /search 的查询串）
    struct SearchQuery: Hashable {
        var q: String = ""
        /// media | torrents | library；nil 表示按权限取第一个可用分区
        var tab: String?
        var scope: String?
        var snapshot: Int?
        /// 手动选种模式：选中的种子直接投给这个订阅
        var forSubscription: Int?
    }
}

/// 设置分区（顺序与分组同 Web `lib/mock-data.ts` 的 settingsSectionGroups）。
/// 网页的「外观」分区（主题、背景图、界面质感、导航顺序）只设置网页本身，App 不提供（用户决定，
/// 已接受的平台差异）；`/settings/appearance` 深链因此落到设置首页。
enum SettingsSection: String, CaseIterable, Hashable, Identifiable {
    case overview, profile
    case members, devices
    case subscription, sites, downloaders, importWatch = "import-watch"
    case scrape, playback
    case imPush = "im-push", webhook, llm, mcp, ai
    case app, network, logs

    var id: String { rawValue }

    var title: String {
        switch self {
        case .overview: "概览"
        case .profile: "个人信息"
        case .members: "成员"
        case .devices: "设备"
        case .subscription: "订阅规则"
        case .sites: "资源站点"
        case .downloaders: "下载器"
        case .importWatch: "自动入库"
        case .scrape: "刮削与整理"
        case .playback: "播放"
        case .imPush: "消息推送"
        case .webhook: "Webhook"
        case .llm: "模型接入"
        case .mcp: "MCP 服务"
        case .ai: "AI 设定"
        case .app: "更新与维护"
        case .network: "网络"
        case .logs: "系统日志"
        }
    }

    var subtitle: String {
        switch self {
        case .overview: "配置状态一览：缺什么、有什么问题、下一步做什么"
        case .profile: "头像、昵称与登录密码"
        case .members: "家庭成员账号、能力开关与可见范围"
        case .devices: "命令行与转码 Worker 的接入审批和吊销"
        case .subscription: "订阅规则组与投递模拟预演"
        case .sites: "站点接入与鉴权、搜索分类、插件 Cookie 同步"
        case .downloaders: "qBittorrent / Transmission 接入"
        case .importWatch: "监听下载目录，下载完成后自动整理进媒体库"
        case .scrape: "海报、简介、命名与目录整理的全局默认"
        case .playback: "远程转码与播放体验"
        case .imPush: "微信 / Telegram / Discord / 飞书 推送与 AI 对话"
        case .webhook: "向外部服务推送播放、收藏等事件"
        case .llm: "接入 OpenAI、百炼等模型供应商，可同时接入多家"
        case .mcp: "把 movieclaw 的能力开放给 Claude Code、Cursor 等 AI 客户端"
        case .ai: "智能体与字幕处理使用的默认模型"
        case .app: "版本更新与应用重启"
        case .network: "代理、镜像与外部访问地址，解决 TMDB 等不可达"
        case .logs: "后端运行日志，按天存档"
        }
    }

    var systemImage: String {
        switch self {
        case .overview: "gauge.with.dots.needle.33percent"
        case .profile: "person.crop.circle"
        case .members: "person.2.badge.key"
        case .devices: "laptopcomputer.and.iphone"
        case .subscription: "bookmark"
        case .sites: "server.rack"
        case .downloaders: "arrow.down.circle"
        case .importWatch: "folder.badge.gearshape"
        case .scrape: "photo.on.rectangle"
        case .playback: "play.rectangle"
        case .imPush: "bubble.left.and.bubble.right"
        case .webhook: "paperplane"
        case .llm: "sparkles"
        case .mcp: "powerplug"
        case .ai: "wand.and.stars"
        case .app: "gearshape.2"
        case .network: "globe"
        case .logs: "terminal"
        }
    }

    /// 成员只能看到「个人信息」，其余分区仅超级管理员可见
    var memberVisible: Bool { self == .profile }

    /// 分组（空标题的组不渲染组头）
    static let groups: [(title: String, items: [SettingsSection])] = [
        ("", [.overview]),
        ("账号", [.profile]),
        ("成员与设备", [.members, .devices]),
        ("资源与下载", [.subscription, .sites, .downloaders, .importWatch]),
        ("媒体库", [.scrape, .playback]),
        ("通知与集成", [.imPush, .webhook, .llm, .mcp, .ai]),
        ("系统", [.app, .network, .logs]),
    ]
}

// MARK: - 站内链接解析

extension AppRoute {
    /// 把 Web 站内路径（可带查询串）解析成原生路由。无法识别返回 nil。
    /// 覆盖 Web 端所有页面，外加旧地址的重定向（/tasks、/settings/search 等）。
    init?(webPath raw: String) {
        guard let components = URLComponents(string: raw.hasPrefix("/") ? raw : "/\(raw)") else { return nil }
        let parts = components.path.split(separator: "/").map(String.init)
        var query: [String: String] = [:]
        for item in components.queryItems ?? [] { query[item.name] = item.value ?? "" }
        func int(_ s: String?) -> Int? { s.flatMap { Int($0) } }

        switch parts.first {
        case nil:
            self = .discover()
        case "discover":
            if parts.count >= 3, parts[1] == "people", let id = int(parts[2]) {
                self = .discoveredPerson(tmdbId: id)
            } else if parts.count >= 5, parts[2] == "collections" {
                self = .discoverCollection(kind: parts[1], provider: parts[3], collectionId: parts[4])
            } else if parts.count == 3, parts[1] == "movie", parts[2] == "top250" {
                self = .discoverCollection(kind: "movie", provider: "douban", collectionId: "movie_top250")
            } else if parts.count == 3, parts[1] == "movie", parts[2] == "high-score" {
                self = .discoverCollection(kind: "movie", provider: "douban", collectionId: "movie_high_score")
            } else {
                let kind = parts.count > 1 ? parts[1] : "movie"
                // 筛选/数据源参数随类型一起带过去（「地址即状态」，见 DiscoverViewpoint）
                if let q = components.percentEncodedQuery, !q.isEmpty {
                    self = .discover(kind: "\(kind)?\(q)")
                } else {
                    self = .discover(kind: kind)
                }
            }
        case "media":
            guard parts.count >= 3 else { return nil }
            self = parts[1] == "douban" ? .mediaDetail(titleRef: "douban:\(parts[2])") : .mediaDetail(titleRef: "tmdb:\(parts[1]):\(parts[2])")
        case "people":
            guard parts.count >= 2, let id = int(parts[1]) else { return nil }
            self = .person(tmdbId: id)
        case "library":
            if parts.count == 1 { self = .libraryHome; return }
            switch parts[1] {
            case "customize": self = .libraryCustomize
            case "favorites": self = .favorites
            case "collections": self = .allCollections
            case "manage": self = .libraryManage(create: query["create"] == "1", tab: query["tab"], item: int(query["item"]))
            case "c":
                guard parts.count >= 3, let cid = int(parts[2]) else { return nil }
                self = .collection(libraryId: nil, collectionId: cid)
            default:
                guard let lib = int(parts[1]) else { return nil }
                if parts.count >= 4, parts[2] == "item", let item = int(parts[3]) {
                    self = .libraryItem(libraryId: lib, itemId: item, season: int(query["season"]), episode: int(query["episode"]))
                } else if parts.count >= 4, parts[2] == "c", let cid = int(parts[3]) {
                    self = .collection(libraryId: lib, collectionId: cid)
                } else {
                    self = .library(id: lib, view: query["view"], pending: query["pending"] == "1")
                }
            }
        case "search":
            // 网页把搜索范围拆成 label/cats/sites/poster/private/browse 多个参数，折叠成一个 scope 串
            let scopeKeys: Set<String> = ["label", "cats", "sites", "poster", "private", "browse"]
            let scopeParams = query.filter { scopeKeys.contains($0.key) }
            self = .search(.init(
                q: query["q"] ?? "", tab: query["tab"],
                scope: query["scope"] ?? (scopeParams.isEmpty ? nil : SearchScope.encode(fromWebQuery: scopeParams)),
                snapshot: int(query["snapshot"]), forSubscription: int(query["for_sub"])
            ))
        case "subscriptions":
            if parts.count >= 2, let id = int(parts[1]) {
                self = .subscription(id: id, upgradeRun: query["upgrade-run"] == "1")
            } else {
                self = .subscriptions
            }
        case "activity", "tasks":
            self = .activity(view: query["view"])
        case "new":
            self = .newSession
        case "sessions":
            guard parts.count >= 2 else { return nil }
            self = .session(id: parts[1])
        case "my":
            self = .my
        case "settings":
            guard parts.count >= 2 else { self = .settings; return }
            switch parts[1] {
            case "search": self = .settingsSection(.sites)
            case "about": self = .settingsSection(.app)
            case "app" where query["tab"] == "remote": self = .settingsSection(.playback)
            default:
                guard let section = SettingsSection(rawValue: parts[1]) else { self = .settings; return }
                self = .settingsSection(section, query: query)
            }
        case "s":
            guard parts.count >= 2 else { return nil }
            self = .share(slug: parts[1])
        default:
            return nil
        }
    }

    /// 该路由归属的标签页（切标签定位用）
    /// nil = 不属于任何标签（搜索、设置、AI 会话、分享等），在当前标签打开
    var tab: MainTab? {
        switch self {
        case .discover, .discoverCollection, .mediaDetail, .person, .discoveredPerson: .discover
        case .libraryHome, .libraryCustomize, .favorites, .allCollections, .collection, .library, .libraryItem, .libraryManage: .library
        case .subscriptions, .subscription: .subscriptions
        case .activity: .activity
        case .my: .more
        case .searchHome, .search, .newSession, .session, .settings, .settingsSection, .share: nil
        }
    }
}
