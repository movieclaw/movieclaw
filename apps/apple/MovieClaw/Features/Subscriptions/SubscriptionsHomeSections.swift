import NukeUI
import SwiftUI

// 订阅首页 Hero 以下的三种版块：刚刚入库（16:9 剧照横滑）、日程（日期条 + 当天议程）、
// 剧集 / 电影海报行。三种形状刻意不同——横卡、竖列、竖海报——一页里有节奏，
// 而不是同一种横滑行一排排往下堆。

// MARK: - 版块标题

/// 版块标题：粗体标题（可点时带「›」压栈到二级页）+ 右侧一句弱化的计数
struct SubsHomeSectionHeader: View {
    let title: String
    var trailing: String?
    /// 可点时按钮的 UI 测试标识
    var actionIdentifier: String?
    var action: (() -> Void)?

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            if let action {
                Button(action: action) {
                    HStack(alignment: .firstTextBaseline, spacing: 5) {
                        Text(title)
                        Image(systemName: "chevron.right")
                            .font(.system(size: 14, weight: .bold))
                            .foregroundStyle(Theme.textFaint)
                    }
                    .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .accessibilityHint("查看全部")
                .accessibilityIdentifier(actionIdentifier ?? "section-more")
            } else {
                Text(title)
            }
            Spacer(minLength: 8)
            if let trailing {
                Text(trailing)
                    .font(.footnote)
                    .monospacedDigit()
                    .foregroundStyle(Theme.textFaint)
            }
        }
        .font(.title3.weight(.bold))
        .foregroundStyle(Theme.text)
        .padding(.horizontal, Theme.pagePadding)
    }
}

// MARK: - 刚刚入库

/// 「刚刚入库」：订阅的回报时刻。一部作品一张 16:9 剧照卡，点一下直接播放（入口是这一批里
/// 第一个没看完的单元）；看完的作品服务端不再返回，这一行自然消失。
struct SubsHomeRecentRow: View {
    let cards: [API.RecentArrivalView]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SubsHomeSectionHeader(title: "刚刚入库", trailing: cards.count > 1 ? "\(cards.count) 部" : nil)
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(alignment: .top, spacing: 14) {
                    ForEach(cards, id: \.subscriptionId) { card in
                        SubsHomeRecentCard(card: card)
                    }
                }
                .scrollTargetLayout()
            }
            .contentMargins(.horizontal, Theme.pagePadding, for: .scrollContent)
            .scrollTargetBehavior(.viewAligned)
            .scrollClipDisabled()
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("recent-arrivals")
    }
}

private struct SubsHomeRecentCard: View {
    let card: API.RecentArrivalView
    @Environment(\.api) private var api
    @Environment(Router.self) private var router

    private static let width: CGFloat = 264
    private var isTV: Bool { card.media.kind == "tv" }

    /// 第三行：什么时候到的、这一批还有几集、看到哪了
    private var note: String {
        var parts: [String] = []
        if let date = SubscriptionsHome.importedAt(card) { parts.append("\(Formatters.fromNow(date))入库") }
        if card.units.count > 1 { parts.append("共 \(card.units.count) 集新内容") }
        if let percent = card.progressPercent { parts.append("看到 \(percent)%") }
        return parts.joined(separator: " · ")
    }

