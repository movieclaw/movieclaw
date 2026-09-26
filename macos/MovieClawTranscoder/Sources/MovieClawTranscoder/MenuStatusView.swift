import AppKit

/// 菜单顶部的状态卡片，取代原来「状态 / Worker / 任务 / 最近错误」四行纯文本。
///
/// 为什么不用普通菜单项：`NSMenuItem` 的标题只有一行、不会折行，片名或错误一长，
/// 整个菜单就被撑成半屏宽——ffmpeg 版本那一项更是把 `ffmpeg -version` 的整行首句
/// 都塞了进去。这里固定宽度，片名最多两行、错误最多三行，超出的完整内容放进
/// 悬停提示。
///
/// 只展示、不响应点击：操作仍是下面的标准菜单项，键盘导航、快捷键和液态玻璃的
/// 高亮都交给系统。菜单本身已经是玻璃，卡片只用淡色底，不再叠一层玻璃。
@MainActor
final class MenuStatusView: NSView {
    struct Model: Equatable {
        /// 一块内容卡片：正在转的片子，或当前状态下该告诉用户的一句话。
        struct Card: Equatable {
            var symbol: String
            var title: String
            var detail: String?
            var tooltip: String?
        }

        var presentation: WorkerStatePresentation
        var subtitle: String
        var card: Card?
        var error: String?
        var footnote: String?
    }

    /// 菜单宽度。放得下两行片名和一行进度，又不至于比系统菜单宽出一截。
    static let width: CGFloat = 300
    private static let insetX: CGFloat = 14
    private static let cardPadding: CGFloat = 10
    private static let cardIconSide: CGFloat = 16
    private static var contentWidth: CGFloat { width - insetX * 2 }
    /// 卡片里文字可用的宽度：扣掉卡片内边距、图标与间距。
    private static var cardTextWidth: CGFloat {
        contentWidth - cardPadding * 2 - cardIconSide - 8
    }

    private let stack = NSStackView()
    private let tile = GlyphTile(side: 28, glass: false)
    private let titleLabel = NSTextField(labelWithString: "MovieClaw Transcoder")
    private let subtitleLabel = NSTextField(labelWithString: "")
    private let pill = StatusPill()

    private let card = RoundedFillView(fill: .neutral)
    private let cardIcon = NSImageView()
    private let cardTitle = NSTextField(wrappingLabelWithString: "")
    private let cardDetail = NSTextField(wrappingLabelWithString: "")

    private let errorCard = RoundedFillView(fill: .tinted(.systemRed))
    private let errorLabel = NSTextField(wrappingLabelWithString: "")

    private let footnoteLabel = NSTextField(labelWithString: "")

    private(set) var model: Model?

