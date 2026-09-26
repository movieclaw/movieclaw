import Foundation

// 登录相关的少量手写补充；接口本体与模型由 Generated/ 生成。

nonisolated extension API.SessionView {
    var isAdmin: Bool { role == "admin" }
}

nonisolated extension APIClient {
    /// 连接测试专用：短超时，避免填错局域网地址时干等一分钟
    func health() async throws -> API.HealthResponse {
        try await raw("GET", "/health", timeout: 8)
    }

    func bootstrapStatus() async throws -> API.BootstrapStatus {
        try await send("GET", "/auth/bootstrap", timeout: 8)
    }
}
