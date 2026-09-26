import SwiftUI

/// 单库墙的筛选条（对应网页 `components/library-filter-bar.tsx`，设计见
/// docs/design/library-filtering.md 5.1 / 5.2）。
///
/// **静止态只留信息，不留控件**——四个都写着「全部」的下拉不承载任何信息。所以同一块地方：
/// - 静止态：「筛选」按钮（唯一一个带玻璃底的按钮）+ 合集 chip，一律靠左，排不下换行；
/// - 点开：拉起原生底部面板（网页窄屏的 FilterSheet），一二级维度全部平铺成胶囊，
///   每个取值带「还剩几部」；每次勾选**立即生效**，不做"确定"式提交——筛选本来就是来回试探；
/// - 有条件：条件行出现在按钮下方，且**与面板开合无关**——收起面板不等于取消筛选。
///
/// 条件行把「或」和「且」画出来：同一维度多个值用「或」连在一个容器里，维度之间用「且」隔开。
/// 多选语义是这类筛选器最大的困惑源，画出来就没人会问。
///
/// 排序控件不在这里（它是墙的偏好，不属于筛选），由宿主页面自己摆。
struct LibraryFilterBar: View {
    let libraryId: Int
    @Binding var filter: LibraryFilter
    /// 本库的合集：以 chip 形式排在同一行（合集本来就是存好的筛选）
    var collections: [API.CollectionView] = []
    /// 「存为合集」——有条件时才出现；不给则不显示该入口
    var onSaveAsCollection: (() -> Void)? = nil
    /// 点名单驱动的合集 chip（网页是跳到合集详情的链接）；不给则直接推入合集详情页
    var onOpenCollection: ((API.CollectionView) -> Void)? = nil
    /// 合集 chip 超过 8 个时的「全部合集 ›」（网页跳到本库的合集视图）；不给则不显示该入口
    var onShowAllCollections: (() -> Void)? = nil

    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @State private var open = false
    @State private var facets: API.LibraryFacetsView?

