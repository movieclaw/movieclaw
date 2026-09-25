import SwiftUI
import UIKit

/// 海报卡片的最小视觉契约（对应 Web `PosterVisualItem`）。
///
/// 发现页、搜索影视结果、影人作品、详情页相似推荐、媒体库搜索结果（以及后续 AI 卡片）
/// 都画成同一种海报卡，数据先各自映射成它。字段缺失就不显示（豆瓣轻量搜索没有年份与类型）。
nonisolated struct DiscoverPosterItem: Identifiable, Hashable, Sendable {
    /// 服务端签发的稳定引用（`tmdb:movie:550` / `douban:1292052`）；历史快照/本地条目可能没有
    var titleRef: String?
    /// 来源站条目 ID
    var externalId: String
    /// tmdb / douban
    var source: String = "tmdb"
    /// movie / tv；豆瓣轻量结果为空
    var mediaType: String?
    var title: String
    var originalTitle: String = ""
    var year: Int?
    /// 0 表示暂无评分（不渲染徽章，避免读成「0 分」）
    var rating: Double = 0
    var posterUrl: String?
    var backdropUrl: String?
    var genres: [String] = []
    /// 规模：电影片长 / 剧集季数；列表数据常为空
    var extent: String = ""
    var overview: String = ""
    /// 有在位文件时的库存摘要：自动打「已入库」绿斜标
    var libraryStatus: API.MediaLibraryStatus?
    /// 当前观看者已收藏（右上角红心）
    var favorite: Bool = false
    /// 调用方显式指定的斜标（优先于「已入库」「已订阅」的自动派生）
    var ribbon: DiscoverRibbon?
    /// 卡片框宽高比（缺省 2:3；本地抓帧缩略图是 16:9）
    var aspect: CGFloat = 2.0 / 3.0

    var id: String { titleRef ?? "\(source):\(mediaType ?? "-"):\(externalId)" }

    /// 进详情用的引用：优先服务端 titleRef，没有时按 Web `titleRef()` 规则拼
    var resolvedTitleRef: String {
        if let titleRef, !titleRef.isEmpty { return titleRef }
        return source == "douban" ? "douban:\(externalId)" : "tmdb:\(mediaType ?? "movie"):\(externalId)"
    }

    var typeLabel: String? {
        switch mediaType {
        case "movie": "电影"
        case "tv": "剧集"
        default: nil
        }
    }
}

nonisolated extension DiscoverPosterItem {
    /// 发现/搜索接口的条目摘要 → 海报卡
    init(_ dto: API.DiscoveredTitleView) {
        self.init(
            titleRef: dto.titleRef,
            externalId: dto.externalId,
            source: dto.provider,
            mediaType: dto.mediaType,
            title: dto.title,
            originalTitle: dto.originalTitle,
            year: dto.releaseYear,
            rating: dto.providerRating,
            posterUrl: dto.posterUrl.isEmpty ? nil : dto.posterUrl,
            backdropUrl: dto.backdropUrl,
            genres: dto.genres,
            extent: dto.extentLabel,
            overview: dto.overview,
            libraryStatus: dto.libraryStatus
        )
    }
}

/// 海报角上的斜标
nonisolated struct DiscoverRibbon: Hashable, Sendable {
    enum Tone: Hashable { case owned, subscribed }
    var label: String
    var tone: Tone
}

/// 信息层操作区的形态（同 Web `PosterCardAction`）：
/// - subscribe：还没拥有，给「订阅影片」；已订阅时变成「已订阅」（点进订阅管理）
/// - follow / backfill：媒体库场景的「自动续订」「补齐缺集」；已订阅时隐藏
/// - owned：在库标识（非交互）
/// - none：不给操作
enum DiscoverPosterAction: Hashable {
    case subscribe, follow, backfill, owned, none

    var subscribeMeta: (label: String, icon: String)? {
        switch self {
        case .subscribe: ("订阅影片", "plus")
        case .follow: ("自动续订", "bell")
        case .backfill: ("补齐缺集", "arrow.down.circle")
        case .owned, .none: nil
        }
    }
}

