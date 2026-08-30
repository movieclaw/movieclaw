import AppKit

/// 设置窗的视觉常量与自绘小控件，尺寸照评审 demo（`device-pairing.html`）逐条对齐。
///
/// 单独成文件是因为这些东西和配对状态机完全无关：控制器只管「现在是什么状态、
/// 哪几行该出现」，尺寸、圆角、配色这类纯外观的决定集中在这里，改样式不用去翻
/// 状态机，改状态机也不会顺手把间距搞乱。
enum SettingsStyle {
    /// 窗口内容区宽度。够放下一条完整的 `http://192.168.1.100:3000`。
    static let windowWidth: CGFloat = 520
    /// 窗体左右留白（demo：22px）。
    static let windowPadding: CGFloat = 22
    /// 分组之间的间距（demo：20px）。
    static let sectionSpacing: CGFloat = 20
    /// 分组标题到分组之间（demo：6px）。
    static let headingSpacing: CGFloat = 6
    /// 分组圆角（demo：9px）。
    static let groupCornerRadius: CGFloat = 9
    /// 行的最小高度与左右内边距（demo：min-height 40，padding 8/13）。
    static let rowMinHeight: CGFloat = 40
    static let rowPaddingX: CGFloat = 13
    static let rowPaddingY: CGFloat = 8

    /// 去掉左右留白后，一行内容可用的宽度。换行标签的 `preferredMaxLayoutWidth`
    /// 都用它，避免各处各写一个魔数。
    static var contentWidth: CGFloat { windowWidth - windowPadding * 2 }
    /// 分组内一行内容可用的宽度。
    static var rowContentWidth: CGFloat { contentWidth - rowPaddingX * 2 }
}

extension NSColor {
    /// 安全地取一个半透明版本。
    ///
    /// `withAlphaComponent` 对 `controlAccentColor`、`separatorColor` 这类
    /// **目录颜色**（catalog color）不保证有效——它们要先在当前外观下解析成
    /// 具体的颜色分量才能改 alpha。这里统一先转 sRGB 再改，转换失败就退回
    /// 原色（丢掉半透明总好过画不出来）。调用点必须已经处在正确的绘制外观
    /// 上下文里，否则解析出来的是另一套深浅色。
    func translucent(_ alpha: CGFloat) -> NSColor {
        (usingColorSpace(.sRGB) ?? self).withAlphaComponent(alpha)
    }
}

/// 一组圆角列表，行与行之间用发丝线分隔——macOS 系统设置的 inset grouped 结构。
///
/// 没有用 `NSBox`：它自带标题区和 `contentViewMargins` 两套间距语义，和
/// Auto Layout 混用时要绕开的坑比自己画一个圆角矩形多。
///
/// **行是常驻的，靠 `isHidden` 增删。** 每个状态重建一遍视图树会更贴近 demo 的
/// 写法，但会丢掉正在输入的地址框焦点，也容易让重复挂上去的约束越积越多。
/// 代价是分隔线要跟着可见性走：`refresh()` 负责把第一条可见行上方的线收掉，
/// 并在整组都不可见时把自己也收掉。
final class GroupView: NSView {
    private let stack = NSStackView()
    private var rows: [NSView] = []
    /// `separators[i]` 是 `rows[i + 1]` 上方的那条线。
    private var separators: [HairlineView] = []

