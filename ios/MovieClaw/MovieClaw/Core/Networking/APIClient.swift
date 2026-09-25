import Foundation

/// 业务接口统一错误。
///
/// 后端的业务错误已经是可读中文（`message` 字段），原样透出给用户；
/// 网络层失败（断网、超时、证书）在这里换成能行动的中文，
/// 与 Web 端 `lib/http.ts` 的兜底文案保持一致，避免用户对着英文系统错误猜原因。
nonisolated enum APIError: LocalizedError, Sendable {
    /// 服务器返回了非 2xx；message 取自响应体 `message`，没有则给通用文案
    case http(status: Int, message: String, code: String?)
    /// 连不上 / 被中断
    case network(String)
    /// 超时
    case timeout
    /// 响应不是预期的 JSON 结构（多半是两端版本不一致）
    case decoding(String)

    var errorDescription: String? {
        switch self {
        case let .http(_, message, _): message
        case let .network(message): message
        case .timeout: "服务器响应太慢，请求已取消——请稍后重试"
        case let .decoding(detail): "服务器返回的数据无法解析（App 与服务器版本可能不一致）：\(detail)"
        }
    }

    var status: Int? {
        if case let .http(status, _, _) = self { return status }
        return nil
    }

    var isUnauthorized: Bool { status == 401 }
}

/// 后端统一响应信封：`success / code / message / data`（见 CLAUDE.md「项目约定」）。
nonisolated struct APIEnvelope<T: Decodable & Sendable>: Decodable, Sendable {
    let success: Bool
    let code: String?
    let message: String?
    let data: T
}

/// 没有 data 的接口（或调用方不关心 data）用它占位解码。
nonisolated struct Empty: Codable, Sendable {}

/// 错误响应体：`success / code / message / details`
private nonisolated struct APIErrorBody: Decodable {
    let message: String?
    let code: String?
}

/// 通知：任何业务接口返回 401（会话过期、被踢下线、密码被改）。
/// 由 AppModel 监听后回到登录页，对应 Web 端 `redirectToLoginOn401`。
nonisolated extension Notification.Name {
    static let apiUnauthorized = Notification.Name("MovieClaw.apiUnauthorized")
}