/// 海报卡片（发现页海报墙的最小单元）。
///
/// 交互同 Web `PosterCardVisual` 的触屏逻辑「首点展开信息层、再点进详情」：
/// - 卡片带订阅类动作（订阅影片 / 自动续订 / 补齐缺集）且账号有订阅权限时，**第一下只展开**海报底部的
///   信息层（类型 / 简介 / 操作键），**第二下**才进详情；点卡片外任意处（含开始滚动）收起，
///   同一时刻只有一张卡展开（`DiscoverPosterReveal` 统一协调）；
/// - 没有订阅类动作的卡（影人作品、在库标识、无权限）单击直达详情；
/// - **长按**另有系统上下文菜单（预览 + 同样的操作），是原生额外提供的快捷方式。
///
/// 信息层的操作键：未订阅给「订阅影片」等；发现页已订阅时切成「已订阅」状态键——两者都打开订阅弹层，
/// 已订阅时由弹层的管理态接手（「该电影已在订阅中…」+ 取消订阅），与 Web `openSubscribe` 一致。
///
/// 角标：左上「已入库」绿斜标（libraryStatus 自动派生）；「已订阅」蓝斜标只在调用方要求时打
/// （Web 只有影人作品页与 AI 作品卡这么做），右上收藏红心 + 评分徽章（评分为 0 不显示）。
struct DiscoverPosterCard: View {
    let item: DiscoverPosterItem
    var action: DiscoverPosterAction = .subscribe
    /// 自定义点按目标；nil = 进发现详情页
    var onOpen: (() -> Void)?
    /// 未入库但已订阅时打「已订阅」斜标（同 Web：只有影人作品页、AI 作品卡显式要求）
    var showsSubscribedRibbon = false
    /// 海报下方的附加一行（媒体库搜索结果的「第 1 季 · 12 集 · 2160p」）
    var footnote: String?

    @Environment(Router.self) private var router
    @Environment(\.permissions) private var permissions
    @Environment(\.api) private var api
    /// 本卡在展开协调器里的身份（同一部影片可能在多行重复出现，不能用条目 ID）
    @State private var revealID = UUID()

    private var reveal: DiscoverPosterReveal { .shared }

    private var subscription: API.SubscriptionView? {
        SubscriptionIndex.shared.subscription(for: item)
    }

    private var ribbon: DiscoverRibbon? {
        if let ribbon = item.ribbon { return ribbon }
        if item.libraryStatus != nil { return DiscoverRibbon(label: "已入库", tone: .owned) }
        if showsSubscribedRibbon, subscription != nil { return DiscoverRibbon(label: "已订阅", tone: .subscribed) }
        return nil
    }

    /// 有订阅类动作（订阅影片 / 自动续订 / 补齐缺集）
    private var hasSubscribeAction: Bool { action.subscribeMeta != nil }

    /// 信息层里是否有操作键：在库标识，或有权限的订阅类动作（媒体库动作已订阅时隐藏）
    private var showsOverlayAction: Bool {
        if action == .owned { return true }
        guard hasSubscribeAction, permissions.canSubscribe else { return false }
        return action == .subscribe || subscription == nil
    }

    /// 触屏首点是否先展开信息层（同 Web `revealOnTouch`：只有可点的订阅类动作需要）
    private var revealsOnTap: Bool { hasSubscribeAction && permissions.canSubscribe }

    private var revealed: Bool { reveal.revealedID == revealID }

