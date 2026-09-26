import AppKit

/// 展示下载、校验和安装进度；实际工作由 FFmpegDownloadManager 串行执行。
///
/// 版式与设置窗一致：透明标题栏、红绿灯下方一块页眉（徽标 + 当前进展 + 细节），
/// 下面是进度条和按钮。徽标里的图形跟着状态换（下载 / 校验 / 完成 / 失败），
/// 细节文字最多三行折行——失败原因往往很长，原来一行截断后只剩中间一截。
@MainActor
final class FFmpegDownloadWindowController: NSWindowController, NSWindowDelegate {
    var onCancel: (() -> Void)?
    var onRetry: (() -> Void)?

    private static let windowWidth: CGFloat = 480
    private static let padding: CGFloat = 22

    private let header = WindowHeaderView(
        title: "准备处理 Jellyfin-ffmpeg",
        subtitle: "",
        glyph: .symbol("arrow.down"),
        subtitleWidth: FFmpegDownloadWindowController.windowWidth
            - FFmpegDownloadWindowController.padding * 2 - 56
    )
    private var statusLabel: NSTextField { header.titleLabel }
    private var detailLabel: NSTextField { header.subtitleLabel }
    private let progressIndicator = NSProgressIndicator()
    private let actionButton = NSButton(title: "取消", target: nil, action: nil)
    private var state: FFmpegDownloadState = .idle
    /// 标题栏高度，内容从这里往下排。
    private let titlebarInset: CGFloat
    private var contentStack: NSView?

