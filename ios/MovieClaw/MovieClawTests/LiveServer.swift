import Foundation
@testable import MovieClaw

/// 联调用的真实服务器（默认本机 dev 环境）。环境变量：
/// MC_LIVE=1 开启；MC_TEST_SERVER / MC_TEST_USERNAME / MC_TEST_PASSWORD 覆盖默认值。
nonisolated enum LiveServer {
    static let env = ProcessInfo.processInfo.environment.filter { !$0.value.isEmpty }
    static let enabled = env["MC_LIVE"] == "1"

    private static let shared = Task { () throws -> APIClient in
        let address = try ServerAddress(parsing: env["MC_TEST_SERVER"] ?? "http://localhost:3000")
        let client = APIClient(server: address)
        _ = try await client.authLogin(body: .init(
            username: env["MC_TEST_USERNAME"] ?? "admin",
            password: env["MC_TEST_PASSWORD"] ?? "mclaw-dev-2026",
            remember: true
        ))
        return client
    }

    /// 登录一次后复用同一个客户端（Cookie 在共享存储里）
    static func client() async throws -> APIClient {
        try await shared.value
    }
}

nonisolated extension LiveServer {
    /// 只把「解码失败」算作失败：业务错误（如部署形态不支持某功能返回 400）说明接口
    /// 可达、信封能解，与模型是否正确无关。
    static func check<T>(_ call: (APIClient) async throws -> T) async throws {
        do {
            _ = try await call(client())
        } catch let error as APIError {
            if case .decoding = error { throw error }
        }
    }
}
