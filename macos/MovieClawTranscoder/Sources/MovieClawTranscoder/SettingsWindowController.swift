import AppKit

/// 设置窗位置的持久化键。文件级常量：init 里要在 super.init 之前用到它。
private let settingsFrameAutosaveName = "MovieClawTranscoderSettings"

/// 配置窗口保持为 AppKit 原生窗口，兼容 macOS 12，不引入 SwiftUI 的最低系统版本约束。
///
/// 布局照评审 demo（`device-pairing.html` 的 `workerShell`）逐条对齐：分区
/// 小标题 + 圆角分组，组内每行「左标题 / 右取值」，行间发丝线，组下一行灰色
/// 脚注，动作栏在底部右侧、左端放一句当前状态。
///
/// **每个状态只显示它需要的那几行。** 这是 demo 最关键的一点，也是这一版
/// 主要补回来的东西：等批准时窗口里只有配对码，配好之后地址框整个消失、变成
/// 一行「服务器 10.1.1.5:3000」。用户任何时刻看到的都只是当下要看的东西。
///
/// 配对是一次点击。填好地址点「连接并配对」，App 连着做完三件事：保存设置、
/// 验证地址真的通、发起接入请求并显示配对码。原来这里是两步（先「验证连接」，
/// 成功后再点「请求接入」），拆开的初衷是让地址填错当场可见，但代价是把一个
/// 中间结论摆成了必须点掉的关卡——「已连接」从来不是用户想要的终点。地址错了
/// 照样当场可见：验证失败就停在失败态，根本走不到发起请求那一步。
///
/// 配好之后地址不可编辑，要改就点「断开并重新配置」回到第一步。这样「保存并
/// 测试连接」「重新配对」「清除配置」三个按钮收敛成一个：稳态下只有一件事
/// 可做，就是推倒重来（docs/design/device-auth.md §5.1）。
@MainActor
final class SettingsWindowController: NSWindowController {
    /// 保存非敏感设置（地址、名称、ffmpeg、并发、自启）。
    var onSave: ((WorkerSettingsDraft) throws -> Void)?
    /// 配对成功，把令牌交给调用方落钥匙串并重启 Worker。
    var onPaired: ((String) throws -> Void)?
    /// 清除本机配置与令牌。
    var onClear: (() throws -> Void)?

    /// 配对状态机。界面完全由它推导，不在各处零散地改控件——手工同步控件状态
    /// 迟早会出现「按钮说等待批准、文案说未连接」这种撕裂。
    private enum Stage {
        /// 还没授权，等待用户点「连接并配对」。
        case idle
        /// 正在验证地址。
        case connecting
        /// 已连上，配对码已生成，等网页批准。
        case pairing(DevicePairing.Grant)
        /// 已授权，稳态。
        case authorized
        case failed(String)
    }

    // 输入控件常驻，靠 isHidden 决定哪一行出现——重建视图树会丢掉正在输入的
    // 地址框焦点，也容易让重复挂上去的约束越积越多。
    private let nasURLField = NSTextField()
    private let workerIDField = NSTextField()
    private let ffmpegPathField = NSTextField()
    private let maxJobsField = NSTextField()
    private let autoConnectButton = NSButton(
        checkboxWithTitle: "", target: nil, action: nil
    )

    private let serverValue = SettingsStyle.rowValue(mono: true)
    private let statusDot = StatusDot()
    private let statusSpinner = NSProgressIndicator()
    private let statusValue = SettingsStyle.rowValue()
    private let identityValue = SettingsStyle.rowValue(mono: true)
    private let expiryValue = SettingsStyle.rowValue(mono: true)

    private let codeCaption = NSTextField(labelWithString: "在浏览器中核对这个配对码")
    private let codeLabel = NSTextField(labelWithString: "")
    private let codeLink = NSButton(title: "", target: nil, action: nil)

