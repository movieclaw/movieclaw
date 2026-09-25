import SwiftUI

/// 媒体库管理页（Web `library-manage-view.tsx`，路由 `/library/manage?create=1&tab=`）。
///
/// 页面回答的第一个问题是「有没有事要我管」：页头摘要里只挂两枚带色胶囊——在跑任务、有待处理文件——
/// 点即筛选；两样都没有就写「一切正常」。四个页签：
/// - 媒体库：搜索（库名 / 根目录）+ 类型筛选 + 一库一行（`ManageLibraryRow`），所有操作收进行尾 ⋯ 菜单；
///   手机上没有拖拽，排序走菜单里的「调整顺序」（上下按钮面板，确认后整单提交）；
/// - 回收站 / 重复文件：见 `ManageRecycleBin.swift` / `ManageDuplicateFiles.swift`；
/// - 分享：见 `ManageSharesTab.swift`。
///
/// 数据只用 `GET /libraries` 一个接口（随库下发的统计快照与任务进度足够填满状态列）。
/// 轮询节奏同 Web：任务中 3 秒（结束后再快轮询 12 秒）/ 刷新元数据 5 秒 / 入库中 10 秒 / 空闲 30 秒。
/// 页签计数：回收站与分享在别的页签时 30 秒低频轮询，重复文件只在进页与切页签时读一次（结论只在扫描后才变）。
struct LibraryManageView: View {
    var openCreate: Bool = false
    var initialTab: String?

    enum Tab: String, CaseIterable {
        case libraries, recycle, duplicates, shares

