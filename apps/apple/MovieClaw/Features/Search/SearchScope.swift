import Foundation
import SwiftUI

/// 搜索的共享规则（对应 Web `lib/categories.ts`、`lib/search-url.ts`、`lib/search-access.ts`）。

/// 搜索垂直：影视条目 / 站点资源 / 媒体库
enum SearchVertical: String, CaseIterable, Hashable {
    case media, torrent, library

    var label: String {
        switch self {
        case .media: "影视"
        case .torrent: "站点资源"
        case .library: "媒体库"
        }
    }

    /// 搜索面板里的短名
    var shortLabel: String {
        switch self {
        case .media: "影视"
        case .torrent: "资源"
        case .library: "媒体库"
        }
    }

    /// 路由 `tab` 参数 → 垂直（缺省 = 站点资源，与 Web 老链接一致）
    init(routeTab: String?) {
        switch routeTab {
        case "media": self = .media
        case "library": self = .library
        default: self = .torrent
        }
    }

    /// 垂直 → 路由 `tab` 参数（站点资源不带）
    var routeTab: String? { self == .torrent ? nil : rawValue }
}

/// 种子一级分类（值与后端 TorrentCategory 逐一对应）
nonisolated enum TorrentCategories {
    static let all: [(value: String, label: String)] = [
        ("movie", "电影"), ("tv", "剧集"), ("documentary", "纪录片"), ("anime", "动漫"),
        ("music", "音乐"), ("game", "游戏"), ("av", "成人"), ("other", "其他"),
    ]

    static func label(_ value: String) -> String {
        all.first { $0.value == value }?.label ?? value
    }

    /// 分类图标（SF Symbols）；「全部分类」用 `allSymbol`
    static func symbol(_ value: String) -> String {
        switch value {
        case "movie": "film"
        case "tv": "tv"
        case "documentary": "binoculars"
        case "anime": "sparkles"
        case "music": "music.note"
        case "game": "gamecontroller"
        case "av": "eye.slash"
        default: "shippingbox"
        }
    }

    static let allSymbol = "square.grid.2x2"
}

/// 一次搜索的范围：分类组合 × 站点组合；label 仅用于展示与历史（nil = 「全部」）
nonisolated struct SearchScope: Hashable, Sendable {
    var label: String?
    var categories: [String] = []
    var siteIds: [String] = []
    /// 结果页图览模式的初始值（自定义分类可设定）
    var posterMode = false
    /// 无痕搜索：不写入搜索历史
    var skipHistory = false

    static let all = SearchScope()

    /// 编码进路由 `SearchQuery.scope`（查询串格式，与 Web URL 参数同名：label / cats / sites / poster / private）。
    /// 「全部」编码为 nil。
    var encoded: String? {
        guard self != .all else { return nil }
        var components = URLComponents()
        var items: [URLQueryItem] = []
        if let label { items.append(URLQueryItem(name: "label", value: label)) }
        if !categories.isEmpty { items.append(URLQueryItem(name: "cats", value: categories.joined(separator: ","))) }
        if !siteIds.isEmpty { items.append(URLQueryItem(name: "sites", value: siteIds.joined(separator: ","))) }
        if posterMode { items.append(URLQueryItem(name: "poster", value: "1")) }
        if skipHistory { items.append(URLQueryItem(name: "private", value: "1")) }
        components.queryItems = items
        return components.percentEncodedQuery
    }

    /// 从路由 `SearchQuery.scope` 还原；未知分类静默丢弃（同 Web parseSearchQuery）
    init(encoded raw: String?) {
        guard let raw, !raw.isEmpty else { self = .all; return }
        var components = URLComponents()
        components.percentEncodedQuery = raw.hasPrefix("?") ? String(raw.dropFirst()) : raw
        var query: [String: String] = [:]
        for item in components.queryItems ?? [] { query[item.name] = item.value ?? "" }
        let known = Set(TorrentCategories.all.map(\.value))
        label = query["label"].flatMap { $0.isEmpty ? nil : $0 }
        categories = (query["cats"] ?? "").split(separator: ",").map(String.init).filter { known.contains($0) }
        siteIds = (query["sites"] ?? "").split(separator: ",").map(String.init)
        posterMode = query["poster"] == "1"
        skipHistory = query["private"] == "1"
    }

    init(label: String? = nil, categories: [String] = [], siteIds: [String] = [], posterMode: Bool = false, skipHistory: Bool = false) {
        self.label = label
        self.categories = categories
        self.siteIds = siteIds
        self.posterMode = posterMode
        self.skipHistory = skipHistory
    }

    /// 流式搜索的查询参数（同 Web searchParamsOf）
    func queryItems(keyword: String, page: Int = 1) -> [URLQueryItem] {
        var items = [URLQueryItem(name: "keyword", value: keyword)]
        items += categories.map { URLQueryItem(name: "categories", value: $0) }
        items += siteIds.map { URLQueryItem(name: "sites", value: $0) }
        if let label { items.append(URLQueryItem(name: "label", value: label)) }
        if skipHistory { items.append(URLQueryItem(name: "no_history", value: "true")) }
        if posterMode { items.append(URLQueryItem(name: "poster_mode", value: "true")) }
        if page > 1 { items.append(URLQueryItem(name: "page", value: String(page))) }
        return items
    }

    /// 供集成方把 Web 站内链接 `/search?label=&cats=&sites=&poster=&private=` 折叠进 `SearchQuery.scope`
    static func encode(fromWebQuery query: [String: String]) -> String? {
        var components = URLComponents()
        components.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
        return SearchScope(encoded: components.percentEncodedQuery).encoded
    }
}

