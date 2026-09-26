import SwiftUI

/// 控制条上方弹出的小菜单面板（音轨 / 字幕 / 设置共用），外观同 Web MenuPanel：
/// 一块半透明的黑、行通宽命中、只用行尾对勾表示选中。
struct PlayerMenuPanel<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content
    @Environment(\.verticalSizeClass) private var verticalSizeClass
    @State private var contentHeight: CGFloat = 0

    private var maxRowsHeight: CGFloat { verticalSizeClass == .compact ? 200 : 360 }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.white.opacity(0.55))
                .padding(.horizontal, 16)
                .padding(.top, 10)
                .padding(.bottom, 4)
            // 内容短就贴合内容高度，超出上限才滚动（横屏矮，上限更低）
            ScrollView {
                rows.onGeometryChange(for: CGFloat.self) { $0.size.height } action: { contentHeight = $0 }
            }
            .scrollBounceBehavior(.basedOnSize)
            .frame(height: min(max(contentHeight, 1), maxRowsHeight))
        }
        .frame(width: 280)
        .background(.black.opacity(0.88), in: .rect(cornerRadius: 14))
        .shadow(color: .black.opacity(0.5), radius: 22, y: 10)
    }

    private var rows: some View {
        VStack(alignment: .leading, spacing: 0) { content }
            .padding(.bottom, 8)
    }
}

struct PlayerMenuRow<Badge: View>: View {
    let title: String
    var hint: String?
    let active: Bool
    var systemImage: String?
    var identifier: String?
    @ViewBuilder var badge: Badge
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                if let systemImage { Image(systemName: systemImage).frame(width: 18) }
                VStack(alignment: .leading, spacing: 1) {
                    Text(title).lineLimit(1)
                    if let hint { Text(hint).font(.caption2).foregroundStyle(.white.opacity(0.4)).lineLimit(2) }
                }
                Spacer(minLength: 6)
                badge
                if active { Image(systemName: "checkmark").font(.footnote.weight(.bold)) }
            }
            .font(.subheadline.weight(.medium))
            .foregroundStyle(active ? .white : .white.opacity(0.8))
            .padding(.horizontal, 16)
            .padding(.vertical, 9)
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier(identifier ?? "menu-\(title)")
    }
}

extension PlayerMenuRow where Badge == EmptyView {
    init(title: String, hint: String? = nil, active: Bool, systemImage: String? = nil, identifier: String? = nil, action: @escaping () -> Void) {
        self.init(title: title, hint: hint, active: active, systemImage: systemImage, identifier: identifier, badge: { EmptyView() }, action: action)
    }
}

private struct MenuDivider: View {
    var body: some View {
        Rectangle().fill(.white.opacity(0.1)).frame(height: 1).padding(.vertical, 6)
    }
}

private struct MenuNote: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(.white.opacity(0.5))
            .fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, 16)
            .padding(.top, 8)
    }
}

// MARK: - 音轨

struct AudioMenu: View {
    let controller: PlaybackController
    let close: () -> Void

    var body: some View {
        PlayerMenuPanel(title: "音轨") {
            ForEach(controller.audioOptions) { option in
                PlayerMenuRow(
                    title: option.label,
                    active: option.ref == controller.currentAudio || (controller.currentAudio == nil && option.isDefault)
                ) {
                    controller.selectAudio(option.ref)
                    close()
                }
            }
            if controller.audioSwitchRestarts {
                MenuDivider()
                MenuNote(text: "换音轨需要重新起流，会从当前位置续上，中间大约停顿一秒。")
            }
        }
    }
}

// MARK: - 字幕

struct SubtitleMenu: View {
    let controller: PlaybackController
    let close: () -> Void

    var body: some View {
        PlayerMenuPanel(title: "字幕") {
            // 选完即关：换轨是一次决定，不是连续调节；要调样式的重新打开菜单（同 Web）
            PlayerMenuRow(title: "关闭", active: controller.selectedSubtitle == nil, identifier: "subtitle-off") {
                controller.selectSubtitle(nil)
                close()
            }
            ForEach(controller.subtitles.options) { option in
                PlayerMenuRow(title: option.label, active: controller.selectedSubtitle == option.ref, badge: {
                    if option.isAI { AIChip() }
                }) {
                    controller.selectSubtitle(option.ref)
                    close()
                }
            }
            ForEach(controller.subtitles.unavailable) { item in
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.label).lineLimit(1)
                    Text(item.reason).font(.caption)
                }
                .font(.subheadline)
                .foregroundStyle(.white.opacity(0.35))
                .padding(.horizontal, 16)
                .padding(.vertical, 6)
            }
            if controller.subtitles.options.isEmpty, controller.subtitles.unavailable.isEmpty {
                Text("这个文件没有可用字幕")
                    .font(.subheadline)
                    .foregroundStyle(.white.opacity(0.45))
                    .padding(.horizontal, 16)
                    .padding(.vertical, 8)
            }
            if !controller.engineRendersSubtitles, controller.subtitles.options.contains(where: { $0.kind == "pgs" }) {
                // 用户点之前就该知道代价：系统播放器渲染不了图形字幕，要转码压制进画面
                MenuDivider()
                MenuNote(text: "图形字幕会转码压制进画面（切换约一秒），画中画等场景也能看到")
            }
            if controller.selectedSubtitle != nil {
                MenuDivider()
                SubtitleStyleEditor(controller: controller)
            }
        }
    }
}

