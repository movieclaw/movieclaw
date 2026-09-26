import SwiftUI

// 活动 / 待处理事项模块沉淀的通用小件，其它模块（订阅详情的下载进度、设置页的任务状态）也可直接复用。

/// 状态圆点：可呼吸（进行中）；完整状态文字给读屏
struct ActivityStatusDot: View {
    var color: Color
    var pulse = false
    var size: CGFloat = 8
    var label: String?

    @State private var animate = false

    var body: some View {
        ZStack {
            if pulse {
                Circle()
                    .fill(color.opacity(0.55))
                    .frame(width: size, height: size)
                    .scaleEffect(animate ? 2.2 : 1)
                    .opacity(animate ? 0 : 0.8)
            }
            Circle().fill(color).frame(width: size, height: size)
        }
        .frame(width: size, height: size)
        .onAppear {
            guard pulse else { return }
            withAnimation(.easeOut(duration: 1.4).repeatForever(autoreverses: false)) { animate = true }
        }
        .accessibilityElement()
        .accessibilityLabel(label.map { "状态：\($0)" } ?? "")
    }
}

/// 细进度条；percent 为 nil 时显示不定进度（1/3 宽度、可呼吸）
struct ActivityProgressBar: View {
    var percent: Double?
    var color: Color = Theme.info
    var height: CGFloat = 6
    var track: Color = Color.white.opacity(0.07)
    var indeterminateOpacity: Double = 0.65

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                Capsule().fill(track)
                if let percent {
                    let clamped = min(100, max(percent > 0 ? 1 : 0, percent))
                    Capsule().fill(color).frame(width: proxy.size.width * clamped / 100)
                        .animation(.easeOut(duration: 0.7), value: clamped)
                } else {
                    Capsule().fill(color.opacity(indeterminateOpacity)).frame(width: proxy.size.width / 3)
                }
            }
        }
        .frame(height: height)
    }
}

/// 卡片式空状态（Web playback-stats-section `EmptyState`）：说清这里本来会有什么、为什么没有、能做什么。
/// compact 版用在面板内部，只占一行。
struct ActivityEmptyCard<Actions: View>: View {
    var systemImage: String
    var title: String
    var message: String?
    var compact = false
    @ViewBuilder var actions: () -> Actions

    var body: some View {
        if compact {
            HStack(spacing: 12) {
                Image(systemName: systemImage)
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textFaint)
                    .frame(width: 28, height: 28)
                    .background(Color.white.opacity(0.05), in: .circle)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.subheadline).foregroundStyle(Theme.textMuted)
                    if let message { Text(message).font(.caption).foregroundStyle(Theme.textFaint) }
                }
                Spacer(minLength: 0)
                actions()
            }
            .padding(14)
        } else {
            VStack(spacing: 0) {
                Image(systemName: systemImage)
                    .font(.system(size: 18))
                    .foregroundStyle(Theme.textMuted)
                    .frame(width: 48, height: 48)
                    .background(Color.white.opacity(0.04), in: .circle)
                    .overlay(Circle().strokeBorder(Theme.line))
                    .padding(.bottom, 12)
                Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text.opacity(0.9))
                    .multilineTextAlignment(.center)
                if let message {
                    Text(message).font(.caption).foregroundStyle(Theme.textFaint)
                        .multilineTextAlignment(.center).lineSpacing(3).padding(.top, 6)
                }
                HStack(spacing: 8) { actions() }.padding(.top, 14)
            }
            .frame(maxWidth: .infinity)
            .padding(.horizontal, 16)
            .padding(.vertical, 32)
            .background(Color.white.opacity(0.02), in: .rect(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.white.opacity(0.07)))
        }
    }
}

extension ActivityEmptyCard where Actions == EmptyView {
    init(systemImage: String, title: String, message: String? = nil, compact: Bool = false) {
        self.init(systemImage: systemImage, title: title, message: message, compact: compact) { EmptyView() }
    }
}

/// 空状态里的文字胶囊按钮（「查看最近播放」「显示全部」）
struct ActivityPillButton: View {
    let title: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(.caption.weight(.medium))
                .foregroundStyle(Theme.text.opacity(0.85))
                .padding(.horizontal, 14)
                .padding(.vertical, 7)
                .background(Color.white.opacity(0.05), in: .capsule)
                .overlay(Capsule().strokeBorder(Color.white.opacity(0.12)))
                .contentShape(Capsule().inset(by: -7))
        }
        .buttonStyle(.plain)
    }
}

/// 琥珀色提示条（数据源异常、加载失败但保留旧数据时）
struct ActivityWarningBanner<Trailing: View>: View {
    let message: String
    @ViewBuilder var trailing: () -> Trailing

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "info.circle").font(.subheadline).padding(.top, 1)
            Text(message).font(.subheadline).frame(maxWidth: .infinity, alignment: .leading)
            trailing()
        }
        .foregroundStyle(Color(red: 1, green: 0.95, blue: 0.8))
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        .background(Theme.warning.opacity(0.10), in: .rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.warning.opacity(0.25)))
    }
}

extension ActivityWarningBanner where Trailing == EmptyView {
    init(message: String) {
        self.init(message: message) { EmptyView() }
    }
}
