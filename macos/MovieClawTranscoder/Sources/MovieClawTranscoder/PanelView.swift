import AppKit

/// 点菜单栏图标弹出的状态面板的内容（面板窗口本身见 ``StatusPanel``）。
///
/// 从上到下四块：
/// 1. 页眉：徽标 + 「Worker 名 · NAS 地址」 + 状态胶囊；
/// 2. 主体（``PanelModel/Body``）：任务卡片 / 今日统计与最近任务 / 提示卡片；
/// 3. CPU / 内存小图表（连上之后才有）；
/// 4. 底栏：能力说明 + 设置 / 更多 / 退出三个图标按钮。
///
/// 没有「接收新任务」开关：App 开着就该接单，谁会、什么时候会去关它都说不清。
/// 排空（手上的转完、不接新的）只留给内部用——更新 ffmpeg 前自动做。
///
/// 只负责画和转发点击，不持有任何业务状态：内容全部来自 ``apply(_:)``。
/// 主体部分在内容变化时整块重建——面板很小、最多一秒刷新一次，重建比逐个控件
/// 对账简单可靠得多，也不会出现「上一个状态的控件没收干净」。
@MainActor
final class PanelView: NSView {
    static let width: CGFloat = 340
    static let padding: CGFloat = 12
    static var contentWidth: CGFloat { width - padding * 2 }

    var onAction: ((PanelModel.Action) -> Void)?
    var onOpenSettings: (() -> Void)?
    /// 「更多」按钮：由控制器在按钮下方弹出菜单。
    var onMore: ((NSView) -> Void)?
    var onQuit: (() -> Void)?

    private let stack = NSStackView()
    private let tile = GlyphTile(side: 32, glass: false)
    private let subtitleLabel = NSTextField(labelWithString: "")
    private let pill = StatusPill()
    private let body = NSStackView()
    private let resourceView = ResourceView()
    private let recoveryLabel = PanelText.label("", size: 10.5, color: .secondaryLabelColor)
    private let footnoteLabel = NSTextField(labelWithString: "")
    private let footer = NSStackView()

    private var model: PanelModel?

