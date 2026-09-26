import SwiftUI

// 订阅模块沉淀出来的通用积木：弹层骨架、提示条、芯片、勾选行。
// 订阅弹层、调整订阅、洗一轮版、规则组编辑器、取消订阅等十来个对话框共用同一套，
// 之后「设置 → 订阅规则」复用规则组编辑器时也直接拿这些积木。

// MARK: - 弹层骨架

/// 对话框骨架（对应 Web Modal 的「头部常驻 + 正文滚动 + 底栏常驻」三段式）：
/// 顶部导航栏放标题与关闭，正文滚动，底部按钮常驻不被滚出屏幕。
struct SubsSheetScaffold<Content: View, Footer: View>: View {
    let title: String
    var subtitle: String?
    var closeTitle = "取消"
    var onClose: (() -> Void)?
    @ViewBuilder let content: () -> Content
    @ViewBuilder let footer: () -> Footer
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if let subtitle {
                        Text(subtitle)
                            .font(.subheadline)
                            .foregroundStyle(Theme.textMuted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    content()
                }
                .padding(.horizontal, 20)
                .padding(.top, 8)
                .padding(.bottom, 24)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .safeAreaBar(edge: .bottom) {
                footer()
                    .padding(.horizontal, 20)
                    .padding(.vertical, 12)
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(closeTitle, systemImage: "xmark") {
                        if let onClose { onClose() } else { dismiss() }
                    }
                    .accessibilityIdentifier("sheet-close")
                }
            }
            .background(Theme.background.opacity(0.35))
        }
        .presentationBackground(.regularMaterial)
    }
}

extension SubsSheetScaffold where Footer == EmptyView {
    init(title: String, subtitle: String? = nil, closeTitle: String = "取消", onClose: (() -> Void)? = nil, @ViewBuilder content: @escaping () -> Content) {
        self.init(title: title, subtitle: subtitle, closeTitle: closeTitle, onClose: onClose, content: content, footer: { EmptyView() })
    }
}

/// 底栏主按钮：满宽液态玻璃强调键（带忙碌转圈）
struct SubsPrimaryButton: View {
    let title: String
    var busy = false
    var enabled = true
    var destructive = false
    var identifier: String?
    let action: () -> Void

    var body: some View {
        styled
            .disabled(!enabled || busy)
            .accessibilityIdentifier(identifier ?? title)
    }

    @ViewBuilder
    private var styled: some View {
        if destructive {
            button.buttonStyle(.glassProminent).tint(SubsTone.error.color)
        } else {
            button.discoverProminentButton()
        }
    }

    private var button: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                if busy { ProgressView().controlSize(.small) }
                Text(title).font(.body.weight(.semibold))
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 6)
        }
    }
}

// MARK: - 提示条

enum SubsTone {
    case error, warn, info, ok, neutral, upgrade

    var color: Color {
        switch self {
        case .error: Color(red: 1, green: 0.42, blue: 0.42)
        case .warn: Color(red: 0xF5 / 255, green: 0xC4 / 255, blue: 0x51 / 255)
        case .info: Color(red: 0x7F / 255, green: 0xB0 / 255, blue: 1)
        case .ok: Color(red: 0x4A / 255, green: 0xDE / 255, blue: 0x80 / 255)
        case .neutral: Color.white
        case .upgrade: Color(red: 0x2D / 255, green: 0xD4 / 255, blue: 0xBF / 255)
        }
    }
}

/// 圆角提示条（红=失败、琥珀=需要注意、蓝=信息、绿=正向、青=洗版）
struct SubsNotice: View {
    let text: String
    var tone: SubsTone = .info
    var systemImage: String?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            if let systemImage {
                Image(systemName: systemImage).foregroundStyle(tone.color)
            }
            Text(text)
                .font(.subheadline)
                .foregroundStyle(tone == .neutral ? Theme.textMuted : tone.color.mix(with: .white, by: 0.45))
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(tone.color.opacity(tone == .neutral ? 0.05 : 0.1), in: .rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(tone.color.opacity(tone == .neutral ? 0.08 : 0.25)))
    }
}

/// 分组小标题 + 可选说明
struct SubsSectionHeader: View {
    let title: String
    var hint: String?
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text.opacity(0.88))
            if let hint {
                Text(hint).font(.caption).foregroundStyle(Theme.textFaint)
            }
        }
    }
}

// MARK: - 芯片

/// 规则摘要芯片墙（「2160p > 1080p」「仅免费」）
struct SubsSpecChips: View {
    let chips: [String]
    var emptyText: String?
    var emptyTone: Color = Theme.textFaint

