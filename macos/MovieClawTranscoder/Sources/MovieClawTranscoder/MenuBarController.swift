import AppKit

/// 菜单栏只负责展示和派发用户动作；网络和 ffmpeg 生命周期仍由 AppDelegate/WorkerClient 管理。
@MainActor
final class MenuBarController: NSObject {
    private let statusItem: NSStatusItem
    private let menu = NSMenu()
    private let stateItem = NSMenuItem(title: "状态：未配置", action: nil, keyEquivalent: "")
    private let workerItem = NSMenuItem(title: "Worker：-", action: nil, keyEquivalent: "")
    private let jobItem = NSMenuItem(title: "任务：无", action: nil, keyEquivalent: "")
    private let errorItem = NSMenuItem(title: "最近错误：无", action: nil, keyEquivalent: "")
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
    }

    func update(status: WorkerStatus?, configured: Bool) {
        guard let status else {
            stateItem.title = configured ? "状态：已配置，未启动" : "状态：未配置"
            workerItem.title = "Worker：-"
            jobItem.title = "任务：无"
            errorItem.title = "最近错误：无"
            connectItem.title = configured ? "连接" : "打开设置"
            connectItem.isEnabled = true
            drainItem.isEnabled = false
            reconnectItem.isEnabled = false
            statusItem.button?.title = ""
            statusItem.button?.toolTip = "MovieClaw Transcoder"
            return
        }
        let state = status.state.displayName
        stateItem.title = "状态：\(state)"
        workerItem.title = "Worker：\(status.workerID) · ffmpeg \(status.ffmpegVersion)"
        if let jobID = status.currentJobID {
            // 显示片名而不是 job id：菜单栏是给人看的，一串 ULID 说明不了
            // 「正在转的是哪一部」。服务端没下发名字时才退回 id。
            let label = status.currentJobName.map { Self.shorten($0) } ?? jobID
            let progress = status.currentProgress?.outTimeMS.map { " · \(formatDuration(milliseconds: $0))" } ?? ""
            jobItem.title = "任务：\(label)\(progress)"
            jobItem.toolTip = status.currentJobName
        } else {
            jobItem.toolTip = nil
            jobItem.title = "任务：无（\(status.activeJobs)/\(status.maxJobs)）"
        }
        errorItem.title = "最近错误：\(status.lastError ?? "无")"
        connectItem.title = status.state == .stopped || status.state == .error ? "连接" : "断开连接"
        drainItem.title = status.state == .draining ? "恢复接收任务" : "暂停接收任务"
        drainItem.isEnabled = status.state != .stopped && status.state != .error
        reconnectItem.isEnabled = status.state != .stopped
        statusItem.button?.title = status.state == .busy ? " •" : ""
        statusItem.button?.toolTip = "MovieClaw Transcoder：\(state)"
    }

    func update(ffmpeg: FFmpegMenuState) {
        ffmpegItem.title = ffmpeg.title
        ffmpegItem.isEnabled = ffmpeg.isEnabled
    }

    private func buildMenu() {
        menu.addItem(stateItem)
        menu.addItem(workerItem)
        menu.addItem(jobItem)
        menu.addItem(errorItem)
        menu.addItem(.separator())
        connectItem.target = self
        drainItem.target = self
        reconnectItem.target = self
        menu.addItem(connectItem)
        menu.addItem(drainItem)
        menu.addItem(reconnectItem)
        menu.addItem(.separator())
        ffmpegItem.target = self
        menu.addItem(ffmpegItem)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "设置…", action: #selector(openSettings), keyEquivalent: ","))
        menu.addItem(NSMenuItem(title: "打开日志", action: #selector(openLog), keyEquivalent: "l"))
        menu.addItem(NSMenuItem(title: "复制诊断信息", action: #selector(copyDiagnostics), keyEquivalent: ""))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "退出 MovieClaw Transcoder", action: #selector(quit), keyEquivalent: "q"))
        for item in menu.items where item.action != nil {
            item.target = self
        }
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

    /// 片名收窄到菜单可读的长度。发布组名往往很长，中间省略比截断更好认——
    /// 片名在前、分辨率与压制组在后，两头都是信息量最大的部分。
    private static func shorten(_ name: String, limit: Int = 42) -> String {
        guard name.count > limit else { return name }
        let head = name.prefix(limit / 2 - 1)
        let tail = name.suffix(limit / 2 - 2)
        return "\(head)…\(tail)"
    }

    private func formatDuration(milliseconds: Int64) -> String {
        let totalSeconds = max(0, milliseconds / 1_000)
        return String(format: "%02d:%02d", totalSeconds / 60, totalSeconds % 60)
    }
}