    init() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: Self.windowWidth, height: 190),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "Jellyfin-ffmpeg"
        window.isReleasedWhenClosed = false
        titlebarInset = window.adoptGlassTitlebar()
        super.init(window: window)
        window.delegate = self
        window.contentView = makeContentView()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func showWindowAndFocus() {
        resizeToFit()
        window?.center()
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func update(state: FFmpegDownloadState) {
        self.state = state
        detailLabel.toolTip = nil
        switch state {
        case .idle:
            statusLabel.stringValue = "准备处理 Jellyfin-ffmpeg"
            detailLabel.stringValue = ""
            header.tile.set(.symbol("arrow.down"))
            setIndeterminate(true)
            actionButton.title = "取消"
            actionButton.isEnabled = false
        case .checking:
            statusLabel.stringValue = "正在检查 Jellyfin 官方版本"
            detailLabel.stringValue = ""
            header.tile.set(.symbol("magnifyingglass"))
            setIndeterminate(true)
            actionButton.title = "取消"
            actionButton.isEnabled = true
        case let .downloading(version, received, total):
            statusLabel.stringValue = "正在下载 Jellyfin-ffmpeg \(version)"
            header.tile.set(.symbol("arrow.down"))
            if let total, total > 0 {
                progressIndicator.isIndeterminate = false
                progressIndicator.stopAnimation(nil)
                progressIndicator.doubleValue = min(1, Double(received) / Double(total))
                detailLabel.stringValue = "\(formatBytes(received)) / \(formatBytes(total))"
            } else {
                detailLabel.stringValue = formatBytes(received)
                setIndeterminate(true)
            }
            actionButton.title = "取消"
            actionButton.isEnabled = true
        case let .installing(version):
            statusLabel.stringValue = "正在校验并安装 Jellyfin-ffmpeg \(version)"
            detailLabel.stringValue = "校验 SHA-256 与硬件编码能力，不会写入 NAS 媒体目录"
            header.tile.set(.symbol("checkmark.shield"))
            setIndeterminate(true)
            actionButton.title = "请等待"
            actionButton.isEnabled = false
        case let .ready(version):
            statusLabel.stringValue = "Jellyfin-ffmpeg \(version) 已安装"
            detailLabel.stringValue = "已完成 SHA-256 和硬件编码能力校验"
            header.tile.set(.symbol("checkmark"))
            progressIndicator.isIndeterminate = false
            progressIndicator.stopAnimation(nil)
            progressIndicator.doubleValue = 1
            actionButton.title = "关闭"
            actionButton.isEnabled = true
        case let .latest(version):
            statusLabel.stringValue = "Jellyfin-ffmpeg \(version) 已是最新版本"
            detailLabel.stringValue = ""
            header.tile.set(.symbol("checkmark"))
            progressIndicator.isIndeterminate = false
            progressIndicator.stopAnimation(nil)
            progressIndicator.doubleValue = 1
            actionButton.title = "关闭"
            actionButton.isEnabled = true
        case .cancelled:
            statusLabel.stringValue = "Jellyfin-ffmpeg 下载已取消"
            detailLabel.stringValue = "可以稍后从菜单栏重新下载"
            header.tile.set(.symbol("xmark"))
            setIndeterminate(false)
            actionButton.title = "关闭"
            actionButton.isEnabled = true
        case let .failed(message):
            statusLabel.stringValue = "Jellyfin-ffmpeg 处理失败"
            detailLabel.stringValue = message
            detailLabel.toolTip = message
            header.tile.set(.symbol("exclamationmark.triangle"))
            setIndeterminate(false)
            actionButton.title = "重试"
            actionButton.isEnabled = true
        }
        // 细节文字会在零到三行之间变化，窗口高度跟着走
        detailLabel.isHidden = detailLabel.stringValue.isEmpty
        resizeToFit()
    }

    func windowWillClose(_ notification: Notification) {
        if state.isProcessing {
            onCancel?()
        }
    }

    private func makeContentView() -> NSView {
        detailLabel.isHidden = true

        progressIndicator.style = .bar
        progressIndicator.isIndeterminate = true
        progressIndicator.controlSize = .regular
        // 本控制器全程用 0…1 的比例喂 doubleValue，而 NSProgressIndicator 默认量程是
        // 0…100。不改量程的话「已完成」会渲染成 1%，看起来像下载卡死。
        progressIndicator.minValue = 0
        progressIndicator.maxValue = 1
        progressIndicator.startAnimation(nil)

        actionButton.target = self
        actionButton.action = #selector(actionButtonClicked)
        actionButton.bezelStyle = .rounded
        if Glass.isAvailable {
            actionButton.controlSize = .large
        }

        let buttons = NSStackView(views: [SettingsStyle.flexibleSpacer(), actionButton])
        buttons.orientation = .horizontal
        buttons.spacing = 8

        let stack = NSStackView(views: [header, progressIndicator, buttons])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 16
        stack.edgeInsets = NSEdgeInsets(
            top: titlebarInset + 6, left: Self.padding, bottom: 18, right: Self.padding
        )
        stack.translatesAutoresizingMaskIntoConstraints = false
        let inner = Self.windowWidth - Self.padding * 2
        for view in [header, progressIndicator, buttons] {
            view.widthAnchor.constraint(equalToConstant: inner).isActive = true
        }

        // 与设置窗同一个做法：底边约束降到低优先级，高度完全由内容决定
        let root = NSView()
        root.addSubview(stack)
        let bottom = stack.bottomAnchor.constraint(equalTo: root.bottomAnchor)
        bottom.priority = .defaultLow
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            stack.topAnchor.constraint(equalTo: root.topAnchor),
            bottom,
        ])
        contentStack = stack
        return root
    }

    /// 窗口高度贴合内容，并保持顶边不动（窗口原点在左下角，直接改高度会往上长）。
    private func resizeToFit() {
        guard let window, let contentStack else { return }
        contentStack.layoutSubtreeIfNeeded()
        let height = ceil(contentStack.fittingSize.height)
        let target = window.frameRect(
            forContentRect: NSRect(x: 0, y: 0, width: Self.windowWidth, height: height)
        )
        var frame = window.frame
        guard abs(frame.height - target.height) > 0.5 else { return }
        frame.origin.y += frame.height - target.height
        frame.size = target.size
        window.setFrame(frame, display: true, animate: false)
    }

    @objc private func actionButtonClicked() {
        switch state {
        case .failed:
            onRetry?()
        case .checking, .downloading:
            onCancel?()
        default:
            close()
        }
    }

    private func setIndeterminate(_ value: Bool) {
        progressIndicator.isIndeterminate = value
        if value {
            progressIndicator.startAnimation(nil)
        } else {
            progressIndicator.stopAnimation(nil)
            progressIndicator.doubleValue = 0
        }
    }

    private func formatBytes(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: max(0, bytes), countStyle: .file)
    }
}
