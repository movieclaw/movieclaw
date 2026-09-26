import SwiftUI

/// 全屏播放器（对应 Web `/play/{mediaItemId}/{sXXeYY}?t=`），由根视图 fullScreenCover 呈现。
///
/// 这一层只做「画面 + 控制层 + 手势」的组装与界面状态（控制条显隐、菜单、锁屏、横屏、亮度），
/// 会话协议、引擎编排、上报全在 `PlaybackController`。层级自下而上：
/// 引擎画面 → 文本字幕（AVPlayer）→ 亮度压暗 → 手势层 → 暂停遮罩/控制层 → 转圈/HUD/卡片/菜单 → 错误/同意弹窗。
struct PlayerScreen: View {
    let request: PlayRequest

    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase

    @State private var controller: PlaybackController?

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            if let controller {
                PlayerContent(controller: controller, exit: exit, openRemoteSettings: openRemoteSettings)
            }
        }
        // 强调色显式钉成冷银：玻璃强调按钮（重试、开启并播放、立即播放）不依赖呈现方传下来的 tint
        .tint(Theme.accentStrong)
        .statusBarHidden()
        .persistentSystemOverlays(.hidden)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("player-screen")
        .onAppear {
            guard controller == nil else { return }
            // 同一次播放的视图被系统重建（旋转等）：接回原控制器，不重开会话
            if let existing = router.activePlayback, existing.request.id == request.id {
                controller = existing
                #if DEBUG
                FileHandle.standardError.write(Data("[PlayerDiag] 播放器视图重建，复用原控制器\n".utf8))
                #endif
                return
            }
            let created = PlaybackController(request: request, api: api)
            controller = created
            router.activePlayback = created
            #if DEBUG
            // 开发期：-mcPlayerDiagnostics YES 起播即打开诊断面板（截图核对用）
            if UserDefaults.standard.bool(forKey: "mcPlayerDiagnostics") { created.diagnosticsOpen = true }
            // -mcAutoNextAfter 秒数：到点自动切下一集（排查切集问题用）
            let autoNext = UserDefaults.standard.double(forKey: "mcAutoNextAfter")
            if autoNext > 0 {
                Task {
                    try? await Task.sleep(for: .seconds(autoNext))
                    created.playNext()
                }
            }
            #endif
            created.start()
            UIApplication.shared.isIdleTimerDisabled = true
            #if DEBUG
            // 真机排查用：-mcAutoPiP <秒> 起播后到点自动点一次画中画（真机跑不了界面测试，靠它验证换引擎进画中画）
            let autoPiP = UserDefaults.standard.double(forKey: "mcAutoPiP")
            if autoPiP > 0 {
                Task {
                    try? await Task.sleep(for: .seconds(autoPiP))
                    created.togglePictureInPicture()
                }
            }
            // 真机排查用：-mcAutoLandscape <秒> 起播后自动切横屏；
            // 再加 -mcAutoRotate <次数> 则之后每 3 秒横竖交替，共转这么多次（测旋转耗时）
            let autoLandscape = UserDefaults.standard.double(forKey: "mcAutoLandscape")
            if autoLandscape > 0 {
                let rotations = max(1, UserDefaults.standard.integer(forKey: "mcAutoRotate"))
                Task {
                    try? await Task.sleep(for: .seconds(autoLandscape))
                    for index in 0 ..< rotations {
                        if index > 0 { try? await Task.sleep(for: .seconds(3)) }
                        PlayerOrientation.request(landscape: index % 2 == 0)
                    }
                }
            }
            #endif
        }
        .onDisappear {
            // 仍在呈现同一个播放请求：只是视图被重建，控制器与方向锁都保留
            if router.player?.id == request.id { return }
            finish()
        }
        .onChange(of: scenePhase) { _, phase in
            controller?.setBackgrounded(phase == .background)
        }
    }

    private func exit() {
        finish()
        dismiss()
    }

    /// 真正离开播放器：关会话、恢复亮度与常亮、解除方向锁
    private func finish() {
        controller?.close()
        if router.activePlayback === controller { router.activePlayback = nil }
        UIApplication.shared.isIdleTimerDisabled = false
        ScreenBrightness.restore()
        PlayerOrientation.release()
    }

    /// 同意弹窗里的「去设置远程转码」：关掉播放器再跳到设置的「播放」分区
    private func openRemoteSettings() {
        exit()
        router.open(.settingsSection(.playback))
    }
}

