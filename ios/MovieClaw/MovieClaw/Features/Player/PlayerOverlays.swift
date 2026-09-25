import SwiftUI

/// 转圈：起播/降档/缓冲，说清楚卡在哪一段，并附实测取流速度（贴着码率跑说明线路没问题、卡的是转码）
struct PlayerBusyView: View {
    let controller: PlaybackController

    var body: some View {
        VStack(spacing: 14) {
            ProgressView()
                .controlSize(.large)
                .tint(.white)
            Text(controller.phase.busyLabel)
                .font(.subheadline)
                .foregroundStyle(.white.opacity(0.75))
            if let speed = controller.speedLabel {
                Text("↓ \(speed)").font(.caption.monospacedDigit()).foregroundStyle(.white.opacity(0.45))
            }
        }
        .allowsHitTesting(false)
        .accessibilityIdentifier("player-busy")
    }
}

/// 错误页：后端中文原因 + 建议 + 重试 / 返回
struct PlayerErrorView: View {
    let message: String
    let suggestion: String?
    let retry: () -> Void
    let exit: () -> Void

    var body: some View {
        ZStack {
            Color.black.opacity(0.85).ignoresSafeArea()
            VStack(spacing: 12) {
                Text(message)
                    .font(.headline)
                    .foregroundStyle(.white)
                    .multilineTextAlignment(.center)
                if let suggestion {
                    Text(suggestion)
                        .font(.subheadline)
                        .foregroundStyle(.white.opacity(0.6))
                        .multilineTextAlignment(.center)
                }
                HStack(spacing: 12) {
                    Button("重试", action: retry)
                        .buttonStyle(PlayerPrimaryButtonStyle())
                        .accessibilityIdentifier("player-retry")
                    Button("返回", action: exit)
                        .buttonStyle(.glass)
                }
                .padding(.top, 12)
            }
            .frame(maxWidth: 480)
            .padding(24)
        }
        .accessibilityIdentifier("player-error")
    }
}

/// 软件转码同意弹窗（对应 Web consent-dialog.tsx，docs/design/web-player.md §3.6）。
///
/// 保存粒度是全局开关，没有「仅本次允许」；普通成员看到的是说明而不是按钮（全局设置只有超管能改，
/// 给一个点了必然 403 的按钮比不给更糟）。
struct PlayerConsentView: View {
    let decision: API.PlaybackDecisionView
    let grant: () async throws -> Void
    let openRemoteSettings: () -> Void
    let cancel: () -> Void

    @State private var saving = false
    @State private var error: String?

    var body: some View {
        ZStack {
            Color.black.opacity(0.75).ignoresSafeArea()
            VStack(alignment: .leading, spacing: 14) {
                Text("这部片需要软件转码才能播放")
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(.white)
                VStack(alignment: .leading, spacing: 10) {
                    labeled("原因", decision.reason)
                    if let cost = decision.costHint { labeled("代价", cost) }
                }
                if let error {
                    Text(error)
                        .font(.footnote)
                        .foregroundStyle(.white)
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Theme.danger.opacity(0.25), in: .rect(cornerRadius: 12))
                }
                if decision.canSelfEnable == true {
                    // 看到这个弹窗的一刻正是最需要远程硬件转码的时候——这里不说一句几乎没人会发现
                    VStack(alignment: .leading, spacing: 6) {
                        Text("局域网里有 Apple Silicon Mac 的话，可以让它替 NAS 做硬件转码，省下这里的 CPU 开销。")
                        Button("去设置远程转码", action: openRemoteSettings)
                            .font(.footnote.weight(.semibold))
                    }
                    .font(.footnote)
                    .foregroundStyle(.white.opacity(0.6))
                    .padding(10)
                    .background(.white.opacity(0.06), in: .rect(cornerRadius: 12))
                    Text("开启后长期生效，之后不再询问。")
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.45))
                    HStack {
                        Spacer()
                        Button("取消", action: cancel).buttonStyle(.glass)
                        Button {
                            Task {
                                saving = true
                                error = nil
                                do {
                                    try await grant()
                                } catch {
                                    self.error = error.localizedDescription
                                    saving = false
                                }
                            }
                        } label: {
                            Text(saving ? "正在开启…" : "开启并播放")
                        }
                        .buttonStyle(PlayerPrimaryButtonStyle())
                        .disabled(saving)
                        .accessibilityIdentifier("consent-enable")
                    }
                } else {
                    Text("当前未开启软件转码。请联系管理员开启（管理员播放此类影片时会收到开启询问）。")
                        .font(.subheadline)
                        .foregroundStyle(.white.opacity(0.7))
                    HStack {
                        Spacer()
                        Button("知道了", action: cancel).buttonStyle(.glass)
                    }
                }
            }
            .padding(24)
            .frame(maxWidth: 460)
            .background(Color(red: 16 / 255, green: 18 / 255, blue: 26 / 255).opacity(0.92), in: .rect(cornerRadius: 20))
            .overlay(RoundedRectangle(cornerRadius: 20).strokeBorder(.white.opacity(0.1)))
            .padding(20)
        }
        .accessibilityIdentifier("player-consent")
    }

    private func labeled(_ title: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption).foregroundStyle(.white.opacity(0.45))
            Text(text).font(.subheadline).foregroundStyle(.white.opacity(0.85))
        }
    }
}

