import SwiftUI

/// 组合发现的六个筛选维度（对应 Web `discovery-filter-dialog.tsx`）：类型多选 + 国家/地区 + 年份 +
/// 最低评分 + 最长片长 + 排序。
///
/// 原生端**不开弹窗**：条件只是「从几个值里挑一个」（类型可多挑），正好是系统下拉菜单装得下的量，
/// 同「照片」「文件」的显示选项——iOS 26 的菜单本身就是液态玻璃、从按钮原地长出，选中立刻生效，
/// 没有「查看结果」。整张 sheet 只该留给要填一张表单的任务（早先的全屏 sheet 被用户否掉）。
/// 两处入口共用同一份取值菜单（`DiscoverFilterOptions`）：
/// - 右上角筛选键 `DiscoverFilterMenu`：按钮上只写当前类型，点开是六个维度各一个二级菜单；
/// - 结果页头部 `DiscoverFilterChips`：每个维度一颗玻璃胶囊，点开直接改或移除，不用回右上角。
enum DiscoverFilterDimension: CaseIterable, Identifiable {
    case genres, country, year, rating, runtime, sort

    var id: Self { self }

    func title(mediaType: String) -> String {
        switch self {
        case .genres: "类型"
        case .country: "国家 / 地区"
        case .year: mediaType == "movie" ? "上映年份" : "首播年份"
        case .rating: "最低评分"
        case .runtime: mediaType == "movie" ? "最长片长" : "最长单集时长"
        case .sort: "排序"
        }
    }

    var systemImage: String {
        switch self {
        case .genres: "theatermasks"
        case .country: "globe.asia.australia"
        case .year: "calendar"
        case .rating: "star"
        case .runtime: "clock"
        case .sort: "arrow.up.arrow.down"
        }
    }

    /// 当前取值的可读文字；未启用返回 nil（排序停在默认的「热门优先」也算未启用，与角标计数同口径）
    func summary(_ filters: DiscoveryFilters, genreNames: [Int: String]) -> String? {
        switch self {
        case .genres:
            guard !filters.genreIds.isEmpty else { return nil }
            let names = filters.genreIds.compactMap { genreNames[$0] }
            // 类型名还没到（深链进来时清单在路上）或选太多时给计数，胶囊不至于被撑成一长条
            guard names.count == filters.genreIds.count, names.count <= 2 else {
                return names.first.map { "\($0)等 \(filters.genreIds.count) 个" } ?? "\(filters.genreIds.count) 个类型"
            }
            return names.joined(separator: "、")
        case .country: return filters.originCountry.map(DiscoveryFilters.countryLabel)
        case .year: return filters.year.map { "\(String($0)) 年" }
        case .rating: return filters.ratingGte.map { "\(DiscoveryFilters.format($0)) 分以上" }
        case .runtime: return filters.runtimeLte.map { "\($0) 分钟以内" }
        case .sort: return filters.sort == "popular" ? nil : DiscoveryFilters.sortLabel(filters.sort)
        }
    }

    func clear(_ filters: inout DiscoveryFilters) {
        switch self {
        case .genres: filters.genreIds = []
        case .country: filters.originCountry = nil
        case .year: filters.year = nil
        case .rating: filters.ratingGte = nil
        case .runtime: filters.runtimeLte = nil
        case .sort: filters.sort = "popular"
        }
    }
}

/// 类型清单（电影与剧集是两套 TMDB 类型 ID）：整个 App 生命周期内不变，按媒体类型缓存，
/// 筛选键与结果页胶囊共用一份，不重复请求
@MainActor
enum DiscoverGenreCatalog {
    private static var cache: [String: [API.DiscoveryGenreView]] = [:]

    static func cached(_ mediaType: String) -> [API.DiscoveryGenreView] { cache[mediaType] ?? [] }

    /// 拉取失败返回 nil（其他维度照常可用，类型菜单里给一行失败提示）
    static func load(api: APIClient, mediaType: String) async -> [API.DiscoveryGenreView]? {
        if let hit = cache[mediaType] { return hit }
        guard let options = try? await api.discoverFilterOptions(mediaType: mediaType) else { return nil }
        cache[mediaType] = options.genres
        return options.genres
    }
}

