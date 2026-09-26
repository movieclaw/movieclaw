import AppKit

/// 设置窗三页的共同骨架：固定宽度、上下留白，高度随内容（窗口跟着切页变高变矮）。
@MainActor
class PaneController: NSViewController, SettingsPane {
    weak var settings: SettingsWindowController?
    let stack = NSStackView()
    static let padding: CGFloat = 22
    static var contentWidth: CGFloat { SettingsStyle.windowWidth - padding * 2 }

    init(settings: SettingsWindowController, title: String) {
        self.settings = settings
        super.init(nibName: nil, bundle: nil)
        self.title = title
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func loadView() {
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = SettingsStyle.sectionSpacing
        let container = NSView()
        stack.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(stack)
        // 底边不钉死：窗口比内容高时（切页动画途中）多出的空白落在底部，而不是把分区之间撑开
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: Self.padding),
            stack.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -Self.padding),
            stack.topAnchor.constraint(equalTo: container.topAnchor, constant: Self.padding),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: container.bottomAnchor, constant: -Self.padding),
        ])
        view = container
        build()
        if let settings {
            render(settings.state)
        }
    }

    /// 子类在这里搭控件（只调一次）。
    func build() {}

    /// 子类按状态刷新控件，最后调 ``fit()``。
    func render(_ state: SettingsState) {}

    func add(_ view: NSView, spacingAfter: CGFloat? = nil) {
        stack.addArrangedSubview(view)
        view.widthAnchor.constraint(equalToConstant: Self.contentWidth).isActive = true
        if let spacingAfter {
            stack.setCustomSpacing(spacingAfter, after: view)
        }
    }

    /// 这一页要多高。当前显示的就是这一页时，窗口立刻跟着变（顶边不动）。
    func fit() {
        guard isViewLoaded else { return }
        stack.layoutSubtreeIfNeeded()
        let height = ceil(stack.fittingSize.height) + Self.padding * 2
        (parent as? SettingsTabsController)?.fitWindow(for: self, height: height)
    }
}

/// 分页控制器：切页时窗口高度贴合这一页的内容。
///
/// `NSTabViewController` 自己只在 preferredContentSize 变化时调窗口，而且会从
/// 底边往上长；这里显式按内容算高度、保持顶边不动（标题栏和工具栏不跟着跳）。
@MainActor
final class SettingsTabsController: NSTabViewController {
    override func tabView(_ tabView: NSTabView, didSelect tabViewItem: NSTabViewItem?) {
        super.tabView(tabView, didSelect: tabViewItem)
        guard let pane = tabViewItem?.viewController as? PaneController else { return }
        // 第一次切到这一页时它的视图可能还没加载（加载时量高度又还不在窗口里），先加载再量
        _ = pane.view
        pane.fit()
    }

    func fitWindow(for pane: PaneController, height: CGFloat) {
        // 以 tabView 自己的选中项为准：didSelect 回调时 selectedTabViewItemIndex 还没跟上
        guard let window = view.window, tabView.selectedTabViewItem?.viewController === pane else { return }
        let target = window.frameRect(forContentRect: NSRect(x: 0, y: 0, width: SettingsStyle.windowWidth, height: height))
        var frame = window.frame
        guard abs(frame.height - target.height) > 0.5 || abs(frame.width - target.width) > 0.5 else { return }
        frame.origin.y += frame.height - target.height
        frame.size = target.size
        window.setFrame(frame, display: true, animate: window.isVisible)
    }
}

// MARK: - 连接

/// 「连接」页：连着哪台服务器、现在通不通，这台 Mac 叫什么、要不要自动连接。
/// 地址不可编辑：换服务器就「断开并重新配置」重新走一遍配对。
@MainActor
final class ConnectionPaneController: PaneController, NSTextFieldDelegate {
    private let statusDot = StatusDot()
    private let statusTitle = NSTextField(labelWithString: "")
    private let statusDetail = NSTextField(labelWithString: "")
    private let nameField = NSTextField()
    private let nameError = PanelText.wrapping("", size: 11, lines: 2, width: contentWidth, color: .systemRed)
    private let autoConnect = NSSwitch()
    private let launchAtLogin = NSSwitch()
    private let loginHint = PanelText.wrapping("", size: 11, lines: 2, width: contentWidth, color: .systemOrange)

