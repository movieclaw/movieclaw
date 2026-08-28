import AppKit

/// 配置窗口保持为 AppKit 原生窗口，兼容 macOS 12，不引入 SwiftUI 的最低系统版本约束。
@MainActor
final class SettingsWindowController: NSWindowController {
    var onSave: ((WorkerSettingsDraft) throws -> Void)?
    var onClear: (() throws -> Void)?

    private let nasURLField = NSTextField()
    private let tokenField = NSSecureTextField()
    private let workerIDField = NSTextField()
    private let ffmpegPathField = NSTextField()
    private let maxJobsField = NSTextField()
    private let autoConnectButton = NSButton(checkboxWithTitle: "启动时自动连接", target: nil, action: nil)

    init(snapshot: WorkerSettingsSnapshot) {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 560, height: 330),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "MovieClaw Transcoder 设置"
        window.isReleasedWhenClosed = false
        super.init(window: window)
        nasURLField.stringValue = snapshot.nasURL
        nasURLField.placeholderString = "https://nas.example.com 或 http://10.1.1.5:3000"
        tokenField.placeholderString = snapshot.tokenConfigured ? "留空则保留当前 Token" : "请输入 Worker Token"
        workerIDField.stringValue = snapshot.workerID
        ffmpegPathField.stringValue = snapshot.ffmpegPath
        maxJobsField.stringValue = String(snapshot.maxJobs)
        autoConnectButton.state = snapshot.autoConnect ? .on : .off
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

    private func makeContentView() -> NSView {
        let container = NSStackView()
        container.orientation = .vertical
        container.alignment = .leading
        container.spacing = 12
        container.translatesAutoresizingMaskIntoConstraints = false

        container.addArrangedSubview(makeRow("NAS 地址", field: nasURLField))
        container.addArrangedSubview(makeRow("Worker Token", field: tokenField))
        container.addArrangedSubview(makeRow("Worker ID", field: workerIDField))
        container.addArrangedSubview(makeRow("ffmpeg 路径", field: ffmpegPathField))
        container.addArrangedSubview(makeRow("最大并发", field: maxJobsField))
        container.addArrangedSubview(autoConnectButton)

        let buttons = NSStackView()
        buttons.orientation = .horizontal
        buttons.spacing = 8
        buttons.addArrangedSubview(NSView())
        let clearButton = NSButton(title: "清除配置", target: self, action: #selector(clearSettings))
        let cancelButton = NSButton(title: "取消", target: self, action: #selector(cancel))
        let saveButton = NSButton(title: "保存并连接", target: self, action: #selector(saveSettings))
        saveButton.keyEquivalent = "\r"
        buttons.addArrangedSubview(clearButton)
        buttons.addArrangedSubview(cancelButton)
        buttons.addArrangedSubview(saveButton)
        buttons.translatesAutoresizingMaskIntoConstraints = false

        let root = NSView()
        root.addSubview(container)
        root.addSubview(buttons)
        NSLayoutConstraint.activate([
            container.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 20),
            container.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -20),
            container.topAnchor.constraint(equalTo: root.topAnchor, constant: 20),
            buttons.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 20),
            buttons.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -20),
            buttons.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -16),
            buttons.topAnchor.constraint(equalTo: container.bottomAnchor, constant: 18),
        ])
        return root
    }

    private func makeRow(_ title: String, field: NSTextField) -> NSView {
        field.translatesAutoresizingMaskIntoConstraints = false
        field.widthAnchor.constraint(greaterThanOrEqualToConstant: 380).isActive = true
        let label = NSTextField(labelWithString: title)
        label.alignment = .right
        label.widthAnchor.constraint(equalToConstant: 100).isActive = true
        let row = NSStackView(views: [label, field])
        row.orientation = .horizontal
        row.spacing = 10
        row.alignment = .centerY
        return row
    }

    @objc private func saveSettings() {
        guard let maxJobs = Int(maxJobsField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            showError("最大并发必须是 1 到 4 之间的整数")
            return
        }
        let draft = WorkerSettingsDraft(
            nasURL: nasURLField.stringValue,
            workerToken: tokenField.stringValue,
            workerID: workerIDField.stringValue,
            ffmpegPath: ffmpegPathField.stringValue,
            maxJobs: maxJobs,
            autoConnect: autoConnectButton.state == .on
        )
        if WorkerConfiguration.isInsecureHTTPAddress(draft.nasURL) {
            let alert = NSAlert()
            alert.messageText = "确认使用内网 HTTP？"
            alert.informativeText = "HTTP 只适合可信内网：源视频、转码分片、控制消息和临时 Token 都不会加密。请确认 NAS URL 中配置的 Nginx 映射端口没有暴露到公网或不可信网络。"
            alert.alertStyle = .warning
            alert.addButton(withTitle: "继续使用 HTTP")
            alert.addButton(withTitle: "取消")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        do {
            try onSave?(draft)
            close()
        } catch {
            showError(error.localizedDescription)
        }
    }

    @objc private func clearSettings() {
        let alert = NSAlert()
        alert.messageText = "清除 Worker 配置？"
        alert.informativeText = "这会删除本机保存的 NAS 地址、Worker ID 和 Keychain Token。"
        alert.alertStyle = .warning
        alert.addButton(withTitle: "清除")
        alert.addButton(withTitle: "取消")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        do {
            try onClear?()
            close()
        } catch {
            showError(error.localizedDescription)
        }
    }

    @objc private func cancel() {
        close()
    }

    private func showError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "配置保存失败"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "确定")
        alert.runModal()
    }
}
