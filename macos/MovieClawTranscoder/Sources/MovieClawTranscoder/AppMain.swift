import AppKit
import Darwin
import Foundation

@main
struct MovieClawTranscoderMain {
    @MainActor
    static func main() {
        AppLogger.installCrashDiagnostics()
        if CommandLine.arguments.contains("--help") {
            WorkerConfiguration.printUsage()
            return
        }
        if CommandLine.arguments.contains("--headless") {
            HeadlessRunner.run(arguments: CommandLine.arguments)
            return
        }

        let application = NSApplication.shared
        application.setActivationPolicy(.accessory)
        let delegate = MovieClawAppDelegate()
        application.delegate = delegate
        application.run()
    }
}

private enum HeadlessRunner {
    @MainActor
    static func run(arguments: [String]) {
        Task {
            do {
                let configuration = try WorkerConfiguration.load(arguments: arguments)
                if configuration.usesInsecureHTTP {
                    AppLogger.shared.warning(
                        "已启用内网 HTTP 模式：源视频、转码产物和 Worker Token 将以明文传输，"
                        + "请确认 NAS 的 Nginx 映射端口仅允许可信内网访问。"
                    )
                }
                let capabilities = try await Task.detached(priority: .utility) {
                    try CapabilityProbe.run(ffmpegPath: configuration.ffmpegPath)
                }.value
                AppLogger.shared.info(
                    "启动无界面 Worker：\(configuration.workerID)，ffmpeg=\(capabilities.ffmpegVersion)",
                    secret: configuration.workerToken
                )
                await WorkerClient(configuration: configuration, capabilities: capabilities).runForever()
            } catch {
                AppLogger.shared.error("无界面 Worker 启动失败：\(error.localizedDescription)")
                fputs("[MovieClaw] 启动失败：\(error)\n", stderr)
                exit(1)
            }
        }
        dispatchMain()
    }
}

/// 菜单栏 App 的生命周期协调器。
///
/// AppKit 主线程只负责 UI 和配置；能力探测、WebSocket 和 ffmpeg 都在后台
/// Task/WorkerClient actor 中运行，避免网络抖动阻塞菜单栏。
@MainActor
final class MovieClawAppDelegate: NSObject, NSApplicationDelegate {
    private let configurationStore = ConfigurationStore()
    private var menuBar: MenuBarController!
    private var ffmpegManager: FFmpegDownloadManager!
    private var ffmpegWindow: FFmpegDownloadWindowController?
    private var settingsWindow: SettingsWindowController?
    private var workerClient: WorkerClient?
    private var runtimeTask: Task<Void, Never>?
    private var startupCheckTask: Task<Void, Never>?
    private var ffmpegPreparationTask: Task<Void, Never>?
    private var configuration: WorkerConfiguration?
    /// 最近一次 Worker 状态。设置窗打开时也要跟着变——它的「状态」行显示的是
    /// 现在通不通，不是钥匙串里有没有令牌。赋值点有好几处，用 didSet 统一推送，
    /// 免得新增一处就漏一处。
    private var latestStatus: WorkerStatus? {
        didSet { settingsWindow?.update(status: latestStatus) }
    }
    private var isConfigured = false
    private var ffmpegSource: FFmpegSource = .custom
    private var workerDrainedForFFmpeg = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        menuBar = MenuBarController()
        ffmpegManager = FFmpegDownloadManager()
        menuBar.onConnect = { [weak self] in self?.connectOrOpenSettings() }
        menuBar.onReconnect = { [weak self] in self?.restartWorker() }
        menuBar.onToggleDraining = { [weak self] in self?.toggleDraining() }
        menuBar.onManageFFmpeg = { [weak self] in self?.manageFFmpeg() }
        menuBar.onOpenSettings = { [weak self] in self?.openSettings() }
        menuBar.onOpenLog = { [weak self] in self?.openLog() }
        menuBar.onCopyDiagnostics = { [weak self] in self?.copyDiagnostics() }
        menuBar.onQuit = { [weak self] in self?.quit() }
        ffmpegManager.onStateChange = { [weak self] state in
            self?.applyFFmpegState(state)
        }
        ffmpegManager.onInstalled = { [weak self] installation in
            self?.applyFFmpegInstallation(installation)
        }

