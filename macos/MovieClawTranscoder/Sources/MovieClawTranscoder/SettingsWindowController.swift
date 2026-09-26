import AppKit

/// 设置窗位置的持久化键。
private let settingsFrameAutosaveName = "MovieClawTranscoderSettings"

/// 设置窗的分页。
enum SettingsTab: Int {
    case connection
    case transcode
    case about
}

/// 设置窗各页共享的当前状态。AppMain 往里推，页面照着画。
@MainActor
final class SettingsState {
    var snapshot: WorkerSettingsSnapshot
    /// 实时连接状态。「已授权」和「连着」是两回事：Mac 睡了、网断了，授权都还在。
    var status: WorkerStatus?
    /// 这一次连上 NAS 的时间，「已连接 2 小时」用。
    var connectedSince: Date?
    var ffmpeg: FFmpegDownloadState = .idle
    /// 要更新 ffmpeg 但手上还有任务，正在等它们转完。
    var waitingForJobs = false

    init(snapshot: WorkerSettingsSnapshot) {
        self.snapshot = snapshot
    }
}

/// 设置窗的一页。
@MainActor
protocol SettingsPane: AnyObject {
    func render(_ state: SettingsState)
}

/// 设置窗：没配对时是三步引导，配好之后是 macOS 标准的顶部图标分页。
///
/// 两种形态共用一个窗口，只换内容：
/// - **引导**（``OnboardingViewController``）：找到服务器 → 网页批准 → 完成。透明标题栏，
///   整窗只讲当前这一步；
/// - **分页**（`NSTabViewController` 的 toolbar 样式，和系统自带 App 的设置窗一样）：
///   「连接」「转码」「关于」三页。改动即时生效，没有「保存」按钮——这是 macOS
///   设置窗的惯例，也省掉「改了忘了点保存」这一类困惑。
///
/// 配好之后服务器地址不可编辑，要换就「断开并重新配置」回到引导第一步
/// （docs/design/device-auth.md §5.1）。
@MainActor
final class SettingsWindowController: NSWindowController, NSWindowDelegate {
    /// 保存非敏感设置（地址、名称、ffmpeg、并发、自启）。是否要重启 Worker 由调用方判断。
    var onSave: ((WorkerSettingsDraft) throws -> Void)?
    /// 配对成功，把令牌交给调用方落钥匙串并启动 Worker。
    var onPaired: ((String) throws -> Void)?
    /// 清除本机配置与令牌。
    var onClear: (() throws -> Void)?
    /// 下载 / 更新 Jellyfin-ffmpeg。参数为 true 表示装好后要切换过去用它
    /// （用户在「转码」页选了 Jellyfin-ffmpeg，但还没下载过）。
    var onManageFFmpeg: ((Bool) -> Void)?
    var onCancelFFmpeg: (() -> Void)?
    var onOpenLog: (() -> Void)?
    var onCopyDiagnostics: (() -> Void)?
    /// 窗口关掉了（AppMain 据此把整个控制器丢掉）。
    var onClose: (() -> Void)?

    let state: SettingsState
    private var onboarding: OnboardingViewController?
    private var panes: [SettingsPane] = []
    private var tabs: SettingsTabsController?

