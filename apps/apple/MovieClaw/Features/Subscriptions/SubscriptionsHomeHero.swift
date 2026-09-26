import Nuke
import NukeUI
import SwiftUI
import UIKit

// MARK: - Hero

/// 订阅首页的沉浸 Hero：「下一部到手的」轮播。
///
/// 与发现页 Hero 刻意区分：发现页是编辑推荐（剧照 + 片名 + 简介 + 订阅键，左对齐），
/// 这里是**时间驱动**的——居中的片名 Logo 下面讲「几点能看」，大号细体时刻是主角，
/// 刚到的那张主按钮直接播放。
///
/// 视觉：剧照铺满并慢速推近（Ken Burns），上滑时视差下沉、文字淡出；底部渐隐进页面底色——
/// 底色取自当前这张剧照底边的颜色（同 Apple Music，见 `SubsHomeAmbient`），剧照像延伸成了整页。
/// 8 秒一张，指示器里当前那枚胶囊按 8 秒填满（看得出「还有多久换下一张」）；
/// 手动滑动后重新计时，退到后台不推进。
struct SubsHomeHero: View {
    /// Hero 高度（pt，从屏幕物理顶边算起）：比发现页（520）略低，首屏底部露出「刚刚入库」的标题，
    /// 暗示下面还有内容
    static let height: CGFloat = 500
    private static let interval: Double = 8

    let slides: [SubsHomeHeroSlide]
    /// 列表向上滚动的距离（下拉为负）：驱动视差与淡出
    let scrollOffset: CGFloat
    @Binding var index: Int

    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.api) private var api
    /// 指示器当前胶囊的填充进度 0...1
    @State private var fill: CGFloat = 0

    /// 预载下一张的剧照与 Logo：原图约 1MB，等轮到它才下载会闪一下空底；
    /// 只预载下一张而不是全部，蜂窝网络下不白烧流量
    private static let prefetcher = ImagePrefetcher()

    private var fade: Double { Double(max(0, min(1, 1 - scrollOffset / 260))) }

    var body: some View {
        TabView(selection: $index) {
            ForEach(Array(slides.enumerated()), id: \.element.id) { offset, slide in
                SubsHomeHeroSlideView(slide: slide, active: offset == index, scrollOffset: scrollOffset, fade: fade)
                    .tag(offset)
            }
        }
        .tabViewStyle(.page(indexDisplayMode: .never))
        .frame(height: Self.height)
        .overlay(alignment: .bottom) {
            if slides.count > 1 {
                indicator
                    .padding(.bottom, 16)
                    .opacity(fade)
            }
        }
        .task(id: "\(index)-\(scenePhase == .active)-\(slides.count)") {
            // index 作为任务标识：手动切换即重新计时；先无动画归零、让出一帧，再按轮播周期线性填满
            var reset = Transaction()
            reset.disablesAnimations = true
            withTransaction(reset) { fill = 0 }
            guard slides.count > 1, scenePhase == .active else { return }
            await Task.yield()
            withAnimation(.linear(duration: Self.interval)) { fill = 1 }
            try? await Task.sleep(for: .seconds(Self.interval))
            guard !Task.isCancelled else { return }
            withAnimation(.easeInOut(duration: 0.8)) { index = (index + 1) % slides.count }
        }
        .onChange(of: slides.count) { _, count in
            if index >= count { index = 0 }
        }
        .onChange(of: index, initial: true) { _, current in
            guard slides.count > 1 else { return }
            let next = slides[(current + 1) % slides.count].media
            let urls = [
                api.server.originalTMDBImageURL(next.backdropUrl) ?? api.image(next.posterUrl),
                api.image(next.logoUrl),
            ].compactMap { $0 }
            Self.prefetcher.startPrefetching(with: urls)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("subscriptions-hero")
    }

    private var indicator: some View {
        HStack(spacing: 6) {
            ForEach(slides.indices, id: \.self) { offset in
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
                        .accessibilityLabel("切换到《\(slides[offset].media.title)》")
                }
            }
        }
        .animation(.easeInOut(duration: 0.3), value: index)
    }
}

/// Hero 骨架：订阅清单还没到时占住 Hero 的位置，数据到达原位替换不跳版
struct SubsHomeHeroSkeleton: View {
    var body: some View {
        DiscoverSkeletonBlock(cornerRadius: 0)
            .frame(height: SubsHomeHero.height)
            .accessibilityLabel("订阅首页加载中")
    }
}

// MARK: - 单张

private struct SubsHomeHeroSlideView: View {
    let slide: SubsHomeHeroSlide
    let active: Bool
    let scrollOffset: CGFloat
    let fade: Double

    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    /// 慢速推近：切到这一张时从 1 开始，12 秒推到 1.1
    @State private var zoom: CGFloat = 1

