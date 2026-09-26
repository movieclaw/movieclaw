import SwiftUI

/// 控制条上方弹出的菜单面板（音轨 / 字幕 / 设置共用），外观向 iOS 26 系统菜单看齐：
/// 一块常规液态玻璃、大圆角、行首对勾表示选中、行尾放图标，按下时整行浅色高亮。
/// 由 PlayerScreen 以「从按下的那颗按钮处缩放长出」的过渡呈现。
///
/// 对齐：标题、对勾、说明文字、样式编辑器的左缘同在一条线上（`MenuMetrics.edge`），
/// 各行的标题在对勾列之后另起一条线（`MenuMetrics.titleLeading`）。
struct PlayerMenuPanel<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content
    @Environment(\.playerMenuMaxHeight) private var maxHeight
    @State private var headerHeight: CGFloat = 0
    @State private var contentHeight: CGFloat = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(title)
                .font(.footnote.weight(.semibold))
                .foregroundStyle(.white.opacity(0.55))
                .padding(.horizontal, MenuMetrics.edge)
                .padding(.top, 14)
                .padding(.bottom, 4)
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { headerHeight = $0 }
            // 内容短就贴合内容高度；超出「顶栏与底栏之间的空隙」才在面板里滚动，面板永远不压住顶栏
            ScrollView {
                rows.onGeometryChange(for: CGFloat.self) { $0.size.height } action: { contentHeight = $0 }
            }
            .scrollBounceBehavior(.basedOnSize)
            .frame(height: min(max(contentHeight, 1), max(MenuMetrics.rowHeight, maxHeight - headerHeight)))
        }
        .frame(width: MenuMetrics.width)
        .clipShape(.rect(cornerRadius: MenuMetrics.radius))
        .glassEffect(PlayerGlass.panel, in: .rect(cornerRadius: MenuMetrics.radius))
    }

    private var rows: some View {
        VStack(alignment: .leading, spacing: 0) { content }
            .padding(.bottom, 8)
    }
}

/// 菜单行：行首固定一列放对勾（系统菜单的选中样式），标题下可带一行说明，行尾放标记与图标
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
            HStack(spacing: MenuMetrics.checkGap) {
                // 对勾靠列的左缘放，左缘与面板标题对齐
                Image(systemName: "checkmark")
                    .font(.footnote.weight(.bold))
                    .frame(width: MenuMetrics.checkColumn, alignment: .leading)
                    .opacity(active ? 1 : 0)
                VStack(alignment: .leading, spacing: 1) {
                    Text(title).lineLimit(1)
                    if let hint { Text(hint).font(.caption).foregroundStyle(.white.opacity(0.45)).lineLimit(2) }
                }
                Spacer(minLength: 6)
                badge
                if let systemImage { Image(systemName: systemImage).frame(width: 20) }
            }
            .font(.body)
            .foregroundStyle(.white)
            .padding(.horizontal, MenuMetrics.edge)
            .padding(.vertical, 10)
            // 单行的行也不低于系统最小触控高度
            .frame(minHeight: MenuMetrics.rowHeight)
            .contentShape(.rect)
        }
        .buttonStyle(MenuRowButtonStyle())
        .accessibilityIdentifier(identifier ?? "menu-\(title)")
        .accessibilityAddTraits(active ? .isSelected : [])
    }
}

extension PlayerMenuRow where Badge == EmptyView {
    init(title: String, hint: String? = nil, active: Bool, systemImage: String? = nil, identifier: String? = nil, action: @escaping () -> Void) {
        self.init(title: title, hint: hint, active: active, systemImage: systemImage, identifier: identifier, badge: { EmptyView() }, action: action)
    }
}

