import SwiftUI

/// 合集详情（Web `library-collection-detail-view.tsx`，路由 `/library/{id}/c/{cid}` 与 `/library/c/{cid}`）。
///
/// 页头：名称、「已有 N / 共 M」（系列合集拉到上游档案时）、只有我可见 / 已隐藏；
/// 下面一行说清这个合集怎么来的：系列 / 固定名单 / 自动收录。自动收录按 iOS 的「摘要 + 点开看详情」：
/// 页头一枚玻璃胶囊只放一句摘要（「✦ 自动收录 · 地区：中国大陆、台湾、香港 ›」，与下面的排序胶囊同一质感），
/// 点开是底部弹层（`CollectionRulesSheet`），按维度分组列出条件、把「且 / 或」写成人话，能编辑的给「编辑条件」。
/// 原先照搬 Web 的「标签框 + 值 + 或 + 且」小胶囊拼图在手机上又密又不像可点，维度名还会被挤成「…」。
/// 排序：默认档是合集自己的序（手动合集叫「自定顺序」，系列叫「按上映顺序」），其余同单库页，按合集分别记忆。
/// 系列合集在默认序下把库里缺的几部按上映时间插进墙里（「未入库 / 追踪中」）。
///
/// ⋯ 菜单：改名 / 分享…（管理员）/ 改条件…（规则合集）/ 整理顺序…（手动合集）/
/// 显示在首页 · 从首页移除 / 恢复显示 或 隐藏这个合集 · 删除合集；图床浏览时还有按作品分组与密度。
struct CollectionDetailView: View {
    let libraryId: Int?
    let collectionId: Int

    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Feedback.self) private var feedback
    @Environment(Router.self) private var router
    @Environment(AppModel.self) private var model
    @State private var homePrefs = LibraryHomePrefs.shared
    @State private var gallery = GalleryPrefs.shared

    @State private var collection: API.CollectionView?
    @State private var error: String?
    @State private var series: API.CollectionSeriesView?
    @State private var facets: API.LibraryFacetsView?
    @State private var pager = LibraryWallPager<API.LibraryItemView>(pageSize: 60)
    @State private var sort = WallSortState(sort: "default")
    @State private var sortLoaded = false
    @State private var editing: LibraryFilter?
    @State private var ordering: [API.LibraryItemView]?
    @State private var share: ShareRequest?
    @State private var membersEpoch = 0
    /// 「自动收录」条件详情弹层
    @State private var showingRules = false
    /// 海报墙当前窗口是按哪个排序载入的
    @State private var wallSortKey: String?

    private struct ShareRequest: Identifiable {
        var initial: API.ShareView?
        var id: Int { initial?.id ?? 0 }
    }

    private var sortStorageKey: String { "movieclaw.collection.wall-sort:\(collectionId)" }
    private static let prefLabels: [(String, String)] = [
        ("title", "按标题"), ("added_at", "最近添加"), ("release_date", "按上映时间"), ("rating", "按评分"),
        ("runtime", "按片长"), ("size", "按体积"), ("last_played", "最近观看"),
    ]

    /// 默认档：合集自己的序
    private var defaultSort: (label: String, equivalent: String?, direction: WallSortDirection) {
        guard let collection, collection.ruleDriven else {
            return ("自定顺序", nil, WallSortDirection(naturalAsc: true, asc: "正序", desc: "倒序"))
        }
        if collection.kind == "series" || collection.sort == "release_date_asc" {
            var direction = WallSortDirections.of("release_date")!
            direction.naturalAsc = true
            return ("按上映顺序", "release_date", direction)
        }
        let key = Self.prefLabels.contains { $0.0 == collection.sort } ? collection.sort : "title"
        return (Self.prefLabels.first { $0.0 == key }!.1, key, WallSortDirections.of(key)!)
    }

    private var sortOptions: [WallSortMenu.Option] {
        let d = defaultSort
        return [.init(value: "default", label: d.label, direction: d.direction)]
            + Self.prefLabels.filter { $0.0 != d.equivalent }.map { .init(value: $0.0, label: $0.1, direction: WallSortDirections.of($0.0)) }
    }

    private var sortParams: (sort: String?, order: String?) {
        if sort.sort == "default" {
            return (nil, sort.reversed ? (defaultSort.direction.naturalAsc ? "desc" : "asc") : nil)
        }
        return (sort.sort, WallSortDirections.of(sort.sort)?.orderParam(reversed: sort.reversed))
    }

    private var sortKey: String { "\(sort.sort == "default" ? "" : sort.sort)\(sort.reversed ? ":rev" : "")" }
    private var rows: [API.LibraryItemView] { pager.items ?? [] }
    private var galleryOn: Bool { gallery.galleryMode && !rows.isEmpty }
    private var homePrefsOwner: String { LibraryHomePrefs.ownerKey(api: api, username: model.session?.username) }

    private var homeRow: API.HomeRowPref? { homePrefs.rows?.first { $0.collectionId == collectionId } }
    private var onHome: Bool { homeRow.map { $0.hidden != true } ?? false }

    var body: some View {
        Group {
            if let error {
                EmptyState(systemImage: "rectangle.stack.badge.minus", title: error)
            } else {
                content
            }
        }
        .appBackground()
        .navigationTitle(collection?.name ?? "合集")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if let collection {
                if !rows.isEmpty {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button {
                            gallery.galleryMode.toggle()
                        } label: {
                            Image(systemName: galleryOn ? "square.grid.2x2" : "photo.on.rectangle.angled")
                        }
                        .accessibilityLabel(galleryOn ? "回到海报墙" : "图床浏览")
                    }
                }
                ToolbarItem(placement: .topBarTrailing) { menu(collection) }
            }
        }
        .task { await load() }
        // 缺片格的「追踪中 / 管理订阅」要读全站订阅索引（同 Web subscriptionOf）
        .tracksSubscriptionIndex()
        .task(id: "\(sortKey)|\(sortLoaded)") {
            guard sortLoaded else { return }
            // `.task` 每次重新出现都会重跑：排序没变（从条目详情返回）就只整窗对账、位置不动，
            // 不能 reset 回墙首（同 Web 快照恢复；第二轮审计 N-04a-2）
            if wallSortKey == sortKey, pager.items != nil {
                await pager.refresh()
                return
            }
            sort.save(sortStorageKey)
            wallSortKey = sortKey
            await reloadItems()
        }
        .sheet(item: $share) { request in
            if let collection {
                LibraryShareSheet(
                    target: .collection(id: collection.id), title: collection.name,
                    posterUrl: collection.covers.first?.url,
                    seasonSummary: "\(collection.itemCount) 部\(collection.ruleDriven ? " · 会自动收录新片" : "")",
                    initialShare: request.initial
                )
                .sheetFeedback()
            }
        }
        .sheet(isPresented: $showingRules) {
            if let collection {
                CollectionRulesSheet(
                    groups: ruleGroups(LibraryFilter(rules: collection.rules)),
                    onEdit: libraryId != nil && collection.editable
                        ? {
                            showingRules = false
                            editing = LibraryFilter(rules: collection.rules)
                        }
                        : nil
                )
                .sheetFeedback()
            }
        }
        .sheet(isPresented: Binding(get: { ordering != nil }, set: { if !$0 { ordering = nil } })) {
            if let ordering {
                CollectionOrderSheet(collectionId: collectionId, items: ordering) { membersChanged() }
                    .sheetFeedback()
            }
        }
    }

    @ViewBuilder
    private var content: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                header
                wall.padding(.top, 20)
            }
            .padding(.bottom, 40)
        }
        .refreshable { await load() }
    }

    // MARK: 页头

    @ViewBuilder
    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(collection?.name ?? " ")
                .font(.title2.weight(.semibold))
                .foregroundStyle(Theme.text)
            if let collection {
                let count = series.map { $0.available && $0.total > 0 } == true
                    ? "已有 \(series!.ownedCount) / 共 \(series!.total) 部" : "\(collection.itemCount) 部"
                Text([count, collection.visibility == "private" ? "只有我可见" : nil, collection.hidden ? "已隐藏" : nil]
                    .compactMap { $0 }.joined(separator: " · "))
                    .font(.subheadline)
                    .foregroundStyle(Theme.textMuted)
                    .accessibilityIdentifier("collection-count")
                if let editing {
                    rulesEditor(editing)
                } else {
                    ruleRow(collection)
                    if !rows.isEmpty {
                        WallSortMenu(options: sortOptions, state: $sort)
                            .glassEffect(.regular.interactive(), in: .capsule)
                    }
                }
            }
        }
        .padding(.horizontal, Theme.pagePadding)
        .padding(.top, 4)
    }

    @ViewBuilder
    private func ruleRow(_ collection: API.CollectionView) -> some View {
        if collection.kind == "series" {
            note("作品系列 · 按上映顺序排列，以后入库的续作会自动归进来")
        } else if !collection.ruleDriven {
            note("固定名单 · 不会自动收录新片")
        } else {
            let groups = ruleGroups(LibraryFilter(rules: collection.rules))
            if groups.isEmpty {
                note("自动收录 · 收录本库全部作品")
            } else {
                Button {
                    showingRules = true
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "sparkles").foregroundStyle(Theme.accent)
                        Text("自动收录").fontWeight(.semibold).foregroundStyle(Theme.text)
                        Text("·").foregroundStyle(Theme.textFaint)
                        Text(ruleSummary(groups)).foregroundStyle(Theme.textMuted).lineLimit(1)
                        Image(systemName: "chevron.right").font(.caption2.weight(.semibold)).foregroundStyle(Theme.textFaint)
                    }
                    .font(.footnote)
                    .padding(.horizontal, 12)
                    .frame(height: 32)
                    .contentShape(.capsule)
                }
                .buttonStyle(.plain)
                .glassEffect(.regular.interactive(), in: .capsule)
                .accessibilityLabel("自动收录条件：\(ruleSummary(groups))")
                .accessibilityHint("查看收录条件")
                .accessibilityIdentifier("collection-rules")
            }
        }
    }

    /// 一行摘要：维度之间用「 · 」，维度内的取值用顿号（「地区：中国大陆、台湾、香港 · 类型：剧情」）
    private func ruleSummary(_ groups: [(label: String, values: [String])]) -> String {
        groups.map { "\($0.label)：\($0.values.joined(separator: "、"))" }.joined(separator: " · ")
    }

    private func note(_ text: String) -> some View {
        Text(text).font(.footnote).foregroundStyle(Theme.textFaint)
    }

    /// 规则 → 展示分组（维度之间是「且」，维度内是「或」）；取值的人话来自整库 facet
    private func ruleGroups(_ filter: LibraryFilter) -> [(label: String, values: [String])] {
        func label(_ pool: [API.FacetValueView]?, _ value: String) -> String {
            pool?.first { $0.value == value }?.label ?? "…"
        }
        var groups: [(String, [String])] = []
        if !filter.genres.isEmpty { groups.append(("类型", filter.genres.map { label(facets?.genres, String($0)) })) }
        if !filter.decades.isEmpty { groups.append(("年代", filter.decades.map { label(facets?.decades, $0) })) }
        if !filter.countries.isEmpty { groups.append(("地区", filter.countries.map { label(facets?.countries, $0) })) }
        if let watch = filter.watch { groups.append(("观看", [label(facets?.watch, watch)])) }
        return groups
    }

    @ViewBuilder
    private func rulesEditor(_ editing: LibraryFilter) -> some View {
        if let libraryId {
            VStack(alignment: .leading, spacing: 10) {
                LibraryFilterBar(libraryId: libraryId, filter: Binding(get: { self.editing ?? editing }, set: { self.editing = $0 }))
                HStack(spacing: 10) {
                    Button("保存条件") { Task { await saveRules() } }
                        .buttonStyle(.glassProminent)
                        .disabled(editing.isEmpty)
                    Button("取消") { self.editing = nil }.buttonStyle(.glass)
                    if editing.isEmpty {
                        Text("至少留一个条件，否则这个合集会收录整库").font(.footnote).foregroundStyle(Theme.textFaint)
                    }
                }
            }
        }
    }

    // MARK: 墙

    @ViewBuilder
    private var wall: some View {
        let missing = series.map { s in
            s.available && sort.sort == "default" && !sort.reversed ? s.parts.filter { $0.mediaItemId == nil } : []
        } ?? []
        if pager.items == nil {
            if let failed = pager.failed {
                ErrorState(message: failed) { await reloadItems() }
            } else {
                ProgressView().frame(maxWidth: .infinity).padding(.top, 40)
            }
        } else if galleryOn {
            LibraryGalleryWall(reloadKey: AnyHashable("\(collectionId)|\(sortKey)|\(membersEpoch)")) { [api, collectionId, sortParams] offset, limit in
                try await api.collectionGallery(collectionId: collectionId, limit: limit, offset: offset, sort: sortParams.sort, order: sortParams.order)
            } onOpenItem: { group in
                router.push(.libraryItem(libraryId: group.libraryId, itemId: group.mediaItemId))
            }
        } else if rows.isEmpty, missing.isEmpty {
            Text("这个合集现在一部都没有。")
                .font(.subheadline).foregroundStyle(Theme.textMuted)
                .frame(maxWidth: .infinity).padding(.top, 64)
        } else {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 140), spacing: 12, alignment: .top)], alignment: .leading, spacing: 20) {
                ForEach(cells(missing: pager.hasMore ? [] : missing)) { cell in
                    switch cell {
                    case let .item(item):
                        // 不钉框比例：每格按主图比例取 2:3 或 16:9（同 Web PosterWall 不传 frameAspect）
                        LibraryInventoryCell(item: item, libraryId: libraryId ?? item.libraryId ?? 0)
                    case let .part(part):
                        MissingPartCell(part: part)
                    }
                }
            }
            .padding(.horizontal, Theme.pagePadding)
            .accessibilityIdentifier("collection-wall")
            WallLoadMoreFooter(hasMore: pager.hasMore, start: 0, loaded: rows.count, total: collection?.itemCount) {
                await pager.loadMore()
            }
        }
    }

    private enum Cell: Identifiable {
        case item(API.LibraryItemView)
        case part(API.SeriesPartView)
        var id: String {
            switch self {
            case let .item(item): "item:\(item.mediaItemId)"
            case let .part(part): "missing:\(part.tmdbId)"
            }
        }
    }

    /// 缺的几部按上映时间插进墙里（名单都到齐了才插，否则插的位置不对）
    private func cells(missing: [API.SeriesPartView]) -> [Cell] {
        let dateOf: (String?) -> String = { $0 ?? "9999-12-31" }
        var cells: [Cell] = []
        var next = 0
        for item in rows {
            while next < missing.count, dateOf(missing[next].releaseDate) < dateOf(item.releaseDate) {
                cells.append(.part(missing[next]))
                next += 1
            }
            cells.append(.item(item))
        }
        cells += missing[next...].map { .part($0) }
        return cells
    }

    // MARK: ⋯ 菜单

    private func menu(_ collection: API.CollectionView) -> some View {
        Menu {
            if galleryOn {
                GalleryPrefMenuItems()
                Divider()
            }
            Button("改名") { Task { await rename(collection) } }
            if permissions.canManageLibraries {
                Button("分享…") { Task { await openShare() } }
            }
            if libraryId != nil, collection.editable, collection.ruleDriven {
                Button("改条件…") { editing = LibraryFilter(rules: collection.rules) }
            }
            if collection.editable, !collection.ruleDriven {
                Button("整理顺序…") { Task { await openOrdering() } }
            }
            Button(onHome ? "从首页移除" : "显示在首页") { Task { await toggleOnHome(collection) } }
            Divider()
            if collection.hidden {
                Button("恢复显示") { Task { await unhide(collection) } }
            } else {
                Button(collection.kind != "user" ? "隐藏这个合集" : "删除合集", role: collection.kind != "user" ? nil : .destructive) {
                    Task { await remove(collection) }
                }
            }
        } label: {
            Image(systemName: "ellipsis")
        }
        .accessibilityLabel("更多操作")
        .accessibilityIdentifier("collection-actions")
    }

    // MARK: 动作

    private func load() async {
        do {
            let row = try await api.collectionGet(collectionId: collectionId)
            collection = row
            if !sortLoaded {
                sort = WallSortState.load(sortStorageKey, default: WallSortState(sort: "default"), allowed: ["default"] + Self.prefLabels.map(\.0))
                sortLoaded = true
            }
            await homePrefs.ensureLoaded(api: api, owner: homePrefsOwner)
            if row.kind == "series" { series = try? await api.collectionSeriesGet(collectionId: collectionId) }
            if !row.rules.isEmpty, let libraryId {
                let filter = LibraryFilter(rules: row.rules)
                facets = try? await api.libraryFacetsFiltered(libraryId: libraryId, filter: filter, allTiers: true)
            }
        } catch is CancellationError {
        } catch {
            self.error = "这个合集不存在，或者你看不到它。"
        }
    }

    private func reloadItems() async {
        let api = self.api
        let id = collectionId
        let params = sortParams
        await pager.reset({ offset, limit in
            try await api.collectionItemsList(collectionId: id, limit: limit, offset: offset, sort: params.sort, order: params.order)
        })
    }

    private func membersChanged() {
        membersEpoch += 1
        Task {
            await reloadItems()
            if let row = try? await api.collectionGet(collectionId: collectionId) { collection = row }
        }
    }

    private func rename(_ collection: API.CollectionView) async {
        guard let name = await feedback.prompt("合集名", initial: collection.name)?.trimmingCharacters(in: .whitespaces),
              !name.isEmpty, name != collection.name else { return }
        do {
            self.collection = try await api.collectionUpdate(collectionId: collection.id, body: API.CollectionPayload(name: name))
            feedback.success("已改名")
        } catch {
            feedback.error(error)
        }
    }

    private func openShare() async {
        do {
            let existing = try await api.collectionShareGet(collectionId: collectionId)
            share = ShareRequest(initial: existing)
        } catch {
            feedback.error(error)
        }
    }

    /// 自定顺序下直接用墙上的名单；换了别的排序时先按自定顺序取一份
    private func openOrdering() async {
        if sortKey.isEmpty {
            ordering = rows
            return
        }
        do {
            ordering = try await api.collectionItemsList(collectionId: collectionId, limit: 60)
        } catch {
            feedback.error(error.localizedDescription.isEmpty ? "取不到名单" : error.localizedDescription)
        }
    }

    /// 「显示在首页」：把这个合集加成首页的一行；再点一次是隐藏那一行，不删（与自定义页同一份偏好）
    private func toggleOnHome(_ collection: API.CollectionView) async {
        let wasOnHome = onHome
        do {
            // 首页偏好还没读到（或上次读取失败、刚换了账号）时先强制重拉：以空清单或别的账号的清单为底
            // 整份保存会覆盖用户自定义的行
            let owner = homePrefsOwner
            homePrefs.adopt(owner: owner)
            if homePrefs.rows == nil { homePrefs.accept(try await api.uiPrefsShow().home.rows, for: owner) }
            let rows: [API.HomeRowPrefInput]
            if homeRow != nil {
                let saved = homePrefs.rows ?? []
                rows = try saved.map { row in
                    var input = try JSONDecoder().decode(API.HomeRowPrefInput.self, from: JSONEncoder().encode(row))
                    if row.collectionId == collection.id { input.hidden = wasOnHome ? true : nil }
                    return input
                }
            } else {
                async let libs = api.libraryList(scope: "all")
                async let cols = api.collectionList()
                let (l, c) = try await (libs, cols)
                rows = HomeRows.toPrefs(HomeRows.build(prefs: homePrefs.rows ?? [], libraries: l, collections: c) + [HomeRows.newCollectionRow(collection)])
            }
            try await homePrefs.save(rows, api: api)
            feedback.success(wasOnHome ? "已从首页移除，播放器里的这个媒体库也会一并消失" : "已显示在首页，播放器里也会多出这个媒体库")
        } catch {
            feedback.error(error.localizedDescription.isEmpty ? "保存失败" : error.localizedDescription)
        }
    }

    private func remove(_ collection: API.CollectionView) async {
        let automatic = collection.kind != "user"
        let ok = await feedback.confirm(
            automatic ? "隐藏「\(collection.name)」？" : "删除合集「\(collection.name)」？",
            message: automatic
                ? "自动生成的合集会一直重新出现，所以这里是把它藏起来：影片一部都不会少，想找回来在媒体库设置里打开「显示已隐藏的合集」。"
                : "只删掉这层视图，里面的影片一部都不会少。",
            confirmTitle: automatic ? "隐藏" : "删除",
            destructive: !automatic
        )
        guard ok else { return }
        do {
            try await api.collectionDelete(collectionId: collection.id)
            feedback.success(automatic ? "已隐藏" : "已删除")
            router.pop()
        } catch {
            feedback.error(error.localizedDescription.isEmpty ? (automatic ? "隐藏失败" : "删除失败") : error.localizedDescription)
        }
    }

    private func unhide(_ collection: API.CollectionView) async {
        do {
            self.collection = try await api.collectionUpdate(collectionId: collection.id, body: API.CollectionPayload(hidden: false))
            feedback.success("已恢复显示")
        } catch {
            feedback.error(error.localizedDescription.isEmpty ? "恢复失败" : error.localizedDescription)
        }
    }

    private func saveRules() async {
        guard let collection, let editing else { return }
        do {
            self.collection = try await api.collectionUpdate(collectionId: collection.id, body: API.CollectionPayload(rules: editing.rules))
            self.editing = nil
            membersChanged()
            feedback.success("条件已保存")
        } catch {
            feedback.error(error.localizedDescription.isEmpty ? "保存失败" : error.localizedDescription)
        }
    }
}

