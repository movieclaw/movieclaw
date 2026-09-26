import AppKit

/// 液态玻璃（Liquid Glass，macOS 26 起）设计语言的集中定义。
///
/// **系统控件不用管**：菜单、按钮、弹窗、窗口圆角在 macOS 26 SDK 编译、macOS 26
/// 运行时自动就是液态玻璃。这里只补我们自绘的部分，并给旧系统留一套视觉一致的
/// 退路——App 最低仍支持 macOS 12。
///
/// 三条约定：
/// - 玻璃只用在「浮在内容之上」的那一层（图标徽章这类点缀），内容本身保持不透明，
///   这是 HIG 对 Liquid Glass 的核心要求；菜单本身已是玻璃，菜单里的卡片用淡色
///   底，不再叠玻璃——玻璃叠玻璃会糊成一片；
/// - macOS 26 的 API 一律同时用 `#if compiler(>=6.2)`（Xcode 26 之前的工具链没有
///   这些符号，CI 换 runner 之前也得能编过）和 `#available(macOS 26, *)`（旧系统
///   跑到这里走退路）守住；
/// - 圆角跟着系统走：macOS 26 的窗口与控件更圆，自绘分组同步放大，保持同心。
enum Glass {
    /// 当前构建与运行环境能否用液态玻璃 API。
    static var isAvailable: Bool {
        #if compiler(>=6.2)
        if #available(macOS 26, *) { return true }
        #endif
        return false
    }

    /// 自绘圆角分组的圆角（设置窗的列表分组）。
    static var groupCornerRadius: CGFloat { isAvailable ? 12 : 9 }
}

/// Worker 连接状态的统一呈现：菜单与设置窗说同一套话、用同一套颜色。
///
/// 菜单栏里「已连接」这种说法不够用——用户点开菜单想知道的是「现在能不能转、
/// 在不在转」，所以这里的文案按用途改写（空闲 / 转码中 / 暂停接单），和
/// `WorkerConnectionState.displayName`（日志与诊断用的内部称呼）分开。
struct WorkerStatePresentation: Equatable {
    let title: String
    let color: NSColor

    static func make(_ state: WorkerConnectionState?, configured: Bool) -> WorkerStatePresentation {
        switch state {
        case .none:
            return configured
                ? WorkerStatePresentation(title: "未连接", color: .systemGray)
                : WorkerStatePresentation(title: "未配对", color: .systemGray)
        case .unconfigured:
            return WorkerStatePresentation(title: "未配对", color: .systemGray)
        case .starting:
            return WorkerStatePresentation(title: "启动中", color: .systemOrange)
        case .connecting:
            return WorkerStatePresentation(title: "连接中", color: .systemOrange)
        case .reconnecting:
            return WorkerStatePresentation(title: "等待重连", color: .systemOrange)
        case .ready:
            return WorkerStatePresentation(title: "空闲", color: .systemGreen)
        case .busy:
            return WorkerStatePresentation(title: "转码中", color: .systemBlue)
        case .paused:
            // NAS 让任务歇着是因为转码已经领先播放足够多，对用户来说仍是「在转码」
            return WorkerStatePresentation(title: "转码中", color: .systemBlue)
        case .draining:
            return WorkerStatePresentation(title: "暂停接单", color: .systemYellow)
        case .stopped:
            return WorkerStatePresentation(title: "已停止", color: .systemGray)
        case .error:
            return WorkerStatePresentation(title: "出错", color: .systemRed)
        }
    }
}

/// SF Symbols 的统一取法。菜单项、卡片、徽章都从这里拿，字重与字号一处说了算。
enum Symbols {
    static func image(
        _ name: String,
        pointSize: CGFloat = 13,
        weight: NSFont.Weight = .regular,
        description: String? = nil
    ) -> NSImage? {
        let configuration = NSImage.SymbolConfiguration(pointSize: pointSize, weight: weight)
        return NSImage(systemSymbolName: name, accessibilityDescription: description)?
            .withSymbolConfiguration(configuration)
    }
}