    /// chip 行最多摆几个：再多就该去「合集」视图一次看全，而不是让一行占掉半屏
    private static let chipLimit = 8

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            FilterFlowLayout(spacing: 8) {
                filterButton
                ForEach(shownCollections, id: \.id) { collection in
                    collectionChip(collection)
                }
                if ownCollections.count > shownCollections.count, let onShowAllCollections {
                    Button(action: onShowAllCollections) {
                        Text("全部合集 ›")
                            .font(.caption)
                            .foregroundStyle(.white.opacity(0.5))
                            .frame(height: 28)
                            .expandedHitArea(vertical: 8)
                    }
                    .buttonStyle(.plain)
                }
            }
            if !filter.isEmpty {
                FilterConditionRow(
                    filter: $filter,
                    facets: facets,
                    collectionName: activeCollection?.name,
                    onSaveAsCollection: onSaveAsCollection
                )
            }
        }
        // 计数跟着当前条件走：每次条件变化都重取（"还剩几部"本来就相对当前条件）。
        // 面板没开且一个条件都没有时不请求——静止态不为用户还没表达的意图付网络开销
        .task(id: FacetRequest(filter: filter, active: open || !filter.isEmpty, allTiers: needsAllFacets)) {
            guard open || !filter.isEmpty else { return }
            do {
                facets = try await api.libraryFacetsFiltered(libraryId: libraryId, filter: filter, allTiers: needsAllFacets)
            } catch is CancellationError {
            } catch {
                // 计数拿不到就不显示数字（胶囊仍可点）：数字缺席比数字错了好
                facets = nil
            }
        }
        .sheet(isPresented: $open) {
            FilterSheet(filter: $filter, facets: facets, loading: !secondaryReady)
        }
    }

    /// 底部面板一打开就把一二级维度都摆出来，所以直接取全份；条件里已有二级维度时
    /// 条件行也需要二级的展示名（面板关着也要取全份）
    private var needsAllFacets: Bool { open || filter.hasSecondary }

    /// 全份 facet 到了没有：没到就先给一句话，别摆两个空标题等内容弹进来把面板撑高
    private var secondaryReady: Bool {
        guard let facets else { return false }
        return !facets.ratings.isEmpty || !facets.runtimes.isEmpty
    }

    // MARK: 筛选按钮

    private var filterButton: some View {
        let active = open || !filter.isEmpty
        return Button {
            open = true
        } label: {
            HStack(spacing: 6) {
                Image(systemName: "line.3.horizontal.decrease")
                    .font(.caption)
                    .opacity(0.7)
                Text("筛选")
                    .font(.footnote.weight(.medium))
                if filter.count > 0 {
                    Text("\(filter.count)")
                        .font(.footnote.monospacedDigit().weight(.semibold))
                        .foregroundStyle(.white.opacity(0.85))
                }
            }
            .foregroundStyle(active ? Theme.text : .white.opacity(0.9))
            .padding(.horizontal, 12)
            .frame(height: 32)
            .glassEffect(active ? .regular.tint(.white.opacity(0.16)).interactive() : .regular.interactive(), in: .capsule)
            .contentShape(.capsule)
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("library-filter-button")
    }

    // MARK: 合集 chip

    /// **chip 行只放自建的**：一个 300 部的库可能有 40+ 个自动生成的系列，混进来的话用户自己
    /// 存的那三五个就被挤没了。系列在合集视图里有自己的分组。
    private var ownCollections: [API.CollectionView] {
        collections.filter { $0.kind != "series" }
    }

    private var shownCollections: [API.CollectionView] {
        Array(ownCollections.prefix(Self.chipLimit))
    }

    /// 当前这面墙正好等于哪个合集——**推导**出来的，不存状态（见 LibraryFilter.key）。
    private var activeCollection: API.CollectionView? {
        let current = filter.key
        guard !current.isEmpty else { return nil }
        return collections.first { $0.ruleDriven && LibraryFilter(rules: $0.rules).key == current }
    }

    /// 规则驱动的合集 chip：点一下不是跳页，是**把当前这面墙筛成它**，再点一次退回全库。
    /// 名单驱动的合集没有规则、表达不成一组筛选条件，只能跳详情——让它假装能筛比诚实跳页更糟。
    private func collectionChip(_ collection: API.CollectionView) -> some View {
        let selected = collection.id == activeCollection?.id
        return Button {
            if collection.ruleDriven {
                filter = selected ? LibraryFilter() : LibraryFilter(rules: collection.rules)
            } else if let onOpenCollection {
                onOpenCollection(collection)
            } else {
                router.push(.collection(libraryId: libraryId, collectionId: collection.id))
            }
        } label: {
            HStack(spacing: 6) {
                Text(collection.name)
                Text("\(collection.itemCount)")
                    .monospacedDigit()
                    .foregroundStyle(.white.opacity(0.4))
            }
            .font(.caption)
            .foregroundStyle(selected ? .white : .white.opacity(0.7))
            .padding(.horizontal, 10)
            .frame(height: 28)
            .background(.white.opacity(selected ? 0.16 : 0.05), in: .capsule)
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

/// `.task(id:)` 的键：条件、面板开合、要不要全份，任何一个变了都重取计数
private struct FacetRequest: Equatable {
    let filter: LibraryFilter
    let active: Bool
    let allTiers: Bool
}

// MARK: - 条件行

/// 已选条件行：「＝ 合集「X」」+ 各维度分组（维间「且」、维内「或」）+ 清空 / 存为合集 + 筛出 N 部。
private struct FilterConditionRow: View {
    @Binding var filter: LibraryFilter
    let facets: API.LibraryFacetsView?
    /// 当前条件正好等于这个合集时的合集名；改动任意一条即为 nil
    let collectionName: String?
    let onSaveAsCollection: (() -> Void)?

    var body: some View {
        FilterFlowLayout(spacing: 8) {
            // 告诉用户这面墙此刻就是那个合集；用户一改条件就没了——
            // 那正是他从"在看合集"切换到"在自己筛"的时刻
            if let collectionName {
                HStack(spacing: 4) {
                    Text("＝ 合集").foregroundStyle(.white.opacity(0.7))
                    Text("「\(collectionName)」").fontWeight(.semibold).foregroundStyle(.white)
                }
                .font(.caption)
                .padding(.horizontal, 8)
                .frame(height: 28)
                .background(.white.opacity(0.1), in: .rect(cornerRadius: 8))
            }
            ForEach(Array(groups.enumerated()), id: \.element.key) { index, group in
                HStack(spacing: 8) {
                    // 维度之间是「且」——语义画出来，不写在脚注里
                    if index > 0 {
                        Text("且").font(.caption).foregroundStyle(.white.opacity(0.3))
                    }
                    groupChip(group)
                }
            }
            Button { filter = LibraryFilter() } label: {
                Text("清空")
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.5))
                    .padding(.horizontal, 8)
                    .frame(height: 28)
                    .expandedHitArea(vertical: 8)
            }
            .buttonStyle(.plain)
            // 已经等于某个合集时不再提「存为合集」——那只会存出一个重名的孪生体
            if let onSaveAsCollection, collectionName == nil {
                Button(action: onSaveAsCollection) {
                    Text("存为合集")
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.5))
                        .padding(.horizontal, 8)
                        .frame(height: 28)
                        .expandedHitArea(vertical: 8)
                }
                .buttonStyle(.plain)
            }
            if let facets {
                HStack(spacing: 0) {
                    Text("筛出 ")
                    Text("\(facets.total)").monospacedDigit().fontWeight(.semibold).foregroundStyle(.white)
                    Text(" 部")
                }
                .font(.caption)
                .foregroundStyle(.white.opacity(0.6))
                .frame(height: 28)
            }
        }
    }

    private struct DimGroup: Hashable {
        let key: String
        let label: String
        let values: [String]
    }

    private var groups: [DimGroup] {
        LibraryFilter.dimensions.compactMap { dim in
            let values = filter.values(of: dim.key)
            return values.isEmpty ? nil : DimGroup(key: dim.key, label: dim.label, values: values)
        }
    }

    private func groupChip(_ group: DimGroup) -> some View {
        HStack(spacing: 6) {
            Text(group.label)
                .font(.caption)
                .foregroundStyle(.white.opacity(0.4))
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(.black.opacity(0.25), in: .rect(cornerRadius: 4))
            ForEach(Array(group.values.enumerated()), id: \.element) { index, value in
                // 同一维度内的多个值是「或」
                if index > 0 {
                    Text("或").font(.caption).foregroundStyle(.white.opacity(0.3))
                }
                let label = facetLabel(group.key, value, facets: facets)
                HStack(spacing: 2) {
                    Text(label).font(.caption.weight(.semibold)).foregroundStyle(.white)
                    Button {
                        filter.remove(group.key, value)
                    } label: {
                        Image(systemName: "xmark")
                            .font(.system(size: 9, weight: .bold))
                            .foregroundStyle(.white.opacity(0.4))
                            .frame(width: 20, height: 22)
                            .contentShape(.rect)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("取消 \(group.label) \(label)")
                }
            }
        }
        .padding(.leading, 8)
        .padding(.trailing, 2)
        .frame(height: 28)
        .background(.white.opacity(0.12), in: .rect(cornerRadius: 8))
    }
}

