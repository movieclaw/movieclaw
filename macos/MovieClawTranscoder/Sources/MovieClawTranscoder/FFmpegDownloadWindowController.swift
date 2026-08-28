import AppKit

/// 展示下载、校验和安装进度；实际工作由 FFmpegDownloadManager 串行执行。
@MainActor
final class FFmpegDownloadWindowController: NSWindowController, NSWindowDelegate {
    var onCancel: (() -> Void)?
    var onRetry: (() -> Void)?

    private let statusLabel = NSTextField(labelWithString: "准备处理 Jellyfin-ffmpeg")
    private let detailLabel = NSTextField(labelWithString: "")
    private let progressIndicator = NSProgressIndicator()
    private let actionButton = NSButton(title: "取消", target: nil, action: nil)
    private var state: FFmpegDownloadState = .idle

    init() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 210),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "Jellyfin-ffmpeg"
        window.isReleasedWhenClosed = false
        super.init(window: window)
        window.delegate = self
        window.contentView = makeContentView()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func showWindowAndFocus() {
        window?.center()
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func update(state: FFmpegDownloadState) {
        self.state = state
        switch state {
        case .idle:
            statusLabel.stringValue = "准备处理 Jellyfin-ffmpeg"
            detailLabel.stringValue = ""
            setIndeterminate(true)
            actionButton.title = "取消"
            actionButton.isEnabled = false
        case .checking:
            statusLabel.stringValue = "正在检查 Jellyfin 官方版本"
            detailLabel.stringValue = ""
            setIndeterminate(true)
            actionButton.title = "取消"
            actionButton.isEnabled = true
        case let .downloading(version, received, total):
            statusLabel.stringValue = "正在下载 Jellyfin-ffmpeg \(version)"
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
            detailLabel.stringValue = "不会写入 NAS 媒体目录"
            setIndeterminate(true)
            actionButton.title = "请等待"
            actionButton.isEnabled = false
        case let .ready(version):
            statusLabel.stringValue = "Jellyfin-ffmpeg \(version) 已安装"
            detailLabel.stringValue = "已完成 SHA-256 和硬件编码能力校验"
            progressIndicator.isIndeterminate = false
            progressIndicator.stopAnimation(nil)
            progressIndicator.doubleValue = 1
            actionButton.title = "关闭"
            actionButton.isEnabled = true
        case let .latest(version):
            statusLabel.stringValue = "Jellyfin-ffmpeg \(version) 已是最新版本"
            detailLabel.stringValue = ""
            progressIndicator.isIndeterminate = false
            progressIndicator.stopAnimation(nil)
            progressIndicator.doubleValue = 1
            actionButton.title = "关闭"
            actionButton.isEnabled = true
        case .cancelled:
            statusLabel.stringValue = "Jellyfin-ffmpeg 下载已取消"
            detailLabel.stringValue = "可以稍后从菜单栏重新下载"
            setIndeterminate(false)
            actionButton.title = "关闭"
            actionButton.isEnabled = true
        case let .failed(message):
            statusLabel.stringValue = "Jellyfin-ffmpeg 处理失败"
            detailLabel.stringValue = message
            detailLabel.toolTip = message
            setIndeterminate(false)
            actionButton.title = "重试"
            actionButton.isEnabled = true
        }
    }

    func windowWillClose(_ notification: Notification) {
        if state.isProcessing {
            onCancel?()
        }
    }

    private func makeContentView() -> NSView {
        statusLabel.font = .systemFont(ofSize: 14, weight: .medium)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.lineBreakMode = .byTruncatingMiddle
        detailLabel.maximumNumberOfLines = 2

        progressIndicator.style = .bar
        progressIndicator.isIndeterminate = true
        progressIndicator.controlSize = .regular
        progressIndicator.startAnimation(nil)

        actionButton.target = self
        actionButton.action = #selector(actionButtonClicked)

        let buttons = NSStackView(views: [NSView(), actionButton])
        buttons.orientation = .horizontal
        buttons.spacing = 8

        let stack = NSStackView(views: [statusLabel, detailLabel, progressIndicator, buttons])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        progressIndicator.widthAnchor.constraint(equalToConstant: 480).isActive = true
        buttons.widthAnchor.constraint(equalToConstant: 480).isActive = true

        let root = NSView()
        root.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 20),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -20),
            stack.topAnchor.constraint(equalTo: root.topAnchor, constant: 20),
            stack.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -16),
        ])
        return root
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