    init() {
        super.init(frame: NSRect(x: 0, y: 0, width: Self.width, height: 200))
        buildLayout()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func apply(_ model: PanelModel) {
        let bodyChanged = model.body != self.model?.body
        guard model != self.model else { return }
        self.model = model

        pill.apply(model.presentation)
        subtitleLabel.stringValue = model.subtitle
        subtitleLabel.toolTip = model.subtitle

        if bodyChanged {
            rebuildBody(model.body)
        }
        if let resources = model.resources {
            resourceView.isHidden = false
            resourceView.apply(resources)
        } else {
            resourceView.isHidden = true
        }
        recoveryLabel.stringValue = model.recoveryNote.map { "↻ \($0)" } ?? ""
        recoveryLabel.toolTip = "转码内核出过问题（崩溃、卡死或内存过高），已自动重启恢复，菜单栏 App 没有受影响。"
            + "详情见日志里带「[内核]」的行。"
        recoveryLabel.isHidden = model.recoveryNote == nil

        footnoteLabel.stringValue = model.footnote ?? ""
        footnoteLabel.toolTip = model.footnote
        resizeToFit()
    }

    // MARK: - 布局

    private func buildLayout() {
        let titleLabel = NSTextField(labelWithString: "MovieClaw 转码器")
        titleLabel.font = .systemFont(ofSize: 13, weight: .semibold)
        titleLabel.lineBreakMode = .byTruncatingTail
        titleLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        subtitleLabel.font = .systemFont(ofSize: 11)
        subtitleLabel.textColor = .secondaryLabelColor
        subtitleLabel.lineBreakMode = .byTruncatingMiddle
        subtitleLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let titles = NSStackView(views: [titleLabel, subtitleLabel])
        titles.orientation = .vertical
        titles.alignment = .leading
        titles.spacing = 1
        titles.setContentHuggingPriority(.defaultLow, for: .horizontal)

        let header = NSStackView(views: [tile, titles, SettingsStyle.flexibleSpacer(), pill])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = 10

        body.orientation = .vertical
        body.alignment = .leading
        body.spacing = 8

        footnoteLabel.font = .systemFont(ofSize: 10.5)
        footnoteLabel.textColor = .tertiaryLabelColor
        footnoteLabel.lineBreakMode = .byTruncatingTail
        footnoteLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let settings = IconButton(symbol: "gearshape", tooltip: "设置…（⌘,）") { [weak self] _ in
            self?.onOpenSettings?()
        }
        let more = IconButton(symbol: "ellipsis.circle", tooltip: "更多") { [weak self] button in
            self?.onMore?(button)
        }
        let quit = IconButton(symbol: "power", tooltip: "退出 MovieClaw 转码器（⌘Q）") { [weak self] _ in
            self?.onQuit?()
        }
        footer.setViews([footnoteLabel, SettingsStyle.flexibleSpacer(), settings, more, quit], in: .leading)
        footer.orientation = .horizontal
        footer.alignment = .centerY
        footer.spacing = 2

        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        let separator = HairlineView()
        separator.leadingInset = 0
        separator.translatesAutoresizingMaskIntoConstraints = false
        separator.heightAnchor.constraint(equalToConstant: 1).isActive = true
        recoveryLabel.lineBreakMode = .byTruncatingTail
        recoveryLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        for view in [header, body, resourceView, recoveryLabel, separator, footer] {
            stack.addArrangedSubview(view)
            view.widthAnchor.constraint(equalToConstant: Self.contentWidth).isActive = true
        }
        stack.setCustomSpacing(12, after: header)
        stack.setCustomSpacing(8, after: separator)

        addSubview(stack)
        // 内边距用显式约束（NSStackView 的 edgeInsets 在某些排列下不计底边，量出来会矮一截）
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: Self.padding),
            stack.topAnchor.constraint(equalTo: topAnchor, constant: Self.padding + 2),
            stack.widthAnchor.constraint(equalToConstant: Self.contentWidth),
        ])
    }

    private func rebuildBody(_ content: PanelModel.Body) {
        for view in body.arrangedSubviews {
            body.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
        var views: [NSView] = []
        switch content {
        case let .jobs(cards):
            views = cards.map(JobCardView.init)
        case let .idle(summary, rows):
            views.append(summary.map(SummaryView.init) ?? EmptyStateView())
            if !rows.isEmpty {
                views.append(HistoryView(rows: rows))
            }
        case let .notice(notice):
            let view = NoticeView(notice: notice)
            view.onAction = { [weak self] action in self?.onAction?(action) }
            views = [view]
        }
        for view in views {
            body.addArrangedSubview(view)
            view.widthAnchor.constraint(equalToConstant: Self.contentWidth).isActive = true
        }
    }

    /// 宽度固定，高度随内容；量出来后回写 frame，面板窗口据此调整高度。
    private func resizeToFit() {
        stack.layoutSubtreeIfNeeded()
        let height = ceil(stack.fittingSize.height) + Self.padding * 2 + 2
        guard abs(frame.height - height) > 0.5 else { return }
        setFrameSize(NSSize(width: Self.width, height: height))
    }
}

// MARK: - 主体：任务卡片