/// 界面上显示的文字格式化：ffmpeg 版本、转码位置。
enum DisplayText {
    /// `ffmpeg -version` 的首行是「ffmpeg version 7.1.4-Jellyfin Copyright (c)
    /// 2000-2026 the FFmpeg developers」，整行塞进菜单就是旧版菜单被撑得老宽的
    /// 原因之一。只取版本号那一段；认不出来时原样返回。
    static func ffmpegVersion(_ raw: String) -> String {
        let parts = raw.split(separator: " ")
        if let index = parts.firstIndex(of: "version"), index + 1 < parts.count {
            return String(parts[index + 1])
        }
        return raw
    }

    /// 转码位置（片内时间）：满一小时带小时位，否则分:秒。
    static func clock(milliseconds: Int64) -> String {
        let seconds = max(0, milliseconds / 1_000)
        if seconds >= 3_600 {
            return String(format: "%d:%02d:%02d", seconds / 3_600, seconds / 60 % 60, seconds % 60)
        }
        return String(format: "%d:%02d", seconds / 60, seconds % 60)
    }

    /// ffmpeg 进度里的 speed（「5.21x」「N/A」）→ 5.21；读不出来返回 nil。
    static func speedValue(_ raw: String?) -> Double? {
        guard let raw,
              let value = Double(raw.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: "x", with: "")),
              value > 0
        else { return nil }
        return value
    }

    /// 去掉 scheme 与末尾斜杠，只留主机和端口——用户认得的就是这一段。
    static func host(of address: String?) -> String? {
        guard var text = address?.trimmingCharacters(in: .whitespacesAndNewlines),
              !text.isEmpty
        else { return nil }
        for prefix in ["https://", "http://"] where text.lowercased().hasPrefix(prefix) {
            text.removeFirst(prefix.count)
        }
        while text.hasSuffix("/") {
            text.removeLast()
        }
        return text.isEmpty ? nil : text
    }
}

/// 状态胶囊：状态色的淡底 + 实心圆点 + 文字。
///
/// 文字用正常前景色、只让圆点和底色带状态色：绿、黄这类颜色直接当字色，在浅色
/// 背景上对比度不够，读起来发虚。
final class StatusPill: NSView {
    private let label = NSTextField(labelWithString: "")
    private var tint: NSColor = .systemGray
    private static let dotSide: CGFloat = 6
    private static let paddingX: CGFloat = 8

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        translatesAutoresizingMaskIntoConstraints = false
        label.font = .systemFont(ofSize: 11, weight: .medium)
        label.textColor = .labelColor
        label.translatesAutoresizingMaskIntoConstraints = false
        addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(
                equalTo: leadingAnchor, constant: Self.paddingX + Self.dotSide + 5
            ),
            label.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -Self.paddingX),
            label.centerYAnchor.constraint(equalTo: centerYAnchor),
            heightAnchor.constraint(equalToConstant: 20),
        ])
        setContentHuggingPriority(.required, for: .horizontal)
        setContentCompressionResistancePriority(.required, for: .horizontal)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func apply(_ presentation: WorkerStatePresentation) {
        label.stringValue = presentation.title
        setAccessibilityLabel("状态：\(presentation.title)")
        tint = presentation.color
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        let capsule = NSBezierPath(
            roundedRect: bounds, xRadius: bounds.height / 2, yRadius: bounds.height / 2
        )
        tint.translucent(0.16).setFill()
        capsule.fill()
        let dot = NSRect(
            x: Self.paddingX,
            y: bounds.midY - Self.dotSide / 2,
            width: Self.dotSide,
            height: Self.dotSide
        )
        tint.setFill()
        NSBezierPath(ovalIn: dot).fill()
    }
}

/// 圆角方块徽章：白色 SF Symbol 压在强调色底上（`.app` 例外，画品牌方块）。
///
/// macOS 26 起底是一块着了强调色的液态玻璃（`NSGlassEffectView`）；旧系统退回
/// 从上到下略微变深的强调色渐变，形状与尺寸完全一致。`glass: false` 时一律用
/// 渐变——放进面板时就这么用，面板本身已经是玻璃了。
final class GlyphTile: NSView {
    enum Glyph {
        /// App 自己的品牌方块（``BrandMark``），不套强调色与玻璃。
        case app
        case symbol(String)
    }