    var body: some View {
        Button(action: tap) {
            VStack(alignment: .leading, spacing: 0) {
                poster
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.title)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.text)
                        .lineLimit(1)
                    Text(metaLine)
                        .font(.caption)
                        .monospacedDigit()
                        .foregroundStyle(Theme.textMuted)
                        .lineLimit(1)
                    if let footnote {
                        Text(footnote)
                            .font(.caption)
                            .foregroundStyle(Theme.textMuted)
                            .lineLimit(1)
                    }
                }
                .padding(.top, 8)
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        // 无障碍标识只挂在卡片按钮上：信息层里的订阅键要作为独立元素可达
        .accessibilityIdentifier("poster-card")
        .accessibilityLabel(accessibilityText)
        .accessibilityHint(revealsOnTap && !revealed ? "轻点展开简介与订阅操作，再次轻点查看详情" : "")
        // 信息层叠在按钮之外：里面的操作键是独立按钮；文字部分不接收点按，点它等于点卡片（进详情）
        .overlay(alignment: .top) {
            if revealed {
                VStack(spacing: 0) {
                    Color.clear
                        .aspectRatio(item.aspect, contentMode: .fit)
                        .overlay(alignment: .bottom) { infoLayer }
                        .clipShape(.rect(cornerRadius: Theme.posterRadius))
                    Spacer(minLength: 0)
                }
                // 展开期间上报整张卡的位置：触摸落在卡内不收起（点操作键、再点进详情都不受影响）
                .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { reveal.updateFrame(revealID, $0) }
                .transition(.opacity.combined(with: .offset(y: 8)))
            }
        }
        .animation(.easeOut(duration: 0.25), value: revealed)
        .onDisappear { reveal.collapse(revealID) }
        .contextMenu {
            menuActions
        } preview: {
            DiscoverPosterInfoPreview(item: item)
                .environment(\.api, api)
        }
    }

    private var metaLine: String {
        // 始终保留一行，避免缺年份的卡片高低跳动
        [item.year.map(String.init), item.extent.isEmpty ? nil : item.extent].compactMap { $0 }.joined(separator: " · ")
    }

    private var accessibilityText: String {
        var parts = ["《\(item.title)》"]
        if let ribbon { parts.append(ribbon.label) }
        if item.rating > 0 { parts.append(String(format: "评分 %.1f", item.rating)) }
        return parts.joined(separator: "，")
    }

    private var poster: some View {
        Color.clear
            .aspectRatio(item.aspect, contentMode: .fit)
            .overlay {
                RemoteImage(url: api.image(item.posterUrl, .card(aspect: Double(item.aspect))))
            }
            .overlay(alignment: .topLeading) {
                if let ribbon {
                    RibbonBadge(ribbon: ribbon)
                }
            }
            .overlay(alignment: .topTrailing) {
                HStack(spacing: 4) {
                    if item.favorite {
                        Image(systemName: "heart.fill")
                            .font(.caption2)
                            .foregroundStyle(Theme.danger)
                            .padding(.horizontal, 4).padding(.vertical, 3)
                            .background(.black.opacity(0.7), in: .rect(cornerRadius: 6))
                            .accessibilityLabel("已收藏")
                    }
                    if item.rating > 0 {
                        RatingBadge(rating: item.rating)
                    }
                }
                .padding(6)
            }
            .clipShape(.rect(cornerRadius: Theme.posterRadius))
            .overlay(RoundedRectangle(cornerRadius: Theme.posterRadius).strokeBorder(Color.white.opacity(0.08)))
            .shadow(color: .black.opacity(0.35), radius: 10, y: 6)
    }

    /// 展开后的信息层（同 Web hover 信息层）：底部渐变升起，类型 / 简介（3 行）/ 操作键
    private var infoLayer: some View {
        VStack(alignment: .leading, spacing: 4) {
            Group {
                if !item.genres.isEmpty {
                    Text(item.genres.joined(separator: " · "))
                        .font(.caption.weight(.medium))
                        .foregroundStyle(Theme.accent2)
                        .lineLimit(1)
                }
                if !item.overview.isEmpty {
                    Text(item.overview)
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.75))
                        .lineLimit(3)
                }
            }
            .allowsHitTesting(false)
            if showsOverlayAction {
                overlayActionButton
                    .padding(.top, 6)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 10)
        .padding(.bottom, 10)
        .padding(.top, 36)
        .background {
            LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black.opacity(0.6), location: 0.4), .init(color: .black.opacity(0.9), location: 1)], startPoint: .top, endPoint: .bottom)
                .allowsHitTesting(false)
        }
    }

    @ViewBuilder
    private var overlayActionButton: some View {
        if let meta = action.subscribeMeta {
            let subscribed = subscription != nil
            Button {
                presentSubscribe()
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: subscribed ? "checkmark" : meta.icon)
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(subscribed ? Theme.success : .black)
                    Text(subscribed ? "已订阅" : meta.label)
                }
                .font(.caption.weight(.semibold))
                .foregroundStyle(subscribed ? .white.opacity(0.9) : .black)
                .padding(.horizontal, 10)
                .frame(height: 28)
                .background(subscribed ? AnyShapeStyle(Color.white.opacity(0.18)) : AnyShapeStyle(Theme.accent), in: .capsule)
            }
            .buttonStyle(.plain)
            .accessibilityLabel(subscribed ? "管理《\(item.title)》的订阅" : "\(meta.label)《\(item.title)》")
            .accessibilityIdentifier("poster-card-subscribe")
        } else {
            // 在库标识：非交互
            HStack(spacing: 6) {
                Circle().fill(Theme.success).frame(width: 6, height: 6)
                Text("在库")
            }
            .font(.caption.weight(.semibold))
            .foregroundStyle(.white.opacity(0.9))
            .padding(.horizontal, 10)
            .frame(height: 28)
            .background(Color.white.opacity(0.18), in: .capsule)
            .allowsHitTesting(false)
        }
    }

    @ViewBuilder
    private var menuActions: some View {
        if let meta = action.subscribeMeta, permissions.canSubscribe {
            if let sub = subscription {
                // 发现页的「订阅影片」在已订阅时切成状态键（打开订阅弹层的管理态）；媒体库动作已订阅时隐藏
                if action == .subscribe {
                    Button(action: presentSubscribe) {
                        Label("已订阅 · \(SubscriptionStatusMeta.label(sub.status))", systemImage: "checkmark.circle")
                    }
                }
            } else {
                Button(action: presentSubscribe) {
                    Label(meta.label, systemImage: meta.icon)
                }
            }
        } else if action == .owned {
            Label("在库", systemImage: "checkmark.seal")
        }
        Button(action: open) {
            Label("查看详情", systemImage: "info.circle")
        }
    }

    /// 订阅弹层：未订阅走订阅表单，已订阅由弹层预检发现既有订阅、进入管理态（同 Web `openSubscribe`）
    private func presentSubscribe() {
        router.present(.subscribe(SubscribeRequest(titleRef: item.resolvedTitleRef, title: item.title)))
    }

    private func tap() {
        if revealsOnTap, !reveal.wasRevealed(revealID) {
            reveal.reveal(revealID)
            return
        }
        reveal.collapse(revealID)
        open()
    }

    private func open() {
        if let onOpen {
            onOpen()
        } else {
            DiscoverMediaSeed.remember(item)
            router.push(.mediaDetail(titleRef: item.resolvedTitleRef))
        }
    }
}