/// 一个正在转的任务：封面位 + 片名 + 转码方式；下面一行带颜色的结论（播放流畅 /
/// 已提前准备好 / 跟不上……）和「已准备到哪儿」，再下面一句人话解释。
/// 倍速之类的数字不上卡片，悬停提示里有。
private final class JobCardView: RoundedFillView {
    init(card: PanelModel.JobCard) {
        super.init(fill: .neutral)
        toolTip = card.tooltip

        let poster = PosterTile(symbol: "film", tint: .controlAccentColor)
        let title = PanelText.wrapping(card.title, size: 13, weight: .semibold, lines: 2,
                                       width: Self.textWidth)
        var texts: [NSView] = [title]
        if let encoder = card.encoder {
            texts.append(PanelText.label(encoder, size: 11, color: .secondaryLabelColor))
        }
        let titleColumn = NSStackView(views: texts)
        titleColumn.orientation = .vertical
        titleColumn.alignment = .leading
        titleColumn.spacing = 2

        let top = NSStackView(views: [poster, titleColumn])
        top.orientation = .horizontal
        top.alignment = .top
        top.spacing = 10

        var rows: [NSView] = [top]
        if card.watchedFraction != nil || card.preparedFraction != nil {
            rows.append(ProgressCapsule(watched: card.watchedFraction, prepared: card.preparedFraction))
        }

        let color = Self.color(card.health)
        let dot = StatusDot()
        dot.color = color
        let verdict = PanelText.label(card.health.title, size: 12.5, weight: .semibold,
                                      color: card.health.tone == .critical ? .systemRed : .labelColor)
        var statusViews: [NSView] = [dot, verdict, SettingsStyle.flexibleSpacer()]
        if let progress = card.progress {
            let label = PanelText.label(progress, size: 11, color: .secondaryLabelColor, monospacedDigits: true)
            label.setContentCompressionResistancePriority(.required, for: .horizontal)
            statusViews.append(label)
        }
        let statusRow = NSStackView(views: statusViews)
        statusRow.orientation = .horizontal
        statusRow.alignment = .centerY
        statusRow.spacing = 6
        rows.append(statusRow)
        rows.append(PanelText.wrapping(card.explanation, size: 11, lines: 3,
                                       width: PanelView.contentWidth - 24, color: .secondaryLabelColor))

        let column = NSStackView(views: rows)
        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = 8
        column.setCustomSpacing(12, after: top)
        column.setCustomSpacing(3, after: statusRow)
        for row in rows where row !== top {
            row.widthAnchor.constraint(equalTo: column.widthAnchor).isActive = true
        }
        pin(column, padding: 12)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    static var textWidth: CGFloat { PanelView.contentWidth - 24 - PosterTile.side - 10 }

    /// 状态点的颜色：好的一律绿，刚够用橙，跟不上红，刚起转灰。
    private static func color(_ health: PanelModel.Health) -> NSColor {
        switch health {
        case .starting, .viewerPaused: return .tertiaryLabelColor
        case .ahead, .smooth: return .systemGreen
        case .tight: return .systemOrange
        case .lagging: return .systemRed
        }
    }
}

/// 封面位：圆角方块里一个 SF Symbol（服务端下发海报后换成真图）。
private final class PosterTile: NSView {
    static let side: CGFloat = 40
    private let tint: NSColor

    init(symbol: String, tint: NSColor) {
        self.tint = tint
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            widthAnchor.constraint(equalToConstant: Self.side),
            heightAnchor.constraint(equalToConstant: Self.side),
        ])
        let image = NSImageView(image: Symbols.image(symbol, pointSize: 16, weight: .semibold) ?? NSImage())
        image.contentTintColor = tint
        image.translatesAutoresizingMaskIntoConstraints = false
        addSubview(image)
        NSLayoutConstraint.activate([
            image.centerXAnchor.constraint(equalTo: centerXAnchor),
            image.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func draw(_ dirtyRect: NSRect) {
        tint.translucent(0.16).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 9, yRadius: 9).fill()
    }
}

/// 细进度条，和视频播放器底下那条一个意思：实心段是观众看到的位置，浅色段是转码
/// 已经准备好的部分，灰色是还没转到的。系统的 NSProgressIndicator 画不出两段，
/// 在面板里也偏粗。
private final class ProgressCapsule: NSView {
    private let watched: Double?
    private let prepared: Double?

