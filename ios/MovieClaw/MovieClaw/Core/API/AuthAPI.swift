import Foundation

// 对应 Web 端 apps/web/lib/api/auth.ts 与 lib/api/health.ts。

/// `/health` 的响应（不带信封）。
nonisolated struct HealthResponse: Decodable, Sendable {
    let status: String
    let service: String
    let environment: String?
}

/// 能力开关快照：客户端据此裁剪入口；安全边界仍在后端 403。
nonisolated struct SessionCapabilities: Codable, Sendable, Hashable {
    let allowSubscribe: Bool
    let allowSearch: Bool
    let allowDirectDownload: Bool
}

/// 当前登录会话（后端 schemas.auth.SessionView）。
nonisolated struct SessionView: Codable, Sendable, Hashable {
    let username: String
    let nickname: String
    /// 头像相对 URL（含版本号）；未上传过为空
    let avatarUrl: String?
    /// admin=超级管理员；member=成员
    let role: String
    let capabilities: SessionCapabilities

    var isAdmin: Bool { role == "admin" }
}

nonisolated struct BootstrapStatus: Decodable, Sendable {
    let initialized: Bool
}

private nonisolated struct LoginBody: Encodable, Sendable {
    let username: String
    let password: String
    let remember: Bool
}

private nonisolated struct Credentials: Encodable, Sendable {
    let username: String
    let password: String
}

private nonisolated struct LogoutBody: Encodable, Sendable {
    let all: Bool
}

nonisolated extension APIClient {
    func health() async throws -> HealthResponse {
        try await raw("GET", "/health", timeout: 8)
    }

    func bootstrapStatus() async throws -> BootstrapStatus {
        try await send("GET", "/auth/bootstrap", timeout: 8)
    }

    /// 首次初始化：创建超级管理员并自动登录（会话 Cookie 由后端种下）。
    func createAdmin(username: String, password: String) async throws -> SessionView {
        try await send("POST", "/auth/bootstrap", body: Credentials(username: username, password: password))
    }

    /// 登录。remember 为 true 时会话有效期 7 天 → 30 天（同 Web「30 天内记住我」）。
    func login(username: String, password: String, remember: Bool) async throws -> SessionView {
        try await send("POST", "/auth/login", body: LoginBody(username: username, password: password, remember: remember))
    }

    func me() async throws -> SessionView {
        try await send("GET", "/auth/me")
    }

    /// 退出当前账号；还有别的已登录账号时后端自动切过去并返回它，否则返回 nil。
    func logout(all: Bool = false) async throws -> SessionView? {
        try await send("POST", "/auth/logout", body: LogoutBody(all: all))
    }
}