/// 维度 → facet 里对应的候选表（取展示名 / 计数用）。
private func facetPool(_ dim: String, _ facets: API.LibraryFacetsView?) -> [API.FacetValueView] {
    guard let facets else { return [] }
    switch dim {
    case "genres": return facets.genres
    case "countries": return facets.countries
    case "decades": return facets.decades
    case "watch": return facets.watch
    case "rating_gte": return facets.ratings
    case "runtimes": return facets.runtimes
    case "languages": return facets.languages
    case "resolutions": return facets.resolutions
    case "hdr": return facets.hdr
    case "stock": return facets.stock
    default: return []
    }
}

/// 取值 → 展示名。**绝不把裸值印出来**：类型存的是 TMDB id、地区是国家码、片长是档位键，
/// 界面上冒出「878」「JP」「gt120」比空着更糟。查不到只有一种情况——展示名还在路上
/// （服务端保证选中的取值一定在 facet 里），所以给省略号占位，到了自然补上。
/// 分辨率、语言的取值本身就是人话（"2160p"、"ja"），直接印。
private func facetLabel(_ dim: String, _ value: String, facets: API.LibraryFacetsView?) -> String {
    if let label = facetPool(dim, facets).first(where: { $0.value == value })?.label { return label }
    return dim == "resolutions" || dim == "languages" ? value : "…"
}

// MARK: - 筛选面板（原生底部面板）