    var body: some View {
        Button {
            router.play(SubscriptionsHome.playRequest(card))
        } label: {
            VStack(alignment: .leading, spacing: 0) {
                artwork
                VStack(alignment: .leading, spacing: 2) {
                    Text(card.media.title)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.text)
                    Text(SubscriptionsHome.recentDetail(card))
                        .font(.footnote)
                        .monospacedDigit()
                        .foregroundStyle(Theme.textMuted)
                    if !note.isEmpty {
                        Text(note)
                            .font(.caption)
                            .monospacedDigit()
                            .foregroundStyle(Theme.textFaint)
                    }
                }
                .lineLimit(1)
                .padding(.top, 9)
            }
            .frame(width: Self.width, alignment: .leading)
            .contentShape(.rect)
        }
        .buttonStyle(SubsHomePressStyle())
        .contextMenu {
            Button("播放", systemImage: "play.fill") { router.play(SubscriptionsHome.playRequest(card)) }
            Button("查看订阅详情", systemImage: "list.bullet.rectangle") { router.push(.subscription(id: card.subscriptionId)) }
            Button("查看影片详情", systemImage: "info.circle") {
                router.push(.mediaDetail(titleRef: "tmdb:\(card.media.kind):\(card.media.tmdbId)"))
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("播放《\(card.media.title)》，\(SubscriptionsHome.recentDetail(card))，\(note)")
        .accessibilityAddTraits(.isButton)
        .accessibilityIdentifier("recent-card")
    }

    private var artwork: some View {
        // 剧集用这一集的剧照；电影和缺剧照的集用作品剧照，再没有退回海报
        Color.clear
            .aspectRatio(16 / 9, contentMode: .fit)
            .overlay {
                RemoteImage(url: api.image(card.stillUrl ?? card.media.backdropUrl ?? card.media.posterUrl, .landscapeCard))
            }
            .overlay(alignment: .bottom) {
                LinearGradient(colors: [.clear, .black.opacity(0.72)], startPoint: .top, endPoint: .bottom)
                    .frame(height: 76)
            }
            .overlay(alignment: .bottomLeading) { logo.padding(12) }
            .overlay(alignment: .bottomTrailing) {
                Image(systemName: "play.fill")
                    .font(.system(size: 13, weight: .bold))
                    .foregroundStyle(.white)
                    .frame(width: 36, height: 36)
                    .glassEffect(.regular, in: .circle)
                    .padding(10)
            }
            .overlay(alignment: .topLeading) {
                if card.units.count > 1 {
                    SubsHomeChipView(chip: SubsHomeChip(text: "新 \(card.units.count) 集", tone: .ok))
                        .padding(9)
                }
            }
            .overlay(alignment: .bottom) {
                if let percent = card.progressPercent {
                    SubsHomeProgressLine(value: Double(percent) / 100, height: 3)
                }
            }
            .clipShape(.rect(cornerRadius: 14, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Color.white.opacity(0.08)))
            .shadow(color: .black.opacity(0.35), radius: 12, y: 6)
    }

    /// 卡片左下角的小号片名 Logo（没有就不画，片名在卡片下面）
    @ViewBuilder
    private var logo: some View {
        if let raw = card.media.logoUrl, let url = api.image(raw) {
            LazyImage(url: url) { state in
                if let image = state.image {
                    image.resizable()
                        .aspectRatio(contentMode: .fit)
                        .shadow(color: .black.opacity(0.6), radius: 6, y: 1)
                }
            }
            .frame(maxWidth: 118, maxHeight: 34, alignment: .bottomLeading)
        }
    }
}

// MARK: - 日程

/// 「日程」：今天起一周的日期条 + 选中那天的议程。
///
/// Hero 只挑亮点，扫全貌靠这里。日期条上的小圆点是当天的更新数（有正在下载 / 整理的，圆点带状态色）；
/// 没有更新的日子照样占位但点不了——一周的节奏本身就是信息。议程是一列竖排：左列大号时刻、
/// 中间剧照、右边片名与集号，和上下两排横滑行形状不同，页面有呼吸。
struct SubsHomeSchedule: View {
    let days: [SubsHomeScheduleDay]
    /// nil = 默认选中第一个有安排的日子
    @State private var selected: Int?
    @Namespace private var chipSpace

    private var current: SubsHomeScheduleDay? {
        let pick = selected ?? days.first(where: { !$0.entries.isEmpty })?.daysAhead
        return days.first { $0.daysAhead == pick } ?? days.first
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SubsHomeSectionHeader(title: "日程", trailing: current.map(caption))
            strip
            agenda
        }
        .sensoryFeedback(.selection, trigger: selected)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("schedule")
    }

    private func caption(_ day: SubsHomeScheduleDay) -> String {
        day.entries.isEmpty ? day.dateLabel : "\(day.dateLabel) · \(day.entries.count) 部"
    }

    private var strip: some View {
        HStack(spacing: 6) {
            ForEach(days) { day in
                chip(day, on: day.daysAhead == current?.daysAhead)
            }
        }
        .padding(.horizontal, Theme.pagePadding)
    }

