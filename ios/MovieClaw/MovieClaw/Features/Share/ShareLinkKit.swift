import Foundation

/// 分享链接的纯逻辑（同 Web `lib/share.ts`）：链接补全、复制文本、到期提示。
///
/// 媒体库模块的分享对话框（`LibraryShareSheet`）里有一份私有实现；这里给管理页「分享」页签与
/// 访客页用，口径逐字相同，改文案请三处一起改。
enum ShareLinkKit {
    /// 分享链接补全：后端在未配置外部访问地址时给的是相对路径 `/s/{slug}`，用服务器根地址补成绝对地址
    static func absoluteURL(_ url: String, origin: URL) -> String {
        let lower = url.lowercased()
        if lower.hasPrefix("http://") || lower.hasPrefix("https://") { return url }
        var base = origin.absoluteString
        while base.hasSuffix("/") { base.removeLast() }
        return url.hasPrefix("/") ? base + url : base + "/" + url
    }

    /// 「复制链接和密码」：一段可以直接粘进聊天框的话
    static func copyText(title: String, url: String, password: String?) -> String {
        var parts = ["《\(title)》", "链接：\(url)"]
        if let password { parts.append("密码：\(password)") }
        return parts.joined(separator: " ")
    }

    /// 到期提示：「N 天后失效」/「N 小时后失效」/「N 分钟后失效」/「已失效」
    static func expiryHint(_ expiresAt: String) -> String {
        guard let date = Formatters.date(expiresAt) else { return "已失效" }
        let remaining = date.timeIntervalSinceNow
        if remaining <= 0 { return "已失效" }
        let hours = remaining / 3600
        if hours >= 47 { return "\(Int((hours / 24).rounded())) 天后失效" }
        if hours >= 1 { return "\(Int(hours.rounded(.down))) 小时后失效" }
        return "\(max(1, Int((remaining / 60).rounded(.down)))) 分钟后失效"
    }
}

// MARK: - 访客接口

/// 访客页的请求入口。`/share/*` 的 401 表示「需要分享密码 / 密码错误」，
/// `APIClient` 已把它排除在「会话过期」之外（见 `isAuthEndpoint`），访客输错密码不会把 App 登出。
nonisolated extension APIClient {
    func shareGuest<T: Decodable & Sendable>(
        _ method: String, _ path: String, query: [URLQueryItem] = [], body: (any Encodable & Sendable)? = nil
    ) async throws -> T {
        try await send(method, path, query: query, body: body)
    }

    func shareGuestProbe(slug: String) async throws -> API.SharePublicView {
        try await shareGuest("GET", "/share/\(slug)")
    }

    func shareGuestUnlock(slug: String, password: String) async throws -> API.SharePublicView {
        try await shareGuest("POST", "/share/\(slug)/unlock", body: API.ShareUnlockRequest(password: password))
    }

    func shareGuestItem(slug: String, item: Int?) async throws -> API.SharedItemView {
        try await shareGuest("GET", "/share/\(slug)/item", query: item.map { [URLQueryItem(name: "item", value: String($0))] } ?? [])
    }

    func shareGuestCollection(slug: String) async throws -> API.SharedCollectionView {
        try await shareGuest("GET", "/share/\(slug)/collection")
    }

    func shareGuestEpisodes(slug: String, seasonNumber: Int, item: Int?) async throws -> API.SeasonEpisodesView {
        var query = [URLQueryItem(name: "season_number", value: String(seasonNumber))]
        if let item { query.append(URLQueryItem(name: "item", value: String(item))) }
        return try await shareGuest("GET", "/share/\(slug)/episodes", query: query)
    }
}
