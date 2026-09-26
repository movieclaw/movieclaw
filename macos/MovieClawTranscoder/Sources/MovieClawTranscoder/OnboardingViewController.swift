import AppKit

/// 首次配置的三步引导：找到服务器 → 在网页上批准 → 完成。
///
/// 配对逻辑与原设置窗一致（docs/design/device-auth.md §5）：
/// - 配对是**一次点击**：点「连接并配对」，连着做完保存设置、验证地址、发起接入请求
///   三件事。地址错了照样当场可见——验证失败就停在第一步、原因写在按钮上方；
/// - 等批准时整窗只剩配对码：那一刻用户唯一要做的就是核对那串字符；
/// - 令牌由配对流程拿回来直接进钥匙串，任何时候都不显示在屏幕上。
///
/// 和原来的差别在「找服务器」这一步：局域网里搜到的 movieclaw 直接列成可以点的
/// 卡片（名称 + 地址），手填地址退到后面作为备选。第一次配置的人最卡的就是
/// 「地址该填什么」，能点就不让他敲。
@MainActor
final class OnboardingViewController: NSViewController, NSTextFieldDelegate {
    private enum Step: Equatable {
        case find
        case connecting
        case pairing(DevicePairing.Grant)
        case done
    }

    var onSave: ((WorkerSettingsDraft) throws -> Void)?
    var onPaired: ((String) throws -> Void)?
    var onFinish: (() -> Void)?
    var onFFmpegAction: ((FFmpegStatusView.Action) -> Void)?
    /// 用它弹 sheet。
    weak var settings: SettingsWindowController?
    /// 标题栏高度，内容从这里往下排（透明标题栏，内容铺在标题栏下面）。
    var titlebarInset: CGFloat = 28

    private static let width = SettingsStyle.windowWidth
    private static var contentWidth: CGFloat { width - 32 * 2 }

    private let state: SettingsState
    private var step: Step = .find
    private var discovered: [LANDiscovery.Server] = []
    private var discovering = false
    private var selectedAddress: String?
    private var failure: String?
    private var pollTask: Task<Void, Never>?
    private var countdownTask: Task<Void, Never>?
    private var didAutoDiscover = false

    // 常驻控件：每一步只切换可见性，输入框焦点和内容不会因为重建而丢
    private let stepIndicator = StepIndicator(titles: ["连接", "批准", "完成"])
    private let titleLabel = NSTextField(labelWithString: "")
    private let subtitleLabel = PanelText.wrapping("", size: 13, lines: 4, width: contentWidth, color: .secondaryLabelColor)
    private let findView = NSStackView()
    private let serverList = NSStackView()
    private let rescanButton = NSButton(title: "重新查找", target: nil, action: nil)
    private let addressField = NSTextField()
    private let nameField = NSTextField()
    private let pairingView = NSStackView()
    private let codeLabel = NSTextField(labelWithString: "")
    private let codeLink = NSButton(title: "", target: nil, action: nil)
    private let waitingLabel = NSTextField(labelWithString: "")
    private let doneView = NSStackView()
    private let doneTitle = NSTextField(labelWithString: "")
    private let ffmpegCard = RoundedFillView(fill: .neutral)
    private let ffmpegStatus = FFmpegStatusView(width: contentWidth - 28)
    private let errorLabel = PanelText.wrapping("", size: 12, lines: 5, width: contentWidth, color: .systemRed)
    private let secondaryButton = NSButton(title: "", target: nil, action: nil)
    private let primaryButton = NSButton(title: "", target: nil, action: nil)
    private let busySpinner = NSProgressIndicator()
    private var root: NSStackView?

