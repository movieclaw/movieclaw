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
        // 菜单栏 App 自己拉起的转码内核进程（见 CoreRunner / CoreSupervisor）
        if CommandLine.arguments.contains("--core") {
            CoreRunner.run()
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
                let exit = await WorkerClient(configuration: configuration, capabilities: capabilities).runForever()
                if exit == .ffmpegUnusable {
                    fputs("[MovieClaw] Worker 已停止：ffmpeg 不可用\n", stderr)
                    Darwin.exit(1)
                }
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
/// AppKit 主线程只负责 UI 和配置；连 NAS、管 ffmpeg 都在独立的转码内核进程里
/// （``CoreSupervisor`` 看管），内核崩溃、卡死都只重启内核，菜单栏 App 不受影响。
@MainActor
final class MovieClawAppDelegate: NSObject, NSApplicationDelegate {
    private let configurationStore = ConfigurationStore()
    /// 已结束任务的本地记录，状态面板的「今天」与「最近」从这里读。
    private let jobHistory = JobHistory()
    private var menuBar: MenuBarController!
    private var ffmpegManager: FFmpegDownloadManager!
    private var settingsWindow: SettingsWindowController?
    /// 看管转码内核进程（崩溃自动重启、卡死检测）。
    private let supervisor = CoreSupervisor()
    private var startupCheckTask: Task<Void, Never>?
    private var ffmpegPreparationTask: Task<Void, Never>?
    private var configuration: WorkerConfiguration?
    /// 最近一次 Worker 状态。设置窗打开时也要跟着变——它的「连接」页显示的是
    /// 现在通不通，不是钥匙串里有没有令牌。赋值点有好几处，用 didSet 统一推送，
    /// 免得新增一处就漏一处。
    private var latestStatus: WorkerStatus? {
        didSet {
            let connected = latestStatus.map { Self.connectedStates.contains($0.state) } ?? false
            if !connected {
                connectedSince = nil
            } else if connectedSince == nil {
                connectedSince = Date()
            }
            settingsWindow?.update(status: latestStatus, connectedSince: connectedSince)
        }
    }
    /// 这一次连上 NAS 的时间（设置「连接」页的「已连接 2 小时」）。
    private var connectedSince: Date?
    private static let connectedStates: Set<WorkerConnectionState> = [.ready, .busy, .paused, .draining]
    private var isConfigured = false
    private var ffmpegSource: FFmpegSource = .custom
    private var workerDrainedForFFmpeg = false
    /// 用户在「转码」页选了 Jellyfin-ffmpeg 但还没下载过：装好后无论当前路径能不能用都切过去。
    private var activateManagedOnInstall = false
    /// 钥匙串说明上点了「暂不连接」：面板停在「没有连接」，等他自己点「连接」。
    private var awaitingKeychainApproval = false
    /// 这个进程已经试过交班给登录项（只在第一次连接前试一次）。
    private var handOverChecked = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        // 防多开：已经有一个在跑（开机自启动的那个、或另一个位置的副本），这个直接退出。
        // 退出码 0：就算这个实例是登录项拉起的，系统也不会当成崩溃再拉一遍。
        // 例外是登录项拉起的实例碰上正在交班的手动实例（见 handOverToLoginItem）：等它退出后接班
        if let other = Self.otherInstances().first,
           !(LoginItem.isLaunchdInstance && Self.wait(upTo: 5, until: { Self.otherInstances().isEmpty })) {
            AppLogger.shared.info("已经有一个 MovieClaw 转码器在运行（pid=\(other.processIdentifier)），本次启动直接退出")
            exit(0)
        }
        LoginItem.applyOnLaunch()

        menuBar = MenuBarController(history: jobHistory)
        ffmpegManager = FFmpegDownloadManager()
        menuBar.onConnect = { [weak self] in self?.connectOrOpenSettings() }
        menuBar.onReconnect = { [weak self] in self?.restartWorker() }
        menuBar.onManageFFmpeg = { [weak self] in self?.manageFFmpeg() }
        menuBar.onOpenSettings = { [weak self] in self?.openSettings(tab: nil) }
        menuBar.onOpenLog = { [weak self] in self?.openLog() }
        menuBar.onCopyDiagnostics = { [weak self] in self?.copyDiagnostics() }
        menuBar.onQuit = { [weak self] in self?.quit() }
        ffmpegManager.onStateChange = { [weak self] state in
            self?.applyFFmpegState(state)
        }
        ffmpegManager.onInstalled = { [weak self] installation in
            self?.applyFFmpegInstallation(installation)
        }
        supervisor.onStatus = { [weak self] status in self?.apply(status) }
        supervisor.onJobEnded = { [weak self] record in
            self?.jobHistory.record(record)
        }
        supervisor.onRecoveriesChanged = { [weak self] in
            guard let self else { return }
            self.menuBar.recoveries = self.supervisor.recoveries
        }
        // 睡眠唤醒：让内核立刻确认连接，别等 45 秒的半开检测或退避
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                AppLogger.shared.info("系统从睡眠中唤醒，立刻确认与 NAS 的连接")
                self?.supervisor.reconnectNow()
            }
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
            menuBar.nasAddress = isConfigured ? snapshot.nasURL : nil
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
        alert.informativeText = "MovieClaw 转码器需要带有 h264_videotoolbox 的 Jellyfin-ffmpeg 才能执行硬件转码。是否从 Jellyfin 官方下载 macOS arm64 版本？"
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
        latestStatus = .offline(
            .error,
            message: message,
            workerID: configuration?.workerID ?? "-",
            maxJobs: configuration?.maxJobs ?? 1,
            error: message
        )
        menuBar.update(status: latestStatus, configured: isConfigured)
    }

    /// 下载或更新 Jellyfin-ffmpeg，进度显示在设置的「转码」页。
    private func manageFFmpeg(activateWhenInstalled: Bool = false) {
        if activateWhenInstalled {
            activateManagedOnInstall = true
        }
        guard !ffmpegManager.isProcessing, ffmpegPreparationTask == nil else { return }
        let updateManagedWorker = ffmpegSource == .managed && supervisor.isActive
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
            showFFmpegProgress()
            return
        }
        ffmpegManager.start()
        showFFmpegProgress()
    }

    private func drainWorkerBeforeFFmpegOperation() async {
        supervisor.setDraining(true)
        let deadline = Date().addingTimeInterval(15 * 60)
        while !Task.isCancelled, Date() < deadline {
            if (latestStatus?.activeJobs ?? 0) == 0 {
                ffmpegPreparationTask = nil
                ffmpegManager.start()
                pushFFmpegState()
                return
            }
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
        guard !Task.isCancelled else { return }
        ffmpegPreparationTask = nil
        workerDrainedForFFmpeg = false
        supervisor.setDraining(false)
        menuBar.update(ffmpeg: ffmpegManager.menuState)
        pushFFmpegState()
        showStartupError(ConfigurationError.message("等待当前转码任务完成超时，暂未更新 Jellyfin-ffmpeg"))
    }

    /// 打开设置的「转码」页（没配对时是引导页，底部同样有 ffmpeg 状态条）。
    private func showFFmpegProgress() {
        openSettings(tab: .transcode)
        pushFFmpegState()
    }

    private func pushFFmpegState() {
        settingsWindow?.update(ffmpeg: ffmpegManager.state, waitingForJobs: ffmpegPreparationTask != nil)
    }

    private func cancelFFmpegOperation() {
        ffmpegPreparationTask?.cancel()
        ffmpegPreparationTask = nil
        activateManagedOnInstall = false
        ffmpegManager.cancel()
        pushFFmpegState()
        menuBar.update(ffmpeg: ffmpegManager.menuState)
        if workerDrainedForFFmpeg {
            workerDrainedForFFmpeg = false
            supervisor.setDraining(false)
        }
    }

    private func applyFFmpegState(_ state: FFmpegDownloadState) {
        menuBar?.update(ffmpeg: ffmpegManager.menuState)
        pushFFmpegState()
        switch state {
        case .failed, .cancelled, .latest:
            activateManagedOnInstall = false
            resumeWorkerAfterFFmpegOperationIfNeeded()
        default:
            break
        }
    }

    private func resumeWorkerAfterFFmpegOperationIfNeeded() {
        guard workerDrainedForFFmpeg else { return }
        workerDrainedForFFmpeg = false
        supervisor.setDraining(false)
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
            let shouldActivate = sourceBefore == .managed || !currentPathUsable || self.activateManagedOnInstall
            self.activateManagedOnInstall = false
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
                self.settingsWindow?.update(snapshot: snapshot)

                if shouldActivate {
                    // 不管之前是不是为更新而排空过，换了 ffmpeg 都得重启 Worker 才会用上；
                    // 缓存的配置里还是旧路径，清掉让 ensureConfiguration 重新装配。
                    // 没在跑就不必停（停了会把「未配对」的状态改写成「已断开」）
                    if self.supervisor.isActive {
                        await self.stopWorkerAndWait()
                    }
                    self.workerDrainedForFFmpeg = false
                    self.configuration = nil
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
        if supervisor.isActive {
            stopWorker()
        } else if ensureConfiguration() != nil {
            startWorker()
        } else {
            openSettings(tab: nil)
        }
    }

    /// 需要令牌时才把配置装配出来。
    ///
    /// 启动时如果没开自动连接就不会去读钥匙串，`configuration` 于是是空的；
    /// 用户点「连接」时在这里补上。读失败（比如他在系统弹窗上点了拒绝）就把
    /// 原因说清楚，而不是默默什么也不发生。
    ///
    /// **先静默探测，再真读。** 新装或刚更新的 App（ad-hoc 签名每次构建身份都变）
    /// 读旧令牌时系统会弹「想要使用你存储在钥匙串中的机密信息」——冷不丁冒出来，
    /// 用户不知道存了什么、为什么问。所以先禁止交互试读一次：不需要授权就直接
    /// 拿到；需要的话，先弹我们自己的说明，他点「继续」才让系统弹窗。
    @discardableResult
    private func ensureConfiguration() -> WorkerConfiguration? {
        if let configuration {
            return configuration
        }
        guard isConfigured else { return nil }
        awaitingKeychainApproval = false
        do {
            configuration = try configurationStore.loadConfiguration(interactive: false)
        } catch is KeychainStore.ApprovalRequired {
            guard confirmKeychainAccess() else {
                awaitingKeychainApproval = true
                AppLogger.shared.info("用户暂不授权读取钥匙串里的设备令牌，等他点「连接」再问")
                return nil
            }
            do {
                configuration = try configurationStore.loadConfiguration()
            } catch {
                showStartupError(error)
            }
        } catch {
            showStartupError(error)
        }
        return configuration
    }

    /// 系统弹钥匙串授权窗之前，先用一句话讲清楚：存的是连接密钥、该点什么。
    /// 文案刻意短：用户要的只是「这是什么、点哪个」，原理留给 README。
    private func confirmKeychainAccess() -> Bool {
        if #available(macOS 14.0, *) {
            NSApp.activate()
        } else {
            NSApp.activate(ignoringOtherApps: true)
        }
        let alert = NSAlert()
        alert.messageText = "需要存储连接密钥"
        alert.informativeText = "连接密钥是配对时生成的，用来连接 movieclaw，保存在钥匙串里。"
            + "点「继续」后输入这台 Mac 的登录密码，选「始终允许」即可。"
        alert.alertStyle = .informational
        alert.addButton(withTitle: "继续")
        alert.addButton(withTitle: "暂不连接")
        return alert.runModal() == .alertFirstButtonReturn
    }

    private func startWorker() {
        guard let configuration = ensureConfiguration() else {
            // 用户在钥匙串说明上选了「暂不连接」：尊重他，不要转头又把设置窗甩出来
            if !awaitingKeychainApproval {
                openSettings(tab: nil)
            }
            return
        }
        if !handOverChecked {
            handOverChecked = true
            handOverToLoginItem()
        }
        supervisor.start(configuration)
        latestStatus = WorkerStatus.offline(
            .starting,
            message: "正在检查 ffmpeg 能力",
            workerID: configuration.workerID,
            maxJobs: configuration.maxJobs,
            ffmpegVersion: "检查中"        )
        menuBar.update(status: latestStatus, configured: true)
        AppLogger.shared.info("准备连接 NAS：\(configuration.nasURL.host ?? "未知主机")")
    }

    /// 手动打开（访达、聚焦、更新后重新打开）的实例不归 launchd 管，意外退出后不会被拉起。
    /// 开着开机自启动时请 launchd 按登录项另起一个，等它出现就退出、由它接班。
    ///
    /// 放在读到连接密钥之后、启动内核之前：更新后第一次打开要在钥匙串里重新授权，这一步
    /// 得留在用户亲手打开、正在最前面的这个实例里——launchd 在后台拉起的实例不一定能把
    /// 弹窗摆到用户眼前（macOS 14 起激活要「协商」），模态弹窗被挡住，App 就像卡死了。
    /// 授权时选了「始终允许」，接班的实例读钥匙串就不会再问。设置窗开着（用户正在填）
    /// 时不交班。5 秒没等到（登录项被系统拦住之类）就自己接着跑——保护少一层，但 App
    /// 总归是开着的。
    private func handOverToLoginItem() {
        guard !LoginItem.isLaunchdInstance, LoginItem.state == .enabled, settingsWindow == nil else { return }
        LoginItem.kickstart()
        if Self.wait(upTo: 5, until: { !Self.otherInstances().isEmpty }) {
            AppLogger.shared.info("已交给登录项接管（App 意外退出后系统会重新拉起它），本次打开的实例退出")
            exit(0)
        }
        AppLogger.shared.warning("登录项 5 秒内没有接班，本实例继续运行（App 意外退出后不会被自动拉起）")
    }

    private func stopWorker() {
        let supervisor = supervisor
        Task { await supervisor.stop() }
        latestStatus = WorkerStatus.offline(
            .stopped,
            message: "已停止",
            workerID: configuration?.workerID ?? "-",
            maxJobs: configuration?.maxJobs ?? 1        )
        menuBar.update(status: latestStatus, configured: isConfigured)
    }

    private func stopWorkerAndWait() async {
        await supervisor.stop()
        latestStatus = WorkerStatus.offline(
            .stopped,
            message: "已停止",
            workerID: configuration?.workerID ?? "-",
            maxJobs: configuration?.maxJobs ?? 1        )
        menuBar.update(status: latestStatus, configured: isConfigured)
    }

    private func restartWorker() {
        guard supervisor.isActive else {
            startWorker()
            return
        }
        Task { [weak self] in
            await self?.supervisor.stop()
            self?.startWorker()
        }
    }

    private func openSettings(tab: SettingsTab?) {
        // 窗口还开着就直接切过去，不要另起一个——两个设置窗各说各的最让人糊涂
        if let settingsWindow, settingsWindow.window?.isVisible == true {
            settingsWindow.showWindowAndFocus(tab: tab)
            return
        }
        do {
            let snapshot = try configurationStore.snapshot()
            let controller = SettingsWindowController(snapshot: snapshot)
            // 保存与配对是两回事：改地址不该动令牌，配对成功也不该改地址。
            // 分成两个回调，「改个端口结果掉线了」这种事就不会发生。
            controller.onSave = { [weak self] draft in
                guard let self else { return }
                let before = self.configuration
                let newConfiguration = try self.configurationStore.save(draft)
                self.settingsWindow?.update(snapshot: try self.configurationStore.snapshot())
                if Self.needsRestart(from: before, to: newConfiguration) {
                    self.applyConfiguration(newConfiguration)
                } else {
                    // 只改了「启动后自动连接」这类不影响运行中连接的项：别为它断一次线
                    self.configuration = newConfiguration
                }
                AppLogger.shared.info("Worker 设置已保存：\(draft.nasURL)")
            }
            controller.onPaired = { [weak self] token in
                guard let self else { return }
                try self.configurationStore.saveToken(token)
                self.applyConfiguration(try self.configurationStore.loadConfiguration())
                self.settingsWindow?.update(snapshot: try self.configurationStore.snapshot())
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
                self.menuBar.nasAddress = nil
                self.menuBar.update(status: nil, configured: false)
                self.menuBar.update(ffmpeg: self.ffmpegManager.menuState)
                self.settingsWindow?.update(snapshot: snapshot)
                AppLogger.shared.info("Worker 配置已清除")
            }
            controller.onManageFFmpeg = { [weak self] activate in self?.manageFFmpeg(activateWhenInstalled: activate) }
            controller.onCancelFFmpeg = { [weak self] in self?.cancelFFmpegOperation() }
            controller.onOpenLog = { [weak self] in self?.openLog() }
            controller.onCopyDiagnostics = { [weak self] in self?.copyDiagnostics() }
            // 关掉就整个丢掉：设置窗一天开不了一次，常驻它的视图与图层白占几 MB
            controller.onClose = { [weak self, weak controller] in
                DispatchQueue.main.async {
                    guard let self, self.settingsWindow === controller else { return }
                    self.settingsWindow = nil
                }
            }
            settingsWindow = controller
            // 窗口是现开的，didSet 推不到它，开窗时补一次当前状态
            controller.update(status: latestStatus, connectedSince: connectedSince)
            controller.update(ffmpeg: ffmpegManager.state, waitingForJobs: ffmpegPreparationTask != nil)
            controller.showWindowAndFocus(tab: tab)
        } catch {
            showStartupError(error)
        }
    }

    /// 改了设置之后要不要重启 Worker：地址、名称、ffmpeg、并发变了才要。
    /// 之前还没装配过配置（没开自动连接、也没点过连接）时按老规矩重来一遍。
    private static func needsRestart(from old: WorkerConfiguration?, to new: WorkerConfiguration?) -> Bool {
        guard let old, let new else { return true }
        return old.nasURL != new.nasURL
            || old.workerID != new.workerID
            || old.ffmpegPath != new.ffmpegPath
            || old.maxJobs != new.maxJobs
    }

    /// 配置变更后的统一收尾：刷新 ffmpeg 状态、菜单栏，并重启 Worker 连接。
    ///
    /// 配置为 nil 表示还没配对完（地址填了但没授权）——此时不该去连，
    /// 也不该把菜单栏显示成已配置。
    private func applyConfiguration(_ newConfiguration: WorkerConfiguration?) {
        configuration = newConfiguration
        isConfigured = newConfiguration != nil
        menuBar.nasAddress = newConfiguration?.nasURL.absoluteString
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
            "core_pid=\(supervisor.corePID.map(String.init) ?? "-")",
            "core_recoveries_24h=\(supervisor.recoveries.count)"
                + (supervisor.recoveries.last.map { " last=\($0.reason)" } ?? ""),
            "login_item=\(LoginItem.state)",
        ].joined(separator: "\n")
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(lines, forType: .string)
        AppLogger.shared.info("已复制脱敏诊断信息")
    }

    private func showStartupError(_ error: Error) {
        let message = LogSanitizer.redact(error.localizedDescription, secret: configuration?.workerToken)
        AppLogger.shared.error("Worker 启动失败：\(message)", secret: configuration?.workerToken)
        latestStatus = WorkerStatus.offline(
            .error,
            message: message,
            workerID: configuration?.workerID ?? "-",
            maxJobs: configuration?.maxJobs ?? 1,
            error: message        )
        menuBar?.update(status: latestStatus, configured: isConfigured)
    }

    private func quit() {
        startupCheckTask?.cancel()
        ffmpegPreparationTask?.cancel()
        ffmpegManager?.cancel()
        Task {
            await supervisor.stop()
            NSApp.terminate(nil)
        }
    }

    /// 被系统要求退出（注销、关机、别的实例接管）时也把内核带走。
    /// 同一个 bundle id 的其他实例（不管 App 放在哪个位置）。
    private static func otherInstances() -> [NSRunningApplication] {
        let bundleID = Bundle.main.bundleIdentifier ?? "com.movieclaw.transcoder"
        return NSRunningApplication.runningApplications(withBundleIdentifier: bundleID)
            .filter { $0.processIdentifier != getpid() && !$0.isTerminated }
    }

    /// 启动交班时的短暂等待。转着 RunLoop 等：运行中 App 的列表靠主线程上的通知刷新，
    /// 主线程干睡可能一直读到旧列表。
    private static func wait(upTo seconds: TimeInterval, until condition: () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        while !condition() {
            guard Date() < deadline else { return false }
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        supervisor.terminateNow()
    }
}