    init(watched: Double?, prepared: Double?) {
        self.watched = watched.map { min(1, max(0, $0)) }
        self.prepared = prepared.map { min(1, max(0, $0)) }
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        heightAnchor.constraint(equalToConstant: 5).isActive = true
        setAccessibilityRole(.progressIndicator)
        setAccessibilityValue(NSNumber(value: self.watched ?? self.prepared ?? 0))
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func draw(_ dirtyRect: NSRect) {
        let radius = bounds.height / 2
        func bar(_ fraction: Double, _ color: NSColor) {
            var rect = bounds
            rect.size.width = max(bounds.height, bounds.width * fraction)
            color.setFill()
            NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius).fill()
        }
        NSColor.labelColor.translucent(0.1).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: radius, yRadius: radius).fill()
        if let prepared { bar(prepared, NSColor.controlAccentColor.translucent(0.35)) }
        if let watched { bar(watched, .controlAccentColor) }
    }
}

// MARK: - 主体：空闲

/// 今天的三个数字：转了几次、一共转出多长的片子、顺不顺利。
private final class SummaryView: RoundedFillView {
    init(_ summary: PanelModel.Summary) {
        super.init(fill: .neutral)
        let tiles = [
            Self.metric("\(summary.count) 次", caption: "今天转码"),
            Self.metric(summary.media, caption: "转出的片子时长"),
            summary.failures == 0
                ? Self.metric("都顺利", caption: "没有失败")
                : Self.metric("\(summary.failures) 次", caption: "失败", color: .systemRed),
        ]
        let row = NSStackView(views: tiles)
        row.orientation = .horizontal
        row.distribution = .fillEqually
        row.alignment = .top
        row.spacing = 8
        pin(row, padding: 12)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    private static func metric(_ value: String, caption: String, color: NSColor = .labelColor) -> NSView {
        let valueLabel = PanelText.label(value, size: 18, weight: .semibold, color: color, monospacedDigits: true)
        let captionLabel = PanelText.label(caption, size: 10.5, color: .secondaryLabelColor)
        let column = NSStackView(views: [valueLabel, captionLabel])
        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = 1
        return column
    }
}

/// 今天还没有任务时的占位：说清楚「现在不用做什么」。
private final class EmptyStateView: RoundedFillView {
    init() {
        super.init(fill: .neutral)
        let icon = NSImageView(image: Symbols.image("checkmark.circle", pointSize: 20, weight: .light) ?? NSImage())
        icon.contentTintColor = .systemGreen
        let title = PanelText.label("准备就绪，等待任务", size: 13, weight: .medium, color: .labelColor)
        let detail = PanelText.wrapping(
            "有人用 movieclaw 播放需要转码的片子时，任务会自动派给这台 Mac。",
            size: 11, lines: 2, width: PanelView.contentWidth - 24, color: .secondaryLabelColor,
            alignment: .center
        )
        let column = NSStackView(views: [icon, title, detail])
        column.orientation = .vertical
        column.alignment = .centerX
        column.spacing = 4
        pin(column, padding: 14)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }
}

/// 「最近」列表：结果图标 + 片名 + 速度与时间。失败的原因在悬停提示里。
private final class HistoryView: NSView {
    init(rows: [PanelModel.HistoryRow]) {
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        let heading = PanelText.label("最近", size: 11, weight: .semibold, color: .secondaryLabelColor)
        var views: [NSView] = [heading]
        for row in rows {
            views.append(Self.row(row))
        }
        let column = NSStackView(views: views)
        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = 7
        for view in views.dropFirst() {
            view.widthAnchor.constraint(equalTo: column.widthAnchor).isActive = true
        }
        column.setCustomSpacing(8, after: heading)
        pin(column, padding: 0, insetX: 4)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    private static func row(_ row: PanelModel.HistoryRow) -> NSView {
        let symbol: String
        let tint: NSColor
        switch row.outcome {
        case .finished:
            symbol = "checkmark.circle.fill"
            tint = .systemGreen
        case .stopped:
            // 观众关了播放器、正常收尾：不是坏事，别用停止符号吓人
            symbol = "checkmark.circle"
            tint = .secondaryLabelColor
        case .failed:
            symbol = "xmark.octagon.fill"
            tint = .systemRed
        }
        let icon = NSImageView(image: Symbols.image(symbol, pointSize: 12, weight: .medium) ?? NSImage())
        icon.contentTintColor = tint
        icon.setContentHuggingPriority(.required, for: .horizontal)
        let title = PanelText.label(row.title, size: 12, color: .labelColor)
        title.lineBreakMode = .byTruncatingMiddle
        title.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let detail = PanelText.label(row.detail, size: 11, color: row.outcome == .failed ? .systemRed : .tertiaryLabelColor,
                                     monospacedDigits: true)
        detail.setContentCompressionResistancePriority(.required, for: .horizontal)
        let line = NSStackView(views: [icon, title, SettingsStyle.flexibleSpacer(), detail])
        line.orientation = .horizontal
        line.alignment = .centerY
        line.spacing = 7
        line.toolTip = row.tooltip
        return line
    }
}

// MARK: - 主体：提示卡片

/// 未配对、连接中、重连、出错、已断开时的主体：一句结论 + 原因 + 能直接点的动作。
private final class NoticeView: RoundedFillView {
    var onAction: ((PanelModel.Action) -> Void)?
    private var actions: [PanelModel.Action] = []