/// 字幕样式：时间轴 ±0.1 秒（夹 ±30 秒）、字号、位置、描边、背景——存本机
private struct SubtitleStyleEditor: View {
    let controller: PlaybackController

    var body: some View {
        let style = controller.subtitleStyle
        VStack(spacing: 8) {
            StepRow(label: "时间轴", value: String(format: "%@%.1f 秒", style.offsetSeconds > 0 ? "+" : "", style.offsetSeconds)) {
                controller.subtitleStyle.offsetSeconds = SubtitleStyle.clampOffset(style.offsetSeconds - SubtitleStyle.offsetStep)
            } plus: {
                controller.subtitleStyle.offsetSeconds = SubtitleStyle.clampOffset(style.offsetSeconds + SubtitleStyle.offsetStep)
            }
            StepRow(label: "字号", value: String(format: "%.1f", style.fontScale)) {
                controller.subtitleStyle.fontScale = max(2, style.fontScale - 0.4)
            } plus: {
                controller.subtitleStyle.fontScale = min(10, style.fontScale + 0.4)
            }
            StepRow(label: "位置", value: "\(Int(style.bottomPercent))%") {
                controller.subtitleStyle.bottomPercent = max(0, style.bottomPercent - 2)
            } plus: {
                controller.subtitleStyle.bottomPercent = min(40, style.bottomPercent + 2)
            }
            HStack(spacing: 8) {
                StyleToggle(title: "描边", on: style.outline) { controller.subtitleStyle.outline.toggle() }
                StyleToggle(title: "背景", on: style.background) { controller.subtitleStyle.background.toggle() }
                Spacer()
            }
            .padding(.horizontal, 16)
        }
        .padding(.top, 4)
    }
}

private struct StepRow: View {
    let label: String
    let value: String
    let minus: () -> Void
    let plus: () -> Void

    var body: some View {
        HStack {
            Text(label).foregroundStyle(.white.opacity(0.65))
            Spacer()
            StepButton(symbol: "minus", label: "\(label)减", action: minus)
            Text(value)
                .monospacedDigit()
                .foregroundStyle(.white.opacity(0.9))
                .frame(width: 72)
                .accessibilityIdentifier("subtitle-\(label)-value")
            StepButton(symbol: "plus", label: "\(label)加", action: plus)
        }
        .font(.subheadline)
        .padding(.horizontal, 16)
    }
}

private struct StepButton: View {
    let symbol: String
    let label: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.caption.weight(.bold))
                .frame(width: 32, height: 32)
                .background(.white.opacity(0.08), in: .rect(cornerRadius: 8))
                .foregroundStyle(.white.opacity(0.85))
                .contentShape(Rectangle().inset(by: -6))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}

private struct StyleToggle: View {
    let title: String
    let on: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(.caption.weight(.medium))
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .foregroundStyle(on ? .black : .white.opacity(0.65))
                .background(on ? Theme.accentStrong : .white.opacity(0.06), in: .capsule)
        }
        .buttonStyle(.plain)
    }
}

/// 「AI 生成」标记：AI 字幕的译文与时间轴都可能有偏差，用户有权在选中之前就知道
struct AIChip: View {
    var body: some View {
        Label("AI 生成", systemImage: "sparkles")
            .font(.system(size: 10, weight: .semibold))
            .labelStyle(.titleAndIcon)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .foregroundStyle(.white)
            .background(LinearGradient(colors: [.purple, .blue], startPoint: .leading, endPoint: .trailing), in: .capsule)
    }
}

// MARK: - ⋯ 设置

struct SettingsMenu: View {
    let controller: PlaybackController
    let close: () -> Void

    var body: some View {
        PlayerMenuPanel(title: "设置") {
            // 画质：语义是上限——源不超所选档就照常直通（无损），超了才转码降下去
            Text("画质").font(.caption.weight(.medium)).foregroundStyle(.white.opacity(0.45)).padding(.horizontal, 16).padding(.top, 2)
            ForEach(QualityOption.all) { option in
                PlayerMenuRow(title: option.label, hint: option.hint, active: controller.quality == option.maxHeight, identifier: "quality-\(option.label)") {
                    controller.selectQuality(option.maxHeight)
                    close()
                }
            }
            MenuDivider()
            Text("播放引擎").font(.caption.weight(.medium)).foregroundStyle(.white.opacity(0.45)).padding(.horizontal, 16)
            ForEach(EnginePreference.allCases) { preference in
                PlayerMenuRow(title: preference.label, hint: preference.hint, active: controller.enginePreference == preference, identifier: "engine-\(preference.rawValue)") {
                    controller.selectEngine(preference)
                    close()
                }
            }
            MenuDivider()
            PlayerMenuRow(title: "播放诊断", active: controller.diagnosticsOpen, systemImage: "waveform.path.ecg", identifier: "menu-diagnostics") {
                controller.diagnosticsOpen.toggle()
                close()
            }
        }
    }
}
