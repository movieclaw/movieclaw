import SwiftUI

/// 首页行清单偏好的进程内共享副本。
///
/// Web 把全站界面偏好放在一个 React Context 里（应用启动拉一次）；App 里只有媒体库首页与
/// 自定义页用到 `home.rows`，放一个模块级单例即可：自定义页保存后直接写回这里，
/// 返回首页时立刻按新清单渲染，不必等下一轮轮询。
@Observable
final class LibraryHomePrefs {
    static let shared = LibraryHomePrefs()
    /// nil = 还没从服务器拉到
    var rows: [API.HomeRowPref]?

    /// 保存首页行清单。后端 PUT `/ui/preferences` 是整体覆盖，所以以当前完整偏好为底只换 home.rows；
    /// 成功后写回共享副本（自定义页、合集页「显示在首页」共用）。
    func save(_ rows: [API.HomeRowPrefInput], api: APIClient) async throws {
        let base = try await api.uiPrefsShow()
        var input = try JSONDecoder().decode(API.UiPreferencesSettingInput.self, from: JSONEncoder().encode(base))
        input.home = API.HomeUiPrefsInput(rows: rows)
        let saved = try await api.uiPrefsUpdate(body: input)
        self.rows = saved.home.rows
    }
}

/// 媒体库首页（Web `library-view.tsx`，路由 `/library`）。
///
/// 页面 = 标题统计 + 按 `ui.preferences.home.rows` 合并出的行清单：
/// 接下来继续 / 我的收藏 / 我的媒体库（库卡片 + 扫描进度环）/ 每库一行 / 合集行。
/// 只负责「看」，排序与行的增删改全部收进自定义页。
///
/// 刷新策略同 Web：有库在扫描/整理时 3 秒一轮（结束后再保持 12 秒快轮询，接住监控去抖触发的连环扫描），
/// 元数据刷新 5 秒，有文件写入中等待入账 10 秒，完全空闲 30 秒；
/// 库状态、要取的行、合集都没变时不重拉各行条目（空闲时每 30 秒不必打 1+N 个请求）。
/// 瞬时失败不清已有数据，只挂提示条；一次都没成功过才整页报错。
struct LibraryHomeView: View {
    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Router.self) private var router
    @State private var prefs = LibraryHomePrefs.shared

    @State private var libraries: [API.LibraryView]?
    @State private var collections: [API.CollectionView] = []
    @State private var upNext: [API.UpNextItemView]?
    @State private var favorites: API.FavoritesView?
    @State private var itemsByKey: [String: [API.LibraryItemView]] = [:]
    @State private var failed = false
    @State private var lastSnapshot: String?
    @State private var busyUntil: Date = .distantPast
    @State private var clearingLibrary = false

    private static let rowCount = 20

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                header
                content
            }
            .padding(.bottom, 32)
        }
        .appBackground()
        .navigationTitle("媒体库")
        .toolbarTitleDisplayMode(.inlineLarge)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                NavigationLink(value: AppRoute.libraryCustomize) {
                    Image(systemName: "list.bullet")
                }
                .accessibilityLabel("自定义首页")
                .accessibilityIdentifier("library-customize")
            }
            if permissions.canManageLibraries {
                ToolbarItem(placement: .topBarTrailing) {
                    NavigationLink(value: AppRoute.libraryManage()) {
                        Image(systemName: "gearshape")
                    }
                    .accessibilityLabel("管理媒体库")
                }
            }
        }
        .refreshable { await reload() }
        .onAppear { Task { await reload() } }
        .polling(every: pollInterval) { await reload() }
        .sheet(isPresented: $clearingLibrary) {
            ClearLibraryHistorySheet(libraries: visibleLibraries) { Task { await reload() } }
        }
    }

    // MARK: 派生状态

    private var visibleLibraries: [API.LibraryView] { (libraries ?? []).filter(\.viewerAccess) }

    private var rows: [HomeRows.Row] {
        HomeRows.build(prefs: prefs.rows ?? [], libraries: libraries ?? [], collections: collections)
    }

    private var pollInterval: Double {
        let libs = libraries ?? []
        if libs.contains(where: { $0.scanning || $0.organizing }) || Date.now < busyUntil { return 3 }
        if libs.contains(where: { $0.metadataRefresh?.refreshing == true }) { return 5 }
        if libs.contains(where: { !$0.scanning && !$0.organizing && ($0.lastScan?.deferred ?? 0) > 0 }) { return 10 }
        return 30
    }

    // MARK: 页头

    @ViewBuilder
    private var header: some View {
        let visibleRows = rows.filter { !$0.hidden }
        let librariesRowVisible = visibleRows.contains { $0.kind == .libraries } && !visibleLibraries.isEmpty
        // 「全部合集」默认挂在「我的媒体库」行标题右侧；那一行不在时抬到页头，保证始终有路进得去
        let collectionsEntryInHeader = !collections.isEmpty && !librariesRowVisible
        HStack(alignment: .top) {
            Text(failed && libraries == nil ? "暂时无法获取媒体库统计，正在自动重试" : libraryStatsSummary(libraries == nil ? nil : visibleLibraries))
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)
                .lineLimit(2)
                .accessibilityIdentifier("library-stats")
            Spacer(minLength: 8)
            if collectionsEntryInHeader {
                NavigationLink(value: AppRoute.allCollections) {
                    Text("全部合集 ›").font(.subheadline).foregroundStyle(Theme.textFaint)
                }
            }
        }
        .padding(.horizontal, Theme.pagePadding)
        .padding(.top, 4)
    }

    @ViewBuilder
    private var content: some View {
        if libraries == nil, !failed {
            HStack(spacing: 10) {
                ProgressView()
                Text("正在加载媒体库…")
            }
            .font(.subheadline)
            .foregroundStyle(Theme.textMuted)
            .frame(maxWidth: .infinity)
            .padding(.top, 64)
        } else if libraries == nil, failed {
            VStack(spacing: 12) {
                Text("媒体库加载失败").font(.subheadline).foregroundStyle(Theme.textMuted)
                Button("重试") { Task { await reload() } }.buttonStyle(.glass)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 64)
        } else if let libraries {
            if failed {
                Text("与后端通信失败，正在自动重试；下方显示的是最近一次成功加载的数据")
                    .font(.footnote)
                    .foregroundStyle(Color(red: 0.99, green: 0.9, blue: 0.54))
                    .padding(.horizontal, 16).padding(.vertical, 12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Theme.warning.opacity(0.1), in: .rect(cornerRadius: 12))
                    .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.warning.opacity(0.25)))
                    .padding(.horizontal, Theme.pagePadding)
                    .padding(.top, 16)
            }
            if libraries.isEmpty {
                EmptyState(
                    systemImage: "film.stack",
                    title: permissions.canManageLibraries ? "为收藏准备一个家" : "还没有可浏览的媒体库",
                    message: permissions.canManageLibraries
                        ? "创建电影库或剧集库，选好根目录后，订阅完成的内容会自动整理到这里。"
                        : "当前账号暂时没有可浏览的媒体库，请联系管理员分配媒体库权限。",
                    actionTitle: permissions.canManageLibraries ? "创建第一个媒体库" : nil,
                    action: permissions.canManageLibraries ? { router.push(.libraryManage(create: true)) } : nil
                )
                .padding(.top, 40)
            } else {
                let visibleRows = rows.filter { !$0.hidden }
                if visibleRows.isEmpty {
                    VStack(spacing: 6) {
                        Text("首页空空如也").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text)
                        Text("所有行都被隐藏了。到「自定义首页」挑几行回来，或恢复默认。")
                            .font(.footnote).foregroundStyle(Theme.textMuted).multilineTextAlignment(.center)
                        NavigationLink(value: AppRoute.libraryCustomize) { Text("自定义首页") }
                            .buttonStyle(.glass)
                            .padding(.top, 10)
                    }
                    .padding(.horizontal, 24).padding(.vertical, 32)
                    .frame(maxWidth: .infinity)
                    .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [5])).foregroundStyle(.white.opacity(0.15)))
                    .padding(.horizontal, Theme.pagePadding)
                    .padding(.top, 64)
                    .accessibilityIdentifier("home-all-hidden")
                }
                ForEach(visibleRows) { row in
                    rowView(row)
                }
            }
        }
    }

    // MARK: 行

    @ViewBuilder
    private func rowView(_ row: HomeRows.Row) -> some View {
        switch row.kind {
        case .upNext:
            if let upNext, !upNext.isEmpty {
                VStack(alignment: .leading, spacing: 12) {
                    LibrarySectionHeader(title: row.title) {
                        WatchHistoryMenu(onCleared: { Task { await reload() } }, pickLibrary: { clearingLibrary = true })
                    }
                    ScrollView(.horizontal, showsIndicators: false) {
                        LazyHStack(alignment: .top, spacing: 12) {
                            ForEach(upNext, id: \.mediaItemId) { UpNextCard(item: $0) }
                        }
                        .padding(.horizontal, Theme.pagePadding)
                    }
                    .scrollClipDisabled()
                }
                .padding(.top, 24)
                .accessibilityIdentifier("up-next-row")
            }
        case .favorites:
            if let favorites, !favorites.items.isEmpty {
                posterRow(
                    title: row.title,
                    moreTitle: "查看全部 \(favorites.total) 部",
                    more: .favorites,
                    items: favorites.items.map { item in
                        PosterRowItem(
                            id: item.mediaItemId, libraryId: item.libraryId, title: item.title, year: item.year,
                            posterUrl: item.posterUrl, aspect: item.primaryAspect,
                            info: favoriteLevelLabel(kind: item.kind, season: item.favoriteSeasonNumber, episode: item.favoriteEpisodeNumber).map { [$0] } ?? []
                        )
                    }
                )
                .accessibilityIdentifier("favorites-row")
            }
        case .libraries:
            if !visibleLibraries.isEmpty {
                VStack(alignment: .leading, spacing: 12) {
                    LibrarySectionHeader(title: row.title) {
                        if !collections.isEmpty {
                            NavigationLink(value: AppRoute.allCollections) { Text("全部合集 ›") }
                                .accessibilityIdentifier("all-collections-link")
                        }
                    }
                    ScrollView(.horizontal, showsIndicators: false) {
                        LazyHStack(spacing: 14) {
                            ForEach(visibleLibraries, id: \.id) { library in
                                NavigationLink(value: AppRoute.library(id: library.id)) {
                                    LibraryHomeCard(library: library, hasPosters: !(itemsByKey[Self.coverKey(library.id)] ?? []).isEmpty)
                                }
                                .buttonStyle(.plain)
                                .accessibilityIdentifier("library-card-\(library.id)")
                            }
                        }
                        .padding(.horizontal, Theme.pagePadding)
                    }
                    .scrollClipDisabled()
                }
                .padding(.top, 24)
            }
        case let .library(library, _, _, _, _, _):
            let items = itemsByKey[Self.fetchKey(row)] ?? []
            if !items.isEmpty {
                posterRow(title: row.title, moreTitle: "查看全部", more: .library(id: library.id), items: items.map { PosterRowItem($0, fallbackLibrary: library.id) })
                    .accessibilityIdentifier("home-row-\(row.id)")
            }
        case let .collection(collection, _, _, _):
            let items = itemsByKey[Self.fetchKey(row)] ?? []
            if !items.isEmpty {
                posterRow(title: row.title, moreTitle: "查看全部", more: .collection(libraryId: collection.libraryId, collectionId: collection.id),
                          items: items.map { PosterRowItem($0, fallbackLibrary: collection.libraryId ?? 0) })
                    .accessibilityIdentifier("home-row-\(row.id)")
            }
        }
    }

    private func posterRow(title: String, moreTitle: String, more: AppRoute, items: [PosterRowItem]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            LibrarySectionHeader(title: title) {
                NavigationLink(value: more) { Text(moreTitle) }
            }
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(alignment: .top, spacing: 12) {
                    ForEach(items) { item in
                        NavigationLink(value: AppRoute.libraryItem(libraryId: item.libraryId, itemId: item.id)) {
                            LibraryPosterCell(title: item.title, year: item.year, url: api.image(item.posterUrl, .posterCard), imageAspect: item.aspect)
                                .frame(width: 124)
                        }
                        .buttonStyle(.plain)
                        .contextMenu {
                            ForEach(item.info, id: \.self) { Text($0) }
                        }
                    }
                }
                .padding(.horizontal, Theme.pagePadding)
            }
            .scrollClipDisabled()
        }
        .padding(.top, 24)
    }

    // MARK: 加载

    /// 一行取数的缓存键：同一个库同一种排序同一方向（同一个未看开关）只请求一次
    private static func fetchKey(_ row: HomeRows.Row) -> String {
        switch row.kind {
        case let .library(library, sort, reversed, unwatched, _, _): "lib:\(library.id):\(sort):\(reversed):\(unwatched)"
        case let .collection(collection, sort, reversed, _): "col:\(collection.id):\(sort):\(reversed)"
        default: row.id
        }
    }

    /// 库卡片封面用的那批条目，与默认的「最近添加」行共用一份
    private static func coverKey(_ libraryId: Int) -> String { "lib:\(libraryId):added_at:false:false" }

    private func reload() async {
        do {
            // 偏好：首次进入拉一次（之后由自定义页写回共享副本）
            if prefs.rows == nil {
                prefs.rows = (try? await api.uiPrefsShow())?.home.rows ?? []
            }
            async let libsTask = api.libraryList()
            async let colsTask = try? api.collectionList()
            let libs = try await libsTask
            let cols = await colsTask ?? collections
            failed = false
            if libs != libraries { libraries = libs }
            if cols != collections { collections = cols }
            if libs.contains(where: { $0.scanning || $0.organizing }) { busyUntil = .now.addingTimeInterval(12) }

            let visibleRows = HomeRows.build(prefs: prefs.rows ?? [], libraries: libs, collections: cols).filter { !$0.hidden }
            let favoritesRow = visibleRows.first { if case .favorites = $0.kind { true } else { false } }
            let wantsUpNext = visibleRows.contains { $0.kind == .upNext }
            let api = self.api
            async let upNextTask: [API.UpNextItemView]? = wantsUpNext ? (try? await api.playbackUpNext(limit: Self.rowCount))?.items : nil
            async let favoritesTask: API.FavoritesView? = Self.fetchFavorites(api, favoritesRow)
            let (latestUpNext, latestFavorites) = await (upNextTask, favoritesTask)
            if let latestUpNext { upNext = latestUpNext } else if upNext == nil { upNext = [] }
            if let latestFavorites { favorites = latestFavorites } else if favorites == nil { favorites = API.FavoritesView(items: [], total: 0) }

            // 各行条目：库状态、要取的行、合集都没变时跳过
            let fetches = rowFetches(visibleRows, libs)
            let snapshot = [
                String(data: (try? JSONEncoder().encode(libs)) ?? Data(), encoding: .utf8) ?? "",
                fetches.keys.sorted().joined(separator: ","),
                String(data: (try? JSONEncoder().encode(cols)) ?? Data(), encoding: .utf8) ?? "",
            ].joined(separator: "|")
            if snapshot == lastSnapshot { return }
            var next: [String: [API.LibraryItemView]] = [:]
            await withTaskGroup(of: (String, [API.LibraryItemView]).self) { group in
                for (key, fetch) in fetches {
                    group.addTask { (key, (try? await fetch()) ?? []) }
                }
                for await (key, items) in group { next[key] = items }
            }
            lastSnapshot = snapshot
            itemsByKey = next
        } catch is CancellationError {
        } catch {
            failed = true
        }
    }

    private static func fetchFavorites(_ api: APIClient, _ row: HomeRows.Row?) async -> API.FavoritesView? {
        guard let row, case let .favorites(sort, reversed) = row.kind else { return nil }
        return try? await api.playbackFavorites(
            limit: rowCount, offset: 0,
            // 「未看优先」是首页这一行的默认（全量页不传，保持收藏时间序）
            unwatchedFirst: sort == "unwatched_first",
            sort: sort == "unwatched_first" ? "favorited_at" : sort,
            order: HomeRows.favoritesPreset(sort).direction?.orderParam(reversed: reversed)
        )
    }

    /// 显示中的行各自要打的请求，按缓存键去重；排序与截断交给服务端
    private func rowFetches(_ rows: [HomeRows.Row], _ libs: [API.LibraryView]) -> [String: @Sendable () async throws -> [API.LibraryItemView]] {
        var fetches: [String: @Sendable () async throws -> [API.LibraryItemView]] = [:]
        let api = self.api
        let limit = Self.rowCount
        for row in rows {
            switch row.kind {
            case let .library(library, sort, reversed, unwatched, _, _):
                // 「最近观看」行只要播过的（w=seen）
                let watch: String? = sort == "last_played" ? "seen" : unwatched ? "unwatched" : nil
                let order = HomeRows.preset(sort).direction?.orderParam(reversed: reversed)
                let id = library.id
                fetches[Self.fetchKey(row)] = { try await api.libraryItemsList(libraryId: id, sort: sort, order: order, limit: limit, w: watch) }
            case let .collection(collection, sort, reversed, _):
                let order = HomeRows.preset(sort).direction?.orderParam(reversed: reversed)
                let id = collection.id
                fetches[Self.fetchKey(row)] = { try await api.collectionItemsList(collectionId: id, limit: limit, sort: sort, order: order) }
            case .libraries:
                for library in libs where library.viewerAccess {
                    let key = Self.coverKey(library.id)
                    let id = library.id
                    if fetches[key] == nil {
                        fetches[key] = { try await api.libraryItemsList(libraryId: id, sort: "added_at", limit: limit) }
                    }
                }
            default: break
            }
        }
        return fetches
    }
}

