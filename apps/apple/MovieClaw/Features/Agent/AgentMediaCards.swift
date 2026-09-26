import SwiftUI

// Agent 生成式 UI：行内媒体卡片（对应 Web `lib/agent-media-cards.ts` + `components/agent-media-cards.tsx`）。
//
// 机制（docs/design/agent-generative-ui.md）：模型调用 show_media_cards_v1 时只给编号，后端只回一句固定回执；
// 客户端按编号调产品既有接口现取数据绘制卡片——卡片与发现页、媒体库页长得一样，订阅/入库/观看进度也永远是此刻的真实状态。
// 流式与历史回放走同一条路径（轨迹里的 tool_calls 与实时 tool_call 事件都会经过这里）。
// 加载与失败占同样尺寸的盒子：卡片组在时间线中间，尺寸抖动会把正在读的正文推来推去；
// 编号查不到显示「未找到」而不是整组消失——用户要能看出模型引用了不存在的东西。

/// 一张卡片的规格
enum AgentMediaCardSpec: Identifiable, Equatable {
    case library(libraryId: Int)
    case title(titleRef: String)
    case libraryItem(mediaItemId: Int, season: Int?, episode: Int?, index: Int)
    case subscription(subscriptionId: Int)

    var id: String {
        switch self {
        case let .library(id): "library:\(id)"
        case let .title(ref): ref
        case let .libraryItem(id, season, episode, index): "item:\(id)\(episode.map { ":s\(season ?? 0)e\($0)" } ?? ""):\(index)"
        case let .subscription(id): "subscription:\(id)"
        }
    }
}

/// 一次 show_media_cards 调用绘制的卡片组
struct AgentMediaCardGroup: Equatable {
    var component: String
    var title: String?
    var cards: [AgentMediaCardSpec]
}

enum AgentMediaCards {
    /// 当前能绘制的版本；工具名带版本后缀，参数契约不兼容变更时后端发 _v2，旧会话的 _v1 继续按旧规则画
    static let toolV1 = "show_media_cards_v1"

    static func isMediaCardsTool(_ name: String) -> Bool { name == toolV1 }

    private static let titleRefPattern = #"^(tmdb:(movie|tv):\d+|douban:[^:/\s]+)$"#

    /// 工具参数 → 卡片组。参数由模型生成、任何字段都可能缺失或错型：单项不合法跳过，整组一张都画不了返回 nil
    static func parse(name: String, args: AgentJSONObject?) -> AgentMediaCardGroup? {
        guard name == toolV1, let args, case let .string(component)? = args["component"],
              ["library", "title", "library_item", "subscription"].contains(component) else { return nil }
        var cards: [AgentMediaCardSpec] = []
        var seen = Set<String>()
        for (index, raw) in (args["items"]?.arrayValue ?? []).enumerated() {
            guard let item = raw.objectValue, let spec = parseItem(component, item, index) else { continue }
            // 同一编号重复给出只画一次
            if seen.insert(spec.id).inserted { cards.append(spec) }
        }
        guard !cards.isEmpty else { return nil }
        let title = args["title"]?.stringValue?.trimmingCharacters(in: .whitespaces)
        return AgentMediaCardGroup(component: component, title: (title?.isEmpty ?? true) ? nil : title, cards: cards)
    }

    private static func positiveInt(_ value: API.JSONValue?) -> Int? {
        guard case let .int(n)? = value, n > 0 else { return nil }
        return n
    }

    private static func parseItem(_ component: String, _ item: [String: API.JSONValue], _ index: Int) -> AgentMediaCardSpec? {
        switch component {
        case "library":
            return positiveInt(item["library_id"]).map { .library(libraryId: $0) }
        case "subscription":
            return positiveInt(item["subscription_id"]).map { .subscription(subscriptionId: $0) }
        case "title":
            // 首选 title_ref；只有 TMDB 编号时按 tmdb_id + media_type 拼成同一形态
            var ref = item["title_ref"]?.stringValue?.trimmingCharacters(in: .whitespaces) ?? ""
            if ref.isEmpty {
                guard let tmdb = positiveInt(item["tmdb_id"]), case let .string(type)? = item["media_type"],
                      type == "movie" || type == "tv" else { return nil }
                ref = "tmdb:\(type):\(tmdb)"
            }
            guard ref.range(of: titleRefPattern, options: .regularExpression) != nil else { return nil }
            return .title(titleRef: ref)
        default:
            guard let id = positiveInt(item["media_item_id"]) else { return nil }
            var season: Int?
            if case let .int(s)? = item["season_number"], s >= 0 { season = s }
            let episode = positiveInt(item["episode_number"])
            // 季集必须成对；只给一半按整部处理
            if season != nil, episode != nil {
                return .libraryItem(mediaItemId: id, season: season, episode: episode, index: index)
            }
            return .libraryItem(mediaItemId: id, season: nil, episode: nil, index: index)
        }
    }