/// MovieClaw 业务接口客户端。
///
/// 认证方式与 Web 端完全相同：登录接口种下 HttpOnly 会话 Cookie，之后每个请求
/// 由 URLSession 自动携带（共享 `HTTPCookieStorage`，持久化到磁盘，重启 App 不掉登录）。
/// 这样多账号切换（`movieclaw_accounts` Cookie 账号袋）、会话吊销、改密强制下线
/// 等行为与浏览器一模一样，后端无需为 App 另开一套令牌体系。
///
/// 路径约定：调用方传 `/auth/me` 这种以 `/` 开头、相对 `/api/v1` 的路径，
/// 与 Web 端 `lib/api/*.ts` 里的写法逐字一致，方便对照移植。
nonisolated struct APIClient: Sendable {
    let server: ServerAddress
    let session: URLSession

    /// 统一的 JSON 编解码器。不设 key 策略：生成的模型都显式写了 CodingKeys
    /// （snake_case ↔ camelCase），而 convertFromSnakeCase 会连字典的键一起改写。
    static let decoder = JSONDecoder()
    static let encoder = JSONEncoder()

    /// App 全局共用的 URLSession：共享 Cookie 存储、接受并回写 Cookie。
    static let sharedSession: URLSession = {
        let config = URLSessionConfiguration.default
        config.httpCookieStorage = .shared
        config.httpShouldSetCookies = true
        config.httpCookieAcceptPolicy = .always
        config.waitsForConnectivity = false
        config.timeoutIntervalForRequest = 60
        return URLSession(configuration: config)
    }()

    init(server: ServerAddress, session: URLSession = APIClient.sharedSession) {
        self.server = server
        self.session = session
    }

    // MARK: - 地址

    func url(_ path: String, query: [URLQueryItem] = []) -> URL {
        let trimmed = path.hasPrefix("/") ? String(path.dropFirst()) : path
        // path 里可能自带查询串（与 Web 端写法一致），拆开后再合并 query
        let parts = trimmed.split(separator: "?", maxSplits: 1).map(String.init)
        var components = URLComponents(url: server.apiBase.appending(path: parts[0]), resolvingAgainstBaseURL: false)!
        var items: [URLQueryItem] = []
        if parts.count > 1 {
            items += URLComponents(string: "?\(parts[1])")?.queryItems ?? []
        }
        items += query
        if !items.isEmpty { components.queryItems = items }
        return components.url!
    }

    // MARK: - 请求

    /// 发请求并拆信封，返回 `data`。
    func send<T: Decodable & Sendable>(
        _ method: String = "GET",
        _ path: String,
        query: [URLQueryItem] = [],
        body: (any Encodable & Sendable)? = nil,
        timeout: TimeInterval? = nil,
        as type: T.Type = T.self
    ) async throws -> T {
        let envelope: APIEnvelope<T> = try await raw(method, path, query: query, body: body, timeout: timeout)
        return envelope.data
    }

    /// 不拆信封（少数接口如 `/health` 直接返回对象）。
    func raw<T: Decodable & Sendable>(
        _ method: String = "GET",
        _ path: String,
        query: [URLQueryItem] = [],
        body: (any Encodable & Sendable)? = nil,
        timeout: TimeInterval? = nil
    ) async throws -> T {
        var request = URLRequest(url: url(path, query: query))
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let timeout { request.timeoutInterval = timeout }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try Self.encoder.encode(body)
        }
        let data = try await perform(request)
        if T.self == Empty.self, data.isEmpty { return Empty() as! T }
        do {
            return try Self.decoder.decode(T.self, from: data)
        } catch {
            throw APIError.decoding(Self.describe(error))
        }
    }

    /// multipart 上传（头像、字幕、种子文件等）。
    func upload<T: Decodable & Sendable>(
        _ path: String,
        fields: [String: String] = [:],
        file: (name: String, filename: String, mimeType: String, data: Data),
        as type: T.Type = T.self
    ) async throws -> T {
        let boundary = "MovieClaw-\(UUID().uuidString)"
        var body = Data()
        for (key, value) in fields {
            body.append("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(key)\"\r\n\r\n\(value)\r\n")
        }
        body.append("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(file.name)\"; filename=\"\(file.filename)\"\r\n")
        body.append("Content-Type: \(file.mimeType)\r\n\r\n")
        body.append(file.data)
        body.append("\r\n--\(boundary)--\r\n")

        var request = URLRequest(url: url(path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        let data = try await perform(request)
        do {
            return try Self.decoder.decode(APIEnvelope<T>.self, from: data).data
        } catch {
            throw APIError.decoding(Self.describe(error))
        }
    }

    /// 执行请求、统一处理错误；返回响应体原始字节。
    func perform(_ request: URLRequest) async throws -> Data {
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
        guard let http = response as? HTTPURLResponse else {
            throw APIError.network("服务器响应异常")
        }
        // 服务器改动了 Cookie（登录、切换账号、续期、退出）→ 同步备份到钥匙串
        if http.value(forHTTPHeaderField: "Set-Cookie") != nil {
            CookieVault.save(for: server)
        }
        guard (200 ..< 300).contains(http.statusCode) else {
            let body = try? Self.decoder.decode(APIErrorBody.self, from: data)
            let message = body?.message ?? "请求失败（HTTP \(http.statusCode)）"
            if http.statusCode == 401, !Self.isAuthEndpoint(request.url) {
                NotificationCenter.default.post(name: .apiUnauthorized, object: nil)
            }
            throw APIError.http(status: http.statusCode, message: message, code: body?.code)
        }
        return data
    }

    /// 登录 / 初始化接口本身的 401 是「密码错误」，不能当会话过期处理。
    private static func isAuthEndpoint(_ url: URL?) -> Bool {
        guard let path = url?.path else { return false }
        return path.hasSuffix("/auth/login") || path.hasSuffix("/auth/bootstrap")
    }

    static func networkMessage(_ error: URLError) -> String {
        switch error.code {
        case .notConnectedToInternet: "设备未联网，请检查网络后重试"
        case .cannotFindHost, .dnsLookupFailed: "找不到该服务器，请检查地址是否正确"
        case .cannotConnectToHost: "无法连接到服务器：地址或端口不对，或服务器未启动"
        case .networkConnectionLost: "网络连接中断，请重试"
        case .secureConnectionFailed, .serverCertificateUntrusted, .serverCertificateHasBadDate,
             .serverCertificateNotYetValid, .serverCertificateHasUnknownRoot:
            "HTTPS 证书校验失败，请检查服务器证书，或改用 http 地址"
        default: "网络中断或请求失败，请检查连接后重试（\(error.code.rawValue)）"
        }
    }

    static func describe(_ error: Error) -> String {
        switch error {
        case let DecodingError.keyNotFound(key, ctx):
            "缺少字段 \(key.stringValue)（\(ctx.codingPath.map(\.stringValue).joined(separator: "."))）"
        case let DecodingError.typeMismatch(_, ctx), let DecodingError.valueNotFound(_, ctx):
            "字段类型不符 \(ctx.codingPath.map(\.stringValue).joined(separator: "."))"
        case let DecodingError.dataCorrupted(ctx):
            "数据损坏 \(ctx.codingPath.map(\.stringValue).joined(separator: "."))"
        default: error.localizedDescription
        }
    }
}

private nonisolated extension Data {
    mutating func append(_ string: String) {
        append(Data(string.utf8))
    }
}
