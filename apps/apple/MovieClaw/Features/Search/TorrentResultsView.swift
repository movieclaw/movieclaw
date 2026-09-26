import SwiftUI

/// 「站点资源」垂直（对应 Web `components/search-results.tsx` 的界面部分）。
///
/// 自上而下：手动选种横幅（for_sub）→ 状态行（计数 / 快照 / 站点状态）→
/// 条件胶囊行（排序 + 各筛选维度，同发现页结果头部的玻璃胶囊）→ 进度条 → 结果。
/// 视图切换（分组 / 列表 / 图览）在顶栏右上角（`TorrentViewModeMenu`，由 `SearchResultsView` 挂）。
/// 行点按打开资源操作面板（下载 / 投给订阅 / 浏览图片 / 站点详情页）。
struct TorrentResultsView: View {
    @Bindable var model: TorrentSearchModel
    /// 手动选种模式：目标订阅（id + 标题）
    let grabTarget: (id: Int, title: String)?
    let onResearch: (() -> Void)?

    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Router.self) private var router
    @Environment(Feedback.self) private var feedback

    @State private var actions = TorrentActionsState()
    @State private var showsSites = false

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 10, pinnedViews: []) {
                if let grabTarget {
                    grabBanner(grabTarget)
                }
                header
                if model.phase == .done, model.sites.isEmpty, model.snapshotAt == nil {
                    Text("当前没有「已启用且验证通过」的站点，请先在设置里配置站点。")
                        .font(.subheadline)
                        .foregroundStyle(Theme.textMuted)
                        .accessibilityIdentifier("torrent-no-sites")
                }
                if !model.sites.isEmpty {
                    conditionChips
                }
                if model.streaming {
                    progressBar
                }
                results
            }
            .padding(.horizontal, Theme.pagePadding)
            .padding(.bottom, 40)
        }
        .scrollDismissesKeyboard(.immediately)
        .task {
            model.start(api: api)
            guard permissions.canDirectDownload else { return }
            await actions.prefs.refresh(api: api)
            await actions.prefs.loadDirs(api: api)
        }
        .sheet(isPresented: $showsSites) {
            SiteStatusSheet(model: model, canRetry: model.snapshotAt == nil)
        }
        .sheet(item: $actions.sheetHit) { item in
            TorrentActionsSheet(hit: item.hit, actions: actions, grabTarget: grabTarget, showsImages: item.fromGallery)
        }
        .sheet(item: $actions.dialogRequest) { request in
            DownloadTargetSheet(request: request, remembered: actions.prefs.byCategory[request.category]) { result in
                actions.downloadStates[request.hitKey] = result.alreadyExists ? .exists : .done
                feedback.success(result.alreadyExists ? "该种子已在下载器中，未重复添加" : "已提交到「\(result.downloaderName)」\(result.savePath.map { " · \($0)" } ?? "")")
                Task { await actions.prefs.refresh(api: api) }
            }
        }
        .sheet(item: $actions.confirming) { confirming in
            DownloadConfirmSheet(
                request: confirming.request,
                target: confirming.target,
                onConfirm: { actions.confirmRemembered(confirming, api: api, feedback: feedback) },
                onChange: { actions.reopenDialog(confirming.request, reason: nil) },
                onForget: {
                    Task { await actions.prefs.forget(confirming.target.category, api: api) }
                    actions.reopenDialog(confirming.request, reason: "已清除「\(TorrentCategories.label(confirming.target.category))」的默认位置。想重新设一个，在下面勾上「记住本次选择」。")
                }
            )
        }
        .fullScreenCover(item: $actions.lightbox) { DiscoverLightbox(content: $0).sheetFeedback() }
        .onChange(of: grabTarget?.id, initial: true) { actions.grabTarget = grabTarget }
        .environment(actions)
    }

    // MARK: 头部

    private func grabBanner(_ target: (id: Int, title: String)) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("正在为《\(target.title)》手动选种——点资源上的「投给订阅」直接下载并计入该订阅（跳过规则组限制）")
                .font(.subheadline)
            Button {
                router.push(.subscription(id: target.id))
            } label: {
                Label("返回订阅", systemImage: "chevron.left").font(.subheadline.weight(.semibold))
            }
        }
        .foregroundStyle(Color(red: 0.6, green: 0.78, blue: 1))
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.info.opacity(0.12), in: .rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.info.opacity(0.3)))
        .discoverContainer("grab-banner")
    }

    /// 结果状态行：左边条数，右边站点状态；快照回放时下面再给一行快照时间与「重新搜索」。
    /// 关键词不在这里重复——顶栏正中的搜索词胶囊已经写着（见 `SearchResultsView`）
    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                switch model.phase {
                case .streaming:
                    Circle().fill(Theme.accent).frame(width: 6, height: 6)
                    Text(verbatim: "已找到 \(model.items.count) 条")
                case .done:
                    Text(model.filters.isActive ? "筛选后 \(model.filtered.count) / 共 \(model.items.count) 条" : "共 \(model.items.count) 条结果")
                default:
                    EmptyView()
                }
                Spacer(minLength: 0)
                if !model.sites.isEmpty {
                    siteSummaryButton
                }
            }
            if let snapshotAt = model.snapshotAt, model.phase == .done {
                HStack(spacing: 8) {
                    Label("\(SubsFormat.relative(snapshotAt))的快照", systemImage: "clock")
                        .font(.caption)
                    if let onResearch {
                        Button("重新搜索", action: onResearch)
                            .font(.caption.weight(.semibold))
                            .accessibilityIdentifier("torrent-research")
                    }
                }
            }
        }
        .font(.subheadline)
        .monospacedDigit()
        .foregroundStyle(Theme.textMuted)
        .discoverContainer("torrent-status")
        .padding(.top, 4)
    }

    /// 站点状态聚合 chip：搜索中「x/N 站点」、完成后「N 站点 · M 失败」；点开看逐站详情
    private var siteSummaryButton: some View {
        Button {
            showsSites = true
        } label: {
            HStack(spacing: 6) {
                Circle()
                    .fill(model.streaming ? Theme.accent : model.failedCount > 0 ? Theme.danger : Theme.success)
                    .frame(width: 6, height: 6)
                if model.streaming {
                    Text("\(model.settledCount)/\(model.sites.count) 站点")
                } else {
                    Text("\(model.sites.count) 站点")
                    if model.failedCount > 0 { Text("· \(model.failedCount) 失败").foregroundStyle(Theme.danger) }
                }
                Image(systemName: "chevron.down").font(.caption2)
            }
            .font(.caption)
            .monospacedDigit()
            .foregroundStyle(Theme.textMuted)
            .padding(.horizontal, 10)
            .frame(height: 28)
            // 与条件胶囊同一种玻璃，点开是统一玻璃弹层（SiteStatusSheet）
            .glassEffect(.regular.interactive(), in: .capsule)
            .contentShape(.capsule)
            .expandedHitArea(vertical: 8)
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("torrent-sites")
    }

    // MARK: 条件胶囊

    /// 排序与筛选：一排玻璃胶囊（同发现页结果头部 `DiscoverFilterChips`、地图搜索结果那排胶囊的形态）。
    /// 不开弹窗：每颗胶囊点开是系统液态玻璃菜单、从胶囊原地长出，选中立刻生效，没有「查看结果」
    /// （原先的整页筛选 sheet 与发现页、订阅页的交互都不一样，按用户反馈拆掉）。
    /// - 第一颗是排序，写当前键与方向；
    /// - 其后按固定顺序列本次结果里出现过的维度（分辨率 → 站点 → 年份 → … → 压制组），
    ///   没有取值的维度不出（已选的例外，免得选中项凭空消失）。已启用的胶囊高亮并写出所选值；
    /// - 有条件时末尾一颗「清除」。
    /// 顺序固定不按启用与否重排——改完一项胶囊原地变亮，手指下的东西不会跑位。
    private var conditionChips: some View {
        let dims = TorrentFilterDim.allCases.filter { !dimValues($0).isEmpty || !model.filters.values($0).isEmpty }
        return ScrollView(.horizontal) {
            GlassEffectContainer(spacing: 8) {
                HStack(spacing: 8) {
                    sortChip
                    ForEach(dims, id: \.self) { dim in
                        filterChip(dim)
                    }
                    if model.filters.isActive {
                        Button {
                            model.filters = TorrentFilters()
                        } label: {
                            conditionLabel(Label("清除", systemImage: "xmark").labelStyle(.titleAndIcon), active: false, chevron: false)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("清除全部筛选条件")
                        .accessibilityIdentifier("torrent-filter-clear")
                    }
                }
                .padding(.horizontal, Theme.pagePadding)
                .padding(.vertical, 4)
            }
        }
        .scrollIndicators(.hidden)
        // 横滑出页边距：胶囊能滑到屏幕边缘再消失，而不是在 16pt 页边距处被一刀切掉
        .padding(.horizontal, -Theme.pagePadding)
        .accessibilityIdentifier("torrent-conditions")
    }

    /// 排序胶囊：写当前键与方向；菜单分「常规 / 智能」，点当前项翻转升降序（同 Web SortDropdown）
    private var sortChip: some View {
        let smart = TorrentSortKey.smart.filter { model.smartSortKeys.contains($0) || $0 == model.sort.key }
        return Menu {
            Section("常规") {
                ForEach(TorrentSortKey.regular, id: \.self) { key in sortItem(key) }
            }
            if !smart.isEmpty {
                Section("智能") {
                    ForEach(smart, id: \.self) { key in sortItem(key) }
                }
            }
            Section { Text("点当前项可切换升降序") }
        } label: {
            conditionLabel(
                HStack(spacing: 4) {
                    Text(model.sort.key.label)
                    Image(systemName: model.sort.descending ? "arrow.down" : "arrow.up").font(.caption.weight(.semibold))
                },
                active: model.sort != TorrentSort()
            )
        }
        .buttonStyle(.plain)
        .accessibilityLabel("排序：\(model.sort.key.label)\(model.sort.descending ? "降序" : "升序")")
        .accessibilityIdentifier("torrent-sort")
    }

    private func sortItem(_ key: TorrentSortKey) -> some View {
        Button {
            model.sort = model.sort.picking(key)
        } label: {
            if key == model.sort.key {
                Label(key.label, systemImage: model.sort.descending ? "arrow.down" : "arrow.up")
            } else {
                Text(key.label)
            }
        }
    }

    /// 一个筛选维度的胶囊：菜单里每个取值一行（副标题是命中条数），组内多选 = 或；
    /// 选一项菜单即收起（同发现页胶囊，要再加一个就再点开一次）；已启用的末尾多一项「移除此条件」
    private func filterChip(_ dim: TorrentFilterDim) -> some View {
        let selected = model.filters.values(dim)
        let summary = chipSummary(dim, selected: selected)
        return Menu {
            ForEach(dimValues(dim), id: \.value) { facet in
                Toggle(isOn: Binding(
                    get: { model.filters.values(dim).contains(facet.value) },
                    set: { _ in model.filters.toggle(dim, facet.value) }
                )) {
                    Text(TorrentSearchLogic.facetLabel(dim, facet.value, siteName: model.siteName))
                    Text("\(facet.count) 条")
                }
            }
            if !selected.isEmpty {
                Section {
                    Button("移除此条件", systemImage: "xmark", role: .destructive) {
                        model.filters.selected[dim] = nil
                    }
                }
            }
        } label: {
            conditionLabel(Text(summary ?? dim.title), active: summary != nil)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(summary.map { "\(dim.title)：\($0)" } ?? dim.title)
        .accessibilityIdentifier("torrent-filter-\(dim.rawValue)")
    }

    /// 胶囊上的所选值：一两个直接写名字，多了写「首个等 N 个」，胶囊不至于被撑成一长条；未选 nil
    private func chipSummary(_ dim: TorrentFilterDim, selected: Set<String>) -> String? {
        guard !selected.isEmpty else { return nil }
        let labels = selected.sorted().map { TorrentSearchLogic.facetLabel(dim, $0, siteName: model.siteName) }
        return labels.count <= 2 ? labels.joined(separator: "、") : "\(labels[0])等 \(labels.count) 个"
    }

    /// 胶囊外观（与发现页结果头部胶囊同一套玻璃）：启用态加粗、叠一层白色 tint
    private func conditionLabel(_ content: some View, active: Bool, chevron: Bool = true) -> some View {
        HStack(spacing: 4) {
            content.lineLimit(1)
            if chevron {
                Image(systemName: "chevron.down")
                    .font(.caption2.weight(.semibold))
                    .opacity(0.6)
            }
        }
        .font(.subheadline.weight(active ? .semibold : .regular))
        .foregroundStyle(active ? Theme.text : Theme.textMuted)
        .padding(.horizontal, 12)
        .frame(height: 34)
        .glassEffect(active ? .regular.tint(.white.opacity(0.16)).interactive() : .regular.interactive(), in: .capsule)
        .contentShape(.capsule)
    }

    /// 站点维度只列成功返回的站点（顺序同站点状态），其余维度用聚合结果
    private func dimValues(_ dim: TorrentFilterDim) -> [TorrentFacetValue] {
        guard dim == .site else { return model.facets.values(dim) }
        let counts = Dictionary(model.facets.values(.site).map { ($0.value, $0.count) }, uniquingKeysWith: { a, _ in a })
        return model.okSites.map { TorrentFacetValue(value: $0.siteId, count: counts[$0.siteId] ?? 0) }
    }

    /// 站点数已知：按已结束站点比例推进（起步 6%）；未知（还在连接搜索服务）：来回扫光的不定进度（同 Web progress-sweep）
    private var progressBar: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(0.07))
                if model.sites.isEmpty {
                    TorrentProgressSweep(width: proxy.size.width)
                } else {
                    Capsule()
                        .fill(Theme.accent)
                        .frame(width: proxy.size.width * max(0.06, Double(model.settledCount) / Double(model.sites.count)))
                        .animation(.easeOut, value: model.settledCount)
                }
            }
            .clipShape(.capsule)
        }
        .frame(height: 2)
        .accessibilityLabel("搜索进度")
    }

    // MARK: 结果

    @ViewBuilder
    private var results: some View {
        if model.streaming, model.items.isEmpty {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(model.sites.isEmpty ? "正在连接搜索服务…" : "正在搜索 \(model.sites.count) 个站点，结果将实时呈现…")
            }
            .font(.subheadline)
            .foregroundStyle(Theme.textMuted)
            ForEach(0 ..< 5, id: \.self) { i in
                DiscoverSkeletonBlock(cornerRadius: 16).frame(height: 76).opacity(1 - Double(i) * 0.15)
            }
        }
        if model.phase == .error {
            emptyHint("搜索出错", model.fatalError ?? "搜索失败，请稍后重试")
        }
        if !model.sorted.isEmpty {
            switch model.view {
            case .poster:
                posterResults
            case .group where model.entities.count >= 2:
                groupedResults
            default:
                ForEach(model.sorted, id: \.rowKey) { hit in
                    TorrentRow(hit: hit, rawTitles: model.view == .list)
                }
            }
        } else if !model.items.isEmpty {
            emptyHint("没有符合筛选条件的结果", "放宽或清除顶部的筛选条件试试。")
        } else if model.phase == .done {
            emptyHint("没有找到匹配的资源", model.keyword.isEmpty ? "换个分类，或检查已配置站点是否验证通过。" : "换个关键词，或检查已配置站点是否验证通过。")
        }
        if model.phase == .done, model.snapshotAt == nil, !model.items.isEmpty, !model.exhausted {
            Button {
                model.loadMore(api: api)
            } label: {
                Text(model.loadingMore ? "正在加载…" : "加载更多（第 \(model.page + 1) 页）")
                    .font(.subheadline.weight(.semibold))
                    .padding(.horizontal, 8)
            }
            .buttonStyle(.glass)
            .disabled(model.loadingMore)
            .frame(maxWidth: .infinity)
            .padding(.top, 8)
            .accessibilityIdentifier("torrent-load-more")
        }
        if model.phase == .done, model.exhausted, !model.items.isEmpty {
            Text("已加载全部 \(model.page) 页结果")
                .font(.caption).foregroundStyle(Theme.textFaint)
                .frame(maxWidth: .infinity)
        }
    }

    private func emptyHint(_ title: String, _ hint: String) -> some View {
        VStack(spacing: 6) {
            Text(title).font(.headline).foregroundStyle(.white)
            Text(hint).font(.subheadline).foregroundStyle(Theme.textMuted).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity, minHeight: 200)
        .discoverContainer("torrent-empty")
    }

    @ViewBuilder
    private var groupedResults: some View {
        ForEach(TorrentSearchLogic.buckets(model.sorted), id: \.key) { bucket in
            TorrentGroupSection(key: bucket.key, rows: bucket.rows, entity: model.entities[bucket.key])
        }
    }

    @ViewBuilder
    private var posterResults: some View {
        let withPoster = model.sorted.filter { $0.posterUrl != nil }
        let withoutPoster = model.sorted.filter { $0.posterUrl == nil }
        if !withPoster.isEmpty {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 12, alignment: .top)], spacing: 12) {
                ForEach(withPoster, id: \.rowKey) { hit in
                    TorrentPosterCard(hit: hit)
                }
            }
        }
        if !withoutPoster.isEmpty {
            if !withPoster.isEmpty {
                Text("以下 \(withoutPoster.count) 条结果没有海报，按列表展示")
                    .font(.caption).foregroundStyle(Theme.textMuted)
                    .padding(.top, 8)
            }
            ForEach(withoutPoster, id: \.rowKey) { hit in
                TorrentRow(hit: hit)
            }
        }
    }
}