/// 横滚海报行里的一格（库行 / 合集行 / 收藏行统一形态）
private struct PosterRowItem: Identifiable {
    var id: Int
    var libraryId: Int
    var title: String
    var year: Int?
    var posterUrl: String?
    var aspect: Double
    /// 长按信息（最近添加的季集范围、入库时间、收藏层级）
    var info: [String]

    init(id: Int, libraryId: Int, title: String, year: Int?, posterUrl: String?, aspect: Double, info: [String]) {
        self.id = id
        self.libraryId = libraryId
        self.title = title
        self.year = year
        self.posterUrl = posterUrl
        self.aspect = aspect
        self.info = info
    }

    init(_ item: API.LibraryItemView, fallbackLibrary: Int) {
        var info: [String] = []
        if item.kind == "tv", let addition = item.recentAddition, let label = formatRecentAddition(addition) { info.append(label) }
        if let added = item.addedAt { info.append("\(libraryFromNow(added))入库") }
        self.init(id: item.mediaItemId, libraryId: item.libraryId ?? fallbackLibrary, title: item.title, year: item.year,
                  posterUrl: item.posterUrl, aspect: item.primaryAspect, info: info)
    }
}

// MARK: - 库卡片

/// 库卡片（Web `LibraryCard`）：服务端拼好的「氛围光货架」封面 + 库名 +「默认」；
/// 扫描 / 整理 / 元数据刷新进行中时封面归进度环并写出阶段，其余时间有待入账文件就挂「N 个新文件入库中」。
private struct LibraryHomeCard: View {
    let library: API.LibraryView
    /// 库里有没有海报素材（没有且没自定义封面时直接画类型占位，不请求拼贴）
    let hasPosters: Bool
    @Environment(\.api) private var api