    private let primaryButton = NSButton(title: "连接并配对", target: nil, action: nil)
    private let discoverButton = NSButton(title: "在局域网中查找", target: nil, action: nil)
    private let resetButton = NSButton(title: "断开并重新配置", target: nil, action: nil)
    private let cancelButton = NSButton(title: "取消", target: nil, action: nil)
    private let barStatusLabel = SettingsStyle.caption("")
    private let advancedToggle = NSButton(title: "", target: nil, action: nil)

    // 行与分区。render() 只做一件事：决定这些的 isHidden。
    private var addressRow = NSView()
    private var serverRow = NSView()
    private var statusRow = NSView()
    private var codeRow = NSView()
    private var expiryRow = NSView()
    private var identityRow = NSView()
    private var credentialRow = NSView()
    private var connectSection: SectionView?
    private var authSection: SectionView?
    private var advancedSection: SectionView?

    /// AppMain 推来的实时连接状态。
    ///
    /// 「已授权」和「连着」是两回事：Mac 睡了、网断了、服务端重启了，授权都
    /// 还在。稳态下状态行要说的是后者——用户打开这扇窗，想知道的就是现在
    /// 到底通不通，而不是钥匙串里有没有那把钥匙。
    private var liveStatus: WorkerStatus?

    private var stage: Stage = .idle
    private var pollTask: Task<Void, Never>?
    private var countdownTask: Task<Void, Never>?
    /// 本机是否已经持有令牌。
    private var isAuthorized: Bool

    init(snapshot: WorkerSettingsSnapshot) {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: SettingsStyle.windowWidth, height: 460),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        // 惯例是「<App 名> 设置」：菜单栏 App 的窗口会混在窗口列表里，
        // 只写 App 名分不清这是设置窗还是 ffmpeg 下载窗
        window.title = "MovieClaw Transcoder 设置"
        window.isReleasedWhenClosed = false
        // 记住用户挪过的位置：每次打开都跳回屏幕正中是很烦的
        window.setFrameAutosaveName(settingsFrameAutosaveName)
        isAuthorized = snapshot.tokenConfigured
        super.init(window: window)

        nasURLField.stringValue = snapshot.nasURL
        nasURLField.placeholderString = "http://10.1.1.5:3000"
        workerIDField.stringValue = snapshot.workerID
        ffmpegPathField.stringValue = snapshot.ffmpegPath
        maxJobsField.stringValue = String(snapshot.maxJobs)
        autoConnectButton.state = snapshot.autoConnect ? .on : .off
        serverValue.stringValue = displayHost(snapshot.nasURL)
        identityValue.stringValue = snapshot.workerID

        window.contentView = makeContentView()
        stage = isAuthorized ? .authorized : .idle
        render()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    deinit {
        pollTask?.cancel()
        countdownTask?.cancel()
    }