/// 海报卡「首点展开」的全局协调（同 Web `PosterCardVisual` 的 revealed + 卡片外 pointerdown 收起）。
///
/// - 同一时刻只有一张卡展开；
/// - 点卡片外任意处（包括开始滚动）就收起：在窗口上挂一个只观察、不参与识别的手势识别器，
///   触摸一落下就比对展开卡上报的位置，落在卡外即收起；
/// - 兜底：位置还没上报时（刚展开的瞬间）照样收起，但记下被收起的是哪张（`justCollapsed`）——
///   点的正是那张卡时（触摸落下先收起、抬手才触发按钮），按钮仍能认出「刚才是展开的」、直接进详情。
@Observable
final class DiscoverPosterReveal {
    static let shared = DiscoverPosterReveal()

    private(set) var revealedID: UUID?
    /// 展开卡在窗口坐标系里的位置（`.zero` = 尚未上报）
    @ObservationIgnored private var revealedFrame: CGRect = .zero
    /// 最近一次触摸落下时被收起的卡
    @ObservationIgnored private var justCollapsed: UUID?
    @ObservationIgnored private var observedWindows: [ObjectIdentifier] = []

    func reveal(_ id: UUID) {
        installTouchObserver()
        revealedFrame = .zero
        revealedID = id
    }

    func updateFrame(_ id: UUID, _ frame: CGRect) {
        if revealedID == id { revealedFrame = frame }
    }

    func collapse(_ id: UUID) {
        if revealedID == id { revealedID = nil }
    }

    /// 这张卡在本次点按开始前是否处于展开状态
    func wasRevealed(_ id: UUID) -> Bool {
        revealedID == id || justCollapsed == id
    }

