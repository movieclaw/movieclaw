import AppKit

/// 设置窗位置的持久化键。文件级常量：init 里要在 super.init 之前用到它。
private let settingsFrameAutosaveName = "MovieClawTranscoderSettings"

/// 配置窗口保持为 AppKit 原生窗口，兼容 macOS 12，不引入 SwiftUI 的最低系统版本约束。
///
/// 布局按 macOS 系统设置的语言组织：小标题 + 圆角分组，组内每行左标签右控件，
/// 组下一行灰色脚注。**主界面只有一个必填项——movieclaw 地址**，其余四项都有
/// 可用默认值，收进折叠的「高级设置」（docs/design/device-auth.md §5.1）。
///
/// 配对是两步，且第二步必须在第一步验证通过之后才出现：
///
///   1. 填地址 → 「验证连接」拿到确定结论（版本 + 是否可达）；
///      地址可以点「在局域网中查找」自动填（§6.5），首次打开且地址为空时自动跑一次；
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
        case verified(service: String)  // 已连接，尚未授权
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
    private let discoverButton = NSButton(title: "在局域网中查找", target: nil, action: nil)
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
        // 惯例是「<App 名> 设置」：菜单栏 App 的窗口会混在窗口列表里，
        // 只写 App 名分不清这是设置窗还是别的什么
        window.title = "MovieClaw Transcoder 设置"
        window.isReleasedWhenClosed = false
        // 记住用户挪过的位置：每次打开都跳回屏幕正中是很烦的
        window.setFrameAutosaveName(settingsFrameAutosaveName)
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
        // 没有存过位置时才居中，存过就用上次的
        if window?.setFrameUsingName(settingsFrameAutosaveName) != true {
            window?.center()
        }
        window?.makeKeyAndOrderFront(nil)
        // activate(ignoringOtherApps:) 在 macOS 14 已废弃，且抢焦点的行为一向
        // 不被推荐；新 API 由系统判断该不该把 App 提到前面
        if #available(macOS 14.0, *) {
            NSApp.activate()
        } else {
            NSApp.activate(ignoringOtherApps: true)
        }
        // 地址还空着就自动找一次：第一次配置的人正卡在「该填什么」上。
        // 已经填过的不动——用户敲进去的地址不该被一次后台广播覆盖。
        if nasURLField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            discover(auto: true)
        }
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

        // 「在局域网中查找」属于「连接」这件事，放在地址输入框正下方；
        // 底部动作栏只留窗口级动作（HIG：主按钮在右下角，右侧不堆多个动作）
        discoverButton.target = self
        discoverButton.action = #selector(discoverAction)
        discoverButton.bezelStyle = .rounded
        discoverButton.controlSize = .small
        let discoverRow = NSStackView(views: [makeSpacer(width: 108), discoverButton])
        discoverRow.orientation = .horizontal
        discoverRow.spacing = 10
        discoverRow.alignment = .centerY

        root.addArrangedSubview(makeSection(
            title: "连接",
            rows: [makeRow("movieclaw 地址", field: nasURLField), discoverRow],
            footnote: "不知道地址就点「在局域网中查找」。请填写局域网地址和端口："
                + "转码要来回传输大量视频分片，走公网或反向代理会明显变慢，也更容易中断。"
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
        // Esc 关窗：菜单栏 App 没有主菜单，⌘W 走不通，键盘用户只剩这条路
        closeButton.keyEquivalent = "\u{1b}"
        let buttons = NSStackView(views: [clearButton, NSView(), closeButton, primaryButton])
        buttons.orientation = .horizontal
        buttons.spacing = 8
        root.addArrangedSubview(buttons)
        buttons.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -44).isActive = true

        return root
    }

    /// 占位视图：让分组内不带标签的行与上面带标签的行左对齐。
    private func makeSpacer(width: CGFloat) -> NSView {
        let spacer = NSView()
        spacer.translatesAutoresizingMaskIntoConstraints = false
        spacer.widthAnchor.constraint(equalToConstant: width).isActive = true
        return spacer
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
        // 标签是独立控件，不加这句 VoiceOver 读到输入框只会说「文本框」
        field.setAccessibilityLabel(title)
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
        case let .verified(service):
            statusLabel.stringValue = "● 已连接 · \(service)"
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
            Task { await verify() }
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

    @objc private func discoverAction() {
        discover(auto: false)
    }

    /// 在局域网里找 movieclaw，把结果填进地址框（docs/design/device-auth.md §6.5）。
    ///
    /// **只填不存**：发现到的地址交给用户过目，由他点「验证连接」拍板。服务端
    /// 优先返回的是用户为播放器配的「对外访问地址」，那可能是反向代理域名——
    /// 对 Worker 来说走反代明显更慢，这个判断得留给人。
    ///
    /// auto 为 true 表示是打开窗口时自动触发的：没找到就安静收场，
    /// 不弹窗打断一个还没开口的用户。
    private func discover(auto: Bool) {
        discoverButton.isEnabled = false
        discoverButton.title = "查找中…"
        Task { [weak self] in
            let found = await Task.detached(priority: .userInitiated) {
                LANDiscovery.find(timeout: 1.5)
            }.value
            guard let self else { return }
            self.discoverButton.isEnabled = true
            self.discoverButton.title = "在局域网中查找"
            await self.applyDiscovery(found, auto: auto)
        }
    }

    private func applyDiscovery(_ found: [LANDiscovery.Server], auto: Bool) async {
        guard let chosen = await chooseDiscovered(found, auto: auto) else { return }
        nasURLField.stringValue = chosen.address
        // 回到待验证态：地址换了，之前那次验证的结论就作废了
        transition(to: .idle)
        hintLabel.stringValue = "已填入局域网中找到的地址（\(chosen.displayName)）。"
            + "确认无误后点「验证连接」；走反向代理的地址传分片会明显变慢，"
            + "内网直连地址更合适。"
    }

    /// 一台直接用，多台弹菜单让选，一台都没有按 auto 决定是否提示。
    private func chooseDiscovered(
        _ found: [LANDiscovery.Server], auto: Bool
    ) async -> LANDiscovery.Server? {
        if found.isEmpty {
            if !auto {
                let alert = NSAlert()
                alert.messageText = "局域网里没有找到 movieclaw"
                alert.informativeText = "可能是跨网段或 VPN、服务端关掉了「Jellyfin 兼容层」，"
                    + "或者它跑在桥接网络里。直接填写地址即可，形如 http://10.1.1.5:3000。"
                alert.alertStyle = .informational
                alert.addButton(withTitle: "好")
                _ = await runSheet(alert)
            }
            return nil
        }
        if found.count == 1 {
            return found[0]
        }
        let alert = NSAlert()
        alert.messageText = "局域网里找到 \(found.count) 台"
        alert.informativeText = "选择要连接的那一台。"
        let picker = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        for server in found {
            picker.addItem(withTitle: "\(server.displayName) — \(server.address)")
        }
        alert.accessoryView = picker
        alert.addButton(withTitle: "使用这台")
        alert.addButton(withTitle: "取消")
        guard await runSheet(alert) == .alertFirstButtonReturn else { return nil }
        let index = picker.indexOfSelectedItem
        return found.indices.contains(index) ? found[index] : nil
    }

    /// 第一步：保存非敏感设置并验证地址可达。
    private func verify() async {
        guard let maxJobs = Int(maxJobsField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            showError("最大并发必须是 1 到 4 之间的整数")
            return
        }
        if WorkerConfiguration.isInsecureHTTPAddress(nasURLField.stringValue),
           await confirmInsecureHTTP() == false {
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
        do {
            let service = try await pairing.verifyConnection()
            transition(to: .verified(service: service))
        } catch {
            transition(to: .failed("连不上：\(error.localizedDescription)"))
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

    private func confirmInsecureHTTP() async -> Bool {
        let alert = NSAlert()
        alert.messageText = "确认使用内网 HTTP？"
        alert.informativeText = "HTTP 只适合可信内网：源视频、转码分片和控制消息都不会加密。"
            + "请确认这个地址没有暴露到公网或不可信网络。"
        alert.alertStyle = .warning
        alert.addButton(withTitle: "继续使用 HTTP")
        alert.addButton(withTitle: "取消")
        return await runSheet(alert) == .alertFirstButtonReturn
    }

    @objc private func clearSettings() {
        Task { await confirmAndClear() }
    }

    private func confirmAndClear() async {
        let alert = NSAlert()
        alert.messageText = "清除本机配置？"
        alert.informativeText = "这会删除本机保存的地址与授权，需要重新配对才能继续转码。"
            + "服务端的授权记录不会一起删除——要彻底停用，请到网页「设置 → 设备」里吊销。"
        alert.alertStyle = .warning
        // 破坏性动作标红，并把「取消」设为默认回车项——HIG：确认框里
        // 回车应当落在安全的那一侧，别让手快的人一路回车删掉配置
        let destructive = alert.addButton(withTitle: "清除")
        if #available(macOS 11.0, *) { destructive.hasDestructiveAction = true }
        destructive.keyEquivalent = ""
        alert.addButton(withTitle: "取消").keyEquivalent = "\r"
        guard await runSheet(alert) == .alertFirstButtonReturn else { return }
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

    // MARK: - 提示

    /// 窗口范围的提示一律用 sheet（从标题栏下滑、附着在本窗口上），不用独立弹窗。
    ///
    /// HIG 的分界是「这件事只关乎这个窗口，还是要打断整个 App」——这里每一处
    /// 都属于前者。菜单栏 App 尤其要守这条：没有 Dock 图标，app-modal 弹窗
    /// 可能落在用户找不到的层级上，表现成「点了按钮没反应」。
    private func runSheet(_ alert: NSAlert) async -> NSApplication.ModalResponse {
        guard let window else { return alert.runModal() }
        return await withCheckedContinuation { continuation in
            alert.beginSheetModal(for: window) { continuation.resume(returning: $0) }
        }
    }

    /// 只是通知一声、不需要等结果的提示。
    private func showError(_ message: String, title: String = "无法继续") {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "好")
        guard let window else {
            alert.runModal()
            return
        }
        alert.beginSheetModal(for: window, completionHandler: nil)
    }
}
