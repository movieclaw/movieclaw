import AppKit

/// movieclaw 的品牌标志（「转子」）：三片折纸般的刀锋顺时针旋成一圈，中间留出一个
/// 播放三角。与网页 `apps/web/public/movieclaw-logo-mark-rotor.png` 同一个图形。
///
/// 网页那张是位图；这里按它逐面描成矢量多边形（低多边形本来就是它的风格），
/// 于是菜单栏的单色图标、面板页眉的彩色徽标、App 图标都能从同一份数据任意倍率
/// 画出来，永远和网页一致。`scripts/render-app-icon.sh` 也是编译这个文件来生成
/// `Resources/AppIcon.icns` 的。
///
/// 坐标是描摹时的 525×525 像素空间（y 向下），绘制时再缩放进目标矩形。
enum BrandMark {
    /// 一个面：明暗档位 + 多边形顶点。
    struct Face {
        /// 0 最亮 … 4 最暗。光从左上方来，折纸的每个面按朝向分亮暗。
        let tone: Int
        let points: [CGPoint]

        init(_ tone: Int, _ points: [(CGFloat, CGFloat)]) {
            self.tone = tone
            self.points = points.map { CGPoint(x: $0.0, y: $0.1) }
        }
    }

    static let canvas: CGFloat = 525

    /// 三片刀锋、七个面。每片从三角的一个顶点附近细细地起头，贴着三角的一条边长宽，
    /// 最后在外圈收成一个朝顺时针方向的爪尖。
    static let faces: [Face] = [
        // 左片：上半亮面 + 下半暗面，爪尖在正上方偏左
        Face(0, [(171, 30), (146, 175), (146, 306), (30, 194), (78, 118)]),
        Face(3, [(30, 194), (146, 306), (146, 378), (165, 392), (232, 396), (222, 408), (170, 421),
                 (140, 420), (98, 402), (68, 378), (50, 330), (36, 262), (28, 210)]),
        // 上片：左端窄亮条 + 主面 + 右侧弯钩，钩尖在正右方
        Face(1, [(171, 30), (276, 31), (332, 39), (340, 46), (296, 53), (243, 66), (205, 80),
                 (190, 110), (158, 145), (146, 175)]),
        Face(3, [(340, 46), (372, 52), (398, 60), (386, 72), (380, 110), (372, 126), (358, 131),
                 (322, 138), (315, 147), (322, 158), (336, 177), (362, 205), (369, 214), (354, 205),
                 (328, 190), (302, 170), (270, 150), (236, 131), (200, 117), (190, 110), (205, 80),
                 (243, 66), (296, 53)]),
        Face(2, [(398, 60), (408, 62), (442, 124), (461, 152), (474, 184), (487, 216), (501, 248),
                 (490, 232), (466, 210), (446, 183), (426, 170), (407, 150), (380, 131), (372, 126),
                 (380, 110), (386, 72)]),
        // 下片：右半亮面 + 底部暗面，爪尖在左下
        Face(2, [(382, 222), (420, 236), (453, 262), (486, 288), (503, 308), (494, 340), (466, 367),
                 (433, 393), (400, 420), (381, 446), (361, 459), (338, 481), (350, 426), (360, 385),
                 (290, 376), (250, 374), (289, 354), (335, 328), (380, 301), (408, 275), (405, 248)]),
        Face(4, [(290, 376), (360, 385), (350, 426), (338, 481), (290, 488), (157, 488), (79, 466),
                 (131, 452), (184, 433), (210, 421), (225, 405), (232, 396), (262, 390)]),
    ]

    /// 片与片之间的分界。单色剪影里所有面都是同一种颜色，不沿这里切开一道细缝，
    /// 左片和下片会在小尺寸下粘成一坨，看不出是三片。左片与上片之间不切：那一刀
    /// 在 18pt 下像一道裂纹，而且上片本来就是从左片顺着转出去的。
    static let bladeSeams: [[CGPoint]] = [
        [(232, 396), (225, 405), (210, 421), (184, 433), (131, 452), (79, 466)],
    ].map { $0.map { CGPoint(x: $0.0, y: $0.1) } }

    /// 银蓝色阶，取自网页 logo。
    static let palette: [NSColor] = [
        NSColor(srgbRed: 0.97, green: 0.97, blue: 0.98, alpha: 1),
        NSColor(srgbRed: 0.86, green: 0.89, blue: 0.94, alpha: 1),
        NSColor(srgbRed: 0.80, green: 0.84, blue: 0.91, alpha: 1),
        NSColor(srgbRed: 0.62, green: 0.69, blue: 0.81, alpha: 1),
        NSColor(srgbRed: 0.44, green: 0.48, blue: 0.56, alpha: 1),
    ]

    /// 把标志画进 `rect`（取其中最大的正方形居中）。
    ///
    /// - Parameter template: true 时画成单色剪影（菜单栏模板图用），片与片之间切出细缝。
    static func draw(in rect: NSRect, template: Bool = false) {
        guard let context = NSGraphicsContext.current else { return }
        let side = min(rect.width, rect.height)
        let scale = side / canvas
        let origin = NSPoint(x: rect.midX - side / 2, y: rect.midY - side / 2)
        // 描摹坐标 y 向下；非翻转的上下文里要把 y 翻过来
        let flipped = context.isFlipped
        func map(_ p: CGPoint) -> NSPoint {
            NSPoint(x: origin.x + p.x * scale, y: flipped ? origin.y + p.y * scale : origin.y + side - p.y * scale)
        }
        func path(_ points: [CGPoint], close: Bool) -> NSBezierPath {
            let path = NSBezierPath()
            path.move(to: map(points[0]))
            points.dropFirst().forEach { path.line(to: map($0)) }
            if close { path.close() }
            path.lineJoinStyle = .round
            path.lineCapStyle = .round
            return path
        }

        context.saveGraphicsState()
        for face in faces {
            let shape = path(face.points, close: true)
            let color = template ? NSColor.black : palette[face.tone]
            color.setFill()
            shape.fill()
            // 相邻两个面各自抗锯齿，接缝处会漏出一丝底色；同色描一道细边盖住
            color.setStroke()
            shape.lineWidth = max(0.5, scale * 1.2)
            shape.stroke()
        }
        if template {
            context.compositingOperation = .clear
            for seam in bladeSeams {
                let cut = path(seam, close: false)
                // 缝宽约 1 个物理像素：再细会被抗锯齿抹平，再宽标志就散了
                cut.lineWidth = max(0.9, side * 0.045)
                cut.stroke()
            }
        }
        context.restoreGraphicsState()
    }

    /// 品牌方块：深蓝渐变圆角方块 + 彩色标志。面板页眉、设置「关于」页、App 图标都用它。
    static func drawTile(in rect: NSRect, cornerRatio: CGFloat = 0.225) {
        let radius = rect.width * cornerRatio
        let tile = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
        let gradient = NSGradient(colors: [
            NSColor(srgbRed: 0.14, green: 0.16, blue: 0.24, alpha: 1),
            NSColor(srgbRed: 0.04, green: 0.05, blue: 0.08, alpha: 1),
        ])
        gradient?.draw(in: tile, angle: -90)
        // 顶边一道极淡的高光，深色菜单与深色窗口里方块才不会糊进背景
        NSGraphicsContext.saveGraphicsState()
        tile.addClip()
        NSColor.white.withAlphaComponent(0.10).setStroke()
        tile.lineWidth = max(1, rect.width / 48)
        tile.stroke()
        NSGraphicsContext.restoreGraphicsState()
        draw(in: rect.insetBy(dx: rect.width * 0.17, dy: rect.height * 0.17))
    }
}
