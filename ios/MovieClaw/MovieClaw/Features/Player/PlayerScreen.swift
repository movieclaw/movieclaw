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
        .statusBarHidden()
        .persistentSystemOverlays(.hidden)
        .accessibilityIdentifier("player-screen")
        .onAppear {
            guard controller == nil else { return }
            let created = PlaybackController(request: Self.resolved(request), api: api)
            controller = created
            #if DEBUG
            // 开发期：-mcPlayerDiagnostics YES 起播即打开诊断面板（截图核对用）
            if UserDefaults.standard.bool(forKey: "mcPlayerDiagnostics") { created.diagnosticsOpen = true }
            #endif
            created.start()
            UIApplication.shared.isIdleTimerDisabled = true
        }
        .onDisappear {
            controller?.close()
            UIApplication.shared.isIdleTimerDisabled = false
        }
        .onChange(of: scenePhase) { _, phase in
            controller?.setBackgrounded(phase == .background)
        }
    }

    private func exit() {
        controller?.close()
        PlayerOrientation.request(landscape: false)
        dismiss()
    }

    /// 同意弹窗里的「去设置远程转码」：关掉播放器再跳到设置的「播放」分区
    private func openRemoteSettings() {
        exit()
        router.open(.settingsSection(.playback))
    }

    /// Debug：`-mcRoute /play/{id}/s01e02?t=30` 的季集与起播秒数（根视图只解析了条目 id）
    private static func resolved(_ request: PlayRequest) -> PlayRequest {
        #if DEBUG
        guard request.season == nil, request.startSeconds == nil, let route = DebugLaunch.route,
              let components = URLComponents(string: route) else { return request }
        let parts = components.path.split(separator: "/").map(String.init)
        guard parts.count >= 2, parts[0] == "play", Int(parts[1]) == request.mediaItemId else { return request }
        var resolved = request
        if parts.count >= 3, let match = parts[2].lowercased().wholeMatch(of: /s(\d+)e(\d+)/) {
            resolved.season = Int(match.1)
            resolved.episode = Int(match.2)
        }
        if let t = components.queryItems?.first(where: { $0.name == "t" })?.value.flatMap(Double.init) {
            resolved.startSeconds = t
        }
        return resolved
        #else
        return request
        #endif
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
    @State private var brightness = 1.0
    @State private var adjust: AdjustState?
    @State private var volumeUnsupported = false
    @State private var scrubMs: Int?
    @State private var scrubBase = 0
    @State private var scrubbingByGesture = false
    @State private var lastTapChromeState = true
    @State private var trickplay = TrickplayImages()

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
                    if !engine.rendersSubtitles {
                        SubtitleOverlay(
                            url: controller.overlaySubtitleURL,
                            style: controller.subtitleStyle,
                            videoSize: engine.videoSize,
                            time: { Double(controller.originMs) / 1000 + (controller.engine?.currentTime ?? 0) },
                            session: controller.scope.api.session
                        )
                        .ignoresSafeArea()
                    }
                }
                // 亮度：压暗蒙层（0.1~1，同 Web；不改系统亮度，退出即复原）
                Color.black.opacity(1 - brightness).ignoresSafeArea().allowsHitTesting(false)

                PlayerGestureLayer(
                    enabled: !isModal,
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
                    chrome(landscape: landscape)
                }

                if controller.phase.isBusy {
                    PlayerBusyView(controller: controller)
                }

                hud

                if let notice = controller.notice {
                    VStack {
                        PlayerHUD { Text(notice) }
                            .padding(.top, 70)
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
                SystemVolumeHost().frame(width: 1, height: 1).allowsHitTesting(false)
            }
            .animation(.easeInOut(duration: 0.25), value: chromeVisible)
            .animation(.easeInOut(duration: 0.2), value: controller.notice)
        }
        .task(id: autoHideKey) {
            // 控制条 4 秒无操作自动隐藏（暂停、菜单展开、拖动中、诊断打开时不隐藏）
            guard chromeVisible, !controller.paused, menu == .none, scrubMs == nil, !controller.diagnosticsOpen else { return }
            try? await Task.sleep(for: .seconds(4))
            if !Task.isCancelled { chromeVisible = false }
        }
    }

    private var isModal: Bool { controller.phase == .error || controller.phase == .consent }

    /// 暂停遮罩只跟「用户意图」走：缓冲饥饿、换流时的程序性暂停不压暗
    private var showPaused: Bool {
        controller.paused && !controller.wantsPlay && !controller.phase.isBusy && !isModal && controller.positionMs > 0
    }

    private var autoHideKey: String {
        "\(chromeVisible)-\(controller.paused)-\(menu)-\(scrubMs == nil)-\(chromeActivity)"
    }

    // MARK: 控制层

    @ViewBuilder
    private func chrome(landscape: Bool) -> some View {
        ZStack {
            if chromeVisible {
                LinearGradient(colors: [.black.opacity(0.75), .clear], startPoint: .top, endPoint: .center)
                    .ignoresSafeArea()
                    .allowsHitTesting(false)
                LinearGradient(colors: [.clear, .black.opacity(0.7)], startPoint: .center, endPoint: .bottom)
                    .ignoresSafeArea()
                    .allowsHitTesting(false)
            }
            VStack(spacing: 0) {
                if chromeVisible {
                    PlayerTopBar(controller: controller, landscape: landscape, onBack: { back(landscape: landscape) }, onLock: {
                        locked = true
                        menu = .none
                        revealLock()
                    })
                    .padding(.horizontal, 16)
                    .padding(.top, 8)
                    .transition(.opacity)
                }
                if controller.diagnosticsOpen {
                    HStack {
                        PlayerDiagnosticsPanel(controller: controller, height: landscape ? 240 : 300) { controller.diagnosticsOpen = false }
                        Spacer(minLength: 0)
                    }
                    .padding(.horizontal, 16)
                    .padding(.top, 8)
                }
                Spacer(minLength: 0)
                if showPaused {
                    PausedOverlay(title: controller.title, episodeLabel: controller.episodeLabel(controller.currentEpisode))
                        .padding(.horizontal, 20)
                        .padding(.bottom, 12)
                }
                if controller.showsUpNext, let next = controller.nextEpisode {
                    HStack {
                        Spacer()
                        UpNextCard(
                            label: controller.episodeLabel(next) ?? "",
                            dismiss: { controller.nextDismissed = true },
                            play: controller.playNext
                        )
                    }
                    .padding(.horizontal, 16)
                    .padding(.bottom, 12)
                }
                if chromeVisible {
                    ZStack(alignment: .bottomLeading) {
                        PlayerBottomBar(
                            controller: controller, trickplay: trickplay, menu: $menu, scrubMs: $scrubMs,
                            landscape: landscape, onToggleLandscape: { PlayerOrientation.request(landscape: !landscape) }
                        )
                        menuPanel
                            .padding(.bottom, 64)
                    }
                    .padding(.horizontal, 16)
                    .padding(.bottom, 8)
                    .transition(.opacity)
                }
            }
            if chromeVisible, !controller.phase.isBusy, !isModal {
                PlayerCenterControls(controller: controller)
                    .transition(.opacity)
            }
        }
        .onChange(of: menu) { chromeActivity += 1 }
    }

    @ViewBuilder
    private var menuPanel: some View {
        switch menu {
        case .none: EmptyView()
        case .audio: AudioMenu(controller: controller) { menu = .none }
        case .subtitles: SubtitleMenu(controller: controller) { menu = .none }
        case .settings: SettingsMenu(controller: controller) { menu = .none }
        }
    }

    /// 锁屏：碰哪儿都不响应，点一下只唤出「解锁」键，3 秒后自己收起
    private var lockOverlay: some View {
        ZStack(alignment: .leading) {
            Color.clear.contentShape(.rect).onTapGesture { revealLock() }
            if lockHint {
                GlassIconButton(systemImage: "lock.open", label: "解锁", identifier: "player-unlock") {
                    locked = false
                    lockHint = false
                    chromeVisible = true
                }
                .padding(.leading, 40)
                .transition(.opacity)
            }
        }
        .ignoresSafeArea()
        .animation(.easeInOut(duration: 0.2), value: lockHint)
    }

    private func revealLock() {
        lockHint = true
        Task {
            try? await Task.sleep(for: .seconds(3))
            lockHint = false
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
    private var hud: some View {
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
        .padding(.top, 16)
        .allowsHitTesting(false)
        if let scrubMs, scrubbingByGesture {
            // 横滑定位的落点读数放在眼睛看的地方：大字落点、小字相对起点的增量
            PlayerHUD {
                VStack(spacing: 4) {
                    Text(Formatters.clock(Double(scrubMs) / 1000)).font(.title2.monospacedDigit().weight(.semibold))
                    let delta = (scrubMs - scrubBase) / 1000
                    Text("\(delta >= 0 ? "+" : "-")\(Formatters.clock(Double(abs(delta))))")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.white.opacity(0.7))
                }
            }
        }
    }

    // MARK: 手势

    private func handleTap(_ xRatio: CGFloat, _ isDouble: Bool) {
        if menu != .none {
            menu = .none
            return
        }
        guard isDouble, controller.session != nil else {
            // 第一下永远是控制层开关，不为等双击而延迟
            lastTapChromeState = chromeVisible
            chromeVisible.toggle()
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
        } else {
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
            scrubMs = min(max(0, scrubBase + Int(delta * 90_000)), duration)
        case .ended:
            if let scrubMs { controller.seek(toFileMs: scrubMs) }
            scrubMs = nil
            scrubbingByGesture = false
        case .cancelled:
            scrubMs = nil
            scrubbingByGesture = false
        }
    }

    /// 竖滑：左半屏亮度（压暗蒙层 0.1~1），右半屏系统音量
    private func handleAdjust(_ phase: PlayerGestureLayer.GesturePhase, _ side: PlayerGestureLayer.AdjustSide, _ delta: CGFloat) {
        switch phase {
        case .began:
            let base = side == .brightness ? brightness : Double(SystemVolume.shared.value)
            volumeUnsupported = side == .volume && !SystemVolume.shared.isAdjustable
            adjust = AdjustState(side: side, value: base, base: base)
        case .changed:
            guard var current = adjust else { return }
            let value = min(1, max(side == .brightness ? 0.1 : 0, current.base + Double(delta)))
            if side == .brightness {
                brightness = value
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
