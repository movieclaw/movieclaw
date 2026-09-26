import SwiftUI

/// 单库页（Web `library-detail-view.tsx`，路由 `/library/{id}`）。
///
/// 结构：库头（名称、默认/仅管理徽标、统计、最近扫描成绩单、健康状态胶囊、元数据刷新面板）
/// → 作品 / 合集两个视图 → 作品视图里是筛选栏 + 排序 + 墙。墙有三种形态：
/// - 海报墙：窗口式分页（`LibraryWallPager`，每页 60），按标题排序时右侧 A–Z 跳转条；
///   其他库（非刮削）按时间排、竖版与横版分两区；识别不出的文件单独一区「未识别」；
/// - 图床浏览：`LibraryGalleryWall`（⋯ 菜单切换，按作品分组与密度同在菜单里）；
/// - 照片库：`PhotoWallView`（按月分组、月份跳转、灯箱）。
///
/// 管理员 ⋯ 菜单：待处理（抽屉）/ 扫描与停止 / 整理文件名 / 刷新元数据与停止 / 生成章节 / 编辑库。
/// 轮询：扫描或整理中 3 秒（结束后再快轮询 12 秒），入库中或刷新元数据中 10 秒，其余 30 秒；
/// 每轮连照片墙一起整窗对账。元数据刷新状态单独读 progress 接口（同 Web：进页探一次、刷新中每 2 秒）。
/// 「回到上次位置」三种形态共用一条记录（都按作品计）；挂后台 30 分钟以上再回来算重新进入，重新询问。
/// 排序偏好全站共用一个键（在哪个库选了「按评分」，换个库还是按评分），同 Web。
struct LibraryDetailView: View {
    let libraryId: Int
    /// 进来时落在哪个视图（Web `?view=collections`）
    var initialView: WallView = .items
    /// 数据到齐后自动打开待处理抽屉（Web `?pending=1`）
    var openPending = false

