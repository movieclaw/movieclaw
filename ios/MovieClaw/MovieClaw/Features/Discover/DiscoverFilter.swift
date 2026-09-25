import SwiftUI

/// 组合发现（对应 Web `discovery-filter-dialog.tsx`）：类型多选 + 国家/地区 + 年份 + 最低评分 + 最长片长 + 排序。
/// 草稿在弹层内编辑，点「查看结果」才生效；「清空」只清草稿。
struct DiscoverFilterSheet: View {
    let mediaType: String
    let initial: DiscoveryFilters
    let onApply: (DiscoveryFilters) -> Void

    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss
    @State private var draft = DiscoveryFilters.empty
    @State private var genres: [API.DiscoveryGenreView] = []
    @State private var genreError = false

    /// 类型清单按媒体类型缓存（整个 App 生命周期内不变）
    private static var genreCache: [String: [API.DiscoveryGenreView]] = [:]

    private var currentYear: Int { Calendar.current.component(.year, from: .now) }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    if genreError {
                        Text("类型加载失败，其他筛选仍可使用").foregroundStyle(Theme.danger)
                    } else if genres.isEmpty {
                        DiscoverSkeletonBlock(cornerRadius: 12).frame(height: 64)
                    } else {
                        DiscoverFlowLayout(spacing: 8, lineSpacing: 8) {
                            ForEach(genres, id: \.id) { genre in
                                DiscoverChip(label: genre.name, active: draft.genreIds.contains(genre.id)) {
                                    if let index = draft.genreIds.firstIndex(of: genre.id) {
                                        draft.genreIds.remove(at: index)
                                    } else {
                                        draft.genreIds.append(genre.id)
                                    }
                                }
                            }
                        }
                        .padding(.vertical, 4)
                    }
                } header: {
                    Text("类型（可多选）")
                }

                Section {
                    Picker("国家 / 地区", selection: $draft.originCountry) {
                        Text("不限").tag(String?.none)
                        ForEach(DiscoveryFilters.countries, id: \.code) { Text($0.name).tag(String?.some($0.code)) }
                    }
                    Picker(mediaType == "movie" ? "上映年份" : "首播年份", selection: $draft.year) {
                        Text("不限").tag(Int?.none)
                        ForEach(Array((1874 ... currentYear).reversed()), id: \.self) { Text("\(String($0)) 年").tag(Int?.some($0)) }
                    }
                    Picker("最低评分", selection: $draft.ratingGte) {
                        Text("不限").tag(Double?.none)
                        ForEach([6.0, 7, 8, 9], id: \.self) { Text("\(Int($0)) 分以上").tag(Double?.some($0)) }
                    }
                    Picker(mediaType == "movie" ? "最长片长" : "最长单集时长", selection: $draft.runtimeLte) {
                        Text("不限").tag(Int?.none)
                        ForEach([60, 90, 120, 150], id: \.self) { Text("\($0) 分钟以内").tag(Int?.some($0)) }
                    }
                    Picker("排序", selection: $draft.sort) {
                        ForEach(DiscoveryFilters.sorts, id: \.value) { Text($0.label).tag($0.value) }
                    }
                } footer: {
                    Text("筛选仅 TMDB 数据源支持")
                }
            }
            .scrollContentBackground(.hidden)
            .background(Theme.background)
            .navigationTitle("组合发现")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("清空") { draft = .empty }
                        .accessibilityIdentifier("filter-clear")
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("查看结果") {
                        onApply(draft)
                        dismiss()
                    }
                    .discoverProminentButton()
                    .accessibilityIdentifier("filter-apply")
                }
            }
        }
        .presentationDetents([.large])
        .task {
            draft = initial
            if let cached = Self.genreCache[mediaType] {
                genres = cached
                return
            }
            do {
                let options = try await api.discoverFilterOptions(mediaType: mediaType)
                Self.genreCache[mediaType] = options.genres
                genres = options.genres
            } catch {
                genreError = true
            }
        }
    }
}

/// 组合筛选结果网格（对应 Web `filtered-discovery-view.tsx`）：TMDB discover 原生分页，
/// 滚到底自动加载下一页（失败给「重试」），头部是当前筛选的可读标签与「清除全部」。
struct DiscoverFilteredGrid: View {
    let mediaType: String
    let filters: DiscoveryFilters
    let onClear: () -> Void

    @Environment(\.api) private var api
    @State private var items: [DiscoverPosterItem] = []
    @State private var nextPage = 1
    @State private var totalResults = 0
    @State private var hasMore = true
    @State private var loading = false
    @State private var error: String?
    @State private var genreNames: [Int: String] = [:]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                if !items.isEmpty {
                    LazyVGrid(columns: DiscoverGrid.columns, spacing: 20) {
                        ForEach(items) { item in
                            DiscoverPosterCard(item: item)
                                .onAppear {
                                    if item.id == items.last?.id { Task { await loadMore() } }
                                }
                        }
                    }
                }
                if loading, items.isEmpty {
                    LazyVGrid(columns: DiscoverGrid.columns, spacing: 20) {
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
        .task {
            await loadMore()
            guard !filters.genreIds.isEmpty,
                  let options = try? await api.discoverFilterOptions(mediaType: mediaType) else { return }
            genreNames = Dictionary(options.genres.map { ($0.id, $0.name) }, uniquingKeysWith: { a, _ in a })
        }
        .accessibilityIdentifier("discover-filtered")
    }

    private var header: some View {
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
                DiscoverFlowLayout(spacing: 6, lineSpacing: 6) {
                    ForEach(filters.labels(genreNames: genreNames), id: \.self) { label in
                        DiscoverTag(text: label, foreground: Theme.text, background: Color.white.opacity(0.1))
                    }
                }
                .padding(.top, 4)
                .accessibilityLabel("当前筛选条件")
            }
            Spacer(minLength: 12)
            Button("清除全部", action: onClear)
                .buttonStyle(.glass)
                .accessibilityIdentifier("filtered-clear")
        }
        .padding(.top, 8)
    }

    private func loadMore() async {
        guard !loading, hasMore else { return }
        loading = true
        error = nil
        defer { loading = false }
        do {
            let page = try await api.discoverFilterTitles(
                mediaType: mediaType,
                genreIds: filters.genreIds.isEmpty ? nil : filters.genreIds,
                originCountry: filters.originCountry,
                year: filters.year,
                ratingGte: filters.ratingGte,
                runtimeLte: filters.runtimeLte,
                sort: filters.sort,
                page: nextPage
            )
            let known = Set(items.map(\.id))
            items += page.titles.map(DiscoverPosterItem.init).filter { !known.contains($0.id) }
            totalResults = page.totalResults
            hasMore = page.hasMore
            nextPage = page.page + 1
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription.isEmpty ? "筛选结果加载失败，请稍后重试" : error.localizedDescription
        }
    }
}
