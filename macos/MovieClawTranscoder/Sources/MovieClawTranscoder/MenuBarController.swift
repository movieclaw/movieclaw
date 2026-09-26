import AppKit

/// 菜单栏图标与状态面板。只负责展示和派发用户动作；网络和 ffmpeg 生命周期仍由
/// AppDelegate/WorkerClient 管理。
///
/// 点图标弹出 ``StatusPanel``（内容是 ``PanelView``），不再是下拉菜单。面板里显示
/// 什么由纯函数 ``PanelModel/make(status:configured:nasAddress:today:recent:now:)``
/// 决定；这里做的是把状态推过去、维护速度采样，以及把按钮点击转成回调。
/// 低频操作（断开连接、ffmpeg、日志、诊断）收在面板底栏的「更多」菜单里。
@MainActor
final class MenuBarController: NSObject {
    private let statusItem: NSStatusItem
    /// 面板与内容**只在打开时才建、收起后整个丢掉**（见 ``openPanel(below:)`` / ``releasePanel()``）。
    ///
    /// 常驻的代价是实测出来的：面板（液态玻璃窗口 + 全部子视图）即使从不打开也要 2 MB；
    /// 打开过一次后，视图、图层与字形缓存又多占 5 MB，收起后也不会自己回落。而面板一天
    /// 打不开几次，重建一次只要几毫秒。
    private var panel: StatusPanel?
    private var panelView: PanelView?

    /// 连接或断开（未配置时打开设置），由 AppMain 决定具体做什么。
    var onConnect: (() -> Void)?
    var onReconnect: (() -> Void)?
    var onManageFFmpeg: (() -> Void)?
    var onOpenSettings: (() -> Void)?
    var onOpenLog: (() -> Void)?
    var onCopyDiagnostics: (() -> Void)?
    var onQuit: (() -> Void)?

    /// 设置里填的 movieclaw 地址，面板页眉的副标题用它。配置变化时由 AppMain 推过来。
    var nasAddress: String? {
        didSet { refresh() }
    }

    private let history: JobHistory?
    /// CPU / 内存采样（面板里的小图表）。App 一启动就开始采，打开面板时已有曲线。
    private let resources = ResourceMonitor()
    private var status: WorkerStatus?
    private var configured = false
    private var ffmpeg: FFmpegMenuState = .download
    /// 各任务的实时转码速度（见 ``SpeedTracker``）。
    private var speedTrackers: [String: SpeedTracker] = [:]
    /// 最近一次收起面板的时间。点图标收起面板时，面板先因失焦收起、按钮动作随后
    /// 又到——不挡一下就会「刚关上又打开」。
    private var lastDismissAt = Date.distantPast
    private var badge: MenuBarIcon.Badge = .none
    /// 转码内核的自动恢复记录（面板底部那句「自动恢复过 N 次」）。
    var recoveries: [CoreRecovery] = [] {
        didSet { refresh() }
    }

    init(history: JobHistory?) {
        self.history = history
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        super.init()
        statusItem.button?.image = MenuBarIcon.statusItemImage()
        statusItem.button?.toolTip = "MovieClaw 转码器"
        statusItem.button?.target = self
        statusItem.button?.action = #selector(togglePanel)
        // 和系统菜单一样按下即弹出，而不是松开才弹
        statusItem.button?.sendAction(on: [.leftMouseDown, .rightMouseDown])

        resources.onSample = { [weak self] in self?.refresh() }
        resources.start()
        refresh()
    }

    func update(status: WorkerStatus?, configured: Bool) {
        self.status = status
        self.configured = configured
        trackSpeeds(status)
        refresh()
    }

    func update(ffmpeg: FFmpegMenuState) {
        self.ffmpeg = ffmpeg
    }

    // MARK: - 刷新

    private func currentModel() -> PanelModel {
        PanelModel.make(
            status: status,
            configured: configured,
            nasAddress: nasAddress,
            today: history?.today() ?? [],
            recent: history?.recent(limit: PanelModel.recentLimit) ?? [],
            speeds: speedTrackers.compactMapValues(\.speed),
            resources: PanelModel.ResourceHistory(
                samples: resources.samples,
                physicalMemory: resources.physicalMemory,
                cores: resources.cores
            ),
            recoveries: recoveries
        )
    }

    private func refresh() {
        let model = currentModel()
        if model.badge != badge {
            badge = model.badge
            statusItem.button?.image = MenuBarIcon.statusItemImage(badge: badge)
        }
        statusItem.button?.toolTip = "MovieClaw 转码器：\(model.presentation.title)"
        // 面板收着时根本不存在；下次打开时会用最新状态重建
        guard let panel, panel.isVisible, let panelView else { return }
        panelView.apply(model)
        panel.fitToContent()
    }

    /// 用相邻两次进度算实时速度：片内前进了多少 ÷ 过了多久。被 NAS 暂停时丢掉基线，
    /// 恢复后从新的一点重新算——暂停的时间不计入，速度反映的才是 Mac 真实的转码能力。
    private func trackSpeeds(_ status: WorkerStatus?) {
        let jobs = status?.jobs ?? []
        let running = Set(jobs.map(\.id))
        speedTrackers = speedTrackers.filter { running.contains($0.key) }
        let now = Date()
        for job in jobs {
            guard !job.paused, let position = job.progress?.outTimeMS else {
                // 暂停：基线作废，但保留已算出的速度，恢复后第一次采样前还有东西可显示
                speedTrackers[job.id]?.base = nil
                continue
            }
            speedTrackers[job.id, default: SpeedTracker()].observe(position: position, at: now)
        }
    }