/// 一个维度的取值清单，放进菜单里用：单选维度是内联 Picker（系统画勾），类型是可连点的多选开关
struct DiscoverFilterOptions: View {
    let dimension: DiscoverFilterDimension
    let mediaType: String
    /// nil = 类型清单拉取失败
    let genres: [API.DiscoveryGenreView]?
    @Binding var filters: DiscoveryFilters

    private var currentYear: Int { Calendar.current.component(.year, from: .now) }

    var body: some View {
        let title = dimension.title(mediaType: mediaType)
        switch dimension {
        case .genres:
            if let genres, !genres.isEmpty {
                ForEach(genres, id: \.id) { genre in
                    Toggle(genre.name, isOn: Binding(
                        get: { filters.genreIds.contains(genre.id) },
                        set: { on in
                            filters.genreIds.removeAll { $0 == genre.id }
                            if on { filters.genreIds.append(genre.id) }
                        }
                    ))
                }
            } else {
                Button(genres == nil ? "类型加载失败，其他条件仍可使用" : "类型加载中…") {}
                    .disabled(true)
            }
        case .country:
            Picker(title, selection: $filters.originCountry) {
                Text("不限").tag(String?.none)
                ForEach(DiscoveryFilters.countries, id: \.code) { Text($0.name).tag(String?.some($0.code)) }
            }
            .pickerStyle(.inline)
        case .year:
            Picker(title, selection: $filters.year) {
                Text("不限").tag(Int?.none)
                ForEach(Array((1874 ... currentYear).reversed()), id: \.self) { Text("\(String($0)) 年").tag(Int?.some($0)) }
            }
            .pickerStyle(.inline)
        case .rating:
            Picker(title, selection: $filters.ratingGte) {
                Text("不限").tag(Double?.none)
                ForEach([6.0, 7, 8, 9], id: \.self) { Text("\(Int($0)) 分以上").tag(Double?.some($0)) }
            }
            .pickerStyle(.inline)
        case .runtime:
            Picker(title, selection: $filters.runtimeLte) {
                Text("不限").tag(Int?.none)
                ForEach([60, 90, 120, 150], id: \.self) { Text("\($0) 分钟以内").tag(Int?.some($0)) }
            }
            .pickerStyle(.inline)
        case .sort:
            Picker(title, selection: $filters.sort) {
                ForEach(DiscoveryFilters.sorts, id: \.value) { Text($0.label).tag($0.value) }
            }
            .pickerStyle(.inline)
        }
    }
}

/// 右上角筛选键，照 App Store「App」页右上角的类别按钮：按钮只写当前类型，不带箭头、不带计数——
/// 没选是「全部」，选一个写类型名，选多个写「动作等 2 个」。其他维度的条件不在这里表达：
/// 一旦有条件就进了结果页，头部的条件胶囊已把每一项写得清清楚楚（用户拍板从简）。
/// 点开是六个维度各一个二级菜单（标题下是当前值），有条件时末尾给「清空条件」。
/// 选一项菜单即收起，类型虽是多选也一样（与结果页胶囊一致；要再加一个类型就再点开一次）。
struct DiscoverFilterMenu: View {
    let mediaType: String
    @Binding var filters: DiscoveryFilters

    @Environment(\.api) private var api
    @State private var genres: [API.DiscoveryGenreView]? = []

    var body: some View {
        let genreNames = Dictionary(genres.map { $0.map { ($0.id, $0.name) } } ?? [], uniquingKeysWith: { a, _ in a })
        let title = buttonTitle(genreNames: genreNames)
        Menu {
            ForEach(DiscoverFilterDimension.allCases) { dimension in
                Menu {
                    DiscoverFilterOptions(dimension: dimension, mediaType: mediaType, genres: genres, filters: $filters)
                } label: {
                    Label(dimension.title(mediaType: mediaType), systemImage: dimension.systemImage)
                    Text(dimension.summary(filters, genreNames: genreNames) ?? (dimension == .sort ? "热门优先" : "不限"))
                }
            }
            if filters.activeCount > 0 {
                Section {
                    Button("清空条件", systemImage: "xmark.circle", role: .destructive) { filters = .empty }
                }
            }
        } label: {
            Text(title)
                .lineLimit(1)
        }
        .accessibilityLabel("筛选：\(title)")
        .accessibilityIdentifier("discover-filter")
        .task(id: mediaType) {
            genres = DiscoverGenreCatalog.cached(mediaType)
            genres = await DiscoverGenreCatalog.load(api: api, mediaType: mediaType)
        }
    }