        do {
            let snapshot = try configurationStore.snapshot()
            ffmpegSource = snapshot.ffmpegSource
            ffmpegManager.configure(
                managedPath: snapshot.managedFFmpegPath,
                managedVersion: snapshot.managedFFmpegVersion
            )
            // 「配没配过」只看 UserDefaults 里的标记，不读钥匙串
            isConfigured = !snapshot.nasURL.isEmpty && snapshot.tokenConfigured
            // 这里**刻意不去读令牌**。
            //
            // 读令牌意味着敲钥匙串，而钥匙串可能弹窗要密码（见 KeychainStore
            // 顶部关于代码签名的说明）。冷启动就甩用户一个系统授权框，他既不
            // 知道为什么弹，也不知道点了会发生什么——尤其是他可能压根没打算
            // 让 App 现在连上去。
            //
            // 令牌改成用到时才读（ensureConfiguration）：开了自动连接就在
            // ffmpeg 检查通过、真要连的那一刻读；没开就等他点「连接」。
            // 两种情况下弹窗都紧跟着一个他自己发起的动作，说得通。
            menuBar.update(status: nil, configured: isConfigured)
            menuBar.update(ffmpeg: ffmpegManager.menuState)
            prepareFFmpeg(snapshot: snapshot)
        } catch {
            showStartupError(error)
        }
        AppLogger.shared.info("菜单栏 App 已启动，配置状态=\(isConfigured ? "已配置" : "未配置")")
    }

    /// 启动时先验证当前路径；只有没有可用 ffmpeg 且用户尚未取消过提示时才弹窗。
    private func prepareFFmpeg(snapshot: WorkerSettingsSnapshot) {
        startupCheckTask?.cancel()
        startupCheckTask = Task { [weak self] in
            guard let self else { return }
            let usable = await self.probeFFmpeg(path: snapshot.ffmpegPath)
            guard !Task.isCancelled else { return }
            if usable {
                // ffmpeg 不可用时根本不会连，也就不必为此读一次钥匙串
                if snapshot.autoConnect, self.ensureConfiguration() != nil {
                    self.startWorker()
                }
                return
            }

            if self.isConfigured {
                self.showFFmpegUnavailable(path: snapshot.ffmpegPath)
            }
            if !snapshot.startupDownloadPromptDismissed {
                self.showStartupDownloadPrompt()
            }
        }
    }

    private func probeFFmpeg(path: String) async -> Bool {
        await Task.detached(priority: .utility) {
            (try? CapabilityProbe.run(ffmpegPath: path)) != nil
        }.value
    }

    private func showStartupDownloadPrompt() {
        guard !ffmpegManager.isProcessing else { return }
        // activate(ignoringOtherApps:) 在 macOS 14 已废弃
        if #available(macOS 14.0, *) {
            NSApp.activate()
        } else {
            NSApp.activate(ignoringOtherApps: true)
        }
        let alert = NSAlert()
        alert.messageText = "未检测到可用的 Jellyfin-ffmpeg"
        alert.informativeText = "MovieClaw Transcoder 需要带有 h264_videotoolbox 的 Jellyfin-ffmpeg 才能执行硬件转码。是否从 Jellyfin 官方下载 macOS arm64 版本？"
        alert.alertStyle = .informational
        alert.addButton(withTitle: "下载")
        alert.addButton(withTitle: "取消")
        if alert.runModal() == .alertFirstButtonReturn {
            manageFFmpeg()
        } else {
            configurationStore.dismissStartupDownloadPrompt()
            menuBar.update(ffmpeg: ffmpegManager.menuState)
            AppLogger.shared.info("用户取消了 Jellyfin-ffmpeg 首次下载，保留菜单栏下载入口")
        }
    }

    private func showFFmpegUnavailable(path: String) {
        let message = "未检测到可用的 Jellyfin-ffmpeg：\(path)"
        AppLogger.shared.warning(message)
        latestStatus = WorkerStatus(
            state: .error,
            message: message,
            workerID: configuration?.workerID ?? "-",
            activeJobs: 0,
            maxJobs: configuration?.maxJobs ?? 1,
            currentJobID: nil,
            currentJobName: nil,
            currentProgress: nil,
            ffmpegVersion: "-",
            encoders: [],
            lastError: message,
            updatedAt: Date()
        )
        menuBar.update(status: latestStatus, configured: isConfigured)
    }

    private func manageFFmpeg() {
        guard !ffmpegManager.isProcessing, ffmpegPreparationTask == nil else { return }
        let updateManagedWorker = ffmpegSource == .managed && workerClient != nil
        if updateManagedWorker {
            let activeJobs = latestStatus?.activeJobs ?? 0
            if activeJobs > 0 {
                let alert = NSAlert()
                alert.messageText = "当前正在转码"
                alert.informativeText = "更新前会暂停接收新任务，并等待当前任务完成。是否继续？"
                alert.alertStyle = .informational
                alert.addButton(withTitle: "等待并更新")
                alert.addButton(withTitle: "取消")
                guard alert.runModal() == .alertFirstButtonReturn else { return }
            }
            workerDrainedForFFmpeg = true
            menuBar.update(ffmpeg: .processing)
            ffmpegPreparationTask = Task { [weak self] in
                guard let self else { return }
                await self.drainWorkerBeforeFFmpegOperation()
            }
            return
        }
        startFFmpegDownloadWindow()
        ffmpegManager.start()
    }

    private func drainWorkerBeforeFFmpegOperation() async {
        if let workerClient {
            await workerClient.setDraining(true)
        }
        let deadline = Date().addingTimeInterval(15 * 60)
        while !Task.isCancelled, Date() < deadline {
            if (latestStatus?.activeJobs ?? 0) == 0 {
                startFFmpegDownloadWindow()
                ffmpegManager.start()
                ffmpegPreparationTask = nil
                return
            }
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
        guard !Task.isCancelled else { return }
        ffmpegPreparationTask = nil
        workerDrainedForFFmpeg = false
        if let workerClient {
            await workerClient.setDraining(false)
        }
        menuBar.update(ffmpeg: ffmpegManager.menuState)
        showStartupError(ConfigurationError.message("等待当前转码任务完成超时，暂未更新 Jellyfin-ffmpeg"))
    }

    private func startFFmpegDownloadWindow() {
        if ffmpegWindow == nil {
            let controller = FFmpegDownloadWindowController()
            controller.onCancel = { [weak self] in self?.cancelFFmpegOperation() }
            controller.onRetry = { [weak self] in self?.manageFFmpeg() }
            ffmpegWindow = controller
        }
        ffmpegWindow?.update(state: ffmpegManager.state)
        ffmpegWindow?.showWindowAndFocus()
    }

    private func cancelFFmpegOperation() {
        ffmpegPreparationTask?.cancel()
        ffmpegPreparationTask = nil
        ffmpegManager.cancel()
        if workerDrainedForFFmpeg {
            workerDrainedForFFmpeg = false
            if let workerClient {
                Task { await workerClient.setDraining(false) }
            }
        }
    }

    private func applyFFmpegState(_ state: FFmpegDownloadState) {
        menuBar?.update(ffmpeg: ffmpegManager.menuState)
        ffmpegWindow?.update(state: state)
        switch state {
        case .failed, .cancelled, .latest:
            resumeWorkerAfterFFmpegOperationIfNeeded()
        default:
            break
        }
    }

    private func resumeWorkerAfterFFmpegOperationIfNeeded() {
        guard workerDrainedForFFmpeg else { return }
        workerDrainedForFFmpeg = false
        if let workerClient {
            Task { await workerClient.setDraining(false) }
        }
    }

    private func applyFFmpegInstallation(_ installation: FFmpegInstallation) {
        let sourceBefore = ffmpegSource
        let currentPath = configuration?.ffmpegPath
        Task { [weak self] in
            guard let self else { return }
            let pathToCheck = currentPath ?? (try? self.configurationStore.snapshot().ffmpegPath)
            let currentPathUsable = if let pathToCheck {
                await self.probeFFmpeg(path: pathToCheck)
            } else {
                false
            }
            let shouldActivate = sourceBefore == .managed || !currentPathUsable
            do {
                if shouldActivate {
                    self.configurationStore.activateManagedFFmpeg(
                        path: installation.ffmpegPath,
                        version: installation.version
                    )
                    self.ffmpegSource = .managed
                } else {
                    self.configurationStore.recordManagedFFmpeg(
                        path: installation.ffmpegPath,
                        version: installation.version
                    )
                }
                let snapshot = try self.configurationStore.snapshot()
                self.menuBar.update(ffmpeg: self.ffmpegManager.menuState)

                if shouldActivate {
                    if self.workerDrainedForFFmpeg {
                        await self.stopWorkerAndWait()
                        self.workerDrainedForFFmpeg = false
                    }
                    if self.isConfigured, snapshot.autoConnect,
                       self.ensureConfiguration() != nil {
                        self.startWorker()
                    }
                }
                AppLogger.shared.info(
                    "Jellyfin-ffmpeg 已安装：version=\(installation.version)，active=\(shouldActivate ? "是" : "否")"
                )
            } catch {
                self.showStartupError(error)
                self.resumeWorkerAfterFFmpegOperationIfNeeded()
            }
        }
    }

    private func connectOrOpenSettings() {
        if workerClient != nil {
            stopWorker()
        } else if ensureConfiguration() != nil {
            startWorker()
        } else {
            openSettings()
        }
    }

    /// 需要令牌时才把配置装配出来。
    ///
    /// 启动时如果没开自动连接就不会去读钥匙串，`configuration` 于是是空的；
    /// 用户点「连接」时在这里补上。读失败（比如他在系统弹窗上点了拒绝）就把
    /// 原因说清楚，而不是默默什么也不发生。
    @discardableResult
    private func ensureConfiguration() -> WorkerConfiguration? {
        if let configuration {
            return configuration
        }
        guard isConfigured else { return nil }
        do {
            configuration = try configurationStore.loadConfiguration()
        } catch {
            showStartupError(error)
        }
        return configuration
    }

    private func startWorker() {
        guard let configuration = ensureConfiguration() else {
            openSettings()
            return
        }
        runtimeTask?.cancel()
        let task = Task { [weak self] in
            do {
                let capabilities = try await Task.detached(priority: .utility) {
                    try CapabilityProbe.run(ffmpegPath: configuration.ffmpegPath)
                }.value
                guard !Task.isCancelled else { return }
                let client = WorkerClient(configuration: configuration, capabilities: capabilities)
                guard let self else { return }
                self.workerClient = client
                let monitor = Task { [weak self, client] in
                    for await status in client.statuses {
                        if Task.isCancelled { break }
                        self?.apply(status)
                    }
                }
                await client.runForever()
                monitor.cancel()
            } catch {
                self?.showStartupError(error)
            }
        }
        runtimeTask = task
        latestStatus = WorkerStatus(
            state: .starting,
            message: "正在检查 ffmpeg 能力",
            workerID: configuration.workerID,
            activeJobs: 0,
            maxJobs: configuration.maxJobs,
            currentJobID: nil,
            currentJobName: nil,
            currentProgress: nil,
            ffmpegVersion: "检查中",
            encoders: [],
            lastError: nil,
            updatedAt: Date()
        )
        menuBar.update(status: latestStatus, configured: true)
        AppLogger.shared.info("准备连接 NAS：\(configuration.nasURL.host ?? "未知主机")")
    }

    private func stopWorker() {
        let client = workerClient
        workerClient = nil
        runtimeTask?.cancel()
        runtimeTask = nil
        if let client {
            Task { await client.stop() }
        }
        latestStatus = WorkerStatus(
            state: .stopped,
            message: "已停止",
            workerID: configuration?.workerID ?? "-",
            activeJobs: 0,
            maxJobs: configuration?.maxJobs ?? 1,
            currentJobID: nil,
            currentJobName: nil,
            currentProgress: nil,
            ffmpegVersion: "-",
            encoders: [],
            lastError: nil,
            updatedAt: Date()
        )
        menuBar.update(status: latestStatus, configured: isConfigured)
    }

    private func stopWorkerAndWait() async {
        let client = workerClient
        workerClient = nil
        runtimeTask?.cancel()
        runtimeTask = nil
        if let client {
            await client.stop()
        }
        latestStatus = WorkerStatus(
            state: .stopped,
            message: "已停止",
            workerID: configuration?.workerID ?? "-",
            activeJobs: 0,
            maxJobs: configuration?.maxJobs ?? 1,
            currentJobID: nil,
            currentJobName: nil,
            currentProgress: nil,
            ffmpegVersion: "-",
            encoders: [],
            lastError: nil,
            updatedAt: Date()
        )
        menuBar.update(status: latestStatus, configured: isConfigured)
    }

    private func restartWorker() {
        guard let oldClient = workerClient else {
            startWorker()
            return
        }
        workerClient = nil
        runtimeTask?.cancel()
        runtimeTask = nil
        Task { [weak self] in
            await oldClient.stop()
            self?.startWorker()
        }
    }

    private func toggleDraining() {
        guard let workerClient else { return }
        let draining = latestStatus?.state != .draining
        Task { await workerClient.setDraining(draining) }
    }

    private func openSettings() {
        do {
            let snapshot = try configurationStore.snapshot()
            let controller = SettingsWindowController(snapshot: snapshot)
            // 保存与配对是两回事：改地址不该动令牌，配对成功也不该改地址。
            // 分成两个回调，「改个端口结果掉线了」这种事就不会发生。
            controller.onSave = { [weak self] draft in
                guard let self else { return }
                let newConfiguration = try self.configurationStore.save(draft)
                self.applyConfiguration(newConfiguration)
                AppLogger.shared.info("Worker 设置已保存：\(draft.nasURL)")
            }
            controller.onPaired = { [weak self] token in
                guard let self else { return }
                try self.configurationStore.saveToken(token)
                self.applyConfiguration(try self.configurationStore.loadConfiguration())
                AppLogger.shared.info("Worker 已完成配对并获得授权")
            }
            controller.onClear = { [weak self] in
                guard let self else { return }
                self.stopWorker()
                try self.configurationStore.clear()
                self.configuration = nil
                self.isConfigured = false
                self.latestStatus = nil
                let snapshot = try self.configurationStore.snapshot()
                self.ffmpegSource = snapshot.ffmpegSource
                self.ffmpegManager.configure(
                    managedPath: snapshot.managedFFmpegPath,
                    managedVersion: snapshot.managedFFmpegVersion
                )
                self.menuBar.update(status: nil, configured: false)
                self.menuBar.update(ffmpeg: self.ffmpegManager.menuState)
                AppLogger.shared.info("Worker 配置已清除")
            }
            settingsWindow = controller
            // 窗口是现开的，didSet 推不到它，开窗时补一次当前状态
            controller.update(status: latestStatus)
            controller.showWindowAndFocus()
        } catch {
            showStartupError(error)
        }
    }

    /// 配置变更后的统一收尾：刷新 ffmpeg 状态、菜单栏，并重启 Worker 连接。
    ///
    /// 配置为 nil 表示还没配对完（地址填了但没授权）——此时不该去连，
    /// 也不该把菜单栏显示成已配置。
    private func applyConfiguration(_ newConfiguration: WorkerConfiguration?) {
        configuration = newConfiguration
        isConfigured = newConfiguration != nil
        if let snapshot = try? configurationStore.snapshot() {
            ffmpegSource = snapshot.ffmpegSource
            ffmpegManager.configure(
                managedPath: snapshot.managedFFmpegPath,
                managedVersion: snapshot.managedFFmpegVersion
            )
        }
        menuBar.update(ffmpeg: ffmpegManager.menuState)
        stopWorker()
        if newConfiguration != nil {
            startWorker()
        } else {
            menuBar.update(status: nil, configured: false)
        }
    }

    private func apply(_ status: WorkerStatus) {
        latestStatus = status
        menuBar.update(status: status, configured: isConfigured)
    }

    private func openLog() {
        NSWorkspace.shared.open(AppLogger.shared.logURL)
    }

    private func copyDiagnostics() {
        let status = latestStatus
        let lines = [
            "MovieClawTranscoder \(BuildInfo.version)",
            "macOS \(ProcessInfo.processInfo.operatingSystemVersionString)",
            "arch arm64",
            "state=\(status?.state.rawValue ?? "unconfigured")",
            "worker_id=\(status?.workerID ?? configuration?.workerID ?? "-")",
            "ffmpeg=\(status?.ffmpegVersion ?? "-")",
            "ffmpeg_source=\(ffmpegSource.rawValue)",
            "managed_ffmpeg_version=\(ffmpegManager?.managedVersion ?? "-")",
            "encoders=\((status?.encoders ?? []).joined(separator: ","))",
            "active_jobs=\(status?.activeJobs ?? 0)/\(status?.maxJobs ?? configuration?.maxJobs ?? 1)",
            "last_error=\(status?.lastError ?? "-")",
        ].joined(separator: "\n")
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(lines, forType: .string)
        AppLogger.shared.info("已复制脱敏诊断信息")
    }

    private func showStartupError(_ error: Error) {
        let message = LogSanitizer.redact(error.localizedDescription, secret: configuration?.workerToken)
        AppLogger.shared.error("Worker 启动失败：\(message)", secret: configuration?.workerToken)
        latestStatus = WorkerStatus(
            state: .error,
            message: message,
            workerID: configuration?.workerID ?? "-",
            activeJobs: 0,
            maxJobs: configuration?.maxJobs ?? 1,
            currentJobID: nil,
            currentJobName: nil,
            currentProgress: nil,
            ffmpegVersion: "-",
            encoders: [],
            lastError: message,
            updatedAt: Date()
        )
        menuBar?.update(status: latestStatus, configured: isConfigured)
    }

    private func quit() {
        startupCheckTask?.cancel()
        ffmpegPreparationTask?.cancel()
        ffmpegManager?.cancel()
        let client = workerClient
        workerClient = nil
        runtimeTask?.cancel()
        runtimeTask = nil
        Task {
            if let client {
                await client.stop()
            }
            NSApp.terminate(nil)
        }
    }
}