        var title: String {
            switch self {
            case .libraries: "媒体库"
            case .recycle: "回收站"
            case .duplicates: "重复文件"
            case .shares: "分享"
            }
        }
    }

    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Feedback.self) private var feedback
    @Environment(Router.self) private var router

    @State private var tab: Tab = .libraries
    @State private var libraries: [API.LibraryView]?
    @State private var failed = false
    @State private var filter = ManageLibraryFilter()
    @State private var recycleCount: Int?
    @State private var duplicateCount: Int?
    @State private var shareCount: Int?
    @State private var busyUntil = Date.distantPast
    @State private var didOpenCreate = false
    @State private var reloadSeq = 0

    // 弹层
    @State private var form: ManageFormTarget?
    @State private var organizeTarget: ManageLibraryRef?
    @State private var pendingTarget: ManagePendingTarget?
    @State private var chaptersTarget: API.LibraryView?
    @State private var reordering = false

    var body: some View {
        Group {
            if !permissions.canManageLibraries {
                EmptyState(
                    systemImage: "lock",
                    title: "没有管理权限",
                    message: "媒体库的创建、扫描与排序由管理员负责；你可以回到媒体库继续浏览。",
                    actionTitle: "返回媒体库"
                ) { router.pop() }
            } else {
                page
            }
        }
        .appBackground()
        .navigationTitle("媒体库管理")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            if let initialTab, let t = Tab(rawValue: initialTab) { tab = t }
            guard permissions.canManageLibraries else { return }
            // 首页空状态的「创建第一个媒体库」落到 ?create=1：进页即开建库向导（只开一次）
            if openCreate, !didOpenCreate {
                didOpenCreate = true
                form = .create
            }
            await reload()
            await reloadRecycleCount()
            await reloadShareCount()
        }
        .task(id: tab) {
            guard permissions.canManageLibraries else { return }
            await reloadDuplicateCount()
        }
        .polling(every: pollInterval) { await reload() }
        .polling(every: 30) {
            // 列表激活时由列表自己回报计数，其余时候低频轮询
            if tab != .recycle { await reloadRecycleCount() }
            if tab != .shares { await reloadShareCount() }
        }
        .sheet(item: $form, onDismiss: { Task { await reload() } }) { target in
            // 封面是上传即生效的，不走「保存」：关窗（含取消）也要把列表对齐
            LibraryFormSheet(libraryId: target.libraryId) { _ in Task { await reload() } }
                .sheetFeedback()
        }
        .sheet(item: $organizeTarget) { target in
            LibraryOrganizeSheet(libraryId: target.id) { Task { await reload() } }
                .sheetFeedback()
        }
        .sheet(item: $pendingTarget) { target in
            IssueDrawerView(libraryId: target.libraryId, initialTab: target.tab) { Task { await reload() } }
                .sheetFeedback()
        }
        .sheet(isPresented: $reordering) {
            if let libraries {
                ManageReorderSheet(libraries: libraries) { next in commitOrder(next) }
                    .sheetFeedback()
            }
        }
        .alert("为「\(chaptersTarget?.name ?? "")」生成章节？", isPresented: Binding(
            get: { chaptersTarget != nil },
            set: { if !$0 { chaptersTarget = nil } }
        ), presenting: chaptersTarget) { library in
            Button("开始生成") { startChapters(library, force: false) }
            Button("已有的章节也重新生成") { startChapters(library, force: true) }
            Button("取消", role: .cancel) {}
        } message: { _ in
            Text(ManageConfirmText.chapters)
        }
    }

    // MARK: 页面

    private var page: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                header
                tabBar
                    .padding(.top, 16)
                switch tab {
                case .libraries:
                    librariesTab
                case .recycle:
                    ManageRecycleBinTab { recycleCount = $0 }
                        .padding(.top, 12)
                case .duplicates:
                    ManageDuplicateFilesTab(libraries: libraries) { duplicateCount = $0 }
                        .padding(.top, 12)
                case .shares:
                    ManageSharesTab { shareCount = $0 }
                        .padding(.top, 12)
                }
            }
            .padding(.bottom, 40)
        }
        .scrollDismissesKeyboard(.interactively)
    }

    /// 页头：大标题 + 「创建媒体库」，下一行是活的摘要（规模事实 + 在跑任务 / 待处理两枚胶囊）
    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center, spacing: 12) {
                Text("媒体库管理")
                    .font(.system(size: 24, weight: .bold))
                    .foregroundStyle(.white)
                Spacer(minLength: 8)
                Button {
                    form = .create
                } label: {
                    Label("创建媒体库", systemImage: "plus")
                        .font(.subheadline.weight(.semibold))
                }
                .discoverProminentButton()
                .accessibilityIdentifier("manage-create")
            }
            ManageFlow(spacing: 8, lineSpacing: 6) {
                if let libraries {
                    let summary = ManageLibraryRules.summary(libraries)
                    Text(libraries.isEmpty ? "还没有媒体库" : summary.facts)
                        .foregroundStyle(Theme.textMuted)
                    if summary.busy > 0 {
                        ManageFilterChip(active: filter.focus == .busy, dot: Theme.info, title: "\(summary.busy) 个在跑任务") {
                            toggleFocus(.busy)
                        }
                    }
                    if summary.attention > 0 {
                        ManageFilterChip(active: filter.focus == .attention, dot: summary.missing ? Theme.danger : Theme.warning,
                                         title: "\(summary.attention) 个库有待处理文件") {
                            toggleFocus(.attention)
                        }
                    }
                    if !libraries.isEmpty, summary.busy == 0, summary.attention == 0 {
                        Text("一切正常").foregroundStyle(Theme.textFaint)
                    }
                } else {
                    Text("正在汇总媒体库…").foregroundStyle(Theme.textMuted)
                }
            }
            .font(.footnote)
            .accessibilityIdentifier("manage-summary")
        }
        .padding(.horizontal, Theme.pagePadding)
        .padding(.top, 8)
    }

    /// 页签条：计数为 0 时标签照常渲染（入口要被看见），只是不带数字
    private var tabBar: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(Tab.allCases, id: \.self) { t in
                    tabButton(t)
                }
            }
            .padding(.horizontal, Theme.pagePadding)
        }
    }

    private func tabButton(_ t: Tab) -> some View {
        let count: Int? = switch t {
        case .libraries: libraries?.count
        case .recycle: recycleCount
        case .duplicates: duplicateCount
        case .shares: shareCount
        }
        return Button {
            tab = t
        } label: {
            HStack(spacing: 6) {
                Text(t.title)
                if let count, count > 0 {
                    Text("\(count)").monospacedDigit()
                        .foregroundStyle(t == tab ? .white.opacity(0.7) : Theme.textFaint)
                }
            }
            .font(.subheadline.weight(.medium))
            .foregroundStyle(t == tab ? .white : Theme.textMuted)
            .padding(.horizontal, 14)
            .padding(.vertical, 7)
            .background(t == tab ? Color.white.opacity(0.14) : .clear, in: .capsule)
            .contentShape(.capsule)
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("manage-tab-\(t.rawValue)")
        .accessibilityAddTraits(t == tab ? .isSelected : [])
    }

    // MARK: 媒体库页签

    @ViewBuilder
    private var librariesTab: some View {
        if failed, libraries != nil {
            ManageBanner(text: "与后端通信失败，正在自动重试；下方显示的是最近一次成功加载的数据")
                .padding(.horizontal, Theme.pagePadding)
                .padding(.top, 16)
        }
        if let libraries {
            if libraries.isEmpty {
                EmptyState(
                    systemImage: "square.stack.3d.up",
                    title: "为收藏准备一个家",
                    message: "创建电影库或剧集库，选好根目录后，订阅完成的内容会自动整理到这里。",
                    actionTitle: "创建第一个媒体库"
                ) { form = .create }
                    .padding(.top, 40)
            } else {
                libraryList(libraries)
            }
        } else if failed {
            VStack(spacing: 12) {
                Text("媒体库加载失败").font(.subheadline).foregroundStyle(Theme.textMuted)
                Button("重试") { Task { await reload() } }.buttonStyle(.glass)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 60)
        } else {
            HStack(spacing: 10) {
                ProgressView()
                Text("正在加载媒体库…").font(.subheadline).foregroundStyle(Theme.textMuted)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 60)
        }
    }

    @ViewBuilder
    private func libraryList(_ libraries: [API.LibraryView]) -> some View {
        let visible = filter.apply(libraries)
        // 工具栏：搜索 / 类型筛选（状态筛选在页头摘要的胶囊上）
        VStack(alignment: .leading, spacing: 10) {
            ManageSearchField(text: $filter.query, placeholder: "按库名或根目录搜索", identifier: "manage-search")
            ManageFlow(spacing: 6, lineSpacing: 6) {
                ManageFilterChip(active: filter.kind == nil, title: "全部 \(libraries.count)") { filter.kind = nil }
                ForEach(ManageLibraryRules.kindOrder, id: \.self) { kind in
                    let count = libraries.filter { $0.kind == kind }.count
                    if count > 0 {
                        ManageFilterChip(active: filter.kind == kind, title: "\(ManageKind.label(kind)) \(count)") {
                            filter.kind = filter.kind == kind ? nil : kind
                        }
                    }
                }
            }
        }
        .padding(.horizontal, Theme.pagePadding)
        .padding(.top, 18)

        // 收藏范围重叠提示：只读不阻断
        ForEach(ManageLibraryRules.routingOverlapWarnings(libraries), id: \.self) { warning in
            ManageBanner(text: warning)
                .padding(.horizontal, Theme.pagePadding)
                .padding(.top, 14)
        }

        VStack(spacing: 0) {
            if visible.isEmpty {
                HStack(spacing: 8) {
                    Text("没有符合条件的媒体库").foregroundStyle(Theme.textMuted)
                    Button("清除筛选") { filter = ManageLibraryFilter() }
                        .foregroundStyle(Theme.info)
                        .accessibilityIdentifier("manage-clear-filter")
                }
                .font(.subheadline)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 36)
            } else {
                ForEach(Array(visible.enumerated()), id: \.element.id) { index, library in
                    if index > 0 { Divider().overlay(Color.white.opacity(0.06)) }
                    ManageLibraryRow(library: library, actions: rowActions)
                }
            }
        }
        .background(Color.white.opacity(0.02), in: .rect(cornerRadius: 18))
        .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(Theme.line))
        .padding(.horizontal, Theme.pagePadding)
        .padding(.top, 14)

        Text("顺序即首页「我的媒体库」的展示顺序，在 ··· 菜单里「调整顺序」")
            .font(.caption)
            .foregroundStyle(Theme.textFaint)
            .padding(.horizontal, Theme.pagePadding)
            .padding(.top, 10)
    }

    /// 页头摘要胶囊即筛选：再点一次取消；在其他页签上点则先切回库列表
    private func toggleFocus(_ focus: ManageLibraryFocus) {
        filter.focus = filter.focus == focus ? nil : focus
        tab = .libraries
    }

    // MARK: 行动作

    private var rowActions: ManageLibraryRow.Actions {
        .init(
            toggleScan: toggleScan,
            openPending: { library in
                // Web 跳到单库页的待处理清单；App 直接唤起媒体库模块的待处理抽屉（同一份清单）
                pendingTarget = ManagePendingTarget(
                    libraryId: library.id,
                    tab: library.stats.missingCount > 0 ? "missing" : "unidentified"
                )
            },
            organize: { organizeTarget = ManageLibraryRef(id: $0.id) },
            toggleRefresh: toggleRefresh,
            chapterImages: { chaptersTarget = $0 },
            edit: { form = .edit($0.id) },
            setDefault: { library in
                run("已将「\(library.name)」设为默认库") { _ = try await api.librarySetDefault(libraryId: library.id) }
            },
            toggleHome: toggleHome,
            reorder: { reordering = true },
            delete: delete
        )
    }

    /// 动作统一收口：成功后立刻拉一次列表（可选给一句回执），失败用 Toast 报后端的中文错误
    private func run(_ done: String? = nil, _ action: @escaping () async throws -> Void) {
        Task {
            do {
                try await action()
                await reload()
                if let done { feedback.success(done) }
            } catch {
                feedback.error(error)
            }
        }
    }

    /// 重操作先确认；停止不确认——停止本身就是在纠正。开始给一句回执：已是最新的库扫描毫秒级就结束，
    /// 行内只会从「最近扫描 3 分钟前」变成「几秒前」，没有这句用户会以为没点上
    private func toggleScan(_ library: API.LibraryView) {
        if library.scanning {
            run { _ = try await api.libraryScanStop(libraryId: library.id) }
            return
        }
        Task {
            guard await feedback.confirm("扫描「\(library.name)」？", message: ManageConfirmText.scan, confirmTitle: "开始扫描") else { return }
            run("已开始扫描「\(library.name)」") { _ = try await api.libraryScanStart(libraryId: library.id) }
        }
    }

    private func toggleRefresh(_ library: API.LibraryView) {
        if library.metadataRefresh?.refreshing == true {
            run { _ = try await api.libraryMetadataStopRefresh(libraryId: library.id) }
            return
        }
        Task {
            let caps = library.capabilities
            let ok: Bool
            if !caps.scraped, caps.playable {
                ok = await feedback.confirm("重新读取「\(library.name)」的 NFO 与封面？", message: ManageConfirmText.rereadNfo, confirmTitle: "开始读取")
            } else {
                ok = await feedback.confirm("刷新「\(library.name)」的元数据？", message: ManageConfirmText.refresh, confirmTitle: "开始刷新")
            }
            guard ok else { return }
            run { _ = try await api.libraryMetadataRefreshLibrary(libraryId: library.id) }
        }
    }

    private func startChapters(_ library: API.LibraryView, force: Bool) {
        run { _ = try await api.libraryChapterImagesGenerate(libraryId: library.id, force: force) }
    }

    /// 首页显示开关：只改这一个字段——payload 里没传的字段后端按「不改动」处理（同 Web）
    private func toggleHome(_ library: API.LibraryView) {
        let body = API.LibraryPayload(
            name: library.name,
            kind: library.kind,
            excludeFromHome: !library.excludeFromHome,
            rootPaths: library.rootPaths
        )
        run(library.excludeFromHome ? "「\(library.name)」已在首页展示" : "「\(library.name)」已从首页排除") {
            _ = try await api.libraryUpdate(libraryId: library.id, body: body)
        }
    }

    private func delete(_ library: API.LibraryView) {
        Task {
            guard await feedback.confirm(
                "删除媒体库「\(library.name)」？",
                message: "磁盘文件不受影响，挂在它上面的订阅将回落到该类型的默认库。",
                confirmTitle: "删除库",
                destructive: true
            ) else { return }
            run("已删除媒体库「\(library.name)」") { _ = try await api.libraryDelete(libraryId: library.id) }
        }
    }

    /// 提交新顺序：先乐观换位，失败回滚。全量 id 一次提交（后端接口要求）
    private func commitOrder(_ next: [API.LibraryView]) {
        let previous = libraries
        libraries = next
        Task {
            do {
                _ = try await api.libraryReorder(body: API.LibraryReorderPayload(orderedIds: next.map(\.id)))
                await reload()
                feedback.success("顺序已更新，首页「我的媒体库」同步生效")
            } catch {
                libraries = previous
                feedback.error(error)
            }
        }
    }

    // MARK: 加载

    /// 轮询节奏与首页一致：任务中 3 秒 / 刷新元数据 5 秒 / 入库中 10 秒 / 空闲 30 秒
    private var pollInterval: Double {
        let all = libraries ?? []
        if all.contains(where: { $0.scanning || $0.organizing }) || Date.now < busyUntil { return 3 }
        if all.contains(where: { $0.metadataRefresh?.refreshing == true }) { return 5 }
        if all.contains(where: { !$0.scanning && !$0.organizing && ($0.lastScan?.deferred ?? 0) > 0 }) { return 10 }
        return 30
    }

    private func reload() async {
        // 轮询乱序守卫：扫描期间慢响应可能晚于下一轮到达
        reloadSeq += 1
        let seq = reloadSeq
        do {
            let libs = try await api.libraryList(scope: "all")
            guard seq == reloadSeq else { return }
            failed = false
            if libs != libraries { libraries = libs }
            if libs.contains(where: { $0.scanning || $0.organizing }) { busyUntil = .now.addingTimeInterval(12) }
        } catch is CancellationError {
        } catch {
            if seq == reloadSeq { failed = true }
        }
    }

    /// 回收站计数：一次 limit=1 的列表请求只为拿 total_files
    private func reloadRecycleCount() async {
        if let data = try? await api.libraryRecycleList(limit: 1, offset: 0) { recycleCount = data.totalFiles }
    }

    /// 重复文件计数：读上一轮扫描落库的结论（limit=0 只要摘要）
    private func reloadDuplicateCount() async {
        if let data = try? await api.libraryDuplicatesList(limit: 0, offset: 0) { duplicateCount = data.totalFiles }
    }

    private func reloadShareCount() async {
        if let rows = try? await api.sharesList() { shareCount = rows.count }
    }
}