    init(state: SettingsState) {
        self.state = state
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    deinit {
        pollTask?.cancel()
        countdownTask?.cancel()
    }

    // MARK: - 布局

    override func loadView() {
        titleLabel.font = .systemFont(ofSize: 22, weight: .bold)
        buildFindView()
        buildPairingView()
        buildDoneView()

        ffmpegStatus.onAction = { [weak self] action in self?.onFFmpegAction?(action) }
        ffmpegCard.pin(ffmpegStatus, padding: 12, insetX: 14)

        primaryButton.target = self
        primaryButton.action = #selector(primaryAction)
        primaryButton.bezelStyle = .rounded
        primaryButton.keyEquivalent = "\r"
        secondaryButton.target = self
        secondaryButton.action = #selector(secondaryAction)
        secondaryButton.bezelStyle = .rounded
        if Glass.isAvailable {
            primaryButton.controlSize = .large
            secondaryButton.controlSize = .large
        }
        busySpinner.style = .spinning
        busySpinner.controlSize = .small
        busySpinner.isDisplayedWhenStopped = false
        let bar = NSStackView(views: [secondaryButton, SettingsStyle.flexibleSpacer(), busySpinner, primaryButton])
        bar.orientation = .horizontal
        bar.alignment = .centerY
        bar.spacing = 10

        let root = NSStackView(views: [
            stepIndicator, titleLabel, subtitleLabel, findView, pairingView, doneView, ffmpegCard, errorLabel, bar,
        ])
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 16
        root.setCustomSpacing(18, after: stepIndicator)
        root.setCustomSpacing(6, after: titleLabel)
        root.setCustomSpacing(22, after: subtitleLabel)
        root.translatesAutoresizingMaskIntoConstraints = false
        for view in root.arrangedSubviews where view !== titleLabel {
            view.widthAnchor.constraint(equalToConstant: Self.contentWidth).isActive = true
        }
        self.root = root

        let container = NSView()
        container.addSubview(root)
        // 底边约束降到低优先级：高度完全由内容决定，窗口再跟着内容走
        let bottom = root.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -24)
        bottom.priority = .defaultLow
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 32),
            root.topAnchor.constraint(equalTo: container.topAnchor, constant: titlebarInset + 6),
            container.widthAnchor.constraint(equalToConstant: Self.width),
            bottom,
        ])
        view = container
        render(state)
    }

    private func buildFindView() {
        let heading = SettingsStyle.groupLabel("局域网中的 movieclaw")
        rescanButton.target = self
        rescanButton.action = #selector(rescan)
        rescanButton.isBordered = false
        rescanButton.font = .systemFont(ofSize: 12)
        rescanButton.contentTintColor = .linkColor
        let headingRow = NSStackView(views: [heading, SettingsStyle.flexibleSpacer(), rescanButton])
        headingRow.orientation = .horizontal
        headingRow.alignment = .firstBaseline

        serverList.orientation = .vertical
        serverList.alignment = .leading
        serverList.spacing = 8

        addressField.placeholderString = "http://10.1.1.5:3000"
        addressField.font = .monospacedSystemFont(ofSize: 13, weight: .regular)
        addressField.delegate = self
        addressField.setAccessibilityLabel("movieclaw 地址")
        let addressGroup = SectionView(title: "或者手动填写地址", rows: [SettingsStyle.row("地址", trailing: addressField)])

        nameField.delegate = self
        nameField.setAccessibilityLabel("这台 Mac 的名称")
        let nameGroup = SectionView(title: "这台 Mac", rows: [SettingsStyle.row("名称", trailing: nameField)])
        nameGroup.note = "会显示在网页的设备列表和播放活动里。"

        for view in [headingRow, serverList, addressGroup, nameGroup] {
            findView.addArrangedSubview(view)
            view.widthAnchor.constraint(equalToConstant: Self.contentWidth).isActive = true
        }
        findView.orientation = .vertical
        findView.alignment = .leading
        findView.spacing = 8
        findView.setCustomSpacing(20, after: serverList)
        findView.setCustomSpacing(20, after: addressGroup)
        for field in [addressField, nameField] {
            field.widthAnchor.constraint(equalToConstant: 260).isActive = true
        }
    }

    private func buildPairingView() {
        let caption = PanelText.label("在网页上核对这个配对码", size: 12, color: .secondaryLabelColor)
        // 等宽 + 加大字号 + 拉开字距：配对码要在屏幕和网页之间用眼睛核对，
        // 0/O、1/l 分不清会直接让人对不上
        codeLabel.font = .monospacedSystemFont(ofSize: 34, weight: .semibold)
        codeLink.target = self
        codeLink.action = #selector(openVerificationPage)
        codeLink.bezelStyle = .rounded
        let card = RoundedFillView(fill: .neutral)
        let column = NSStackView(views: [caption, codeLabel, codeLink])
        column.orientation = .vertical
        column.alignment = .centerX
        column.spacing = 12
        card.pin(column, padding: 22)

        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.startAnimation(nil)
        waitingLabel.font = .monospacedDigitSystemFont(ofSize: 12, weight: .regular)
        waitingLabel.textColor = .secondaryLabelColor
        let waiting = NSStackView(views: [spinner, waitingLabel])
        waiting.orientation = .horizontal
        waiting.spacing = 8

        let note = SettingsStyle.footnote(
            "配对码本身不是密钥，即使被别人看到也拿不到任何权限；批准后连接密钥直接存进这台 Mac 的钥匙串。",
            width: Self.contentWidth
        )
        for view in [card, waiting, note] {
            pairingView.addArrangedSubview(view)
        }
        card.widthAnchor.constraint(equalToConstant: Self.contentWidth).isActive = true
        pairingView.orientation = .vertical
        pairingView.alignment = .centerX
        pairingView.spacing = 14
    }

    private func buildDoneView() {
        let tile = GlyphTile(side: 64, glyph: .symbol("checkmark"))
        doneTitle.font = .systemFont(ofSize: 15, weight: .semibold)
        doneTitle.alignment = .center
        let detail = PanelText.wrapping(
            "之后打开 App 会自动连接，不需要再来这里。点菜单栏图标可以随时看转码状态。",
            size: 12, lines: 3, width: Self.contentWidth, color: .secondaryLabelColor, alignment: .center
        )
        for view in [tile, doneTitle, detail] {
            doneView.addArrangedSubview(view)
        }
        doneView.orientation = .vertical
        doneView.alignment = .centerX
        doneView.spacing = 10
        doneView.setCustomSpacing(16, after: tile)
    }

    // MARK: - 渲染

    func render(_ state: SettingsState) {
        guard isViewLoaded else { return }
        if nameField.stringValue.isEmpty {
            nameField.stringValue = state.snapshot.workerID
        }
        if addressField.stringValue.isEmpty, !state.snapshot.nasURL.isEmpty, step == .find {
            addressField.stringValue = state.snapshot.nasURL
        }

        findView.isHidden = true
        pairingView.isHidden = true
        doneView.isHidden = true
        secondaryButton.isHidden = true
        primaryButton.isHidden = false
        primaryButton.isEnabled = true
        busySpinner.stopAnimation(nil)
        addressField.isEnabled = true
        nameField.isEnabled = true

        switch step {
        case .find, .connecting:
            stepIndicator.current = 0
            titleLabel.stringValue = "连接到 movieclaw"
            subtitleLabel.stringValue = "选择局域网里找到的 movieclaw，或者手动填写地址。"
                + "转码要来回传输大量视频分片，内网直连最快也最稳。"
            findView.isHidden = false
            renderServerList()
            primaryButton.title = "连接并配对"
            if step == .connecting {
                primaryButton.title = "正在连接…"
                primaryButton.isEnabled = false
                busySpinner.startAnimation(nil)
                addressField.isEnabled = false
                nameField.isEnabled = false
            }
        case let .pairing(grant):
            stepIndicator.current = 1
            titleLabel.stringValue = "在网页上批准这台 Mac"
            subtitleLabel.stringValue = "浏览器已打开设备页。用你平时登录 movieclaw 的账号核对下面的配对码并批准。"
            pairingView.isHidden = false
            codeLabel.attributedStringValue = Self.trackedCode(grant.userCode)
            codeLink.title = "打开批准页面（\(Self.displayHost(grant.verificationURI))）"
            secondaryButton.isHidden = false
            secondaryButton.title = "返回"
            primaryButton.isHidden = true
        case .done:
            stepIndicator.current = 2
            titleLabel.stringValue = "一切就绪"
            subtitleLabel.stringValue = "这台 Mac 已经可以为 movieclaw 转码了。"
            doneView.isHidden = false
            doneTitle.stringValue = "\(state.snapshot.workerID) 已连接到 \(DisplayText.host(of: state.snapshot.nasURL) ?? "movieclaw")"
            primaryButton.title = "完成"
        }

        // ffmpeg 状态条：最后一步总是显示（装没装好是用户最后要确认的事）；
        // 前两步只在正在下载或出了问题时显示
        let ffmpegRelevant = state.ffmpeg.isProcessing || state.waitingForJobs
            || { if case .failed = state.ffmpeg { return true } else { return false } }()
        ffmpegCard.isHidden = !(step == .done || ffmpegRelevant)
        ffmpegStatus.apply(
            state.ffmpeg, installedVersion: state.snapshot.managedFFmpegVersion, waitingForJobs: state.waitingForJobs
        )

        errorLabel.stringValue = failure ?? ""
        errorLabel.isHidden = failure == nil || step != .find
        resizeWindowToFit()
    }

    /// 局域网查找结果：查找中一行转圈；找到的每台一张可点的卡片；一台都没有时说明原因。
    private func renderServerList() {
        for view in serverList.arrangedSubviews {
            serverList.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
        rescanButton.isEnabled = !discovering
        var rows: [NSView] = []
        if discovering {
            let spinner = NSProgressIndicator()
            spinner.style = .spinning
            spinner.controlSize = .small
            spinner.startAnimation(nil)
            let row = NSStackView(views: [spinner, PanelText.label("正在局域网中查找…", size: 12.5, color: .secondaryLabelColor)])
            row.orientation = .horizontal
            row.spacing = 8
            let card = RoundedFillView(fill: .neutral)
            card.pin(row, padding: 14)
            rows.append(card)
        } else if discovered.isEmpty {
            let card = RoundedFillView(fill: .neutral)
            let text = PanelText.wrapping(
                "没有找到。跨网段、开了 VPN、服务端关掉了「Jellyfin 兼容层」或跑在桥接网络里时都会找不到，直接在下面填地址即可。",
                size: 12, lines: 4, width: Self.contentWidth - 28, color: .secondaryLabelColor
            )
            card.pin(text, padding: 12, insetX: 14)
            rows.append(card)
        } else {
            for server in discovered {
                let row = ServerChoiceView(server: server, selected: server.address == selectedAddress)
                row.onSelect = { [weak self] in self?.choose(server) }
                rows.append(row)
            }
        }
        for row in rows {
            serverList.addArrangedSubview(row)
            row.widthAnchor.constraint(equalToConstant: Self.contentWidth).isActive = true
        }
    }

    /// 窗口高度贴合内容，顶边不动（窗口原点在左下角，直接改高度会往上长）。
    private func resizeWindowToFit() {
        guard let root, let window = view.window else { return }
        root.layoutSubtreeIfNeeded()
        let height = ceil(root.fittingSize.height) + titlebarInset + 6 + 24
        let target = window.frameRect(forContentRect: NSRect(x: 0, y: 0, width: Self.width, height: height))
        var frame = window.frame
        guard abs(frame.height - target.height) > 0.5 else { return }
        frame.origin.y += frame.height - target.height
        frame.size = target.size
        window.setFrame(frame, display: true, animate: window.isVisible)
    }

    private func transition(to next: Step) {
        countdownTask?.cancel()
        countdownTask = nil
        step = next
        render(state)
        if case let .pairing(grant) = next {
            startCountdown(expiresIn: grant.expiresIn)
        }
    }

    // MARK: - 窗口生命周期

    /// 窗口打开时调用：地址还空着就自动找一次——第一次配置的人正卡在「该填什么」上。
    func didAppear() {
        render(state)
        guard !didAutoDiscover, step == .find else { return }
        didAutoDiscover = true
        discover()
    }

    /// 窗口关掉或切走时停掉轮询与倒计时。
    func cancelPending() {
        pollTask?.cancel()
        pollTask = nil
        countdownTask?.cancel()
        countdownTask = nil
        if case .pairing = step {
            step = .find
        }
    }

    // MARK: - 找服务器

    @objc private func rescan() {
        discover()
    }

    /// 在局域网里找 movieclaw（docs/design/device-auth.md §6.5）。
    ///
    /// **只列不存**：服务端优先返回的是给播放器配的「对外访问地址」，可能是反向代理
    /// 域名——对 Worker 来说走反代明显更慢，选哪个得留给人。只找到一台且地址框还
    /// 空着时替他预选上，仍由他点「连接并配对」拍板。
    private func discover() {
        discovering = true
        render(state)
        Task { [weak self] in
            let found = await Task.detached(priority: .userInitiated) {
                LANDiscovery.find(timeout: 1.5)
            }.value
            guard let self else { return }
            self.discovering = false
            self.discovered = found
            if found.count == 1,
               self.addressField.stringValue.trimmingCharacters(in: .whitespaces).isEmpty {
                self.choose(found[0])
            } else {
                self.render(self.state)
            }
        }
    }

    private func choose(_ server: LANDiscovery.Server) {
        selectedAddress = server.address
        addressField.stringValue = server.address
        failure = nil
        render(state)
    }

    func controlTextDidChange(_ notification: Notification) {
        guard (notification.object as? NSTextField) === addressField else { return }
        // 手动改了地址，卡片上的勾就不再对应它
        let typed = addressField.stringValue.trimmingCharacters(in: .whitespaces)
        let matched = discovered.first { $0.address == typed }?.address
        if matched != selectedAddress {
            selectedAddress = matched
            renderServerList()
        }
    }

    // MARK: - 按钮

    @objc private func primaryAction() {
        switch step {
        case .find:
            Task { await connect() }
        case .done:
            onFinish?()
        case .connecting, .pairing:
            break
        }
    }

    @objc private func secondaryAction() {
        if case .pairing = step {
            cancelPending()
            transition(to: .find)
        }
    }

    @objc private func openVerificationPage() {
        guard case let .pairing(grant) = step, let url = URL(string: grant.verificationURI) else { return }
        NSWorkspace.shared.open(url)
    }

    // MARK: - 配对

    /// 保存设置 → 验证地址可达 → 发起接入请求并等批准，一次做完。
    ///
    /// 验证仍然先做且失败即停：地址填错是自部署产品最容易劝退用户的一步，
    /// 得在发起请求之前给出确定结论。只是这个结论不再需要用户点一下才继续。
    private func connect() async {
        failure = nil
        let address = addressField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !address.isEmpty else {
            failure = "先选一台 movieclaw，或者手动填写地址。"
            render(state)
            return
        }
        if WorkerConfiguration.isInsecureHTTPAddress(address), await confirmInsecureHTTP() == false {
            return
        }
        var draft = WorkerSettingsDraft(state.snapshot)
        draft.nasURL = address
        draft.workerID = nameField.stringValue
        let url: URL
        do {
            url = try WorkerConfiguration.normalizedNASURL(address)
            try onSave?(draft)
        } catch {
            failure = error.localizedDescription
            render(state)
            return
        }

        transition(to: .connecting)
        let pairing = DevicePairing(nasURL: url)
        do {
            _ = try await pairing.verifyConnection()
        } catch {
            failure = "连不上 \(DisplayText.host(of: address) ?? address)：\(error.localizedDescription)\n"
                + "确认地址填写正确、movieclaw 正在运行，然后重试。"
            transition(to: .find)
            return
        }
        startPairing(pairing: pairing, fallbackURL: url)
    }

    /// 发起接入请求并轮询兑换，直到拿到令牌或得到确定的失败结论。
    private func startPairing(pairing: DevicePairing, fallbackURL: URL) {
        let name = nameField.stringValue
        pollTask?.cancel()
        pollTask = Task { @MainActor in
            do {
                let grant = try await pairing.authorize(clientName: name)
                transition(to: .pairing(grant))
                NSWorkspace.shared.open(URL(string: grant.verificationURI) ?? fallbackURL)
                try await awaitApproval(pairing: pairing, grant: grant)
            } catch is CancellationError {
                // 用户点了返回或关了窗口，界面已经切走
            } catch {
                failure = error.localizedDescription
                transition(to: .find)
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
                transition(to: .done)
                return
            case let .finished(reason):
                failure = reason
                transition(to: .find)
                return
            }
        }
        failure = "配对超时：有效期内没有等到批准，请重新发起。"
        transition(to: .find)
    }

    /// 配对码有效期倒计时。知道还剩多久，人才不会在浏览器那边慢慢找。
    private func startCountdown(expiresIn: Int) {
        let deadline = Date().addingTimeInterval(TimeInterval(expiresIn))
        countdownTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let left = Int(deadline.timeIntervalSinceNow)
                guard left > 0 else {
                    self.waitingLabel.stringValue = "配对码已过期"
                    return
                }
                self.waitingLabel.stringValue = String(
                    format: "等待批准 · %d 分 %02d 秒后过期", left / 60, left % 60
                )
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
        }
    }

    private func confirmInsecureHTTP() async -> Bool {
        let alert = NSAlert()
        alert.messageText = "确认使用内网 HTTP？"
        alert.informativeText = "HTTP 只适合可信内网：源视频、转码分片和控制消息都不会加密。"
            + "请确认这个地址没有暴露到公网或不可信网络。"
        alert.alertStyle = .warning
        alert.addButton(withTitle: "继续使用 HTTP")
        alert.addButton(withTitle: "取消")
        guard let settings else { return alert.runModal() == .alertFirstButtonReturn }
        return await settings.runSheet(alert) == .alertFirstButtonReturn
    }

    // MARK: - 文本工具

    /// 只留主机和端口：设备页链接上显示这一段就够认了。
    private static func displayHost(_ raw: String) -> String {
        var text = DisplayText.host(of: raw) ?? raw
        if let slash = text.firstIndex(of: "/") {
            text = String(text[text.startIndex..<slash])
        }
        return text
    }

    /// 配对码的字距。AppKit 的 `stringValue` 没有字距，只能走 attributed string。
    /// 字距只加到倒数第二个字符：加在最后一个后面会多出尾随空白，让居中的码整体偏左。
    private static func trackedCode(_ code: String) -> NSAttributedString {
        let attributed = NSMutableAttributedString(
            string: code,
            attributes: [
                .font: NSFont.monospacedSystemFont(ofSize: 34, weight: .semibold),
                .foregroundColor: NSColor.labelColor,
            ]
        )
        let length = (code as NSString).length
        if length > 1 {
            attributed.addAttribute(.kern, value: 5, range: NSRange(location: 0, length: length - 1))
        }
        return attributed
    }
}

