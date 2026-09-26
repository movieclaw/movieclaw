import SwiftUI

/// 全部合集（Web `all-collections-view.tsx`，路由 `/library/collections`）。
///
/// 按库分组列出当前身份可见的合集（跨库名单单独一组在最前）；顶部两组带计数的筛选：
/// 类型（按所属库的 kind）与来源（自建 / 自动=系列+内置收藏）。计数按 faceted 口径算
/// （带上另一个维度的筛选），数为 0 的档压暗且点不动——「永不空货架」。
struct AllCollectionsView: View {
    @Environment(\.api) private var api
    @State private var rows: [API.CollectionView]?
    @State private var libraries: [API.LibraryView] = []
    @State private var kindFilter = "all"
    @State private var sourceFilter = "all"

    private static let kindOptions = [("all", "全部"), ("tv", "剧集"), ("movie", "电影")]
    private static let sourceOptions = [("all", "全部"), ("user", "自建"), ("auto", "自动")]

    var body: some View {
        let all = rows ?? []
        let kindOf = Dictionary(libraries.map { ($0.id, $0.kind) }, uniquingKeysWith: { a, _ in a })
        let matchesKind: (API.CollectionView, String) -> Bool = { row, kind in
            kind == "all" || (row.libraryId.flatMap { kindOf[$0] } == kind)
        }
        let matchesSource: (API.CollectionView, String) -> Bool = { row, source in
            source == "all" || (source == "user" ? row.kind == "user" : row.kind != "user")
        }
        let shown = all.filter { matchesKind($0, kindFilter) && matchesSource($0, sourceFilter) }
        let rank: (API.CollectionView) -> Int = { $0.kind == "user" ? 0 : $0.kind == "builtin" ? 1 : 2 }
        let bySource: ([API.CollectionView]) -> [API.CollectionView] = { $0.enumerated().sorted { (rank($0.element), $0.offset) < (rank($1.element), $1.offset) }.map(\.element) }
        let cross = bySource(shown.filter { $0.libraryId == nil })
        let grouped = libraries.map { lib in (lib, bySource(shown.filter { $0.libraryId == lib.id })) }.filter { !$0.1.isEmpty }
        let filtered = kindFilter != "all" || sourceFilter != "all"

        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                Text(rows == nil ? "正在读取合集…"
                    : all.isEmpty ? "还没有合集"
                    : filtered ? "\(shown.count) / \(all.count) 个合集 · 按库分组" : "\(all.count) 个合集 · 按库分组")
                    .font(.subheadline)
                    .foregroundStyle(Theme.textMuted)
                    .padding(.horizontal, Theme.pagePadding)
                    .accessibilityIdentifier("collections-summary")

                if rows == nil {
                    ProgressView().frame(maxWidth: .infinity).padding(.top, 64)
                } else if all.isEmpty {
                    Text("还没有合集。在某个库里筛出一批片，点「存为合集」就能把这组条件留下来。")
                        .font(.subheadline)
                        .foregroundStyle(Theme.textMuted)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: .infinity)
                        .padding(.horizontal, 24)
                        .padding(.top, 64)
                } else {
                    VStack(alignment: .leading, spacing: 10) {
                        chipGroup("类型", Self.kindOptions, selection: $kindFilter) { value in
                            all.filter { matchesSource($0, sourceFilter) && matchesKind($0, value) }.count
                        }
                        chipGroup("来源", Self.sourceOptions, selection: $sourceFilter) { value in
                            all.filter { matchesKind($0, kindFilter) && matchesSource($0, value) }.count
                        }
                    }
                    .padding(.horizontal, Theme.pagePadding)
                    .padding(.top, 14)

                    VStack(alignment: .leading, spacing: 28) {
                        if !cross.isEmpty {
                            VStack(alignment: .leading, spacing: 4) {
                                Text("跨库").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text)
                                Text("不属于任何一个库的手动名单").font(.footnote).foregroundStyle(Theme.textFaint)
                            }
                            .padding(.horizontal, Theme.pagePadding)
                            .padding(.bottom, -14)
                            LibraryCollectionsGrid(collections: cross, libraryId: nil)
                        }
                        ForEach(grouped, id: \.0.id) { library, items in
                            Text(library.name).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text)
                                .padding(.horizontal, Theme.pagePadding)
                                .padding(.bottom, -16)
                            LibraryCollectionsGrid(collections: items, libraryId: library.id)
                        }
                    }
                    .padding(.top, 20)
                }
            }
            .padding(.bottom, 32)
        }
        .appBackground()
        .navigationTitle("全部合集")
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        async let cols = try? api.collectionList()
        async let libs = try? api.libraryList(scope: "all")
        let (c, l) = await (cols, libs)
        rows = c ?? rows ?? []
        if let l { libraries = l }
    }

    private func chipGroup(_ label: String, _ options: [(String, String)], selection: Binding<String>, count: @escaping (String) -> Int) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                Text(label).font(.caption).foregroundStyle(Theme.textFaint).padding(.trailing, 2)
                ForEach(options, id: \.0) { value, title in
                    let n = count(value)
                    let on = selection.wrappedValue == value
                    Button {
                        selection.wrappedValue = value
                    } label: {
                        HStack(spacing: 6) {
                            Text(title)
                            Text("\(n)").monospacedDigit().foregroundStyle(.white.opacity(0.4))
                        }
                        .font(.caption)
                        .foregroundStyle(on ? .white : .white.opacity(0.7))
                        .padding(.horizontal, 10)
                        .frame(height: 28)
                        .background(.white.opacity(on ? 0.16 : 0.05), in: .capsule)
                    }
                    .buttonStyle(.plain)
                    .disabled(n == 0 && !on)
                    .opacity(n == 0 && !on ? 0.3 : 1)
                    .accessibilityIdentifier("chip-\(label)-\(value)")
                }
            }
        }
    }
}