// MARK: - 弹层目标

/// 表单弹层：新建或编辑某个库
enum ManageFormTarget: Identifiable {
    case create
    case edit(Int)

    var id: String {
        switch self {
        case .create: "create"
        case let .edit(id): "edit-\(id)"
        }
    }

    var libraryId: Int? {
        if case let .edit(id) = self { return id }
        return nil
    }
}

struct ManageLibraryRef: Identifiable {
    let id: Int
}

struct ManagePendingTarget: Identifiable {
    let libraryId: Int
    let tab: String
    var id: Int { libraryId }
}

// MARK: - 调整顺序（手机端没有拖拽）

/// 手机端的排序面板：上下箭头换位，确认后一次提交整单；面板内持有顺序草稿，取消不影响列表
struct ManageReorderSheet: View {
    let libraries: [API.LibraryView]
    let onSubmit: ([API.LibraryView]) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var draft: [API.LibraryView] = []

    private var changed: Bool { draft.map(\.id) != libraries.map(\.id) }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach(Array(draft.enumerated()), id: \.element.id) { index, library in
                        HStack(spacing: 12) {
                            Text("\(index + 1)").font(.caption.monospacedDigit()).foregroundStyle(Theme.textFaint)
                                .frame(width: 20)
                            Text(library.name).font(.body.weight(.medium)).lineLimit(1)
                            Spacer(minLength: 8)
                            Button {
                                if let next = ManageLibraryRules.move(draft, from: index, to: index - 1) { draft = next }
                            } label: {
                                Image(systemName: "chevron.up").frame(width: 28, height: 28)
                            }
                            .buttonStyle(.glass)
                            .buttonBorderShape(.circle)
                            .disabled(index == 0)
                            .accessibilityLabel("「\(library.name)」上移")
                            .accessibilityIdentifier("reorder-up-\(library.id)")
                            Button {
                                if let next = ManageLibraryRules.move(draft, from: index, to: index + 1) { draft = next }
                            } label: {
                                Image(systemName: "chevron.down").frame(width: 28, height: 28)
                            }
                            .buttonStyle(.glass)
                            .buttonBorderShape(.circle)
                            .disabled(index == draft.count - 1)
                            .accessibilityLabel("「\(library.name)」下移")
                            .accessibilityIdentifier("reorder-down-\(library.id)")
                        }
                    }
                } footer: {
                    Text("这也是首页「我的媒体库」的展示顺序。")
                }
            }
            .scrollContentBackground(.hidden)
            .navigationTitle("调整顺序")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }.accessibilityIdentifier("reorder-cancel")
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存顺序") {
                        onSubmit(draft)
                        dismiss()
                    }
                    .disabled(!changed)
                    .accessibilityIdentifier("reorder-save")
                }
            }
        }
        .presentationDetents([.medium, .large])
        .presentationBackground(.regularMaterial)
        .onAppear { draft = libraries }
    }
}