    init(libraryId: Int, initialView: WallView = .items, openPending: Bool = false) {
        self.libraryId = libraryId
        self.initialView = initialView
        self.openPending = openPending
        _view = State(initialValue: initialView)
    }

    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Feedback.self) private var feedback
    @Environment(Router.self) private var router

    @State private var libraries: [API.LibraryView]?
    @State private var loadFailed = false
    @State private var collections: [API.CollectionView] = []
    @State private var provisional: [API.LibraryItemView] = []
    @State private var index: [API.LibraryIndexEntryView] = []
    @State private var missing: [API.MissingItemView] = []
    @State private var unidentified: [API.UnidentifiedGroupView] = []
    @State private var review: [API.ReviewGroupView] = []
    @State private var ignored: [API.UnidentifiedGroupView] = []
    @State private var activeJobs: [API.JobView] = []
    @State private var pager = LibraryWallPager<API.LibraryItemView>(pageSize: 60)
    @State private var filter = LibraryFilter()
    @State private var sort = WallSortState.load(Self.sortStorageKey, default: WallSortState(sort: "default"), allowed: Self.allSorts)
    @State private var view: WallView = .items
    @State private var showHiddenCollections = false
    @State private var gallery = GalleryPrefs.shared
    @State private var busyUntil = Date.distantPast
    @State private var notice: String?
    @State private var issueTab: IssueSheet?
    @State private var organizing = false
    @State private var editing = false
    @State private var savingCollection = false
    @State private var askingChapters = false
    @State private var recallOffset: Int?
    @State private var recallChecked = false
    @State private var firstVisible = 0
    /// 元数据刷新的实时状态（progress 接口；仅管理员）
    @State private var metaRefresh: API.MetadataRefreshView?
    /// 每轮轮询 / 下拉刷新递增：照片墙据此整窗重拉
    @State private var wallEpoch = 0
    /// 图廊窗口起点 / 照片墙跳转（「回到上次位置」在这两种形态下跳这里）
    @State private var galleryStart = 0
    @State private var photoJump: PhotoWallJump?
    @State private var pendingOpened = false
    /// 换排序/筛选触发的整墙重载（新的一次作废旧的）
    @State private var wallTask: Task<Void, Never>?
    /// 页面滚动代理：久别回归复位时滚回墙顶
    @State private var scrollProxy: ScrollViewProxy?

    enum WallView: String { case items, collections }

    private struct IssueSheet: Identifiable {
        var tab: String
        var id: String { tab }
    }

    private static let sortStorageKey = "movieclaw.library.wall-sort"
    private static let allSorts = ["default", "title", "added_at", "release_date", "rating", "runtime", "size", "last_played"]

    // MARK: 派生

    private var library: API.LibraryView? { libraries?.first { $0.id == libraryId } }
    private var busy: Bool { library.map { $0.scanning || $0.organizing } ?? false }
    /// 元数据刷新中：读 progress 接口的状态（同 Web）——库列表里的状态只看进程内，排队中的持久化作业看不到
    private var refreshingMeta: Bool { metaRefresh?.refreshing == true }
    private var importing: Int { busy ? 0 : (library?.lastScan?.deferred ?? 0) }
    private var timeline: Bool { library.map { !$0.capabilities.scraped } ?? false }
    private var photoWall: Bool { library.map { !$0.capabilities.playable } ?? false }
    private var probing: Bool { library?.scanning == true && library?.scanProgress?.phase == "probing" }
    private var galleryOn: Bool { gallery.galleryMode && library?.capabilities.playable == true }
    private var recentFirst: Bool { sort.sort == "added_at" && !probing }

    /// 实际请求的排序档（补探阶段强制按补探序；照片库只认「最近添加」或默认）
    private var effectiveSort: String {
        if probing { return "probing" }
        if sort.sort != "default", !(photoWall && sort.sort != "added_at") { return sort.sort }
        return timeline ? "release_date" : "title"
    }

    private var order: String? {
        guard effectiveSort != "probing", !photoWall else { return nil }
        return WallSortDirections.of(effectiveSort)?.orderParam(reversed: sort.reversed)
    }

    private var sortOptions: [WallSortMenu.Option] {
        var options: [WallSortMenu.Option] = [
            .init(value: "default", label: timeline ? "按时间" : "按标题", direction: WallSortDirections.of(timeline ? "release_date" : "title")),
            .init(value: "added_at", label: "最近添加", direction: WallSortDirections.of("added_at")),
        ]
        if !timeline { options.append(.init(value: "release_date", label: "按上映时间", direction: WallSortDirections.of("release_date"))) }
        options += [
            .init(value: "rating", label: "按评分", direction: WallSortDirections.of("rating")),
            .init(value: "runtime", label: "按片长", direction: WallSortDirections.of("runtime")),
            .init(value: "size", label: "按体积", direction: WallSortDirections.of("size")),
            .init(value: "last_played", label: "最近观看", direction: WallSortDirections.of("last_played")),
        ]
        return options
    }

    /// 墙的取数口径（排序 + 方向 + 筛选），变化即整面墙从头重载
    private var wallKey: String { "\(effectiveSort)|\(order ?? "")|\(filter.key)" }

    private var recallScope: String { filter.key.isEmpty ? "library:\(libraryId)" : "library:\(libraryId):\(filter.key)" }
    private var recallView: String {
        let defaultSort = effectiveSort == "title" || effectiveSort == "probing" || (timeline && effectiveSort == "release_date")
        return (timeline ? "wall:time" : "wall:title") + (recentFirst ? ":added" : defaultSort ? "" : ":\(effectiveSort)") + (order != nil ? ":rev" : "")
    }

    private var pendingCount: Int { missing.count + unidentified.count + review.count }
    private var pendingTab: String {
        !missing.isEmpty ? "missing" : !unidentified.isEmpty ? "unidentified" : !review.isEmpty ? "review" : !ignored.isEmpty ? "ignored" : "unidentified"
    }

    // MARK: 视图

    var body: some View {
        Group {
            if loadFailed, libraries == nil {
                ErrorState(title: "媒体库加载失败", message: "与后端通信失败") { await reload() }
            } else if libraries == nil {
                VStack(spacing: 10) {
                    ProgressView()
                    Text("正在加载媒体库…").font(.subheadline).foregroundStyle(Theme.textMuted)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let library {
                page(library)
            } else {
                EmptyState(systemImage: "questionmark.folder", title: "这个媒体库不存在（可能已被删除）", actionTitle: "返回媒体库") { router.pop() }
            }
        }
        .appBackground()
        .navigationTitle(library?.name ?? "")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { toolbar }
        .task {
            await reload()
            await probeMetaRefresh()
        }
        .task(id: refreshingMeta) { await pollMetaRefresh() }
        .onWallReentry(scope: recallScope) { resetForReentry() }
        // 首载与换排序分开（同收藏页）：`.task` 每次重新出现都会重跑，把 reset 放在里面会让
        // 从条目详情返回时整面墙清空、跳回墙首（Web 这里是快照恢复 + 整窗对账，2026-09-07 用户反馈过）。
        // 返回时的整窗对账由上面的 reload（pager.refresh）负责
        .task {
            // 还没有墙：首载（或上次首载失败、重新出现时再试）；正在载的那一轮不重复发起
            guard pager.items == nil, wallTask == nil || pager.failed != nil else { return }
            wallTask?.cancel()
            wallTask = Task { await reloadWall() }
        }
        .onChange(of: wallKey) {
            // 换排序 / 筛选：图廊窗口也回墙首（同 Web galleryStart = 0）
            galleryStart = 0
            wallTask?.cancel()
            wallTask = Task { await reloadWall() }
        }
        .onChange(of: sort) { sort.save(Self.sortStorageKey) }
        .task(id: showHiddenCollections) { await reloadCollections() }
        .polling(every: pollInterval) { await reload() }
        .sheet(item: $issueTab) { sheet in
            IssueDrawerView(libraryId: libraryId, initialTab: sheet.tab, pollInterval: pollInterval) { Task { await reload() } }
                .sheetFeedback()
        }
        .sheet(isPresented: $organizing) {
            LibraryOrganizeSheet(libraryId: libraryId) { Task { await reload() } }
                .sheetFeedback()
        }
        .sheet(isPresented: $editing, onDismiss: { Task { await reload() } }) {
            LibraryFormSheet(libraryId: libraryId).sheetFeedback()
        }
        .sheet(isPresented: $savingCollection) {
            SaveAsCollectionSheet(libraryId: libraryId, filter: filter) { _ in Task { await reloadCollections() } }
                .sheetFeedback()
        }
        .alert("为「\(library?.name ?? "")」生成章节？", isPresented: $askingChapters) {
            Button("开始生成") { startChapters(force: false) }
            Button("已有的章节也重新生成") { startChapters(force: true) }
            Button("取消", role: .cancel) {}
        } message: {
            Text(Self.bullets("后台低优先级执行，可在任务中心观察或取消：", [
                "只处理章节图还缺的文件（含上次没抓完、图丢了的），已经齐的不动",
                "每个文件按章节数定位读取若干次，网络挂载的库会有读取流量",
                "不会移动、修改或删除你的视频文件",
            ]) + "\n\n选「已有的章节也重新生成」会按当前章节与合成策略全部重新生成，你手动选定的图不会被覆盖。")
        }
    }

    private var pollInterval: Double {
        if busy || Date.now < busyUntil { return 3 }
        if importing > 0 || refreshingMeta { return 10 }
        return 30
    }

    /// ⋯ 菜单里有没有东西（同 Web hasMenuItems）：普通成员在合集视图下什么都没有，整个按钮不画
    private var hasMenuItems: Bool {
        permissions.canManageLibraries || photoWall || galleryOn || (library?.capabilities.playable == true && view == .items)
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        if let library, !photoWall, !collections.isEmpty {
            ToolbarItem(placement: .principal) {
                Picker("库内视图", selection: $view) {
                    Text("作品").tag(WallView.items)
                    Text("合集").tag(WallView.collections)
                }
                .pickerStyle(.segmented)
                .frame(width: 140)
                .accessibilityIdentifier("library-view-switch")
                .id(library.id)
            }
        }
        if library != nil, hasMenuItems {
            ToolbarItem(placement: .topBarTrailing) { actionsMenu }
        }
    }

    @ViewBuilder
    private func page(_ library: API.LibraryView) -> some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    header(library)
                        .id(LibraryWallRecall.pageTopID)
                    if !library.viewerAccess {
                        hiddenContent
                    } else if view == .collections {
                        LibraryCollectionsGrid(collections: collections, libraryId: libraryId)
                            .padding(.top, 20)
                    } else {
                        wall(library, proxy: proxy)
                    }
                }
                .padding(.bottom, 40)
            }
            .onScrollTargetVisibilityChange(idType: Int.self, threshold: 0.2) { trackVisible($0) }
            .overlay(alignment: .trailing) {
                if effectiveSort == "title", !galleryOn, view == .items, !index.isEmpty, !photoWall {
                    WallIndexBar(index: index, reversed: order != nil, active: activeInitial) { offset in
                        Task {
                            await pager.jump(to: offset)
                            if let first = pager.items?.first { proxy.scrollTo(first.id, anchor: .top) }
                        }
                    }
                }
            }
            .overlay(alignment: .bottom) {
                if let recallOffset {
                    WallRecallPill {
                        self.recallOffset = nil
                        // 按当前形态分派：图廊换窗口、照片墙跳到那张、海报墙换分页窗口
                        if galleryOn {
                            galleryStart = recallOffset
                        } else if photoWall {
                            photoJump = PhotoWallJump(offset: recallOffset)
                        } else {
                            Task {
                                await pager.jump(to: recallOffset)
                                if let first = pager.items?.first { proxy.scrollTo(first.id, anchor: .top) }
                            }
                        }
                    } onDismiss: { self.recallOffset = nil }
                }
            }
            .animation(.snappy, value: recallOffset)
            .refreshable { await reload() }
            .onAppear { scrollProxy = proxy }
        }
    }

    /// 视口第一格所在的首字母档（A–Z 条高亮当前档，同 Web activeWallInitial）
    private var activeInitial: String? {
        index.filter { $0.offset <= firstVisible }.max { $0.offset < $1.offset }?.initial
    }

    // MARK: 库头

    @ViewBuilder
    private func header(_ library: API.LibraryView) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Text(library.name)
                    .font(.title2.weight(.bold))
                    .foregroundStyle(.white)
                    .lineLimit(1)
                if library.isDefault {
                    Text("默认").font(.caption.weight(.semibold)).foregroundStyle(.white.opacity(0.9))
                        .padding(.horizontal, 8).padding(.vertical, 2)
                        .background(.white.opacity(0.12), in: .capsule)
                        .overlay(Capsule().strokeBorder(.white.opacity(0.14)))
                }
                if !library.viewerAccess {
                    Label("仅管理", systemImage: "lock.fill")
                        .font(.caption.weight(.semibold)).foregroundStyle(Theme.warning)
                        .padding(.horizontal, 8).padding(.vertical, 2)
                        .background(Theme.warning.opacity(0.12), in: .capsule)
                }
            }
            Text("\(LibraryKindMeta.label(library.kind))库 · \(library.stats.itemCount) \(library.kind == "photo" ? "张" : "部作品") · \(library.stats.fileCount) 个文件 · \(libraryBytes(library.stats.totalSizeBytes))")
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)
                .lineLimit(1)
                .accessibilityIdentifier("library-stats-line")
            if let scan = library.lastScan, !busy {
                Text(lastScanText(scan))
                    .font(.footnote)
                    .foregroundStyle(.white.opacity(0.45))
            }
            if permissions.canManageLibraries {
                healthChips(library)
            }
            if permissions.canManageLibraries, refreshingMeta, let metaRefresh {
                MetadataRefreshPanel(state: metaRefresh) { stopMetaRefresh() }
            }
            if loadFailed {
                banner("与后端通信失败，正在自动重试；下方显示的是最近一次成功加载的数据", color: Theme.warning)
            }
            if let notice {
                banner(notice, color: Theme.danger)
            }
        }
        .padding(.horizontal, Theme.pagePadding)
        .padding(.top, 4)
    }

    private func lastScanText(_ scan: API.LastScanView) -> String {
        var text = "最近扫描 \(libraryFromNow(scan.finishedAt))\(scan.cancelled ? "（手动停止，未扫完）" : "") · 新入账 \(scan.scanned)（识别 \(scan.identified) / 未识别 \(scan.unidentified)）"
        if scan.retried > 0 { text += " · 重试识别 \(scan.retried) 个待识别文件" }
        if scan.markedMissing > 0 { text += " · 标记丢失 \(scan.markedMissing)" }
        if scan.clearedMissing > 0 { text += " · 清理丢失记录 \(scan.clearedMissing)" }
        if scan.deferred > 0 { text += " · \(scan.deferred) 个写入中暂缓（稍后自动补扫）" }
        if let first = scan.errors.first { text += " · \(first)" }
        return text
    }

    private var libraryJobs: [API.JobView] {
        activeJobs.filter { $0.resources.contains { $0.resourceType == "library" && $0.resourceId == String(libraryId) } }
    }

    @ViewBuilder
    private func healthChips(_ library: API.LibraryView) -> some View {
        let unidentifiedFiles = unidentified.reduce(0) { $0 + $1.fileCount }
        if busy || importing > 0 || !libraryJobs.isEmpty || !missing.isEmpty || !unidentified.isEmpty || !review.isEmpty {
            TrackFlowLayout(spacing: 8, lineSpacing: 8) {
                if busy {
                    let progress = library.scanning ? library.scanProgress : library.organizeProgress
                    chip(color: Theme.info, spinning: true, text: progress.map { p in
                        "\(ScanPhase.label(p.phase))\(p.total > 0 ? " \(p.processed)/\(p.total)" : "") · \(ScanPhase.hint(p.phase))"
                    } ?? "正在处理…")
                }
                if importing > 0 {
                    chip(color: Theme.info, text: "已发现 \(importing) 个新文件 · 写入完成后自动入库")
                }
                if !libraryJobs.isEmpty {
                    chip(color: Theme.info, text: "\(libraryJobs.count) 个后台任务正在处理库内影片")
                }
                if !missing.isEmpty {
                    Button { issueTab = IssueSheet(tab: "missing") } label: {
                        chip(color: .white.opacity(0.6), dot: .white.opacity(0.4), text: "\(missing.count) 个条目缺失")
                    }.buttonStyle(.plain)
                }
                if !unidentified.isEmpty {
                    Button { issueTab = IssueSheet(tab: "unidentified") } label: {
                        chip(color: Theme.warning, text: "\(unidentifiedFiles) 个文件待识别\(unidentified.count > 1 ? " · \(unidentified.count) 组" : "")")
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("chip-unidentified")
                }
                if !review.isEmpty {
                    Button { issueTab = IssueSheet(tab: "review") } label: {
                        chip(color: Theme.warning, text: "\(review.count) 个条目待复核身份")
                    }.buttonStyle(.plain)
                }
            }
            .padding(.top, 2)
        }
    }

    private func chip(color: Color, dot: Color? = nil, spinning: Bool = false, text: String) -> some View {
        HStack(spacing: 6) {
            if spinning {
                ProgressView().controlSize(.mini).tint(color)
            } else {
                Circle().fill(dot ?? color).frame(width: 6, height: 6)
            }
            Text(text).lineLimit(2)
        }
        .font(.footnote.weight(.semibold))
        .foregroundStyle(color)
        .padding(.horizontal, 12).padding(.vertical, 6)
        .background(color.opacity(0.12), in: .capsule)
        .overlay(Capsule().strokeBorder(color.opacity(0.35)))
    }

    private func banner(_ text: String, color: Color) -> some View {
        Text(text)
            .font(.footnote)
            .foregroundStyle(color)
            .padding(.horizontal, 14).padding(.vertical, 8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(color.opacity(0.1), in: .rect(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(color.opacity(0.25)))
            .padding(.top, 4)
    }

    private var hiddenContent: some View {
        VStack(spacing: 12) {
            Image(systemName: "lock.fill").font(.largeTitle).foregroundStyle(.white.opacity(0.28))
            Text("内容已隐藏：你不在这个库的可见范围内。\n设置、扫描与待处理仍可使用；把自己加入可见范围即可浏览。")
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)
                .multilineTextAlignment(.center)
            if permissions.canManageLibraries {
                Button("把我加入可见范围") { editing = true }.buttonStyle(.glass)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 64)
        .padding(.horizontal, Theme.pagePadding)
    }

    // MARK: 墙

    @ViewBuilder
    private func wall(_ library: API.LibraryView, proxy: ScrollViewProxy) -> some View {
        let items = pager.items ?? []
        if pager.items != nil, items.isEmpty, provisional.isEmpty, !filter.isEmpty {
            filterControls
            LibraryFilterEmptyState(libraryId: libraryId, filter: $filter)
                .padding(.top, 12)
        } else if pager.items != nil, items.isEmpty, provisional.isEmpty {
            Text("这个库还没有内容。\n" + (permissions.canManageLibraries
                ? "点右上角菜单里的「扫描库」把已有影片识别入库；订阅内容下载完成后也会自动进来。"
                : "订阅内容下载并入库后会显示在这里。"))
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)
                .multilineTextAlignment(.center)
                .frame(maxWidth: .infinity)
                .padding(.top, 64)
                .padding(.horizontal, Theme.pagePadding)
        } else {
            if !photoWall { filterControls }
            VStack(alignment: .leading, spacing: 0) {
                if galleryOn {
                    LibraryGalleryWall(reloadKey: AnyHashable(wallKey), startOffset: galleryStart, fetch: { [api, libraryId, filter, effectiveSort, order] offset, limit in
                        try await api.libraryGalleryFiltered(libraryId: libraryId, filter: filter, sort: effectiveSort, order: order, limit: limit, offset: offset)
                    }, onOpenItem: { group in
                        router.push(.libraryItem(libraryId: group.libraryId, itemId: group.mediaItemId))
                    }, onFirstVisible: { recordOffset($0, windowStart: galleryStart) })
                } else if photoWall {
                    PhotoWallView(libraryId: libraryId, reloadKey: AnyHashable(wallKey), fetch: { [api, libraryId, filter, effectiveSort, order] offset, limit in
                        try await api.libraryItemsFiltered(libraryId: libraryId, filter: filter, sort: effectiveSort, order: order, limit: limit, offset: offset)
                    }, grouped: !recentFirst, refreshToken: wallEpoch, jumpRequest: photoJump, working: workingLabel,
                    onFirstVisible: { recordOffset($0, windowStart: photoJump?.offset ?? 0) })
                } else if pager.items == nil {
                    ProgressView().frame(maxWidth: .infinity).padding(.top, 40)
                } else {
                    posterWall(library, items: items, proxy: proxy)
                }
                if !galleryOn, !photoWall, !provisional.isEmpty {
                    provisionalSection
                }
            }
            .padding(.top, 16)
            .padding(.trailing, effectiveSort == "title" && !galleryOn && !index.isEmpty ? 14 : 0)
        }
    }

    private var filterControls: some View {
        VStack(alignment: .leading, spacing: 10) {
            WallSortMenu(options: sortOptions, state: $sort)
                .glassEffect(.regular.interactive(), in: .capsule)
                .disabled(probing)
            LibraryFilterBar(
                libraryId: libraryId,
                filter: $filter,
                collections: collections,
                onSaveAsCollection: { savingCollection = true },
                onShowAllCollections: { view = .collections }
            )
        }
        .padding(.horizontal, Theme.pagePadding)
        .padding(.top, 16)
    }

    @ViewBuilder
    private func posterWall(_ library: API.LibraryView, items: [API.LibraryItemView], proxy: ScrollViewProxy) -> some View {
        let showRating = sort.sort == "rating" || filter.ratingGte != nil
        WallLoadPreviousSentinel(start: pager.start) {
            // 接上上一页后把原来的第一格钉回顶部，眼下这一屏不跳
            let anchor = pager.items?.first?.id
            await pager.loadPrevious()
            if let anchor { proxy.scrollTo(anchor, anchor: .top) }
        }
        if timeline {
            let posters = items.filter { $0.primaryAspect < 1 }
            let thumbs = items.filter { $0.primaryAspect >= 1 }
            if !posters.isEmpty, !thumbs.isEmpty {
                sectionTitle("海报")
                grid(posters, wide: false, showRating: showRating)
                sectionTitle("缩略图").padding(.top, 24)
                grid(thumbs, wide: true, showRating: showRating)
            } else {
                grid(items, wide: posters.isEmpty, showRating: showRating)
            }
        } else {
            grid(items, wide: library.capabilities.defaultAspect > 1, showRating: showRating)
        }
        WallLoadMoreFooter(hasMore: pager.hasMore, start: pager.start, loaded: items.count, total: library.stats.itemCount) {
            await pager.loadMore()
        }
    }

    private func sectionTitle(_ text: String) -> some View {
        Text(text).font(.headline).foregroundStyle(.white.opacity(0.85))
            .padding(.horizontal, Theme.pagePadding).padding(.bottom, 12)
    }

    private func grid(_ items: [API.LibraryItemView], wide: Bool, showRating: Bool) -> some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: wide ? 160 : 140), spacing: 12, alignment: .top)], alignment: .leading, spacing: 20) {
            ForEach(items) { item in
                LibraryInventoryCell(item: item, libraryId: libraryId, frameAspect: wide ? 16 / 9 : Theme.posterAspect,
                                     showRating: showRating, working: workingLabel(item))
            }
        }
        .scrollTargetLayout()
        .padding(.horizontal, Theme.pagePadding)
        .accessibilityIdentifier("poster-wall")
    }

    private var provisionalSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .bottom) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("未识别 \(provisional.count)").font(.headline).foregroundStyle(.white.opacity(0.85))
                    Text("按文件名展示，可以直接播放；认领身份后会并入上方的正式条目。")
                        .font(.caption).foregroundStyle(Theme.textMuted)
                }
                Spacer()
                if permissions.canManageLibraries {
                    Button("去待处理认领") { issueTab = IssueSheet(tab: "unidentified") }
                        .font(.footnote.weight(.semibold))
                        .buttonStyle(.glass)
                }
            }
            .padding(.horizontal, Theme.pagePadding)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 160), spacing: 12, alignment: .top)], alignment: .leading, spacing: 20) {
                ForEach(provisional) { item in
                    LibraryInventoryCell(item: item, libraryId: libraryId, frameAspect: 16 / 9,
                                         working: probing && item.probePendingCount > 0 ? "正在读取规格" : nil)
                }
            }
            .padding(.horizontal, Theme.pagePadding)
        }
        .padding(.top, 32)
    }

    /// 这一格正被后台处理的文案：整库刷新阶段 > 后台任务 > 扫描补探
    private func workingLabel(_ item: API.LibraryItemView) -> String? {
        if let phase = metaRefresh?.active.first(where: { $0.mediaItemId == item.mediaItemId })?.phase { return phase }
        if let job = activeJobs.first(where: { $0.resources.contains { $0.resourceType == "media_item" && $0.resourceId == String(item.mediaItemId) } }) {
            return "\(job.status == "blocked" ? "需要处理" : "后台任务") · \(job.progress.message)"
        }
        return probing && item.probePendingCount > 0 ? "正在读取规格" : nil
    }

    // MARK: ⋯ 菜单

    private var actionsMenu: some View {
        let running = library.map { $0.scanning || $0.organizing } == true || refreshingMeta
        return Menu {
            if permissions.canManageLibraries, let library {
                Button("待处理\(pendingCount > 0 ? " \(pendingCount)" : "")") { issueTab = IssueSheet(tab: pendingTab) }
                Divider()
                let stoppable = library.scanning && library.scanProgress?.phase != "reidentifying"
                Button(scanLabel(library, stoppable: stoppable)) { toggleScan(library) }
                    .disabled((busy && !library.scanning) || (library.scanning && !stoppable))
                if library.capabilities.naming {
                    Button(library.organizing ? "整理中…\(percent(library.organizeProgress).map { " \($0)%" } ?? "")" : "整理文件名") {
                        notice = nil
                        organizing = true
                    }
                    .disabled(busy && !library.organizing)
                }
                Button(metaLabel(library)) { toggleMetaRefresh(library) }
                if library.extractChapterImages {
                    Button(chapterJobLabel(library.chapterJob)) { askingChapters = true }
                        .disabled(busy || library.chapterJob != nil)
                }
                Button("编辑库") { editing = true }.disabled(busy)
                Divider()
            }
            if library?.capabilities.playable == true, view == .items || galleryOn {
                Button(galleryOn ? "回到海报墙" : "图床浏览") { gallery.galleryMode.toggle() }
            }
            if view == .collections {
                // 只要 ⋯ 在、处于合集视图就给（同 Web：showHiddenCollections 只看视图，不看身份）
                Button(showHiddenCollections ? "不显示已隐藏的合集" : "显示已隐藏的合集") { showHiddenCollections.toggle() }
            }
            if galleryOn || photoWall {
                GalleryPrefMenuItems(showsGrouping: galleryOn)
            }
        } label: {
            Image(systemName: "ellipsis")
                .overlay(alignment: .topTrailing) {
                    if running { Circle().fill(Theme.info).frame(width: 6, height: 6).offset(x: 6, y: -4) }
                }
        }
        .accessibilityLabel("更多操作")
        .accessibilityIdentifier("library-actions")
    }

    private func percent(_ progress: API.ScanProgressView?) -> Int? {
        guard let progress, progress.total > 0 else { return nil }
        return min(100, Int((Double(progress.processed) / Double(progress.total) * 100).rounded()))
    }

    private func scanLabel(_ library: API.LibraryView, stoppable: Bool) -> String {
        if !library.scanning { return "扫描库" }
        if stoppable { return "停止扫描\(percent(library.scanProgress).map { " \($0)%" } ?? "")" }
        return "\(ScanPhase.label(library.scanProgress?.phase))…"
    }

    private func metaLabel(_ library: API.LibraryView) -> String {
        if refreshingMeta, let meta = metaRefresh {
            return "停止刷新\(meta.total > 0 ? " \(meta.processed)/\(meta.total)" : "")"
        }
        return library.capabilities.scraped ? "刷新元数据" : library.capabilities.playable ? "重新读取 NFO 与封面" : "重新生成封面"
    }

    private func chapterJobLabel(_ job: API.ChapterJobView?) -> String {
        guard let job else { return "生成章节" }
        if job.stopping { return "正在停止生成章节" }
        // cancelling 也算运行中（同 Web chapterJobRunning）
        if !["running", "cancelling"].contains(job.status) { return "生成章节排队中" }
        guard job.total > 0 else { return "正在生成章节" }
        return "正在生成章节 \(min(100, Int((Double(job.processed) / Double(job.total) * 100).rounded())))%"
    }

    static func bullets(_ lead: String, _ items: [String]) -> String {
        ([lead] + items.map { "• \($0)" }).joined(separator: "\n")
    }

    private func toggleScan(_ library: API.LibraryView) {
        notice = nil
        Task {
            do {
                if library.scanning {
                    _ = try await api.libraryScanStop(libraryId: libraryId)
                } else {
                    let ok = await feedback.confirm("扫描「\(library.name)」？", message: Self.bullets("本次扫描会检查库文件夹的最新变化：", [
                        "找出新增的影片文件，自动识别并加入媒体库",
                        "标记已经不在硬盘上的文件（记录保留，文件回来自动恢复）",
                        "为新入库的影片补齐简介、海报等信息",
                        "首次扫描会读取每个文件的画质与音轨信息，文件多时较慢、可随时停止",
                        "不会移动、修改或删除你的任何文件",
                    ]), confirmTitle: "开始扫描")
                    guard ok else { return }
                    _ = try await api.libraryScanStart(libraryId: libraryId)
                }
                await reload()
            } catch {
                notice = error.localizedDescription
            }
        }
    }

    private func toggleMetaRefresh(_ library: API.LibraryView) {
        notice = nil
        Task {
            do {
                if refreshingMeta {
                    _ = try await api.libraryMetadataStopRefresh(libraryId: libraryId)
                    await probeMetaRefresh()
                } else {
                    let caps = library.capabilities
                    let ok: Bool
                    if !caps.scraped, caps.playable {
                        ok = await feedback.confirm("重新读取「\(library.name)」的 NFO 与封面？", message: Self.bullets("本次会为库里的全部视频：", [
                            "重新读取视频旁同名的 NFO 文件，标题、简介、系列等以 NFO 为准",
                            "NFO 里写了系列（<set>）的，按库设置自动归进系列合集",
                            "重新生成封面",
                            "不联网，不会修改或删除你的文件",
                        ]), confirmTitle: "开始读取")
                    } else {
                        ok = await feedback.confirm("刷新「\(library.name)」的元数据？", message: Self.bullets("本次刷新会为库里的全部影片：", [
                            "重新获取最新的简介、评分、演职员和剧集信息",
                            "海报或背景图有更新时重新下载（你手动锁定的不动）",
                            "同步更新影片文件夹里的海报和 NFO 信息文件",
                            "影片较多时需要一段时间，可随时停止",
                            "不会移动、修改或删除你的视频文件",
                        ]), confirmTitle: "开始刷新")
                    }
                    guard ok else { return }
                    _ = try await api.libraryMetadataRefreshLibrary(libraryId: libraryId)
                    // 开始后立刻探一次：作业可能还在排队，库列表里看不到，面板和菜单要马上切过去
                    await probeMetaRefresh()
                }
                await reload()
            } catch {
                notice = error.localizedDescription
            }
        }
    }

    private func startChapters(force: Bool) {
        notice = nil
        Task {
            do {
                _ = try await api.libraryChapterImagesGenerate(libraryId: libraryId, force: force)
                feedback.success(force ? "已开始重新生成章节，可在任务中心查看进度" : "已开始生成章节，可在任务中心查看进度")
                await reload()
            } catch {
                feedback.error(error)
            }
        }
    }

    // MARK: 加载

    private func reload() async {
        let api = self.api
        let id = libraryId
        let manage = permissions.canManageLibraries
        do {
            async let libs = api.libraryList(scope: "all")
            async let prov = try? api.libraryItemsList(libraryId: id, sort: "added_at", limit: 200, identity: "provisional")
            async let miss = manage ? try? api.libraryMissingList(libraryId: id) : []
            async let unknown = manage ? try? api.libraryIdentificationListUnidentifiedFiles(libraryId: id) : []
            async let rev = manage ? try? api.libraryIdentificationListReviewCases(libraryId: id) : []
            async let ign = manage ? try? api.libraryIdentificationListIgnoredFiles(libraryId: id) : []
            async let jobs = manage ? try? api.jobsList(activeOnly: true, limit: 100).items : []
            let l = try await libs
            if l != libraries { libraries = l }
            loadFailed = false
            // 整库元数据刷新可能是别处发起的（首页卡片、其他设备、定时任务）：库列表里带着状态，
            // 据此补种进度面板；已有进行中的状态时不覆盖（专用 2 秒轮询更新鲜），同 Web
            if let remote = l.first(where: { $0.id == id })?.metadataRefresh, remote.refreshing, metaRefresh?.refreshing != true {
                metaRefresh = remote
            }
            // 首轮墙可能比库列表先回来：那时还问不了「回到上次位置」，这里补问
            if pager.items != nil { checkRecall() }
            if let p = await prov, p != provisional { provisional = p }
            let (m, u, r, i, j) = await (miss, unknown, rev, ign, jobs)
            var partialFailure = false
            if let m { if m != missing { missing = m } } else { partialFailure = true }
            if let u { if u != unidentified { unidentified = u } } else { partialFailure = true }
            if let r { if r != review { review = r } } else { partialFailure = true }
            if let i { if i != ignored { ignored = i } } else { partialFailure = true }
            if let j { activeJobs = j }
            loadFailed = partialFailure
            if busy { busyUntil = .now.addingTimeInterval(12) }
            wallEpoch += 1
            if openPending, !pendingOpened, manage {
                // `?pending=1`：数据到齐后自动打开待处理抽屉
                pendingOpened = true
                issueTab = IssueSheet(tab: pendingTab)
            }
            await pager.refresh()
            // 墙可能在这一轮才填好：再问一次（幂等），不必等下一轮 30 秒轮询
            checkRecall()
            if effectiveSort == "title" || effectiveSort == "release_date" {
                index = (try? await api.libraryIndexFiltered(libraryId: id, filter: filter, sort: effectiveSort, order: order)) ?? index
            }
        } catch is CancellationError {
        } catch {
            loadFailed = true
        }
    }

    private func reloadCollections() async {
        guard !photoWall else { return }
        collections = (try? await api.collectionList(libraryId: libraryId, includeHidden: showHiddenCollections)) ?? collections
        if collections.isEmpty, view == .collections { view = .items }
    }

    /// 排序 / 筛选变了：整面墙回到墙首重载，索引条跟着换
    private func reloadWall() async {
        let api = self.api
        let id = libraryId
        let filter = self.filter
        let sort = effectiveSort
        let order = self.order
        index = []
        await pager.reset({ offset, limit in
            do {
                return try await api.libraryItemsFiltered(libraryId: id, filter: filter, sort: sort, order: order, limit: limit, offset: offset)
            } catch let error as APIError where error.status == 404 {
                // 超管不在浏览范围内：墙对当前身份不存在
                return []
            }
        })
        // 被更新的排序 / 筛选打断：这一轮的索引与询问作废（新一轮会自己做）
        guard !Task.isCancelled else { return }
        if sort == "title" || sort == "release_date" {
            let next = (try? await api.libraryIndexFiltered(libraryId: id, filter: filter, sort: sort, order: order)) ?? []
            guard !Task.isCancelled else { return }
            index = next
        }
        checkRecall()
    }

    /// 进页后问一次「回到上次位置」：墙与库列表（要用作品总数判断记录是否还有效）都到齐才问，
    /// 两者谁先回来都行——另一方回来时会再调一次
    private func checkRecall() {
        guard !recallChecked, libraries != nil, pager.items != nil else { return }
        recallChecked = true
        let offset = LibraryWallRecall.read(scope: recallScope, view: recallView)
        if let offset, offset < (library?.stats.itemCount ?? 0) { recallOffset = offset }
    }

    private func trackVisible(_ ids: [Int]) {
        guard let items = pager.items, !probing, !galleryOn, !photoWall else { return }
        let offsets = ids.compactMap { id in items.firstIndex { $0.mediaItemId == id } }
        guard let first = offsets.min() else { return }
        recordOffset(pager.start + first, windowStart: pager.start)
    }

    /// 三种形态写同一条位置记录；往下滑够一屏，「回到上次位置」胶囊自己让位
    private func recordOffset(_ offset: Int, windowStart: Int) {
        guard !probing else { return }
        if recallOffset != nil {
            if offset - windowStart >= 12 { recallOffset = nil }
            return
        }
        if offset != firstVisible {
            firstVisible = offset
            LibraryWallRecall.write(scope: recallScope, view: recallView, offset: offset)
        }
    }

    /// 挂后台 30 分钟以上再回来算「重新进入」（同 Web library-wall-recall，判定见 `onWallReentry`）：
    /// 墙复位到墙首（连滚动位置一起），重新问一次要不要回去
    private func resetForReentry() {
        guard recallChecked else { return } // 首载还没问过：正常流程会问
        let offset = LibraryWallRecall.read(scope: recallScope, view: recallView)
        galleryStart = 0
        photoJump = PhotoWallJump(offset: 0)
        scrollProxy?.scrollTo(LibraryWallRecall.pageTopID, anchor: .top)
        Task {
            await pager.jump(to: 0)
            scrollProxy?.scrollTo(LibraryWallRecall.pageTopID, anchor: .top)
        }
        if let offset, offset < (library?.stats.itemCount ?? 0) { recallOffset = offset }
    }

    // MARK: 元数据刷新状态

    /// 探一次 progress（进页、开始/停止之后）；只有管理员看得到
    private func probeMetaRefresh() async {
        guard permissions.canManageLibraries else { return }
        if let next = try? await api.libraryMetadataGetRefreshStatus(libraryId: libraryId), next != metaRefresh { metaRefresh = next }
    }

    /// 刷新中每 2 秒轮询；结束时重拉一次（海报 / 档案已更新）。瞬时失败保留旧状态、下一轮继续
    private func pollMetaRefresh() async {
        guard permissions.canManageLibraries, refreshingMeta else { return }
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(2))
            if Task.isCancelled { return }
            guard let next = try? await api.libraryMetadataGetRefreshStatus(libraryId: libraryId) else { continue }
            if !next.refreshing {
                // 先重拉再写状态：写状态会让 refreshingMeta 变 false，本任务（task(id:)）随之被取消，
                // 先写就会把收尾这次重拉一起取消掉，新海报要等下一轮 30 秒轮询才出现
                await reload()
                metaRefresh = next
                return
            }
            if next != metaRefresh { metaRefresh = next }
        }
    }

    /// 面板「停止」：失败放进页面 notice 横幅（同 Web）
    private func stopMetaRefresh() {
        notice = nil
        Task {
            do {
                _ = try await api.libraryMetadataStopRefresh(libraryId: libraryId)
                await probeMetaRefresh()
            } catch {
                notice = error.localizedDescription
            }
        }
    }
}