    /// 处理过程块里所有 show_media_cards 调用的卡片组（参数未生成完或调用已失败的不画）
    static func groups(in items: [AgentProcessItem]) -> [(id: String, group: AgentMediaCardGroup)] {
        items.compactMap { item in
            guard case let .tool(tool) = item, tool.argsDone != false, !tool.failed,
                  let group = parse(name: tool.name, args: tool.args) else { return nil }
            return (tool.id, group)
        }
    }
}

// MARK: - 视图

/// 一组卡片：可选小标题 + 横滚行
struct AgentMediaCardsBlock: View {
    let group: AgentMediaCardGroup

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let title = group.title {
                Text(title)
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(Theme.textMuted)
            }
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(alignment: .top, spacing: 12) {
                    ForEach(group.cards) { spec in
                        AgentMediaCard(spec: spec)
                    }
                }
                .padding(.vertical, 4)
            }
            .scrollClipDisabled()
        }
        .accessibilityIdentifier("agent-media-cards")
    }
}

struct AgentMediaCard: View {
    let spec: AgentMediaCardSpec

    var body: some View {
        switch spec {
        case let .library(id): AgentLibraryCard(libraryId: id)
        case let .title(ref): AgentTitleCard(titleRef: ref)
        case let .libraryItem(id, season, episode, _): AgentLibraryItemCard(mediaItemId: id, season: season, episode: episode)
        case let .subscription(id): AgentSubscriptionCard(subscriptionId: id)
        }
    }
}

/// 加载 / 失败占位：与真实卡片同尺寸，中央一行小字
private struct AgentCardPlaceholder: View {
    var aspect: CGFloat
    var failed: Bool
    var failedText = "未找到"

    var body: some View {
        RoundedRectangle(cornerRadius: 16)
            .fill(Color.white.opacity(0.04))
            .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.white.opacity(0.06)))
            .aspectRatio(aspect, contentMode: .fit)
            .overlay {
                if failed {
                    Text(failedText).font(.system(size: 13)).foregroundStyle(Theme.textFaint).padding(.horizontal, 12)
                } else {
                    ProgressView().controlSize(.small)
                }
            }
    }
}

/// 卡片数据的三态（失败不区分原因：404 与网络错误在卡片上都是「未找到」，用户下一步都是重问）
private enum AgentCardState<T> {
    case loading, failed, ready(T)
}

// MARK: 影片海报卡（与发现页同款：已入库/已订阅斜标，长按菜单里订阅）

private struct AgentTitleCard: View {
    let titleRef: String
    @Environment(\.api) private var api
    @State private var state: AgentCardState<DiscoverPosterItem> = .loading

    var body: some View {
        Group {
            switch state {
            case .loading: AgentCardPlaceholder(aspect: 2 / 3, failed: false)
            case .failed: AgentCardPlaceholder(aspect: 2 / 3, failed: true)
            // 同 Web TitlePosterCardBody：未入库但已订阅时打「已订阅」蓝斜标
            case let .ready(item): DiscoverPosterCard(item: item, action: .subscribe, showsSubscribedRibbon: true)
            }
        }
        .frame(width: 126)
        .task(id: titleRef) {
            do {
                let detail = try await api.discoverGetTitleDetails(titleRef: titleRef)
                state = .ready(DiscoverPosterItem(detail.title))
            } catch is CancellationError {
            } catch {
                state = .failed
            }
        }
    }
}

// MARK: 媒体库卡片（封面拼贴 + 库名 + 类型与库存统计）

private struct AgentLibraryCard: View {
    let libraryId: Int
    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @State private var state: AgentCardState<API.LibraryView> = .loading

