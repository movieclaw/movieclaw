import AppKit

/// Jellyfin-ffmpeg 的状态条：图标 + 一句结论 + 细节 + 一个按钮，处理中时下面多一条进度。
///
/// 取代原来单独弹出的「Jellyfin-ffmpeg」窗口：下载、校验、失败重试都就地显示在
/// 设置的「转码」页（以及首次引导的最后一步）里。多弹一个窗口的问题是它和设置窗
/// 各说各的——设置里看不到正在下载，下载窗里又没法改路径。
@MainActor
final class FFmpegStatusView: NSView {
    enum Action {
        /// 下载 / 检查更新 / 重试。
        case start
        case cancel
    }

    var onAction: ((Action) -> Void)?

    private let icon = NSImageView()
    private let titleLabel = NSTextField(labelWithString: "")
    private let detailLabel: NSTextField
    private let button = NSButton(title: "", target: nil, action: nil)
    private let progress = NSProgressIndicator()
    private var buttonAction: Action = .start

    init(width: CGFloat) {
        detailLabel = PanelText.wrapping("", size: 11.5, lines: 3, width: width - 26 - 110, color: .secondaryLabelColor)
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false

        icon.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            icon.widthAnchor.constraint(equalToConstant: 18),
            icon.heightAnchor.constraint(equalToConstant: 18),
        ])
        titleLabel.font = .systemFont(ofSize: 13, weight: .medium)
        titleLabel.lineBreakMode = .byTruncatingTail
        titleLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let texts = NSStackView(views: [titleLabel, detailLabel])
        texts.orientation = .vertical
        texts.alignment = .leading
        texts.spacing = 2
        texts.setContentHuggingPriority(.defaultLow, for: .horizontal)

        button.target = self
        button.action = #selector(clicked)
        button.bezelStyle = .rounded
        button.setContentHuggingPriority(.required, for: .horizontal)
        button.setContentCompressionResistancePriority(.required, for: .horizontal)

        let row = NSStackView(views: [icon, texts, SettingsStyle.flexibleSpacer(), button])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 8

        progress.style = .bar
        progress.controlSize = .small
        // 用 0…1 的比例喂 doubleValue，量程要跟着改（默认 0…100，完成会显示成 1%）
        progress.minValue = 0
        progress.maxValue = 1

        let column = NSStackView(views: [row, progress])
        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = 8
        row.widthAnchor.constraint(equalTo: column.widthAnchor).isActive = true
        progress.widthAnchor.constraint(equalTo: column.widthAnchor).isActive = true
        pin(column, padding: 0)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    /// - Parameters:
    ///   - installedVersion: 本 App 管理的 Jellyfin-ffmpeg 版本（没下过为 nil）。
    ///   - waitingForJobs: 要更新但手上还有任务，正在等它们转完。
    func apply(_ state: FFmpegDownloadState, installedVersion: String?, waitingForJobs: Bool = false) {
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.toolTip = nil
        var symbol = "shippingbox"
        var tint: NSColor = .secondaryLabelColor
        var detail = ""
        var fraction: Double??  // nil = 不显示进度条；.some(nil) = 不确定进度
        button.isHidden = false
        button.isEnabled = true

        switch state {
        case .idle, .cancelled:
            if let installedVersion {
                titleLabel.stringValue = "Jellyfin-ffmpeg \(installedVersion)"
                detail = state == .cancelled ? "更新已取消，仍在使用当前版本。" : "由本 App 下载和管理，校验过硬件编码能力。"
                symbol = "checkmark.seal.fill"
                tint = .systemGreen
                setButton("检查更新", .start)
            } else {
                titleLabel.stringValue = state == .cancelled ? "下载已取消" : "还没有下载 Jellyfin-ffmpeg"
                detail = "硬件转码需要带 VideoToolbox 的 Jellyfin-ffmpeg，从 Jellyfin 官方下载。"
                symbol = "arrow.down.circle"
                tint = .controlAccentColor
                setButton("下载", .start)
            }
        case .checking:
            titleLabel.stringValue = "正在检查 Jellyfin 官方版本"
            symbol = "magnifyingglass"
            fraction = .some(nil)
            setButton("取消", .cancel)
        case let .downloading(version, received, total):
            titleLabel.stringValue = "正在下载 Jellyfin-ffmpeg \(version)"
            symbol = "arrow.down.circle"
            tint = .controlAccentColor
            if let total, total > 0 {
                detail = "\(Self.bytes(received)) / \(Self.bytes(total))"
                fraction = .some(min(1, Double(received) / Double(total)))
            } else {
                detail = Self.bytes(received)
                fraction = .some(nil)
            }
            setButton("取消", .cancel)
        case let .installing(version):
            titleLabel.stringValue = "正在校验并安装 \(version)"
            detail = "校验 SHA-256 与硬件编码能力，不会写入 NAS 媒体目录。"
            symbol = "checkmark.shield"
            tint = .controlAccentColor
            fraction = .some(nil)
            button.isHidden = true
        case let .ready(version):
            titleLabel.stringValue = "Jellyfin-ffmpeg \(version) 已安装"
            detail = "已完成 SHA-256 与硬件编码能力校验。"
            symbol = "checkmark.seal.fill"
            tint = .systemGreen
            setButton("检查更新", .start)
        case let .latest(version):
            titleLabel.stringValue = "Jellyfin-ffmpeg \(version) 已是最新版本"
            detail = "由本 App 下载和管理，校验过硬件编码能力。"
            symbol = "checkmark.seal.fill"
            tint = .systemGreen
            setButton("检查更新", .start)
        case let .failed(message):
            titleLabel.stringValue = "Jellyfin-ffmpeg 处理失败"
            detail = message
            detailLabel.textColor = .systemRed
            detailLabel.toolTip = message
            symbol = "exclamationmark.triangle.fill"
            tint = .systemRed
            setButton("重试", .start)
        }

        if waitingForJobs, !state.isProcessing {
            titleLabel.stringValue = "等当前转码任务结束后开始更新"
            detail = "已暂停接收新任务，手上的任务转完就开始。"
            symbol = "hourglass"
            tint = .secondaryLabelColor
            fraction = .some(nil)
            setButton("取消", .cancel)
        }

        icon.image = Symbols.image(symbol, pointSize: 15, weight: .medium)
        icon.contentTintColor = tint
        detailLabel.stringValue = detail
        detailLabel.isHidden = detail.isEmpty
        switch fraction {
        case .none:
            progress.isHidden = true
            progress.stopAnimation(nil)
        case let .some(value?):
            progress.isHidden = false
            progress.isIndeterminate = false
            progress.stopAnimation(nil)
            progress.doubleValue = value
        case .some(nil):
            progress.isHidden = false
            progress.isIndeterminate = true
            progress.startAnimation(nil)
        }
    }

    private func setButton(_ title: String, _ action: Action) {
        button.title = title
        buttonAction = action
    }

    @objc private func clicked() {
        onAction?(buttonAction)
    }

    private static func bytes(_ value: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: max(0, value), countStyle: .file)
    }
}