// MARK: - 小组件

/// 页头 / 工具栏的胶囊筛选（Web `FilterChip`）：可带状态圆点，激活态提亮
struct ManageFilterChip: View {
    var active: Bool
    var dot: Color?
    var title: String
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                if let dot { Circle().fill(dot).frame(width: 6, height: 6) }
                Text(title).monospacedDigit()
            }
            .font(.caption.weight(.medium))
            .foregroundStyle(active ? Theme.text : Theme.textMuted)
            .padding(.horizontal, 10)
            .frame(height: 28)
            .background(active ? Color.white.opacity(0.14) : .clear, in: .capsule)
            .overlay(Capsule().strokeBorder(Color.white.opacity(active ? 0.2 : 0.1)))
            .contentShape(.capsule)
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(active ? .isSelected : [])
    }
}

/// 琥珀色提示条（通信失败 / 收藏范围重叠）
struct ManageBanner: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.footnote)
            .foregroundStyle(Color(red: 0.99, green: 0.85, blue: 0.55))
            .fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.orange.opacity(0.1), in: .rect(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.orange.opacity(0.25)))
    }
}

/// 圆角搜索框（放大镜 + 清除）
struct ManageSearchField: View {
    @Binding var text: String
    var placeholder: String
    var identifier: String

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass").font(.subheadline).foregroundStyle(Theme.textMuted)
            TextField(placeholder, text: $text)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .submitLabel(.search)
                .accessibilityIdentifier(identifier)
            if !text.isEmpty {
                Button {
                    text = ""
                } label: {
                    Image(systemName: "xmark.circle.fill").foregroundStyle(Theme.textFaint)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("清除搜索")
            }
        }
        .font(.subheadline)
        .padding(.horizontal, 12)
        .frame(height: 38)
        .background(Color.white.opacity(0.04), in: .capsule)
        .overlay(Capsule().strokeBorder(Theme.line))
    }
}

