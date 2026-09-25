import SwiftUI

/// 搜索结果页（对应 Web `app/(app)/search/page.tsx`）：`/search?q=&tab=&scope=&snapshot=&for_sub=`。
///
/// 顶部垂直选项卡「影视 | 站点资源 | 媒体库」（按权限裁剪），站点资源下还有范围 chips（全部 + 可见分类/预设，
/// 切换即按新范围重新搜索）。各垂直**惰性挂载 + 切换保活**：站点资源的跨站搜索是秒级重操作，
/// 只有真正切到它才发起；切走后流式搜索照常进行、结果保留，切回来不重搜。
///
/// 关键词为空 = 浏览模式：只逛站点资源的分类列表页（影视/媒体库没有「浏览」语义）。
/// 快照（snapshot）属于打开它的那个垂直；另一个垂直一律实时搜索。
/// 路由的 `scope` 参数是 `SearchScope.encoded` 的查询串（label / cats / sites / poster / private）。
struct SearchResultsView: View {
    let query: AppRoute.SearchQuery

    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions

    @State private var vertical: SearchVertical = .torrent
    @State private var scope = SearchScope.all
    @State private var access = SearchAccess()
    @State private var tabs: [SearchTab] = []
    @State private var visited: Set<SearchVertical> = []
    @State private var torrentModel: TorrentSearchModel?
    @State private var torrentSnapshot: Int?
    @State private var mediaSnapshot: Int?
    @State private var grabTarget: (id: Int, title: String)?
    @State private var initialized = false

    private var keyword: String { query.q.trimmingCharacters(in: .whitespaces) }
    private var browsing: Bool { keyword.isEmpty }