    var body: some View {
        if chips.isEmpty {
            if let emptyText {
                Text(emptyText).font(.caption).foregroundStyle(emptyTone).fixedSize(horizontal: false, vertical: true)
            }
        } else {
            DiscoverFlowLayout(spacing: 6, lineSpacing: 6) {
                ForEach(chips, id: \.self) { chip in
                    DiscoverTag(text: chip, foreground: Theme.text.opacity(0.75), background: Color.white.opacity(0.07), weight: .regular)
                }
            }
        }
    }
}

/// 可切换芯片；`order` 有值时在前面标偏好序号（「点击依次选择，先选的优先」）
struct SubsToggleChip: View {
    let label: String
    var active: Bool
    var order: Int?
    var suffix: String?
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                if let order {
                    Text("\(order)")
                        .font(.system(size: 10, weight: .semibold))
                        .frame(width: 16, height: 16)
                        .background(Color.white.opacity(0.2), in: .circle)
                }
                Text(label)
                if let suffix { Text(suffix).foregroundStyle(Theme.textFaint) }
            }
            .font(.subheadline.weight(.medium))
            .foregroundStyle(active ? Theme.text : Theme.textMuted)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(active ? Color.white.opacity(0.14) : Color.white.opacity(0.03), in: .capsule)
            .overlay(Capsule().strokeBorder(active ? Color.white.opacity(0.25) : Color.white.opacity(0.08)))
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(active ? .isSelected : [])
    }
}

/// 三态芯片：不限 → 只要 → 排除（红色带 ✕）
struct SubsTriChip: View {
    enum Value { case off, include, exclude }
    let label: String
    let state: Value
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 4) {
                if state == .exclude { Text("✕") }
                Text(label)
            }
            .font(.subheadline.weight(.medium))
            .foregroundStyle(state == .include ? Theme.text : state == .exclude ? Color(red: 1, green: 0.75, blue: 0.75) : Theme.textMuted)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(fill, in: .capsule)
            .overlay(Capsule().strokeBorder(stroke))
        }
        .buttonStyle(.plain)
        .accessibilityValue(state == .include ? "只要" : state == .exclude ? "排除" : "不限")
    }

    private var fill: Color {
        switch state {
        case .include: Color.white.opacity(0.14)
        case .exclude: Color.red.opacity(0.12)
        case .off: Color.white.opacity(0.03)
        }
    }

    private var stroke: Color {
        switch state {
        case .include: Color.white.opacity(0.25)
        case .exclude: Color.red.opacity(0.35)
        case .off: Color.white.opacity(0.08)
        }
    }
}

// MARK: - 勾选行

/// 标题 + 说明 + 开关的一行（自动续订、只要免费资源等）
struct SubsToggleRow: View {
    let title: String
    var hint: String?
    @Binding var isOn: Bool
    var identifier: String?

    var body: some View {
        Toggle(isOn: $isOn) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.body.weight(.medium)).foregroundStyle(Theme.text.opacity(0.9))
                if let hint {
                    Text(hint).font(.caption).foregroundStyle(Theme.textFaint).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.white.opacity(0.08)))
        .accessibilityIdentifier(identifier ?? title)
    }
}

/// 清理开关：标题 + 后果说明 +（勾上后才出现的）风险警示；禁用时整体压暗
struct SubsCleanupToggle: View {
    let label: String
    var description: String?
    var warning: String?
    @Binding var isOn: Bool
    var disabled: Bool

    var body: some View {
        Button {
            isOn.toggle()
        } label: {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: isOn ? "checkmark.square.fill" : "square")
                    .font(.title3)
                    .foregroundStyle(isOn ? Theme.accent2 : Theme.textMuted)
                VStack(alignment: .leading, spacing: 3) {
                    Text(label).font(.subheadline).foregroundStyle(Theme.text.opacity(0.9))
                    if let description {
                        Text(description).font(.caption).foregroundStyle(Theme.textMuted)
                    }
                    if let warning {
                        Text(warning).font(.caption).foregroundStyle(SubsTone.error.color)
                    }
                }
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .disabled(disabled)
        .opacity(disabled ? 0.45 : 1)
        .accessibilityAddTraits(isOn ? .isSelected : [])
    }
}

/// 列表式单选行（规则组、片源档位）：选中描边高亮
struct SubsChoiceRow<Trailing: View>: View {
    let title: String
    var subtitle: String?
    var selected: Bool
    var tint: Color = .white
    @ViewBuilder var trailing: () -> Trailing
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(alignment: .center, spacing: 10) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(title).font(.body.weight(.medium)).foregroundStyle(Theme.text.opacity(0.92))
                    if let subtitle {
                        Text(subtitle).font(.caption).foregroundStyle(Theme.textMuted).fixedSize(horizontal: false, vertical: true)
                    }
                }
                .multilineTextAlignment(.leading)
                Spacer(minLength: 4)
                trailing()
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
            .background(selected ? tint.opacity(0.1) : Color.white.opacity(0.03), in: .rect(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(selected ? tint.opacity(0.4) : Color.white.opacity(0.08)))
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}