/// 换行布局（SwiftUI 没有 flex-wrap）：摘要胶囊、类型筛选、区域 / 类型芯片墙共用；同行内垂直居中
struct ManageFlow: Layout {
    var spacing: CGFloat = 6
    var lineSpacing: CGFloat = 6

    private func rows(_ subviews: Subviews, width: CGFloat) -> [[(LayoutSubviews.Element, CGSize)]] {
        var rows: [[(LayoutSubviews.Element, CGSize)]] = [[]]
        var rowWidth: CGFloat = 0
        for view in subviews {
            let natural = view.sizeThatFits(.unspecified)
            let w = min(natural.width, width)
            let size = w < natural.width ? view.sizeThatFits(ProposedViewSize(width: w, height: nil)) : natural
            if rowWidth > 0, rowWidth + w > width {
                rows.append([])
                rowWidth = 0
            }
            rows[rows.count - 1].append((view, CGSize(width: w, height: size.height)))
            rowWidth += w + spacing
        }
        return rows
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        let all = rows(subviews, width: width)
        let height = all.reduce(0) { $0 + ($1.map(\.1.height).max() ?? 0) } + lineSpacing * CGFloat(max(0, all.count - 1))
        let maxRow = all.map { row in row.reduce(0) { $0 + $1.1.width } + spacing * CGFloat(max(0, row.count - 1)) }.max() ?? 0
        return CGSize(width: proposal.width ?? maxRow, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var y = bounds.minY
        for row in rows(subviews, width: bounds.width) {
            let lineHeight = row.map(\.1.height).max() ?? 0
            var x = bounds.minX
            for (view, size) in row {
                view.place(at: CGPoint(x: x, y: y + (lineHeight - size.height) / 2), proposal: ProposedViewSize(size))
                x += size.width + spacing
            }
            y += lineHeight + lineSpacing
        }
    }
}