    /// 顶栏寸土寸金：多选只写第一个再带个数，不像结果页胶囊那样并排两个名字
    private func buttonTitle(genreNames: [Int: String]) -> String {
        guard let first = filters.genreIds.first else { return "全部" }
        let name = genreNames[first]
        if filters.genreIds.count == 1 { return name ?? "1 个类型" }
        return name.map { "\($0)等 \(filters.genreIds.count) 个" } ?? "\(filters.genreIds.count) 个类型"
    }
}

/// 结果页头部的条件胶囊（地图搜索结果那排胶囊的形态）：六个维度固定顺序各一颗，已启用的
/// 高亮并写出当前值，未启用的只写维度名。点开是同一份取值菜单，已启用的末尾多一项「移除」；
/// 选一项菜单即收起。
/// 顺序固定不按启用与否重排——改完一项胶囊原地变亮，手指下的东西不会跑位。
struct DiscoverFilterChips: View {
    let mediaType: String
    let genres: [API.DiscoveryGenreView]?
    @Binding var filters: DiscoveryFilters

    var body: some View {
        let genreNames = Dictionary(genres.map { $0.map { ($0.id, $0.name) } } ?? [], uniquingKeysWith: { a, _ in a })
        ScrollView(.horizontal) {
            GlassEffectContainer(spacing: 8) {
                HStack(spacing: 8) {
                    ForEach(DiscoverFilterDimension.allCases) { dimension in
                        chip(dimension, summary: dimension.summary(filters, genreNames: genreNames))
                    }
                }
                .padding(.horizontal, Theme.pagePadding)
                .padding(.vertical, 4)
            }
        }
        .scrollIndicators(.hidden)
        // 横滑出页边距：胶囊能滑到屏幕边缘再消失，而不是在 16pt 页边距处被一刀切掉
        .padding(.horizontal, -Theme.pagePadding)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("当前筛选条件")
    }

    private func chip(_ dimension: DiscoverFilterDimension, summary: String?) -> some View {
        let active = summary != nil
        return Menu {
            DiscoverFilterOptions(dimension: dimension, mediaType: mediaType, genres: genres, filters: $filters)
            if active {
                Section {
                    Button(dimension == .sort ? "恢复热门优先" : "移除此条件", systemImage: "xmark", role: .destructive) {
                        dimension.clear(&filters)
                    }
                }
            }
        } label: {
            HStack(spacing: 4) {
                Text(summary ?? dimension.title(mediaType: mediaType))
                    .lineLimit(1)
                Image(systemName: "chevron.down")
                    .font(.caption2.weight(.semibold))
                    .opacity(0.6)
            }
            .font(.subheadline.weight(active ? .semibold : .regular))
            .foregroundStyle(active ? Theme.text : Theme.textMuted)
            .padding(.horizontal, 12)
            .frame(height: 34)
            // 与媒体库筛选键同一套玻璃：启用态叠一层白色 tint
            .glassEffect(active ? .regular.tint(.white.opacity(0.16)).interactive() : .regular.interactive(), in: .capsule)
            .contentShape(.capsule)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(active ? "\(dimension.title(mediaType: mediaType))：\(summary ?? "")" : dimension.title(mediaType: mediaType))
    }
}

/// 组合筛选结果网格（对应 Web `filtered-discovery-view.tsx`）：TMDB discover 原生分页，
/// 滚到底自动加载下一页（失败给「重试」），头部是可点的条件胶囊与「清空条件」。
///
/// 条件就在本页头部被改（胶囊菜单），所以网格**不随条件重建**（重建会让头部胶囊跟着重来、
/// 横滑位置归零），而是 `.task(id: filters)` 原地重查：
/// 先等 0.3 秒防抖（连勾几个类型只查最后一次），期间再改就取消重来。
struct DiscoverFilteredGrid: View {
    let mediaType: String
    @Binding var filters: DiscoveryFilters