    var body: some View {
        Group {
            switch state {
            case .loading: AgentCardPlaceholder(aspect: 21 / 10, failed: false)
            case .failed: AgentCardPlaceholder(aspect: 21 / 10, failed: true, failedText: "未找到媒体库 #\(libraryId)")
            case let .ready(library): content(library)
            }
        }
        .frame(width: 212)
        .task(id: libraryId) {
            do {
                state = .ready(try await api.libraryGet(libraryId: libraryId))
            } catch is CancellationError {
            } catch {
                state = .failed
            }
        }
    }

    private func content(_ library: API.LibraryView) -> some View {
        let kind = LibraryKindMeta.label(library.kind)
        let summary = [
            kind,
            "\(library.stats.itemCount) 部",
            library.stats.totalSizeBytes > 0 ? Formatters.bytes(library.stats.totalSizeBytes) : nil,
        ].compactMap { $0 }.joined(separator: " · ")
        return Button {
            router.push(.library(id: library.id))
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                Color.clear
                    .aspectRatio(21 / 10, contentMode: .fit)
                    .overlay {
                        if library.stats.itemCount == 0 {
                            ZStack {
                                LinearGradient(colors: [Color(red: 0.11, green: 0.13, blue: 0.19), Color(red: 0.06, green: 0.07, blue: 0.11)], startPoint: .topLeading, endPoint: .bottomTrailing)
                                Image(systemName: LibraryKindMeta.symbol(library.kind)).font(.system(size: 36)).foregroundStyle(.white.opacity(0.13))
                            }
                        } else {
                            RemoteImage(url: api.image("/libraries/\(library.id)/cover"), placeholderSymbol: LibraryKindMeta.symbol(library.kind))
                        }
                    }
                    .overlay(alignment: .bottom) {
                        LinearGradient(colors: [.clear, .black.opacity(0.7)], startPoint: .top, endPoint: .bottom).frame(height: 56)
                    }
                    .overlay(alignment: .topLeading) {
                        Label(kind, systemImage: LibraryKindMeta.symbol(library.kind))
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(.white.opacity(0.85))
                            .padding(.horizontal, 8).padding(.vertical, 3)
                            .background(.black.opacity(0.55), in: .capsule)
                            .padding(8)
                    }
                    .overlay(alignment: .topTrailing) {
                        if library.isDefault {
                            Text("默认")
                                .font(.system(size: 10, weight: .semibold))
                                .foregroundStyle(.white.opacity(0.85))
                                .padding(.horizontal, 8).padding(.vertical, 3)
                                .background(.white.opacity(0.12), in: .capsule)
                                .padding(8)
                        }
                    }
                    .overlay(alignment: .bottomLeading) {
                        Text(library.name)
                            .font(.system(size: 15, weight: .semibold))
                            .foregroundStyle(.white)
                            .lineLimit(1)
                            .shadow(color: .black.opacity(0.6), radius: 3)
                            .padding(.horizontal, 12).padding(.bottom, 10)
                    }
                    .clipShape(.rect(cornerRadius: 16))
                    .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.white.opacity(0.1)))
                    .shadow(color: .black.opacity(0.38), radius: 14, y: 10)
                Text(summary)
                    .font(.system(size: 13))
                    .monospacedDigit()
                    .foregroundStyle(Theme.textMuted)
                    .lineLimit(1)
                    .padding(.horizontal, 2)
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("打开媒体库「\(library.name)」，\(summary)")
    }
}

// MARK: 库内条目播放卡（剧照 + 一键播放 + 观看进度 + 片源规格）

private struct AgentLibraryItemCard: View {
    let mediaItemId: Int
    var season: Int?
    var episode: Int?

    private struct CardData {
        var info: API.PlaybackItemView
        var detail: API.LibraryItemDetailView?
        var watch: API.PlaybackStateView?
    }

    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @State private var state: AgentCardState<CardData> = .loading

    var body: some View {
        Group {
            switch state {
            case .loading: AgentCardPlaceholder(aspect: 16 / 9, failed: false)
            case .failed: AgentCardPlaceholder(aspect: 16 / 9, failed: true, failedText: "未找到条目 #\(mediaItemId)")
            case let .ready(data): content(data)
            }
        }
        .frame(width: 212)
        .task(id: "\(mediaItemId)-\(season ?? -1)-\(episode ?? -1)") { await load() }
    }

