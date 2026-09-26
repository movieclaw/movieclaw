import SwiftUI

/// 播放器控制层（对应 Web `components/player/player-controls.tsx`，布局照 iOS 26 系统播放器分三块）：
/// - 顶栏：退出（独立玻璃圆钮）/ 片名 / 实时吞吐 / 右侧一枚玻璃胶囊装隔空播放、画中画；
/// - 中部：后退 10 秒 / 播放暂停 / 前进 10 秒，三颗玻璃圆钮，播放键更大，对准画面（整屏）正中；
/// - 底部：左侧玻璃胶囊装音轨 / 字幕 / ⋯ 设置，右侧横屏圆钮；下面是进度条，已播 / 剩余时间紧贴在条下两端；
/// - 横屏另有锁屏键，在左缘竖直居中（拇指够得着），锁屏后解锁键出现在同一位置（见 PlayerScreen）。
///
/// 视觉统一走系统液态玻璃（`PlayerGlass`）：按钮压在画面上用透明玻璃叠薄黑、可交互（按下有系统的形变高光），
/// 菜单与弹窗承载文字用常规玻璃。同一组按钮合进一枚胶囊，而不是各自一块底——这是 iOS 26 的分组方式。
///
/// 菜单仍是自绘面板而不是系统 Menu：字幕菜单里要放时间轴/字号的步进器与开关，系统菜单放不下；
/// 面板外观向系统菜单看齐（玻璃底、大圆角、行首对勾表示选中、行尾放图标）。
enum PlayerMenu: Equatable {
    case none, audio, subtitles, settings
}

/// 控制层的尺寸与间距：横竖屏、各个浮层共用同一套数，保证边缘对齐、间隔一致
enum PlayerLayout {
    /// 控件离安全区左右边缘的距离；进度条、时间、各胶囊的外缘都对齐在这条线上
    static let edge: CGFloat = 16
    /// 图标按钮边长 = 系统最小触控尺寸
    static let button: CGFloat = 44
    /// 相邻区块的间隔（顶栏到 HUD、顶栏到菜单）
    static let gap: CGFloat = 12
    /// 音轨 / 字幕 / 设置菜单面板的宽度（菜单从按钮处长出的锚点按它换算）
    static let menuWidth: CGFloat = 290

    /// 顶栏离上缘：竖屏上方已有灵动岛安全区，再留 8 即可；横屏顶部没有安全区，留 16 与左右边距一致，
    /// 否则按钮几乎贴着屏幕上沿
    static func topInset(landscape: Bool) -> CGFloat { landscape ? 16 : 8 }

    /// 顶栏下缘（安全区坐标）：HUD、提示条、菜单的上限都从这里往下排，保证不压住顶栏
    static func topBarBottom(landscape: Bool) -> CGFloat { topInset(landscape: landscape) + button }
}

/// 播放器里所有液态玻璃的材质，集中在一处，改风格只动这里
enum PlayerGlass {
    /// 压在画面上的按钮：透明玻璃叠一层薄黑，亮画面上白色图标也看得清；
    /// 不用常规玻璃是因为小尺寸的常规玻璃会随背后画面明暗在深浅两态间翻转，白图标在浅态下看不见
    static let control = Glass.clear.tint(.black.opacity(0.32)).interactive()
    /// 菜单、弹窗、HUD、诊断面板：要承载成段文字，用常规玻璃并压暗，保证任何画面上都读得清
    static let panel = Glass.regular.tint(.black.opacity(0.35))
}

struct PlayerTopBar: View {
    let controller: PlaybackController
    let landscape: Bool
    let onBack: () -> Void

