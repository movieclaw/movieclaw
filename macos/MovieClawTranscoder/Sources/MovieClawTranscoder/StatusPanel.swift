import AppKit

/// 从菜单栏图标下方弹出的浮动面板，替代原来的下拉菜单。
///
/// 为什么不用 `NSMenu`：菜单只能一行一行往下排，顶上塞一张卡片已是上限，进度条、
/// 开关、统计这些都放不进去；为什么不用 `NSPopover`：它带一个指向图标的小箭头，
/// 和系统控制中心那种「贴着菜单栏展开的一块玻璃」不是一个样子。
///
/// 行为照系统控制中心对齐：
/// - 无边框、不激活 App（`.nonactivatingPanel`）：点开面板不会把别的 App 的窗口压到后面；
/// - 能成为 key window：Esc 关闭、⌘, 打开设置、⌘Q 退出都在这里接住；
/// - 失去 key（点了别处）或在别的 App 里点鼠标就收起；
/// - macOS 26 起背景是液态玻璃（`NSGlassEffectView`），旧系统退回 popover 材质的毛玻璃。
@MainActor
final class StatusPanel: NSPanel {
    /// 面板被收起（不论什么原因）时回调，菜单栏按钮据此取消高亮。
    var onDismiss: (() -> Void)?
    var onOpenSettings: (() -> Void)?
    var onQuit: (() -> Void)?

    static let cornerRadius: CGFloat = 18
    private let content: NSView
    private var outsideClickMonitor: Any?

    init(content: NSView) {
        self.content = content
        super.init(
            contentRect: NSRect(origin: .zero, size: content.frame.size),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: true
        )
        isFloatingPanel = true
        level = .statusBar
        hasShadow = true
        backgroundColor = .clear
        isOpaque = false
        isMovable = false
        hidesOnDeactivate = false
        isReleasedWhenClosed = false
        animationBehavior = .none
        // 跟着当前桌面走、不进 ⌘Tab 与窗口循环、全屏 App 上也能弹出
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient, .ignoresCycle]
        contentView = Self.makeBackground(for: content)
    }

    override var canBecomeKey: Bool { true }

    /// 在菜单栏按钮正下方展开。水平方向以按钮为中心，碰到屏幕边就往里收。
    func show(below button: NSStatusBarButton) {
        guard let buttonWindow = button.window else { return }
        let anchor = buttonWindow.convertToScreen(button.convert(button.bounds, to: nil))
        let screen = buttonWindow.screen ?? NSScreen.main
        let size = content.frame.size
        var x = anchor.midX - size.width / 2
        if let visible = screen?.visibleFrame {
            x = min(max(x, visible.minX + 8), visible.maxX - size.width - 8)
        }
        setFrame(NSRect(x: x, y: anchor.minY - 6 - size.height, width: size.width, height: size.height), display: true)
        invalidateShadow()

        let animate = !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        alphaValue = animate ? 0 : 1
        makeKeyAndOrderFront(nil)
        if animate {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.14
                animator().alphaValue = 1
            }
        }
        // 在别的 App 里点鼠标时本面板收不到任何事件（key 也未必会变），用全局监听兜底
        outsideClickMonitor = NSEvent.addGlobalMonitorForEvents(
            matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown]
        ) { [weak self] _ in
            Task { @MainActor in self?.dismiss() }
        }
    }

    func dismiss() {
        guard isVisible else { return }
        if let monitor = outsideClickMonitor {
            NSEvent.removeMonitor(monitor)
            outsideClickMonitor = nil
        }
        orderOut(nil)
        onDismiss?()
    }

    /// 内容高度变了（状态切换、任务增减）就跟着改，**顶边不动**：面板挂在菜单栏下面，
    /// 往上长会顶进菜单栏里。
    func fitToContent() {
        let size = content.frame.size
        var frame = self.frame
        guard abs(frame.height - size.height) > 0.5 || abs(frame.width - size.width) > 0.5 else { return }
        frame.origin.y += frame.height - size.height
        frame.size = size
        setFrame(frame, display: true)
        invalidateShadow()
    }

    override func resignKey() {
        super.resignKey()
        dismiss()
    }

    override func cancelOperation(_ sender: Any?) {
        dismiss()
    }

    /// 菜单栏 App 没有主菜单，⌘, 与 ⌘Q 不会自动生效，在面板这里接住。
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        guard flags == .command else { return super.performKeyEquivalent(with: event) }
        switch event.charactersIgnoringModifiers {
        case ",":
            dismiss()
            onOpenSettings?()
            return true
        case "q":
            onQuit?()
            return true
        case "w":
            dismiss()
            return true
        default:
            return super.performKeyEquivalent(with: event)
        }
    }

    /// 面板背景：macOS 26 起是液态玻璃，旧系统是 popover 材质的毛玻璃，圆角一致。
    ///
    /// 玻璃**不能直接当窗口的 contentView**：那样圆角以外的四个角并不是全透明，
    /// 而是留着一层约 5% 的黑，窗口服务器据此把整个矩形当成窗口本体，沿直角画描边
    /// 和阴影——面板展开后四个角各多出一块黑色直角（让 App 自己用
    /// CGWindowListCreateImage 截自己的面板窗口实测：改前角上 alpha≈0.05，改后为 0）。
    /// 放进一个透明容器里，容器再按同样的圆角裁一刀，角外就是真正透明的了。
    private static func makeBackground(for content: NSView) -> NSView {
        content.autoresizingMask = [.width, .height]
        #if compiler(>=6.2)
        if #available(macOS 26, *) {
            let container = NSView(frame: content.frame)
            container.wantsLayer = true
            container.layer?.backgroundColor = NSColor.clear.cgColor
            container.layer?.cornerRadius = cornerRadius
            container.layer?.cornerCurve = .continuous
            container.layer?.masksToBounds = true
            let glass = NSGlassEffectView(frame: container.bounds)
            glass.autoresizingMask = [.width, .height]
            glass.cornerRadius = cornerRadius
            glass.contentView = content
            container.addSubview(glass)
            return container
        }
        #endif
        let effect = NSVisualEffectView(frame: content.frame)
        effect.material = .popover
        effect.blendingMode = .behindWindow
        effect.state = .active
        effect.maskImage = roundedMask(radius: cornerRadius)
        content.frame = effect.bounds
        effect.addSubview(content)
        return effect
    }

    /// 可拉伸的圆角蒙版（九宫格：四角固定、中间拉伸）。
    private static func roundedMask(radius: CGFloat) -> NSImage {
        let side = radius * 2 + 1
        let image = NSImage(size: NSSize(width: side, height: side), flipped: false) { rect in
            NSColor.black.setFill()
            NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius).fill()
            return true
        }
        image.capInsets = NSEdgeInsets(top: radius, left: radius, bottom: radius, right: radius)
        image.resizingMode = .stretch
        return image
    }
}
