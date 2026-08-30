import Foundation

/// 设置窗保存的东西。
///
/// **没有令牌字段**：令牌不是用户填的，是配对流程拿回来的
/// （docs/design/device-auth.md §5），由 `saveToken` 单独写进钥匙串。
struct WorkerSettingsDraft: Sendable {
    let nasURL: String
    let workerID: String
    let ffmpegPath: String
    let maxJobs: Int
    let autoConnect: Bool
}

struct WorkerSettingsSnapshot: Sendable {
    let nasURL: String
    let workerID: String
    let ffmpegPath: String
    let ffmpegSource: FFmpegSource
    let managedFFmpegPath: String?
    let managedFFmpegVersion: String?
    let maxJobs: Int
    let autoConnect: Bool
    let tokenConfigured: Bool
    let startupDownloadPromptDismissed: Bool
}

/// 非敏感配置使用 UserDefaults，令牌只通过 KeychainStore 读写。
///
/// 地址与令牌的生命周期是分开的：改地址不动令牌（同一台 NAS 换了入口），
/// 重新配对只换令牌（换机器或被吊销后）。把两者塞进一次保存会让「改个端口
/// 结果掉线了」这种事无法解释。
///
/// ## 钥匙串一次启动最多读一次
///
/// 这里原来每次 `snapshot()` 都去读一次钥匙串，只为算出「配没配过令牌」这个
/// 布尔值；而 AppMain 里有九处在调 `snapshot()`（启动、ffmpeg 检查、开设置窗、
/// 应用配置、诊断信息……），`loadConfiguration()` 更是一次调用读两遍。
///
/// 钥匙串**每一次读取都可能弹窗**（见 `KeychainStore` 顶部关于代码签名的说明），
/// 于是「打开一次 App 被问五六遍密码」。真正需要令牌明文的只有一个地方：
/// 要拿它去连服务端的时候。所以：
///
/// * 「配没配过」记在 UserDefaults 里——它不是秘密，没有理由为它敲钥匙串；
/// * 令牌明文本身读到就在进程内缓存，同一次运行不重复读。
final class ConfigurationStore: @unchecked Sendable {
    private let defaults: UserDefaults
    /// 令牌明文的进程内缓存。`loaded` 单独存，因为「读过了，结果是没有」和
    /// 「还没读过」必须分得开——否则每次都会重读一遍。
    private var cachedTokenLoaded = false
    private var cachedToken: String?
    private let lock = NSLock()

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    /// 有没有配过令牌。**不读钥匙串**，只看 UserDefaults 里的标记。
    ///
    /// 标记只是布尔值，泄露它不泄露任何秘密。老版本没写过它，需要补一次；
    /// 但补的时候有个前提：
    ///
    /// **没配过服务器地址就绝不去敲钥匙串。** 没有地址就不可能配过令牌，答案
    /// 是确定的 false，没有任何理由去问。而钥匙串条目在 App 被删掉之后仍然
    /// 留在系统里——全新安装的用户如果机器上还留着上一次的残条，一启动就会
    /// 被弹一个「请输入登录钥匙串密码」，而他什么都还没做过，完全无从理解。
    private func tokenConfigured(hasServer: Bool) throws -> Bool {
        if let flag = defaults.object(forKey: Keys.tokenConfigured) as? Bool {
            return flag
        }
        guard hasServer else {
            defaults.set(false, forKey: Keys.tokenConfigured)
            return false
        }
        return try readTokenOnce()?.isEmpty == false
    }

    /// 读令牌明文，一个进程内只真读一次。
    private func readTokenOnce() throws -> String? {
        lock.lock()
        defer { lock.unlock() }
        if cachedTokenLoaded {
            return cachedToken
        }
        let token = try KeychainStore.readToken()
        cachedToken = token
        cachedTokenLoaded = true
        // 顺手校正标记。用户在「钥匙串访问」里手工删掉那条记录时，标记会停在
        // true，界面就会显示「已授权」却怎么也连不上。真读过一次，以钥匙串为准。
        defaults.set(token?.isEmpty == false, forKey: Keys.tokenConfigured)
        return token
    }

    /// 令牌变了（写入或清除）之后同步缓存与标记。
    private func rememberToken(_ token: String?) {
        lock.lock()
        cachedToken = token
        cachedTokenLoaded = true
        lock.unlock()
        defaults.set(token?.isEmpty == false, forKey: Keys.tokenConfigured)
    }

    func snapshot() throws -> WorkerSettingsSnapshot {
        let nasURL = defaults.string(forKey: Keys.nasURL) ?? ""
        let tokenConfigured = try tokenConfigured(hasServer: !nasURL.isEmpty)
        let managedPath = defaults.string(forKey: Keys.managedFFmpegPath)
        let storedPath = defaults.string(forKey: Keys.ffmpegPath)
        let storedSource = defaults.string(forKey: Keys.ffmpegSource)
        let source = FFmpegSource(rawValue: storedSource ?? "")
            ?? (managedPath != nil && managedPath == storedPath ? .managed : .custom)
        let activePath: String
        if source == .managed, let managedPath {
            activePath = managedPath
        } else {
            activePath = storedPath ?? WorkerConfiguration.defaultFFmpegPath()
        }
        return WorkerSettingsSnapshot(
            nasURL: nasURL,
            workerID: defaults.string(forKey: Keys.workerID) ?? WorkerConfiguration.defaultWorkerID(),
            ffmpegPath: activePath,
            ffmpegSource: source,
            managedFFmpegPath: managedPath,
            managedFFmpegVersion: defaults.string(forKey: Keys.managedFFmpegVersion),
            maxJobs: max(1, min(4, defaults.integer(forKey: Keys.maxJobs) == 0 ? 1 : defaults.integer(forKey: Keys.maxJobs))),
            autoConnect: defaults.object(forKey: Keys.autoConnect) as? Bool ?? true,
            tokenConfigured: tokenConfigured,
            startupDownloadPromptDismissed: defaults.bool(forKey: Keys.startupDownloadPromptDismissed)
        )
    }