    var body: some View {
        GlassEffectContainer(spacing: 8) {
            HStack(spacing: PlayerLayout.gap) {
                // 返回箭头笔画细，用大一号的符号尺度，与胶囊里的图标视觉上一样重（系统返回键也是这样）
                GlassIconButton(
                    systemImage: "chevron.backward", label: landscape ? "退出横屏" : "退出播放",
                    identifier: "player-close", scale: .large, action: onBack
                )
                VStack(alignment: .leading, spacing: 2) {
                    Text(controller.title)
                        .font(.headline)
                        .foregroundStyle(.white)
                        .lineLimit(1)
                    if let label = controller.episodeLabel(controller.currentEpisode) {
                        Text(label)
                            .font(.footnote)
                            .foregroundStyle(.white.opacity(0.65))
                            .lineLimit(1)
                    }
                }
                .shadow(color: .black.opacity(0.4), radius: 6)
                Spacer(minLength: 8)
                if let speed = controller.speedLabel {
                    Text("↓ \(speed)")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.white.opacity(0.45))
                        .accessibilityIdentifier("player-speed")
                }
                // 右侧工具合成一枚胶囊（同 iOS 26 系统播放器右上角）；不加内边距，两端按钮与胶囊端头同心
                HStack(spacing: 0) {
                    AirPlayButton()
                        .frame(width: PlayerLayout.button, height: PlayerLayout.button)
                    // MPV 播放时也显示：点了会换成系统播放器再进画中画（见 PlaybackController.togglePictureInPicture）
                    if controller.pictureInPictureAvailable {
                        PlayerIconButton(
                            systemImage: controller.pipActive ? "pip.exit" : "pip.enter",
                            label: controller.pipActive ? "退出画中画" : "画中画",
                            identifier: "player-pip",
                            action: controller.togglePictureInPicture
                        )
                    }
                }
                .glassEffect(PlayerGlass.control, in: .capsule)
            }
        }
    }
}

struct PlayerCenterControls: View {
    let controller: PlaybackController
    /// 点击计数，只用来触发跳转图标的转动动效
    @State private var backTaps = 0
    @State private var forwardTaps = 0

    var body: some View {
        GlassEffectContainer(spacing: 8) {
            HStack(spacing: 36) {
                TransportButton(label: "后退 10 秒", size: 56, action: {
                    backTaps += 1
                    controller.seek(by: -10)
                }) {
                    Image(systemName: "gobackward.10")
                        .symbolEffect(.rotate.counterClockwise.byLayer, value: backTaps)
                }
                TransportButton(label: controller.paused ? "播放" : "暂停", size: 76, identifier: "player-play-pause", action: controller.togglePlay) {
                    // 播放 / 暂停两个图标之间用系统的符号替换动效过渡
                    Image(systemName: controller.paused ? "play.fill" : "pause.fill")
                        .contentTransition(.symbolEffect(.replace))
                }
                TransportButton(label: "前进 10 秒", size: 56, action: {
                    forwardTaps += 1
                    controller.seek(by: 10)
                }) {
                    Image(systemName: "goforward.10")
                        .symbolEffect(.rotate.clockwise.byLayer, value: forwardTaps)
                }
            }
        }
    }
}

/// 中央的玻璃圆钮：整颗圆都能点，按下由交互玻璃给出系统的形变反馈
private struct TransportButton<Icon: View>: View {
    let label: String
    let size: CGFloat
    var identifier: String?
    let action: () -> Void
    @ViewBuilder let icon: Icon

    var body: some View {
        Button(action: action) {
            icon
                .font(.system(size: size * 0.42, weight: .semibold))
                .foregroundStyle(.white)
                // 中央没有渐变压暗，画面最亮处图标靠一圈淡阴影托住
                .shadow(color: .black.opacity(0.35), radius: 3)
                .frame(width: size, height: size)
                .contentShape(.circle)
        }
        .buttonStyle(.plain)
        .glassEffect(PlayerGlass.control, in: .circle)
        .accessibilityLabel(label)
        .accessibilityIdentifier(identifier ?? label)
    }
}