/// 海报墙右侧的 A–Z 跳转条（Web `WallIndexBar`）：按住上下滑动选字母，松手跳到该档第一格；
/// 库里没有的字母压暗；倒序（Z→A）时字母表跟着倒过来。
private struct WallIndexBar: View {
    let index: [API.LibraryIndexEntryView]
    let reversed: Bool
    /// 视口第一格所在的档（高亮）
    var active: String?
    var onJump: (Int) -> Void
    @State private var preview: String?

    private static let initials = (65 ... 90).map { String(UnicodeScalar($0)!) } + ["#"]

    var body: some View {
        let slots = reversed ? Self.initials.reversed() : Self.initials
        let byInitial = Dictionary(index.map { ($0.initial, $0) }, uniquingKeysWith: { a, _ in a })
        GeometryReader { proxy in
            let height = min(CGFloat(slots.count) * 17, proxy.size.height * 0.7)
            VStack(spacing: 0) {
                ForEach(slots, id: \.self) { letter in
                    Text(letter)
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(byInitial[letter] == nil ? Theme.textFaint.opacity(0.5) : preview == letter ? .black : letter == active ? .white : Theme.textMuted)
                        .frame(width: 16, height: height / CGFloat(slots.count))
                        .background(
                            byInitial[letter] == nil ? .clear
                                : preview == letter ? Theme.accentStrong
                                : letter == active ? Theme.accentStrong.opacity(0.35) : .clear,
                            in: .rect(cornerRadius: 4)
                        )
                }
            }
            .frame(width: 18, height: height)
            .contentShape(.rect)
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { value in
                        let i = min(slots.count - 1, max(0, Int(value.location.y / height * CGFloat(slots.count))))
                        if preview != slots[i] {
                            preview = slots[i]
                            UISelectionFeedbackGenerator().selectionChanged()
                        }
                    }
                    .onEnded { _ in
                        // 空字母不跳（同 Web）：气泡已写明「无作品」
                        if let preview, let entry = byInitial[preview] {
                            onJump(entry.offset)
                        }
                        preview = nil
                    }
            )
            .overlay(alignment: .leading) {
                if let preview {
                    // 滑选气泡：大字预览 + 该档作品数（指尖压着的字母被手指挡住，没有它等于盲选）
                    HStack(spacing: 8) {
                        Text(preview).font(.title.weight(.bold))
                        Text(byInitial[preview].map { "\($0.count) 部" } ?? "无作品")
                            .font(.caption)
                            .foregroundStyle(Theme.textMuted)
                            .fixedSize()
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .glassEffect(.regular, in: .rect(cornerRadius: 12))
                    .fixedSize()
                    .frame(width: 0, alignment: .trailing)
                    .offset(x: -12)
                }
            }
            .frame(maxHeight: .infinity)
            .padding(.trailing, 2)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("按首字母跳转")
            .accessibilityIdentifier("wall-index-bar")
        }
        .frame(width: 22)
    }
}