extension API.TorrentHit {
    /// 结果行的稳定标识（站点 + 种子 ID）
    var rowKey: String { "\(siteId):\(torrentId)" }
}

/// `.sheet(item:)` 用的种子包装（不给生成模型追加协议一致性，避免与其它模块冲突）
struct TorrentHitItem: Identifiable {
    let hit: API.TorrentHit
    /// 从图览卡片打开：操作面板才给「浏览图片」（同 Web：列表行不传 onViewImages）
    var fromGallery = false
    var id: String { hit.rowKey }

    /// 有站点详情页或下载地址才有可做的操作；两者都没有时整行/文字区不可点（同 Web）
    static func hasActions(_ hit: API.TorrentHit) -> Bool {
        hit.detailUrl?.isEmpty == false || hit.downloadUrl?.isEmpty == false
    }
}

// MARK: - 作品分组

/// 一个作品分组：组头（片名中英文 + 年份/类型/题材 + 版本数、最高分辨率、全集包、免费数），
/// 可折叠；组内超过 4 条时余量收起，按需展开。
private struct TorrentGroupSection: View {
    let key: String
    let rows: [API.TorrentHit]
    let entity: TorrentEntity?
    @State private var collapsed = false
    @State private var showAll = false

    private static let cap = 4

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                withAnimation(.snappy) { collapsed.toggle() }
            } label: {
                header
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("torrent-group")
            if !collapsed {
                let shown = showAll || rows.count <= Self.cap ? rows : Array(rows.prefix(Self.cap))
                ForEach(shown, id: \.rowKey) { hit in
                    TorrentRow(hit: hit, grouped: true)
                        .padding(.leading, 12)
                }
                if !showAll, rows.count > Self.cap {
                    Button("展开其余 \(rows.count - Self.cap) 个版本 ▾") { withAnimation { showAll = true } }
                        .font(.caption)
                        .foregroundStyle(Theme.textMuted)
                        .padding(.leading, 12)
                }
            }
        }
        .padding(.top, 8)
    }

    private var header: some View {
        let free = rows.filter { $0.free || $0.downloadVolumeFactor == 0 }.count
        let topRes = TorrentSearchLogic.maxResolution(rows)
        let hasPack = rows.contains { $0.attrs?.complete == true }
        let info: String = {
            guard let entity else { return "按原始名展示" }
            return [
                entity.year.map(String.init),
                entity.mediaType.flatMap { TorrentSearchLogic.mediaTypeLabels[$0] },
                entity.contentType.flatMap { TorrentSearchLogic.contentTypeLabels[$0] },
            ].compactMap { $0 }.joined(separator: " · ")
        }()
        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: "chevron.right")
                    .font(.caption2)
                    .rotationEffect(.degrees(collapsed ? 0 : 90))
                    .foregroundStyle(Theme.textFaint)
                Text(entity.map { $0.nameZh ?? $0.nameEn ?? "" } ?? "未识别")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(Theme.text)
                    .lineLimit(1)
                if let entity, entity.nameZh != nil, let en = entity.nameEn {
                    Text(en).font(.subheadline).foregroundStyle(Theme.textMuted).lineLimit(1)
                }
                if !info.isEmpty {
                    Text(info).font(.subheadline).foregroundStyle(Theme.textFaint).lineLimit(1)
                }
            }
            HStack(spacing: 6) {
                DiscoverTag(text: "\(rows.count) 个版本")
                if let topRes { DiscoverTag(text: "最高 \(topRes)", foreground: Theme.accent2) }
                if hasPack { DiscoverTag(text: "全集包", foreground: Color(red: 0.6, green: 0.78, blue: 1), background: Theme.info.opacity(0.18)) }
                if free > 0 { DiscoverTag(text: "\(free) 个免费", foreground: Color(red: 0.47, green: 0.82, blue: 0.58), background: Theme.success.opacity(0.15), weight: .semibold) }
            }
            .padding(.leading, 16)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(.rect)
    }
}