/// 不带底的图标按钮，放进玻璃胶囊组里用（组共用一块玻璃）。
/// - `highlighted`：对应菜单正开着时垫一枚浅色圆，表示「从这里弹出的」；
/// - `pressFeedback`：按下时也垫这枚圆。组里必须开——组的交互玻璃只会让整枚胶囊一起动，分不清按的是哪颗；
///   独立玻璃圆钮关掉，由它自己的交互玻璃给反馈，免得两层反馈叠在一起。
struct PlayerIconButton: View {
    let systemImage: String
    let label: String
    var identifier: String?
    var highlighted = false
    var pressFeedback = true
    var scale: Image.Scale = .medium
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: systemImage)
                .font(.system(size: 17, weight: .semibold))
                .imageScale(scale)
                .foregroundStyle(.white)
                .contentTransition(.symbolEffect(.replace))
                .frame(width: PlayerLayout.button, height: PlayerLayout.button)
                // 无样式按钮只在不透明像素上响应点击：背景透明时只有图标笔画能点中，
                // 「⋯」只剩三个小圆点，真机上几乎点不到（用户反馈），必须显式给整块点击区域。
                // 用方形而不是圆形：胶囊里相邻两颗之间不留点不中的缝
                .contentShape(.rect)
        }
        .buttonStyle(PlayerIconButtonStyle(highlighted: highlighted, pressFeedback: pressFeedback))
        .accessibilityLabel(label)
        .accessibilityIdentifier(identifier ?? label)
    }
}

/// 图标按钮的浅色圆：比按钮内缩 4pt，放在胶囊两端时正好与胶囊端头同心
private struct PlayerIconButtonStyle: ButtonStyle {
    let highlighted: Bool
    let pressFeedback: Bool

    func makeBody(configuration: Configuration) -> some View {
        let pressed = pressFeedback && configuration.isPressed
        configuration.label
            .background {
                Circle()
                    .fill(.white.opacity(pressed ? 0.28 : 0.2))
                    .padding(4)
                    .opacity(pressed || highlighted ? 1 : 0)
            }
            .animation(.easeOut(duration: 0.15), value: pressed)
    }
}

/// 独立的圆形玻璃按钮（退出、横屏、锁屏 / 解锁）
struct GlassIconButton: View {
    let systemImage: String
    let label: String
    var identifier: String?
    var scale: Image.Scale = .medium
    let action: () -> Void

    var body: some View {
        PlayerIconButton(systemImage: systemImage, label: label, identifier: identifier, pressFeedback: false, scale: scale, action: action)
            .glassEffect(PlayerGlass.control, in: .circle)
    }
}

// MARK: - 底栏

