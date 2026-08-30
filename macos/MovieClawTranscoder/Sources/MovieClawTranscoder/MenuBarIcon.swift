import AppKit

/// 菜单栏图标（docs/design/device-auth.md §5.1）。
///
/// **不是把 App logo 缩小**，而是照 logo 的构成——三片光圈叶子 + 中心播放
/// 三角——按 18pt 重新排的尺度。原 logo 的三片叶子靠明暗渐变分离，压成单色
/// 剪影后会粘成一坨，18px 下认不出来；这里把叶子之间留成真实缺口，才站得住。
/// Apple 自家也是这么做的：菜单栏图标单独设计，不是缩小的 App 图标。
///
/// 用代码画而不是打包图片资源：不到 40 行、任意倍率都清晰，也省掉一套
/// asset catalog 与 bundle 查找的失败模式（`swift run` 不在 .app 里时尤其）。
enum MenuBarIcon {
    /// 菜单栏可用高度约 22pt，图标画到 18pt 留出上下呼吸位。
    private static let side: CGFloat = 18

    /// 生成模板图。模板图只用 alpha，由系统按浅色/深色菜单栏和高亮态自动着色，
    /// 这是菜单栏图标唯一正确的形态——写死颜色的图在另一种外观下会看不见。
    static func statusItemImage() -> NSImage {
        let image = NSImage(size: NSSize(width: side, height: side), flipped: false) { rect in
            draw(in: rect)
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = "MovieClaw Transcoder"
        return image
    }

    private static func draw(in rect: NSRect) {
        let center = NSPoint(x: rect.midX, y: rect.midY)
        let unit = min(rect.width, rect.height)
        let outer = unit * 0.46
        let inner = unit * 0.315
        // 每片叶子占 120° 中的 94°，留 26° 缺口——缺口小于这个数三片会糊在一起，
        // 大于这个数环就散了，18px 下两边都试过
        let span: CGFloat = 94

        NSColor.black.setFill()
        for index in 0..<3 {
            let start = CGFloat(index) * 120 - 90 + (120 - span) / 2
            let blade = NSBezierPath()
            blade.appendArc(withCenter: center, radius: outer,
                            startAngle: start, endAngle: start + span)
            blade.appendArc(withCenter: center, radius: inner,
                            startAngle: start + span, endAngle: start, clockwise: true)
            blade.close()
            blade.fill()
        }

        // 中心播放三角：logo 的负空间元素，这里画成实心才在小尺寸下读得出
        let half = unit * 0.115
        let triangle = NSBezierPath()
        // 稍微右移，让三角的视觉重心落在圆心上（三角形的形心偏左）
        let tip = NSPoint(x: center.x + half * 1.25, y: center.y)
        triangle.move(to: NSPoint(x: center.x - half * 0.55, y: center.y + half * 1.2))
        triangle.line(to: tip)
        triangle.line(to: NSPoint(x: center.x - half * 0.55, y: center.y - half * 1.2))
        triangle.close()
        triangle.fill()
    }
}
