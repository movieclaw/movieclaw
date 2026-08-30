import Foundation

/// 设备配对客户端（docs/design/device-auth.md §2）。
///
/// 这个类型替代了原来的粘贴式配对码。差别不只是交互形式：
///
/// - 旧做法里网页显示的那串东西**就是令牌本身**，会留在剪贴板、聊天记录和
///   截图里，而且没有兑换与失效的概念；
/// - 新做法里这台 Mac 显示的只是一段五分钟作废的短码，它不是凭据。真正的
///   令牌通过 `deviceCode` 兑换，直接回到本进程，从不显示在任何屏幕上。
///
/// 纯网络层，不碰 UI 也不碰钥匙串——状态机在 `SettingsWindowController`，
/// 落盘在 `ConfigurationStore`。
struct DevicePairing {
    /// 一次接入请求的回执。`userCode` 给人看，`deviceCode` 只有本机持有。
    struct Grant: Equatable {
        let userCode: String
        let deviceCode: String
        let verificationURI: String
        /// 服务端要求的轮询间隔（秒）。不要比它更快。
        let interval: Int
        /// 配对码有效期（秒）。超时就得重新发起。
        let expiresIn: Int
    }

    /// 一次轮询的结论。四种终态各自对应明确的下一步，不做含糊处理。
    enum PollResult: Equatable {
        /// 还没批准，按 interval 继续等。
        case pending
        /// 轮询过快，退避一拍再来。挑战没有作废。
        case slowDown
        /// 拿到令牌了。明文仅此一次，立刻存进钥匙串。
        case granted(token: String, clientName: String)
        /// 被拒绝 / 已过期 / 配对码不存在——**停止轮询**，让用户重新发起。
        case finished(reason: String)
    }

    enum PairingError: LocalizedError {
        case badResponse(String)
        case server(String)

        var errorDescription: String? {
            switch self {
            case let .badResponse(detail):
                return "服务器返回了预期之外的内容：\(detail)"
            case let .server(message):
                return message
            }
        }
    }

    let nasURL: URL
    var session: URLSession = .shared

    /// 探测服务器可达性，返回服务名。地址填错了要当场知道，而不是保存之后
    /// 表现成「连不上」——那时用户已经无从判断是地址错了还是别的问题。
    ///
    /// 只取 service 不取版本号：/health 是匿名端点，为一句文案而向未登录者
    /// 公开精确版本，对自部署用户不是好交易。
    func verifyConnection() async throws -> String {
        let (data, response) = try await send(path: "/api/v1/health", body: nil)
        try ensureSuccess(response, data: data)
        // /health 不走业务信封：后端 health.py 直接返回 HealthResponse
        // （`{status,service,environment,spec_hash}`），根本没有 data 键。
        // 这里若套 envelope() 解，任何一台正常的 movieclaw 都会被判成
        //「服务器返回了预期之外的内容」，验证连接永远不可能通过。
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw PairingError.badResponse("健康检查返回的不是 JSON 对象")
        }
        return root["service"] as? String ?? "服务"
    }

    /// 发起接入请求。只声明自己是什么形态、叫什么名字——能做什么由批准者决定。
    func authorize(clientName: String) async throws -> Grant {
        let (data, response) = try await send(
            path: "/api/v1/auth/device/authorize",
            body: ["client_type": "worker", "client_name": clientName]
        )
        try ensureSuccess(response, data: data)
        let payload = try envelope(data)
        guard
            let userCode = payload["user_code"] as? String,
            let deviceCode = payload["device_code"] as? String,
            let verificationURI = payload["verification_uri"] as? String
        else {
            throw PairingError.badResponse("配对回执缺少必要字段")
        }
        return Grant(
            userCode: userCode,
            deviceCode: deviceCode,
            verificationURI: verificationURI,
            interval: payload["interval"] as? Int ?? 2,
            expiresIn: payload["expires_in"] as? Int ?? 300
        )
    }

    /// 轮询兑换。HTTP 状态码直接映射成四种结论，调用方不需要解析业务码。
    func poll(deviceCode: String) async throws -> PollResult {
        let (data, response) = try await send(
            path: "/api/v1/auth/device/token",
            body: ["device_code": deviceCode]
        )
        guard let http = response as? HTTPURLResponse else {
            throw PairingError.badResponse("非 HTTP 响应")
        }
        switch http.statusCode {
        case 202:
            return .pending
        case 429:
            return .slowDown
        case 200:
            let payload = try envelope(data)
            guard let token = payload["token"] as? String else {
                throw PairingError.badResponse("兑换成功但没有令牌")
            }
            return .granted(token: token, clientName: payload["client_name"] as? String ?? "")
        default:
            return .finished(reason: message(from: data) ?? "配对已结束，请重新发起")
        }
    }

    // MARK: - 内部

    private func send(path: String, body: [String: String]?) async throws -> (Data, URLResponse) {
        var request = URLRequest(url: nasURL.appendingPathComponent(path.trimmingCharacters(in: ["/"])))
        request.httpMethod = body == nil ? "GET" : "POST"
        request.timeoutInterval = 15
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        return try await session.data(for: request)
    }

    private func ensureSuccess(_ response: URLResponse, data: Data) throws {
        guard let http = response as? HTTPURLResponse else {
            throw PairingError.badResponse("非 HTTP 响应")
        }
        guard (200..<300).contains(http.statusCode) else {
            // 服务端的中文 message 原样透传：它比「HTTP 4xx」有用得多
            throw PairingError.server(message(from: data) ?? "服务器返回 HTTP \(http.statusCode)")
        }
    }

    /// 拆 `ApiResponse{success,code,message,data}` 信封，取出 data。
    private func envelope(_ data: Data) throws -> [String: Any] {
        guard
            let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let payload = root["data"] as? [String: Any]
        else {
            throw PairingError.badResponse("响应不是 movieclaw 的标准信封")
        }
        return payload
    }

    private func message(from data: Data) -> String? {
        guard
            let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let text = root["message"] as? String,
            !text.isEmpty
        else {
            return nil
        }
        return text
    }
}