struct PlayerBottomBar: View {
    let controller: PlaybackController
    let trickplay: TrickplayImages
    @Binding var menu: PlayerMenu
    /// 拖动中的覆盖位置（手势横滑或进度条拖动）
    @Binding var scrubMs: Int?
    let landscape: Bool
    let onToggleLandscape: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            GlassEffectContainer(spacing: 8) {
                HStack(spacing: PlayerLayout.gap) {
                    // 不加内边距：两端按钮与胶囊端头同心，按下的高亮圆正好嵌在端头里
                    HStack(spacing: 0) {
                        if !controller.audioOptions.isEmpty {
                            PlayerIconButton(systemImage: "waveform", label: "音轨", identifier: "player-音轨", highlighted: menu == .audio) { toggle(.audio) }
                        }
                        PlayerIconButton(
                            systemImage: controller.selectedSubtitle == nil ? "captions.bubble" : "captions.bubble.fill",
                            label: "字幕", identifier: "player-字幕", highlighted: menu == .subtitles
                        ) { toggle(.subtitles) }
                        PlayerIconButton(systemImage: "ellipsis", label: "设置", identifier: "player-设置", highlighted: menu == .settings) { toggle(.settings) }
                    }
                    .glassEffect(PlayerGlass.control, in: .capsule)
                    Spacer()
                    let rotateLabel = landscape ? "退出横屏" : "横屏"
                    GlassIconButton(
                        systemImage: landscape ? "rectangle.portrait.rotate" : "rectangle.landscape.rotate",
                        label: rotateLabel, identifier: "player-\(rotateLabel)", action: onToggleLandscape
                    )
                }
            }
            // 拖进度时让出这一行：预览缩略图正好浮在这里，按钮留着只会和它叠成一团
            .opacity(scrubMs == nil ? 1 : 0)
            .allowsHitTesting(scrubMs == nil)
            .animation(.easeOut(duration: 0.2), value: scrubMs == nil)
            // 进度条的触控带（44pt）紧接按钮行：细条离胶囊下缘 20pt，两块触控区首尾相接、互不重叠
            PlayerProgressBar(controller: controller, trickplay: trickplay, scrubMs: $scrubMs)
            // 已播放在左、剩余在右，紧贴细条下方 8pt（同系统播放器）。
            // 时间行叠进触控带的下半截，所以不接收触摸：按在时间上也是在拖进度
            HStack {
                Text(Formatters.clock(Double(position) / 1000))
                    .accessibilityIdentifier("player-time")
                    .accessibilityValue(String(controller.positionMs / 1000))
                Spacer()
                Text(remainingText)
            }
            .font(.caption.monospacedDigit())
            .foregroundStyle(.white.opacity(0.85))
            .padding(.top, -PlayerProgressBar.labelOverlap)
            .allowsHitTesting(false)
        }
    }

    private var position: Int { scrubMs ?? controller.positionMs }

    /// 剩余时长；总时长未知时显示占位
    private var remainingText: String {
        guard let duration = controller.durationMs else { return Formatters.clock(nil) }
        return "-" + Formatters.clock(Double(max(0, duration - position)) / 1000)
    }

    private func toggle(_ target: PlayerMenu) {
        menu = menu == target ? .none : target
    }
}

/// 进度条：文件时间轴，已缓冲区、章节刻度；拖动时上方浮出缩略图与落点时间。
/// 跳转便宜（落点在缓冲里 / 原文件直出停住时）拖动途中画面就跟过去，松手再精确落地。
///
/// 细条只有 4pt，但整条带子 44pt 高都能按（系统最小触控尺寸），细条在带子正中。
/// 圆点在条内滑动（同 UISlider）：落点 0 时圆点左缘与条左缘齐平，不探出左右那条对齐线。
struct PlayerProgressBar: View {
    let controller: PlaybackController
    let trickplay: TrickplayImages
    @Binding var scrubMs: Int?
    @State private var dragging = false

    /// 触控带高度
    static let touchHeight: CGFloat = 44
    /// 下方时间行叠进触控带的深度：时间贴在细条下方 8pt，而不是被触控带推到 20pt 开外
    static let labelOverlap: CGFloat = 12
    /// 静止时圆点的半径，也是两端让出的行程
    private static let knobRadius: CGFloat = 6
    /// 拖动预览底边离细条中线的高度：给按在条上的手指指尖留出空隙，预览不被手指挡住
    private static let previewLift: CGFloat = 28

