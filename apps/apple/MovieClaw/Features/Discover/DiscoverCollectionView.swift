import SwiftUI

/// 榜单全列表（对应 Web `collection-grid-view.tsx`）：`/discover/{type}/collections/{provider}/{id}`。
///
/// - TMDB 片单每页 20 条，滚到底自动续页（失败给「重试下一页」）；豆瓣片单一次取完（limit 500）；
/// - 顶部可按片名搜索、按类型筛选**已加载**的条目（纯本地过滤，不触发请求；过滤期间不自动续页）。
struct DiscoverCollectionView: View {
    let kind: String
    let provider: String
    let collectionId: String

    @Environment(\.api) private var api
    @State private var items: [DiscoverPosterItem]?
    @State private var title = "影视片单"
    @State private var nextPage = 2
    @State private var totalResults = 0
    @State private var hasMore = false
    @State private var loadingMore = false
    @State private var error: String?
    @State private var query = ""
    @State private var selectedGenres: [String] = []

    private var collectionRef: String { "\(provider):\(kind):\(collectionId)" }
    private var isTMDB: Bool { provider == "tmdb" }

    private var genres: [(name: String, count: Int)] {
        var counts: [String: Int] = [:]
        for item in items ?? [] { for name in item.genres { counts[name, default: 0] += 1 } }
        return counts.map { ($0.key, $0.value) }.sorted { $0.count > $1.count || ($0.count == $1.count && $0.name < $1.name) }
    }

    private var filtered: [DiscoverPosterItem] {
        let keyword = query.trimmingCharacters(in: .whitespaces).lowercased()
        return (items ?? []).filter { item in
            let genreOK = selectedGenres.isEmpty || selectedGenres.contains { item.genres.contains($0) }
            let keywordOK = keyword.isEmpty || item.title.lowercased().contains(keyword) || item.originalTitle.lowercased().contains(keyword)
            return genreOK && keywordOK
        }
    }

    private var filteringLoaded: Bool { !query.trimmingCharacters(in: .whitespaces).isEmpty || !selectedGenres.isEmpty }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                if let items {
                    if filtered.isEmpty {
                        Text("没有找到匹配的影片")
                            .foregroundStyle(Theme.textMuted)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 60)
                    } else {
                        LazyVGrid(columns: DiscoverGrid.wideColumns, spacing: 28) {
                            ForEach(filtered) { item in
                                DiscoverPosterCard(item: item)
                                    .onAppear {
                                        if item.id == filtered.last?.id, !filteringLoaded { Task { await loadNextPage() } }
                                    }
                            }
                        }
                    }
                    if loadingMore { ProgressView().frame(maxWidth: .infinity).padding() }
                    if let error {
                        HStack(spacing: 12) {
                            Text(error).font(.subheadline).foregroundStyle(Theme.textMuted)
                            if hasMore {
                                Button("重试下一页") { Task { await loadNextPage() } }
                                    .buttonStyle(.glass)
                            }
                        }
                        .frame(maxWidth: .infinity)
                        .padding(12)
                        .cardStyle(radius: 12)
                    }
                    if !hasMore, !filtered.isEmpty, !filteringLoaded {
                        Text("已加载全部 \(items.count) 部影片")
                            .font(.caption).foregroundStyle(Theme.textFaint)
                            .frame(maxWidth: .infinity)
                    }
                } else if let error {
                    ErrorState(message: error) { await loadFirstPage() }
                        .padding(.top, 40)
                } else {
                    LazyVGrid(columns: DiscoverGrid.wideColumns, spacing: 28) {
                        ForEach(0 ..< 12, id: \.self) { _ in
                            DiscoverSkeletonBlock(cornerRadius: Theme.posterRadius).aspectRatio(2 / 3, contentMode: .fit)
                        }
                    }
                }
            }
            .padding(.horizontal, Theme.pagePadding)
            .padding(.bottom, 32)
        }
        .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .automatic),
                    prompt: isTMDB ? "搜索已加载片名" : "搜索片名")
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
        .appBackground()
        .tracksSubscriptionIndex()
        .task { if items == nil { await loadFirstPage() } }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("\(provider.uppercased()) COLLECTION")
                .font(.subheadline.weight(.semibold))
                .tracking(2)
                .foregroundStyle(Theme.accent2)
            Text(title)
                .font(.title.bold())
                .foregroundStyle(Theme.text)
            Text(items.map { items in
                isTMDB ? "已加载 \(items.count) / \(totalResults.formatted()) 部影片" : "完整收录 \(items.count) 部影片"
            } ?? "正在读取完整榜单…")
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)
            if !genres.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        Text(isTMDB ? "已加载类型" : "类型")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(Theme.textMuted)
                        DiscoverChip(label: "全部", active: selectedGenres.isEmpty) { selectedGenres = [] }
                        ForEach(genres, id: \.name) { genre in
                            DiscoverChip(label: genre.name, count: genre.count, active: selectedGenres.contains(genre.name)) {
                                if let index = selectedGenres.firstIndex(of: genre.name) {
                                    selectedGenres.remove(at: index)
                                } else {
                                    selectedGenres.append(genre.name)
                                }
                            }
                        }
                        if !selectedGenres.isEmpty {
                            Button("清除筛选（\(selectedGenres.count)）") { selectedGenres = [] }
                                .font(.subheadline)
                        }
                    }
                }
                .padding(.top, 8)
            }
        }
        .padding(.top, 8)
    }

    private func loadFirstPage() async {
        error = nil
        do {
            let result = try await api.discoverBrowseCollection(collectionRef: collectionRef, limit: isTMDB ? 20 : 500, page: 1)
            items = result.titles.map(DiscoverPosterItem.init)
            title = result.collection.name
            totalResults = result.totalResults
            hasMore = isTMDB && result.hasMore
            nextPage = result.page + 1
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription.isEmpty ? "榜单加载失败，请稍后重试" : error.localizedDescription
        }
    }

    private func loadNextPage() async {
        guard isTMDB, hasMore, !loadingMore, items != nil else { return }
        loadingMore = true
        error = nil
        defer { loadingMore = false }
        do {
            let result = try await api.discoverBrowseCollection(collectionRef: collectionRef, limit: 20, page: nextPage)
            let known = Set((items ?? []).map(\.id))
            items = (items ?? []) + result.titles.map(DiscoverPosterItem.init).filter { !known.contains($0.id) }
            totalResults = result.totalResults
            hasMore = result.hasMore
            nextPage = result.page + 1
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription.isEmpty ? "下一页加载失败，请稍后重试" : error.localizedDescription
        }
    }
}