/// 播放器画面与控制层
private struct PlayerContent: View {
    let controller: PlaybackController
    let exit: () -> Void
    let openRemoteSettings: () -> Void

    @State private var chromeVisible = true
    @State private var chromeActivity = 0
    @State private var menu: PlayerMenu = .none
    @State private var locked = false
    @State private var lockHint = false
    @State private var lockHintTask: Task<Void, Never>?
    @State private var adjust: AdjustState?
    @State private var volumeUnsupported = false
    @State private var scrubMs: Int?
    @State private var scrubBase = 0
    @State private var scrubbingByGesture = false
    @State private var lastTapChromeState = true
    @State private var trickplay = TrickplayImages()
    /// 控制条与菜单在窗口坐标里的位置：交给手势层当「禁区」，落在这里的触摸只归按钮，
    /// 不再同时被当成「轻点画面」（否则一次点击既开菜单又收控制层/关菜单）
    @State private var topBarFrame: CGRect = .zero
    @State private var bottomBarFrame: CGRect = .zero
    @State private var menuFrame: CGRect = .zero
    @State private var lockButtonFrame: CGRect = .zero
    /// 中央三键的位置（窗口坐标）：判断菜单会不会压住它、诊断面板该停在哪
    @State private var centerFrame: CGRect = .zero

    struct AdjustState {
        var side: PlayerGestureLayer.AdjustSide
        var value: Double
        var base: Double
    }

    var body: some View {
        GeometryReader { proxy in
            let landscape = proxy.size.width > proxy.size.height
            ZStack {
                // 画面
                if let engine = controller.engine {
                    EngineSurface(engineView: engine.view)
                        .id(ObjectIdentifier(engine))
                        .ignoresSafeArea()
                        .accessibilityIdentifier("player-video")
                    // 文字字幕两个引擎都由叠加层用系统字体画（没有要画的字幕时地址为空、什么也不显示）
                    SubtitleOverlay(
                        url: controller.overlaySubtitleURL,
                        style: controller.subtitleStyle,
                        videoSize: engine.videoSize,
                        time: { Double(controller.originMs) / 1000 + (controller.engine?.currentTime ?? 0) },
                        session: controller.scope.api.session
                    )
                    .ignoresSafeArea()
                }

                PlayerGestureLayer(
                    enabled: !isModal,
                    canHold: controller.canHoldSpeed && !locked,
                    excludedRects: gestureExclusions,
                    onTap: handleTap,
                    onScrub: handleScrub,
                    onAdjust: handleAdjust,
                    onHold: handleHold
                )
                .ignoresSafeArea()

                if showPaused {
                    LinearGradient(colors: [.black.opacity(0.1), .black.opacity(0.55)], startPoint: .top, endPoint: .bottom)
                        .ignoresSafeArea()
                        .allowsHitTesting(false)
                }

                if locked {
                    lockOverlay
                } else {
                    chrome(landscape: landscape, height: proxy.size.height)
                }

                if controller.phase.isBusy {
                    // 与中央三键同一个中心（整屏正中），转圈结束时播放键正好接在原地
                    PlayerBusyView(controller: controller)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .ignoresSafeArea()
                }

                hud(landscape: landscape)

                if let notice = controller.notice {
                    VStack {
                        PlayerHUD { Text(notice) }
                            .padding(.top, hudTop(landscape: landscape))
                        Spacer()
                    }
                    .transition(.opacity)
                }

                if controller.phase == .error, let message = controller.errorMessage {
                    PlayerErrorView(message: message, suggestion: controller.errorSuggestion, retry: controller.retry, exit: exit)
                }
                if controller.phase == .consent, let decision = controller.pendingDecision {
                    PlayerConsentView(decision: decision, grant: controller.grantConsent, openRemoteSettings: openRemoteSettings, cancel: exit)
                }
                if let infoError = controller.infoError {
                    // 条目信息都拿不到（无权访问、已删除）：同 Web player-page 整页换成原因 +「返回」
                    PlayerInfoErrorView(message: infoError, exit: exit)
                }
                SystemVolumeHost().frame(width: 1, height: 1).allowsHitTesting(false)
            }
            .animation(.easeInOut(duration: 0.25), value: chromeVisible)
            .animation(.easeInOut(duration: 0.2), value: controller.notice)
        }
        .task(id: autoHideKey) {
            // 控制条 4 秒无操作自动隐藏；必须常显的情况（暂停、菜单、拖动、等用户拍板）直接钉住（同 Web chromeMustStayVisible）
            if !locked, chromeMustStayVisible {
                chromeVisible = true
                return
            }
            // 诊断面板开着不钉住控制层（同 Web chrome.ts：「诊断面板不在此列（曾经在）」）
            guard chromeVisible, !Self.debugPinChrome else { return }
            try? await Task.sleep(for: .seconds(4))
            if !Task.isCancelled { chromeVisible = false }
        }
    }