    init() {
        super.init(frame: NSRect(x: 0, y: 0, width: Self.width, height: 60))
        buildLayout()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func apply(_ model: Model) {
        guard model != self.model else { return }
        self.model = model

        pill.apply(model.presentation)
        subtitleLabel.stringValue = model.subtitle
        subtitleLabel.toolTip = model.subtitle

        if let content = model.card {
            card.isHidden = false
            cardIcon.image = Symbols.image(content.symbol, pointSize: 14, weight: .medium)
            cardTitle.stringValue = content.title
            cardDetail.stringValue = content.detail ?? ""
            cardDetail.isHidden = content.detail?.isEmpty ?? true
            card.toolTip = content.tooltip
        } else {
            card.isHidden = true
        }

        if let error = model.error, !error.isEmpty {
            errorCard.isHidden = false
            errorLabel.stringValue = error
            errorCard.toolTip = error
        } else {
            errorCard.isHidden = true
        }

        footnoteLabel.stringValue = model.footnote ?? ""
        footnoteLabel.isHidden = model.footnote?.isEmpty ?? true
        resizeToFit()
    }

    // MARK: - 布局

    private func buildLayout() {
        titleLabel.font = .systemFont(ofSize: 13, weight: .semibold)
        titleLabel.lineBreakMode = .byTruncatingTail
        titleLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        subtitleLabel.font = .systemFont(ofSize: 11)
        subtitleLabel.textColor = .secondaryLabelColor
        subtitleLabel.lineBreakMode = .byTruncatingMiddle
        subtitleLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        // 胶囊跟标题同一行，副标题独占下面一整行：副标题是「Worker 名 · NAS 地址」，
        // 和胶囊抢同一行宽度的话，地址会被从中间截掉
        let titleRow = NSStackView(views: [titleLabel, SettingsStyle.flexibleSpacer(), pill])
        titleRow.orientation = .horizontal
        titleRow.alignment = .centerY
        titleRow.spacing = 6

        let titles = NSStackView(views: [titleRow, subtitleLabel])
        titles.orientation = .vertical
        titles.alignment = .leading
        titles.spacing = 0
        titleRow.widthAnchor.constraint(equalTo: titles.widthAnchor).isActive = true

        let header = NSStackView(views: [tile, titles])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.distribution = .fill
        header.spacing = 10
        // 标题列吃掉徽章之外的全部宽度，胶囊才能靠到最右
        titles.setContentHuggingPriority(.defaultLow, for: .horizontal)

        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.edgeInsets = NSEdgeInsets(top: 10, left: Self.insetX, bottom: 10, right: Self.insetX)
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.addArrangedSubview(header)
        stack.addArrangedSubview(makeCard())
        stack.addArrangedSubview(makeErrorCard())
        footnoteLabel.font = .systemFont(ofSize: 10.5)
        footnoteLabel.textColor = .tertiaryLabelColor
        footnoteLabel.lineBreakMode = .byTruncatingMiddle
        stack.addArrangedSubview(footnoteLabel)
        stack.setCustomSpacing(10, after: header)

        addSubview(stack)
        // 只钉顶边与宽度：高度由内容决定，resizeToFit 量出来后再回写 frame
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor),
            stack.topAnchor.constraint(equalTo: topAnchor),
            stack.widthAnchor.constraint(equalToConstant: Self.width),
        ])
        for view in [header, card, errorCard, footnoteLabel] {
            view.widthAnchor.constraint(equalToConstant: Self.contentWidth).isActive = true
        }
    }

    private func makeCard() -> NSView {
        cardIcon.contentTintColor = .secondaryLabelColor
        cardIcon.translatesAutoresizingMaskIntoConstraints = false
        cardIcon.setContentHuggingPriority(.required, for: .horizontal)
        NSLayoutConstraint.activate([
            cardIcon.widthAnchor.constraint(equalToConstant: Self.cardIconSide),
            cardIcon.heightAnchor.constraint(equalToConstant: Self.cardIconSide),
        ])
        configureWrapping(cardTitle, size: 13, weight: .medium, lines: 2, color: .labelColor)
        configureWrapping(cardDetail, size: 11, weight: .regular, lines: 2, color: .secondaryLabelColor)
        // 进度数字等宽：转码位置每秒都在跳，比例字宽会让整行左右抖
        cardDetail.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular)

        let texts = NSStackView(views: [cardTitle, cardDetail])
        texts.orientation = .vertical
        texts.alignment = .leading
        texts.spacing = 3

        return fill(card, icon: cardIcon, content: texts)
    }

    private func makeErrorCard() -> NSView {
        let icon = NSImageView(image: Symbols.image(
            "exclamationmark.triangle.fill", pointSize: 12, weight: .medium, description: "错误"
        ) ?? NSImage())
        icon.contentTintColor = .systemRed
        icon.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            icon.widthAnchor.constraint(equalToConstant: Self.cardIconSide),
            icon.heightAnchor.constraint(equalToConstant: 14),
        ])
        configureWrapping(errorLabel, size: 11, weight: .regular, lines: 3, color: .labelColor)
        errorLabel.setAccessibilityLabel("最近错误")
        return fill(errorCard, icon: icon, content: errorLabel)
    }

    /// 卡片 = 圆角淡底 + 左上角图标 + 右侧文字。
    ///
    /// 内边距用显式约束而不是 `NSStackView.edgeInsets`：顶端对齐的横向 stack 在
    /// 垂直方向不计底部内边距，量出来的高度少一截，最后一行字贴着卡片底边。
    private func fill(_ background: RoundedFillView, icon: NSView, content: NSView) -> NSView {
        icon.translatesAutoresizingMaskIntoConstraints = false
        content.translatesAutoresizingMaskIntoConstraints = false
        background.addSubview(icon)
        background.addSubview(content)
        let padding = Self.cardPadding
        NSLayoutConstraint.activate([
            icon.leadingAnchor.constraint(equalTo: background.leadingAnchor, constant: padding),
            icon.topAnchor.constraint(equalTo: content.topAnchor),
            icon.bottomAnchor.constraint(lessThanOrEqualTo: background.bottomAnchor, constant: -padding),
            content.leadingAnchor.constraint(equalTo: icon.trailingAnchor, constant: 8),
            content.trailingAnchor.constraint(equalTo: background.trailingAnchor, constant: -padding),
            content.topAnchor.constraint(equalTo: background.topAnchor, constant: padding),
            content.bottomAnchor.constraint(equalTo: background.bottomAnchor, constant: -padding),
        ])
        return background
    }

    private func configureWrapping(
        _ label: NSTextField,
        size: CGFloat,
        weight: NSFont.Weight,
        lines: Int,
        color: NSColor
    ) {
        label.font = .systemFont(ofSize: size, weight: weight)
        label.textColor = color
        label.maximumNumberOfLines = lines
        label.lineBreakMode = .byWordWrapping
        // 超出行数时最后一行末尾补省略号，而不是生硬地截掉半个字
        label.cell?.truncatesLastVisibleLine = true
        label.preferredMaxLayoutWidth = Self.cardTextWidth
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
    }

    /// 高度随内容走：宽度固定，量出高度后回写自己的 frame（菜单按 frame 排版）。
    private func resizeToFit() {
        stack.layoutSubtreeIfNeeded()
        let height = ceil(stack.fittingSize.height)
        guard abs(frame.height - height) > 0.5 else { return }
        setFrameSize(NSSize(width: Self.width, height: height))
    }
}

/// 圆角淡底。颜色在绘制时按当前外观解析，深浅色切换自动跟上。
final class RoundedFillView: NSView {
    enum Fill {
        /// 中性：浅色下是 6% 的黑，深色下是 6% 的白。
        case neutral
        case tinted(NSColor)
    }

    private let fill: Fill

    init(fill: Fill) {
        self.fill = fill
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func draw(_ dirtyRect: NSRect) {
        let color: NSColor
        switch fill {
        case .neutral:
            color = NSColor.labelColor.translucent(0.06)
        case let .tinted(tint):
            color = tint.translucent(0.12)
        }
        color.setFill()
        let radius = Glass.menuCardCornerRadius
        NSBezierPath(roundedRect: bounds, xRadius: radius, yRadius: radius).fill()
    }
}