/// 点「筛选」拉起的底部面板（网页窄屏 FilterSheet）：一级四维在上，二级「找片 / 查库」在下，
/// 每个维度直接平铺成胶囊——取值和各自还剩几部一眼看全，没有「菜单里再弹菜单」。
///
/// **非全屏**是要点：上方留出一截墙、背景不压暗，用户看得见条件在实时影响什么。
/// 底部那颗「查看 N 部」不是提交（条件早已生效），只是"我看完了，收起来看结果"。
private struct FilterSheet: View {
    @Binding var filter: LibraryFilter
    let facets: API.LibraryFacetsView?
    /// 全份 facet 还没到（二级维度的档位要等它）
    let loading: Bool
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                // 一级四维
                pills("类型", hint: "可多选 · 维度内是「或」", dim: "genres")
                pills("年代", dim: "decades")
                pills("地区", hint: "可多选 · 维度内是「或」", dim: "countries")
                pills("观看", hint: "单选", dim: "watch")
                if loading {
                    Divider().overlay(Theme.line)
                    Text("正在数各档位还剩多少部…")
                        .font(.subheadline)
                        .foregroundStyle(Theme.textFaint)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 16)
                } else {
                    // 分栏依据是作用对象不同：「找片」问作品是什么样的，「查库」问文件是什么规格——
                    // 两种人格混在一排 chips 里会让两边都难用
                    Divider().overlay(Theme.line)
                    sectionTitle("找片", "作品是什么样的 · 来自刮削档案")
                    pills("评分", dim: "rating_gte")
                    pills("片长", dim: "runtimes")
                    pills("原始语言", dim: "languages")
                    Divider().overlay(Theme.line)
                    sectionTitle("查库", "文件是什么规格 · 来自库存台账")
                    pills("分辨率", dim: "resolutions")
                    pills("动态范围", dim: "hdr")
                    // 库存状态给语义色：要处理的事和不用处理的事，扫一眼就分得开
                    pills("库存状态", hint: "要处理的事", dim: "stock",
                          tone: ["missing": Theme.danger, "unscraped": Theme.warning])
                }
            }
            .padding(.horizontal, 16)
            .padding(.top, 20)
            .padding(.bottom, 12)
        }
        .scrollIndicators(.hidden)
        .safeAreaInset(edge: .bottom) {
            HStack(spacing: 8) {
                Button("清空") { filter = LibraryFilter() }
                    .buttonStyle(.glass)
                    .controlSize(.large)
                // 不是「确定」：条件早就生效了，这颗只是把面板收起来看结果
                Button {
                    dismiss()
                } label: {
                    Text(facets.map { "查看 \($0.total) 部" } ?? "查看结果")
                        .fontWeight(.medium)
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.glassProminent)
                .controlSize(.large)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
        }
        .presentationDetents([.fraction(0.7), .large])
        .presentationDragIndicator(.visible)
        // 不透明深色底（同 Web 筛选面板）：透明液态玻璃会让底下的海报透上来，筛选项文字看不清（R-5）
        .presentationBackground(Color(red: 0x17 / 255, green: 0x1A / 255, blue: 0x23 / 255))
        // 背景不压暗、可交互：压暗就等于全屏，看不见墙在变——恰好废掉这个面板存在的理由
        .presentationBackgroundInteraction(.enabled(upThrough: .fraction(0.7)))
    }

    private func sectionTitle(_ title: String, _ detail: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(.white)
            Text(detail).font(.caption).foregroundStyle(Theme.textFaint)
        }
    }

    /// 一组固定档位的胶囊。计数为 0 的置灰不可点——「永不空货架」的第一道闸。
    @ViewBuilder
    private func pills(_ label: String, hint: String? = nil, dim: String, tone: [String: Color] = [:]) -> some View {
        let options = facetPool(dim, facets)
        if !options.isEmpty {
            let selected = filter.values(of: dim)
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(label).foregroundStyle(Theme.textFaint)
                    if let hint { Text(hint).foregroundStyle(.white.opacity(0.25)) }
                }
                .font(.caption)
                FilterFlowLayout(spacing: 6) {
                    ForEach(options, id: \.value) { option in
                        let on = selected.contains(option.value)
                        Button {
                            filter.toggle(dim, option.value)
                        } label: {
                            HStack(spacing: 6) {
                                Text(option.label)
                                Text("\(option.count)")
                                    .font(.system(size: 10).monospacedDigit())
                                    .foregroundStyle(.white.opacity(0.35))
                            }
                            .font(.caption.weight(on ? .semibold : .regular))
                            .foregroundStyle(on ? .white : (tone[option.value] ?? .white.opacity(0.65)))
                            .padding(.horizontal, 12)
                            .padding(.vertical, 7)
                            .background(on ? .white.opacity(0.14) : .clear, in: .capsule)
                            .overlay(Capsule().strokeBorder(.white.opacity(on ? 0.4 : 0.14)))
                            .contentShape(.capsule)
                        }
                        .buttonStyle(.plain)
                        .disabled(option.count == 0 && !on)
                        .opacity(option.count == 0 && !on ? 0.3 : 1)
                        .accessibilityAddTraits(on ? .isSelected : [])
                    }
                }
            }
        }
    }
}

// MARK: - 筛空后的出路