    private func chip(_ day: SubsHomeScheduleDay, on: Bool) -> some View {
        Button {
            withAnimation(.snappy(duration: 0.32)) { selected = day.daysAhead }
        } label: {
            VStack(spacing: 3) {
                Text(day.weekday)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(on ? Color.black.opacity(0.55) : Theme.textMuted)
                Text(day.dayNumber)
                    .font(.system(size: 19, weight: on ? .bold : .medium))
                    .monospacedDigit()
                    .foregroundStyle(on ? Color.black : Theme.text)
                HStack(spacing: 3) {
                    ForEach(0 ..< min(day.entries.count, 3), id: \.self) { _ in
                        Circle()
                            .fill(on ? Color.black.opacity(0.4) : dotColor(day))
                            .frame(width: 4, height: 4)
                    }
                }
                .frame(height: 4)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 9)
            .background {
                if on {
                    // 选中的白底在日期间滑动（同一块几何体），而不是这边消失那边出现
                    RoundedRectangle(cornerRadius: 15, style: .continuous)
                        .fill(Color.white)
                        .matchedGeometryEffect(id: "selected-day", in: chipSpace)
                } else {
                    RoundedRectangle(cornerRadius: 15, style: .continuous)
                        .fill(Color.white.opacity(0.05))
                        .overlay(RoundedRectangle(cornerRadius: 15, style: .continuous).strokeBorder(Color.white.opacity(0.07)))
                }
            }
            .contentShape(.rect)
        }
        .buttonStyle(SubsHomePressStyle(scale: 0.92))
        .disabled(day.entries.isEmpty)
        .opacity(day.entries.isEmpty ? 0.36 : 1)
        .accessibilityLabel("\(day.weekday)，\(day.dateLabel)，\(day.entries.isEmpty ? "没有更新" : "\(day.entries.count) 部更新")")
        .accessibilityAddTraits(on ? .isSelected : [])
        .accessibilityIdentifier("schedule-day-\(day.daysAhead)")
    }

    /// 日期条小圆点：当天有正在发生的（下载 / 整理）就亮状态色，否则中性白
    private func dotColor(_ day: SubsHomeScheduleDay) -> Color {
        day.entries.first(where: { $0.tone.glows })?.tone.color ?? Color.white.opacity(0.45)
    }

    @ViewBuilder
    private var agenda: some View {
        if let day = current, !day.entries.isEmpty {
            VStack(spacing: 0) {
                ForEach(Array(day.entries.enumerated()), id: \.element.id) { offset, entry in
                    if offset > 0 {
                        Rectangle()
                            .fill(Color.white.opacity(0.06))
                            .frame(height: 1)
                            .padding(.leading, 86)
                    }
                    SubsHomeAgendaRow(entry: entry)
                }
            }
            .padding(.vertical, 4)
            .background(Color.white.opacity(0.045), in: .rect(cornerRadius: 22, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 22, style: .continuous).strokeBorder(Color.white.opacity(0.07)))
            .padding(.horizontal, Theme.pagePadding)
            .id(day.daysAhead)
            .transition(.opacity.combined(with: .offset(y: 8)))
        }
    }
}

private struct SubsHomeAgendaRow: View {
    let entry: SubsHomeScheduleEntry
    @Environment(\.api) private var api
    @Environment(Router.self) private var router

    var body: some View {
        Button {
            router.push(.subscription(id: entry.subscriptionId))
        } label: {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(entry.time ?? "待定")
                        .font(.system(size: 20, weight: entry.time == nil ? .regular : .semibold))
                        .monospacedDigit()
                        .foregroundStyle(entry.time == nil ? Theme.textFaint : Theme.text)
                    HStack(spacing: 4) {
                        SubsHomeDot(tone: entry.tone, pulse: entry.tone == .live, size: 5)
                        Text(entry.status)
                            .font(.caption2.weight(.semibold))
                            .monospacedDigit()
                            .foregroundStyle(entry.tone.color)
                            .lineLimit(1)
                            .minimumScaleFactor(0.8)
                    }
                }
                .frame(width: 62, alignment: .leading)
                thumbnail
                VStack(alignment: .leading, spacing: 3) {
                    Text(entry.title)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.text)
                    Text(entry.episodeLabel)
                        .font(.footnote)
                        .monospacedDigit()
                        .foregroundStyle(Theme.textMuted)
                }
                .lineLimit(1)
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Theme.textFaint)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("《\(entry.title)》\(entry.episodeLabel)，\(entry.status)，\(entry.time ?? "时间待定")")
        .accessibilityAddTraits(.isButton)
        .accessibilityIdentifier("schedule-row")
    }

    private var thumbnail: some View {
        Color.clear
            .frame(width: 92, height: 52)
            .overlay {
                RemoteImage(url: api.image(entry.media?.backdropUrl ?? entry.media?.posterUrl, .landscapeCard))
            }
            .overlay(alignment: .bottom) {
                if let progress = entry.progress {
                    SubsHomeProgressLine(value: progress, tint: SubsHomeTone.live.color, height: 2.5)
                        .padding(.horizontal, 6)
                        .padding(.bottom, 5)
                }
            }
            .clipShape(.rect(cornerRadius: 9, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).strokeBorder(Color.white.opacity(0.08)))
    }
}

