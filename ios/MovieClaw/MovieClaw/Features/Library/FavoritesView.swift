import SwiftUI

/// 我的收藏（Web `favorites-view.tsx`，路由 `/library/favorites`）。
///
/// 跨库的一面墙：与 Jellyfin 客户端里点的心同一份名单。两种形态共用同一份排序：
/// - 海报墙：`GET /playback/favorites` 分页（每页 60），整面墙钉死 2:3，每格落回自己所属的库；
/// - 图床浏览：`GET /playback/favorites/gallery`，灯箱里的心可取消收藏（`POST /playback/marks`）。
/// 排序记在本机（同 Web localStorage 键）；进来时若上次滑得够深，底部弹「回到上次浏览的位置」。
struct FavoritesView: View {
    @Environment(\.api) private var api
    @State private var pager = LibraryWallPager<API.FavoriteItemView>(pageSize: 60)
    @State private var sort = WallSortState.load(Self.sortKey, default: WallSortState(sort: "default"), allowed: Self.sortOptions.map(\.value))
    @State private var prefs = GalleryPrefs.shared
    @State private var recallOffset: Int?
    @State private var firstVisible: Int = 0
    @State private var didOfferRecall = false
    @State private var scrollProxy: ScrollViewProxy?

    private static let sortKey = "movieclaw.favorites.wall-sort"
    private static let recallScope = "library:favorites"

    /// 默认档叫「最近收藏」，其余同单库页
    static let sortOptions: [WallSortMenu.Option] = [
        .init(value: "default", label: "最近收藏", direction: WallSortDirections.of("favorited_at")),
        .init(value: "title", label: "按标题", direction: WallSortDirections.of("title")),
        .init(value: "added_at", label: "最近添加", direction: WallSortDirections.of("added_at")),
        .init(value: "release_date", label: "按上映时间", direction: WallSortDirections.of("release_date")),
        .init(value: "rating", label: "按评分", direction: WallSortDirections.of("rating")),
        .init(value: "runtime", label: "按片长", direction: WallSortDirections.of("runtime")),
        .init(value: "size", label: "按体积", direction: WallSortDirections.of("size")),
        .init(value: "last_played", label: "最近观看", direction: WallSortDirections.of("last_played")),
    ]