    private func load() async {
        do {
            // 条目信息只认 media_item_id（库归属服务端按可见性解析）；拿到库 id 后并行取详情与观看状态，
            // 这两样失败不拖垮卡片——没剧照用海报铺底，没进度就不画进度条
            let info = try await api.playbackItemInfo(mediaItemId: mediaItemId)
            async let detail = try? api.libraryItemsGet(libraryId: info.libraryId, mediaItemId: info.mediaItemId)
            async let watch = try? api.playbackResume(mediaItemId: info.mediaItemId, seasonNumber: season, episodeNumber: episode)
            state = .ready(CardData(info: info, detail: await detail, watch: await watch))
        } catch is CancellationError {
        } catch {
            state = .failed
        }
    }

    private func content(_ data: CardData) -> some View {
        let info = data.info
        let unit: (season: Int, episode: Int)? = info.kind == "tv" ? season.flatMap { s in episode.map { (s, $0) } } : nil
        let code = unit.map { String(format: "S%02dE%02d", $0.season, $0.episode) }
        let context = [info.year.map(String.init), LibraryKindMeta.label(info.kind), code].compactMap { $0 }.joined(separator: " · ")
        let specs = specLine(data.detail)
        let watch = data.watch
        let progress: Double? = {
            guard let watch, watch.positionMs > 0, let duration = watch.durationMs, duration > 0 else { return nil }
            return min(1, Double(watch.positionMs) / Double(duration))
        }()
        let verb = watch?.played == true ? "重新播放" : (watch?.positionMs ?? 0) > 0 ? "继续播放" : "播放"
        let backdrop = data.detail?.backdropUrl
        let poster = data.detail?.posterUrl ?? info.posterUrl

        return VStack(alignment: .leading, spacing: 0) {
            // 两个入口叠在一起：整卡进条目页、中央播放键直接起播
            Button {
                router.push(.libraryItem(libraryId: info.libraryId, itemId: info.mediaItemId, season: unit?.season, episode: unit?.episode))
            } label: {
                Color.clear
                    .aspectRatio(16 / 9, contentMode: .fit)
                    .overlay {
                        if let backdrop {
                            RemoteImage(url: api.image(backdrop, .landscapeCard))
                        } else {
                            AgentPosterFill(title: info.title, url: api.image(poster, .posterCard))
                        }
                    }
                    .overlay(alignment: .bottom) {
                        LinearGradient(colors: [.clear, .black.opacity(0.75)], startPoint: .top, endPoint: .bottom).frame(height: 64)
                    }
                    .overlay(alignment: .topTrailing) {
                        if watch?.played == true {
                            Text("已看完")
                                .font(.system(size: 10, weight: .semibold))
                                .foregroundStyle(Color(red: 0.03, green: 0.07, blue: 0.05))
                                .padding(.horizontal, 8).padding(.vertical, 3)
                                .background(Theme.success, in: .capsule)
                                .padding(8)
                        }
                    }
                    .overlay(alignment: .bottom) {
                        if let progress {
                            GeometryReader { proxy in
                                ZStack(alignment: .leading) {
                                    Capsule().fill(.white.opacity(0.25))
                                    Capsule().fill(Theme.accent).frame(width: proxy.size.width * progress)
                                }
                            }
                            .frame(height: 3)
                            .padding(.horizontal, 8).padding(.bottom, 8)
                        }
                    }
                    .clipShape(.rect(cornerRadius: 16))
                    // 底是 Color.clear：无图时只剩一行淡字，整张卡都得显式声明可点
                    .contentShape(.rect(cornerRadius: 16))
                    .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.white.opacity(0.08)))
                    .shadow(color: .black.opacity(0.38), radius: 14, y: 10)
            }
            .buttonStyle(.plain)
            .overlay {
                Button {
                    router.play(PlayRequest(mediaItemId: info.mediaItemId, season: unit?.season, episode: unit?.episode))
                } label: {
                    Image(systemName: "play.fill")
                        .font(.system(size: 16))
                        .foregroundStyle(.white)
                        .frame(width: 40, height: 40)
                        .overlay(Circle().strokeBorder(.white.opacity(0.75), lineWidth: 1.5))
                        .shadow(color: .black.opacity(0.45), radius: 5)
                        .contentShape(Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(verb)《\(info.title)》\(code.map { " \($0)" } ?? "")")
            }

            Button {
                router.push(.libraryItem(libraryId: info.libraryId, itemId: info.mediaItemId, season: unit?.season, episode: unit?.episode))
            } label: {
                VStack(alignment: .leading, spacing: 2) {
                    Text(info.title).font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.text).lineLimit(1)
                    if !context.isEmpty {
                        Text(context).font(.system(size: 14)).monospacedDigit().foregroundStyle(Theme.textMuted).lineLimit(1)
                    }
                    if !specs.isEmpty {
                        Text(specs).font(.system(size: 13)).monospacedDigit().foregroundStyle(Theme.textFaint).lineLimit(1)
                    }
                }
                .padding(.top, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(info.title)\(context.isEmpty ? "" : "，\(context)")")
        }
    }

    /// 片源规格一行：去重的分辨率/HDR 标签 + 文件数与体积
    private func specLine(_ detail: API.LibraryItemDetailView?) -> String {
        guard let detail else { return "" }
        var tags: [String] = []
        for file in detail.files {
            for tag in [file.resolution, file.hdr].compactMap({ $0 }) where !tag.isEmpty && !tags.contains(tag) {
                tags.append(tag)
            }
        }
        if detail.fileCount > 0 { tags.append("\(detail.fileCount) 个文件") }
        if detail.totalSizeBytes > 0 { tags.append(Formatters.bytes(detail.totalSizeBytes)) }
        return tags.joined(separator: " · ")
    }
}

/// 没有横向剧照时：海报模糊铺底，中央完整保留一张清晰海报
private struct AgentPosterFill: View {
    let title: String
    let url: URL?

