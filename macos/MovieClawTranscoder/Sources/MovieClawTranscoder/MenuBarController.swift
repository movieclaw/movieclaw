import AppKit

/// 菜单栏只负责展示和派发用户动作；网络和 ffmpeg 生命周期仍由 AppDelegate/WorkerClient 管理。
///
/// 菜单分两层：顶部一张状态卡片（``MenuStatusView``，固定宽度、长文字折行），
/// 下面是带 SF Symbols 图标的标准菜单项。菜单的液态玻璃外观、圆角高亮都由系统
/// 绘制（macOS 26 SDK 编译即生效），这里不自绘任何菜单背景。
@MainActor
final class MenuBarController: NSObject {
    private let statusItem: NSStatusItem
    private let menu = NSMenu()
    private let statusView = MenuStatusView()
    private let statusMenuItem = NSMenuItem()
    private let connectItem = NSMenuItem(title: "连接", action: #selector(connect), keyEquivalent: "")
    private let drainItem = NSMenuItem(title: "暂停接收任务", action: #selector(toggleDraining), keyEquivalent: "")
    private let reconnectItem = NSMenuItem(title: "立即重连", action: #selector(reconnect), keyEquivalent: "")
    private let ffmpegItem = NSMenuItem(title: "下载 Jellyfin-ffmpeg", action: #selector(manageFFmpeg), keyEquivalent: "")

    var onConnect: (() -> Void)?
    var onReconnect: (() -> Void)?
    var onToggleDraining: (() -> Void)?
    var onManageFFmpeg: (() -> Void)?
    var onOpenSettings: (() -> Void)?
    var onOpenLog: (() -> Void)?
    var onCopyDiagnostics: (() -> Void)?
    var onQuit: (() -> Void)?

    /// 设置里填的 movieclaw 地址，状态卡片的副标题用它。配置变化时由 AppMain 推过来。
    var nasAddress: String? {
        didSet { refresh() }
    }

    private var status: WorkerStatus?
    private var configured = false

    override init() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        super.init()
        statusItem.button?.image = MenuBarIcon.statusItemImage()
        // 忙时在图标右侧补一个圆点。图标本身保持不变——菜单栏图标频繁换形
        // 会让人在一排图标里认不出自家 App
        statusItem.button?.imagePosition = .imageLeading
        statusItem.button?.title = ""
        statusItem.button?.toolTip = "MovieClaw Transcoder"
        statusItem.menu = menu
        buildMenu()
        refresh()
    }

    func update(status: WorkerStatus?, configured: Bool) {
        self.status = status
        self.configured = configured
        refresh()
    }

    func update(ffmpeg: FFmpegMenuState) {
        ffmpegItem.title = ffmpeg.title
        ffmpegItem.image = Symbols.image(ffmpeg.symbolName)
        ffmpegItem.isEnabled = ffmpeg.isEnabled
    }

    /// 状态卡片的内容。纯函数：只看传进来的状态，便于单测覆盖各种组合。
    static func statusModel(
        status: WorkerStatus?,
        configured: Bool,
        nasAddress: String?
    ) -> MenuStatusView.Model {
        let presentation = WorkerStatePresentation.make(status?.state, configured: configured)
        let host = DisplayText.host(of: nasAddress)
        guard let status else {
            if configured {
                return MenuStatusView.Model(
                    presentation: presentation,
                    subtitle: host.map { "已配对 · \($0)" } ?? "已配对",
                    card: .init(symbol: "bolt.slash", title: "没有连接", detail: "点「连接」开始接收转码任务"),
                    error: nil,
                    footnote: nil
                )
            }
            return MenuStatusView.Model(
                presentation: presentation,
                subtitle: "还没有连接任何 movieclaw",
                card: .init(
                    symbol: "link",
                    title: "还没有配对",
                    detail: "打开设置填写 movieclaw 地址，再到网页上批准这台 Mac。"
                ),
                error: nil,
                footnote: nil
            )
        }

        let error = status.lastError.flatMap { $0.isEmpty ? nil : $0 }
        var card: MenuStatusView.Model.Card?
        switch status.state {
        case .busy, .paused:
            if let jobID = status.currentJobID {
                card = jobCard(status: status, jobID: jobID)
            }
        case .draining:
            card = .init(
                symbol: "pause.circle",
                title: "暂停接收新任务",
                detail: status.activeJobs > 0
                    ? "手上的 \(status.activeJobs) 个任务会继续转完"
                    : "点「恢复接收任务」继续接单"
            )
        case .starting, .connecting:
            card = .init(symbol: "antenna.radiowaves.left.and.right", title: status.message)
        case .reconnecting:
            // 断线时 message 先是错误原因、随后是「N 秒后重连」；错误原因已在下面的
            // 错误卡片里，这里不再重复一遍
            let detail = status.message == error ? nil : status.message
            card = .init(symbol: "arrow.triangle.2.circlepath", title: "正在重连 NAS", detail: detail)
        case .stopped:
            card = .init(symbol: "bolt.slash", title: "已停止", detail: "点「连接」重新开始接收任务")
        case .ready, .error, .unconfigured:
            card = nil
        }

        return MenuStatusView.Model(
            presentation: presentation,
            subtitle: [status.workerID, host].compactMap { $0 }.joined(separator: " · "),
            card: card,
            error: error,
            footnote: "ffmpeg \(DisplayText.ffmpegVersion(status.ffmpegVersion))"
                + " · 并发 \(status.activeJobs)/\(status.maxJobs)"
        )
    }

    /// 正在转的片子：片名（服务端没下发时退回 job id）+ 转到哪了 + 速度。
    private static func jobCard(status: WorkerStatus, jobID: String) -> MenuStatusView.Model.Card {
        var parts: [String] = []
        if status.state == .paused {
            // NAS 在转码头领先播放足够远、或磁盘吃紧时让任务歇一歇，之后自动续上
            parts.append("暂停中，NAS 会在需要时自动继续")
        } else if let milliseconds = status.currentProgress?.outTimeMS {
            parts.append("已转到 \(DisplayText.clock(milliseconds: milliseconds))")
            if let speed = DisplayText.speed(status.currentProgress?.speed) {
                parts.append("速度 \(speed)")
            }
        } else {
            parts.append("正在起转…")
        }
        if status.activeJobs > 1 {
            parts.append("另有 \(status.activeJobs - 1) 个任务")
        }
        return .init(
            symbol: "film",
            title: status.currentJobName ?? jobID,
            detail: parts.joined(separator: " · "),
            tooltip: status.currentJobName
        )
    }

    private func refresh() {
        statusView.apply(Self.statusModel(status: status, configured: configured, nasAddress: nasAddress))

        guard let status else {
            connectItem.title = configured ? "连接" : "打开设置…"
            connectItem.image = Symbols.image(configured ? "play.circle" : "gearshape")
            connectItem.isEnabled = true
            drainItem.title = "暂停接收任务"
            drainItem.image = Symbols.image("pause.circle")
            drainItem.isEnabled = false
            reconnectItem.isEnabled = false
            statusItem.button?.title = ""
            statusItem.button?.toolTip = "MovieClaw Transcoder"
            return
        }
        let disconnected = status.state == .stopped || status.state == .error
        connectItem.title = disconnected ? "连接" : "断开连接"
        connectItem.image = Symbols.image(disconnected ? "play.circle" : "stop.circle")
        connectItem.isEnabled = true
        let draining = status.state == .draining
        drainItem.title = draining ? "恢复接收任务" : "暂停接收任务"
        drainItem.image = Symbols.image(draining ? "play.circle" : "pause.circle")
        drainItem.isEnabled = !disconnected
        reconnectItem.isEnabled = status.state != .stopped
        statusItem.button?.title = status.state == .busy ? " •" : ""
        let presentation = WorkerStatePresentation.make(status.state, configured: configured)
        statusItem.button?.toolTip = "MovieClaw Transcoder：\(presentation.title)"
    }

    private func buildMenu() {
        // 菜单项的可用性由 refresh() 按状态手工决定。自动启用开着的话，AppKit 只看
        // target 能不能响应 action，「未连接时不能暂停接单」这类设置会被悄悄覆盖掉
        menu.autoenablesItems = false

        statusMenuItem.view = statusView
        menu.addItem(statusMenuItem)
        menu.addItem(.separator())

        reconnectItem.image = Symbols.image("arrow.clockwise")
        for item in [connectItem, drainItem, reconnectItem] {
            item.target = self
            menu.addItem(item)
        }
        menu.addItem(.separator())
        ffmpegItem.target = self
        ffmpegItem.image = Symbols.image(FFmpegMenuState.download.symbolName)
        menu.addItem(ffmpegItem)
        menu.addItem(.separator())
        menu.addItem(actionItem("设置…", symbol: "gearshape", action: #selector(openSettings), key: ","))
        menu.addItem(actionItem("打开日志", symbol: "doc.text.magnifyingglass", action: #selector(openLog), key: "l"))
        menu.addItem(actionItem("复制诊断信息", symbol: "doc.on.clipboard", action: #selector(copyDiagnostics), key: ""))
        menu.addItem(.separator())
        menu.addItem(actionItem("退出 MovieClaw Transcoder", symbol: "power", action: #selector(quit), key: "q"))
    }

    private func actionItem(_ title: String, symbol: String, action: Selector, key: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        item.image = Symbols.image(symbol)
        return item
    }

    @objc private func connect() {
        onConnect?()
    }

    @objc private func reconnect() {
        onReconnect?()
    }

    @objc private func toggleDraining() {
        onToggleDraining?()
    }

    @objc private func manageFFmpeg() {
        onManageFFmpeg?()
    }

    @objc private func openSettings() {
        onOpenSettings?()
    }

    @objc private func openLog() {
        onOpenLog?()
    }

    @objc private func copyDiagnostics() {
        onCopyDiagnostics?()
    }

    @objc private func quit() {
        onQuit?()
    }
}
