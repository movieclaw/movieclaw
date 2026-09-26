#!/usr/bin/env bash
# 从 BrandMark.swift 的矢量数据重新生成 Resources/AppIcon.icns。
#
# 标志只有一份数据（BrandMark.swift）：菜单栏图标、面板徽标在运行时直接画，
# App 图标用这个脚本离线画成 icns。改了标志之后跑一次、把新的 icns 一起提交。
#
# 版式按 macOS 图标网格：1024 画布里一块 824×824 的圆角方块（四周留 100 给阴影），
# 小尺寸（16/32）同样照画——标志是低多边形，缩小后依旧认得出。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

cat > "${WORK}/main.swift" <<'SWIFT'
import AppKit

let output = URL(fileURLWithPath: CommandLine.arguments[1])
let sizes: [(name: String, pixels: Int)] = [
    ("icon_16x16", 16), ("icon_16x16@2x", 32), ("icon_32x32", 32), ("icon_32x32@2x", 64),
    ("icon_128x128", 128), ("icon_128x128@2x", 256), ("icon_256x256", 256), ("icon_256x256@2x", 512),
    ("icon_512x512", 512), ("icon_512x512@2x", 1024),
]
for (name, pixels) in sizes {
    let side = CGFloat(pixels)
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8,
        samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
        bytesPerRow: 0, bitsPerPixel: 0
    )!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let body = NSRect(x: side * 100 / 1024, y: side * 100 / 1024, width: side * 824 / 1024, height: side * 824 / 1024)
    // 方块下的投影（macOS 图标网格的惯例：略向下、柔和）
    let shadow = NSShadow()
    shadow.shadowOffset = NSSize(width: 0, height: -side * 10 / 1024)
    shadow.shadowBlurRadius = side * 20 / 1024
    shadow.shadowColor = NSColor.black.withAlphaComponent(0.3)
    NSGraphicsContext.saveGraphicsState()
    shadow.set()
    NSColor.black.setFill()
    NSBezierPath(roundedRect: body, xRadius: body.width * 0.225, yRadius: body.width * 0.225).fill()
    NSGraphicsContext.restoreGraphicsState()
    BrandMark.drawTile(in: body)
    NSGraphicsContext.restoreGraphicsState()
    try! rep.representation(using: .png, properties: [:])!.write(to: output.appendingPathComponent("\(name).png"))
}
SWIFT

swiftc -O -o "${WORK}/render" \
    "${PROJECT_DIR}/Sources/MovieClawTranscoder/BrandMark.swift" "${WORK}/main.swift"
mkdir -p "${WORK}/AppIcon.iconset"
"${WORK}/render" "${WORK}/AppIcon.iconset"
iconutil -c icns "${WORK}/AppIcon.iconset" -o "${PROJECT_DIR}/Resources/AppIcon.icns"
echo "已生成：${PROJECT_DIR}/Resources/AppIcon.icns"