    var body: some View {
        ZStack(alignment: .bottom) {
            backdrop
            content
        }
        .contentShape(.rect)
        .onTapGesture { router.push(.subscription(id: slide.subscriptionId)) }
        .onChange(of: active, initial: true) { _, isActive in
            var reset = Transaction()
            reset.disablesAnimations = true
            withTransaction(reset) { zoom = 1 }
            guard isActive else { return }
            withAnimation(.linear(duration: 12)) { zoom = 1.1 }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(accessibilityText)
    }

    /// 剧照换 TMDB 原图（Hero 把 16:9 横图放大裁切铺满竖向大区域，w1280 会糊，同发现页）；
    /// 没有剧照（老条目还没刷新到）退回海报铺满
    private var imageURL: URL? {
        api.server.originalTMDBImageURL(slide.media.backdropUrl) ?? api.image(slide.media.posterUrl)
    }

    private var backdrop: some View {
        Color.clear
            .overlay {
                RemoteImage(url: imageURL)
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
    }

    private var content: some View {
        VStack(spacing: 0) {
            titleArt
            if let clock = slide.clock {
                // 讲时间的：状态小圆点 + 一句说明，下面是大号细体时刻（粗细反差是这块的主要表情）
                statusLine(slide.clockLabel, keep: .head)
                    .padding(.top, 16)
                Text(clock)
                    // 时刻数字用大号细体；纯中文的词（「马上就好」「周四」）同样大小会压过片名 Logo，降一档用轻体
                    .font(.system(size: clock.contains(where: \.isNumber) ? 48 : 36, weight: clock.contains(where: \.isNumber) ? .thin : .light))
                    .monospacedDigit()
                    .foregroundStyle(.white)
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
                    .contentTransition(.numericText())
                    .padding(.top, 2)
            } else {
                statusLine(slide.detail, keep: .tail)
                    .padding(.top, 16)
                if let footnote = slide.footnote {
                    fitted(footnote, keep: .tail) { text in
                        text.font(.footnote).foregroundStyle(.white.opacity(0.6))
                    }
                    .padding(.top, 6)
                }
            }
            if let progress = slide.progress {
                SubsHomeProgressLine(value: progress, tint: SubsHomeTone.live.color)
                    .frame(width: 168)
                    .padding(.top, 12)
            }
            primaryButton
                .padding(.top, 20)
        }
        .multilineTextAlignment(.center)
        .padding(.horizontal, 28)
        .padding(.bottom, 44)
        .opacity(fade)
        .offset(y: max(0, scrollOffset) * 0.15)
    }

    /// Logo 下第一行：状态小圆点 + 一句说明。状态有用但不是重点（用户拍板：文字标签太重），
    /// 只留一颗点：绿 = 刚到 / 整理中，黄 = 等资源，蓝 = 下载中（呼吸），淡紫 = 今天更新；
    /// 平常状态（即将更新、追踪中）不放点。状态文字仍在读屏标签里
    private func statusLine(_ text: String?, keep: SubsHomeShortening) -> some View {
        HStack(spacing: 7) {
            if slide.eyebrow.tone != .calm {
                SubsHomeDot(tone: slide.eyebrow.tone, pulse: slide.eyebrow.pulse, size: 7)
            }
            if let text {
                fitted(text, keep: keep) { line in
                    line.font(.subheadline.weight(.semibold)).foregroundStyle(.white.opacity(0.9))
                }
            }
        }
    }

    /// 一行放不下就逐段收短（按「 · 」分段），整行永远不折行、不挤成省略号：
    /// 说明与补充行去尾（「S01E01 · 凶 · 好端端坏了起来」→「S01E01」），时刻上方的小字留尾（「S03E05 · 预计可看」→「预计可看」）
    private func fitted(_ text: String, keep: SubsHomeShortening, style: @escaping (Text) -> some View) -> some View {
        let options = keep.candidates(text)
        return ViewThatFits(in: .horizontal) {
            ForEach(options, id: \.self) { option in
                style(Text(option).monospacedDigit())
                    .lineLimit(1)
                    .fixedSize()
            }
            // 最短的写法仍放不下（极窄的屏）：截断兜底，不溢出
            style(Text(options.last ?? text).monospacedDigit())
                .lineLimit(1)
        }
    }

    /// 片名：有 Logo 用 Logo（透明底 PNG，不带派生预设请求以保住透明通道），没有或加载失败退回文字片名。
    /// 固定占一块 240×88 的框：各张高度一致，轮播时下面的文字不上下跳
    @ViewBuilder
    private var titleArt: some View {
        if let logo = slide.media.logoUrl, let url = api.image(logo) {
            LazyImage(url: url, transaction: Transaction(animation: .easeOut(duration: 0.25))) { state in
                if let image = state.image {
                    image.resizable()
                        .aspectRatio(contentMode: .fit)
                        .shadow(color: .black.opacity(0.45), radius: 14, y: 4)
                } else if state.error != nil {
                    titleText
                } else {
                    Color.clear
                }
            }
            .frame(maxWidth: 240, maxHeight: 88)
            .accessibilityHidden(true)
        } else {
            titleText
        }
    }

    private var titleText: some View {
        Text(slide.media.title)
            .font(.system(size: 34, weight: .bold))
            .tracking(-0.3)
            .foregroundStyle(.white)
            .lineLimit(2)
            .minimumScaleFactor(0.7)
            .shadow(color: .black.opacity(0.45), radius: 12, y: 3)
            .frame(maxWidth: 300)
            .accessibilityHidden(true)
    }

    @ViewBuilder
    private var primaryButton: some View {
        if let play = slide.play {
            Button {
                router.play(play)
            } label: {
                Label(slide.resumePercent == nil ? "播放" : "继续播放", systemImage: "play.fill")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(.black)
                    .padding(.horizontal, 30)
                    .frame(height: 46)
                    .background(.white, in: .capsule)
                    .contentShape(.capsule)
            }
            .buttonStyle(SubsHomePressStyle())
            .accessibilityLabel("播放《\(slide.media.title)》\(slide.detail.map { " \($0)" } ?? "")")
            .accessibilityIdentifier("hero-play")
        } else {
            Button {
                router.push(.subscription(id: slide.subscriptionId))
            } label: {
                Text("查看订阅")
                    .font(.body.weight(.semibold))
                    .padding(.horizontal, 12)
                    .frame(height: 30)
            }
            .buttonStyle(.glass)
            .controlSize(.large)
            .accessibilityLabel("查看《\(slide.media.title)》的订阅")
            .accessibilityIdentifier("hero-detail")
        }
    }

    private var accessibilityText: String {
        var parts = ["《\(slide.media.title)》", slide.eyebrow.text]
        if let label = slide.clockLabel { parts.append(label) }
        if let clock = slide.clock { parts.append(clock) }
        if let detail = slide.detail { parts.append(detail) }
        if let footnote = slide.footnote { parts.append(footnote) }
        return parts.joined(separator: "，")
    }
}

// MARK: - 小部件

/// 一行文字按「 · 」分段收短的方向
enum SubsHomeShortening {
    /// 保留开头、去掉结尾（说明 / 补充行：先舍集名，再舍后半句）
    case tail
    /// 保留结尾（时刻上方的小字：「预计可看」是大号时刻的注解，集号可以舍）
    case head

    /// 从长到短的候选（去重），ViewThatFits 按顺序挑第一个放得下的
    func candidates(_ text: String) -> [String] {
        let parts = text.components(separatedBy: " · ")
        guard parts.count > 1 else { return [text] }
        var options = [text]
        switch self {
        case .tail:
            for count in stride(from: parts.count - 1, through: 1, by: -1) {
                options.append(parts.prefix(count).joined(separator: " · "))
            }
        case .head:
            for count in stride(from: parts.count - 1, through: 1, by: -1) {
                options.append(parts.suffix(count).joined(separator: " · "))
            }
        }
        var seen = Set<String>()
        return options.filter { seen.insert($0).inserted }
    }
}

/// 状态小圆点：「正在发生」的两档带柔光，下载中 / 整理中再加呼吸
struct SubsHomeDot: View {
    let tone: SubsHomeTone
    var pulse = false
    var size: CGFloat = 6

    var body: some View {
        let dot = Circle()
            .fill(tone.color)
            .frame(width: size, height: size)
            .shadow(color: tone.glows ? tone.color.opacity(0.75) : .clear, radius: size * 0.6)
            .accessibilityHidden(true)
        if pulse {
            dot.phaseAnimator([1.0, 0.3]) { view, phase in
                view.opacity(phase)
            } animation: { _ in
                .easeInOut(duration: 0.9)
            }
        } else {
            dot
        }
    }
}

/// 发丝进度线：底槽 + 带柔光的进度段（下载进度、海报收录、续播进度共用）
struct SubsHomeProgressLine: View {
    let value: Double
    var tint: Color = .white
    var height: CGFloat = 3

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(0.2))
                Capsule()
                    .fill(tint)
                    .frame(width: max(height, proxy.size.width * CGFloat(min(max(value, 0), 1))))
                    .shadow(color: tint.opacity(0.6), radius: 4)
            }
        }
        .frame(height: height)
        .accessibilityHidden(true)
    }
}