    var body: some View {
        let refreshing = library.metadataRefresh?.refreshing == true
        let busy = library.scanning || library.organizing || refreshing
        let importing = busy ? 0 : (library.lastScan?.deferred ?? 0)
        VStack(spacing: 10) {
            ZStack {
                LinearGradient(colors: [Color(red: 0.11, green: 0.13, blue: 0.19), Color(red: 0.06, green: 0.07, blue: 0.11)], startPoint: .topLeading, endPoint: .bottomTrailing)
                Image(systemName: LibraryKindMeta.symbol(library.kind))
                    .font(.system(size: 40))
                    .foregroundStyle(.white.opacity(0.13))
                if hasPosters || library.customCover {
                    RemoteImage(url: api.image("/libraries/\(library.id)/cover"), placeholderSymbol: LibraryKindMeta.symbol(library.kind))
                }
                if busy {
                    ZStack {
                        Color.black.opacity(0.55)
                        VStack(spacing: 4) {
                            let progress = library.scanning ? library.scanProgress : library.organizing ? library.organizeProgress : nil
                            LibraryProgressRing(
                                processed: progress?.processed ?? (refreshing ? library.metadataRefresh?.processed : nil),
                                total: progress?.total ?? (refreshing ? library.metadataRefresh?.total : nil),
                                size: 62
                            )
                            Text(library.scanning ? ScanPhase.label(library.scanProgress?.phase) : library.organizing ? "整理中" : "刷新元数据")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(.white.opacity(0.85))
                            if refreshing, let active = library.metadataRefresh?.active.first {
                                Text("\(active.title) · \(active.phase)")
                                    .font(.caption2)
                                    .foregroundStyle(.white.opacity(0.6))
                                    .lineLimit(1)
                                    .padding(.horizontal, 12)
                            }
                        }
                    }
                } else if importing > 0 {
                    VStack {
                        Spacer()
                        HStack(spacing: 6) {
                            Circle().fill(Theme.info).frame(width: 6, height: 6)
                            Text("\(importing) 个新文件入库中")
                        }
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(Theme.info)
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(.black.opacity(0.55), in: .capsule)
                        .overlay(Capsule().strokeBorder(Theme.info.opacity(0.35)))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(8)
                    }
                }
            }
            .aspectRatio(21 / 10, contentMode: .fit)
            .clipShape(.rect(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(.white.opacity(0.1)))
            HStack(spacing: 8) {
                Text(library.name)
                    .font(.headline)
                    .foregroundStyle(.white)
                    .lineLimit(1)
                if library.isDefault {
                    Text("默认")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.white.opacity(0.8))
                        .padding(.horizontal, 8).padding(.vertical, 2)
                        .background(.white.opacity(0.1), in: .capsule)
                        .overlay(Capsule().strokeBorder(.white.opacity(0.14)))
                }
            }
            .padding(.horizontal, 8)
        }
        .frame(width: 230)
    }
}

