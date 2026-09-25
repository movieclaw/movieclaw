import Foundation
import Observation

/// App 顶层状态机：决定此刻显示「连接服务器」「登录」还是主界面。
///
/// 流程（与用户约定的首次启动体验）：
/// 1. 没有保存过服务器 → `.needsServer`：输入服务器页面地址，点「连接」时先测通；
/// 2. 服务器可达 → 查 `/auth/bootstrap`：未初始化则 `.needsSetup`（创建超管，同 Web /setup），
///    已初始化则查 `/auth/me`，Cookie 有效直接进主界面，否则 `.needsLogin`；
/// 3. 登录成功 → `.ready`，之后所有接口都打到这台服务器。
///
/// 任意接口 401（会话过期、被踢下线）都会经 `.apiUnauthorized` 通知把状态打回 `.needsLogin`，
/// 对应 Web 端全站的 401 跳登录页兜底。
@Observable
final class AppModel {
    enum Phase: Equatable {
        /// 启动时正在恢复上次的服务器与会话
        case launching
        case needsServer
        case needsSetup
        case needsLogin
        case ready(SessionView)
    }

    private(set) var phase: Phase = .launching
    /// 当前服务器；为空表示还没配置
    private(set) var server: ServerAddress?
    /// 启动时恢复失败的原因（例如服务器暂时连不上），在登录页顶部提示
    var launchError: String?

    var api: APIClient? { server.map { APIClient(server: $0) } }

    var session: SessionView? {
        if case let .ready(session) = phase { return session }
        return nil
    }

    private static let serverKey = "movieclaw.server.origin"
    private var unauthorizedObserver: (any NSObjectProtocol)?

    init() {
        // UI 自动化测试用：以全新安装的状态启动（清掉服务器地址与全部 Cookie）
        if ProcessInfo.processInfo.arguments.contains("--reset-state") {
            UserDefaults.standard.removeObject(forKey: Self.serverKey)
            HTTPCookieStorage.shared.removeCookies(since: .distantPast)
            CookieVault.clearAll()
        }
        if let saved = UserDefaults.standard.url(forKey: Self.serverKey) {
            let address = ServerAddress(origin: saved)
            server = address
            CookieVault.restore(for: address)
        }
        unauthorizedObserver = NotificationCenter.default.addObserver(
            forName: .apiUnauthorized, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.sessionExpired() }
        }
    }

    /// 冷启动：有服务器就尝试恢复会话，否则进入连接页。
    func restore() async {
        guard let api else {
            phase = .needsServer
            return
        }
        do {
            let status = try await api.bootstrapStatus()
            if !status.initialized {
                phase = .needsSetup
                return
            }
            phase = .ready(try await api.me())
        } catch let error as APIError where error.isUnauthorized {
            phase = .needsLogin
        } catch {
            // 服务器暂时连不上：停在登录页并提示，而不是清掉已保存的地址
            launchError = error.localizedDescription
            phase = .needsLogin
        }
    }

    /// 连接页「连接」按钮：测通后保存地址并决定下一步。
    /// 返回值仅用于测试；失败以抛错形式交给界面展示。
    func connect(to address: ServerAddress) async throws {
        let api = APIClient(server: address)
        let health: HealthResponse
        do {
            health = try await api.health()
        } catch APIError.decoding {
            throw ConnectError.notMovieClaw
        } catch let error as APIError where error.status == 404 {
            throw ConnectError.notMovieClaw
        }
        guard health.status == "ok" else { throw ConnectError.unhealthy(health.status) }
        let status = try await api.bootstrapStatus()

        server = address
        UserDefaults.standard.set(address.origin, forKey: Self.serverKey)
        launchError = nil
        if !status.initialized {
            phase = .needsSetup
        } else if let session = try? await api.me() {
            // 这台服务器之前登录过，Cookie 仍有效
            phase = .ready(session)
        } else {
            phase = .needsLogin
        }
    }

    func login(username: String, password: String, remember: Bool) async throws {
        guard let api else { throw ConnectError.noServer }
        let session = try await api.login(username: username, password: password, remember: remember)
        launchError = nil
        phase = .ready(session)
    }

    func createAdmin(username: String, password: String) async throws {
        guard let api else { throw ConnectError.noServer }
        let session = try await api.createAdmin(username: username, password: password)
        phase = .ready(session)
    }

    /// 改昵称、换头像、切换账号后同步全局会话
    func update(session: SessionView) {
        phase = .ready(session)
    }

    /// 退出当前账号：还有别的已登录账号时自动切过去（与 Web 行为一致）。
    func logout(all: Bool = false) async {
        let next = try? await api?.logout(all: all)
        if let next {
            phase = .ready(next)
        } else {
            phase = .needsLogin
        }
    }

    /// 更换服务器：回到连接页。旧服务器的 Cookie 按域名隔离，保留无害。
    func changeServer() {
        phase = .needsServer
    }

    private func sessionExpired() {
        if case .ready = phase { phase = .needsLogin }
    }

    enum ConnectError: LocalizedError {
        case notMovieClaw
        case unhealthy(String)
        case noServer

        var errorDescription: String? {
            switch self {
            case .notMovieClaw: "该地址能访问，但不是 MovieClaw 服务器（请填写浏览器打开 MovieClaw 时地址栏里的地址）"
            case let .unhealthy(status): "服务器状态异常：\(status)"
            case .noServer: "尚未配置服务器"
            }
        }
    }
}
