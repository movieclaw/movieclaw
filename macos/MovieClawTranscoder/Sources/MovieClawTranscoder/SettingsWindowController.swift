import AppKit

/// 配置窗口保持为 AppKit 原生窗口，兼容 macOS 12，不引入 SwiftUI 的最低系统版本约束。
///
/// 布局按 macOS 系统设置的语言组织：小标题 + 圆角分组，组内每行左标签右控件，
/// 组下一行灰色脚注。**主界面只有一个必填项——movieclaw 地址**，其余四项都有
/// 可用默认值，收进折叠的「高级设置」（docs/design/device-auth.md §5.1）。
///
/// 配对是两步，且第二步必须在第一步验证通过之后才出现：
///
///   1. 填地址 → 「验证连接」拿到确定结论（版本 + 是否可达）；
///   2. 「请求接入」→ 显示配对码 → 人在网页批准 → 令牌自动回到本机。
///
/// 顺序不能反：自部署产品最容易劝退用户的就是「该填哪个地址」，把它单独作为
/// 第一步并当场验证，填错了立刻知道，而不是保存之后表现成「连不上」。
@MainActor
final class SettingsWindowController: NSWindowController {
    /// 保存非敏感设置（地址、名称、ffmpeg、并发、自启）。
    var onSave: ((WorkerSettingsDraft) throws -> Void)?
    /// 配对成功，把令牌交给调用方落钥匙串并重启 Worker。
    var onPaired: ((String) throws -> Void)?
    /// 清除本机配置与令牌。
    var onClear: (() throws -> Void)?

    private enum Stage {
        case idle                       // 待验证
        case verifying
        case verified(version: String)  // 已连接，尚未授权
        case pairing(DevicePairing.Grant)
        case paired
        case failed(String)
    }

    private let nasURLField = NSTextField()
    private let workerIDField = NSTextField()
    private let ffmpegPathField = NSTextField()
    private let maxJobsField = NSTextField()
    private let autoConnectButton = NSButton(checkboxWithTitle: "开机自动连接", target: nil, action: nil)

    private let statusLabel = NSTextField(labelWithString: "")
    private let codeLabel = NSTextField(labelWithString: "")
    private let hintLabel = NSTextField(labelWithString: "")
    private let primaryButton = NSButton(title: "验证连接", target: nil, action: nil)
    private let advancedToggle = NSButton(title: "高级设置", target: nil, action: nil)
    private let advancedBox = NSStackView()

    private var stage: Stage = .idle
    private var pollTask: Task<Void, Never>?
    private let tokenAlreadyConfigured: Bool

    init(snapshot: WorkerSettingsSnapshot) {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 560, height: 420),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "MovieClaw Transcoder"
        window.isReleasedWhenClosed = false
        tokenAlreadyConfigured = snapshot.tokenConfigured
        super.init(window: window)

        nasURLField.stringValue = snapshot.nasURL
        nasURLField.placeholderString = "http://10.1.1.5:3000"
        workerIDField.stringValue = snapshot.workerID
        ffmpegPathField.stringValue = snapshot.ffmpegPath
        maxJobsField.stringValue = String(snapshot.maxJobs)
        autoConnectButton.state = snapshot.autoConnect ? .on : .off

        window.contentView = makeContentView()
        stage = snapshot.tokenConfigured ? .paired : .idle
        render()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    deinit {
        pollTask?.cancel()
    }