    private var isModal: Bool { controller.phase == .error || controller.phase == .consent }

    /// Debug：`-mcPlayerPinChrome YES` 让控制层不自动收起（UI 测试每一步都慢，
    /// 会跨过 4 秒自动收起，造成「刚确认按钮在、点下去时已隐藏」的竞态）。正式构建恒为 false
    private static var debugPinChrome: Bool {
        #if DEBUG
        UserDefaults.standard.bool(forKey: "mcPlayerPinChrome")
        #else
        false
        #endif
    }

    /// 手势层的禁区（窗口坐标）：控制层可见时的顶栏、底栏、横屏的锁屏键与打开着的菜单
    private var gestureExclusions: [CGRect] {
        guard chromeVisible, !locked else { return [] }
        var rects = [topBarFrame, bottomBarFrame, lockButtonFrame]
        if menu != .none { rects.append(menuFrame) }
        return rects.filter { !$0.isEmpty }
    }

    /// 顶部 HUD（亮度 / 音量 / 倍速 / 提示）的上缘：顶栏下方再隔一个间距，控制层开着时也不压住顶栏
    private func hudTop(landscape: Bool) -> CGFloat {
        PlayerLayout.topBarBottom(landscape: landscape) + PlayerLayout.gap
    }

    /// 菜单最高多高：底栏按钮行上缘往上 10pt，到顶栏下缘往下一个间距为止——横屏时菜单再高也不会顶到顶栏；
    /// 竖屏空间大，最多 460，免得一张菜单盖住大半个画面
    private var menuMaxHeight: CGFloat {
        guard !topBarFrame.isEmpty, !bottomBarFrame.isEmpty else { return 400 }
        return min(460, bottomBarFrame.minY - 10 - (topBarFrame.maxY + PlayerLayout.gap))
    }

    /// 菜单压住了中央三键：这时三键让位——只露出半截的玻璃圆钮既难看，露出来的那半截还会被误点。
    /// 菜单矮、够不着三键时（比如只有两条音轨）三键照常显示，与 Web 一致（对等审计 P-8 的「菜单会压住三键」例外）
    private var menuCoversCenter: Bool {
        menu != .none && !menuFrame.isEmpty && !centerFrame.isEmpty && menuFrame.intersects(centerFrame)
    }

    /// 诊断面板高度：竖屏停在中央三键上方（面板本来就按「不压住播放键」定的高）、横屏停在底栏按钮行上方，
    /// 各留一个间距；上限仍是原来的 300 / 240
    private func diagnosticsHeight(landscape: Bool) -> CGFloat {
        let cap: CGFloat = landscape ? 240 : 300
        let floor = landscape ? bottomBarFrame.minY : centerFrame.minY
        guard !topBarFrame.isEmpty, floor > 0 else { return cap }
        // 下限 120：窗口再矮（iPad 分屏）也不给出负高度
        return max(120, min(cap, floor - PlayerLayout.gap - (topBarFrame.maxY + PlayerLayout.gap)))
    }

    /// 控制条必须常显（对应 Web `lib/player/chrome.ts` chromeMustStayVisible）：
    /// 暂停时用户在找播放键；菜单是从控制条里长出来的；按着进度条就是在用它；报错/同意弹窗在等用户拍板。
    /// 这些情况下既不自动收起，轻点画面也收不起来。起播/缓冲转圈时的「暂停」是程序性的，不算
    private var chromeMustStayVisible: Bool {
        let userPaused = controller.paused && !controller.phase.isBusy && controller.session != nil
        return userPaused || menu != .none || scrubMs != nil || isModal
    }