    private let side: CGFloat
    private let imageView = NSImageView()
    private let gradient = CAGradientLayer()
    private var usesGlass = false

    init(side: CGFloat, glyph: Glyph = .app, glass: Bool = true) {
        self.side = side
        super.init(frame: NSRect(x: 0, y: 0, width: side, height: side))
        translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            widthAnchor.constraint(equalToConstant: side),
            heightAnchor.constraint(equalToConstant: side),
        ])
        if case .app = glyph {
            // App 自己的标志是品牌方块（深蓝底 + 银蓝转子），和 Dock 里的 App 图标、网页 logo
            // 一模一样；不套强调色、也不叠玻璃——品牌色不该跟着系统强调色变
            let brand = BrandTileView(frame: bounds)
            brand.autoresizingMask = [.width, .height]
            addSubview(brand)
            return
        }
        imageView.contentTintColor = .white
        imageView.imageScaling = .scaleProportionallyUpOrDown
        set(glyph)

        let radius = (side * 0.26).rounded()
        #if compiler(>=6.2)
        if glass, #available(macOS 26, *) {
            usesGlass = true
            let effect = NSGlassEffectView(frame: bounds)
            effect.autoresizingMask = [.width, .height]
            effect.cornerRadius = radius
            effect.tintColor = .controlAccentColor
            effect.contentView = centered(imageView, in: effect.bounds)
            addSubview(effect)
            return
        }
        #endif
        wantsLayer = true
        layer?.cornerRadius = radius
        layer?.cornerCurve = .continuous
        layer?.masksToBounds = true
        gradient.frame = bounds
        gradient.autoresizingMask = [.layerWidthSizable, .layerHeightSizable]
        layer?.addSublayer(gradient)
        addSubview(centered(imageView, in: bounds))
        applyGradient()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func set(_ glyph: Glyph) {
        switch glyph {
        case .app:
            // 品牌方块在 init 里就画好了，不走图形层
            break
        case let .symbol(name):
            imageView.image = Symbols.image(name, pointSize: side * 0.42, weight: .semibold)
        }
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyGradient()
    }

    /// 把图形放进一个铺满的容器里居中，尺寸取徽章的六成。
    private func centered(_ view: NSView, in rect: NSRect) -> NSView {
        let container = NSView(frame: rect)
        container.autoresizingMask = [.width, .height]
        view.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(view)
        NSLayoutConstraint.activate([
            view.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            view.centerYAnchor.constraint(equalTo: container.centerYAnchor),
            view.widthAnchor.constraint(equalToConstant: side * 0.6),
            view.heightAnchor.constraint(equalToConstant: side * 0.6),
        ])
        return container
    }

    private func applyGradient() {
        guard !usesGlass else { return }
        // 目录颜色要在本视图的外观里解析，深浅色切换时才拿得到对的那一套
        effectiveAppearance.performAsCurrentDrawingAppearance {
            let accent = NSColor.controlAccentColor.usingColorSpace(.sRGB) ?? .systemBlue
            let top = accent.blended(withFraction: 0.18, of: .white) ?? accent
            let bottom = accent.blended(withFraction: 0.12, of: .black) ?? accent
            gradient.colors = [top.cgColor, bottom.cgColor]
        }
    }
}

/// 品牌方块视图：见 ``BrandMark/drawTile(in:cornerRatio:)``。
final class BrandTileView: NSView {
    override func draw(_ dirtyRect: NSRect) {
        BrandMark.drawTile(in: bounds)
    }
}

extension NSWindow {
    /// 透明标题栏 + 内容铺到标题栏下面，窗口页眉与红绿灯共处一行高度。
    ///
    /// 返回标题栏高度：内容要从这个高度往下排，否则会压到红绿灯。窗口标题照旧
    /// 设置（窗口列表、调度中心里要靠它认窗口），只是不画出来——页眉已经写了。
    @discardableResult
    func adoptGlassTitlebar() -> CGFloat {
        styleMask.insert(.fullSizeContentView)
        titlebarAppearsTransparent = true
        titleVisibility = .hidden
        isMovableByWindowBackground = true
        return max(0, frame.height - contentLayoutRect.height)
    }
}
