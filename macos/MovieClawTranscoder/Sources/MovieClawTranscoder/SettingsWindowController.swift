import AppKit

/// 设置窗位置的持久化键。文件级常量：init 里要在 super.init 之前用到它。
private let settingsFrameAutosaveName = "MovieClawTranscoderSettings"

/// 配置窗口保持为 AppKit 原生窗口，兼容 macOS 12，不引入 SwiftUI 的最低系统版本约束。
///
/// **首次配置只需要一次点击。** 填好地址点「连接并配对」，App 会连着做完
/// 三件事：保存设置、验证地址真的通、发起接入请求并显示配对码。
///
/// 这里原本是两步（先「验证连接」，成功后再点「请求接入」）。拆开的初衷是让
/// 地址填错当场可见，但代价是把一个中间结论摆成了必须点掉的关卡——「已连接」
/// 从来不是用户想要的终点，他要的是配对完成。地址错了照样当场可见：验证失败
/// 就停在失败态，根本走不到发起请求那一步，中间结论用一行进度文案交代即可。
///
/// 已经授权过之后按钮变成「保存并测试连接」——改端口不该动令牌，重新配对是
/// 另一个明确的入口（docs/design/device-auth.md §5.1）。
///
/// 布局按 macOS 系统设置的语言组织：分组小标题 + 圆角卡片，卡片内每行左标签
/// 右控件，卡片下一行灰色脚注。**主界面只有一个必填项——movieclaw 地址**，
/// 其余四项都有可用默认值，收进折叠的「高级设置」。
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
        /// 正在验证地址。`pairing` 表示验证通过后会接着发起接入请求。
        case connecting(pairing: Bool)
        /// 已连上，配对码已生成，等网页批准。
        case pairing(DevicePairing.Grant)
        /// 已授权。`detail` 说明最近一次操作的结论。
        case authorized(detail: String)
        case failed(String)
    }

    private let nasURLField = NSTextField()
    private let workerIDField = NSTextField()
    private let ffmpegPathField = NSTextField()
    private let maxJobsField = NSTextField()
    private let autoConnectButton = NSButton(
        checkboxWithTitle: "开机自动连接", target: nil, action: nil
    )

    private let statusDot = StatusDot()
    private let statusSpinner = NSProgressIndicator()
    private let statusLabel = NSTextField(labelWithString: "")
    private let hintLabel = NSTextField(wrappingLabelWithString: "")
    private let codeLabel = NSTextField(labelWithString: "")
    private let copyCodeButton = NSButton(title: "拷贝", target: nil, action: nil)
    private let openBrowserButton = NSButton(title: "打开网页", target: nil, action: nil)
    private var codePanel = CardView()
    private var statusCard = CardView()

    private let primaryButton = NSButton(title: "连接并配对", target: nil, action: nil)
    private let repairButton = NSButton(title: "重新配对…", target: nil, action: nil)
    private let discoverButton = NSButton(title: "在局域网中查找", target: nil, action: nil)
    private let advancedToggle = NSButton(title: "高级设置", target: nil, action: nil)
    private var advancedCard = CardView()

    private var stage: Stage = .idle
    private var pollTask: Task<Void, Never>?
    /// 本机是否已经持有令牌。决定主按钮是「连接并配对」还是「保存并测试连接」。
    private var isAuthorized: Bool

    init(snapshot: WorkerSettingsSnapshot) {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: SettingsStyle.windowWidth, height: 520),
            styleMask: [.titled, .closable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        // 惯例是「<App 名> 设置」：菜单栏 App 的窗口会混在窗口列表里，
        // 只写 App 名分不清这是设置窗还是别的什么
        window.title = "MovieClaw Transcoder 设置"
        // 标题栏透明 + 隐藏标题文字：窗口内已经有一块带图标和名字的抬头，
        // 再顶一行系统标题就重复了。window.title 保留，Mission Control 和
        // 窗口菜单还要靠它认这扇窗。
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
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

        window.contentView = makeContentView()
        stage = isAuthorized ? .authorized(detail: "开机后会自动连接。") : .idle
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
        root.spacing = 10
        root.translatesAutoresizingMaskIntoConstraints = false
        root.edgeInsets = NSEdgeInsets(
            // 顶部留白比其余三边大：标题栏透明后内容会顶到窗口最上沿，
            // 得自己空出原本标题栏占的那段
            top: 34,
            left: SettingsStyle.windowPadding,
            bottom: SettingsStyle.windowPadding,
            right: SettingsStyle.windowPadding
        )

        let header = makeHeader()
        root.addArrangedSubview(header)
        SettingsStyle.stretch(header, in: root)
        root.setCustomSpacing(20, after: header)

        statusCard = makeStatusCard()
        root.addArrangedSubview(statusCard)
        SettingsStyle.stretch(statusCard, in: root)
        root.setCustomSpacing(20, after: statusCard)

        let serverLabel = SettingsStyle.groupLabel("服务器")
        root.addArrangedSubview(serverLabel)
        root.setCustomSpacing(6, after: serverLabel)

        let serverCard = makeServerCard()
        root.addArrangedSubview(serverCard)
        SettingsStyle.stretch(serverCard, in: root)

        let serverNote = SettingsStyle.footnote(
            "请填局域网地址和端口：转码要来回传输大量视频分片，走公网或反向代理会"
                + "明显变慢，也更容易中断。"
        )
        root.addArrangedSubview(serverNote)
        SettingsStyle.stretch(serverNote, in: root)
        root.setCustomSpacing(18, after: serverNote)

        advancedToggle.bezelStyle = .inline
        advancedToggle.isBordered = false
        advancedToggle.target = self
        advancedToggle.action = #selector(toggleAdvanced)
        advancedToggle.contentTintColor = .secondaryLabelColor
        root.addArrangedSubview(advancedToggle)

        advancedCard = makeAdvancedCard()
        root.addArrangedSubview(advancedCard)
        SettingsStyle.stretch(advancedCard, in: root)
        root.setCustomSpacing(18, after: advancedCard)

        let buttons = makeButtonBar()
        root.addArrangedSubview(buttons)
        SettingsStyle.stretch(buttons, in: root)

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

    /// 抬头：App 图标 + 名字 + 一句话说明这个 App 到底在干什么。
    ///
    /// 菜单栏 App 没有 Dock 图标，设置窗往往是用户唯一见到它「长什么样」的
    /// 地方；一行说明也省得人对着一堆输入框猜这是什么。
    private func makeHeader() -> NSView {
        let icon = NSImageView()
        icon.image = NSApp.applicationIconImage
        icon.imageScaling = .scaleProportionallyUpOrDown
        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.widthAnchor.constraint(equalToConstant: 44).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 44).isActive = true

        let title = NSTextField(labelWithString: "MovieClaw Transcoder")
        title.font = .systemFont(ofSize: 15, weight: .semibold)
        let subtitle = NSTextField(labelWithString: "把这台 Mac 的硬件编码器借给 movieclaw")
        subtitle.font = .systemFont(ofSize: 11)
        subtitle.textColor = .secondaryLabelColor

        let text = NSStackView(views: [title, subtitle])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 2

        let header = NSStackView(views: [icon, text])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = 12
        return header
    }

    /// 状态卡：一行结论 + 一行说明，配对进行中时再展开配对码面板。
    private func makeStatusCard() -> CardView {
        statusSpinner.style = .spinning
        statusSpinner.controlSize = .small
        // 停下来就藏起来，省得留一个静止的转圈占位
        statusSpinner.isDisplayedWhenStopped = false
        statusSpinner.translatesAutoresizingMaskIntoConstraints = false

        statusLabel.font = .systemFont(ofSize: 13, weight: .medium)

        let statusRow = NSStackView(views: [statusDot, statusSpinner, statusLabel])
        statusRow.orientation = .horizontal
        statusRow.alignment = .centerY
        statusRow.spacing = 8

        hintLabel.font = .systemFont(ofSize: 11)
        hintLabel.textColor = .secondaryLabelColor
        hintLabel.preferredMaxLayoutWidth = SettingsStyle.cardContentWidth

        codePanel = makeCodePanel()

        return SettingsStyle.card(rows: [statusRow, hintLabel, codePanel], spacing: 10)
    }

    /// 配对码面板。配对码是这一步唯一要人动脑的东西，给它独立的底色和字号，
    /// 用户扫一眼就知道要核对哪串字符。
    private func makeCodePanel() -> CardView {
        // 等宽 + 加大字号：配对码要在屏幕和网页之间用眼睛核对，
        // 0/O、1/l 分不清会直接让人对不上
        codeLabel.font = .monospacedSystemFont(ofSize: 30, weight: .semibold)
        codeLabel.textColor = .controlAccentColor

        copyCodeButton.target = self
        copyCodeButton.action = #selector(copyCode)
        copyCodeButton.bezelStyle = .rounded
        copyCodeButton.controlSize = .small

        openBrowserButton.target = self
        openBrowserButton.action = #selector(openVerificationPage)
        openBrowserButton.bezelStyle = .rounded
        openBrowserButton.controlSize = .small

        let actions = NSStackView(views: [copyCodeButton, openBrowserButton])
        actions.orientation = .horizontal
        actions.spacing = 8

        let row = NSStackView(views: [codeLabel, SettingsStyle.flexibleSpacer(), actions])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 12

        let panel = SettingsStyle.card(rows: [row], spacing: 0)
        panel.isHighlighted = true
        panel.isHidden = true
        return panel
    }

    private func makeServerCard() -> CardView {
        // 「在局域网中查找」属于「填地址」这件事，放在地址输入框正下方；
        // 底部动作栏只留窗口级动作（HIG：主按钮在右下角，右侧不堆多个动作）
        discoverButton.target = self
        discoverButton.action = #selector(discoverAction)
        discoverButton.bezelStyle = .rounded
        discoverButton.controlSize = .small

        let discoverRow = NSStackView(views: [
            makeSpacer(width: SettingsStyle.labelColumnWidth),
            discoverButton,
            SettingsStyle.flexibleSpacer(),
        ])
        discoverRow.orientation = .horizontal
        discoverRow.spacing = 10
        discoverRow.alignment = .centerY

        return SettingsStyle.card(
            rows: [SettingsStyle.formRow("地址", control: nasURLField), discoverRow],
            spacing: 10
        )
    }

    private func makeAdvancedCard() -> CardView {
        let card = SettingsStyle.card(
            rows: [
                SettingsStyle.formRow("名称", control: workerIDField),
                SettingsStyle.formRow("ffmpeg", control: ffmpegPathField),
                SettingsStyle.formRow("并发", control: maxJobsField),
                autoConnectButton,
            ],
            spacing: 10
        )
        // 直接折叠这张卡片本身，不再套一层容器：多一层就多一组要对齐的
        // 宽度约束，而它什么也没多做
        card.isHidden = true
        return card
    }

    private func makeButtonBar() -> NSView {
        primaryButton.target = self
        primaryButton.action = #selector(primaryAction)
        primaryButton.bezelStyle = .rounded
        // 回车即主按钮，系统会自动把它画成强调色
        primaryButton.keyEquivalent = "\r"

        repairButton.target = self
        repairButton.action = #selector(repairAction)
        repairButton.bezelStyle = .rounded

        let clearButton = NSButton(title: "清除配置", target: self, action: #selector(clearSettings))
        clearButton.bezelStyle = .rounded
        let closeButton = NSButton(title: "完成", target: self, action: #selector(finish))
        closeButton.bezelStyle = .rounded
        // Esc 关窗：菜单栏 App 没有主菜单，⌘W 走不通，键盘用户只剩这条路
        closeButton.keyEquivalent = "\u{1b}"

        let bar = NSStackView(views: [
            clearButton, SettingsStyle.flexibleSpacer(), repairButton, closeButton, primaryButton,
        ])
        bar.orientation = .horizontal
        bar.spacing = 8
        bar.alignment = .centerY
        return bar
    }

    /// 占位视图：让分组内不带标签的行与上面带标签的行左对齐。
    private func makeSpacer(width: CGFloat) -> NSView {
        let spacer = NSView()
        spacer.translatesAutoresizingMaskIntoConstraints = false
        spacer.widthAnchor.constraint(equalToConstant: width).isActive = true
        return spacer
    }

    /// 根 stack 的引用，`resizeToFit` 要拿它量高度。
    private var contentStack: NSView?

    /// 让窗口高度贴合内容。展开/收起「高级设置」后内容高度会变，固定窗高要么
    /// 底部空一大块，要么把按钮栏挤出可视区。
    private func resizeToFit() {
        guard let window, let contentStack else { return }
        contentStack.layoutSubtreeIfNeeded()
        let height = contentStack.fittingSize.height
        guard height > 100 else { return }
        let target = window.frameRect(
            forContentRect: NSRect(x: 0, y: 0, width: SettingsStyle.windowWidth, height: height)
        )
        var frame = window.frame
        // 只改高度并保持顶边不动：窗口原点在左下角，直接改 size 会让窗口
        // 「往上长」，标题栏跟着跑，看起来像窗口自己在跳
        frame.origin.y += frame.height - target.height
        frame.size = target.size
        window.setFrame(frame, display: true, animate: false)
    }

    // MARK: - 渲染

    private func render() {
        switch stage {
        case .idle:
            statusDot.color = .tertiaryLabelColor
            setBusy(false)
            statusLabel.stringValue = "尚未连接"
            hintLabel.stringValue = "填好上面的地址，点「连接并配对」。"
                + "接下来只需要在浏览器里确认一次，不用在这里输入任何密钥。"
            codePanel.isHidden = true
            primaryButton.title = "连接并配对"
            primaryButton.isEnabled = true
            repairButton.isHidden = true

        case let .connecting(pairing):
            statusDot.color = .systemGray
            setBusy(true)
            statusLabel.stringValue = pairing ? "正在连接并申请接入…" : "正在测试连接…"
            hintLabel.stringValue = ""
            codePanel.isHidden = true
            primaryButton.title = pairing ? "连接并配对" : "保存并测试连接"
            primaryButton.isEnabled = false
            repairButton.isHidden = true

        case let .pairing(grant):
            statusDot.color = .systemOrange
            setBusy(true)
            statusLabel.stringValue = "等待网页批准"
            hintLabel.stringValue = "已在浏览器打开 \(grant.verificationURI)。"
                + "核对下面这串配对码后点「批准接入」即可。"
                + "配对码不是密钥，被别人看到也拿不到权限。"
            codeLabel.stringValue = grant.userCode
            codePanel.isHidden = false
            primaryButton.title = "取消"
            primaryButton.isEnabled = true
            repairButton.isHidden = true

        case let .authorized(detail):
            statusDot.color = .systemGreen
            setBusy(false)
            statusLabel.stringValue = "已授权"
            hintLabel.stringValue = detail
                + "要停用这台机器，在网页「设置 → 设备」里吊销即可。"
            codePanel.isHidden = true
            primaryButton.title = "保存并测试连接"
            primaryButton.isEnabled = true
            repairButton.isHidden = false

        case let .failed(message):
            statusDot.color = .systemRed
            setBusy(false)
            statusLabel.stringValue = message
            hintLabel.stringValue = "确认地址填写正确且 movieclaw 正在运行，然后重试。"
            codePanel.isHidden = true
            primaryButton.title = "重试"
            primaryButton.isEnabled = true
            repairButton.isHidden = true
        }
        finishRender()
    }

    /// 每次渲染的统一收尾。
    ///
    /// 三件事都属于「由上面的状态推导出来、每一路都一样」的收尾动作，
    /// 写在各个 case 里只会是五份一模一样的代码，漏掉一份就是一个撕裂。
    private func finishRender() {
        // 转圈和圆点是同一件事的两种画法，同时出现只是噪音
        let busy = statusSpinner.isHidden == false
        statusDot.isHidden = busy
        // 空的折行标签仍然占一行行高，会在卡片里留一道空白
        hintLabel.isHidden = hintLabel.stringValue.isEmpty
        resizeToFit()
    }

    /// 开/停状态转圈。`isDisplayedWhenStopped` 只保证不画出来，是否还占位置
    /// 属于实现细节；显式改 `isHidden`，stack view 才一定会收掉那段空间。
    private func setBusy(_ busy: Bool) {
        statusSpinner.isHidden = !busy
        if busy {
            statusSpinner.startAnimation(nil)
        } else {
            statusSpinner.stopAnimation(nil)
        }
    }

    private func transition(to next: Stage) {
        stage = next
        render()
    }

    // MARK: - 动作

    @objc private func toggleAdvanced() {
        advancedCard.isHidden.toggle()
        advancedToggle.title = advancedCard.isHidden ? "高级设置" : "隐藏高级设置"
        resizeToFit()
    }

    @objc private func primaryAction() {
        switch stage {
        case .idle, .failed:
            // 还没授权就一次做完（连接 + 配对）；已授权的失败重试只是重连，
            // 不该顺手把现有令牌换掉
            Task { await connect(alsoPair: !isAuthorized) }
        case .authorized:
            Task { await connect(alsoPair: false) }
        case .pairing:
            pollTask?.cancel()
            pollTask = nil
            transition(to: isAuthorized ? .authorized(detail: "已取消这次配对。") : .idle)
        case .connecting:
            break
        }
    }

    @objc private func repairAction() {
        Task {
            let alert = NSAlert()
            alert.messageText = "重新配对这台 Mac？"
            alert.informativeText = "会向服务端申请一份新授权，替换本机现有的令牌。"
                + "旧授权仍留在网页「设置 → 设备」里，可以在那里吊销。"
            alert.alertStyle = .informational
            alert.addButton(withTitle: "重新配对")
            alert.addButton(withTitle: "取消")
            guard await runSheet(alert) == .alertFirstButtonReturn else { return }
            await connect(alsoPair: true)
        }
    }

    @objc private func copyCode() {
        guard case let .pairing(grant) = stage else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(grant.userCode, forType: .string)
        copyCodeButton.title = "已拷贝"
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            self?.copyCodeButton.title = "拷贝"
        }
    }

    /// 浏览器可能被用户不小心关掉了，或者压根没弹出来——留一个手动入口，
    /// 免得只能取消重来。
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
        transition(to: isAuthorized ? .authorized(detail: "地址已更新，点「保存并测试连接」确认。") : .idle)
        hintLabel.stringValue = "已填入局域网中找到的地址（\(chosen.displayName)）。"
            + "走反向代理的地址传分片会明显变慢，内网直连地址更合适。"
        // 这行文案是在 render() 之后盖上去的，长度变了得再量一次窗高
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

    /// 保存设置 → 验证地址可达 →（可选）发起接入请求并等批准，一次做完。
    ///
    /// 验证仍然先做且失败即停：地址填错是自部署产品最容易劝退用户的一步，
    /// 得在发起请求之前给出确定结论。只是这个结论不再需要用户点一下才继续。
    private func connect(alsoPair: Bool) async {
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

        transition(to: .connecting(pairing: alsoPair))
        let pairing = DevicePairing(nasURL: url)
        let service: String
        do {
            service = try await pairing.verifyConnection()
        } catch {
            transition(to: .failed("连不上：\(error.localizedDescription)"))
            return
        }
        guard alsoPair else {
            transition(to: .authorized(detail: "连接正常（\(service)）。"))
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
                transition(to: .authorized(detail: "配置结束，之后开机自动连接。"))
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
        destructive.hasDestructiveAction = true
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