    /// 暂停遮罩只跟「用户意图」走：缓冲饥饿、换流时的程序性暂停不压暗
    private var showPaused: Bool {
        controller.paused && !controller.wantsPlay && !controller.phase.isBusy && !isModal && controller.positionMs > 0
    }

    private var autoHideKey: String {
        "\(chromeVisible)-\(chromeMustStayVisible)-\(locked)-\(menu)-\(scrubMs == nil)-\(chromeActivity)"
    }

    // MARK: 控制层

    @ViewBuilder
    private func chrome(landscape: Bool, height: CGFloat) -> some View {
        ZStack {
            if chromeVisible {
                LinearGradient(colors: [.black.opacity(0.75), .clear], startPoint: .top, endPoint: .center)
                    .ignoresSafeArea()
                    .allowsHitTesting(false)
                LinearGradient(colors: [.clear, .black.opacity(0.7)], startPoint: .center, endPoint: .bottom)
                    .ignoresSafeArea()
                    .allowsHitTesting(false)
            }
            // 中央三键：控制层可见、不在转圈、不在等用户拍板时一直在（菜单打开时也在，同 Web video-player；
            // 只有菜单真压住它时才让位，见 menuCoversCenter）。
            // 对准整屏正中而不是安全区正中：画面按整屏居中，安全区上下不对称（竖屏顶上有灵动岛），
            // 按安全区摆会比画面中心低 17pt。拖进度时让位：画面要露出来，横滑的落点读数也正好在这个位置。
            // 摆在顶栏/底栏（含菜单）之下：菜单压住的部分点到的是菜单
            if chromeVisible, !controller.phase.isBusy, !isModal, scrubMs == nil, !menuCoversCenter {
                PlayerCenterControls(controller: controller)
                    .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { centerFrame = $0 }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .ignoresSafeArea()
                    .transition(.opacity)
            }
            if chromeVisible, landscape {
                lockButton(unlock: false)
                    .transition(.opacity)
            }
            VStack(spacing: 0) {
                if chromeVisible {
                    PlayerTopBar(controller: controller, landscape: landscape, onBack: { back(landscape: landscape) })
                        .padding(.horizontal, PlayerLayout.edge)
                        .padding(.top, PlayerLayout.topInset(landscape: landscape))
                        .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { topBarFrame = $0 }
                        .transition(.opacity)
                }
                if controller.diagnosticsOpen {
                    HStack {
                        PlayerDiagnosticsPanel(controller: controller, height: diagnosticsHeight(landscape: landscape)) { controller.diagnosticsOpen = false }
                        Spacer(minLength: 0)
                    }
                    .padding(.horizontal, PlayerLayout.edge)
                    .padding(.top, PlayerLayout.gap)
                }
                Spacer(minLength: 0)
                // 高度 ≤480 的横屏（手机横放）不显示片名大字，免得压住中央三键（同 Web）
                if showPaused, menu == .none, !(landscape && height <= 480) {
                    PausedOverlay(title: controller.title, episodeLabel: controller.episodeLabel(controller.currentEpisode))
                        .padding(.horizontal, PlayerLayout.edge)
                        .padding(.bottom, PlayerLayout.gap)
                }
                if controller.showsUpNext, let next = controller.nextEpisode {
                    HStack {
                        Spacer()
                        PlayerUpNextCard(
                            label: controller.episodeLabel(next) ?? "",
                            dismiss: { controller.nextDismissed = true },
                            play: controller.playNext
                        )
                    }
                    .padding(.horizontal, PlayerLayout.edge)
                    .padding(.bottom, PlayerLayout.gap)
                }
                if chromeVisible {
                    PlayerBottomBar(
                        controller: controller, trickplay: trickplay, menu: $menu, scrubMs: $scrubMs,
                        landscape: landscape, onToggleLandscape: { PlayerOrientation.request(landscape: !landscape) }
                    )
                    .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { bottomBarFrame = $0 }
                    // 菜单用 overlay 挂在底栏上：不参与布局（否则高菜单会把底栏挤扁），
                    // 底边贴在按钮组上方 10pt（对齐指南把菜单的「顶」换成它的底边），不压住进度条
                    .overlay(alignment: .topLeading) {
                        menuPanel
                            .environment(\.playerMenuMaxHeight, menuMaxHeight)
                            .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { menuFrame = $0 }
                            .alignmentGuide(.top) { $0[.bottom] + 10 }
                    }
                    .padding(.horizontal, PlayerLayout.edge)
                    .padding(.bottom, 8)
                    .transition(.opacity)
                }
            }
        }
        .onChange(of: menu) { chromeActivity += 1 }
    }