    @Environment(\.api) private var api
    @State private var items: [DiscoverPosterItem] = []
    @State private var nextPage = 1
    @State private var totalResults = 0
    @State private var hasMore = true
    @State private var loading = false
    @State private var error: String?
    @State private var genres: [API.DiscoveryGenreView]? = []

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                if !items.isEmpty {
                    LazyVGrid(columns: DiscoverGrid.wideColumns, spacing: 28) {
                        ForEach(items) { item in
                            DiscoverPosterCard(item: item)
                                .onAppear {
                                    if item.id == items.last?.id { Task { await loadMore() } }
                                }
                        }
                    }
                }
                if loading, items.isEmpty {
                    LazyVGrid(columns: DiscoverGrid.wideColumns, spacing: 28) {
                        ForEach(0 ..< 9, id: \.self) { _ in
                            DiscoverSkeletonBlock(cornerRadius: Theme.posterRadius).aspectRatio(2 / 3, contentMode: .fit)
                        }
                    }
                }
                if !loading, error == nil, items.isEmpty {
                    Text("没有符合条件的影片")
                        .font(.body)
                        .foregroundStyle(Theme.textMuted)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 80)
                }
                if loading, !items.isEmpty {
                    ProgressView().frame(maxWidth: .infinity).padding()
                }
                if let error {
                    VStack(spacing: 12) {
                        Text(error).foregroundStyle(Theme.textMuted).multilineTextAlignment(.center)
                        Button("重试") { Task { await loadMore() } }
                            .discoverProminentButton()
                    }
                    .frame(maxWidth: .infinity)
                    .padding(24)
                    .cardStyle()
                }
                if !hasMore, !items.isEmpty {
                    Text("已加载全部 \(items.count) 部影片")
                        .font(.caption)
                        .foregroundStyle(Theme.textFaint)
                        .frame(maxWidth: .infinity)
                }
            }
            .padding(.horizontal, Theme.pagePadding)
            .padding(.bottom, 32)
        }
        .task(id: filters) {
            // 条件变了：清空重查。首次进入不必等防抖
            let first = items.isEmpty && nextPage == 1 && !loading
            items = []
            nextPage = 1
            totalResults = 0
            hasMore = true
            error = nil
            loading = true
            if !first {
                try? await Task.sleep(for: .milliseconds(300))
                guard !Task.isCancelled else { return }
            }
            loading = false
            await loadMore()
        }
        .task(id: mediaType) {
            genres = DiscoverGenreCatalog.cached(mediaType)
            genres = await DiscoverGenreCatalog.load(api: api, mediaType: mediaType)
        }
        .accessibilityIdentifier("discover-filtered")
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .bottom) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("TMDB DISCOVER")
                        .font(.subheadline.weight(.semibold))
                        .tracking(2)
                        .foregroundStyle(Theme.accent2)
                    Text("筛选结果")
                        .font(.title.bold())
                        .foregroundStyle(Theme.text)
                    Text(totalResults > 0
                        ? "找到 \(totalResults.formatted()) 部，已加载 \(items.count) 部"
                        : "已启用 \(filters.activeCount) 项筛选")
                        .font(.subheadline)
                        .foregroundStyle(Theme.textMuted)
                }
                Spacer(minLength: 12)
                Button("清空条件") { filters = .empty }
                    .buttonStyle(.glass)
                    .accessibilityIdentifier("filtered-clear")
            }
            DiscoverFilterChips(mediaType: mediaType, genres: genres, filters: $filters)
        }
        .padding(.top, 8)
    }

    private func loadMore() async {
        guard !loading, hasMore else { return }
        let requested = filters
        loading = true
        error = nil
        // 条件已变时不碰 loading：那时它属于新条件那次查询
        defer { if requested == filters { loading = false } }
        do {
            let page = try await api.discoverFilterTitles(
                mediaType: mediaType,
                genreIds: requested.genreIds.isEmpty ? nil : requested.genreIds,
                originCountry: requested.originCountry,
                year: requested.year,
                ratingGte: requested.ratingGte,
                runtimeLte: requested.runtimeLte,
                sort: requested.sort,
                page: nextPage
            )
            // 请求途中条件又变了：这页属于旧条件，丢掉（新条件的查询已在路上）
            guard requested == filters else { return }
            let known = Set(items.map(\.id))
            items += page.titles.map(DiscoverPosterItem.init).filter { !known.contains($0.id) }
            totalResults = page.totalResults
            hasMore = page.hasMore
            nextPage = page.page + 1
        } catch is CancellationError {
        } catch {
            guard requested == filters else { return }
            self.error = error.localizedDescription.isEmpty ? "筛选结果加载失败，请稍后重试" : error.localizedDescription
        }
    }
}