// MARK: - 单条种子

/// 下载 / 投递按钮的状态（终态不可再点，error 可重试）
enum TorrentSubmitState: Hashable {
    case idle, submitting, done, exists, error

    var downloadLabel: String {
        switch self {
        case .idle: "下载"
        case .submitting: "提交中…"
        case .done: "已提交"
        case .exists: "已在下载器"
        case .error: "失败·重试"
        }
    }

    var grabLabel: String {
        switch self {
        case .idle: "投给订阅"
        case .submitting: "投递中…"
        case .done, .exists: "已投递"
        case .error: "失败·重试"
        }
    }
}

/// 列表 / 分组里的一条种子：解析片名（或原始名）、副标题/站点分类、徽标（站点、全集、促销、属性）、
/// 体积·做种/下载·发布时间；点按打开资源操作面板（没有详情页与下载地址时不可点）。
struct TorrentRow: View {
    let hit: API.TorrentHit
    var grouped = false
    /// 列表视图直接展示站点原始种子名
    var rawTitles = false
    @Environment(TorrentActionsState.self) private var actions

    var body: some View {
        let name = rawTitles ? nil : TorrentSearchLogic.parsedName(hit)
        let specRow = grouped && name != nil
        Button {
            actions.sheetHit = TorrentHitItem(hit: hit)
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                if specRow, let attrs = hit.attrs {
                    Text(TorrentSearchLogic.specSummary(attrs) ?? hit.title)
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Theme.text)
                        .lineLimit(2)
                } else if let name {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(name.primary).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text).lineLimit(1)
                        if let secondary = name.secondary {
                            Text(secondary).font(.caption).foregroundStyle(Theme.textMuted).lineLimit(1)
                        }
                        if let year = hit.attrs?.year {
                            Text(String(year)).font(.caption).foregroundStyle(Theme.textFaint)
                        }
                    }
                } else {
                    Text(hit.title).font(.subheadline.weight(.medium)).foregroundStyle(Theme.text).lineLimit(2)
                }
                if (name == nil && !hit.subtitle.isEmpty) || hit.siteCategoryName != nil {
                    HStack(spacing: 6) {
                        if name == nil, !hit.subtitle.isEmpty {
                            Text(hit.subtitle).lineLimit(1)
                        }
                        if let category = hit.siteCategoryName {
                            Text(category).foregroundStyle(Theme.textFaint)
                        }
                    }
                    .font(.caption)
                    .foregroundStyle(Theme.textMuted)
                }
                DiscoverFlowLayout(spacing: 5, lineSpacing: 5) {
                    DiscoverTag(text: hit.siteName, foreground: Theme.accent2)
                    if let complete = TorrentSearchLogic.completeLabel(hit.attrs) {
                        DiscoverTag(text: complete, foreground: Color(red: 0.6, green: 0.78, blue: 1), background: Theme.info.opacity(0.18), weight: .semibold)
                    }
                    TorrentPromoBadges(hit: hit)
                    if let attrs = hit.attrs, !specRow {
                        TorrentAttrBadges(attrs: attrs)
                    }
                }
                metrics
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(red: 16 / 255, green: 18 / 255, blue: 25 / 255).opacity(0.82), in: .rect(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.white.opacity(0.06)))
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .allowsHitTesting(TorrentHitItem.hasActions(hit))
        .accessibilityIdentifier("torrent-row")
        .accessibilityLabel(TorrentHitItem.hasActions(hit) ? "打开「\(hit.title)」的资源操作" : hit.title)
    }

    private var metrics: some View {
        HStack(spacing: 10) {
            let size = TorrentSearchLogic.sizeText(hit)
            if !size.isEmpty { Text(size) }
            HStack(spacing: 2) {
                Image(systemName: "arrow.up").font(.system(size: 9, weight: .bold))
                Text(verbatim: "\(hit.seeders)")
            }
            // 同 Web seederTone：0 红、<5 黄、≥100 加粗亮绿、其余绿
            .fontWeight(hit.seeders >= 100 ? .semibold : nil)
            .foregroundStyle(seederTone(hit.seeders))
            HStack(spacing: 2) {
                Image(systemName: "arrow.down").font(.system(size: 9, weight: .bold))
                Text(verbatim: "\(hit.leechers)")
            }
            .foregroundStyle(Theme.textFaint)
            if hit.snatched > 0 { Text(verbatim: "完成 \(hit.snatched)") }
            Spacer(minLength: 0)
            if let time = hit.uploadTime { Text(SubsFormat.relative(time)) }
        }
        .font(.caption)
        .monospacedDigit()
        .foregroundStyle(Theme.textMuted)
    }

    private func seederTone(_ n: Int) -> Color {
        if n == 0 { return Color(red: 1, green: 0.6, blue: 0.6) }
        if n < 5 { return Theme.warning }
        if n >= 100 { return Theme.success }
        return Color(red: 0.47, green: 0.82, blue: 0.58)
    }
}