    /// 菜单从按下的那颗按钮处缩放长出、收回时缩回那里（近似系统菜单从按钮展开的观感）。
    /// 过渡挂在每种菜单自己身上：收起时 menu 已经是 .none，锚点得跟着被收起的那张菜单走
    private var menuPanel: some View {
        Group {
            switch menu {
            case .none: EmptyView()
            case .audio: AudioMenu(controller: controller) { menu = .none }.transition(menuTransition(.audio))
            case .subtitles: SubtitleMenu(controller: controller) { menu = .none }.transition(menuTransition(.subtitles))
            case .settings: SettingsMenu(controller: controller) { menu = .none }.transition(menuTransition(.settings))
            }
        }
        .animation(.spring(duration: 0.32, bounce: 0.18), value: menu)
    }

    /// 缩放锚点：菜单底边上、正对那颗按钮圆心的点（胶囊里的按钮各 44pt，从左数；没有音轨时少一颗）
    private func menuTransition(_ kind: PlayerMenu) -> AnyTransition {
        let hasAudio = !controller.audioOptions.isEmpty
        let index = switch kind {
        case .audio: 0
        case .subtitles: hasAudio ? 1 : 0
        default: hasAudio ? 2 : 1
        }
        let centerX = PlayerLayout.button * (CGFloat(index) + 0.5)
        return .scale(scale: 0.5, anchor: UnitPoint(x: centerX / PlayerLayout.menuWidth, y: 1)).combined(with: .opacity)
    }

    /// 锁屏：碰哪儿都不响应，点一下只唤出「解锁」键，3 秒后自己收起
    private var lockOverlay: some View {
        ZStack {
            Color.clear.contentShape(.rect).ignoresSafeArea().onTapGesture { revealLock() }
            if lockHint {
                lockButton(unlock: true)
                    .transition(.opacity)
            }
        }
        .animation(.easeInOut(duration: 0.2), value: lockHint)
    }

    /// 横屏的锁屏 / 解锁键，两者在同一个位置：在哪儿锁的就在哪儿解，不用满屏找。
    /// 左缘、竖直对准整屏中线（与中央三键同一水平线）——横握时左手拇指的落点；
    /// 横向留在安全区之内，离开灵动岛（灵动岛横屏时就在左右边缘的中段）
    private func lockButton(unlock: Bool) -> some View {
        HStack {
            if unlock {
                GlassIconButton(systemImage: "lock.open", label: "解锁", identifier: "player-unlock") {
                    locked = false
                    lockHint = false
                    chromeVisible = true
                }
            } else {
                GlassIconButton(systemImage: "lock", label: "锁屏", identifier: "player-lock") {
                    locked = true
                    menu = .none
                    revealLock()
                }
                .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { lockButtonFrame = $0 }
                // 键消失（转回竖屏、控制层收起）后禁区跟着撤掉，否则那块画面会一直点不动
                .onDisappear { lockButtonFrame = .zero }
            }
            Spacer(minLength: 0)
        }
        .padding(.leading, PlayerLayout.edge)
        .frame(maxHeight: .infinity)
        .ignoresSafeArea(edges: .vertical)
    }

    /// 唤出解锁键，3 秒后收起；再点一下重新计时（旧的倒计时作废，否则会提前把刚唤出的键收掉）
    private func revealLock() {
        lockHint = true
        lockHintTask?.cancel()
        lockHintTask = Task {
            try? await Task.sleep(for: .seconds(3))
            if !Task.isCancelled { lockHint = false }
        }
    }

    /// 横屏时左上角是「退出横屏」，竖屏时才是「退出播放」（同 Web 的后退语义）
    private func back(landscape: Bool) {
        if landscape {
            PlayerOrientation.request(landscape: false)
        } else {
            exit()
        }
    }

    // MARK: HUD