// MARK: - 接下来继续

/// 「接下来继续」横卡（Web `UpNextCard`）：分集剧照或背景图、续播进度、「还有 N 集」，
/// 中央常驻空心播放键直接进播放器（触摸屏没有悬停，藏起来等于不存在）；点卡片进条目详情。
private struct UpNextCard: View {
    let item: API.UpNextItemView
    @Environment(\.api) private var api
    @Environment(Router.self) private var router

    private var isEpisode: Bool { item.kind == "tv" }
    private var code: String? { isEpisode ? episodeCode(season: item.seasonNumber, episode: item.episodeNumber) : nil }
    private var context: String {
        if isEpisode { return [code, item.episodeTitle].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ") }
        return item.year.map(String.init) ?? ""
    }

    private var route: AppRoute {
        .libraryItem(libraryId: item.libraryId, itemId: item.mediaItemId,
                     season: isEpisode ? item.seasonNumber : nil, episode: isEpisode ? item.episodeNumber : nil)
    }

    /// 「已播 / 总时长」：只在看了一半时出现
    private var clockText: String? {
        guard item.positionMs > 0, let duration = item.durationMs, duration > 0 else { return nil }
        return "\(Formatters.clock(Double(item.positionMs) / 1000)) / \(Formatters.clock(Double(duration) / 1000))"
    }

    /// 第三行：为什么它在这儿
    private var stateLabel: String {
        let ago = libraryFromNow(item.lastPlayedAt)
        if item.advanced { return "\(ago)看完上一集" }
        if item.positionMs > 0, let percent = item.progressPercent, clockText == nil { return "\(ago)看到 \(percent)%" }
        if item.positionMs > 0 { return "\(ago)看过一段" }
        return "\(ago)打开过"
    }

    private var playVerb: String {
        if item.positionMs > 0 { return "继续播放" }
        return item.advanced ? "播放下一集" : "播放"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ZStack {
                NavigationLink(value: route) { artwork }
                    .buttonStyle(.plain)
                Button {
                    router.play(PlayRequest(
                        mediaItemId: item.mediaItemId,
                        season: isEpisode ? item.seasonNumber : nil,
                        episode: isEpisode ? item.episodeNumber : nil
                    ))
                } label: {
                    Image(systemName: "play.fill")
                        .font(.system(size: 17))
                        .foregroundStyle(.white)
                        .shadow(color: .black.opacity(0.55), radius: 2, y: 1)
                        .frame(width: 42, height: 42)
                        .overlay(Circle().strokeBorder(.white.opacity(0.75), lineWidth: 1.5))
                        .shadow(color: .black.opacity(0.45), radius: 5)
                        .contentShape(.circle)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(playVerb)《\(item.title)》\(context.isEmpty ? "" : " \(context)")")
                .accessibilityIdentifier("up-next-play-\(item.mediaItemId)")
            }
            NavigationLink(value: route) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text).lineLimit(1)
                    if !context.isEmpty {
                        Text(context).font(.footnote).monospacedDigit().foregroundStyle(Theme.textMuted).lineLimit(1)
                    }
                    Text(stateLabel).font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
                }
                .padding(.top, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .buttonStyle(.plain)
        }
        .frame(width: 200)
    }