    func loadConfiguration() throws -> WorkerConfiguration? {
        let snapshot = try snapshot()
        guard !snapshot.nasURL.isEmpty, snapshot.tokenConfigured else {
            return nil
        }
        guard let token = try readTokenOnce() else {
            return nil
        }
        return try WorkerConfiguration.make(
            nasText: snapshot.nasURL,
            token: token,
            workerID: snapshot.workerID,
            ffmpegPath: snapshot.ffmpegPath,
            maxJobs: snapshot.maxJobs
        )
    }

    /// 保存非敏感设置。返回可运行的配置；尚未配对时返回 nil。
    @discardableResult
    func save(_ draft: WorkerSettingsDraft) throws -> WorkerConfiguration? {
        let nasURL = try WorkerConfiguration.normalizedNASURL(draft.nasURL)
        let workerID = try WorkerConfiguration.validatedWorkerID(draft.workerID)
        let ffmpegPath = try WorkerConfiguration.validatedFFmpegPath(draft.ffmpegPath)

        defaults.set(nasURL.absoluteString, forKey: Keys.nasURL)
        defaults.set(workerID, forKey: Keys.workerID)
        defaults.set(ffmpegPath, forKey: Keys.ffmpegPath)
        let managedPath = defaults.string(forKey: Keys.managedFFmpegPath)
        defaults.set(
            managedPath == ffmpegPath ? FFmpegSource.managed.rawValue : FFmpegSource.custom.rawValue,
            forKey: Keys.ffmpegSource
        )
        defaults.set(max(1, min(4, draft.maxJobs)), forKey: Keys.maxJobs)
        defaults.set(draft.autoConnect, forKey: Keys.autoConnect)
        return try loadConfiguration()
    }

    /// 写入配对拿回来的令牌。明文只在这一步经过内存，不落 UserDefaults。
    func saveToken(_ token: String) throws {
        try KeychainStore.saveToken(token)
        rememberToken(token)
    }

    /// 记录已下载版本，但不改变当前正在使用的自定义 ffmpeg。
    func recordManagedFFmpeg(path: String, version: String) {
        defaults.set(path, forKey: Keys.managedFFmpegPath)
        defaults.set(version, forKey: Keys.managedFFmpegVersion)
    }

    /// 将已验证的托管版本切换为当前 Worker 使用的 ffmpeg。
    func activateManagedFFmpeg(path: String, version: String) {
        recordManagedFFmpeg(path: path, version: version)
        defaults.set(path, forKey: Keys.ffmpegPath)
        defaults.set(FFmpegSource.managed.rawValue, forKey: Keys.ffmpegSource)
        defaults.set(true, forKey: Keys.startupDownloadPromptDismissed)
    }

    func dismissStartupDownloadPrompt() {
        defaults.set(true, forKey: Keys.startupDownloadPromptDismissed)
    }

    func clear() throws {
        let managedPath = defaults.string(forKey: Keys.managedFFmpegPath)
        let source = FFmpegSource(rawValue: defaults.string(forKey: Keys.ffmpegSource) ?? "")
        for key in [Keys.nasURL, Keys.workerID, Keys.maxJobs, Keys.autoConnect] {
            defaults.removeObject(forKey: key)
        }
        if source == .managed, let managedPath {
            defaults.set(managedPath, forKey: Keys.ffmpegPath)
            defaults.set(FFmpegSource.managed.rawValue, forKey: Keys.ffmpegSource)
        } else {
            defaults.removeObject(forKey: Keys.ffmpegPath)
            defaults.removeObject(forKey: Keys.ffmpegSource)
        }
        try KeychainStore.deleteToken()
        rememberToken(nil)
    }

    private enum Keys {
        static let nasURL = "movieclaw.nasURL"
        static let workerID = "movieclaw.workerID"
        static let ffmpegPath = "movieclaw.ffmpegPath"
        static let ffmpegSource = "movieclaw.ffmpegSource"
        static let managedFFmpegPath = "movieclaw.managedFFmpegPath"
        static let managedFFmpegVersion = "movieclaw.managedFFmpegVersion"
        static let startupDownloadPromptDismissed = "movieclaw.startupDownloadPromptDismissed"
        static let maxJobs = "movieclaw.maxJobs"
        static let autoConnect = "movieclaw.autoConnect"
        /// 「配过令牌」的布尔标记。存的不是令牌，只是「钥匙串里有没有那一条」，
        /// 免得每次看一眼状态都要去敲钥匙串、招来一次授权弹窗。
        static let tokenConfigured = "movieclaw.tokenConfigured"
    }
}