    @ViewBuilder
    private func hud(landscape: Bool) -> some View {
        VStack {
            if controller.holdSpeedActive {
                PlayerHUD {
                    Text("2× 快进中").monospacedDigit()
                    Image(systemName: "forward.fill")
                }
                .accessibilityIdentifier("player-hold-speed")
            } else if let adjust {
                PlayerHUD {
                    Image(systemName: adjust.side == .brightness ? "sun.max.fill" : (adjust.value <= 0.001 ? "speaker.slash.fill" : "speaker.wave.2.fill"))
                    if adjust.side == .volume, volumeUnsupported {
                        Text("音量由系统侧键控制")
                    } else {
                        LevelBar(value: adjust.value)
                    }
                }
            }
            Spacer()
        }
        .padding(.top, hudTop(landscape: landscape))
        .allowsHitTesting(false)
        if let scrubMs, scrubbingByGesture {
            // 横滑定位的落点读数放在眼睛看的地方：大字落点、小字相对起点的增量。
            // 与中央三键同一个中心（整屏正中），拖动时三键让位，读数正好接在播放键的位置
            PlayerHUD {
                VStack(spacing: 4) {
                    Text(Formatters.clock(Double(scrubMs) / 1000)).font(.title2.monospacedDigit().weight(.semibold))
                    let delta = (scrubMs - scrubBase) / 1000
                    Text("\(delta >= 0 ? "+" : "-")\(Formatters.clock(Double(abs(delta))))")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.white.opacity(0.7))
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .ignoresSafeArea()
        }
    }

    // MARK: 手势

    private func handleTap(_ xRatio: CGFloat, _ isDouble: Bool) {
        if menu != .none {
            menu = .none
            return
        }
        guard isDouble, controller.session != nil else {
            // 第一下永远是控制层开关，不为等双击而延迟；必须常显时（暂停等）只能唤出、收不起来
            lastTapChromeState = chromeVisible
            if chromeMustStayVisible {
                chromeVisible = true
            } else {
                chromeVisible.toggle()
            }
            chromeActivity += 1
            return
        }
        // 双击左右三分之一 = ∓10 秒；第一下切换过的控制层恢复原状，净效果只剩跳转
        if xRatio < 1 / 3 {
            chromeVisible = lastTapChromeState
            controller.seek(by: -10)
        } else if xRatio > 2 / 3 {
            chromeVisible = lastTapChromeState
            controller.seek(by: 10)
        } else if !chromeMustStayVisible {
            chromeVisible.toggle()
        }
    }

    /// 横滑定位：满屏一划 = 90 秒（同 Web FULL_SWEEP_SEEK_S），松手才跳
    private func handleScrub(_ phase: PlayerGestureLayer.GesturePhase, _ delta: CGFloat) {
        guard let duration = controller.durationMs else { return }
        switch phase {
        case .began:
            scrubBase = controller.positionMs
            scrubMs = scrubBase
            scrubbingByGesture = true
        case .changed:
            let target = min(max(0, scrubBase + Int(delta * 90_000)), duration)
            scrubMs = target
            controller.scrubFollow(toFileMs: target)
        case .ended:
            if let scrubMs { controller.seek(toFileMs: scrubMs) }
            scrubMs = nil
            scrubbingByGesture = false
        case .cancelled:
            scrubMs = nil
            scrubbingByGesture = false
        }
    }

    /// 竖滑：左半屏调系统屏幕亮度（退出播放器时恢复进入前的亮度），右半屏系统音量
    private func handleAdjust(_ phase: PlayerGestureLayer.GesturePhase, _ side: PlayerGestureLayer.AdjustSide, _ delta: CGFloat) {
        switch phase {
        case .began:
            let base = side == .brightness ? ScreenBrightness.current : Double(SystemVolume.shared.value)
            volumeUnsupported = side == .volume && !SystemVolume.shared.isAdjustable
            adjust = AdjustState(side: side, value: base, base: base)
        case .changed:
            guard var current = adjust else { return }
            let value = min(1, max(0, current.base + Double(delta)))
            if side == .brightness {
                ScreenBrightness.set(value)
            } else {
                SystemVolume.shared.set(Float(value))
            }
            current.value = value
            adjust = current
        case .ended, .cancelled:
            Task {
                try? await Task.sleep(for: .milliseconds(900))
                adjust = nil
            }
        }
    }

    /// 长按 2 倍速：按住期间加速，抬手恢复；缓冲跟不上由控制器自动退回
    private func handleHold(_ began: Bool) {
        if began {
            _ = controller.beginHoldSpeed()
        } else {
            controller.endHoldSpeed()
        }
    }
}
