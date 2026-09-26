import SwiftUI

// 订阅模块沉淀出来的通用积木：表单弹层骨架、提示行、芯片、勾选行。
// 订阅弹层、调整订阅、洗一轮版、规则组编辑器、取消订阅等十来个对话框共用同一套，
// 之后「设置 → 订阅规则」复用规则组编辑器时也直接拿这些积木。

// MARK: - 弹层骨架

/// 表单弹层骨架（iOS 26 原生形态）：导航栏左上 ✕ 关闭、右上 ✓ 确认，正文是原生分组列表（`Form`），
/// 调用方往 `content` 里直接放 `Section`。
///
/// 为什么这样搭：
/// - 不自设弹层背景：系统只在半高 / 自定义高度时给弹层悬浮的液态玻璃材质，自设 `presentationBackground` 会把它盖掉；
/// - 高度贴合内容（`SubsFittedDetents`）：这些弹层大多只是确认一两项，内容多高弹层就开多高，上拉仍可到全高；
/// - 确认放右上角 ✓（`role: .confirm`）而不是底部满宽大按钮：弹层停在半高时右上角离拇指并不远，也和系统日历、提醒事项一致；
///   破坏性动作（取消订阅、清理）不放 ✓，由调用方在列表末尾放一组红色行按钮，与系统「删除」类操作同一形态。
struct SubsSheetScaffold<Content: View>: View {
    let title: String
    /// 顶部说明：一段不带底色的说明文字（替代 Web 弹层标题下的副标题）
    var subtitle: String?
    /// 关闭键的读屏名（界面上显示 ✕）
    var closeTitle = "取消"
    var onClose: (() -> Void)?
    /// 是否显示左上 ✕：结果页只留右上「完成」，两个键做同一件事反而让人犹豫
    var closable = true
    /// 右上角确认；nil = 只有关闭（纯展示或确认动作在列表行里）
    var confirm: SubsSheetConfirm?
    /// 内容是否已加载好：加载中弹层停在半高，免得先缩成一条再涨回去
    var ready = true
    /// 直接开到全高（报告类长内容）
    var fullHeight = false
    @ViewBuilder let content: () -> Content
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                if let subtitle {
                    Section {
                        Text(subtitle)
                            .font(.subheadline)
                            .foregroundStyle(Theme.text.opacity(0.75))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .subsBareRow()
                }
                content()
            }
            .subsFormStyle()
            .modifier(SubsFittedDetents(ready: ready, fullHeight: fullHeight))
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if closable {
                    ToolbarItem(placement: .cancellationAction) {
                        Button(closeTitle, systemImage: "xmark", role: .close) {
                            if let onClose { onClose() } else { dismiss() }
                        }
                        .accessibilityIdentifier("sheet-close")
                    }
                }
                if let confirm {
                    ToolbarItem(placement: .confirmationAction) {
                        if confirm.busy {
                            ProgressView().accessibilityLabel(confirm.title)
                        } else {
                            Button(confirm.title, systemImage: "checkmark", role: .confirm, action: confirm.action)
                                .discoverProminentButton()
                                .disabled(!confirm.enabled)
                                .accessibilityIdentifier(confirm.identifier ?? confirm.title)
                        }
                    }
                }
            }
        }
    }
}

/// 右上角确认键：界面上是 ✓，`title` 作读屏名；忙碌时换成转圈
struct SubsSheetConfirm {
    let title: String
    var enabled = true
    var busy = false
    var identifier: String?
    let action: () -> Void
}

/// 弹层高度贴合表单内容：量出「内容 + 导航栏 + 底部安全区」的总高度作为自定义档位，另留全高档可上拉。
///
/// 两个实测过的坑：
/// - 要的高度超过弹层能开的最大值时系统虽会截断，但每次重设都会把列表往底部带（长表单停在最底下，顶部内容被滚出视野），
///   所以封顶到「窗口高度 − 顶部安全区」；
/// - 加载中内容很矮，贴合过去再随数据涨回来是两段动画，所以 `ready` 之前停在半高。
/// 用户已手动拉到全高时不再跟随内容变化去抢。
struct SubsFittedDetents: ViewModifier {
    var ready = true
    var fullHeight = false
    @State private var measured: CGFloat = 0
    @State private var fitHeight: CGFloat?
    @State private var detent: PresentationDetent = .medium

    private var fitDetent: PresentationDetent { fitHeight.map { .height($0) } ?? .medium }

    func body(content: Content) -> some View {
        content
            .onScrollGeometryChange(for: CGFloat.self) { geometry in
                geometry.contentSize.height + geometry.contentInsets.top + geometry.contentInsets.bottom
            } action: { _, height in
                measured = height
                fit()
            }
            .onChange(of: ready) { fit() }
            .onChange(of: fullHeight, initial: true) { _, full in
                if full { detent = .large }
            }
            .presentationDetents([fitDetent, .large], selection: $detent)
    }

    private func fit() {
        guard ready, !fullHeight else { return }
        let height = min(measured.rounded(.up), Self.maxSheetHeight)
        guard height > 0, height != fitHeight else { return }
        let following = detent != .large
        fitHeight = height
        if following { detent = .height(height) }
    }

