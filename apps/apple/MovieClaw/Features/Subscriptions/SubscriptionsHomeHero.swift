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
/// 视觉：剧照铺满并慢速推近（Ken Burns），上滑时视差下沉、文字淡出；底部渐隐进页面的
/// 氛围色（当前这张剧照的主色，见 `ImmersiveHeroAmbient`），整页像被这部作品的光照着。
/// 剧照层、轮播计时与指示器是与发现页共用的沉浸 Hero 组件（DesignSystem/ImmersiveHero.swift）。
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
        .immersiveHeroRotation(index: $index, count: slides.count, fill: $fill, interval: Self.interval)
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
        ImmersiveHeroIndicator(count: slides.count, index: $index, fill: fill) { "切换到《\(slides[$0].media.title)》" }
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

    var body: some View {
        ZStack(alignment: .bottom) {
            backdrop
            content
        }
        .contentShape(.rect)
        .onTapGesture { router.push(.subscription(id: slide.subscriptionId)) }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(accessibilityText)
    }

    /// 剧照换 TMDB 原图（Hero 把 16:9 横图放大裁切铺满竖向大区域，w1280 会糊，同发现页）；
    /// 没有剧照（老条目还没刷新到）退回海报铺满
    private var imageURL: URL? {
        api.server.originalTMDBImageURL(slide.media.backdropUrl) ?? api.image(slide.media.posterUrl)
    }

    private var backdrop: some View {
        ImmersiveHeroBackdrop(url: imageURL, active: active, scrollOffset: scrollOffset)
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