    init(notice: PanelModel.Notice) {
        let tint: NSColor
        switch notice.tone {
        case .neutral: tint = .secondaryLabelColor
        case .warning: tint = .systemOrange
        case .critical: tint = .systemRed
        }
        super.init(fill: notice.tone == .neutral ? .neutral : .tinted(tint))
        actions = notice.actions

        var head: [NSView] = []
        if notice.spinning {
            let spinner = NSProgressIndicator()
            spinner.style = .spinning
            spinner.controlSize = .small
            spinner.startAnimation(nil)
            head.append(spinner)
        } else {
            let icon = NSImageView(image: Symbols.image(notice.symbol, pointSize: 15, weight: .semibold) ?? NSImage())
            icon.contentTintColor = tint
            head.append(icon)
        }
        head.append(PanelText.wrapping(notice.title, size: 13, weight: .semibold, lines: 2,
                                       width: PanelView.contentWidth - 24 - 26))
        let headRow = NSStackView(views: head)
        headRow.orientation = .horizontal
        headRow.alignment = .centerY
        headRow.spacing = 8

        var rows: [NSView] = [headRow]
        if let detail = notice.detail, !detail.isEmpty {
            let label = PanelText.wrapping(detail, size: 11.5, lines: 4, width: PanelView.contentWidth - 24,
                                           color: .secondaryLabelColor)
            label.toolTip = detail
            rows.append(label)
        }
        if !notice.actions.isEmpty {
            let buttons = notice.actions.enumerated().map { index, action -> NSButton in
                let button = NSButton(title: action.title, target: self, action: #selector(clicked(_:)))
                button.tag = index
                button.bezelStyle = .rounded
                button.controlSize = .regular
                if index == 0 {
                    // 第一个是这张卡片最该点的那个，用强调色标出来
                    button.bezelColor = .controlAccentColor
                }
                return button
            }
            let actionRow = NSStackView(views: buttons)
            actionRow.orientation = .horizontal
            actionRow.spacing = 8
            rows.append(actionRow)
        }
        let column = NSStackView(views: rows)
        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = 8
        if !notice.actions.isEmpty, rows.count >= 2 {
            // 按钮与上面的文字多隔开一点，读完再点
            column.setCustomSpacing(12, after: rows[rows.count - 2])
        }
        pin(column, padding: 12)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    @objc private func clicked(_ sender: NSButton) {
        guard actions.indices.contains(sender.tag) else { return }
        onAction?(actions[sender.tag])
    }
}

// MARK: - 资源占用

/// CPU 与内存两张小图表（iStat Menus 那种），左右并排。
///
/// 每一栏：标题 + 右上角的大数字，中间是最近 2 分钟的面积图，下面一行整机对照。
/// CPU 图两层：灰色是整机、蓝色是转码，一眼看出 Mac 忙是不是转码在忙。
/// 常驻视图，就地更新（每 2 秒一次），不随主体重建。
private final class ResourceView: RoundedFillView {
    private let cpu = GaugeColumn(title: "转码 CPU")
    private let memory = GaugeColumn(title: "转码内存")

    init() {
        super.init(fill: .neutral)
        let row = NSStackView(views: [cpu, memory])
        row.orientation = .horizontal
        row.distribution = .fillEqually
        row.alignment = .top
        row.spacing = 14
        pin(row, padding: 10, insetX: 12)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func apply(_ resources: PanelModel.Resources) {
        cpu.apply(resources.cpu)
        memory.apply(resources.memory)
    }
}

private final class GaugeColumn: NSView {
    private let value = PanelText.label("", size: 12.5, weight: .semibold, color: .labelColor, monospacedDigits: true)
    private let detail = PanelText.label("", size: 10.5, color: .tertiaryLabelColor, monospacedDigits: true)
    private let chart = AreaChart()

    init(title: String) {
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        let label = PanelText.label(title, size: 11, weight: .semibold, color: .secondaryLabelColor)
        let head = NSStackView(views: [label, SettingsStyle.flexibleSpacer(), value])
        head.orientation = .horizontal
        head.alignment = .firstBaseline
        detail.lineBreakMode = .byTruncatingTail
        let column = NSStackView(views: [head, chart, detail])
        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = 4
        for view in [head, chart] {
            view.widthAnchor.constraint(equalTo: column.widthAnchor).isActive = true
        }
        pin(column, padding: 0, insetX: 0)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func apply(_ gauge: PanelModel.Resources.Gauge) {
        value.stringValue = gauge.value
        detail.stringValue = gauge.detail
        toolTip = gauge.tooltip
        chart.set(series: gauge.series, background: gauge.background)
    }
}

/// 面积图：最新的一格贴右边，采样不满 2 分钟时左边留空（和 iStat 一样从右往左长）。
private final class AreaChart: NSView {
    private var series: [Double] = []
    private var background: [Double]?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        translatesAutoresizingMaskIntoConstraints = false
        heightAnchor.constraint(equalToConstant: 30).isActive = true
        setAccessibilityElement(false)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func set(series: [Double], background: [Double]?) {
        guard series != self.series || background != self.background else { return }
        self.series = series
        self.background = background
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        let frame = NSBezierPath(roundedRect: bounds, xRadius: 5, yRadius: 5)
        NSColor.labelColor.translucent(0.05).setFill()
        frame.fill()
        NSGraphicsContext.saveGraphicsState()
        frame.addClip()
        if let background {
            area(background).fill(with: NSColor.labelColor.translucent(0.16))
        }
        let path = area(series)
        path.fill(with: NSColor.controlAccentColor.translucent(0.35))
        line(series).stroke(with: .controlAccentColor)
        NSGraphicsContext.restoreGraphicsState()
    }

    private func points(_ values: [Double]) -> [NSPoint] {
        let step = bounds.width / CGFloat(ResourceMonitor.capacity - 1)
        let offset = CGFloat(ResourceMonitor.capacity - values.count) * step
        return values.enumerated().map { index, value in
            NSPoint(x: offset + CGFloat(index) * step,
                    y: 1 + (bounds.height - 3) * CGFloat(min(1, max(0, value))))
        }
    }

    private func area(_ values: [Double]) -> NSBezierPath {
        let path = NSBezierPath()
        let points = points(values)
        guard let first = points.first, let last = points.last else { return path }
        path.move(to: NSPoint(x: first.x, y: 0))
        points.forEach(path.line)
        path.line(to: NSPoint(x: last.x, y: 0))
        path.close()
        return path
    }

    private func line(_ values: [Double]) -> NSBezierPath {
        let path = NSBezierPath()
        let points = points(values)
        guard let first = points.first else { return path }
        path.move(to: first)
        points.dropFirst().forEach(path.line)
        path.lineWidth = 1.2
        path.lineJoinStyle = .round
        return path
    }
}

private extension NSBezierPath {
    func fill(with color: NSColor) {
        color.setFill()
        fill()
    }

    func stroke(with color: NSColor) {
        color.setStroke()
        stroke()
    }
}

// MARK: - 小部件

/// 底栏的图标按钮：平时只有图标，悬停时浮出一块圆角底，和系统控制中心的按钮一个手感。
final class IconButton: NSButton {
    private let handler: (IconButton) -> Void
    private var hovering = false {
        didSet { needsDisplay = true }
    }

    init(symbol: String, tooltip: String, handler: @escaping (IconButton) -> Void) {
        self.handler = handler
        super.init(frame: NSRect(x: 0, y: 0, width: 28, height: 28))
        image = Symbols.image(symbol, pointSize: 14, weight: .regular, description: tooltip)
        imagePosition = .imageOnly
        isBordered = false
        contentTintColor = .secondaryLabelColor
        toolTip = tooltip
        setAccessibilityLabel(tooltip)
        target = self
        action = #selector(fire)
        translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            widthAnchor.constraint(equalToConstant: 28),
            heightAnchor.constraint(equalToConstant: 28),
        ])
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(
            rect: bounds, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self
        ))
    }

    override func mouseEntered(with event: NSEvent) { hovering = true }
    override func mouseExited(with event: NSEvent) { hovering = false }

    override func draw(_ dirtyRect: NSRect) {
        if hovering {
            NSColor.labelColor.translucent(0.1).setFill()
            NSBezierPath(roundedRect: bounds, xRadius: 7, yRadius: 7).fill()
        }
        super.draw(dirtyRect)
    }

    @objc private func fire() {
        handler(self)
    }
}

/// 圆角淡底。颜色在绘制时按当前外观解析，深浅色切换自动跟上。
class RoundedFillView: NSView {
    enum Fill {
        /// 中性：浅色下是 5% 的黑，深色下是 7% 的白。
        case neutral
        case tinted(NSColor)
    }