    /// 弹层能开的最大高度 = 窗口高度 − 顶部安全区（iPhone Air 实测 912 − 68 = 844）
    private static var maxSheetHeight: CGFloat {
        let window = UIApplication.shared.connectedScenes.compactMap { ($0 as? UIWindowScene)?.keyWindow }.first
        guard let window else { return .greatestFiniteMagnitude }
        return window.bounds.height - window.safeAreaInsets.top
    }
}

extension View {
    /// 玻璃弹层里的表单统一样式：透出弹层材质、收紧首尾留白、内容变化时守住顶部
    func subsFormStyle() -> some View {
        scrollContentBackground(.hidden)
            .scrollBounceBehavior(.basedOnSize)
            .contentMargins(.top, 4, for: .scrollContent)
            .contentMargins(.bottom, 8, for: .scrollContent)
            .defaultScrollAnchor(.top, for: .sizeChanges)
    }

    /// 不要行底色的「裸行」：说明文字、条目卡、海报墙这类自带版式的内容放进表单时用
    func subsBareRow() -> some View {
        listRowBackground(Color.clear)
            .listRowInsets(EdgeInsets(top: 2, leading: 4, bottom: 2, trailing: 4))
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

/// 表单里的提示行：带语义图标的一行字（错误、注意、信息），放进自己的 Section；
/// 比彩色圆角提示条更贴近系统表单，文字用主色保证在玻璃弹层上看得清
struct SubsNoticeRow: View {
    let text: String
    var tone: SubsTone = .info
    var systemImage: String?

    private var icon: String {
        if let systemImage { return systemImage }
        switch tone {
        case .error: return "exclamationmark.octagon.fill"
        case .warn: return "exclamationmark.triangle.fill"
        case .ok: return "checkmark.circle.fill"
        case .upgrade: return "sparkles"
        case .info, .neutral: return "info.circle.fill"
        }
    }

    var body: some View {
        Label {
            Text(text)
                .font(.subheadline)
                .foregroundStyle(tone == .error ? tone.color.mix(with: .white, by: 0.3) : Theme.text.opacity(0.88))
                .fixedSize(horizontal: false, vertical: true)
        } icon: {
            Image(systemName: icon).foregroundStyle(tone == .neutral ? Theme.textMuted : tone.color)
        }
    }
}

// MARK: - 芯片

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

// MARK: - 勾选行（原生表单行）

/// 标题 + 说明 + 开关的一行（自动续订、只要免费资源等）：放在表单 Section 里，行底由列表提供
struct SubsToggleRow: View {
    let title: String
    var hint: String?
    @Binding var isOn: Bool
    var identifier: String?

    var body: some View {
        Toggle(isOn: $isOn) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).foregroundStyle(Theme.text)
                if let hint {
                    Text(hint).font(.footnote).foregroundStyle(Theme.textMuted).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .accessibilityIdentifier(identifier ?? title)
    }
}

/// 清理开关：标题 + 后果说明 +（打开后才出现的）风险警示；禁用时整体压暗。
/// 原生开关行——删不删是「开 / 关」的决定，比勾选框更符合系统习惯
struct SubsCleanupToggle: View {
    let label: String
    var description: String?
    var warning: String?
    @Binding var isOn: Bool
    var disabled: Bool

    var body: some View {
        Toggle(isOn: $isOn) {
            VStack(alignment: .leading, spacing: 3) {
                Text(label).foregroundStyle(Theme.text)
                if let description {
                    Text(description).font(.footnote).foregroundStyle(Theme.textMuted)
                }
                if let warning {
                    Text(warning).font(.footnote).foregroundStyle(SubsTone.error.color)
                }
            }
            .fixedSize(horizontal: false, vertical: true)
        }
        .tint(SubsTone.error.color)
        .disabled(disabled)
        .opacity(disabled ? 0.45 : 1)
    }
}

/// 单选 / 多选行（规则组、片源档位、季）：右侧对勾表示选中，`trailing` 放对勾前的附加标记
struct SubsChoiceRow<Trailing: View>: View {
    let title: String
    var subtitle: String?
    var selected: Bool
    var tint: Color = Theme.accentStrong
    @ViewBuilder var trailing: () -> Trailing
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(alignment: .center, spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).foregroundStyle(Theme.text)
                    if let subtitle {
                        Text(subtitle).font(.footnote).foregroundStyle(Theme.textMuted).fixedSize(horizontal: false, vertical: true)
                    }
                }
                .multilineTextAlignment(.leading)
                Spacer(minLength: 4)
                trailing()
                Image(systemName: "checkmark")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(tint)
                    .opacity(selected ? 1 : 0)
            }
            .contentShape(.rect)
        }
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

extension SubsChoiceRow where Trailing == EmptyView {
    init(title: String, subtitle: String? = nil, selected: Bool, tint: Color = Theme.accentStrong, action: @escaping () -> Void) {
        self.init(title: title, subtitle: subtitle, selected: selected, tint: tint, trailing: { EmptyView() }, action: action)
    }
}
