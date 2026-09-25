import SwiftUI

/// 播放器控制层（对应 Web `components/player/player-controls.tsx`，布局照 YouTube 分三块）：
/// - 顶栏：退出 / 片名 / 实时吞吐 / 锁屏（横屏）/ 隔空播放 / 画中画；
/// - 中部：后退 10 秒 / 播放暂停 / 前进 10 秒；
/// - 底部：进度条（缓冲区、章节刻度、拖动时的缩略图预览）+ 时间 + 音轨 / 字幕 / ⋯ 设置 / 横屏。
///
/// 菜单用自绘的深色面板而不是系统 Menu：字幕菜单里要放时间轴/字号的步进器与开关，
/// 系统菜单放不下；外观与网页的菜单同一套语言（半透明黑、行尾对勾表示选中）。
enum PlayerMenu: Equatable {
    case none, audio, subtitles, settings
}

struct PlayerTopBar: View {
    let controller: PlaybackController
    let landscape: Bool
    let onBack: () -> Void
    let onLock: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            GlassIconButton(systemImage: "chevron.left", label: landscape ? "退出横屏" : "退出播放", identifier: "player-close", action: onBack)
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
            Spacer(minLength: 8)
            if let speed = controller.speedLabel {
                Text("↓ \(speed)")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.white.opacity(0.45))
                    .accessibilityIdentifier("player-speed")
            }
            if landscape {
                GlassIconButton(systemImage: "lock", label: "锁屏", identifier: "player-lock", action: onLock)
            }
            AirPlayButton()
                .frame(width: 40, height: 40)
                .background(.black.opacity(0.3), in: .circle)
            if controller.engine?.supportsPictureInPicture == true {
                GlassIconButton(
                    systemImage: controller.pipActive ? "pip.exit" : "pip.enter",
                    label: controller.pipActive ? "退出画中画" : "画中画",
                    identifier: "player-pip",
                    action: controller.togglePictureInPicture
                )
            }
        }
    }
}

struct PlayerCenterControls: View {
    let controller: PlaybackController

    var body: some View {
        HStack(spacing: 44) {
            CenterButton(systemImage: "gobackward.10", label: "后退 10 秒", size: 44) { controller.seek(by: -10) }
            CenterButton(
                systemImage: controller.paused ? "play.fill" : "pause.fill",
                label: controller.paused ? "播放" : "暂停",
                size: 60,
                identifier: "player-play-pause"
            ) { controller.togglePlay() }
            CenterButton(systemImage: "goforward.10", label: "前进 10 秒", size: 44) { controller.seek(by: 10) }
        }
    }
}

private struct CenterButton: View {
    let systemImage: String
    let label: String
    let size: CGFloat
    var identifier: String?
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: systemImage)
                .font(.system(size: size * 0.5, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: size, height: size)
                .contentShape(.circle)
                .shadow(color: .black.opacity(0.6), radius: 8, y: 2)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityIdentifier(identifier ?? label)
    }
}

/// 顶栏的圆形玻璃按钮（与站内返回键同形制）
struct GlassIconButton: View {
    let systemImage: String
    let label: String
    var identifier: String?
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: systemImage)
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(.white.opacity(0.9))
                .frame(width: 40, height: 40)
                .background(.black.opacity(0.3), in: .circle)
                .overlay(Circle().strokeBorder(.white.opacity(0.09)))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityIdentifier(identifier ?? label)
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
        VStack(spacing: 10) {
            HStack(alignment: .center, spacing: 12) {
                Text(timeText)
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.white.opacity(0.85))
                    .accessibilityIdentifier("player-time")
                    .accessibilityValue("\(controller.positionMs / 1000)")
                Spacer()
            }
            PlayerProgressBar(controller: controller, trickplay: trickplay, scrubMs: $scrubMs)
            HStack(spacing: 4) {
                HStack(spacing: 2) {
                    if !controller.audioOptions.isEmpty {
                        BarButton(systemImage: "waveform", label: "音轨", open: menu == .audio) { toggle(.audio) }
                    }
                    BarButton(
                        systemImage: controller.selectedSubtitle == nil ? "captions.bubble" : "captions.bubble.fill",
                        label: "字幕", open: menu == .subtitles
                    ) { toggle(.subtitles) }
                    BarButton(systemImage: "ellipsis", label: "设置", open: menu == .settings) { toggle(.settings) }
                }
                .padding(.horizontal, 6)
                .padding(.vertical, 4)
                .glassEffect(.clear, in: .capsule)
                Spacer()
                HStack(spacing: 2) {
                    BarButton(
                        systemImage: landscape ? "rectangle.portrait.rotate" : "rectangle.landscape.rotate",
                        label: landscape ? "退出横屏" : "横屏", open: false, action: onToggleLandscape
                    )
                }
                .padding(.horizontal, 6)
                .padding(.vertical, 4)
                .glassEffect(.clear, in: .capsule)
            }
        }
    }

    private var timeText: String {
        let position = scrubMs ?? controller.positionMs
        return "\(Formatters.clock(Double(position) / 1000)) / \(Formatters.clock(controller.durationMs.map { Double($0) / 1000 }))"
    }

    private func toggle(_ target: PlayerMenu) {
        menu = menu == target ? .none : target
    }
}