    init(rows: [NSView]) {
        super.init(frame: .zero)
        wantsLayer = true
        layer?.cornerRadius = SettingsStyle.groupCornerRadius
        layer?.borderWidth = 1
        translatesAutoresizingMaskIntoConstraints = false

        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 0
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor),
            stack.topAnchor.constraint(equalTo: topAnchor),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])

        for (index, row) in rows.enumerated() {
            if index > 0 {
                let line = Self.makeSeparator()
                separators.append(line)
                stack.addArrangedSubview(line)
                line.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
            }
            self.rows.append(row)
            stack.addArrangedSubview(row)
            // 竖直 stack 的 alignment 只对齐不拉宽，行要铺满得逐个挂宽度约束
            row.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        }
        applyColors()
        refresh()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    /// 按当前各行的可见性重排分隔线，并在整组为空时收起自己。
    func refresh() {
        var sawVisible = false
        for (index, row) in rows.enumerated() {
            let visible = !row.isHidden
            if index > 0 {
                separators[index - 1].isHidden = !(visible && sawVisible)
            }
            if visible { sawVisible = true }
        }
        isHidden = !sawVisible
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyColors()
    }

    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        applyColors()
    }

    private func applyColors() {
        // 解析颜色必须在本视图的外观上下文里做，否则拿到的是 App 当前外观，
        // 窗口单独设了 appearance 时会串色。
        effectiveAppearance.performAsCurrentDrawingAppearance {
            layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor
            layer?.borderColor = NSColor.separatorColor.cgColor
        }
    }

    private static func makeSeparator() -> HairlineView {
        let line = HairlineView()
        line.translatesAutoresizingMaskIntoConstraints = false
        // 1px 而不是 1pt：Retina 上就是最细的一条真实发丝线
        line.heightAnchor.constraint(equalToConstant: 1).isActive = true
        return line
    }
}

/// 分组内的发丝线，左端按行内边距缩进——系统设置里的分隔线不顶到圆角边。
///
/// 用 `draw` 而不是 layer 背景色：`draw` 天然跑在本视图的绘制外观里，
/// `separatorColor` 每次重绘都会重新解析，深浅色切换不需要额外的回调兜底。
final class HairlineView: NSView {
    var leadingInset: CGFloat = SettingsStyle.rowPaddingX

    override func draw(_ dirtyRect: NSRect) {
        NSColor.separatorColor.setFill()
        NSRect(
            x: leadingInset,
            y: 0,
            width: max(0, bounds.width - leadingInset),
            height: bounds.height
        ).fill()
    }
}

/// 状态圆点：一个实心点外面套一圈同色淡光晕（demo：8px）。
///
/// 比在文案前面拼一个「●」字符好在两点：颜色和字色解耦（文字保持正常前景色，
/// 只有点在变色，可读性更好），尺寸也不随字体设置漂移。
final class StatusDot: NSView {
    var color: NSColor = .tertiaryLabelColor {
        didSet {
            guard color != oldValue else { return }
            needsDisplay = true
        }
    }

    override var intrinsicContentSize: NSSize { NSSize(width: 10, height: 10) }

    override func draw(_ dirtyRect: NSRect) {
        let side = min(bounds.width, bounds.height)
        let box = NSRect(
            x: bounds.midX - side / 2,
            y: bounds.midY - side / 2,
            width: side,
            height: side
        )
        color.translucent(0.22).setFill()
        NSBezierPath(ovalIn: box).fill()
        color.setFill()
        NSBezierPath(ovalIn: box.insetBy(dx: side * 0.2, dy: side * 0.2)).fill()
    }
}

// MARK: - 组装辅助

extension SettingsStyle {
    /// 分组标题（demo：12px / medium / 次级色）。
    static func groupLabel(_ text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = .systemFont(ofSize: 12, weight: .medium)
        label.textColor = .secondaryLabelColor
        return label
    }

