import SwiftUI

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
/// 交互（原生化的 Web「首点展开信息层、再点进详情」）：
/// - **点按**直接进详情（`onOpen`，缺省进发现详情页）；
/// - **长按**弹出系统上下文菜单：预览区就是 Web 的信息层（类型 / 简介），菜单里是信息层的操作
///   （订阅影片 / 已订阅→订阅管理 / 在库 / 查看详情）——信息层的每个动作都能用，海报墙也不必张张印按钮。
///
/// 角标：左上「已入库」绿斜标（libraryStatus 自动派生）/「已订阅」蓝斜标（未入库但已订阅），
/// 右上收藏红心 + 评分徽章（评分为 0 不显示）。
struct DiscoverPosterCard: View {
    let item: DiscoverPosterItem
    var action: DiscoverPosterAction = .subscribe
    /// 自定义点按目标；nil = 进发现详情页
    var onOpen: (() -> Void)?
    /// 未入库但已订阅时自动打「已订阅」斜标
    var showsSubscribedRibbon = true
    /// 海报下方的附加一行（媒体库搜索结果的「第 1 季 · 12 集 · 2160p」）
    var footnote: String?

    @Environment(Router.self) private var router
    @Environment(\.permissions) private var permissions
    @Environment(\.api) private var api

    private var subscription: API.SubscriptionView? {
        SubscriptionIndex.shared.subscription(for: item)
    }

    private var ribbon: DiscoverRibbon? {
        if let ribbon = item.ribbon { return ribbon }
        if item.libraryStatus != nil { return DiscoverRibbon(label: "已入库", tone: .owned) }
        if showsSubscribedRibbon, subscription != nil { return DiscoverRibbon(label: "已订阅", tone: .subscribed) }
        return nil
    }

    var body: some View {
        Button(action: open) {
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
        .contextMenu {
            menuActions
        } preview: {
            DiscoverPosterInfoPreview(item: item)
                .environment(\.api, api)
        }
        .accessibilityIdentifier("poster-card")
        .accessibilityLabel(accessibilityText)
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

    @ViewBuilder
    private var menuActions: some View {
        if let meta = action.subscribeMeta, permissions.canSubscribe {
            if let sub = subscription {
                // 发现页的「订阅影片」在已订阅时切成状态键（点进订阅管理）；媒体库动作已订阅时隐藏
                if action == .subscribe {
                    Button {
                        router.push(.subscription(id: sub.id))
                    } label: {
                        Label("已订阅 · \(SubscriptionStatusMeta.label(sub.status))", systemImage: "checkmark.circle")
                    }
                }
            } else {
                Button {
                    router.present(.subscribe(SubscribeRequest(titleRef: item.resolvedTitleRef, title: item.title)))
                } label: {
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

    private func open() {
        if let onOpen {
            onOpen()
        } else {
            router.push(.mediaDetail(titleRef: item.resolvedTitleRef))
        }
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

/// 海报网格的列定义：手机三列、宽屏自适应
enum DiscoverGrid {
    static let columns = [GridItem(.adaptive(minimum: 104, maximum: 170), spacing: 12, alignment: .top)]
}