    // MARK: - 面板

    @objc private func togglePanel() {
        if let panel, panel.isVisible {
            panel.dismiss()
            return
        }
        guard Date().timeIntervalSince(lastDismissAt) > 0.25, let button = statusItem.button else { return }
        openPanel(below: button)
    }

    private func openPanel(below button: NSStatusBarButton) {
        let view = PanelView()
        view.onAction = { [weak self] action in self?.perform(action) }
        view.onOpenSettings = { [weak self] in
            self?.panel?.dismiss()
            self?.onOpenSettings?()
        }
        view.onMore = { [weak self] button in self?.showMoreMenu(from: button) }
        view.onQuit = { [weak self] in self?.onQuit?() }
        view.apply(currentModel())

        let panel = StatusPanel(content: view)
        panel.onDismiss = { [weak self] in
            self?.statusItem.button?.highlight(false)
            self?.lastDismissAt = Date()
            // 等这一轮事件处理完再丢：dismiss 可能正是面板自己的按钮触发的
            DispatchQueue.main.async { self?.releasePanel() }
        }
        panel.onOpenSettings = { [weak self] in self?.onOpenSettings?() }
        panel.onQuit = { [weak self] in self?.onQuit?() }
        self.panel = panel
        panelView = view
        button.highlight(true)
        panel.show(below: button)
    }

    /// 收起后整个丢掉面板。
    ///
    /// **必须 close**，光把引用置空没用：orderOut 只是把窗口藏起来，它还在
    /// `NSApp.windows` 里、被 AppKit 的窗口表持有着，面板和全部子视图一个都不会释放
    /// （实测：收起 30 秒后堆里 PanelView、StatusPanel、按钮、图表全都还活着）。
    private func releasePanel() {
        guard let panel, !panel.isVisible else { return }
        panel.close()
        panel.contentView = nil
        self.panel = nil
        panelView = nil
    }

    private func perform(_ action: PanelModel.Action) {
        switch action {
        case .connect, .retry:
            onConnect?()
        case .reconnect:
            onReconnect?()
        case .openSettings, .pairAgain:
            panel?.dismiss()
            onOpenSettings?()
        case .copyDiagnostics:
            onCopyDiagnostics?()
        case .openLog:
            panel?.dismiss()
            onOpenLog?()
        }
    }

    /// 底栏「更多」：低频操作。都是标准菜单项，键盘和读屏照常可用。
    private func showMoreMenu(from button: NSView) {
        let menu = NSMenu()
        menu.autoenablesItems = false
        let connected = status.map { $0.state != .stopped && $0.state != .error } ?? false
        menu.addItem(item(connected ? "断开连接" : "连接", symbol: connected ? "stop.circle" : "play.circle") { [weak self] in
            self?.onConnect?()
        })
        let reconnect = item("立即重连", symbol: "arrow.clockwise") { [weak self] in self?.onReconnect?() }
        reconnect.isEnabled = status != nil && status?.state != .stopped
        menu.addItem(reconnect)
        menu.addItem(.separator())
        let ffmpegItem = item(ffmpeg.title, symbol: ffmpeg.symbolName) { [weak self] in
            self?.panel?.dismiss()
            self?.onManageFFmpeg?()
        }
        ffmpegItem.isEnabled = ffmpeg.isEnabled
        menu.addItem(ffmpegItem)
        menu.addItem(.separator())
        menu.addItem(item("打开日志", symbol: "doc.text.magnifyingglass") { [weak self] in
            self?.panel?.dismiss()
            self?.onOpenLog?()
        })
        menu.addItem(item("复制诊断信息", symbol: "doc.on.clipboard") { [weak self] in self?.onCopyDiagnostics?() })
        // 菜单左上角落在按钮左下角稍下一点（按钮坐标系是否翻转要分开算）
        let y = button.isFlipped ? button.bounds.maxY + 4 : button.bounds.minY - 4
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: y), in: button)
    }

    private func item(_ title: String, symbol: String, action: @escaping () -> Void) -> NSMenuItem {
        let item = ClosureMenuItem(title: title, handler: action)
        item.image = Symbols.image(symbol)
        return item
    }
}

/// 一个任务的实时转码速度。
///
/// 每隔至少 2 秒取一次「片内位置」，用前进的片长除以经过的时间；再做一次指数平滑，
/// 免得分片边界上的抖动让卡片在「流畅」和「刚好跟得上」之间来回跳。
struct SpeedTracker {
    var base: (position: Int64, at: Date)?
    private(set) var speed: Double?

    mutating func observe(position: Int64, at now: Date) {
        guard let base else {
            self.base = (position, now)
            return
        }
        let seconds = now.timeIntervalSince(base.at)
        guard seconds >= 2 else { return }
        // seek 重启会让位置倒退，这一段不算
        if position >= base.position {
            let instant = Double(position - base.position) / 1_000 / seconds
            speed = speed.map { $0 * 0.6 + instant * 0.4 } ?? instant
        }
        self.base = (position, now)
    }
}

/// 带闭包的菜单项，省得给每一项写一个 @objc 方法。
private final class ClosureMenuItem: NSMenuItem {
    private let handler: () -> Void

    init(title: String, handler: @escaping () -> Void) {
        self.handler = handler
        super.init(title: title, action: #selector(fire), keyEquivalent: "")
        target = self
    }

    required init(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    @objc private func fire() {
        handler()
    }
}