    private var visibleVerticals: [SearchVertical] {
        access.available.filter { !browsing || $0 == .torrent }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if access.ready, visibleVerticals.isEmpty {
                EmptyState(systemImage: "magnifyingglass", title: "无法搜索", message: "当前账号没有可用的搜索入口，请联系管理员调整成员权限。")
            } else {
                selector
                ZStack(alignment: .top) {
                    if visited.contains(.media), access.canMedia {
                        MediaSearchResultsView(
                            keyword: keyword,
                            snapshotId: mediaSnapshot,
                            onResearch: { mediaSnapshot = nil },
                            onSwitchToTorrent: access.canTorrent ? { switchTo(.torrent) } : nil
                        )
                        .id("media-\(mediaSnapshot ?? -1)")
                        .opacity(vertical == .media ? 1 : 0)
                        .allowsHitTesting(vertical == .media)
                    }
                    if visited.contains(.torrent), access.canTorrent, let torrentModel {
                        TorrentResultsView(model: torrentModel, grabTarget: grabTarget, onResearch: {
                            torrentSnapshot = nil
                            rebuildTorrentModel()
                        })
                        .id(ObjectIdentifier(torrentModel))
                        .opacity(vertical == .torrent ? 1 : 0)
                        .allowsHitTesting(vertical == .torrent)
                    }
                    if visited.contains(.library), access.canLibrary {
                        LibrarySearchResultsView(keyword: keyword, onSwitchToMedia: access.canMedia ? { switchTo(.media) } : nil)
                            .opacity(vertical == .library ? 1 : 0)
                            .allowsHitTesting(vertical == .library)
                    }
                    if !access.ready {
                        ProgressView().padding(.top, 60)
                    }
                }
                .frame(maxHeight: .infinity, alignment: .top)
            }
        }
        .appBackground()
        .navigationTitle(browsing ? "浏览\(scope.label ?? "站点资源")" : "搜索“\(keyword)”")
        .navigationBarTitleDisplayMode(.inline)
        .tracksSubscriptionIndex()
        .task { await initialize() }
    }

    private func initialize() async {
        guard !initialized else { return }
        initialized = true
        scope = SearchScope(encoded: query.scope)
        var target = browsing ? .torrent : SearchVertical(routeTab: query.tab)
        access = await SearchAccess.resolve(api: api, permissions: permissions)
        // 当前垂直不可用：落到第一个可用垂直（快照属于原垂直，丢掉）
        var snapshot = query.snapshot
        if !browsing, !access.available.contains(target), let first = access.available.first {
            target = first
            snapshot = nil
        }
        vertical = target
        mediaSnapshot = target == .media ? snapshot : nil
        torrentSnapshot = target == .torrent ? snapshot : nil
        rebuildTorrentModel()
        visited.insert(target)
        tabs = await SearchTabs.visible(api: api, isAdmin: permissions.isAdmin)
        if let subId = query.forSubscription, let detail = try? await api.subscriptionsGet(subscriptionId: subId) {
            grabTarget = (detail.id, detail.media.title)
        }
    }

    // MARK: 顶部选择器

    private var selector: some View {
        VStack(alignment: .leading, spacing: 10) {
            if visibleVerticals.count > 1 {
                Picker("搜索垂直类别", selection: Binding(get: { vertical }, set: { switchTo($0) })) {
                    ForEach(visibleVerticals, id: \.self) { Text($0.label).tag($0) }
                }
                .pickerStyle(.segmented)
                .accessibilityIdentifier("search-vertical")
            }
            if vertical == .torrent {
                // 分类是真实搜索范围而非结果筛选：点击后按该范围重新请求站点
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        DiscoverChip(label: "全部", active: scope == .all) { switchScope(.all) }
                        ForEach(tabs, id: \.key) { tab in
                            DiscoverChip(label: tab.label, active: scope == tab.scope) { switchScope(tab.scope) }
                        }
                    }
                }
                .scrollClipDisabled()
                .accessibilityIdentifier("search-scope")
            }
        }
        .padding(.horizontal, Theme.pagePadding)
        .padding(.top, 8)
        .padding(.bottom, 6)
    }

    /// 切换垂直：只切显示，范围与已出的结果保留
    private func switchTo(_ target: SearchVertical) {
        guard target != vertical else { return }
        vertical = target
        visited.insert(target)
    }

    /// 切换分类范围：关键词不变，按新范围重新搜索（丢掉快照）
    private func switchScope(_ next: SearchScope) {
        guard next != scope || torrentSnapshot != nil else { return }
        scope = next
        torrentSnapshot = nil
        rebuildTorrentModel()
    }

    private func rebuildTorrentModel() {
        torrentModel = TorrentSearchModel(keyword: keyword, scope: scope, snapshotId: torrentSnapshot)
    }
}

// MARK: - 影视垂直

/// 「影视」垂直（对应 Web `media-search-results.tsx`）：`POST /search/titles` 同时搜豆瓣与 TMDB，
/// 两个来源分区并列（没有可靠对齐键，不合并去重），单边失败只在该分区提示。
/// 快照回放读历史留存结果，不访问上游；两边都空时给「搜索站点资源」逃生入口。
struct MediaSearchResultsView: View {
    let keyword: String
    let snapshotId: Int?
    let onResearch: () -> Void
    let onSwitchToTorrent: (() -> Void)?