    var body: some View {
        GeometryReader { proxy in
            let width = proxy.size.width
            let duration = Double(controller.durationMs ?? 0)
            let position = Double(scrubMs ?? controller.positionMs)
            let ratio = duration > 0 ? min(1, max(0, position / duration)) : 0
            let buffered = duration > 0 ? min(1, max(0, Double(controller.bufferedEndMs ?? 0) / duration)) : 0
            // 时间轴比例 ↔ 横坐标：两端各让出一个圆点半径
            let travel = max(1, width - 2 * Self.knobRadius)
            let x: (Double) -> CGFloat = { Self.knobRadius + travel * $0 }
            let track: CGFloat = dragging ? 6 : 4
            ZStack(alignment: .leading) {
                Capsule().fill(.white.opacity(0.22)).frame(height: track)
                Capsule().fill(.white.opacity(0.35)).frame(width: x(buffered), height: track)
                Capsule().fill(Theme.accentStrong).frame(width: x(ratio), height: track)
                // 章节刻度（合成章节服务端不下发）
                ForEach(controller.session?.chapters ?? [], id: \.startMs) { chapter in
                    if duration > 0, chapter.startMs > 0 {
                        Rectangle()
                            .fill(.black.opacity(0.7))
                            .frame(width: 2, height: track)
                            .offset(x: x(Double(chapter.startMs) / duration) - 1)
                    }
                }
                Circle()
                    .fill(.white)
                    .frame(width: dragging ? 18 : 12, height: dragging ? 18 : 12)
                    .offset(x: x(ratio) - (dragging ? 9 : 6))
                    .shadow(radius: 2)
            }
            .frame(height: Self.touchHeight)
            // 拖动预览挂在 overlay 上、不参与排版：放进上面的 ZStack 会被 44pt 高的触控带压扁（真机上缩略图只剩 35pt 宽），
            // 按中心往上挪又会在它被压矮后浮在半空、离圆点六十多 pt。改成按自然尺寸摆：底边离细条中线 previewLift，
            // 水平正对圆点，到两头收在进度条内。拖动时上面那排按钮会淡出，预览正好占那块地方。
            // 定位靠一个零尺寸锚点：锚点放在预览底边的中点，预览按底边居中贴着它往上长。不用自定义对齐参考线——
            // 写在 if 里时参考线会被忽略，预览退回顶对齐、整个掉到进度条下面（真机踩过，macOS 离屏渲染同样复现）
            .overlay(alignment: .topLeading) {
                Color.clear
                    .frame(width: 0, height: 0)
                    .overlay(alignment: .bottom) {
                        if dragging, let scrubMs {
                            ScrubPreview(controller: controller, trickplay: trickplay, fileMs: scrubMs)
                                .fixedSize()
                        }
                    }
                    .offset(
                        x: min(max(ScrubPreview.width / 2, x(ratio)), width - ScrubPreview.width / 2),
                        y: Self.touchHeight / 2 - Self.previewLift
                    )
            }
            .contentShape(.rect)
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { value in
                        guard duration > 0 else { return }
                        dragging = true
                        let target = Int(min(1, max(0, (value.location.x - Self.knobRadius) / travel)) * duration)
                        scrubMs = target
                        // 跳转便宜时画面跟着手指走（节奏见 ScrubFollow），松手再精确落地
                        controller.scrubFollow(toFileMs: target)
                    }
                    .onEnded { _ in
                        if let target = scrubMs { controller.seek(toFileMs: target) }
                        dragging = false
                        scrubMs = nil
                    }
            )
            .accessibilityElement()
            .accessibilityLabel("播放进度")
            .accessibilityIdentifier("player-progress")
            .accessibilityValue(Formatters.clock(position / 1000))
        }
        .frame(height: Self.touchHeight)
    }
}

/// 拖动预览：缩略图（有索引时）+ 落点时间 + 章节名
struct ScrubPreview: View {
    let controller: PlaybackController
    let trickplay: TrickplayImages
    let fileMs: Int

    /// 预览宽度（缩略图按它等比缩放）；进度条按它把预览对准圆点
    static let width: CGFloat = 160

    var body: some View {
        VStack(spacing: 4) {
            if let image = trickplay.tile(controller.trickplay, atMs: fileMs, resolve: controller.scope.streamURL, session: controller.scope.api.session) {
                Image(uiImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fit)
                    .frame(width: Self.width)
                    .clipShape(.rect(cornerRadius: 8))
            }
            Text(label)
                .font(.caption.monospacedDigit().weight(.semibold))
                .foregroundStyle(.white)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .glassEffect(PlayerGlass.panel, in: .capsule)
        }
        .frame(width: Self.width)
        .allowsHitTesting(false)
    }

    private var label: String {
        let clock = Formatters.clock(Double(fileMs) / 1000)
        let chapter = controller.session?.chapters.last { $0.startMs <= fileMs }?.title
        return chapter.map { "\(clock) · \($0)" } ?? clock
    }
}