// MARK: - 海报行

/// 剧集 / 电影海报行：进行中的在前（此刻最要紧的排最左，连没上映的也算进行中），
/// 已暂停 / 已完成压暗排在一道竖排小字的分隔线后面。
///
/// 横滑最多放 `limit` 张：一排是浏览亮点的地方，翻到底要十几下就不再是「一眼扫过」；
/// 超出时末尾放一张「查看全部」卡，与标题「›」一样压栈到完整海报墙（`AppRoute.subscriptionWall`），
/// 墙上是同一份排好的结果，顺序不变。
struct SubsHomeShelfRow: View {
    let title: String
    let kind: String
    let shelf: SubsHomeShelf
    @Environment(Router.self) private var router

    /// 与发现页、媒体库横滑行同宽：一屏两张半，第三张露出一截提示还能滑
    static let cardWidth: CGFloat = 126
    /// 横滑最多几张（约七屏）；其余在海报墙里
    static let limit = 20

    var body: some View {
        let active = Array(shelf.active.prefix(Self.limit))
        let resting = Array(shelf.resting.prefix(Self.limit - active.count))
        let hidden = shelf.all.count - active.count - resting.count
        VStack(alignment: .leading, spacing: 12) {
            SubsHomeSectionHeader(
                title: title, trailing: SubscriptionsHome.countSummary(shelf), actionIdentifier: "shelf-more-\(kind)"
            ) {
                router.push(.subscriptionWall(kind: kind))
            }
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(alignment: .top, spacing: 12) {
                    ForEach(active) { item in
                        SubsHomePosterCard(item: item).frame(width: Self.cardWidth)
                    }
                    if !resting.isEmpty {
                        if !active.isEmpty {
                            SubsHomeRestingDivider(label: shelf.restingLabel, height: Self.cardWidth * 1.5)
                        }
                        ForEach(resting) { item in
                            SubsHomePosterCard(item: item).frame(width: Self.cardWidth)
                        }
                    }
                    if hidden > 0 {
                        SubsHomeSeeAllCard(total: shelf.all.count) {
                            router.push(.subscriptionWall(kind: kind))
                        }
                        .frame(width: Self.cardWidth)
                    }
                }
                .scrollTargetLayout()
            }
            .contentMargins(.horizontal, Theme.pagePadding, for: .scrollContent)
            .scrollTargetBehavior(.viewAligned)
            .scrollClipDisabled()
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("section-\(kind)")
    }
}

/// 横滑末尾的「查看全部」：与海报同尺寸的一块透明玻璃，排在最后一张之后
private struct SubsHomeSeeAllCard: View {
    let total: Int
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            // 与海报同一种撑法：先用 2:3 的空底定住尺寸（横滑里没有给定高度，按比例撑不开），内容叠在上面
            Color.clear
                .aspectRatio(Theme.posterAspect, contentMode: .fit)
                .overlay {
                    VStack(spacing: 8) {
                        Image(systemName: "square.grid.2x2")
                            .font(.system(size: 22, weight: .regular))
                            .foregroundStyle(Theme.textMuted)
                        Text("查看全部")
                            .font(.footnote.weight(.semibold))
                            .foregroundStyle(Theme.text)
                        Text("\(total) 部")
                            .font(.caption)
                            .monospacedDigit()
                            .foregroundStyle(Theme.textFaint)
                    }
                }
            .background(Color.white.opacity(0.045), in: .rect(cornerRadius: Theme.posterRadius, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Theme.posterRadius, style: .continuous).strokeBorder(Color.white.opacity(0.09)))
            .contentShape(.rect)
        }
        .buttonStyle(SubsHomePressStyle())
        .accessibilityLabel("查看全部 \(total) 部")
        .accessibilityIdentifier("shelf-see-all")
    }
}

/// 海报：只留一个状态小签 + 进行中剧集的当季收录细线，其余交给下面两行字。
/// 首页横滑与海报墙共用这一张，状态签与顺序两处一致；墙上多一行「规则组 → 媒体库」流向
struct SubsHomePosterCard: View {
    let item: SubsHomeShelfItem
    /// 海报墙的第三行（规则组 → 媒体库）；首页横滑不带
    var flow: String?
    /// 已暂停 / 已完成是否压暗：首页一排里靠压暗衬出分隔线；海报墙已按分段标题分组，保持原色便于浏览
    var dimsResting = true
    @Environment(\.api) private var api
    @Environment(Router.self) private var router