    @Environment(\.api) private var api
    @State private var douban: [DiscoverPosterItem]?
    @State private var doubanError: String?
    @State private var tmdb: [DiscoverPosterItem]?
    @State private var tmdbError: String?
    @State private var snapshotAt: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                HStack(alignment: .firstTextBaseline) {
                    Text("“\(keyword)”").font(.title2.weight(.semibold)).foregroundStyle(.white).lineLimit(1)
                    Spacer()
                    if let snapshotAt {
                        Label("\(Formatters.relative(snapshotAt))的快照", systemImage: "clock")
                            .font(.caption).foregroundStyle(Theme.textMuted)
                        Button("重新搜索", action: onResearch)
                            .font(.caption.weight(.semibold))
                            .accessibilityIdentifier("media-research")
                    }
                }
                let settled = (douban != nil || doubanError != nil) && (tmdb != nil || tmdbError != nil)
                let empty = (douban?.isEmpty ?? true) && (tmdb?.isEmpty ?? true)
                if settled, empty {
                    let error = doubanError ?? tmdbError
                    VStack(spacing: 8) {
                        Text(error != nil ? "影视搜索出错" : "没有找到相关影视条目").font(.headline).foregroundStyle(.white)
                        Text(error ?? "换个关键词试试；如果找的是非影视资源，可以直接搜索站点。")
                            .font(.subheadline).foregroundStyle(Theme.textMuted).multilineTextAlignment(.center)
                        if let onSwitchToTorrent {
                            Button("搜索站点资源", action: onSwitchToTorrent)
                                .discoverProminentButton()
                                .padding(.top, 8)
                        }
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.top, 60)
                    .accessibilityIdentifier("media-empty")
                } else {
                    section("豆瓣", items: douban, error: doubanError)
                    section("TMDB", items: tmdb, error: tmdbError)
                }
            }
            .padding(.horizontal, Theme.pagePadding)
            .padding(.bottom, 40)
        }
        .task { await load() }
        .accessibilityIdentifier("media-results")
    }

    private func section(_ label: String, items: [DiscoverPosterItem]?, error: String?) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                DiscoverTag(text: label, foreground: Theme.accent, background: .black.opacity(0.3))
                if let items, !items.isEmpty {
                    Text("共 \(items.count) 条结果").font(.subheadline).foregroundStyle(Theme.textMuted)
                }
            }
            if items == nil, error == nil {
                LazyVGrid(columns: DiscoverGrid.columns, spacing: 20) {
                    ForEach(0 ..< 6, id: \.self) { _ in
                        DiscoverSkeletonBlock(cornerRadius: Theme.posterRadius).aspectRatio(2 / 3, contentMode: .fit)
                    }
                }
            }
            if let error {
                Text(error).font(.subheadline).foregroundStyle(Theme.textMuted)
            }
            if let items {
                if items.isEmpty {
                    Text("该来源没有找到相关条目").font(.subheadline).foregroundStyle(Theme.textMuted)
                } else {
                    LazyVGrid(columns: DiscoverGrid.columns, spacing: 20) {
                        ForEach(items) { DiscoverPosterCard(item: $0) }
                    }
                }
            }
        }
    }

    private func load() async {
        if let snapshotId {
            if let snap = try? await api.titleSearchSnapshot(historyId: snapshotId) {
                snapshotAt = snap.snapshotAt
                douban = snap.items.filter { $0.source == "douban" }
                tmdb = snap.items.filter { $0.source == "tmdb" }
                return
            }
            // 快照缺失（被清理/老数据）回退实时搜索
        }
        do {
            let result = try await api.searchTitles(body: .init(query: keyword, provider: "all", saveHistory: true))
            let items = result.titles.map(DiscoverPosterItem.init)
            douban = items.filter { $0.source == "douban" }
            tmdb = items.filter { $0.source == "tmdb" }
            if let status = result.providers.first(where: { $0.provider == "douban" }), !status.success {
                doubanError = status.message ?? "豆瓣搜索失败"
            }
            if let status = result.providers.first(where: { $0.provider == "tmdb" }), !status.success {
                tmdbError = status.message ?? "TMDB 搜索失败"
            }
        } catch is CancellationError {
        } catch {
            let message = error.localizedDescription.isEmpty ? "影视搜索失败，请稍后重试" : error.localizedDescription
            doubanError = message
            tmdbError = message
        }
    }
}

// MARK: - 媒体库垂直