/// 不定进度的扫光条：40% 宽的亮条从左侧外滑到右侧外，1.1 秒一轮（同 Web `search-progress-sweep`）
private struct TorrentProgressSweep: View {
    let width: CGFloat
    @State private var sweeping = false

    var body: some View {
        Capsule()
            .fill(Theme.accent)
            .frame(width: width * 0.4)
            .offset(x: sweeping ? width : -width * 0.4)
            .onAppear {
                withAnimation(.easeInOut(duration: 1.1).repeatForever(autoreverses: false)) { sweeping = true }
            }
    }
}

/// 促销角标：免费 / 折扣 / 上传倍率 / H&R
struct TorrentPromoBadges: View {
    let hit: API.TorrentHit
    var solid = false

    var body: some View {
        ForEach(TorrentSearchLogic.promos(hit), id: \.self) { promo in
            switch promo {
            case .free:
                DiscoverTag(text: "免费", foreground: solid ? Color(red: 0.02, green: 0.18, blue: 0.09) : Color(red: 0.47, green: 0.82, blue: 0.58), background: Theme.success.opacity(solid ? 0.9 : 0.15), weight: .semibold)
            case let .discount(text):
                DiscoverTag(text: text, foreground: solid ? .white : Color(red: 0.6, green: 0.78, blue: 1), background: Theme.info.opacity(solid ? 0.85 : 0.15), weight: .semibold)
            case let .upload(text):
                DiscoverTag(text: text, foreground: solid ? .white : Color(red: 0.78, green: 0.65, blue: 1), background: Color(red: 0.78, green: 0.57, blue: 1).opacity(solid ? 0.85 : 0.15), weight: .semibold)
            case .hitAndRun:
                DiscoverTag(text: "H&R", foreground: solid ? Color(red: 0.23, green: 0.15, blue: 0) : Theme.warning, background: Color(red: 0.96, green: 0.62, blue: 0.04).opacity(solid ? 0.9 : 0.15), weight: .semibold)
            }
        }
    }
}

