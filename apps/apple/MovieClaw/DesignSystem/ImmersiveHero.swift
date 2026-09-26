import Nuke
import SwiftUI
import UIKit

// 沉浸式大图（Hero）的共用部件：订阅首页与发现页同一套轮播与图片处理机制，只有文字与按钮各自排。
//
// - `ImmersiveHeroBackdrop`：剧照层——慢速推近（Ken Burns）、上滑视差下沉、下半部压暗托字、
//   底部渐隐进页面氛围色、顶部压暗托住状态栏与顶栏；
// - `immersiveHeroRotation`：轮播计时——每张停留固定时长，指示器当前那格按时长填满，
//   手动切换重新计时，退到后台不推进；
// - `ImmersiveHeroIndicator`：指示器——当前格是会填满的长胶囊，其余是可点的小圆点；
// - `ImmersiveHeroAmbient` / `ImmersiveHeroAmbientColor`：页面氛围底色与取色；
// - `ImmersiveHeroScroll`：页面滚动距离，只给 Hero 与氛围底读，滚动时不重算整页。

/// 页面的连续滚动距离（向上为正）。放在可观察对象里而不是页面的 @State：
/// 页面主体不读它，只有 Hero（视差、淡出）与氛围底（随滚动退淡）读，滚动时只重算这两块。
@Observable
final class ImmersiveHeroScroll {
    var offset: CGFloat = 0
}

/// 沉浸 Hero 的剧照层。`active` 为当前正在展示的这张（切到它时从头推近），
/// `scrollOffset` 驱动视差（内容上滑 1 倍，画面只跟 0.6 倍）。
struct ImmersiveHeroBackdrop: View {
    let url: URL?
    let active: Bool
    let scrollOffset: CGFloat

    /// 慢速推近：切到这一张时从 1 开始，12 秒推到 1.1
    @State private var zoom: CGFloat = 1

    var body: some View {
        Color.clear
            .overlay {
                RemoteImage(url: url)
                    .scaleEffect(zoom)
            }
            // 下半部压暗托住文字。必须和剧照一起进下面的渐隐遮罩：压暗层若单独叠在遮罩外，
            // Hero 底边会比下面的氛围色暗一截，切出一道横线
            .overlay {
                LinearGradient(colors: [.clear, .black.opacity(0.5)], startPoint: UnitPoint(x: 0.5, y: 0.36), endPoint: .bottom)
            }
            .clipped()
            // 视差：内容上滑 1 倍，画面只跟 0.6 倍（相对下沉 0.4），景深感
            .offset(y: max(0, scrollOffset) * 0.4)
            // 底部渐隐进页面氛围色，而不是切一刀黑边
            .mask(LinearGradient(stops: [
                .init(color: .black, location: 0),
                .init(color: .black, location: 0.56),
                .init(color: .black.opacity(0.6), location: 0.8),
                .init(color: .clear, location: 1),
            ], startPoint: .top, endPoint: .bottom))
            // 顶部压暗托住状态栏、大标题与工具栏（Hero 顶到屏幕物理顶边，这里没有接缝问题）
            .overlay {
                LinearGradient(colors: [.black.opacity(0.5), .clear], startPoint: .top, endPoint: UnitPoint(x: 0.5, y: 0.26))
            }
            .onChange(of: active, initial: true) { _, isActive in
                var reset = Transaction()
                reset.disablesAnimations = true
                withTransaction(reset) { zoom = 1 }
                guard isActive else { return }
                withAnimation(.linear(duration: 12)) { zoom = 1.1 }
            }
    }
}

/// 轮播指示器：当前格是按 `fill`（0...1）填满的长胶囊，看得出「还有多久换下一张」；其余是可点的小圆点
struct ImmersiveHeroIndicator: View {
    let count: Int
    @Binding var index: Int
    let fill: CGFloat
    /// 小圆点的读屏标签（「切换到《片名》」）
    let label: (Int) -> String

    var body: some View {
        HStack(spacing: 6) {
            ForEach(0 ..< count, id: \.self) { offset in
                if offset == index {
                    Capsule()
                        .fill(Color.white.opacity(0.26))
                        .frame(width: 26, height: 5)
                        .overlay(alignment: .leading) {
                            Capsule().fill(Color.white.opacity(0.95)).frame(width: 26 * fill)
                        }
                        .clipShape(.capsule)
                } else {
                    Capsule()
                        .fill(Color.white.opacity(0.34))
                        .frame(width: 5, height: 5)
                        .contentShape(.rect.inset(by: -8))
                        .onTapGesture { withAnimation(.easeInOut(duration: 0.6)) { index = offset } }
                        .accessibilityLabel(label(offset))
                }
            }
        }
        .animation(.easeInOut(duration: 0.3), value: index)
    }
}

extension View {
    /// 轮播计时：每张停留 `interval` 秒，期间把 `fill` 线性推到 1，然后切下一张；
    /// 手动切换（index 变了）重新计时，退到后台不推进，张数变少时把越界的 index 拉回 0
    func immersiveHeroRotation(index: Binding<Int>, count: Int, fill: Binding<CGFloat>, interval: Double) -> some View {
        modifier(ImmersiveHeroRotation(index: index, count: count, fill: fill, interval: interval))
    }
}