/// 「自动收录」条件详情（合集页头胶囊点开）。
///
/// 系统分组列表：每个维度一组、列出取值（组内满足其一即可），顶部一句话说清「同时满足每一组」与
/// 「以后入库的新片也会自动归进来」——把 Web 胶囊里的「且 / 或」符号写成人话。
/// 能编辑的合集（本库视图里、用户自建的规则合集）底部给「编辑条件」，回到页头的条件编辑器。
private struct CollectionRulesSheet: View {
    let groups: [(label: String, values: [String])]
    /// nil = 不可编辑（系统生成的合集、跨库视图）
    let onEdit: (() -> Void)?

    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section {
                } footer: {
                    Text(groups.count > 1
                        ? "同时满足下面每一组条件的作品会自动收进这个合集，以后入库的新片也会自动归进来。"
                        : "满足下面条件的作品会自动收进这个合集，以后入库的新片也会自动归进来。")
                }
                ForEach(Array(groups.enumerated()), id: \.offset) { _, group in
                    Section {
                        ForEach(Array(group.values.enumerated()), id: \.offset) { _, value in
                            Text(value)
                        }
                    } header: {
                        Text(group.label)
                    } footer: {
                        if group.values.count > 1 { Text("满足其中之一即可") }
                    }
                }
                if let onEdit {
                    Section {
                        Button("编辑条件", systemImage: "slider.horizontal.3", action: onEdit)
                    }
                }
            }
            .navigationTitle("自动收录")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") { dismiss() }
                }
            }
        }
        .presentationDetents([.medium, .large])
        .accessibilityIdentifier("collection-rules-sheet")
    }
}