    func showWindowAndFocus() {
        window?.center()
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    override func close() {
        pollTask?.cancel()
        pollTask = nil
        super.close()
    }

    // MARK: - 布局

    private func makeContentView() -> NSView {
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 18
        root.translatesAutoresizingMaskIntoConstraints = false
        root.edgeInsets = NSEdgeInsets(top: 20, left: 22, bottom: 18, right: 22)

        root.addArrangedSubview(makeSection(
            title: "连接",
            rows: [makeRow("movieclaw 地址", field: nasURLField)],
            footnote: "请填写局域网地址和端口。转码要来回传输大量视频分片，"
                + "走公网或反向代理会明显变慢，也更容易中断。"
        ))

        statusLabel.font = .systemFont(ofSize: 12)
        codeLabel.font = .monospacedSystemFont(ofSize: 26, weight: .semibold)
        codeLabel.alignment = .center
        hintLabel.font = .systemFont(ofSize: 11)
        hintLabel.textColor = .tertiaryLabelColor
        hintLabel.lineBreakMode = .byWordWrapping
        hintLabel.maximumNumberOfLines = 3
        hintLabel.preferredMaxLayoutWidth = 480

        let authStack = NSStackView(views: [statusLabel, codeLabel, hintLabel])
        authStack.orientation = .vertical
        authStack.alignment = .leading
        authStack.spacing = 8
        root.addArrangedSubview(makeSection(title: "授权", rows: [authStack], footnote: nil))

        advancedToggle.bezelStyle = .inline
        advancedToggle.isBordered = false
        advancedToggle.target = self
        advancedToggle.action = #selector(toggleAdvanced)
        advancedToggle.contentTintColor = .secondaryLabelColor
        root.addArrangedSubview(advancedToggle)

        advancedBox.orientation = .vertical
        advancedBox.alignment = .leading
        advancedBox.spacing = 10
        advancedBox.isHidden = true
        advancedBox.addArrangedSubview(makeRow("Worker 名称", field: workerIDField))
        advancedBox.addArrangedSubview(makeRow("ffmpeg 路径", field: ffmpegPathField))
        advancedBox.addArrangedSubview(makeRow("最大并发", field: maxJobsField))
        advancedBox.addArrangedSubview(autoConnectButton)
        root.addArrangedSubview(advancedBox)

        primaryButton.target = self
        primaryButton.action = #selector(primaryAction)
        primaryButton.keyEquivalent = "\r"
        let clearButton = NSButton(title: "清除配置", target: self, action: #selector(clearSettings))
        let closeButton = NSButton(title: "完成", target: self, action: #selector(finish))
        let buttons = NSStackView(views: [clearButton, NSView(), closeButton, primaryButton])
        buttons.orientation = .horizontal
        buttons.spacing = 8
        root.addArrangedSubview(buttons)
        buttons.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -44).isActive = true

        return root
    }

    /// 一个分组：小标题 + 内容 + 脚注，对应系统设置里的 inset grouped 结构。
    private func makeSection(title: String, rows: [NSView], footnote: String?) -> NSView {
        let heading = NSTextField(labelWithString: title)
        heading.font = .systemFont(ofSize: 12, weight: .medium)
        heading.textColor = .secondaryLabelColor

        let body = NSStackView(views: rows)
        body.orientation = .vertical
        body.alignment = .leading
        body.spacing = 10

        var views: [NSView] = [heading, body]
        if let footnote {
            let note = NSTextField(wrappingLabelWithString: footnote)
            note.font = .systemFont(ofSize: 11)
            note.textColor = .tertiaryLabelColor
            note.preferredMaxLayoutWidth = 480
            views.append(note)
        }
        let section = NSStackView(views: views)
        section.orientation = .vertical
        section.alignment = .leading
        section.spacing = 6
        return section
    }

    private func makeRow(_ title: String, field: NSTextField) -> NSView {
        field.translatesAutoresizingMaskIntoConstraints = false
        field.widthAnchor.constraint(greaterThanOrEqualToConstant: 360).isActive = true
        let label = NSTextField(labelWithString: title)
        label.alignment = .right
        label.widthAnchor.constraint(equalToConstant: 108).isActive = true
        let row = NSStackView(views: [label, field])
        row.orientation = .horizontal
        row.spacing = 10
        row.alignment = .centerY
        return row
    }

    // MARK: - 渲染

    /// 界面完全由 stage 推导，不在各处零散地改控件——配对是个状态机，
    /// 手工同步控件状态迟早会出现「按钮说等待批准、文案说未连接」这种撕裂。
    private func render() {
        switch stage {
        case .idle:
            statusLabel.stringValue = tokenAlreadyConfigured ? "● 已配对" : "○ 尚未连接"
            statusLabel.textColor = tokenAlreadyConfigured ? .systemGreen : .tertiaryLabelColor
            codeLabel.stringValue = ""
            hintLabel.stringValue = tokenAlreadyConfigured
                ? "改完地址点「验证连接」；重新配对会替换本机现有的授权。"
                : "先填好地址并验证连接，下一步才会生成配对码。"
            primaryButton.title = "验证连接"
            primaryButton.isEnabled = true
        case .verifying:
            statusLabel.stringValue = "◌ 正在连接…"
            statusLabel.textColor = .secondaryLabelColor
            codeLabel.stringValue = ""
            hintLabel.stringValue = ""
            primaryButton.title = "验证中…"
            primaryButton.isEnabled = false
        case let .verified(version):
            statusLabel.stringValue = "● 已连接 · movieclaw \(version)"
            statusLabel.textColor = .systemGreen
            codeLabel.stringValue = ""
            hintLabel.stringValue = "这台 Mac 还没有获得授权。下一步会生成一个配对码，"
                + "你在浏览器里确认一次就好，不需要在这里输入任何密钥。"
            primaryButton.title = "请求接入"
            primaryButton.isEnabled = true
        case let .pairing(grant):
            statusLabel.stringValue = "◌ 等待批准…"
            statusLabel.textColor = .systemOrange
            codeLabel.stringValue = grant.userCode
            hintLabel.stringValue = "请在浏览器打开 \(grant.verificationURI)，"
                + "核对上面的配对码后点「批准接入」。配对码不是密钥，被别人看到也拿不到权限。"
            primaryButton.title = "取消"
            primaryButton.isEnabled = true
        case .paired:
            statusLabel.stringValue = "● 已授权并连接"
            statusLabel.textColor = .systemGreen
            codeLabel.stringValue = ""
            hintLabel.stringValue = "配置结束，之后开机自动连接。"
                + "要停用这台机器，在网页「设置 → 设备」里吊销即可。"
            primaryButton.title = "重新配对"
            primaryButton.isEnabled = true
        case let .failed(message):
            statusLabel.stringValue = "● \(message)"
            statusLabel.textColor = .systemRed
            codeLabel.stringValue = ""
            hintLabel.stringValue = "确认地址填写正确且 movieclaw 正在运行，然后重试。"
            primaryButton.title = "重试"
            primaryButton.isEnabled = true
        }
    }

    private func transition(to next: Stage) {
        stage = next
        render()
    }

    // MARK: - 动作

    @objc private func toggleAdvanced() {
        advancedBox.isHidden.toggle()
        advancedToggle.title = advancedBox.isHidden ? "高级设置" : "隐藏高级设置"
    }

    @objc private func primaryAction() {
        switch stage {
        case .idle, .failed:
            verify()
        case .verified:
            startPairing()
        case .pairing:
            pollTask?.cancel()
            pollTask = nil
            transition(to: .idle)
        case .paired:
            transition(to: .idle)
        case .verifying:
            break
        }
    }

    /// 第一步：保存非敏感设置并验证地址可达。
    private func verify() {
        guard let maxJobs = Int(maxJobsField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            showError("最大并发必须是 1 到 4 之间的整数")
            return
        }
        if WorkerConfiguration.isInsecureHTTPAddress(nasURLField.stringValue), !confirmInsecureHTTP() {
            return
        }
        let draft = WorkerSettingsDraft(
            nasURL: nasURLField.stringValue,
            workerID: workerIDField.stringValue,
            ffmpegPath: ffmpegPathField.stringValue,
            maxJobs: maxJobs,
            autoConnect: autoConnectButton.state == .on
        )
        let url: URL
        do {
            url = try WorkerConfiguration.normalizedNASURL(draft.nasURL)
            try onSave?(draft)
        } catch {
            showError(error.localizedDescription)
            return
        }

        transition(to: .verifying)
        let pairing = DevicePairing(nasURL: url)
        Task { @MainActor in
            do {
                let version = try await pairing.verifyConnection()
                transition(to: .verified(version: version))
            } catch {
                transition(to: .failed("连不上：\(error.localizedDescription)"))
            }
        }
    }

    /// 第二步：发起接入请求并轮询兑换，直到拿到令牌或得到确定的失败结论。
    private func startPairing() {
        guard let url = try? WorkerConfiguration.normalizedNASURL(nasURLField.stringValue) else {
            showError("地址无效，请重新验证连接")
            return
        }
        let pairing = DevicePairing(nasURL: url)
        let name = workerIDField.stringValue

        pollTask?.cancel()
        pollTask = Task { @MainActor in
            do {
                let grant = try await pairing.authorize(clientName: name)
                transition(to: .pairing(grant))
                NSWorkspace.shared.open(URL(string: grant.verificationURI) ?? url)
                try await awaitApproval(pairing: pairing, grant: grant)
            } catch is CancellationError {
                // 用户点了取消，界面已切回 idle
            } catch {
                transition(to: .failed(error.localizedDescription))
            }
        }
    }

    private func awaitApproval(pairing: DevicePairing, grant: DevicePairing.Grant) async throws {
        var interval = max(1, grant.interval)
        let deadline = Date().addingTimeInterval(TimeInterval(grant.expiresIn))
        while Date() < deadline {
            try await Task.sleep(nanoseconds: UInt64(interval) * 1_000_000_000)
            try Task.checkCancellation()
            switch try await pairing.poll(deviceCode: grant.deviceCode) {
            case .pending:
                continue
            case .slowDown:
                // 服务端要求退避一拍。挑战没有作废，正常重试不该被当成攻击。
                interval += 1
            case let .granted(token, clientName):
                try onPaired?(token)
                AppLogger.shared.info("已完成配对：\(clientName.isEmpty ? grant.userCode : clientName)")
                transition(to: .paired)
                return
            case let .finished(reason):
                transition(to: .failed(reason))
                return
            }
        }
        transition(to: .failed("配对超时：没有等到批准"))
    }

    private func confirmInsecureHTTP() -> Bool {
        let alert = NSAlert()
        alert.messageText = "确认使用内网 HTTP？"
        alert.informativeText = "HTTP 只适合可信内网：源视频、转码分片和控制消息都不会加密。"
            + "请确认这个地址没有暴露到公网或不可信网络。"
        alert.alertStyle = .warning
        alert.addButton(withTitle: "继续使用 HTTP")
        alert.addButton(withTitle: "取消")
        return alert.runModal() == .alertFirstButtonReturn
    }

    @objc private func clearSettings() {
        let alert = NSAlert()
        alert.messageText = "清除本机配置？"
        alert.informativeText = "这会删除本机保存的地址与授权，需要重新配对才能继续转码。"
            + "服务端的授权记录不会一起删除——要彻底停用，请到网页「设置 → 设备」里吊销。"
        alert.alertStyle = .warning
        alert.addButton(withTitle: "清除")
        alert.addButton(withTitle: "取消")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        do {
            pollTask?.cancel()
            pollTask = nil
            try onClear?()
            close()
        } catch {
            showError(error.localizedDescription)
        }
    }

    @objc private func finish() {
        close()
    }

    private func showError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "无法保存设置"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "确定")
        alert.runModal()
    }
}
