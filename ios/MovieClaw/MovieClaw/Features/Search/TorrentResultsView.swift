import SwiftUI

/// 「站点资源」垂直（对应 Web `components/search-results.tsx` 的界面部分）。
///
/// 自上而下：手动选种横幅（for_sub）→ 状态行（关键词 / 范围 / 计数 / 快照 / 站点状态）→
/// 工具栏（排序、前三个分辨率、年份/季/压制组下拉、视图切换、筛选）→ 已应用条件回显 → 进度条 → 结果。
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
    @Environment(\.openURL) private var openURL

    @State private var actions = TorrentActionsState()
    @State private var showsFilter = false
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
                    toolbar
                    appliedChips
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
        .sheet(isPresented: $showsFilter) {
            TorrentFilterSheet(model: model)
        }
        .sheet(isPresented: $showsSites) {
            SiteStatusSheet(model: model, canRetry: model.snapshotAt == nil)
        }
        .sheet(item: $actions.sheetHit) { item in
            TorrentActionsSheet(hit: item.hit, actions: actions, grabTarget: grabTarget)
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
        .fullScreenCover(item: $actions.lightbox) { DiscoverLightbox(content: $0) }
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
        .accessibilityIdentifier("grab-banner")
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(model.keyword.isEmpty ? "最新资源" : "“\(model.keyword)”")
                    .font(.title2.weight(.semibold))
                    .foregroundStyle(.white)
                    .lineLimit(1)
                if let label = model.scope.label {
                    DiscoverTag(text: label, foreground: Theme.accent, background: .black.opacity(0.3))
                }
                Spacer(minLength: 0)
                if !model.sites.isEmpty {
                    siteSummaryButton
                }
            }
            HStack(spacing: 8) {
                switch model.phase {
                case .streaming, .connecting:
                    Circle().fill(Theme.accent).frame(width: 6, height: 6)
                    Text("已找到 \(model.items.count) 条")
                case .done:
                    Text(model.filters.isActive ? "筛选后 \(model.filtered.count) / 共 \(model.items.count) 条" : "共 \(model.items.count) 条结果")
                default:
                    EmptyView()
                }
                Spacer(minLength: 0)
                if let snapshotAt = model.snapshotAt, model.phase == .done {
                    Label("\(Formatters.relative(snapshotAt))的快照", systemImage: "clock")
                        .font(.caption)
                        .foregroundStyle(Theme.textMuted)
                    if let onResearch {
                        Button("重新搜索", action: onResearch)
                            .font(.caption.weight(.semibold))
                            .accessibilityIdentifier("torrent-research")
                    }
                }
            }
            .font(.subheadline)
            .monospacedDigit()
            .foregroundStyle(Theme.textMuted)
            .accessibilityIdentifier("torrent-status")
        }
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
            .padding(.vertical, 5)
            .background(.black.opacity(0.3), in: .capsule)
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("torrent-sites")
    }

    // MARK: 工具栏

    private var toolbar: some View {
        let facets = model.facets
        return ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                sortMenu
                ForEach(facets.values(.resolution).prefix(3), id: \.value) { facet in
                    DiscoverChip(label: facet.value, count: facet.count, active: model.filters.values(.resolution).contains(facet.value)) {
                        model.filters.toggle(.resolution, facet.value)
                    }
                }
                ForEach([TorrentFilterDim.year, .season, .group], id: \.self) { dim in
                    if facets.values(dim).count >= 2 {
                        facetMenu(dim, facets.values(dim))
                    }
                }
                viewSwitcher
                Button {
                    showsFilter = true
                } label: {
                    HStack(spacing: 4) {
                        Text("筛选")
                        if model.filters.sheetCount > 0 {
                            Text("\(model.filters.sheetCount)")
                                .font(.caption2.bold())
                                .padding(.horizontal, 5)
                                .background(Theme.accent.opacity(0.25), in: .capsule)
                        }
                    }
                    .chipLabel(active: model.filters.sheetCount > 0)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("torrent-filter")
            }
            .padding(.vertical, 2)
        }
        .scrollClipDisabled()
    }

    private var sortMenu: some View {
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
            Section { Text("点击当前项可切换升降序") }
        } label: {
            HStack(spacing: 4) {
                Text(model.sort.key.label)
                Image(systemName: model.sort.descending ? "arrow.down" : "arrow.up").font(.caption)
                Image(systemName: "chevron.down").font(.caption2).opacity(0.7)
            }
            .chipLabel(active: true)
        }
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

    private func facetMenu(_ dim: TorrentFilterDim, _ values: [TorrentFacetValue]) -> some View {
        let active = model.filters.values(dim)
        return Menu {
            ForEach(values, id: \.value) { facet in
                Button {
                    model.filters.toggle(dim, facet.value)
                } label: {
                    let label = "\(TorrentSearchLogic.facetLabel(dim, facet.value, siteName: model.siteName))  \(facet.count)"
                    if active.contains(facet.value) { Label(label, systemImage: "checkmark") } else { Text(label) }
                }
            }
        } label: {
            HStack(spacing: 4) {
                Text(dim.title)
                if !active.isEmpty {
                    Text("\(active.count)").font(.caption2.bold()).padding(.horizontal, 5).background(Theme.accent.opacity(0.25), in: .capsule)
                }
                Image(systemName: "chevron.down").font(.caption2).opacity(0.7)
            }
            .chipLabel(active: !active.isEmpty)
        }
        .menuActionDismissBehavior(.disabled)
    }

    private var viewSwitcher: some View {
        HStack(spacing: 2) {
            ForEach(TorrentResultView.allCases, id: \.self) { view in
                Button {
                    model.view = view
                } label: {
                    Image(systemName: view.systemImage)
                        .font(.caption)
                        .frame(width: 30, height: 26)
                        .background(model.view == view ? Color.white.opacity(0.16) : .clear, in: .capsule)
                        .foregroundStyle(model.view == view ? Theme.text : Theme.textMuted)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(view.label)
                .accessibilityIdentifier("torrent-view-\(view.rawValue)")
                .accessibilityAddTraits(model.view == view ? .isSelected : [])
            }
        }
        .padding(2)
        .background(.black.opacity(0.2), in: .capsule)
        .overlay(Capsule().strokeBorder(Theme.line))
    }

    /// 已应用条件回显行：弹层里激活的条件以可摘除 chip 展示
    @ViewBuilder
    private var appliedChips: some View {
        if model.filters.sheetCount > 0 {
            DiscoverFlowLayout(spacing: 6, lineSpacing: 6) {
                Text("生效条件").font(.caption).foregroundStyle(Theme.textFaint)
                ForEach(TorrentFilterDim.sheetDims, id: \.self) { dim in
                    ForEach(model.filters.values(dim).sorted(), id: \.self) { value in
                        Button {
                            model.filters.toggle(dim, value)
                        } label: {
                            HStack(spacing: 4) {
                                Text(TorrentSearchLogic.facetLabel(dim, value, siteName: model.siteName))
                                Image(systemName: "xmark").font(.system(size: 8, weight: .bold)).opacity(0.6)
                            }
                            .font(.caption)
                            .foregroundStyle(Theme.text)
                            .padding(.horizontal, 8).padding(.vertical, 4)
                            .background(Color.white.opacity(0.1), in: .capsule)
                        }
                        .buttonStyle(.plain)
                    }
                }
                Button("清除全部") { model.filters = TorrentFilters() }
                    .font(.caption)
                    .foregroundStyle(Theme.textMuted)
            }
            .accessibilityIdentifier("torrent-applied")
        }
    }

    private var progressBar: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(0.07))
                Capsule()
                    .fill(Theme.accent)
                    .frame(width: proxy.size.width * (model.sites.isEmpty ? 0.4 : max(0.06, Double(model.settledCount) / Double(model.sites.count))))
                    .animation(.easeOut, value: model.settledCount)
            }
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
        .accessibilityIdentifier("torrent-empty")
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
    var id: String { hit.rowKey }
}