// MARK: - 小部件

/// 引导顶部的步骤指示：「● 连接 —— ○ 批准 —— ○ 完成」。
private final class StepIndicator: NSView {
    var current = 0 {
        didSet { apply() }
    }

    private var dots: [NSImageView] = []
    private var labels: [NSTextField] = []

    init(titles: [String]) {
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        var views: [NSView] = []
        for (index, title) in titles.enumerated() {
            if index > 0 {
                let line = HairlineView()
                line.leadingInset = 0
                line.translatesAutoresizingMaskIntoConstraints = false
                line.heightAnchor.constraint(equalToConstant: 1).isActive = true
                line.widthAnchor.constraint(equalToConstant: 28).isActive = true
                views.append(line)
            }
            let dot = NSImageView()
            let label = PanelText.label(title, size: 12, weight: .medium, color: .secondaryLabelColor)
            dots.append(dot)
            labels.append(label)
            let pair = NSStackView(views: [dot, label])
            pair.orientation = .horizontal
            pair.spacing = 5
            views.append(pair)
        }
        let row = NSStackView(views: views)
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 8
        pin(row, padding: 0)
        apply()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    private func apply() {
        for (index, dot) in dots.enumerated() {
            let symbol = index < current ? "checkmark.circle.fill" : (index == current ? "circle.inset.filled" : "circle")
            dot.image = Symbols.image(symbol, pointSize: 12, weight: .medium)
            dot.contentTintColor = index <= current ? .controlAccentColor : .tertiaryLabelColor
            labels[index].textColor = index == current ? .labelColor : .secondaryLabelColor
        }
    }
}

/// 局域网里找到的一台 movieclaw：整张卡片可点，选中时右侧打勾、描一圈强调色。
private final class ServerChoiceView: RoundedFillView {
    var onSelect: (() -> Void)?
    private let selected: Bool