/// 「媒体库」垂直（对应 Web `library-search-results.tsx`）：`GET /search/library-items`，
/// 跨全部可见媒体库按标题/原名匹配，按库分组；格下标注库存概况。空态出口指向「影视」。
struct LibrarySearchResultsView: View {
    let keyword: String
    let onSwitchToMedia: (() -> Void)?

    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @State private var groups: [API.LibrarySearchGroupView]?
    @State private var error: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                Text("“\(keyword)”").font(.title2.weight(.semibold)).foregroundStyle(.white).lineLimit(1)
                if groups == nil, error == nil {
                    LazyVGrid(columns: DiscoverGrid.columns, spacing: 20) {
                        ForEach(0 ..< 6, id: \.self) { _ in
                            DiscoverSkeletonBlock(cornerRadius: Theme.posterRadius).aspectRatio(2 / 3, contentMode: .fit)
                        }
                    }
                }
                if error != nil || groups?.isEmpty == true {
                    VStack(spacing: 8) {
                        Text(error != nil ? "媒体库搜索出错" : "媒体库中没有找到相关影片").font(.headline).foregroundStyle(.white)
                        Text(error ?? "已入库条目按标题和原名匹配；库里还没有的片子，去影视条目里找。")
                            .font(.subheadline).foregroundStyle(Theme.textMuted).multilineTextAlignment(.center)
                        if let onSwitchToMedia {
                            Button("搜索影视条目", action: onSwitchToMedia)
                                .discoverProminentButton()
                                .padding(.top, 8)
                        }
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.top, 60)
                    .accessibilityIdentifier("library-empty")
                }
                ForEach(groups ?? [], id: \.libraryId) { group in
                    VStack(alignment: .leading, spacing: 12) {
                        HStack(spacing: 10) {
                            DiscoverTag(text: group.libraryName, foreground: Theme.accent, background: .black.opacity(0.3))
                            Text("共 \(group.items.count) 条结果").font(.subheadline).foregroundStyle(Theme.textMuted)
                        }
                        LazyVGrid(columns: DiscoverGrid.columns, spacing: 20) {
                            ForEach(group.items, id: \.mediaItemId) { item in
                                cell(item, libraryId: group.libraryId)
                            }
                        }
                    }
                    .accessibilityIdentifier("library-group")
                }
            }
            .padding(.horizontal, Theme.pagePadding)
            .padding(.bottom, 40)
        }
        .task {
            do {
                groups = try await api.searchLibraryItems(keyword: keyword)
            } catch is CancellationError {
            } catch {
                self.error = error.localizedDescription.isEmpty ? "媒体库搜索失败，请稍后重试" : error.localizedDescription
            }
        }
        .accessibilityIdentifier("library-results")
    }

    private func cell(_ item: API.LibraryItemView, libraryId: Int) -> some View {
        let visual = DiscoverPosterItem(
            externalId: item.tmdbId.map(String.init) ?? "local:\(item.mediaItemId)",
            source: "tmdb",
            mediaType: item.kind == "movie" || item.kind == "tv" ? item.kind : nil,
            title: item.title,
            year: item.year,
            posterUrl: item.posterUrl,
            favorite: item.isFavorite,
            aspect: CGFloat(item.primaryAspect)
        )
        var parts: [String] = []
        if item.kind == "tv", !item.seasons.isEmpty {
            parts.append(item.seasons.count == 1 ? "第 \(item.seasons[0]) 季 · \(item.episodeCount) 集" : "\(item.seasons.count) 季 · \(item.episodeCount) 集")
        }
        if !item.resolutions.isEmpty { parts.append(item.resolutions.joined(separator: "/")) }
        // 与单库海报墙同口径：剧集按季集完整度给「自动续订 / 补齐缺集」
        let action: DiscoverPosterAction = {
            guard item.kind == "tv", let summary = item.inventorySummary else { return .none }
            return summary.allSeasonsOwned && summary.allEpisodesOwned ? .follow : .backfill
        }()
        return DiscoverPosterCard(
            item: visual,
            action: action,
            onOpen: { router.push(.libraryItem(libraryId: libraryId, itemId: item.mediaItemId)) },
            showsSubscribedRibbon: false,
            footnote: parts.isEmpty ? nil : parts.joined(separator: " · ")
        )
    }
}