    init(settings: SettingsWindowController) {
        super.init(settings: settings, title: "连接")
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func build() {
        // 服务器卡片：状态点 + 状态 + 地址与连接时长 + 打开网页
        statusTitle.font = .systemFont(ofSize: 14, weight: .semibold)
        statusDetail.font = .monospacedDigitSystemFont(ofSize: 12, weight: .regular)
        statusDetail.textColor = .secondaryLabelColor
        statusDetail.lineBreakMode = .byTruncatingMiddle
        let texts = NSStackView(views: [statusTitle, statusDetail])
        texts.orientation = .vertical
        texts.alignment = .leading
        texts.spacing = 2
        let open = NSButton(title: "打开网页", target: self, action: #selector(openWeb))
        open.bezelStyle = .rounded
        let serverRow = NSStackView(views: [statusDot, texts, SettingsStyle.flexibleSpacer(), open])
        serverRow.orientation = .horizontal
        serverRow.alignment = .centerY
        serverRow.spacing = 10
        serverRow.edgeInsets = NSEdgeInsets(top: 12, left: 14, bottom: 12, right: 12)
        serverRow.translatesAutoresizingMaskIntoConstraints = false
        add(SectionView(title: "服务器", rows: [serverRow]))

        nameField.delegate = self
        nameField.widthAnchor.constraint(equalToConstant: 220).isActive = true
        nameField.setAccessibilityLabel("名称")
        autoConnect.target = self
        autoConnect.action = #selector(toggleAutoConnect)
        autoConnect.controlSize = .small
        let switchHolder = NSStackView(views: [SettingsStyle.flexibleSpacer(), autoConnect])
        switchHolder.orientation = .horizontal
        launchAtLogin.target = self
        launchAtLogin.action = #selector(toggleLaunchAtLogin)
        launchAtLogin.controlSize = .small
        let loginHolder = NSStackView(views: [SettingsStyle.flexibleSpacer(), launchAtLogin])
        loginHolder.orientation = .horizontal
        let machine = SectionView(title: "这台 Mac", rows: [
            SettingsStyle.row("名称", trailing: nameField),
            SettingsStyle.row("启动后自动连接", trailing: switchHolder),
            SettingsStyle.row("开机时自动启动", trailing: loginHolder),
        ])
        machine.note = "名称会显示在网页的设备列表和播放活动里。改名会重新连接一次。"
            + "开机自启动还会在 App 意外退出后自动把它重新打开。"
        add(machine, spacingAfter: 6)
        add(nameError)
        let approve = NSButton(title: "打开「登录项」设置", target: self, action: #selector(openLoginItems))
        approve.bezelStyle = .rounded
        approve.controlSize = .small
        let hintRow = NSStackView(views: [loginHint, SettingsStyle.flexibleSpacer(), approve])
        hintRow.orientation = .horizontal
        hintRow.alignment = .centerY
        hintRow.identifier = NSUserInterfaceItemIdentifier("loginHint")
        add(hintRow)

        let reset = NSButton(title: "断开并重新配置…", target: self, action: #selector(reset))
        reset.isBordered = false
        reset.contentTintColor = .systemRed
        reset.font = .systemFont(ofSize: 12.5)
        let resetRow = NSStackView(views: [reset, SettingsStyle.flexibleSpacer()])
        resetRow.orientation = .horizontal
        add(resetRow)
    }

    override func viewDidAppear() {
        super.viewDidAppear()
        // 别一开窗就把光标放进「名称」框、还全选了：来这一页多半是看状态，不是改名
        view.window?.makeFirstResponder(nil)
    }

    override func render(_ state: SettingsState) {
        guard isViewLoaded else { return }
        let presentation = WorkerStatePresentation.make(state.status?.state, configured: true)
        statusDot.color = presentation.color
        statusTitle.stringValue = state.status == nil ? "已配对，未连接" : presentation.title
        let host = DisplayText.host(of: state.snapshot.nasURL) ?? "-"
        statusDetail.stringValue = [host, state.connectedSince.map(Self.since)].compactMap { $0 }.joined(separator: " · ")
        // 正在输入时不覆盖
        if nameField.currentEditor() == nil {
            nameField.stringValue = state.snapshot.workerID
        }
        autoConnect.state = state.snapshot.autoConnect ? .on : .off
        let login = LoginItem.state
        launchAtLogin.state = login == .enabled || login == .requiresApproval ? .on : .off
        loginHint.stringValue = login == .requiresApproval
            ? "已加入登录项，但还要在「系统设置 → 通用 → 登录项」里允许它。"
            : ""
        stack.arrangedSubviews.first { $0.identifier?.rawValue == "loginHint" }?.isHidden = login != .requiresApproval
        nameError.isHidden = nameError.stringValue.isEmpty
        fit()
    }

    /// 「已连接 2 小时 5 分」「已连接 12 分钟」「刚刚连上」
    private static func since(_ date: Date) -> String {
        let minutes = Int(Date().timeIntervalSince(date) / 60)
        if minutes < 1 { return "刚刚连上" }
        if minutes < 60 { return "已连接 \(minutes) 分钟" }
        return minutes % 60 == 0 ? "已连接 \(minutes / 60) 小时" : "已连接 \(minutes / 60) 小时 \(minutes % 60) 分"
    }

    func controlTextDidEndEditing(_ notification: Notification) {
        guard let settings else { return }
        let name = nameField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard name != settings.state.snapshot.workerID else {
            nameError.stringValue = ""
            render(settings.state)
            return
        }
        do {
            _ = try WorkerConfiguration.validatedWorkerID(name)
            nameError.stringValue = ""
        } catch {
            // 就地说明，不弹窗：改名是正在输入的事，打断它比错误本身还烦
            nameError.stringValue = error.localizedDescription
            render(settings.state)
            return
        }
        Task { await settings.apply(restartsWorker: true) { $0.workerID = name } }
    }

    @objc private func toggleAutoConnect() {
        let on = autoConnect.state == .on
        Task { await settings?.apply(restartsWorker: false) { $0.autoConnect = on } }
    }

    @objc private func toggleLaunchAtLogin() {
        do {
            try LoginItem.setEnabled(launchAtLogin.state == .on)
        } catch {
            settings?.showError(error.localizedDescription, title: "没能修改开机自启动")
        }
        if let settings {
            render(settings.state)
        }
    }

    @objc private func openLoginItems() {
        LoginItem.openSystemSettings()
    }

    @objc private func openWeb() {
        guard let text = settings?.state.snapshot.nasURL, let url = URL(string: text) else { return }
        NSWorkspace.shared.open(url)
    }

    @objc private func reset() {
        Task { await settings?.confirmAndClear() }
    }
}

// MARK: - 转码

/// 「转码」页：用哪个 ffmpeg、它能硬件编码什么、同时转几路。
/// 原来单独弹出的 Jellyfin-ffmpeg 下载窗并到了这一页里。
@MainActor
final class TranscodePaneController: PaneController {
    private let source = NSSegmentedControl(
        labels: ["Jellyfin-ffmpeg（推荐）", "自定义"], trackingMode: .selectOne, target: nil, action: nil
    )
    private let ffmpegStatus = FFmpegStatusView(width: contentWidth - SettingsStyle.rowPaddingX * 2)
    private var managedRow = NSView()
    private let pathValue = SettingsStyle.rowValue(mono: true)
    private var pathRow = NSView()
    private var ffmpegSection: SectionView?

    private let codecsColumn = NSStackView()
    private var codecsSection: SectionView?
    private let jobsValue = NSTextField(labelWithString: "")
    private let jobsStepper = NSStepper()

    /// 用户点了另一种来源、但还没真正切过去（Jellyfin-ffmpeg 还在下载 / 自定义还没选文件）。
    private var pendingSource: FFmpegSource?
    /// 能力检测：按路径缓存，路径变了才重测。
    private var probedPath: String?
    private var probe: Result<WorkerCapabilities, Error>?

    init(settings: SettingsWindowController) {
        super.init(settings: settings, title: "转码")
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func build() {
        source.target = self
        source.action = #selector(sourceChanged)
        source.segmentDistribution = .fillEqually
        source.widthAnchor.constraint(equalToConstant: 280).isActive = true
        let sourceRow = SettingsStyle.row("来源", trailing: NSStackView(views: [SettingsStyle.flexibleSpacer(), source]))

        ffmpegStatus.onAction = { [weak self] action in
            self?.settings?.ffmpegAction(action, preferManaged: self?.pendingSource == .managed)
        }
        let managedHolder = NSView()
        managedHolder.pin(ffmpegStatus, padding: 12, insetX: SettingsStyle.rowPaddingX)
        managedRow = managedHolder

        let choose = NSButton(title: "选择…", target: self, action: #selector(choosePath))
        choose.bezelStyle = .rounded
        pathValue.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        pathRow = SettingsStyle.row("路径", trailing: NSStackView(views: [pathValue, choose]))

        let ffmpeg = SectionView(title: "ffmpeg", rows: [sourceRow, managedRow, pathRow])
        ffmpegSection = ffmpeg
        add(ffmpeg)

        codecsColumn.orientation = .vertical
        codecsColumn.alignment = .leading
        codecsColumn.spacing = 0
        let codecs = SectionView(title: "硬件编码", rows: [codecsColumn])
        codecs.note = "检测的是当前使用的 ffmpeg，连接时会上报给 movieclaw。"
        codecsSection = codecs
        add(codecs)

        jobsStepper.minValue = 1
        jobsStepper.maxValue = 4
        jobsStepper.increment = 1
        jobsStepper.valueWraps = false
        jobsStepper.target = self
        jobsStepper.action = #selector(jobsChanged)
        jobsValue.font = .monospacedDigitSystemFont(ofSize: 13, weight: .regular)
        jobsValue.textColor = .secondaryLabelColor
        let jobsControl = NSStackView(views: [SettingsStyle.flexibleSpacer(), jobsValue, jobsStepper])
        jobsControl.orientation = .horizontal
        jobsControl.spacing = 6
        let jobs = SectionView(title: "并发", rows: [SettingsStyle.row("同时转码", trailing: jobsControl)])
        jobs.note = "同时进行的转码任务数。多人同时观看需要转码的片子时才需要调高；每多一路，每一路都会变慢。"
        add(jobs)
    }

    override func render(_ state: SettingsState) {
        guard isViewLoaded else { return }
        let snapshot = state.snapshot
        // 真正切过去了，就不再是「待定」
        if pendingSource == snapshot.ffmpegSource { pendingSource = nil }
        let shown = pendingSource ?? snapshot.ffmpegSource
        source.selectedSegment = shown == .managed ? 0 : 1
        managedRow.isHidden = shown != .managed
        pathRow.isHidden = shown != .custom
        ffmpegStatus.apply(state.ffmpeg, installedVersion: snapshot.managedFFmpegVersion, waitingForJobs: state.waitingForJobs)
        pathValue.stringValue = snapshot.ffmpegSource == .custom ? snapshot.ffmpegPath : "未选择"
        pathValue.toolTip = pathValue.stringValue
        ffmpegSection?.refresh()

        jobsStepper.integerValue = snapshot.maxJobs
        jobsValue.stringValue = "\(snapshot.maxJobs) 路"

        startProbeIfNeeded(path: snapshot.ffmpegPath)
        renderCodecs()
        fit()
    }

    // MARK: 硬件编码

    private func startProbeIfNeeded(path: String) {
        guard path != probedPath else { return }
        probedPath = path
        probe = nil
        Task { [weak self] in
            let result = await Task.detached(priority: .utility) {
                Result { try CapabilityProbe.run(ffmpegPath: path) }
            }.value
            guard let self, self.probedPath == path else { return }
            self.probe = result
            self.renderCodecs()
            self.fit()
        }
    }

    private func renderCodecs() {
        for view in codecsColumn.arrangedSubviews {
            codecsColumn.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
        var rows: [NSView] = []
        switch probe {
        case .none:
            let spinner = NSProgressIndicator()
            spinner.style = .spinning
            spinner.controlSize = .small
            spinner.startAnimation(nil)
            rows.append(SettingsStyle.rawRow(views: [spinner, SettingsStyle.rowValue("正在检测…")]))
        case let .failure(error):
            let label = PanelText.wrapping(error.localizedDescription, size: 12, lines: 4,
                                           width: SettingsStyle.rowContentWidth, color: .systemRed)
            rows.append(SettingsStyle.rawRow(views: [label]))
        case let .success(capabilities):
            let available = Set(DisplayText.hardwareCodecs(capabilities.encoders))
            var codecs = ["H.264", "HEVC"]
            if available.contains("ProRes") { codecs.append("ProRes") }
            for codec in codecs {
                rows.append(Self.codecRow(codec, supported: available.contains(codec)))
            }
        }
        for (index, row) in rows.enumerated() {
            if index > 0 {
                let line = HairlineView()
                line.translatesAutoresizingMaskIntoConstraints = false
                line.heightAnchor.constraint(equalToConstant: 1).isActive = true
                codecsColumn.addArrangedSubview(line)
                line.widthAnchor.constraint(equalTo: codecsColumn.widthAnchor).isActive = true
            }
            codecsColumn.addArrangedSubview(row)
            row.widthAnchor.constraint(equalTo: codecsColumn.widthAnchor).isActive = true
        }
    }

    private static func codecRow(_ codec: String, supported: Bool) -> NSView {
        let icon = NSImageView(image: Symbols.image(
            supported ? "checkmark.circle.fill" : "minus.circle", pointSize: 13, weight: .medium
        ) ?? NSImage())
        icon.contentTintColor = supported ? .systemGreen : .tertiaryLabelColor
        let value = SettingsStyle.rowValue(supported ? "VideoToolbox 硬件编码" : "不支持")
        let trailing = NSStackView(views: [SettingsStyle.flexibleSpacer(), value, icon])
        trailing.orientation = .horizontal
        trailing.spacing = 6
        return SettingsStyle.row(codec, trailing: trailing)
    }

    // MARK: 动作

    @objc private func sourceChanged() {
        guard let settings else { return }
        let snapshot = settings.state.snapshot
        let chosen: FFmpegSource = source.selectedSegment == 0 ? .managed : .custom
        guard chosen != snapshot.ffmpegSource else {
            pendingSource = nil
            render(settings.state)
            return
        }
        switch chosen {
        case .managed:
            if let managed = snapshot.managedFFmpegPath {
                Task { await settings.apply(restartsWorker: true) { $0.ffmpegPath = managed } }
            } else {
                // 还没下载过：先下，装好后由 AppMain 切过去
                pendingSource = .managed
                settings.ffmpegAction(.start, preferManaged: true)
            }
        case .custom:
            // 先亮出路径行，选好文件才真正切换
            pendingSource = .custom
        }
        render(settings.state)
    }

    @objc private func choosePath() {
        guard let settings, let window = view.window else { return }
        let panel = NSOpenPanel()
        panel.message = "选择带 VideoToolbox 的 ffmpeg 可执行文件"
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.treatsFilePackagesAsDirectories = true
        panel.directoryURL = URL(fileURLWithPath: "/opt/homebrew/bin")
        panel.beginSheetModal(for: window) { [weak self] response in
            guard response == .OK, let path = panel.url?.path else { return }
            Task { @MainActor [weak self] in
                // 先验能力再保存：选错文件不该让 Worker 重启后连不上
                let result = await Task.detached(priority: .userInitiated) {
                    Result { try CapabilityProbe.run(ffmpegPath: path) }
                }.value
                if case let .failure(error) = result {
                    settings.showError(error.localizedDescription, title: "这个 ffmpeg 不能用")
                    return
                }
                if await settings.apply(restartsWorker: true, { $0.ffmpegPath = path }) {
                    self?.pendingSource = nil
                }
            }
        }
    }

    @objc private func jobsChanged() {
        let value = jobsStepper.integerValue
        jobsValue.stringValue = "\(value) 路"
        Task { await settings?.apply(restartsWorker: true) { $0.maxJobs = value } }
    }
}

// MARK: - 关于

/// 「关于」页：版本、诊断入口、在网页里管理这台设备。
@MainActor
final class AboutPaneController: PaneController {
    private let versions = NSStackView()
    private let copyButton = NSButton(title: "复制诊断信息", target: nil, action: nil)
    private let ffmpegValue = SettingsStyle.rowValue(mono: true)

    init(settings: SettingsWindowController) {
        super.init(settings: settings, title: "关于")
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func build() {
        let tile = GlyphTile(side: 64)
        let name = PanelText.label("MovieClaw 转码器", size: 17, weight: .semibold, color: .labelColor)
        let tagline = PanelText.label("用这台 Mac 的硬件为 movieclaw 转码", size: 12, color: .secondaryLabelColor)
        let hero = NSStackView(views: [tile, name, tagline])
        hero.orientation = .vertical
        hero.alignment = .centerX
        hero.spacing = 6
        hero.setCustomSpacing(12, after: tile)
        add(hero)

        // 取值靠右，与系统设置里的只读信息行一致
        func valueRow(_ title: String, _ value: NSTextField) -> NSView {
            let trailing = NSStackView(views: [SettingsStyle.flexibleSpacer(), value])
            trailing.orientation = .horizontal
            return SettingsStyle.row(title, trailing: trailing)
        }
        let info = SectionView(title: nil, rows: [
            valueRow("版本", SettingsStyle.rowValue(BuildInfo.version, mono: true)),
            valueRow("ffmpeg", ffmpegValue),
            valueRow("系统", SettingsStyle.rowValue(ProcessInfo.processInfo.operatingSystemVersionString, mono: true)),
        ])
        add(info)

        let log = NSButton(title: "打开日志", target: self, action: #selector(openLog))
        copyButton.target = self
        copyButton.action = #selector(copyDiagnostics)
        let devices = NSButton(title: "在网页中管理设备", target: self, action: #selector(openDevices))
        for button in [log, copyButton, devices] {
            button.bezelStyle = .rounded
        }
        let buttons = NSStackView(views: [log, copyButton, SettingsStyle.flexibleSpacer(), devices])
        buttons.orientation = .horizontal
        buttons.spacing = 8
        add(buttons, spacingAfter: 8)
        add(SettingsStyle.footnote(
            "连接密钥保存在这台 Mac 的钥匙串里，任何界面都不会显示。要停用这台 Mac，在网页「设置 → 设备」里吊销即可。"
                + "诊断信息已脱敏，可以直接贴到 issue 里。",
            width: Self.contentWidth
        ))
    }

    override func render(_ state: SettingsState) {
        guard isViewLoaded else { return }
        let reported = state.status.map { DisplayText.ffmpegVersion($0.ffmpegVersion) }
        ffmpegValue.stringValue = reported.flatMap { $0 == "-" ? nil : $0 }
            ?? state.snapshot.managedFFmpegVersion
            ?? "—"
        fit()
    }

    @objc private func openLog() {
        settings?.onOpenLog?()
    }

    @objc private func copyDiagnostics() {
        settings?.onCopyDiagnostics?()
        copyButton.title = "已复制"
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            self?.copyButton.title = "复制诊断信息"
        }
    }

    @objc private func openDevices() {
        guard let base = settings?.state.snapshot.nasURL,
              let url = URL(string: base.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + "/settings/devices")
        else { return }
        NSWorkspace.shared.open(url)
    }
}