/// 筛空之后的出路（铁律 2：永不空货架；对应网页 FilterEmptyState）。
///
/// 不渲染空墙——空墙什么也没说，用户只能一条条试。这里直接告诉他「放宽哪一条能救回多少部」，
/// 一点就生效。服务端**只返回救得回内容的条件**，拿到空表就说明这几条两两之间没有交集，
/// 那时只留「清空全部条件」。
struct LibraryFilterEmptyState: View {
    let libraryId: Int
    @Binding var filter: LibraryFilter

    @Environment(\.api) private var api
    @State private var relax: API.LibraryRelaxView?

    var body: some View {
        let suggestions = relax?.suggestions ?? []
        VStack(alignment: .leading, spacing: 0) {
            // 数的是**全部**条件，不只是一级四维：只数一级的话，用「4K + 评分≥9」筛空时
            // 标题会写「没有同时满足这 0 个条件的作品」
            Text("没有同时满足这 \(filter.count) 个条件的作品")
                .font(.headline)
                .foregroundStyle(.white)
            Text(suggestions.isEmpty
                 ? "这几个条件两两之间就没有交集，去掉任意一条也救不回来。"
                 : "放宽一条就能找回内容。")
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)
                .padding(.top, 4)
            VStack(spacing: 6) {
                ForEach(suggestions, id: \.self) { row in
                    Button {
                        filter.remove(row.dim, row.value)
                    } label: {
                        HStack(spacing: 12) {
                            Text("去掉「\(Text("\(row.dimLabel) = \(row.label)").fontWeight(.semibold).foregroundStyle(.white))」")
                                .frame(maxWidth: .infinity, alignment: .leading)
                            Text("→ \(row.count) 部")
                                .font(.caption.monospacedDigit())
                                .foregroundStyle(Theme.info)
                        }
                        .font(.subheadline)
                        .foregroundStyle(.white.opacity(0.8))
                        .padding(.horizontal, 12)
                        .padding(.vertical, 10)
                        .glassEffect(.regular.interactive(), in: .rect(cornerRadius: 12))
                        .contentShape(.rect(cornerRadius: 12))
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.top, 16)
            Button { filter = LibraryFilter() } label: {
                Text("清空全部条件")
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.5))
                    .expandedHitArea(vertical: 14)
            }
            .buttonStyle(.plain)
            .padding(.vertical, 8)
            .padding(.top, 6)
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(
            RoundedRectangle(cornerRadius: 16)
                .strokeBorder(.white.opacity(0.14), style: StrokeStyle(lineWidth: 1, dash: [5, 4]))
        )
        .task(id: filter) {
            do {
                relax = try await api.libraryRelaxFiltered(libraryId: libraryId, filter: filter)
            } catch is CancellationError {
            } catch {
                relax = nil
            }
        }
    }
}

// MARK: - 换行布局

/// 从左到右排、排不下换行的流式布局。筛选条与合集 chip 用它而不是横滚：
/// **换行会暴露数量，横滚只会掩盖数量**（docs/design/library-filtering.md 4.3）。
private struct FilterFlowLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = arrange(width: proposal.width ?? .infinity, subviews: subviews)
        let width = rows.map(\.width).max() ?? 0
        let height = rows.map(\.height).reduce(0, +) + spacing * CGFloat(max(rows.count - 1, 0))
        return CGSize(width: proposal.width ?? width, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var y = bounds.minY
        for row in arrange(width: bounds.width, subviews: subviews) {
            var x = bounds.minX
            for index in row.indices {
                let size = subviews[index].sizeThatFits(.unspecified)
                let fitted = CGSize(width: min(size.width, bounds.width), height: size.height)
                subviews[index].place(
                    at: CGPoint(x: x, y: y + (row.height - fitted.height) / 2),
                    proposal: ProposedViewSize(fitted)
                )
                x += fitted.width + spacing
            }
            y += row.height + spacing
        }
    }

    private struct Row {
        var indices: [Int] = []
        var width: CGFloat = 0
        var height: CGFloat = 0
    }

    private func arrange(width maxWidth: CGFloat, subviews: Subviews) -> [Row] {
        var rows: [Row] = []
        var current = Row()
        for index in subviews.indices {
            let size = subviews[index].sizeThatFits(.unspecified)
            let w = min(size.width, maxWidth)
            let needed = current.indices.isEmpty ? w : current.width + spacing + w
            if needed > maxWidth, !current.indices.isEmpty {
                rows.append(current)
                current = Row()
            }
            current.width = current.indices.isEmpty ? w : current.width + spacing + w
            current.height = max(current.height, size.height)
            current.indices.append(index)
        }
        if !current.indices.isEmpty { rows.append(current) }
        return rows
    }
}