/// 系列里库中还没有的一部（Web `MissingPartCell`）：点开去发现页详情，长按订阅 / 管理订阅。
///
/// 「追踪中」= 接口标了已订阅，**或**全站订阅索引里已有这部 TMDB 电影（刚在别处订上、合集数据还没刷新）；
/// 已追踪时长按给「管理订阅」而不是再订一遍。只压暗海报图、不压暗片名（同 Web）。
private struct MissingPartCell: View {
    let part: API.SeriesPartView
    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Router.self) private var router

    private var subscription: API.SubscriptionView? {
        SubscriptionIndex.shared.subscription(source: "tmdb", externalId: String(part.tmdbId), mediaType: "movie")
    }

    var body: some View {
        let tracked = part.subscribed || subscription != nil
        NavigationLink(value: AppRoute.mediaDetail(titleRef: "tmdb:movie:\(part.tmdbId)")) {
            LibraryPosterCell(
                title: part.title,
                year: part.releaseDate.flatMap { Int($0.prefix(4)) },
                extent: tracked ? "追踪中" : "未入库",
                url: api.image(part.posterUrl, .posterCard),
                artworkOpacity: 0.4
            )
        }
        .buttonStyle(.plain)
        .contextMenu {
            if permissions.canSubscribe {
                if let subscription {
                    Button {
                        router.push(.subscription(id: subscription.id))
                    } label: {
                        Label("管理订阅", systemImage: "checkmark.circle")
                    }
                } else if !part.subscribed {
                    Button {
                        router.present(.subscribe(SubscribeRequest(titleRef: "tmdb:movie:\(part.tmdbId)", title: part.title)))
                    } label: {
                        Label("订阅影片", systemImage: "plus")
                    }
                }
            }
        }
    }
}