    fileprivate func touchBegan(at point: CGPoint) {
        guard revealedID != nil else {
            justCollapsed = nil
            return
        }
        if revealedFrame != .zero, revealedFrame.contains(point) {
            justCollapsed = nil
            return
        }
        justCollapsed = revealedID
        revealedID = nil
    }

    private func installTouchObserver() {
        let windows = UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .flatMap(\.windows)
        for window in windows where !observedWindows.contains(ObjectIdentifier(window)) {
            observedWindows.append(ObjectIdentifier(window))
            let observer = DiscoverTouchObserver { [weak self] point in self?.touchBegan(at: point) }
            window.addGestureRecognizer(observer)
        }
    }
}

/// 只观察触摸落下的手势识别器：回调后立即判定失败，不拦截、不延迟任何触摸与其它手势
private final class DiscoverTouchObserver: UIGestureRecognizer, UIGestureRecognizerDelegate {
    private let onTouch: (CGPoint) -> Void

    init(onTouch: @escaping (CGPoint) -> Void) {
        self.onTouch = onTouch
        super.init(target: nil, action: nil)
        cancelsTouchesInView = false
        delaysTouchesBegan = false
        delaysTouchesEnded = false
        delegate = self
    }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent) {
        // 窗口坐标，与 SwiftUI `.global` 同一坐标系
        if let touch = touches.first { onTouch(touch.location(in: nil)) }
        state = .failed
    }

    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool {
        true
    }
}

/// 详情页首屏预存（同 Web `getMediaSeed`）：站内点海报卡 / Hero 进详情前记下列表字段，
/// 详情页先用它渲染标题、海报、简介等，接口返回后原位替换；有预存时详情接口失败也不打断页面。
enum DiscoverMediaSeed {
    private static var items: [String: DiscoverPosterItem] = [:]

    static func remember(_ item: DiscoverPosterItem) {
        if items.count >= 200 { items.removeAll() }
        items[item.resolvedTitleRef] = item
    }

    static func item(for titleRef: String) -> DiscoverPosterItem? {
        items[titleRef]
    }
}

/// 长按预览：Web 海报卡 hover 信息层的原生版（海报 + 类型 / 标题 / 年份 / 简介）
struct DiscoverPosterInfoPreview: View {
    let item: DiscoverPosterItem
    @Environment(\.api) private var api

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            RemoteImage(url: api.image(item.posterUrl, .posterCard))
                .frame(width: 96, height: 144)
                .clipShape(.rect(cornerRadius: 10))
            VStack(alignment: .leading, spacing: 6) {
                Text(item.title).font(.headline).foregroundStyle(Theme.text)
                if !item.originalTitle.isEmpty, item.originalTitle != item.title {
                    Text(item.originalTitle).font(.caption).foregroundStyle(Theme.textMuted).lineLimit(1)
                }
                let meta = [
                    item.typeLabel,
                    item.year.map(String.init),
                    item.rating > 0 ? String(format: "★ %.1f", item.rating) : nil,
                    item.extent.isEmpty ? nil : item.extent,
                ].compactMap { $0 }
                if !meta.isEmpty {
                    Text(meta.joined(separator: " · ")).font(.caption).foregroundStyle(Theme.accent2)
                }
                if !item.genres.isEmpty {
                    Text(item.genres.joined(separator: " · ")).font(.caption.weight(.medium)).foregroundStyle(Theme.accent2)
                }
                if !item.overview.isEmpty {
                    Text(item.overview).font(.caption).foregroundStyle(Theme.textMuted).lineLimit(6)
                }
                if item.libraryStatus != nil {
                    Label("在库", systemImage: "circle.fill")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Theme.success)
                        .labelStyle(.titleAndIcon)
                        .imageScale(.small)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(16)
        .frame(width: 340)
        .background(Theme.background)
    }
}

/// 左上角斜标（已入库绿 / 已订阅蓝）
struct RibbonBadge: View {
    let ribbon: DiscoverRibbon