    init(snapshot: WorkerSettingsSnapshot) {
        state = SettingsState(snapshot: snapshot)
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: SettingsStyle.windowWidth, height: 460),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.isReleasedWhenClosed = false
        super.init(window: window)
        window.delegate = self
        if snapshot.tokenConfigured {
            showTabs(select: .connection)
        } else {
            showOnboarding()
        }
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func showWindowAndFocus(tab: SettingsTab? = nil) {
        if let tab, let tabs {
            tabs.selectedTabViewItemIndex = tab.rawValue
        }
        if window?.isVisible != true {
            // 记住用户挪过的位置：每次打开都跳回屏幕正中是很烦的。先恢复位置再按
            // 内容定高度（顶边不动）——反过来的话，存下来的旧高度会把刚量好的覆盖掉
            if window?.setFrameUsingName(settingsFrameAutosaveName) != true {
                window?.center()
            }
            window?.setFrameAutosaveName(settingsFrameAutosaveName)
        }
        render()
        window?.makeKeyAndOrderFront(nil)
        // activate(ignoringOtherApps:) 在 macOS 14 已废弃，且抢焦点的行为一向
        // 不被推荐；新 API 由系统判断该不该把 App 提到前面
        if #available(macOS 14.0, *) {
            NSApp.activate()
        } else {
            NSApp.activate(ignoringOtherApps: true)
        }
        onboarding?.didAppear()
    }

    /// Esc 关窗。菜单栏 App 没有主菜单，⌘W 走不通，键盘用户只剩这条路。
    override func cancelOperation(_ sender: Any?) {
        close()
    }

    func windowWillClose(_ notification: Notification) {
        onboarding?.cancelPending()
        onClose?()
    }

    // MARK: - AppMain 推来的状态

    func update(status: WorkerStatus?, connectedSince: Date?) {
        state.status = status
        state.connectedSince = connectedSince
        render()
    }

    func update(snapshot: WorkerSettingsSnapshot) {
        state.snapshot = snapshot
        render()
    }

    func update(ffmpeg: FFmpegDownloadState, waitingForJobs: Bool) {
        state.ffmpeg = ffmpeg
        state.waitingForJobs = waitingForJobs
        render()
    }

    private func render() {
        onboarding?.render(state)
        panes.forEach { $0.render(state) }
    }

    // MARK: - 两种形态

    private func showOnboarding() {
        let controller = OnboardingViewController(state: state)
        controller.onSave = { [weak self] draft in try self?.onSave?(draft) }
        controller.onPaired = { [weak self] token in try self?.onPaired?(token) }
        controller.onFFmpegAction = { [weak self] action in self?.ffmpegAction(action) }
        controller.onFinish = { [weak self] in self?.showTabs(select: .connection) }
        controller.settings = self
        onboarding = controller
        panes = []
        tabs = nil

        guard let window else { return }
        window.toolbar = nil
        window.title = "连接到 movieclaw"
        // 透明标题栏，内容从红绿灯下方开始：引导每一步只讲一件事，不需要工具栏
        controller.titlebarInset = window.adoptGlassTitlebar()
        window.contentViewController = controller
    }

    private func showTabs(select tab: SettingsTab) {
        onboarding?.cancelPending()
        onboarding = nil
        let connection = ConnectionPaneController(settings: self)
        let transcode = TranscodePaneController(settings: self)
        let about = AboutPaneController(settings: self)
        panes = [connection, transcode, about]

        let tabs = SettingsTabsController()
        tabs.tabStyle = .toolbar
        // 不用系统的切页过渡：它会在过渡结束时按旧页面的尺寸把窗口再撑回去，
        // 和按内容调高度打架。窗口高度统一由 SettingsTabsController 调（带动画）
        tabs.transitionOptions = []
        for (controller, symbol) in [
            (connection as NSViewController, "network"),
            (transcode, "film.stack"),
            (about, "info.circle"),
        ] {
            let item = NSTabViewItem(viewController: controller)
            item.label = controller.title ?? ""
            item.image = NSImage(systemSymbolName: symbol, accessibilityDescription: controller.title)
            tabs.addTabViewItem(item)
        }
        tabs.selectedTabViewItemIndex = tab.rawValue
        self.tabs = tabs

        guard let window else { return }
        window.styleMask.remove(.fullSizeContentView)
        window.titlebarAppearsTransparent = false
        window.titleVisibility = .visible
        window.isMovableByWindowBackground = false
        if #available(macOS 11.0, *) {
            window.toolbarStyle = .preference
        }
        window.contentViewController = tabs
        render()
    }

    // MARK: - 页面共用的动作

    /// 页面改了一项设置。改动要重启 Worker、手上又有任务时先问一句。
    /// 返回是否真的保存了（没保存时页面回到原值）。
    @discardableResult
    func apply(restartsWorker: Bool, _ change: (inout WorkerSettingsDraft) -> Void) async -> Bool {
        var draft = WorkerSettingsDraft(state.snapshot)
        change(&draft)
        let busy = state.status?.activeJobs ?? 0
        if restartsWorker, busy > 0 {
            let alert = NSAlert()
            alert.messageText = "正在转码，现在应用会中断任务"
            alert.informativeText = "这项改动要重启转码服务才能生效，手上的 \(busy) 个任务会被中断。"
            alert.alertStyle = .warning
            alert.addButton(withTitle: "立即应用")
            alert.addButton(withTitle: "取消").keyEquivalent = "\u{1b}"
            guard await runSheet(alert) == .alertFirstButtonReturn else {
                render()
                return false
            }
        }
        do {
            try onSave?(draft)
            return true
        } catch {
            showError(error.localizedDescription)
            render()
            return false
        }
    }

    /// 稳态下唯一的「推倒重来」：清掉地址与令牌，回到引导第一步。
    func confirmAndClear() async {
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
            try onClear?()
            showOnboarding()
            render()
            onboarding?.didAppear()
        } catch {
            showError(error.localizedDescription)
        }
    }

    func ffmpegAction(_ action: FFmpegStatusView.Action, preferManaged: Bool = false) {
        switch action {
        case .start: onManageFFmpeg?(preferManaged)
        case .cancel: onCancelFFmpeg?()
        }
    }

    /// 窗口范围的提示一律用 sheet（从标题栏下滑、附着在本窗口上），不用独立弹窗。
    ///
    /// HIG 的分界是「这件事只关乎这个窗口，还是要打断整个 App」——这里每一处
    /// 都属于前者。菜单栏 App 尤其要守这条：没有 Dock 图标，app-modal 弹窗
    /// 可能落在用户找不到的层级上，表现成「点了按钮没反应」。
    func runSheet(_ alert: NSAlert) async -> NSApplication.ModalResponse {
        guard let window, window.isVisible else { return alert.runModal() }
        return await withCheckedContinuation { continuation in
            alert.beginSheetModal(for: window) { continuation.resume(returning: $0) }
        }
    }

    /// 只是通知一声、不需要等结果的提示。
    func showError(_ message: String, title: String = "无法继续") {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "好")
        guard let window, window.isVisible else {
            alert.runModal()
            return
        }
        alert.beginSheetModal(for: window, completionHandler: nil)
    }
}
