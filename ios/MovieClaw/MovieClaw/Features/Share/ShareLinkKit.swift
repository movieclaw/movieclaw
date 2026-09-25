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

/// 访客页的请求通道：与生成接口同路径同解码，唯一差别是 **401 不当作会话过期**。
///
/// 为什么要单开：`APIClient.perform` 把除登录外的一切 401 广播成「会话过期」并退回登录页，
/// 而分享密码输错时后端回的正是 401（`SHARE_PASSWORD_INVALID`）——走通用通道的话，
/// 访客输错一次密码 App 就把自己登出了。这里自己发请求、自己拆信封，错误文案照样取后端的中文 message。
/// 错误响应体：`success / code / message / details`
private nonisolated struct ShareGuestErrorBody: Decodable {
    let message: String?
    let code: String?
}

nonisolated extension APIClient {
    func shareGuest<T: Decodable & Sendable>(
        _ method: String, _ path: String, query: [URLQueryItem] = [], body: (any Encodable & Sendable)? = nil
    ) async throws -> T {
        var request = URLRequest(url: url(path, query: query))
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try Self.encoder.encode(body)
        }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch let error as URLError {
            switch error.code {
            case .timedOut: throw APIError.timeout
            case .cancelled: throw CancellationError()
            default: throw APIError.network(Self.networkMessage(error))
            }
        }
        guard let http = response as? HTTPURLResponse else { throw APIError.network("服务器响应异常") }
        guard (200 ..< 300).contains(http.statusCode) else {
            let parsed = try? Self.decoder.decode(ShareGuestErrorBody.self, from: data)
            throw APIError.http(status: http.statusCode, message: parsed?.message ?? "请求失败（HTTP \(http.statusCode)）", code: parsed?.code)
        }
        do {
            return try Self.decoder.decode(APIEnvelope<T>.self, from: data).data
        } catch {
            throw APIError.decoding(Self.describe(error))
        }
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