    init(server: LANDiscovery.Server, selected: Bool) {
        self.selected = selected
        super.init(fill: selected ? .tinted(.controlAccentColor) : .neutral)
        let icon = NSImageView(image: Symbols.image("server.rack", pointSize: 17, weight: .regular) ?? NSImage())
        icon.contentTintColor = selected ? .controlAccentColor : .secondaryLabelColor
        let name = PanelText.label(server.displayName, size: 13, weight: .semibold, color: .labelColor)
        let address = NSTextField(labelWithString: server.address)
        address.font = .monospacedSystemFont(ofSize: 11.5, weight: .regular)
        address.textColor = .secondaryLabelColor
        let texts = NSStackView(views: [name, address])
        texts.orientation = .vertical
        texts.alignment = .leading
        texts.spacing = 2
        let check = NSImageView(image: Symbols.image(
            selected ? "checkmark.circle.fill" : "circle", pointSize: 16, weight: .regular
        ) ?? NSImage())
        check.contentTintColor = selected ? .controlAccentColor : .tertiaryLabelColor
        let row = NSStackView(views: [icon, texts, SettingsStyle.flexibleSpacer(), check])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 12
        pin(row, padding: 11, insetX: 14)
        setAccessibilityRole(.radioButton)
        setAccessibilityLabel("\(server.displayName)，\(server.address)")
        setAccessibilityValue(selected ? 1 : 0)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard selected else { return }
        let path = NSBezierPath(
            roundedRect: bounds.insetBy(dx: 0.75, dy: 0.75),
            xRadius: RoundedFillView.cornerRadius, yRadius: RoundedFillView.cornerRadius
        )
        path.lineWidth = 1.5
        NSColor.controlAccentColor.setStroke()
        path.stroke()
    }

    override func mouseDown(with event: NSEvent) {
        onSelect?()
    }

    override func accessibilityPerformPress() -> Bool {
        onSelect?()
        return true
    }
}