/// 结果页回到搜索首页改词重搜时带回去的内容：关键词、所在垂直、站点资源的范围（其余垂直为 nil）
struct SearchDraft: Equatable {
    var keyword: String
    var mode: SearchVertical
    var scope: SearchScope?
}

/// 搜索标签：内置分类或自定义「分类 × 站点」预设（后端 `GET /search/presets`）
enum SearchTab: Hashable {
    case category(id: String, visible: Bool)
    case preset(id: String, name: String, visible: Bool, categories: [String], siteIds: [String], posterMode: Bool, skipHistory: Bool)

    /// 选中态 key：类型加前缀，内置分类与预设 id 不会互撞
    var key: String {
        switch self {
        case let .category(id, _): "category:\(id)"
        case let .preset(id, _, _, _, _, _, _): "preset:\(id)"
        }
    }

    var visible: Bool {
        switch self {
        case let .category(_, visible): visible
        case let .preset(_, _, visible, _, _, _, _): visible
        }
    }

    var label: String {
        switch self {
        case let .category(id, _): TorrentCategories.label(id)
        case let .preset(_, name, _, _, _, _, _): name
        }
    }

    /// 标签 → 搜索范围（内置分类 = 单分类 × 全部站点）
    var scope: SearchScope {
        switch self {
        case let .category(id, _):
            SearchScope(label: TorrentCategories.label(id), categories: [id])
        case let .preset(_, name, _, categories, siteIds, posterMode, skipHistory):
            SearchScope(label: name, categories: categories, siteIds: siteIds, posterMode: posterMode, skipHistory: skipHistory)
        }
    }

    init?(_ json: API.JSONValue) {
        guard let type = json["type"]?.stringValue, let id = json["id"]?.stringValue else { return nil }
        let visible = json["visible"]?.boolValue ?? true
        if type == "category" {
            self = .category(id: id, visible: visible)
        } else if type == "preset" {
            self = .preset(
                id: id,
                name: json["name"]?.stringValue ?? id,
                visible: visible,
                categories: json["categories"]?.arrayValue?.compactMap(\.stringValue) ?? [],
                siteIds: json["site_ids"]?.arrayValue?.compactMap(\.stringValue) ?? [],
                posterMode: json["poster_mode"]?.boolValue ?? false,
                skipHistory: json["skip_history"]?.boolValue ?? false
            )
        } else {
            return nil
        }
    }

    /// 列表 / 菜单里的图标：内置分类各有一个，自定义预设统一用「叠放」表示组合范围
    var symbol: String {
        switch self {
        case let .category(id, _): TorrentCategories.symbol(id)
        case .preset: "square.stack.3d.up"
        }
    }

    /// 预设的范围摘要（同设置页预设行）：分类组合 · 站点组合（· 图览 · 无痕）；内置分类没有摘要
    var summary: String? {
        guard case let .preset(_, _, _, categories, siteIds, posterMode, skipHistory) = self else { return nil }
        let cats = categories.isEmpty ? "不限分类" : categories.map(TorrentCategories.label).joined(separator: "、")
        let sites = siteIds.isEmpty ? "全部站点" : "\(siteIds.count) 个站点"
        return "\(cats) · \(sites)\(posterMode ? " · 图览" : "")\(skipHistory ? " · 无痕" : "")"
    }

    var isPreset: Bool {
        if case .preset = self { return true }
        return false
    }

    /// 默认标签（与后端 default_search_tabs 一致）：常用四类可见
    static let defaults: [SearchTab] = [
        ("movie", true), ("tv", true), ("documentary", true), ("anime", true),
        ("music", false), ("game", false), ("av", false), ("other", false),
    ].map { .category(id: $0.0, visible: $0.1) }
}

/// 搜索标签偏好：管理员从服务端读取（设置里可排序/显隐/自建预设），成员与拉取失败时用内置默认
enum SearchTabs {
    static func visible(api: APIClient, isAdmin: Bool) async -> [SearchTab] {
        guard isAdmin, let list = try? await api.searchPresetsList() else {
            return SearchTab.defaults.filter(\.visible)
        }
        let tabs = list.presets.compactMap(SearchTab.init)
        return (tabs.isEmpty ? SearchTab.defaults : tabs).filter(\.visible)
    }
}

/// 搜索入口权限（同 Web `useSearchAccess`）：影视 = 能订阅；站点资源 = 能搜资源；
/// 媒体库 = 管理员或至少一个可见库。
struct SearchAccess: Equatable {
    var canMedia = false
    var canTorrent = false
    var canLibrary = false
    var ready = false

    var available: [SearchVertical] {
        SearchVertical.allCases.filter {
            switch $0 {
            case .media: canMedia
            case .torrent: canTorrent
            case .library: canLibrary
            }
        }
    }

    static func resolve(api: APIClient, permissions: Permissions) async -> SearchAccess {
        var access = SearchAccess(canMedia: permissions.canSubscribe, canTorrent: permissions.canSearch, canLibrary: permissions.isAdmin, ready: true)
        if !permissions.isAdmin {
            access.canLibrary = ((try? await api.libraryList(scope: "all")) ?? []).isEmpty == false
        }
        return access
    }
}
