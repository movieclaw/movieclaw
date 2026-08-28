import Foundation

enum FFmpegDownloadState: Sendable, Equatable {
    case idle
    case checking
    case downloading(version: String, received: Int64, total: Int64?)
    case installing(version: String)
    case ready(version: String)
    case latest(version: String)
    case cancelled
    case failed(String)

    var isProcessing: Bool {
        switch self {
        case .checking, .downloading, .installing:
            return true
        default:
            return false
        }
    }
}

/// 串行管理 Jellyfin-ffmpeg 的查询、下载和安装，避免重复下载或并发切换版本。
@MainActor
final class FFmpegDownloadManager {
    private(set) var state: FFmpegDownloadState = .idle
    private(set) var managedPath: String?
    private(set) var managedVersion: String?

    var onStateChange: ((FFmpegDownloadState) -> Void)?
    var onInstalled: ((FFmpegInstallation) -> Void)?

    private let releaseClient = FFmpegReleaseClient()
    private var operation: Task<Void, Never>?
    private var downloadSession: URLSession?
    private var downloadTask: URLSessionDownloadTask?
    private var downloadDelegate: FFmpegDownloadDelegate?

    init(managedPath: String? = nil, managedVersion: String? = nil) {
        self.managedPath = managedPath
        self.managedVersion = managedVersion
    }

    var isProcessing: Bool {
        operation != nil || state.isProcessing
    }

    var menuState: FFmpegMenuState {
        switch state {
        case .checking, .downloading, .installing:
            return .processing
        case .failed:
            return .retry(hasManagedVersion: hasManagedInstallation)
        default:
            return hasManagedInstallation ? .update(version: managedVersion) : .download
        }
    }

    func configure(managedPath: String?, managedVersion: String?) {
        self.managedPath = managedPath
        self.managedVersion = managedVersion
        if !state.isProcessing {
            transition(.idle)
        }
    }

    func start() {
        guard operation == nil else { return }
        let previousVersion = managedVersion
        operation = Task { [weak self] in
            await self?.run(previousVersion: previousVersion)
        }
    }

    func cancel() {
        guard operation != nil else { return }
        downloadTask?.cancel()
        downloadSession?.invalidateAndCancel()
        operation?.cancel()
    }

    private var hasManagedInstallation: Bool {
        managedVersion != nil || managedPath != nil
    }

    private func run(previousVersion: String?) async {
        transition(.checking)
        defer {
            downloadTask = nil
            downloadDelegate = nil
            downloadSession = nil
            operation = nil
        }

        do {
            let release = try await releaseClient.fetchLatest()
            try Task.checkCancellation()

            if let managedVersion,
               managedVersion == release.version,
               let managedPath,
               await isUsableManagedFFmpeg(at: managedPath) {
                finish(.latest(version: release.version))
                return
            }

            let archiveURL = try FFmpegInstaller.makeArchiveURL(for: release)
            defer { try? FileManager.default.removeItem(at: archiveURL) }
            transition(.downloading(version: release.version, received: 0, total: release.expectedSize))
            try await download(release: release, to: archiveURL)
            try Task.checkCancellation()

            transition(.installing(version: release.version))
            let installation = try await Task.detached(priority: .utility) {
                try FFmpegInstaller.install(
                    release: release,
                    archiveURL: archiveURL,
                    previousVersion: previousVersion
                )
            }.value

            managedPath = installation.ffmpegPath
            managedVersion = installation.version
            finish(.ready(version: installation.version))
            onInstalled?(installation)
        } catch is CancellationError {
            finish(.cancelled)
        } catch {
            if Task.isCancelled {
                finish(.cancelled)
            } else {
                let message = LogSanitizer.redact(error.localizedDescription)
                AppLogger.shared.error("Jellyfin-ffmpeg 处理失败：\(message)")
                finish(.failed(message))
            }
        }
    }

    private func isUsableManagedFFmpeg(at path: String) async -> Bool {
        await Task.detached(priority: .utility) {
            (try? CapabilityProbe.run(ffmpegPath: path)) != nil
        }.value
    }

    private func download(release: FFmpegRelease, to destination: URL) async throws {
        let request = FFmpegReleaseClient.makeDownloadRequest(for: release)
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            let delegate = FFmpegDownloadDelegate(
                destination: destination,
                onProgress: { [weak self] received, total in
                    Task { @MainActor [weak self] in
                        guard let self else { return }
                        self.updateDownloadProgress(received: received, total: total)
                    }
                },
                onCompletion: { result in
                    continuation.resume(with: result.map { _ in () })
                }
            )
            let configuration = URLSessionConfiguration.ephemeral
            configuration.timeoutIntervalForRequest = 30
            configuration.timeoutIntervalForResource = 30 * 60
            configuration.httpMaximumConnectionsPerHost = 1
            let session = URLSession(configuration: configuration, delegate: delegate, delegateQueue: nil)
            delegate.session = session
            let task = session.downloadTask(with: request)
            self.downloadDelegate = delegate
            self.downloadSession = session
            self.downloadTask = task
            task.resume()
        }
    }

    private func updateDownloadProgress(received: Int64, total: Int64) {
        guard case let .downloading(version, _, currentTotal) = state else { return }
        let expected = total > 0 ? total : currentTotal
        transition(.downloading(version: version, received: received, total: expected))
    }

    private func transition(_ newState: FFmpegDownloadState) {
        state = newState
        onStateChange?(newState)
    }

    private func finish(_ terminalState: FFmpegDownloadState) {
        operation = nil
        transition(terminalState)
    }
}