    var body: some View {
        Button {
            router.push(.subscription(id: item.sub.id))
        } label: {
            VStack(alignment: .leading, spacing: 0) {
                poster
                Text(item.sub.media.title)
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(dimmed ? Theme.textMuted : Theme.text)
                    .lineLimit(1)
                    .padding(.top, 8)
                Text(item.meta)
                    .font(.caption)
                    .monospacedDigit()
                    .foregroundStyle(Theme.textFaint)
                    .lineLimit(1)
                    .padding(.top, 1)
                if let flow {
                    Text(flow)
                        .font(.caption2)
                        .foregroundStyle(Theme.textFaint)
                        .lineLimit(1)
                        .padding(.top, 1)
                }
            }
            .contentShape(.rect)
        }
        .buttonStyle(SubsHomePressStyle())
        .contextMenu {
            Button("查看订阅详情", systemImage: "list.bullet.rectangle") { router.push(.subscription(id: item.sub.id)) }
            Button("查看影片详情", systemImage: "info.circle") {
                router.push(.mediaDetail(titleRef: "tmdb:\(item.sub.media.kind):\(item.sub.media.tmdbId)"))
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(["《\(item.sub.media.title)》", item.chip?.text, item.meta, flow].compactMap { $0 }.joined(separator: "，"))
        .accessibilityAddTraits(.isButton)
        .accessibilityIdentifier("subscription-cell")
    }

    private var dimmed: Bool { dimsResting && item.resting }

    private var poster: some View {
        Color.clear
            .aspectRatio(Theme.posterAspect, contentMode: .fit)
            .overlay {
                RemoteImage(url: api.image(item.sub.media.posterUrl, .posterCard))
                    .saturation(dimmed ? 0.35 : 1)
                    .brightness(dimmed ? -0.12 : 0)
            }
            .overlay(alignment: .bottom) {
                if let progress = item.progress {
                    ZStack(alignment: .bottom) {
                        LinearGradient(colors: [.clear, .black.opacity(0.55)], startPoint: .top, endPoint: .bottom)
                            .frame(height: 34)
                        SubsHomeProgressLine(value: progress, tint: .white.opacity(0.92), height: 2.5)
                            .padding(.horizontal, 9)
                            .padding(.bottom, 8)
                    }
                }
            }
            .overlay(alignment: .topLeading) {
                if let chip = item.chip {
                    SubsHomeChipView(chip: chip).padding(7)
                }
            }
            .clipShape(.rect(cornerRadius: Theme.posterRadius, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Theme.posterRadius, style: .continuous).strokeBorder(Color.white.opacity(0.08)))
            .shadow(color: .black.opacity(dimmed ? 0.18 : 0.4), radius: 10, y: 6)
    }
}

/// 海报上的状态小签：暗色毛玻璃胶囊，颜色只在圆点与文字上
struct SubsHomeChipView: View {
    let chip: SubsHomeChip

    var body: some View {
        HStack(spacing: 4) {
            SubsHomeDot(tone: chip.tone, pulse: chip.pulse, size: 5)
            Text(chip.text)
                .font(.system(size: 10.5, weight: .semibold))
                .monospacedDigit()
                .lineLimit(1)
        }
        .foregroundStyle(chip.tone == .calm ? Color.white.opacity(0.9) : chip.tone.color)
        .padding(.horizontal, 7)
        .padding(.vertical, 4)
        .background(Color.black.opacity(0.38), in: .capsule)
        .background(.ultraThinMaterial, in: .capsule)
        .environment(\.colorScheme, .dark)
    }
}

/// 在追与歇着之间的分隔：一道上下渐隐的发丝线，中间竖排小字（中文竖排天然成立，不用旋转）
private struct SubsHomeRestingDivider: View {
    let label: String
    let height: CGFloat

    var body: some View {
        VStack(spacing: 8) {
            line
            Text(label.map(String.init).joined(separator: "\n"))
                .font(.system(size: 10, weight: .semibold))
                .lineSpacing(1)
                .multilineTextAlignment(.center)
                .foregroundStyle(Theme.textFaint)
                .fixedSize()
            line
        }
        .frame(width: 18, height: height)
        .accessibilityHidden(true)
    }

    private var line: some View {
        Rectangle()
            .fill(LinearGradient(colors: [.clear, Color.white.opacity(0.16), .clear], startPoint: .top, endPoint: .bottom))
            .frame(width: 1)
            .frame(maxHeight: .infinity)
    }
}