    var body: some View {
        GeometryReader { proxy in
            // 同 Web：带宽 62%、左移 18%、距顶 8pt，绕中心逆时针 45°
            let width = proxy.size.width
            Text(ribbon.label)
                .font(.system(size: 9, weight: .bold))
                .tracking(0.8)
                .foregroundStyle(.white)
                .frame(width: width * 0.62)
                .padding(.vertical, 2)
                .background(gradient)
                .rotationEffect(.degrees(-45))
                .position(x: width * 0.13, y: 15)
        }
        .allowsHitTesting(false)
        .accessibilityLabel(ribbon.label)
    }

    private var gradient: LinearGradient {
        switch ribbon.tone {
        case .owned:
            LinearGradient(colors: [Color(red: 0.06, green: 0.73, blue: 0.51), Color(red: 0.08, green: 0.72, blue: 0.65)], startPoint: .leading, endPoint: .trailing)
        case .subscribed:
            LinearGradient(colors: [Color(red: 0.05, green: 0.65, blue: 0.91), Color(red: 0.39, green: 0.40, blue: 0.95)], startPoint: .leading, endPoint: .trailing)
        }
    }
}

/// 评分徽章：黑底 + 黄星 + 一位小数
struct RatingBadge: View {
    let rating: Double

    var body: some View {
        HStack(spacing: 3) {
            Image(systemName: "star.fill").font(.system(size: 9)).foregroundStyle(Theme.warning)
            Text(String(format: "%.1f", rating)).font(.caption2.weight(.semibold)).monospacedDigit()
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 5).padding(.vertical, 2)
        .background(.black.opacity(0.7), in: .rect(cornerRadius: 6))
    }
}

/// 横滚海报行（对应 Web `MediaRow`）：标题 + 可选「查看完整榜单」+ 横滚卡片
struct DiscoverPosterRow: View {
    let title: String
    let items: [DiscoverPosterItem]
    var moreTitle = "查看完整榜单"
    var onMore: (() -> Void)?
    var action: DiscoverPosterAction = .subscribe
    var cardWidth: CGFloat = 126

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(title)
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(Theme.text)
                Spacer()
                if let onMore {
                    Button(moreTitle, action: onMore)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.textMuted)
                        .accessibilityIdentifier("row-more")
                }
            }
            .padding(.horizontal, Theme.pagePadding)
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(alignment: .top, spacing: 12) {
                    ForEach(items) { item in
                        DiscoverPosterCard(item: item, action: action)
                            .frame(width: cardWidth)
                    }
                }
                .padding(.horizontal, Theme.pagePadding)
                .padding(.vertical, 4)
            }
            .scrollClipDisabled()
        }
    }
}

/// 行加载骨架：标题实显、海报位闪烁（数据到达原位替换不跳版）
struct DiscoverRowSkeleton: View {
    let title: String
    var cardWidth: CGFloat = 126

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.title3.weight(.semibold))
                .foregroundStyle(Theme.text)
                .padding(.horizontal, Theme.pagePadding)
            HStack(spacing: 12) {
                ForEach(0 ..< 4, id: \.self) { _ in
                    DiscoverSkeletonBlock(cornerRadius: Theme.posterRadius)
                        .frame(width: cardWidth, height: cardWidth * 1.5)
                }
            }
            .padding(.horizontal, Theme.pagePadding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .clipped()
        }
        .accessibilityLabel("「\(title)」加载中")
    }
}

/// 闪烁占位块
struct DiscoverSkeletonBlock: View {
    var cornerRadius: CGFloat = 8
    @State private var dim = false

    var body: some View {
        RoundedRectangle(cornerRadius: cornerRadius)
            .fill(Color.white.opacity(dim ? 0.04 : 0.08))
            .onAppear {
                withAnimation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true)) { dim = true }
            }
    }
}

/// 海报网格的列定义
enum DiscoverGrid {
    /// 手机三列、宽屏自适应（影人作品页，同 Web）
    static let columns = [GridItem(.adaptive(minimum: 104, maximum: 170), spacing: 12, alignment: .top)]
    /// 手机两列、宽屏自适应（筛选结果、片单全列表 Web `grid-cols-2`；搜索结果 Web `minmax(148px,1fr)`）
    static let wideColumns = [GridItem(.adaptive(minimum: 148, maximum: 220), spacing: 16, alignment: .top)]
}
