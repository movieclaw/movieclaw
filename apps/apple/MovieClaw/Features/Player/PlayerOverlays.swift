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
                // 两颗按钮给同一个最小宽度：字数一样但字形宽窄不同，不等宽会让这一行显得歪
                HStack(spacing: 12) {
                    Button(action: exit) { Text("返回").frame(minWidth: 96) }
                        .buttonStyle(.glass)
                    Button(action: retry) { Text("重试").frame(minWidth: 96) }
                        .discoverProminentButton()
                        .accessibilityIdentifier("player-retry")
                }
                .controlSize(.large)
                .padding(.top, 12)
            }
            .frame(maxWidth: 480)
            .padding(24)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("player-error")
    }
}

/// 条目信息读取失败的整页（对应 Web player-page 的 failed 分支）：只有原因和「返回」，
/// 不给重试——条目都看不到，会话必然也开不了
struct PlayerInfoErrorView: View {
    let message: String
    let exit: () -> Void

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            VStack(spacing: 16) {
                Text(message)
                    .font(.subheadline)
                    .foregroundStyle(.white)
                    .multilineTextAlignment(.center)
                Button("返回", action: exit)
                    .buttonStyle(.glass)
                    .controlSize(.large)
                    .accessibilityIdentifier("player-info-error-back")
            }
            .frame(maxWidth: 480)
            .padding(24)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("player-info-error")
    }
}

/// 软件转码同意弹窗（对应 Web consent-dialog.tsx，docs/design/web-player.md §3.6）。
///
/// 保存粒度是全局开关，没有「仅本次允许」；普通成员看到的是说明而不是按钮（全局设置只有超管能改，
/// 给一个点了必然 403 的按钮比不给更糟）。
/// 外观同 iOS 26 系统提示框：压暗背景上一张大圆角液态玻璃卡片，底部并排两颗等宽的大按钮。
/// 同心：大号玻璃按钮实测高 50（半径 25），离卡片边 14，卡片圆角 = 25 + 14 = 39，按钮的圆头与卡片的圆角是同心圆；
/// 文字区离卡片边更远（24），同系统提示框「文字内收、按钮外扩」的层次。
struct PlayerConsentView: View {
    let decision: API.PlaybackDecisionView
    let grant: () async throws -> Void
    let openRemoteSettings: () -> Void
    let cancel: () -> Void

    @State private var saving = false
    @State private var error: String?

    private static let radius: CGFloat = 39
    private static let buttonInset: CGFloat = 14

    var body: some View {
        ZStack {
            Color.black.opacity(0.75).ignoresSafeArea()
            VStack(alignment: .leading, spacing: 0) {
                message
                    .padding([.top, .horizontal], 24)
                buttons
                    .controlSize(.large)
                    .padding(Self.buttonInset)
                    .padding(.top, 6)
            }
            .frame(maxWidth: 460)
            .glassEffect(PlayerGlass.panel, in: .rect(cornerRadius: Self.radius))
            .padding(20)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("player-consent")
    }

    private var message: some View {
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
                    Button(action: openRemoteSettings) {
                        // 一行字只有 16pt 高：上下各扩 14pt 凑满 44pt 触控高度，外侧再收回去，不改变排版
                        Text("去设置远程转码")
                            .padding(.vertical, 14)
                            .contentShape(.rect)
                    }
                    .padding(.vertical, -14)
                    .font(.footnote.weight(.semibold))
                }
                .font(.footnote)
                .foregroundStyle(.white.opacity(0.6))
                .padding(12)
                .background(.white.opacity(0.08), in: .rect(cornerRadius: 16))
                Text("开启后长期生效，之后不再询问。")
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.45))
            } else {
                Text("当前未开启软件转码。请联系管理员开启（管理员播放此类影片时会收到开启询问）。")
                    .font(.subheadline)
                    .foregroundStyle(.white.opacity(0.7))
            }
        }
    }

    @ViewBuilder
    private var buttons: some View {
        if decision.canSelfEnable == true {
            HStack(spacing: 10) {
                Button(action: cancel) { Text("取消").frame(maxWidth: .infinity) }
                    .buttonStyle(.glass)
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
                    Text(saving ? "正在开启…" : "开启并播放").frame(maxWidth: .infinity)
                }
                .discoverProminentButton()
                .disabled(saving)
                .accessibilityIdentifier("consent-enable")
            }
        } else {
            Button(action: cancel) { Text("知道了").frame(maxWidth: .infinity) }
                .buttonStyle(.glass)
        }
    }

    private func labeled(_ title: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption).foregroundStyle(.white.opacity(0.45))
            Text(text).font(.subheadline).foregroundStyle(.white.opacity(0.85))
        }
    }
}

/// 片尾「即将播放」卡片：常驻到用户点它或关掉，不自动倒计时（倒计时会在片尾没看完时抢走画面）。
///
/// 版式同 iOS 26 的通知 / 提示卡片：关闭收成右上角的 ✕，主操作「立即播放」通栏大按钮，片尾看字幕时一抬拇指就点中。
/// 几何：大号玻璃按钮实测高 50（半径 25），离卡片边 12，卡片圆角 37，三者同心；✕ 圆（半径 15）离上、右边各 21，
/// 也与卡片右上角同心，圆心和左边两行字的中线对齐（两行字高 36，上边距 18）。
/// 宽 224：横屏时离右侧「前进 10 秒」留出 18pt，不挨着。
struct PlayerUpNextCard: View {
    let label: String
    let dismiss: () -> Void
    let play: () -> Void

    private static let radius: CGFloat = 37
    private static let buttonInset: CGFloat = 12

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("即将播放")
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.5))
                    Text(label)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.white)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
                Button(action: dismiss) {
                    Image(systemName: "xmark")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(.white.opacity(0.8))
                        .frame(width: 30, height: 30)
                        .background(.white.opacity(0.14), in: .circle)
                        // 看得见的圆 30pt，触控区 44pt
                        .frame(width: 44, height: 44)
                        .contentShape(.rect)
                }
                .buttonStyle(.plain)
                // 触控区不撑高标题行
                .padding(.vertical, -7)
                .accessibilityLabel("不看下一集")
                .accessibilityIdentifier("upnext-dismiss")
            }
            .padding(.leading, 18)
            .padding(.trailing, 14)
            .padding(.top, 18)
            Button(action: play) {
                Label("立即播放", systemImage: "play.fill").frame(maxWidth: .infinity)
            }
            .discoverProminentButton()
            .controlSize(.large)
            .padding(Self.buttonInset)
            .padding(.top, 2)
            .accessibilityIdentifier("upnext-play")
        }
        .frame(width: 224)
        .glassEffect(PlayerGlass.panel, in: .rect(cornerRadius: Self.radius))
        .accessibilityElement(children: .contain)
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
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("player-paused")
    }
}

/// 胶囊 HUD：倍速、亮度/音量调节、横滑定位预览共用（液态玻璃，同 iOS 26 系统音量 HUD）
struct PlayerHUD<Content: View>: View {
    @ViewBuilder let content: Content

    var body: some View {
        HStack(spacing: 10) { content }
            .font(.subheadline.weight(.medium))
            .foregroundStyle(.white)
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .glassEffect(PlayerGlass.panel, in: .capsule)
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
            // 用最宽的「100%」占位定宽：数字位数变化（9% → 100%）不再撑开读数、带动进度条跳动
            Text("100%").monospacedDigit().hidden()
                .overlay(alignment: .trailing) {
                    Text("\(Int((value * 100).rounded()))%").monospacedDigit()
                }
                .lineLimit(1)
                .fixedSize()
        }
    }
}