    /// 灰色脚注（demo：11px / 三级色），宽度受限所以会自动折行。
    static func footnote(_ text: String, width: CGFloat = contentWidth) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = .systemFont(ofSize: 11)
        label.textColor = .tertiaryLabelColor
        label.preferredMaxLayoutWidth = width
        return label
    }

    /// 不折行的小号说明文字（动作栏左端那句状态）。
    static func caption(_ text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = .systemFont(ofSize: 11)
        label.textColor = .tertiaryLabelColor
        label.lineBreakMode = .byTruncatingTail
        return label
    }

    /// 行内的左侧标题（demo：13px / 主色 / 不参与拉伸）。
    static func rowLabel(_ text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = .systemFont(ofSize: 13)
        label.setContentHuggingPriority(.required, for: .horizontal)
        label.setContentCompressionResistancePriority(.required, for: .horizontal)
        return label
    }

    /// 行内的右侧取值（demo：13px / 次级色 / 右对齐；mono 时 12px 等宽）。
    static func rowValue(_ text: String = "", mono: Bool = false) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = mono
            ? .monospacedSystemFont(ofSize: 12, weight: .regular)
            : .systemFont(ofSize: 13)
        label.textColor = .secondaryLabelColor
        label.alignment = .right
        label.lineBreakMode = .byTruncatingMiddle
        return label
    }

    /// 一行：左标题 + 右内容，内容靠右并吃掉剩余宽度。
    static func row(_ title: String, trailing: NSView) -> NSView {
        trailing.setAccessibilityLabel(title)
        return rawRow(views: [rowLabel(title), trailing])
    }

    /// 一整行由调用方自己排（配对码面板这种不分左右的内容）。
    static func rawRow(views: [NSView]) -> NSView {
        let row = NSStackView(views: views)
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 14
        row.edgeInsets = NSEdgeInsets(
            top: rowPaddingY, left: rowPaddingX, bottom: rowPaddingY, right: rowPaddingX
        )
        row.translatesAutoresizingMaskIntoConstraints = false
        row.heightAnchor.constraint(greaterThanOrEqualToConstant: rowMinHeight).isActive = true
        return row
    }

    /// 一整行的竖排内容（配对码面板：说明 + 大字码 + 链接，居中）。
    static func stackedRow(views: [NSView], spacing: CGFloat = 9) -> NSView {
        let column = NSStackView(views: views)
        column.orientation = .vertical
        column.alignment = .centerX
        column.spacing = spacing
        column.edgeInsets = NSEdgeInsets(top: 20, left: 14, bottom: 18, right: 14)
        column.translatesAutoresizingMaskIntoConstraints = false
        return column
    }

    /// 横向 stack 里的弹性占位，把它后面的控件顶到右边。
    ///
    /// 光放一个空 `NSView` 是不够的：它的抗拉伸优先级和两侧控件一样高，
    /// 多余空间未必落在它身上。把 hugging 压到最低，slack 才一定归它。
    static func flexibleSpacer() -> NSView {
        let spacer = NSView()
        spacer.setContentHuggingPriority(NSLayoutConstraint.Priority(1), for: .horizontal)
        spacer.setContentCompressionResistancePriority(
            NSLayoutConstraint.Priority(1), for: .horizontal
        )
        return spacer
    }

    /// 让一个视图横向铺满根容器（扣掉左右留白）。
    static func stretch(_ view: NSView, in root: NSView) {
        view.translatesAutoresizingMaskIntoConstraints = false
        view.widthAnchor.constraint(
            equalTo: root.widthAnchor, constant: -windowPadding * 2
        ).isActive = true
    }
}

/// 一个分区：小标题 + 圆角分组 + 灰色脚注（demo 的 `.mac-sec`）。
///
/// 三样东西共存亡：分组里一行都不可见时，标题和脚注也要跟着收起来，否则会
/// 留下一个指向空白的标题。这件事只有它自己知道，所以放在这里而不是控制器里。
final class SectionView: NSView {
    let group: GroupView
    private let heading: NSTextField?
    private let noteLabel: NSTextField
    private let column = NSStackView()

    /// 脚注文案；置空即隐藏。不同状态下同一个分区的脚注不一样，所以是可变的。
    var note: String = "" {
        didSet {
            noteLabel.stringValue = note
            refresh()
        }
    }

    init(title: String?, rows: [NSView]) {
        group = GroupView(rows: rows)
        if let title {
            heading = SettingsStyle.groupLabel(title)
        } else {
            heading = nil
        }
        noteLabel = SettingsStyle.footnote("")
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false

        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = SettingsStyle.headingSpacing
        column.translatesAutoresizingMaskIntoConstraints = false
        if let heading {
            column.addArrangedSubview(heading)
        }
        column.addArrangedSubview(group)
        column.addArrangedSubview(noteLabel)

        addSubview(column)
        NSLayoutConstraint.activate([
            column.leadingAnchor.constraint(equalTo: leadingAnchor),
            column.trailingAnchor.constraint(equalTo: trailingAnchor),
            column.topAnchor.constraint(equalTo: topAnchor),
            column.bottomAnchor.constraint(equalTo: bottomAnchor),
            group.widthAnchor.constraint(equalTo: column.widthAnchor),
            noteLabel.widthAnchor.constraint(equalTo: column.widthAnchor),
        ])
        refresh()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func refresh() {
        group.refresh()
        heading?.isHidden = group.isHidden
        noteLabel.isHidden = group.isHidden || note.isEmpty
        isHidden = group.isHidden
    }
}