    var body: some View {
        if let url {
            ZStack {
                Color(red: 0.06, green: 0.07, blue: 0.11)
                RemoteImage(url: url).scaleEffect(1.25).blur(radius: 18).opacity(0.45)
                RemoteImage(url: url).aspectRatio(2 / 3, contentMode: .fit)
            }
            .clipped()
        } else {
            Text(title)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(.white.opacity(0.25))
                .padding(.horizontal, 20)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

// MARK: 订阅卡（订阅墙同款：状态斜标 + 海报底部收录进度，点进订阅详情）

private struct AgentSubscriptionCard: View {
    let subscriptionId: Int
    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @State private var state: AgentCardState<API.SubscriptionView> = .loading

    var body: some View {
        Group {
            switch state {
            case .loading: AgentCardPlaceholder(aspect: 2 / 3, failed: false)
            case .failed: AgentCardPlaceholder(aspect: 2 / 3, failed: true, failedText: "未找到订阅 #\(subscriptionId)")
            case let .ready(sub): content(sub)
            }
        }
        .frame(width: 126)
        .task(id: subscriptionId) {
            do {
                let detail = try await api.subscriptionsGet(subscriptionId: subscriptionId)
                // 详情是列表项的超集：按同名字段转成列表项，复用订阅墙的斜标与进度口径
                let data = try JSONEncoder().encode(detail)
                state = .ready(try JSONDecoder().decode(API.SubscriptionView.self, from: data))
            } catch is CancellationError {
            } catch {
                state = .failed
            }
        }
    }

    private func content(_ sub: API.SubscriptionView) -> some View {
        let meta = SubscriptionSummary.collectionMeta(sub)
        let item = DiscoverPosterItem(
            externalId: String(sub.media.tmdbId),
            mediaType: sub.media.kind,
            title: sub.media.title,
            year: sub.media.year,
            posterUrl: sub.media.posterUrl,
            ribbon: SubscriptionSummary.ribbon(sub)
        )
        // 点击进订阅详情（追踪明细 + 活动时间线）而非影片详情；已是订阅，不再给订阅键
        return DiscoverPosterCard(item: item, action: .none, onOpen: {
            router.push(.subscription(id: sub.id))
        })
        .overlay(alignment: .top) {
            if let meta {
                // 海报内部底栏：「第 2 季 · ● 3 / 10」
                GeometryReader { proxy in
                    HStack(spacing: 4) {
                        Text(meta.label)
                        Spacer(minLength: 2)
                        if meta.tracking { Circle().fill(Theme.success).frame(width: 5, height: 5) }
                        Text(meta.value).monospacedDigit()
                    }
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.9))
                    .padding(.horizontal, 7).padding(.vertical, 5)
                    .background(LinearGradient(colors: [.clear, .black.opacity(0.75)], startPoint: .top, endPoint: .bottom))
                    .frame(width: proxy.size.width)
                    .position(x: proxy.size.width / 2, y: proxy.size.width * 1.5 - 11)
                }
                .clipShape(.rect(cornerRadius: Theme.posterRadius))
                .allowsHitTesting(false)
            }
        }
    }
}