/// 属性徽标：最多 4 个，其余折叠成 +N
struct TorrentAttrBadges: View {
    let attrs: API.TorrentAttrs

    var body: some View {
        let chips = TorrentSearchLogic.attrBadges(attrs)
        ForEach(Array(chips.prefix(4).enumerated()), id: \.offset) { _, chip in
            DiscoverTag(text: chip.text, foreground: color(chip.tone))
        }
        if chips.count > 4 {
            DiscoverTag(text: "+\(chips.count - 4)", foreground: Theme.textFaint)
                .accessibilityLabel(chips.dropFirst(4).map(\.text).joined(separator: " · "))
        }
    }

    private func color(_ tone: TorrentSearchLogic.AttrTone) -> Color {
        switch tone {
        case .plain: Theme.textMuted
        case .season: Theme.accent2
        case .content: Color(red: 0.94, green: 0.71, blue: 0.85)
        case .remux: Color(red: 0.6, green: 0.78, blue: 1)
        case .hdr: Color(red: 0.78, green: 0.65, blue: 1)
        case .subtitle: Color(red: 0.49, green: 0.89, blue: 0.72)
        case .audio: Color(red: 1, green: 0.82, blue: 0.54)
        case .group: Theme.accent
        case .platform: Color(red: 0.56, green: 0.85, blue: 1)
        }
    }
}