private struct BarButton: View {
    let systemImage: String
    let label: String
    let open: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: systemImage)
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 42, height: 38)
                .background(open ? Color.white.opacity(0.15) : .clear, in: .capsule)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityIdentifier("player-\(label)")
    }
}

/// 进度条：文件时间轴，已缓冲区、章节刻度；拖动时上方浮出缩略图与落点时间，松手才跳转。
struct PlayerProgressBar: View {
    let controller: PlaybackController
    let trickplay: TrickplayImages
    @Binding var scrubMs: Int?
    @State private var dragging = false

    var body: some View {
        GeometryReader { proxy in
            let width = proxy.size.width
            let duration = Double(controller.durationMs ?? 0)
            let position = Double(scrubMs ?? controller.positionMs)
            let ratio = duration > 0 ? min(1, max(0, position / duration)) : 0
            let buffered = duration > 0 ? min(1, max(0, Double(controller.bufferedEndMs ?? 0) / duration)) : 0
            ZStack(alignment: .leading) {
                Capsule().fill(.white.opacity(0.22)).frame(height: dragging ? 6 : 4)
                Capsule().fill(.white.opacity(0.35)).frame(width: width * buffered, height: dragging ? 6 : 4)
                Capsule().fill(Theme.accentStrong).frame(width: width * ratio, height: dragging ? 6 : 4)
                // 章节刻度（合成章节服务端不下发）
                ForEach(controller.session?.chapters ?? [], id: \.startMs) { chapter in
                    if duration > 0, chapter.startMs > 0 {
                        Rectangle()
                            .fill(.black.opacity(0.7))
                            .frame(width: 2, height: dragging ? 6 : 4)
                            .offset(x: width * Double(chapter.startMs) / duration - 1)
                    }
                }
                Circle()
                    .fill(.white)
                    .frame(width: dragging ? 18 : 12, height: dragging ? 18 : 12)
                    .offset(x: width * ratio - (dragging ? 9 : 6))
                    .shadow(radius: 2)
                if dragging, let scrubMs {
                    ScrubPreview(controller: controller, trickplay: trickplay, fileMs: scrubMs)
                        .offset(x: min(max(0, width * ratio - 80), width - 160), y: -86)
                }
            }
            .frame(height: 24)
            .contentShape(.rect)
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { value in
                        guard duration > 0 else { return }
                        dragging = true
                        scrubMs = Int(min(1, max(0, value.location.x / width)) * duration)
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
        .frame(height: 24)
    }
}

/// 拖动预览：缩略图（有索引时）+ 落点时间 + 章节名
struct ScrubPreview: View {
    let controller: PlaybackController
    let trickplay: TrickplayImages
    let fileMs: Int

    var body: some View {
        VStack(spacing: 4) {
            if let image = trickplay.tile(controller.trickplay, atMs: fileMs, resolve: controller.scope.streamURL, session: controller.scope.api.session) {
                Image(uiImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fit)
                    .frame(width: 160)
                    .clipShape(.rect(cornerRadius: 8))
            }
            Text(label)
                .font(.caption.monospacedDigit().weight(.semibold))
                .foregroundStyle(.white)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(.black.opacity(0.7), in: .capsule)
        }
        .frame(width: 160)
        .allowsHitTesting(false)
    }

    private var label: String {
        let clock = Formatters.clock(Double(fileMs) / 1000)
        let chapter = controller.session?.chapters.last { $0.startMs <= fileMs }?.title
        return chapter.map { "\(clock) · \($0)" } ?? clock
    }
}