    private var artwork: some View {
        let url = isEpisode ? item.episodeStillUrl : item.backdropUrl
        return ZStack(alignment: .bottom) {
            if let url {
                LibraryArtwork(url: api.image(url, .landscapeCard), frameAspect: 16 / 9, fallbackText: code)
            } else if isEpisode {
                LibraryArtwork(url: nil, frameAspect: 16 / 9, fallbackText: code)
            } else {
                // 缺横向剧照：用海报按真实比例模糊铺底兜底
                LibraryArtwork(url: api.image(item.posterUrl, ImageVariant.card(aspect: item.posterAspect)), imageAspect: item.posterAspect,
                               frameAspect: 16 / 9, fallbackText: item.posterUrl == nil ? item.title : nil)
            }
            LinearGradient(colors: [.black.opacity(0.75), .clear], startPoint: .bottom, endPoint: .top).frame(height: 56)
            VStack(alignment: .leading, spacing: 4) {
                if let clockText {
                    Text(clockText).font(.caption2.weight(.semibold)).monospacedDigit().foregroundStyle(.white.opacity(0.85))
                }
                if let percent = item.progressPercent {
                    GeometryReader { proxy in
                        ZStack(alignment: .leading) {
                            Capsule().fill(.white.opacity(0.25))
                            Capsule().fill(Theme.accent2).frame(width: proxy.size.width * CGFloat(percent) / 100)
                        }
                    }
                    .frame(height: 3)
                } else if item.positionMs > 0 {
                    Capsule().fill(Theme.accent2.opacity(0.6)).frame(height: 3)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(8)
        }
        .overlay(alignment: .topTrailing) {
            if isEpisode, item.unwatchedAheadCount > 0 {
                Text("还有 \(item.unwatchedAheadCount) 集")
                    .font(.caption2.weight(.semibold))
                    .monospacedDigit()
                    .foregroundStyle(Color(red: 0.82, green: 0.98, blue: 0.9))
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(Color(red: 5 / 255, green: 46 / 255, blue: 34 / 255).opacity(0.76), in: .capsule)
                    .overlay(Capsule().strokeBorder(Color(red: 0.65, green: 0.95, blue: 0.82).opacity(0.25)))
                    .padding(8)
            }
        }
        .clipShape(.rect(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(.white.opacity(0.08)))
        .shadow(color: .black.opacity(0.38), radius: 14, y: 10)
    }
}

// MARK: - 清空观看记录

/// 「接下来继续」标题右侧的 ⋯（Web `WatchHistoryMenu`）：今天 / 最近一周 / 全部 / 某个媒体库
private struct WatchHistoryMenu: View {
    var onCleared: () -> Void
    var pickLibrary: () -> Void
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback

    var body: some View {
        Menu {
            Button("清空今天的观看记录…") {
                clear("今天的观看记录", since: Calendar.current.startOfDay(for: .now),
                      "今天播放过的作品，续播进度、已看标记和播放次数都会清除，无法恢复。只影响你自己的记录。")
            }
            Button("清空最近一周的观看记录…") {
                clear("最近一周的观看记录", since: .now.addingTimeInterval(-7 * 86400),
                      "最近 7 天播放过的作品，续播进度、已看标记和播放次数都会清除，无法恢复。只影响你自己的记录。")
            }
            Button("清空全部观看记录…") {
                clear("全部观看记录", since: nil,
                      "所有作品的续播进度、已看标记和播放次数都会清除，无法恢复。只影响你自己的记录；应用更新前的自动备份仍包含历史记录。")
            }
            Divider()
            Button("清空某个媒体库的观看记录…", action: pickLibrary)
        } label: {
            Image(systemName: "ellipsis")
                .font(.body.weight(.semibold))
                .foregroundStyle(Theme.textMuted)
                .frame(width: 30, height: 30)
                .contentShape(.rect)
        }
        .accessibilityLabel("清空观看记录")
        .accessibilityIdentifier("watch-history-menu")
    }

    private func clear(_ label: String, since: Date?, _ description: String) {
        Task {
            guard await feedback.confirm("清空\(label)？", message: description, confirmTitle: "清空", destructive: true) else { return }
            do {
                let (result, message) = try await api.libraryClearHistory(scope: "all", since: since)
                if since != nil {
                    feedback.success(result.deletedStates > 0 ? "已清空\(label)" : "\(label)里没有可清除的记录")
                } else {
                    feedback.success(message)
                }
                onCleared()
            } catch {
                feedback.error(error)
            }
        }
    }
}

/// 「清空某个媒体库的观看记录」选择框（Web `ClearLibraryHistoryDialog`）
private struct ClearLibraryHistorySheet: View {
    let libraries: [API.LibraryView]
    var onCleared: () -> Void
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(\.dismiss) private var dismiss
    @State private var libraryId: Int?
    @State private var busy = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Picker("媒体库", selection: $libraryId) {
                        if libraries.isEmpty { Text("没有可浏览的媒体库").tag(Int?.none) }
                        ForEach(libraries, id: \.id) { Text($0.name).tag(Int?.some($0.id)) }
                    }
                } footer: {
                    Text("选中库里所有作品的续播进度、已看标记和播放次数都会清除，无法恢复。只影响你自己的记录。")
                }
                Section {
                    Button(role: .destructive) {
                        submit()
                    } label: {
                        let selected = libraries.first { $0.id == libraryId }
                        Text(busy ? "清空中…" : selected.map { "清空「\($0.name)」" } ?? "清空")
                            .frame(maxWidth: .infinity)
                    }
                    .disabled(libraryId == nil || busy)
                }
            }
            .navigationTitle("清空某个媒体库的观看记录")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
            }
        }
        .presentationDetents([.medium])
        .onAppear { libraryId = libraryId ?? libraries.first?.id }
    }

    private func submit() {
        guard let libraryId, !busy else { return }
        busy = true
        Task {
            defer { busy = false }
            do {
                let (_, message) = try await api.libraryClearHistory(scope: "library", libraryId: libraryId)
                feedback.success(message)
                onCleared()
                dismiss()
            } catch {
                feedback.error(error)
            }
        }
    }
}