/// 合集卡片网格（Web `LibraryCollectionsView`）：我的合集与系列分两段；卡片是最多 3 张叠放的海报
struct LibraryCollectionsGrid: View {
    let collections: [API.CollectionView]
    let libraryId: Int?
    var emptyHint = "还没有合集。筛出一批片之后，点「存为合集」就能把这组条件留下来。"

    var body: some View {
        if collections.isEmpty {
            Text(emptyHint)
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)
                .multilineTextAlignment(.center)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 60)
                .padding(.horizontal, Theme.pagePadding)
        } else {
            let mine = collections.filter { $0.kind != "series" }
            let series = collections.filter { $0.kind == "series" }
            let grouped = !mine.isEmpty && !series.isEmpty
            VStack(alignment: .leading, spacing: 28) {
                grid(grouped ? "我的合集" : nil, mine)
                grid(grouped ? "系列" : nil, series)
            }
        }
    }

    @ViewBuilder
    private func grid(_ title: String?, _ items: [API.CollectionView]) -> some View {
        if !items.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                if let title {
                    Text(title).font(.subheadline.weight(.medium)).foregroundStyle(Theme.textFaint)
                        .padding(.horizontal, Theme.pagePadding)
                }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 140), spacing: 12, alignment: .top)], alignment: .leading, spacing: 20) {
                    ForEach(items, id: \.id) { collection in
                        NavigationLink(value: AppRoute.collection(libraryId: libraryId ?? collection.libraryId, collectionId: collection.id)) {
                            CollectionCell(collection: collection)
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("collection-card-\(collection.id)")
                    }
                }
                .padding(.horizontal, Theme.pagePadding)
            }
        }
    }
}

private struct CollectionCell: View {
    let collection: API.CollectionView

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            LibraryCollectionCover(covers: collection.covers)
            Text(collection.name)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(Theme.text)
                .lineLimit(1)
                .padding(.top, 8)
            let tags = [
                "\(collection.itemCount) 部",
                collection.kind == "series" ? "系列" : collection.ruleDriven ? "自动收录" : nil,
                collection.visibility == "private" ? "只有我" : nil,
                collection.hidden ? "已隐藏" : nil,
            ].compactMap { $0 }
            Text(tags.joined(separator: " · "))
                .font(.footnote)
                .foregroundStyle(Theme.textFaint)
                .lineLimit(1)
        }
        .opacity(collection.hidden ? 0.45 : 1)
        .contentShape(.rect)
    }
}

/// 合集封面：最多 3 张海报依次向右错开叠放，后面的压暗
struct LibraryCollectionCover: View {
    let covers: [API.CollectionCover]
    @Environment(\.api) private var api

    var body: some View {
        let shown = Array(covers.prefix(3))
        Color.white.opacity(0.04)
            .aspectRatio(Theme.posterAspect, contentMode: .fit)
            .overlay {
                if shown.isEmpty {
                    Text("暂无封面").font(.footnote).foregroundStyle(Theme.textFaint)
                } else {
                    GeometryReader { proxy in
                        let w = proxy.size.width, h = proxy.size.height
                        let step = w * 0.07, inset = h * 0.025
                        let frontWidth = w - step * CGFloat(shown.count - 1)
                        ZStack(alignment: .topLeading) {
                            ForEach(Array(shown.enumerated().reversed()), id: \.offset) { index, cover in
                                RemoteImage(url: api.image(cover.url, .posterCard))
                                    .frame(width: frontWidth, height: h - inset * CGFloat(index) * 2)
                                    .clipShape(.rect(cornerRadius: 12))
                                    .brightness(index == 0 ? 0 : -0.3)
                                    .offset(x: step * CGFloat(index), y: inset * CGFloat(index))
                            }
                        }
                    }
                }
            }
            .clipShape(.rect(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(.white.opacity(0.06)))
    }
}