    static let cornerRadius: CGFloat = 12
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
            let dark = effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            color = NSColor.labelColor.translucent(dark ? 0.07 : 0.05)
        case let .tinted(tint):
            color = tint.translucent(0.13)
        }
        color.setFill()
        NSBezierPath(roundedRect: bounds, xRadius: Self.cornerRadius, yRadius: Self.cornerRadius).fill()
    }
}

extension NSView {
    /// 把内容铺进本视图，四周留白（`insetX` 缺省时左右也用 `padding`）。
    ///
    /// 内边距用显式约束，不依赖 `NSStackView.edgeInsets`：顶端对齐的横向 stack 在
    /// 垂直方向不计底部内边距，量出来的高度少一截，最后一行字贴着卡片底边。
    func pin(_ content: NSView, padding: CGFloat, insetX: CGFloat? = nil) {
        content.translatesAutoresizingMaskIntoConstraints = false
        addSubview(content)
        let x = insetX ?? padding
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: leadingAnchor, constant: x),
            content.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -x),
            content.topAnchor.constraint(equalTo: topAnchor, constant: padding),
            content.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -padding),
        ])
    }
}

/// 面板里文字的统一做法。
enum PanelText {
    static func label(
        _ text: String,
        size: CGFloat,
        weight: NSFont.Weight = .regular,
        color: NSColor,
        monospacedDigits: Bool = false
    ) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        // 进度数字每秒都在跳，比例字宽会让整行左右抖
        label.font = monospacedDigits
            ? .monospacedDigitSystemFont(ofSize: size, weight: weight)
            : .systemFont(ofSize: size, weight: weight)
        label.textColor = color
        return label
    }

    static func wrapping(
        _ text: String,
        size: CGFloat,
        weight: NSFont.Weight = .regular,
        lines: Int,
        width: CGFloat,
        color: NSColor = .labelColor,
        alignment: NSTextAlignment = .natural
    ) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = .systemFont(ofSize: size, weight: weight)
        label.textColor = color
        label.alignment = alignment
        label.maximumNumberOfLines = lines
        label.lineBreakMode = .byWordWrapping
        // 超出行数时最后一行末尾补省略号，而不是生硬地截掉半个字
        label.cell?.truncatesLastVisibleLine = true
        label.preferredMaxLayoutWidth = width
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return label
    }
}
