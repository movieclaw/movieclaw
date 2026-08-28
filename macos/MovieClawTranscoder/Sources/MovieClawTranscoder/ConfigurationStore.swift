import Foundation

struct WorkerSettingsDraft: Sendable {
    let nasURL: String
    let workerToken: String
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
final class ConfigurationStore: @unchecked Sendable {
    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    func snapshot() throws -> WorkerSettingsSnapshot {
        let tokenConfigured = try KeychainStore.readToken()?.isEmpty == false
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
            nasURL: defaults.string(forKey: Keys.nasURL) ?? "",
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
        guard let token = try KeychainStore.readToken() else {
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

    @discardableResult
    func save(_ draft: WorkerSettingsDraft) throws -> WorkerConfiguration {
        let currentToken = try KeychainStore.readToken() ?? ""
        let token = draft.workerToken.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? currentToken
            : draft.workerToken
        let configuration = try WorkerConfiguration.make(
            nasText: draft.nasURL,
            token: token,
            workerID: draft.workerID,
            ffmpegPath: draft.ffmpegPath,
            maxJobs: draft.maxJobs
        )
        try KeychainStore.saveToken(configuration.workerToken)
        defaults.set(configuration.nasURL.absoluteString, forKey: Keys.nasURL)
        defaults.set(configuration.workerID, forKey: Keys.workerID)
        defaults.set(configuration.ffmpegPath, forKey: Keys.ffmpegPath)
        let managedPath = defaults.string(forKey: Keys.managedFFmpegPath)
        defaults.set(
            managedPath == configuration.ffmpegPath ? FFmpegSource.managed.rawValue : FFmpegSource.custom.rawValue,
            forKey: Keys.ffmpegSource
        )
        defaults.set(configuration.maxJobs, forKey: Keys.maxJobs)
        defaults.set(draft.autoConnect, forKey: Keys.autoConnect)
        return configuration
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
    }
}