/// 整理顺序（Web `CollectionOrderPanel`）：拖动或上下键调整先后，✕ 移出名单；保存 `PUT /collections/{id}/order`
private struct CollectionOrderSheet: View {
    let collectionId: Int
    @State var items: [API.LibraryItemView]
    var onSaved: () -> Void
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(\.dismiss) private var dismiss
    @State private var busy = false

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach(Array(items.enumerated()), id: \.element.mediaItemId) { index, row in
                        HStack(spacing: 10) {
                            Text("\(index + 1)").font(.caption.monospacedDigit()).foregroundStyle(.white.opacity(0.35)).frame(width: 24, alignment: .trailing)
                            HStack(spacing: 6) {
                                Text(row.title).foregroundStyle(Theme.text).lineLimit(1)
                                if let year = row.year { Text(String(year)).foregroundStyle(.white.opacity(0.35)) }
                            }
                            Spacer(minLength: 4)
                            Button { move(index, index - 1) } label: { Image(systemName: "arrow.up") }
                                .disabled(index == 0).accessibilityLabel("上移")
                            Button { move(index, index + 1) } label: { Image(systemName: "arrow.down") }
                                .disabled(index == items.count - 1).accessibilityLabel("下移")
                            Button { Task { await drop(row) } } label: { Image(systemName: "xmark") }
                                .disabled(busy).accessibilityLabel("移出 \(row.title)")
                        }
                        .buttonStyle(.borderless)
                    }
                    .onMove { items.move(fromOffsets: $0, toOffset: $1) }
                    if items.isEmpty {
                        Text("这个合集现在一部都没有。").foregroundStyle(Theme.textFaint)
                    }
                } header: {
                    Text("拖动调整先后；顺序在网页、播放器和分享页三处一致。移出去只是从名单里去掉，影片一部都不会少。")
                        .textCase(nil)
                }
            }
            .navigationTitle("整理顺序")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存顺序") { Task { await save() } }.disabled(busy || items.isEmpty)
                }
            }
        }
    }

    private func move(_ from: Int, _ to: Int) {
        guard to >= 0, to < items.count, from != to else { return }
        let row = items.remove(at: from)
        items.insert(row, at: to)
    }

    private func save() async {
        busy = true
        do {
            _ = try await api.collectionItemsReorder(collectionId: collectionId, body: API.CollectionItemsPayload(mediaItemIds: items.map(\.mediaItemId)))
            feedback.success("顺序已保存")
            onSaved()
            dismiss()
        } catch {
            feedback.error(error)
            busy = false
        }
    }

    private func drop(_ row: API.LibraryItemView) async {
        busy = true
        defer { busy = false }
        do {
            _ = try await api.collectionItemsRemove(collectionId: collectionId, mediaItemId: row.mediaItemId)
            items.removeAll { $0.mediaItemId == row.mediaItemId }
            onSaved()
        } catch {
            feedback.error(error)
        }
    }
}