private struct ImmersiveHeroRotation: ViewModifier {
    @Binding var index: Int
    let count: Int
    @Binding var fill: CGFloat
    let interval: Double

    @Environment(\.scenePhase) private var scenePhase

    func body(content: Content) -> some View {
        content
            .task(id: "\(index)-\(scenePhase == .active)-\(count)") {
                // index 作为任务标识：手动切换即重新计时；先无动画归零、让出一帧，再按轮播周期线性填满
                var reset = Transaction()
                reset.disablesAnimations = true
                withTransaction(reset) { fill = 0 }
                guard count > 1, scenePhase == .active else { return }
                await Task.yield()
                withAnimation(.linear(duration: interval)) { fill = 1 }
                try? await Task.sleep(for: .seconds(interval))
                guard !Task.isCancelled else { return }
                withAnimation(.easeInOut(duration: 0.8)) { index = (index + 1) % count }
            }
            .onChange(of: count) { _, newCount in
                if index >= newCount { index = 0 }
            }
    }
}

/// 页面底色：纯黑之上叠一层当前 Hero 剧照的主色，从顶部向下渐隐（Apple TV / Apple Music 的做法）。
///
/// 剧照底部渐隐进这层颜色，Hero 与下面的内容之间没有硬边；换下一张时颜色 1.2 秒交叉淡入。
/// 列表往下滚时整体退淡，不让下半页一直泡在颜色里。
struct ImmersiveHeroAmbient: View {
    let tint: Color?
    /// 列表滚动距离：滚得越深颜色越淡
    let scrollOffset: CGFloat

    var body: some View {
        ZStack {
            Theme.background
            if let tint {
                LinearGradient(stops: [
                    .init(color: tint.opacity(0.85), location: 0),
                    .init(color: tint.opacity(0.5), location: 0.42),
                    .init(color: tint.opacity(0.14), location: 0.72),
                    .init(color: .clear, location: 1),
                ], startPoint: .top, endPoint: .bottom)
                .id(tint.description)
                .transition(.opacity)
                .opacity(Double(max(0.35, 1 - max(0, scrollOffset) / 900)))
            }
        }
        .animation(.easeInOut(duration: 1.2), value: tint?.description)
        .ignoresSafeArea()
    }
}

/// 从剧照里取一个「能当底色」的主色：按饱和度加权求色相（灰黑白不参与），
/// 亮度统一压到深色档，保证上面的白字永远读得清。结果按地址缓存，轮播回到同一张不再计算。
@MainActor
enum ImmersiveHeroAmbientColor {
    private static var cache: [URL: Color] = [:]

    static func color(for url: URL) async -> Color? {
        if let cached = cache[url] { return cached }
        // 与 Hero 显示同一个地址：命中 Nuke 的内存 / 磁盘缓存，不会重复下载
        guard let image = try? await ImagePipeline.shared.image(for: url) else { return nil }
        let color = await Task.detached(priority: .utility) { dominant(of: image) }.value
        cache[url] = color
        return color
    }

    /// 缩到 24×24 取样：每个像素按「饱和度² ×（亮度 + 0.25）」加权，色相按单位圆求平均（避免红色 0/1 两端相消）
    nonisolated static func dominant(of image: UIImage) -> Color {
        let side = 24
        guard let cgImage = image.cgImage,
              let context = CGContext(
                  data: nil, width: side, height: side, bitsPerComponent: 8, bytesPerRow: side * 4,
                  space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
              )
        else { return fallback }
        context.interpolationQuality = .medium
        context.draw(cgImage, in: CGRect(x: 0, y: 0, width: side, height: side))
        guard let data = context.data?.bindMemory(to: UInt8.self, capacity: side * side * 4) else { return fallback }

        var x = 0.0, y = 0.0, saturation = 0.0, weightSum = 0.0
        for index in 0 ..< side * side {
            let r = Double(data[index * 4]) / 255, g = Double(data[index * 4 + 1]) / 255, b = Double(data[index * 4 + 2]) / 255
            let maxC = max(r, g, b), minC = min(r, g, b)
            let value = maxC
            let delta = maxC - minC
            guard value > 0.12, delta > 0.04 else { continue }
            let s = delta / maxC
            var hue: Double
            if maxC == r { hue = (g - b) / delta } else if maxC == g { hue = 2 + (b - r) / delta } else { hue = 4 + (r - g) / delta }
            hue /= 6
            if hue < 0 { hue += 1 }
            let weight = s * s * (value + 0.25)
            x += cos(hue * 2 * .pi) * weight
            y += sin(hue * 2 * .pi) * weight
            saturation += s * weight
            weightSum += weight
        }
        guard weightSum > 2 else { return fallback }
        var hue = atan2(y, x) / (2 * .pi)
        if hue < 0 { hue += 1 }
        let meanSaturation = saturation / weightSum
        return Color(hue: hue, saturation: min(0.72, max(0.28, meanSaturation * 1.1)), brightness: 0.44)
    }

    /// 灰调剧照（黑白片、夜景）：冷银灰，与 App 的银色强调色同一家族
    nonisolated static let fallback = Color(hue: 0.61, saturation: 0.14, brightness: 0.36)
}