/// 卡片按下时轻微缩小（弹簧回弹）：比系统默认的变暗更有「按到了实物」的手感
struct SubsHomePressStyle: ButtonStyle {
    var scale: CGFloat = 0.96

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? scale : 1)
            .animation(.spring(response: 0.28, dampingFraction: 0.72), value: configuration.isPressed)
    }
}

// MARK: - 氛围色

/// 页面底色（同 Apple Music 专辑页）：整页铺当前那张剧照**底边**的颜色，剧照底部渐隐进去，
/// 看起来像剧照自己延伸成了整页；往下只轻微加深一点做层次，不再渐隐成黑、滚动也不变淡。
/// 没有 Hero（无订阅 / 非沉浸）时是纯黑。换张时颜色 1.2 秒过渡。
struct SubsHomeAmbient: View {
    let tint: Color?
    /// 列表滚动距离（保留参数：曾用于滚深变淡，现在整页恒定铺色）
    let scrollOffset: CGFloat

    var body: some View {
        ZStack {
            Theme.background
            if let tint {
                LinearGradient(stops: [
                    .init(color: tint, location: 0),
                    .init(color: tint, location: 0.55),
                    .init(color: tint.mix(with: .black, by: 0.35), location: 1),
                ], startPoint: .top, endPoint: .bottom)
                .id(tint.description)
                .transition(.opacity)
            }
        }
        .animation(.easeInOut(duration: 1.2), value: tint?.description)
        .ignoresSafeArea()
    }
}