/// 片尾「即将播放」卡片：常驻到用户点它或关掉，不自动倒计时（倒计时会在片尾没看完时抢走画面）
struct UpNextCard: View {
    let label: String
    let dismiss: () -> Void
    let play: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("即将播放")
                .font(.caption)
                .textCase(.uppercase)
                .foregroundStyle(.white.opacity(0.5))
            Text(label)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.white)
                .lineLimit(1)
            HStack(spacing: 8) {
                Button("关闭", action: dismiss)
                    .buttonStyle(.glass)
                Button(action: play) {
                    Text("立即播放")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.black)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                        .background(.white, in: .capsule)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("upnext-play")
            }
            .padding(.top, 8)
        }
        .padding(16)
        .frame(width: 260)
        .glassEffect(.regular, in: .rect(cornerRadius: 18))
        .accessibilityIdentifier("player-upnext")
    }
}

/// 暂停遮罩：Netflix 式大字片名，靠左下落在控制条上方（压暗画面，让人一眼看出是暂停）
struct PausedOverlay: View {
    let title: String
    let episodeLabel: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("已暂停")
                .font(.caption)
                .tracking(3)
                .foregroundStyle(.white.opacity(0.55))
            Text(title)
                .font(.title.bold())
                .foregroundStyle(.white)
                .lineLimit(1)
                .shadow(radius: 6)
            if let episodeLabel {
                Text(episodeLabel)
                    .font(.subheadline)
                    .foregroundStyle(.white.opacity(0.7))
                    .lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .allowsHitTesting(false)
        .accessibilityIdentifier("player-paused")
    }
}

/// 胶囊 HUD：倍速、亮度/音量调节、横滑定位预览共用
struct PlayerHUD<Content: View>: View {
    @ViewBuilder let content: Content

    var body: some View {
        HStack(spacing: 10) { content }
            .font(.subheadline.weight(.medium))
            .foregroundStyle(.white)
            .padding(.horizontal, 16)
            .padding(.vertical, 9)
            .background(.black.opacity(0.7), in: .capsule)
            .shadow(color: .black.opacity(0.45), radius: 14, y: 6)
            .allowsHitTesting(false)
    }
}

/// 调节条（亮度 / 音量）
struct LevelBar: View {
    let value: Double

    var body: some View {
        HStack(spacing: 8) {
            ZStack(alignment: .leading) {
                Capsule().fill(.white.opacity(0.25))
                Capsule().fill(.white).frame(width: 96 * min(1, max(0, value)))
            }
            .frame(width: 96, height: 4)
            Text("\(Int((value * 100).rounded()))%").monospacedDigit().frame(width: 40, alignment: .trailing)
        }
    }
}

/// 播放器里的主按钮：白底黑字胶囊（同 Web 的 player-accent 按钮；系统 glassProminent 在纯黑背景上白字白底看不清）
struct PlayerPrimaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(.black)
            .padding(.horizontal, 20)
            .padding(.vertical, 10)
            .background(.white.opacity(configuration.isPressed ? 0.75 : 1), in: .capsule)
    }
}