    private var effectiveSort: String { sort.sort == "default" ? "favorited_at" : sort.sort }
    private var order: String? { WallSortDirections.of(effectiveSort)?.orderParam(reversed: sort.reversed) }
    private var sortKeyString: String { "\(sort.sort == "default" ? "" : effectiveSort)\(sort.reversed ? ":rev" : "")" }
    private var recallView: String { sortKeyString.isEmpty ? "favorites" : "favorites:\(sortKeyString)" }
    private var empty: Bool { pager.items?.isEmpty == true }
    private var gallery: Bool { prefs.galleryMode && !empty }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    header
                    content
                }
                .padding(.bottom, 32)
            }
            .onScrollTargetVisibilityChange(idType: Int.self, threshold: 0.2) { ids in
                trackVisible(ids)
            }
            .overlay(alignment: .bottom) {
                if let recallOffset {
                    WallRecallPill {
                        self.recallOffset = nil
                        Task {
                            await pager.jump(to: recallOffset)
                            if let first = pager.items?.first { proxy.scrollTo(first.id, anchor: .top) }
                        }
                    } onDismiss: {
                        self.recallOffset = nil
                    }
                }
            }
            .animation(.snappy, value: recallOffset)
            .onAppear { scrollProxy = proxy }
        }
        .appBackground()
        .navigationTitle("我的收藏")
        .toolbar {
            if !empty {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        prefs.galleryMode.toggle()
                    } label: {
                        Image(systemName: gallery ? "square.grid.2x2" : "photo.on.rectangle.angled")
                    }
                    .accessibilityLabel(gallery ? "回到海报墙" : "图床浏览")
                }
                if gallery {
                    ToolbarItem(placement: .topBarTrailing) {
                        Menu {
                            GalleryPrefMenuItems()
                        } label: {
                            Image(systemName: "slider.horizontal.3")
                        }
                        .accessibilityLabel("浏览设置")
                    }
                }
            }
        }
        .task(id: sort) {
            sort.save(Self.sortKey)
            await reload()
        }
        .onAppear { Task { await pager.refresh() } }
        .refreshable { await pager.refresh() }
    }

    @ViewBuilder
    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(pager.items == nil ? "正在读取收藏…"
                : (pager.total ?? 0) > 0 ? "\(pager.total ?? 0) 部作品 · 与 Jellyfin 客户端里点的心同一份"
                : "还没有收藏。在影片页点心，或在 Jellyfin 客户端里收藏，都会出现在这里。")
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)
                .accessibilityIdentifier("favorites-summary")
            if pager.items != nil, !empty {
                WallSortMenu(options: Self.sortOptions, state: $sort)
                    .glassEffect(.regular.interactive(), in: .capsule)
            }
        }
        .padding(.horizontal, Theme.pagePadding)
        .padding(.top, 4)
    }

    @ViewBuilder
    private var content: some View {
        if let failed = pager.failed, pager.items == nil {
            ErrorState(title: "收藏加载失败", message: failed) { await reload() }
        } else if pager.items == nil {
            ProgressView().frame(maxWidth: .infinity).padding(.top, 60)
        } else if gallery {
            LibraryGalleryWall(reloadKey: AnyHashable(sortKeyString)) { [api, effectiveSort, order] offset, limit in
                try await api.playbackFavoritesGallery(limit: limit, offset: offset, sort: effectiveSort, order: order)
            }
        } else if let items = pager.items, !items.isEmpty {
            WallLoadPreviousSentinel(start: pager.start) {
                let anchor = pager.items?.first?.id
                await pager.loadPrevious()
                if let anchor, let proxy = scrollProxy { proxy.scrollTo(anchor, anchor: .top) }
            }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 140), spacing: 12, alignment: .top)], alignment: .leading, spacing: 20) {
                ForEach(items) { item in
                    NavigationLink(value: AppRoute.libraryItem(libraryId: item.libraryId, itemId: item.mediaItemId)) {
                        let dead = item.fileCount > 0 && item.missingCount >= item.fileCount
                        LibraryPosterCell(
                            title: item.title, year: item.year,
                            url: api.image(item.posterUrl, ImageVariant.card(aspect: item.primaryAspect)),
                            imageAspect: item.primaryAspect, frameAspect: Theme.posterAspect,
                            favorite: item.isFavorite, dead: dead,
                            abnormal: dead ? "文件已全部缺失" : item.missingCount > 0 ? "\(item.missingCount) 个文件缺失" : nil,
                            cornerLabel: favoriteLevelLabel(kind: item.kind, season: item.favoriteSeasonNumber, episode: item.favoriteEpisodeNumber)
                        )
                    }
                    .buttonStyle(.plain)
                    .id(item.mediaItemId)
                }
            }
            .scrollTargetLayout()
            .padding(.horizontal, Theme.pagePadding)
            WallLoadMoreFooter(hasMore: pager.hasMore, start: pager.start, loaded: items.count, total: pager.total) {
                await pager.loadMore()
            }
        }
    }

    private func reload() async {
        let api = self.api
        let sort = effectiveSort
        let order = self.order
        let pager = self.pager
        await pager.reset({ offset, limit in
            let page = try await api.playbackFavorites(limit: limit, offset: offset, sort: sort, order: order)
            await MainActor.run { pager.total = page.total }
            return page.items
        })
        if !didOfferRecall {
            didOfferRecall = true
            recallOffset = LibraryWallRecall.read(scope: Self.recallScope, view: recallView).flatMap { $0 < (pager.total ?? 0) ? $0 : nil }
        }
    }

    /// 记下第一格的位置；往下滑够一屏，「回到上次位置」胶囊自己让位
    private func trackVisible(_ ids: [Int]) {
        guard let offsets = pager.items.map({ items in ids.compactMap { id in items.firstIndex { $0.mediaItemId == id } } }),
              let first = offsets.min() else { return }
        let offset = pager.start + first
        if recallOffset != nil, offset - pager.start >= 12 { recallOffset = nil }
        if offset != firstVisible, recallOffset == nil {
            firstVisible = offset
            LibraryWallRecall.write(scope: Self.recallScope, view: recallView, offset: offset)
        }
    }
}