/// 从剧照里取页面底色：**底边那一条的平均色**（Apple Music 的做法），而不是全图主色——
/// 剧照底部要无缝融进这个颜色，取别处的颜色就会在交界处看出一道色差。
///
/// - 只取屏幕上真正露出来的部分：Hero 是竖向大区域，横向剧照按高度铺满、左右裁掉，
///   所以横向只取中间 45%，纵向取最底下 12%；
/// - 亮度按 Hero 底部压暗后的观感换算（剧照底部本来就在渐隐里，交界处自然过渡），
///   封顶 0.5，白字永远读得清；饱和度略提，避免发闷。结果按地址缓存，轮播回到同一张不再计算。
@MainActor
enum SubsHomeAmbientColor {
    private static var cache: [URL: Color] = [:]

    static func color(for url: URL) async -> Color? {
        if let cached = cache[url] { return cached }
        // 与 Hero 显示同一个地址：命中 Nuke 的内存 / 磁盘缓存，不会重复下载
        guard let image = try? await ImagePipeline.shared.image(for: url) else { return nil }
        let color = await Task.detached(priority: .utility) { bottomEdge(of: image) }.value
        cache[url] = color
        return color
    }

    /// 缩到 48×48 后取底部 6 行 × 中间 22 列求平均，再换算成页面底色
    nonisolated static func bottomEdge(of image: UIImage) -> Color {
        let side = 48
        guard let cgImage = image.cgImage,
              let context = CGContext(
                  data: nil, width: side, height: side, bitsPerComponent: 8, bytesPerRow: side * 4,
                  space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
              )
        else { return fallback }
        context.interpolationQuality = .medium
        context.draw(cgImage, in: CGRect(x: 0, y: 0, width: side, height: side))
        guard let data = context.data?.bindMemory(to: UInt8.self, capacity: side * side * 4) else { return fallback }

        // 位图内存按行自上而下：最后 6 行就是图片底边
        let rows = (side - 6) ..< side
        let columns = (side - 22) / 2 ..< (side + 22) / 2
        var r = 0.0, g = 0.0, b = 0.0, count = 0.0
        for row in rows {
            for column in columns {
                let index = (row * side + column) * 4
                r += Double(data[index]); g += Double(data[index + 1]); b += Double(data[index + 2])
                count += 1
            }
        }
        guard count > 0 else { return fallback }
        let edge = UIColor(red: r / count / 255, green: g / count / 255, blue: b / count / 255, alpha: 1)
        var hue: CGFloat = 0, saturation: CGFloat = 0, brightness: CGFloat = 0, alpha: CGFloat = 0
        edge.getHue(&hue, saturation: &saturation, brightness: &brightness, alpha: &alpha)
        // 色相保持剧照原样；亮度按 Hero 底部压暗后的观感换算并封顶 0.5（白字读得清），
        // 饱和度略提一档——压暗后的颜色会发闷，Apple Music 的底色是「亮而不刺眼」的那一档
        return Color(
            hue: hue,
            saturation: min(1, saturation * 1.15),
            brightness: max(0.08, min(0.5, brightness * 0.72))
        )
    }

    /// 取不到图时的底色：冷银灰，与 App 的银色强调色同一家族
    nonisolated static let fallback = Color(hue: 0.61, saturation: 0.14, brightness: 0.2)
}