private extension View {
    /// 工具栏胶囊外观
    func chipLabel(active: Bool) -> some View {
        font(.subheadline.weight(active ? .medium : .regular))
            .foregroundStyle(active ? Theme.text : Theme.textMuted)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(active ? Color.white.opacity(0.14) : Color.white.opacity(0.035), in: .capsule)
            .overlay(Capsule().strokeBorder(active ? Color.white.opacity(0.2) : Color.white.opacity(0.08)))
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
/// 体积·做种/下载·发布时间；点按打开资源操作面板。
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
        .accessibilityIdentifier("torrent-row")
        .accessibilityLabel("打开「\(hit.title)」的资源操作")
    }

    private var metrics: some View {
        HStack(spacing: 10) {
            let size = TorrentSearchLogic.sizeText(hit)
            if !size.isEmpty { Text(size) }
            HStack(spacing: 2) {
                Image(systemName: "arrow.up").font(.system(size: 9, weight: .bold))
                Text("\(hit.seeders)")
            }
            .foregroundStyle(seederTone(hit.seeders))
            HStack(spacing: 2) {
                Image(systemName: "arrow.down").font(.system(size: 9, weight: .bold))
                Text("\(hit.leechers)")
            }
            .foregroundStyle(Theme.textFaint)
            if hit.snatched > 0 { Text("完成 \(hit.snatched)") }
            Spacer(minLength: 0)
            if let time = hit.uploadTime { Text(Formatters.relative(time)) }
        }
        .font(.caption)
        .monospacedDigit()
        .foregroundStyle(Theme.textMuted)
    }

    private func seederTone(_ n: Int) -> Color {
        if n == 0 { return Color(red: 1, green: 0.6, blue: 0.6) }
        if n < 5 { return Theme.warning }
        return Color(red: 0.47, green: 0.82, blue: 0.58)
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
                actions.sheetHit = TorrentHitItem(hit: hit)
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
                        Text(hit.siteName + (hit.uploadTime.map { " · \(Formatters.relative($0))" } ?? "")).lineLimit(1)
                        Spacer(minLength: 0)
                        Text("↑\(hit.seeders)").foregroundStyle(Color(red: 0.65, green: 0.85, blue: 0.71))
                        Text("↓\(hit.leechers)").foregroundStyle(Color(red: 0.88, green: 0.76, blue: 0.62))
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
            .accessibilityLabel("打开「\(hit.title)」的资源操作")
        }
        .accessibilityIdentifier("torrent-poster")
    }
}
