import Foundation

/// 用户输入的「服务器页面地址」→ 规范化后的服务器根地址。
///
/// 设计要点：
/// - 用户最自然的输入是浏览器地址栏里那串，可能带路径（`/login`、`/library/3`）、
///   可能没写协议（`192.168.1.10:3000`）。这里只保留 协议 + 主机 + 端口，
///   因为 Web 端不支持子路径部署，服务根就是站点根；
/// - 没写协议时默认 http：自托管最常见的是局域网 http 直连，
///   公网 https 用户粘贴的地址本来就带 `https://`；
/// - 所有接口都挂在 `<根>/api/v1` 下（与 Web 端 `NEXT_PUBLIC_API_BASE_URL` 默认值一致），
///   Jellyfin 兼容层等其它命名空间不经过这里。
nonisolated struct ServerAddress: Hashable, Codable, Sendable {
    /// 服务器根地址，形如 `http://192.168.1.10:3000`（无尾斜杠、无路径）
    let origin: URL

    /// 业务接口根：`<origin>/api/v1`
    var apiBase: URL { origin.appending(path: "api/v1") }

    /// 展示给用户看的地址（去掉协议前缀里的冗余，保留端口）
    var displayString: String { origin.absoluteString }

    enum ParseError: LocalizedError, Equatable {
        case empty
        case invalid

        var errorDescription: String? {
            switch self {
            case .empty: "请输入服务器地址"
            case .invalid: "地址格式不正确，示例：http://192.168.1.10:3000"
            }
        }
    }

    init(origin: URL) {
        self.origin = origin
    }

    /// 解析用户输入。只做格式层面的规范化，不发起网络请求。
    init(parsing raw: String) throws(ParseError) {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { throw .empty }

        let withScheme = trimmed.contains("://") ? trimmed : "http://\(trimmed)"
        guard
            var components = URLComponents(string: withScheme),
            let scheme = components.scheme?.lowercased(),
            scheme == "http" || scheme == "https",
            let host = components.host, !host.isEmpty
        else { throw .invalid }

        components.scheme = scheme
        components.host = host.lowercased()
        components.path = ""
        components.query = nil
        components.fragment = nil
        components.user = nil
        components.password = nil
        // 显式写了默认端口（http:80 / https:443）时去掉，避免同一台服务器被当成两个地址
        if (scheme == "http" && components.port == 80) || (scheme == "https" && components.port == 443) {
            components.port = nil
        }
        guard let url = components.url else { throw .invalid }
        self.origin = url
    }

    /// 把后端返回的站内相对地址（如 `/api/v1/images/...`）解析成绝对 URL；
    /// 已是绝对地址的原样返回。
    func resolve(_ path: String?) -> URL? {
        guard let path, !path.isEmpty else { return nil }
        if path.hasPrefix("http://") || path.hasPrefix("https://") {
            return URL(string: path)
        }
        return URL(string: path, relativeTo: origin)?.absoluteURL
    }
}