/// 图览模式的海报卡：海报点按直接进看图灯箱（与 Web 同一手感），文字区点按打开资源操作面板
struct TorrentPosterCard: View {
    let hit: API.TorrentHit
    @Environment(TorrentActionsState.self) private var actions
    @Environment(\.api) private var api

    var body: some View {
        let slides = actions.slides(for: hit, api: api)
        VStack(alignment: .leading, spacing: 0) {
            Button {
                actions.openImages(hit, api: api)
            } label: {
                Color.clear
                    .aspectRatio(2 / 3, contentMode: .fit)
                    .overlay {
                        ZStack {
                            LinearGradient(colors: [.white.opacity(0.05), .black.opacity(0.4)], startPoint: .top, endPoint: .bottom)
                            Text(hit.siteName).font(.caption).foregroundStyle(Theme.textFaint)
                            RemoteImage(url: api.image(hit.posterUrl, .galleryTile), placeholderSymbol: "photo")
                        }
                    }
                    .overlay(alignment: .topLeading) {
                        VStack(alignment: .leading, spacing: 4) { TorrentPromoBadges(hit: hit, solid: true) }.padding(6)
                    }
                    .overlay(alignment: .topTrailing) {
                        HStack(spacing: 3) {
                            Image(systemName: "photo").font(.system(size: 9))
                            Text("\(slides.count)")
                        }
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 5).padding(.vertical, 2)
                        .background(.black.opacity(0.6), in: .capsule)
                        .padding(6)
                    }
                    .overlay(alignment: .bottomLeading) {
                        if let chip = TorrentSearchLogic.seasonEpisodeChip(hit.attrs) {
                            Text(chip.text)
                                .font(.caption2.weight(.semibold))
                                .foregroundStyle(chip.pack ? .white : Color(red: 0.6, green: 0.78, blue: 1))
                                .padding(.horizontal, 6).padding(.vertical, 3)
                                .background(chip.pack ? AnyShapeStyle(LinearGradient(colors: [Color(red: 0.23, green: 0.51, blue: 0.96), Color(red: 0.55, green: 0.36, blue: 0.96)], startPoint: .leading, endPoint: .trailing)) : AnyShapeStyle(Color.black.opacity(0.7)), in: .rect(cornerRadius: 6))
                                .padding(6)
                        }
                    }
                    .clipShape(.rect(cornerRadius: 12))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("浏览「\(hit.title)」的 \(slides.count) 张图片")

            Button {
                actions.sheetHit = TorrentHitItem(hit: hit, fromGallery: true)
            } label: {
                VStack(alignment: .leading, spacing: 2) {
                    let name = TorrentSearchLogic.parsedName(hit)
                    HStack(spacing: 4) {
                        Text(name?.primary ?? hit.title).font(.subheadline.weight(.semibold)).foregroundStyle(name == nil ? Theme.textMuted : Theme.text).lineLimit(1)
                        if name != nil, let year = hit.attrs?.year { Text(String(year)).font(.caption).foregroundStyle(Theme.textMuted) }
                    }
                    let spec = [hit.attrs?.resolution, TorrentSearchLogic.sizeText(hit)].compactMap { $0 }.filter { !$0.isEmpty }
                    if !spec.isEmpty {
                        Text(spec.joined(separator: " · ")).font(.caption).foregroundStyle(Theme.textMuted).lineLimit(1)
                    }
                    HStack(spacing: 4) {
                        Text(hit.siteName + (hit.uploadTime.map { " · \(SubsFormat.relative($0))" } ?? "")).lineLimit(1)
                        Spacer(minLength: 0)
                        Text(verbatim: "↑\(hit.seeders)").foregroundStyle(Color(red: 0.65, green: 0.85, blue: 0.71))
                        Text(verbatim: "↓\(hit.leechers)").foregroundStyle(Color(red: 0.88, green: 0.76, blue: 0.62))
                    }
                    .font(.caption2)
                    .monospacedDigit()
                    .foregroundStyle(Theme.textFaint)
                }
                .padding(.horizontal, 4)
                .padding(.top, 6)
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .allowsHitTesting(TorrentHitItem.hasActions(hit))
            .accessibilityLabel("打开「\(hit.title)」的资源操作")
        }
        .accessibilityIdentifier("torrent-poster")
    }
}

/// 顶栏右上角的视图切换（同「文件」「照片」的显示选项）：玻璃图标键画的是当前视图，
/// 点开是系统菜单里的三选一——分组（按作品聚合）/ 列表（原始种子名）/ 图览（海报墙）
struct TorrentViewModeMenu: View {
    @Bindable var model: TorrentSearchModel

    var body: some View {
        Menu {
            Picker("显示方式", selection: $model.view) {
                ForEach(TorrentResultView.allCases, id: \.self) { view in
                    Label(view.label, systemImage: view.systemImage).tag(view)
                }
            }
            .pickerStyle(.inline)
        } label: {
            Image(systemName: model.view.systemImage)
        }
        .accessibilityLabel("显示方式：\(model.view.label)")
        .accessibilityIdentifier("torrent-view-menu")
    }
}
