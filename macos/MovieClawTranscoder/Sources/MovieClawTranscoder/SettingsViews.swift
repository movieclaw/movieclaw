import AppKit

/// 设置窗的视觉常量与几个自绘小控件。
///
/// 单独成文件是因为这些东西和配对状态机完全无关：控制器只管「现在是什么状态、
/// 该显示什么」，尺寸、圆角、配色这类纯外观的决定集中在这里，改样式不用去翻
/// 状态机，改状态机也不会顺手把间距搞乱。
enum SettingsStyle {
    /// 窗口内容区宽度。够放下一条完整的 `http://192.168.1.100:3000`，
    /// 又不至于宽到让「地址」这一个输入框显得空荡。
    static let windowWidth: CGFloat = 560
    /// 根容器四周留白。
    static let windowPadding: CGFloat = 24
    /// 卡片内部留白。
    static let cardPadding: CGFloat = 16
    /// 卡片圆角，和系统设置里的分组保持同一量级。
    static let cardCornerRadius: CGFloat = 10
    /// 表单左侧标签列宽。中文两到四字加冒号，60 足够且不浪费横向空间。
    static let labelColumnWidth: CGFloat = 60

    /// 去掉左右留白后，一行内容可用的宽度。换行标签的
    /// `preferredMaxLayoutWidth` 都用它，避免各处各写一个魔数。
    static var contentWidth: CGFloat { windowWidth - windowPadding * 2 }
    /// 卡片内部一行内容可用的宽度。
    static var cardContentWidth: CGFloat { contentWidth - cardPadding * 2 }
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

/// 圆角卡片。
///
/// 没有用 `NSBox`：它自带标题区和 `contentViewMargins` 两套间距语义，和
/// Auto Layout 混用时要绕开的坑比自己画一个圆角矩形多。这里只要一个背景色 +
/// 边框 + 圆角，一个 layer-backed 的 `NSView` 就是最直接的写法。
@MainActor
final class CardView: NSView {
    /// 强调态（配对进行中）用主题色描边，让「现在轮到你了」在窗口里一眼可见。
    var isHighlighted = false {
        didSet {
            guard isHighlighted != oldValue else { return }
            needsDisplay = true
        }
    }

    init() {
        super.init(frame: .zero)
        wantsLayer = true
        layer?.cornerRadius = SettingsStyle.cardCornerRadius
        layer?.borderWidth = 1
        applyColors()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    /// 深浅色切换时必须重新解析一次颜色。
    ///
    /// `CGColor` 是**解析过的**颜色值，不像 `NSColor` 那样会跟随外观动态变化：
    /// 存进 layer 之后系统切到深色模式，这张卡片会保持浅色背景不动。这个
    /// 回调是 layer-backed 视图唯一的补救点。
    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyColors()
    }

    override func updateLayer() {
        applyColors()
    }

    override var wantsUpdateLayer: Bool { true }

    private func applyColors() {
        // 解析颜色必须在本视图的外观上下文里做，否则拿到的是 App 当前外观，
        // 窗口单独设了 appearance 时会串色。
        effectiveAppearance.performAsCurrentDrawingAppearance {
            layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor
            layer?.borderColor = isHighlighted
                ? NSColor.controlAccentColor.translucent(0.55).cgColor
                : NSColor.separatorColor.cgColor
        }
    }
}

/// 状态圆点：一个实心点外面套一圈同色淡光晕。
///
/// 比在文案前面拼一个「●」字符好在两点：颜色和字色解耦（文字保持正常前景色，
/// 只有点在变色，可读性更好），尺寸也不随字体设置漂移。
@MainActor
final class StatusDot: NSView {
    var color: NSColor = .tertiaryLabelColor {
        didSet {
            guard color != oldValue else { return }
            needsDisplay = true
        }
    }

    override var intrinsicContentSize: NSSize { NSSize(width: 12, height: 12) }

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
        NSBezierPath(ovalIn: box.insetBy(dx: side * 0.28, dy: side * 0.28)).fill()
    }
}

// MARK: - 组装辅助

@MainActor
extension SettingsStyle {
    /// 把若干行装进一张卡片。
    ///
    /// 内层用 `NSStackView` 的 `edgeInsets` 做内边距，卡片本身只负责背景，
    /// 两件事不互相牵扯。
    static func card(rows: [NSView], spacing: CGFloat = 12) -> CardView {
        let stack = NSStackView(views: rows)
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = spacing
        stack.edgeInsets = NSEdgeInsets(
            top: cardPadding, left: cardPadding, bottom: cardPadding, right: cardPadding
        )
        stack.translatesAutoresizingMaskIntoConstraints = false

        let card = CardView()
        card.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: card.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: card.trailingAnchor),
            stack.topAnchor.constraint(equalTo: card.topAnchor),
            stack.bottomAnchor.constraint(equalTo: card.bottomAnchor),
        ])
        // 竖直 stack 的 alignment 只对齐不拉宽，行要铺满得逐个挂宽度约束。
        // 铺满是对的：表单行铺满输入框才占得住右侧空间，折行文案铺满才不会
        // 在一张宽卡片里挤成窄长条。
        for row in rows {
            row.widthAnchor.constraint(
                equalTo: stack.widthAnchor, constant: -cardPadding * 2
            ).isActive = true
        }
        return card
    }

    /// 分组小标题。
    static func groupLabel(_ text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = .systemFont(ofSize: 11, weight: .semibold)
        label.textColor = .secondaryLabelColor
        return label
    }

    /// 灰色脚注，宽度受限所以会自动折行。
    static func footnote(_ text: String, width: CGFloat = contentWidth) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = .systemFont(ofSize: 11)
        label.textColor = .tertiaryLabelColor
        label.preferredMaxLayoutWidth = width
        return label
    }

    /// 表单行：右对齐的窄标签 + 铺满剩余宽度的控件。
    static func formRow(_ title: String, control: NSView) -> NSView {
        control.translatesAutoresizingMaskIntoConstraints = false
        // 标签是独立控件，不加这句 VoiceOver 读到输入框只会说「文本框」
        control.setAccessibilityLabel(title)

        let label = NSTextField(labelWithString: title)
        label.alignment = .right
        label.textColor = .secondaryLabelColor
        label.translatesAutoresizingMaskIntoConstraints = false
        label.widthAnchor.constraint(equalToConstant: labelColumnWidth).isActive = true

        let row = NSStackView(views: [label, control])
        row.orientation = .horizontal
        row.spacing = 10
        row.alignment = .firstBaseline
        return row
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
    ///
    /// 竖直 `NSStackView` 的 `alignment` 只能对齐，不能拉宽，所以铺满得靠
    /// 逐个挂宽度约束。
    static func stretch(_ view: NSView, in root: NSView) {
        view.translatesAutoresizingMaskIntoConstraints = false
        view.widthAnchor.constraint(
            equalTo: root.widthAnchor, constant: -windowPadding * 2
        ).isActive = true
    }
}