/// 菜单的尺寸：面板、行、对勾列与高亮块的几何关系
private enum MenuMetrics {
    static let width = PlayerLayout.menuWidth
    static let radius: CGFloat = 26
    /// 面板内容的左右边距：标题、对勾、说明、样式编辑器都贴这条线
    static let edge: CGFloat = 16
    /// 对勾列宽（对勾字形约 14pt）与它到标题的间距
    static let checkColumn: CGFloat = 16
    static let checkGap: CGFloat = 8
    /// 标题起点 = 边距 + 对勾列 + 间距；不可点的说明行（不可用字幕、空状态）也从这里起，和可点行的标题对齐
    static let titleLeading: CGFloat = edge + checkColumn + checkGap
    /// 行的最小高度 = 系统最小触控尺寸
    static let rowHeight: CGFloat = 44
    /// 按下高亮块离面板左右的距离；它的圆角 = 面板圆角 − 这段距离，贴到面板角上时与面板同心
    static let highlightInset: CGFloat = 6
}

extension EnvironmentValues {
    /// 菜单面板的最大高度：PlayerScreen 按「顶栏下缘到底栏按钮行上缘」的空隙算好注入，超高的菜单在面板里滚动
    var playerMenuMaxHeight: CGFloat {
        get { self[PlayerMenuMaxHeightKey.self] }
        set { self[PlayerMenuMaxHeightKey.self] = newValue }
    }
}

private struct PlayerMenuMaxHeightKey: EnvironmentKey {
    static let defaultValue: CGFloat = 400
}

/// 按下时整行浅色高亮（同系统菜单），高亮块左右内缩、圆角与面板同心，松手即恢复
private struct MenuRowButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background {
                RoundedRectangle(cornerRadius: MenuMetrics.radius - MenuMetrics.highlightInset, style: .continuous)
                    .fill(.white.opacity(0.14))
                    .padding(.horizontal, MenuMetrics.highlightInset)
                    .opacity(configuration.isPressed ? 1 : 0)
            }
    }
}

/// 分节线：系统菜单的分节是一道较粗的暗缝，而不是细线
private struct MenuDivider: View {
    var body: some View {
        Rectangle().fill(.black.opacity(0.25)).frame(height: 6).padding(.vertical, 4)
    }
}

private struct MenuNote: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(.white.opacity(0.5))
            .fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, MenuMetrics.edge)
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
                .padding(.leading, MenuMetrics.titleLeading)
                .padding(.trailing, MenuMetrics.edge)
                .padding(.vertical, 6)
            }
            if controller.subtitles.options.isEmpty, controller.subtitles.unavailable.isEmpty {
                Text("这个文件没有可用字幕")
                    .font(.subheadline)
                    .foregroundStyle(.white.opacity(0.45))
                    .padding(.leading, MenuMetrics.titleLeading)
                    .padding(.trailing, MenuMetrics.edge)
                    .padding(.vertical, 8)
            }
            if controller.graphicSubtitlesBurnIn, controller.subtitles.options.contains(where: { $0.kind == "pgs" }) {
                // 用户点之前就该知道代价：系统播放器渲染不了图形字幕、这时又换不了 MPV，要转码压制进画面
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
        // 行距 12：32pt 的步进键各向外扩 6pt 成 44pt 触控区，上下两行正好首尾相接、互不抢点
        VStack(spacing: 12) {
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
            .padding(.horizontal, MenuMetrics.edge)
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
        .padding(.horizontal, MenuMetrics.edge)
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
                .background(.white.opacity(0.14), in: .circle)
                .foregroundStyle(.white)
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
                .padding(.horizontal, 16)
                .frame(height: 36)
                .foregroundStyle(on ? .black : .white.opacity(0.75))
                .background(on ? Theme.accentStrong : .white.opacity(0.14), in: .capsule)
                // 看得见的胶囊 36pt，上下各扩 4pt 凑满 44pt 触控高度
                .contentShape(Capsule().inset(by: -4))
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
        // 面板标题直接用第一节的「画质」：点的就是 ⋯ 设置，再叠一行「设置」只是重复
        PlayerMenuPanel(title: "画质") {
            // 画质：语义是上限——源不超所选档就照常直通（无损），超了才转码降下去
            ForEach(QualityOption.all) { option in
                PlayerMenuRow(title: option.label, hint: option.hint, active: controller.quality == option.maxHeight, identifier: "quality-\(option.label)") {
                    controller.selectQuality(option.maxHeight)
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