    func showWindowAndFocus() {
        // 没有存过位置时才居中，存过就用上次的
        if window?.setFrameUsingName(settingsFrameAutosaveName) != true {
            window?.center()
        }
        resizeToFit()
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
        if !isAuthorized,
           nasURLField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            discover(auto: true)
        }
    }

    override func close() {
        pollTask?.cancel()
        pollTask = nil
        countdownTask?.cancel()
        countdownTask = nil
        super.close()
    }

    /// 连接状态变了就刷新状态行。只有稳态才用得上——其余状态里这一行说的是
    /// 这次配对进行到哪儿了，不该被后台的重连消息盖掉。
    func update(status: WorkerStatus?) {
        liveStatus = status
        // 窗口关掉之后控制器还被 AppMain 持有着，状态照样往这儿推。看不见的
        // 窗口不必重排，下次打开时 render() 会带上最新的。
        guard window?.isVisible == true, case .authorized = stage else { return }
        render()
    }

    /// Esc 关窗。菜单栏 App 没有主菜单，⌘W 走不通，键盘用户只剩这条路；
    /// 底部动作栏又不该为了一个「完成」按钮多占一格，所以直接接响应链。
    override func cancelOperation(_ sender: Any?) {
        close()
    }

    // MARK: - 布局

    private func makeContentView() -> NSView {
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = SettingsStyle.sectionSpacing
        root.translatesAutoresizingMaskIntoConstraints = false
        root.edgeInsets = NSEdgeInsets(
            top: 20,
            left: SettingsStyle.windowPadding,
            bottom: SettingsStyle.windowPadding,
            right: SettingsStyle.windowPadding
        )

        let connect = makeConnectSection()
        connectSection = connect
        root.addArrangedSubview(connect)
        SettingsStyle.stretch(connect, in: root)

        let auth = makeAuthSection()
        authSection = auth
        root.addArrangedSubview(auth)
        SettingsStyle.stretch(auth, in: root)

        let advanced = makeAdvancedSection()
        root.addArrangedSubview(advanced)
        SettingsStyle.stretch(advanced, in: root)

        let bar = makeActionBar()
        root.addArrangedSubview(bar)
        SettingsStyle.stretch(bar, in: root)

        // 根视图不直接当 contentView：那样窗口的四边约束会和内容的固有高度
        // 打架，`fittingSize` 量不准，窗口自适应高度就跟着失灵。用一层容器把
        // 底边约束降到低优先级，根视图的高度就完全由内容说了算。
        let container = NSView()
        container.addSubview(root)
        let bottom = root.bottomAnchor.constraint(equalTo: container.bottomAnchor)
        bottom.priority = .defaultLow
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            root.topAnchor.constraint(equalTo: container.topAnchor),
            bottom,
        ])
        contentStack = root
        return container
    }

    /// 「连接」分区：未授权时是一个地址输入行，授权后变成一行只读的服务器地址。
    private func makeConnectSection() -> SectionView {
        nasURLField.font = .monospacedSystemFont(ofSize: 13, weight: .regular)
        addressRow = SettingsStyle.row("movieclaw 地址", trailing: nasURLField)
        serverRow = SettingsStyle.row("服务器", trailing: serverValue)

        statusSpinner.style = .spinning
        statusSpinner.controlSize = .small
        statusSpinner.isDisplayedWhenStopped = false
        let statusContent = NSStackView(views: [
            SettingsStyle.flexibleSpacer(), statusDot, statusSpinner, statusValue,
        ])
        statusContent.orientation = .horizontal
        statusContent.alignment = .centerY
        statusContent.spacing = 7
        statusRow = SettingsStyle.row("状态", trailing: statusContent)

        return SectionView(title: "连接", rows: [addressRow, serverRow, statusRow])
    }

    /// 「授权」分区：等批准时是配对码 + 倒计时，授权后是身份与凭证去向。
    private func makeAuthSection() -> SectionView {
        codeCaption.font = .systemFont(ofSize: 11.5)
        codeCaption.textColor = .tertiaryLabelColor
        // 等宽 + 加大字号 + 拉开字距（demo：31px / .15em）：配对码要在屏幕和
        // 网页之间用眼睛核对，0/O、1/l 分不清会直接让人对不上
        codeLabel.font = .monospacedSystemFont(ofSize: 31, weight: .semibold)
        // 链接样式的按钮：demo 里这行是蓝色的 URL，看起来就该能点。浏览器被
        // 误关了也不用取消重来。
        codeLink.target = self
        codeLink.action = #selector(openVerificationPage)
        codeLink.isBordered = false
        codeLink.font = .systemFont(ofSize: 12)
        codeLink.contentTintColor = .linkColor
        codeRow = SettingsStyle.stackedRow(views: [codeCaption, codeLabel, codeLink])

        expiryRow = SettingsStyle.row("有效期", trailing: expiryValue)
        identityRow = SettingsStyle.row("身份", trailing: identityValue)
        let credentialValue = SettingsStyle.rowValue("已存入钥匙串 · 不再显示")
        credentialRow = SettingsStyle.row("凭证", trailing: credentialValue)

        return SectionView(
            title: "授权",
            rows: [codeRow, expiryRow, identityRow, credentialRow]
        )
    }

    /// 「高级设置」：默认折叠，四项都有可用默认值。
    private func makeAdvancedSection() -> NSView {
        let section = SectionView(
            title: nil,
            rows: [
                SettingsStyle.row("Worker 名称", trailing: workerIDField),
                SettingsStyle.row("ffmpeg 路径", trailing: ffmpegPathField),
                SettingsStyle.row("最大并发", trailing: maxJobsField),
                SettingsStyle.row("开机自动连接", trailing: autoConnectRow()),
            ]
        )
        section.note = "这四项都有可用的默认值，通常不需要改动。"
        section.isHidden = true
        advancedSection = section

        advancedToggle.target = self
        advancedToggle.action = #selector(toggleAdvanced)
        advancedToggle.isBordered = false
        advancedToggle.font = .systemFont(ofSize: 12)
        advancedToggle.contentTintColor = .secondaryLabelColor
        advancedToggle.title = "▶ 高级设置"

        let column = NSStackView(views: [advancedToggle, section])
        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = SettingsStyle.headingSpacing
        column.translatesAutoresizingMaskIntoConstraints = false
        section.widthAnchor.constraint(equalTo: column.widthAnchor).isActive = true
        return column
    }

    /// 复选框在「左标题右取值」的行里只需要那个小方块，标题由行自己出。
    private func autoConnectRow() -> NSView {
        // 行的无障碍标签挂在容器上，VoiceOver 落到复选框时读不到，得单独补
        autoConnectButton.setAccessibilityLabel("开机自动连接")
        let holder = NSStackView(views: [SettingsStyle.flexibleSpacer(), autoConnectButton])
        holder.orientation = .horizontal
        holder.alignment = .centerY
        holder.spacing = 0
        return holder
    }

    /// 动作栏：左端一句当前状态，右端按钮（demo：`.mac-actions`）。
    private func makeActionBar() -> NSView {
        primaryButton.target = self
        primaryButton.action = #selector(primaryAction)
        primaryButton.bezelStyle = .rounded
        // 回车即主按钮，系统会自动把它画成强调色
        primaryButton.keyEquivalent = "\r"

        discoverButton.target = self
        discoverButton.action = #selector(discoverAction)
        discoverButton.bezelStyle = .rounded

        resetButton.target = self
        resetButton.action = #selector(resetAction)
        resetButton.bezelStyle = .rounded

        cancelButton.target = self
        cancelButton.action = #selector(cancelPairing)
        cancelButton.bezelStyle = .rounded

        let bar = NSStackView(views: [
            barStatusLabel,
            SettingsStyle.flexibleSpacer(),
            resetButton,
            discoverButton,
            cancelButton,
            primaryButton,
        ])
        bar.orientation = .horizontal
        bar.alignment = .centerY
        bar.spacing = 9
        return bar
    }

    /// 根 stack 的引用，`resizeToFit` 要拿它量高度。
    private var contentStack: NSView?

    /// 让窗口高度贴合内容。每个状态显示的行数都不一样，固定窗高要么底部空
    /// 一大块，要么把动作栏挤出可视区。
    private func resizeToFit() {
        guard let window, let contentStack else { return }
        contentStack.layoutSubtreeIfNeeded()
        let height = contentStack.fittingSize.height
        guard height > 100 else { return }
        let target = window.frameRect(
            forContentRect: NSRect(x: 0, y: 0, width: SettingsStyle.windowWidth, height: height)
        )
        var frame = window.frame
        guard abs(frame.height - target.height) > 0.5 else { return }
        // 只改高度并保持顶边不动：窗口原点在左下角，直接改 size 会让窗口
        // 「往上长」，标题栏跟着跑，看起来像窗口自己在跳
        frame.origin.y += frame.height - target.height
        frame.size = target.size
        window.setFrame(frame, display: true, animate: false)
    }

    // MARK: - 渲染

    /// 每个状态显示哪几行、动作栏放什么，全在这里一次说清。
    private func render() {
        // 默认全收起来，各状态只打开自己要的——反过来写（默认全开、各状态
        // 关掉不要的）漏一行就是一处撕裂，而且不会报错。
        addressRow.isHidden = true
        serverRow.isHidden = true
        statusRow.isHidden = true
        codeRow.isHidden = true
        expiryRow.isHidden = true
        identityRow.isHidden = true
        credentialRow.isHidden = true
        resetButton.isHidden = true
        discoverButton.isHidden = true
        cancelButton.isHidden = true
        primaryButton.isHidden = false
        setBusy(false)
        // 连接过程中不让再去广播查找：结论马上就到了，这时候换地址只会打架
        if case .connecting = stage {
            discoverButton.isEnabled = false
        } else {
            discoverButton.isEnabled = true
        }

        let addressNote = "请填写局域网地址和端口。转码要来回传输大量视频分片，"
            + "走公网或反向代理会明显变慢，也更容易中断。"

        switch stage {
        case .idle:
            addressRow.isHidden = false
            discoverButton.isHidden = false
            connectSection?.note = addressNote
            authSection?.note = ""
            barStatusLabel.stringValue = "尚未连接"
            primaryButton.title = "连接并配对"
            primaryButton.isEnabled = true

        case .connecting:
            addressRow.isHidden = false
            statusRow.isHidden = false
            discoverButton.isHidden = false
            setBusy(true)
            statusValue.stringValue = "正在连接…"
            connectSection?.note = addressNote
            authSection?.note = ""
            barStatusLabel.stringValue = ""
            primaryButton.title = "连接中…"
            primaryButton.isEnabled = false

        case let .pairing(grant):
            // 等批准时窗口里只留配对码。地址、状态、高级设置这时候都不是
            // 用户要看的东西，留着只会分散他核对那串字符的注意力。
            codeRow.isHidden = false
            expiryRow.isHidden = false
            cancelButton.isHidden = false
            primaryButton.isHidden = true
            codeLabel.attributedStringValue = Self.trackedCode(grant.userCode)
            codeLink.title = displayHost(grant.verificationURI) + "/settings/devices"
            connectSection?.note = ""
            authSection?.note = "浏览器已打开设备页。用你平时登录 movieclaw 的账号确认即可"
                + "——配对码本身不是密钥，即使被别人看到也拿不到任何权限。"
            barStatusLabel.stringValue = "等待批准…"

        case .authorized:
            serverRow.isHidden = false
            statusRow.isHidden = false
            identityRow.isHidden = false
            credentialRow.isHidden = false
            resetButton.isHidden = false
            primaryButton.isHidden = true
            statusDot.color = Self.dotColor(for: liveStatus?.state)
            statusValue.stringValue = liveStatus?.state.displayName ?? "已授权，未启动"
            connectSection?.note = ""
            authSection?.note = "配置完成，之后开机自动连接。"
                + "要停用这台机器，在网页的设备列表里吊销即可。"
            // 动作栏左端不再重复一句「运行中」：上面的状态行说的是同一件事，
            // 而且它跟着实时连接状态走，说得更准
            barStatusLabel.stringValue = ""

        case let .failed(message):
            addressRow.isHidden = false
            statusRow.isHidden = false
            discoverButton.isHidden = false
            statusDot.color = .systemRed
            statusValue.stringValue = message
            connectSection?.note = "确认地址填写正确且 movieclaw 正在运行，然后重试。"
            authSection?.note = ""
            barStatusLabel.stringValue = ""
            primaryButton.title = "重试"
            primaryButton.isEnabled = true
        }

        // 等批准时高级设置也一并收起：那一刻窗口里只该有配对码。顺手把折叠
        // 箭头复位，否则回到 idle 时会出现「箭头朝下、内容却没有」。
        var pairingNow = false
        if case .pairing = stage { pairingNow = true }
        advancedToggle.isHidden = pairingNow
        if pairingNow {
            advancedSection?.isHidden = true
            advancedToggle.title = "▶ 高级设置"
        }
        barStatusLabel.isHidden = barStatusLabel.stringValue.isEmpty
        connectSection?.refresh()
        authSection?.refresh()
        resizeToFit()
    }

    /// 连接状态到圆点颜色。绿=在干活或随时能干活，橙=还在路上，红=坏了。
    private static func dotColor(for state: WorkerConnectionState?) -> NSColor {
        switch state {
        case .ready, .busy, .draining, .paused:
            return .systemGreen
        case .starting, .connecting, .reconnecting:
            return .systemOrange
        case .error, .stopped:
            return .systemRed
        case .unconfigured, .none:
            return .tertiaryLabelColor
        }
    }

    private func transition(to next: Stage) {
        countdownTask?.cancel()
        countdownTask = nil
        stage = next
        render()
        if case let .pairing(grant) = next {
            startCountdown(expiresIn: grant.expiresIn)
        }
    }

    /// 开/停状态转圈。`isDisplayedWhenStopped` 只保证不画出来，是否还占位置
    /// 属于实现细节；显式改 `isHidden`，stack view 才一定会收掉那段空间。
    private func setBusy(_ busy: Bool) {
        statusSpinner.isHidden = !busy
        statusDot.isHidden = busy
        if busy {
            statusSpinner.startAnimation(nil)
        } else {
            statusSpinner.stopAnimation(nil)
        }
    }

    /// 配对码有效期倒计时。知道还剩多久，人才不会在浏览器那边慢慢找。
    private func startCountdown(expiresIn: Int) {
        let deadline = Date().addingTimeInterval(TimeInterval(expiresIn))
        countdownTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let left = Int(deadline.timeIntervalSinceNow)
                guard left > 0 else {
                    self.expiryValue.stringValue = "已过期"
                    return
                }
                self.expiryValue.stringValue = String(
                    format: "%d 分 %02d 秒", left / 60, left % 60
                )
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
        }
    }

    // MARK: - 文本工具

    /// 去掉 scheme，只留主机和端口——地址栏里用户认得的就是这一段。
    private func displayHost(_ raw: String) -> String {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        for prefix in ["https://", "http://"] where text.hasPrefix(prefix) {
            text.removeFirst(prefix.count)
        }
        while text.hasSuffix("/") {
            text.removeLast()
        }
        // verification_uri 带路径时只取主机段，拼出来才是干净的设备页地址
        if let slash = text.firstIndex(of: "/") {
            text = String(text[text.startIndex..<slash])
        }
        return text
    }

    /// 配对码的字距（demo：`letter-spacing:.15em`）。
    ///
    /// AppKit 的 `stringValue` 没有字距，只能走 attributed string —— 代价是
    /// 字体和颜色也得在这里一并写死，否则会退回系统默认。逐字符插空格看起来
    /// 更省事，但 31pt 下一个空格差不多 18pt，远宽于 .15em 的约 4.6pt，
    /// 反而会把一串码拆得难以整体辨认。
    ///
    /// 字距只加到倒数第二个字符：加在最后一个后面会多出一段尾随空白，让
    /// 居中的码看上去整体偏左。
    private static func trackedCode(_ code: String) -> NSAttributedString {
        let attributed = NSMutableAttributedString(
            string: code,
            attributes: [
                .font: NSFont.monospacedSystemFont(ofSize: 31, weight: .semibold),
                .foregroundColor: NSColor.labelColor,
            ]
        )
        let length = (code as NSString).length
        if length > 1 {
            attributed.addAttribute(
                .kern, value: 4.6, range: NSRange(location: 0, length: length - 1)
            )
        }
        return attributed
    }

    // MARK: - 动作

    @objc private func toggleAdvanced() {
        guard let advancedSection else { return }
        advancedSection.isHidden.toggle()
        advancedToggle.title = advancedSection.isHidden ? "▶ 高级设置" : "▼ 高级设置"
        resizeToFit()
    }

    @objc private func primaryAction() {
        switch stage {
        case .idle, .failed:
            Task { await connect() }
        case .connecting, .pairing, .authorized:
            break
        }
    }

    @objc private func cancelPairing() {
        pollTask?.cancel()
        pollTask = nil
        transition(to: .idle)
    }

    /// 稳态下唯一可做的事：推倒重来。
    ///
    /// 把「保存并测试连接」「重新配对」「清除配置」收敛成这一个。改地址、
    /// 换机器、被吊销，最终都要重新走一遍配对，分成三个按钮只是让用户在
    /// 三个都不确定的选项里挑。
    @objc private func resetAction() {
        Task { await confirmAndClear() }
    }

    @objc private func openVerificationPage() {
        guard case let .pairing(grant) = stage,
              let url = URL(string: grant.verificationURI) else { return }
        NSWorkspace.shared.open(url)
    }

    @objc private func discoverAction() {
        discover(auto: false)
    }

    /// 在局域网里找 movieclaw，把结果填进地址框（docs/design/device-auth.md §6.5）。
    ///
    /// **只填不存**：发现到的地址交给用户过目，由他点主按钮拍板。服务端优先
    /// 返回的是用户为播放器配的「对外访问地址」，那可能是反向代理域名——
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
        // 回到待连接态：地址换了，之前那次连接的结论就作废了
        transition(to: .idle)
        connectSection?.note = "已填入局域网中找到的地址（\(chosen.displayName)）。"
            + "走反向代理的地址传分片会明显变慢，内网直连地址更合适。"
        resizeToFit()
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

    /// 保存设置 → 验证地址可达 → 发起接入请求并等批准，一次做完。
    ///
    /// 验证仍然先做且失败即停：地址填错是自部署产品最容易劝退用户的一步，
    /// 得在发起请求之前给出确定结论。只是这个结论不再需要用户点一下才继续。
    private func connect() async {
        guard let maxJobs = Int(
            maxJobsField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        ) else {
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
        serverValue.stringValue = displayHost(draft.nasURL)
        identityValue.stringValue = draft.workerID

        transition(to: .connecting)
        let pairing = DevicePairing(nasURL: url)
        do {
            _ = try await pairing.verifyConnection()
        } catch {
            transition(to: .failed("连不上：\(error.localizedDescription)"))
            return
        }
        startPairing(pairing: pairing, fallbackURL: url)
    }

    /// 发起接入请求并轮询兑换，直到拿到令牌或得到确定的失败结论。
    private func startPairing(pairing: DevicePairing, fallbackURL: URL) {
        let name = workerIDField.stringValue
        pollTask?.cancel()
        pollTask = Task { @MainActor in
            do {
                let grant = try await pairing.authorize(clientName: name)
                transition(to: .pairing(grant))
                NSWorkspace.shared.open(URL(string: grant.verificationURI) ?? fallbackURL)
                try await awaitApproval(pairing: pairing, grant: grant)
            } catch is CancellationError {
                // 用户点了取消，界面已经切走
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
                isAuthorized = true
                transition(to: .authorized)
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

    private func confirmAndClear() async {
        let alert = NSAlert()
        alert.messageText = "断开并重新配置？"
        alert.informativeText = "这会删除本机保存的地址与授权，需要重新配对才能继续转码。"
            + "服务端的授权记录不会一起删除——要彻底停用，请到网页「设置 → 设备」里吊销。"
        alert.alertStyle = .warning
        // 破坏性动作标红，并把「取消」设为默认回车项——HIG：确认框里
        // 回车应当落在安全的那一侧，别让手快的人一路回车删掉配置
        let destructive = alert.addButton(withTitle: "断开")
        destructive.hasDestructiveAction = true
        destructive.keyEquivalent = ""
        alert.addButton(withTitle: "取消").keyEquivalent = "\r"
        guard await runSheet(alert) == .alertFirstButtonReturn else { return }
        do {
            pollTask?.cancel()
            pollTask = nil
            try onClear?()
            isAuthorized = false
            nasURLField.stringValue = ""
            transition(to: .idle)
        } catch {
            showError(error.localizedDescription)
        }
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
