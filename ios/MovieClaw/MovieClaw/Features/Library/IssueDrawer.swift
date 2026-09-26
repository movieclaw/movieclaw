import SwiftUI

/*
 待处理抽屉（对应 Web components/library-detail-view.tsx 的 IssueDrawer）：
 缺失 / 待识别 / 身份复核 / 已忽略 四个页签，海报墙不再被待办铺满。

 设计要点：
 - Web 由父页面把四份清单传进来；原生这里只拿 libraryId，自己并发拉四份清单 + 库类型
   （电影库 / 剧集库决定认领时搜哪种条目、放错库提示怎么说），任一处理动作成功后
   重拉清单并回调 onChanged，让父页刷新待处理计数；
 - 「已忽略」不是待办，是**已处理**的归档：默认不打扰，只在有内容时露出页签，
   给识别器变强之后反悔的机会；
 - 未指定 initialTab 时的落点同 Web ⋯ 菜单：按页签顺序取第一个有内容的，都空了落在「待识别」；
 - 刷新节奏跟父页（Web 抽屉直接吃父页的清单，随父页轮询：忙时 3 秒 / 入库或刷新中 10 秒 / 平时 30 秒），
   父页把当前轮询间隔经 `pollInterval` 传进来；不传按 30 秒。
 */

/// 以 .sheet 呈现，自带 NavigationStack 与关闭按钮
struct IssueDrawerView: View {
    let libraryId: Int
    var initialTab: String? = nil
    var onChanged: () -> Void = {}
    /// 父页当前的轮询间隔（秒）
    var pollInterval: Double = 30

    init(libraryId: Int, initialTab: String? = nil, pollInterval: Double = 30, onChanged: @escaping () -> Void = {}) {
        self.libraryId = libraryId
        self.initialTab = initialTab
        self.pollInterval = pollInterval
        self.onChanged = onChanged
    }

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(\.dismiss) private var dismiss

    @State private var state: Loadable<IssueSnapshot> = .loading
    @State private var tab: IssueTab?
    @State private var query = ""
    @State private var busy = false

    var body: some View {
        NavigationStack {
            AsyncContent(state, retry: load) { snapshot in
                content(snapshot, tab: tab ?? snapshot.pendingTab)
            }
            .navigationTitle("待处理")
            .navigationBarTitleDisplayMode(.inline)
            .searchable(
                text: $query,
                placement: .navigationBarDrawer(displayMode: .always),
                prompt: (tab ?? .unidentified) == .missing ? "按片名过滤…" : "按文件名过滤…"
            )
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("关闭") { dismiss() }
                }
                if let snapshot = state.value {
                    ToolbarItem(placement: .primaryAction) { batchButton(snapshot) }
                }
            }
            .appBackground()
        }
        .task { await load() }
        // 父页海报墙空闲时 30 秒一轮（清单 13 节），抽屉跟随同一节奏
        .polling(every: pollInterval) { await load() }
        // 切页签清空过滤词（同 Web）
        .onChange(of: tab) { query = "" }
    }

    // MARK: - 内容

    @ViewBuilder
    private func content(_ snapshot: IssueSnapshot, tab: IssueTab) -> some View {
        let keyword = query.trimmingCharacters(in: .whitespaces).lowercased()
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 10) {
                tabPicker(snapshot, current: tab)
                Text(tab.explanation)
                    .font(.caption)
                    .foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)

                // 「放错库了」修复引导：认领解决不了这一类，不给引导用户会在认领里打转
                if tab == .unidentified, snapshot.kindMismatchFiles > 0 {
                    Text("有 \(snapshot.kindMismatchFiles) 个文件的实际类型与本库不符（\(snapshot.movie ? "剧集文件在电影库" : "电影文件在剧集库")），认领无法解决。个别文件放错了：把文件移到对应类型的库即可；整库类型建错了：删除本库并以正确类型重建（删库不会动磁盘文件），重新扫描即可恢复。")
                        .font(.caption)
                        .foregroundStyle(Theme.warning)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Theme.warning.opacity(0.1), in: .rect(cornerRadius: 10))
                }

                rows(snapshot, tab: tab, keyword: keyword)
            }
            .padding(.horizontal, Theme.pagePadding)
            .padding(.bottom, 24)
        }
        .scrollDismissesKeyboard(.interactively)
        .disabled(busy)
    }

    /// 页签：缺失 N / 待识别 N（文件数）/ 身份复核 N / 已忽略 N（文件数，没有内容就不占位）
    private func tabPicker(_ snapshot: IssueSnapshot, current: IssueTab) -> some View {
        let tabs = IssueTab.allCases.filter { $0 != .ignored || snapshot.ignoredFileTotal > 0 || current == .ignored }
        return Picker("待处理", selection: Binding(get: { current }, set: { tab = $0 })) {
            ForEach(tabs, id: \.self) { item in
                let count = snapshot.count(for: item)
                Text(count > 0 ? "\(item.title) \(count)" : item.title).tag(item)
            }
        }
        .pickerStyle(.segmented)
        .padding(.top, 4)
    }

    @ViewBuilder
    private func rows(_ snapshot: IssueSnapshot, tab: IssueTab, keyword: String) -> some View {
        let changed: () async -> Void = { await afterChange() }
        switch tab {
        case .missing:
            let shown = keyword.isEmpty ? snapshot.missing : snapshot.missing.filter { $0.title.lowercased().contains(keyword) }
            ForEach(shown, id: \.mediaItemId) { item in
                IssueMissingRow(libraryId: libraryId, item: item, onChanged: changed)
            }
            if shown.isEmpty { emptyNote(keyword: keyword, tab: tab) }
        case .unidentified:
            let shown = snapshot.unidentified.filter { $0.matches(keyword) }
            ForEach(shown, id: \.key) { group in
                IssueUnidentifiedGroupRow(group: group, movie: snapshot.movie, onChanged: changed)
            }
            if shown.isEmpty { emptyNote(keyword: keyword, tab: tab) }
        case .review:
            let shown = keyword.isEmpty ? snapshot.review : snapshot.review.filter {
                $0.label.lowercased().contains(keyword)
                    || $0.current.title.lowercased().contains(keyword)
                    || $0.suggestion.title.lowercased().contains(keyword)
            }
            ForEach(shown, id: \.key) { group in
                IssueReviewGroupRow(group: group, onChanged: changed)
            }
            if shown.isEmpty { emptyNote(keyword: keyword, tab: tab) }
        case .ignored:
            let shown = snapshot.ignored.filter { $0.matches(keyword) }
            ForEach(shown, id: \.key) { group in
                IssueIgnoredGroupRow(group: group, onChanged: changed)
            }
            if shown.isEmpty { emptyNote(keyword: keyword, tab: tab) }
        }
    }

    private func emptyNote(keyword: String, tab: IssueTab) -> some View {
        Text(!keyword.isEmpty ? "没有匹配的条目" : tab == .ignored ? "没有忽略过的文件" : "没有需要处理的了 🎉")
            .font(.subheadline)
            .foregroundStyle(Theme.textMuted)
            .frame(maxWidth: .infinity)
            .padding(.top, 48)
    }

    // MARK: - 批量操作

    @ViewBuilder
    private func batchButton(_ snapshot: IssueSnapshot) -> some View {
        let current = tab ?? snapshot.pendingTab
        if current == .missing, !snapshot.missing.isEmpty {
            Button("全部清理") { Task { await clearAllMissing(snapshot) } }
                .disabled(busy)
        } else if current == .unidentified, !snapshot.unidentified.isEmpty {
            Button("全部忽略") { Task { await ignoreAllUnidentified(snapshot) } }
                .disabled(busy)
        }
    }

    private func clearAllMissing(_ snapshot: IssueSnapshot) async {
        guard await feedback.confirm(
            "清理全部 \(snapshot.missingFileTotal) 条缺失记录？",
            message: "只删台账，不动磁盘。",
            confirmTitle: "全部清理",
            destructive: true
        ) else { return }
        await runBatch {
            _ = try await api.libraryMissingClearRecords(body: .init(libraryId: libraryId, mediaItemId: nil))
        }
    }

    private func ignoreAllUnidentified(_ snapshot: IssueSnapshot) async {
        guard await feedback.confirm(
            "忽略全部 \(snapshot.unidentifiedFileTotal) 个待识别文件？",
            message: "之后扫描不再过问它们（不动磁盘；可在「已忽略」里恢复）。",
            confirmTitle: "全部忽略",
            destructive: true
        ) else { return }
        await runBatch {
            _ = try await api.libraryIdentificationIgnoreAllUnidentifiedFiles(body: .init(libraryId: libraryId))
        }
    }

    private func runBatch(_ work: () async throws -> Void) async {
        busy = true
        defer { busy = false }
        do {
            try await work()
            await afterChange()
        } catch {
            feedback.error(error)
        }
    }

    // MARK: - 数据

    /// 任一处理动作成功后：重拉清单 + 通知父页刷新待处理计数
    private func afterChange() async {
        await load()
        onChanged()
    }

    private func load() async {
        let api = api
        let libraryId = libraryId
        await Loadable.load(into: $state) {
            async let library = api.libraryGet(libraryId: libraryId)
            async let missing = api.libraryMissingList(libraryId: libraryId)
            async let unidentified = api.libraryIdentificationListUnidentifiedFiles(libraryId: libraryId)
            async let review = api.libraryIdentificationListReviewCases(libraryId: libraryId)
            async let ignored = api.libraryIdentificationListIgnoredFiles(libraryId: libraryId)
            return try await IssueSnapshot(
                movie: library.kind == "movie",
                missing: missing,
                unidentified: unidentified,
                review: review,
                ignored: ignored
            )
        }
        // 首次加载后定落点：调用方指定的页签优先，否则取第一个有内容的
        if tab == nil, let snapshot = state.value {
            tab = initialTab.flatMap(IssueTab.init(rawValue:)) ?? snapshot.pendingTab
        }
    }
}

// MARK: - 页签与清单快照

/// 取值与 Web IssueTab 一致（missing / unidentified / review / ignored）
private enum IssueTab: String, CaseIterable, Hashable {
    case missing, unidentified, review, ignored

    var title: String {
        switch self {
        case .missing: "缺失"
        case .unidentified: "待识别"
        case .review: "身份复核"
        case .ignored: "已忽略"
        }
    }

    /// 页签说明行（Web 原文照搬）
    var explanation: String {
        switch self {
        case .missing:
            "文件已不在磁盘；「重新下载」交给订阅管线补回，「清理记录」只删台账（都不动磁盘）；文件回归会自动恢复。经常自己删片子的话，可在库设置里打开「扫描后自动清理丢失记录」，以后扫完即对齐。"
        case .unidentified:
            "同一目录的文件聚成一组，认领一次整组生效；点候选可先核对海报简介再确认。状态标签悬停看完整原因。"
        case .review:
            "识别器升级后重新核对了这些条目，新结论与现有身份不一致。身份没有被自动改动——由你拍板：采纳新结论改挂条目，或维持现状。拍板后不再提醒。"
        case .ignored:
            "这些文件你选择过「忽略」，之后每次扫描都会直接跳过，不再占用待识别清单（磁盘文件一直都在）。识别器在持续变强，当初认不出的现在未必认不出——「恢复」即可让它重新参与识别。"
        }
    }
}

/// 四份清单 + 库类型：一轮加载一起就位，页签计数与落点据此计算
private struct IssueSnapshot {
    var movie: Bool
    var missing: [API.MissingItemView]
    var unidentified: [API.UnidentifiedGroupView]
    var review: [API.ReviewGroupView]
    var ignored: [API.UnidentifiedGroupView]

    var missingFileTotal: Int { missing.reduce(0) { $0 + $1.files.count } }
    var unidentifiedFileTotal: Int { unidentified.reduce(0) { $0 + $1.fileCount } }
    var ignoredFileTotal: Int { ignored.reduce(0) { $0 + $1.fileCount } }
    /// 「放错库了」的文件数：认领解决不了（TMDB 的 movie/tv 是两套 id 空间），单独给修复引导
    var kindMismatchFiles: Int {
        unidentified.reduce(0) { $0 + ($1.code == "kind_mismatch" ? $1.fileCount : 0) }
    }

    func count(for tab: IssueTab) -> Int {
        switch tab {
        case .missing: missing.count
        case .unidentified: unidentifiedFileTotal
        case .review: review.count
        case .ignored: ignoredFileTotal
        }
    }

    /// 同 Web pendingTab：按页签顺序取第一个有内容的，都空了落在「待识别」
    var pendingTab: IssueTab {
        if !missing.isEmpty { return .missing }
        if !unidentified.isEmpty { return .unidentified }
        if !review.isEmpty { return .review }
        if !ignored.isEmpty { return .ignored }
        return .unidentified
    }
}

private extension API.UnidentifiedGroupView {
    /// 文件名过滤：组名或组内任一文件路径包含关键词
    func matches(_ keyword: String) -> Bool {
        keyword.isEmpty
            || label.lowercased().contains(keyword)
            || files.contains { $0.filePath.lowercased().contains(keyword) }
    }
}

/// 行卡片底（同 Web rounded-xl bg-white/[0.03]）
private extension View {
    func issueRowCard(opacity: Double = 0.03) -> some View {
        padding(.horizontal, 14)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.white.opacity(opacity), in: .rect(cornerRadius: 12))
    }
}

/// 行内的动作执行：忙碌态 + 行内错误（同 Web 每行自带 error 文案，不弹全局提示）
private func issueRun(
    busy: Binding<Bool>,
    error: Binding<String?>,
    onChanged: () async -> Void,
    _ work: () async throws -> Void
) async {
    busy.wrappedValue = true
    error.wrappedValue = nil
    defer { busy.wrappedValue = false }
    do {
        try await work()
        await onChanged()
    } catch is CancellationError {
    } catch let failure {
        error.wrappedValue = failure.localizedDescription
    }
}

/// 展开的文件清单（等宽小字，中间省略）
private struct IssueFileList: View {
    let paths: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Divider().overlay(Theme.line)
            ForEach(paths, id: \.self) { path in
                Text(path)
                    .font(.caption.monospaced())
                    .foregroundStyle(Theme.textFaint)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
            }
        }
        .padding(.top, 4)
    }
}

// MARK: - 缺失行

/// 缺失条目：剧集按季聚合缺失集数做摘要，电影显示文件数；「重新下载」「清理记录」。
private struct IssueMissingRow: View {
    let libraryId: Int
    let item: API.MissingItemView
    let onChanged: () async -> Void

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @State private var busy = false
    @State private var error: String?
    @State private var done: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            VStack(alignment: .leading, spacing: 2) {
                Text(item.title + (item.year.map { " (\($0))" } ?? ""))
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Theme.text.opacity(0.85))
                    .lineLimit(1)
                Text(summary)
                    .font(.caption)
                    .foregroundStyle(Theme.textMuted)
            }
            if let done {
                Text(done).font(.subheadline).foregroundStyle(Theme.success)
            } else {
                HStack(spacing: 8) {
                    Button("重新下载") { Task { await redownload() } }
                        .buttonStyle(.glassProminent)
                        .fontWeight(.semibold)
                    Button("清理记录") { Task { await clear() } }
                        .buttonStyle(.glass)
                }
                .controlSize(.small)
                .disabled(busy)
            }
            if item.subscriptionId != nil, done == nil {
                Text("该条目有订阅在追踪").font(.caption).foregroundStyle(Theme.warning.opacity(0.8))
            }
            if let error {
                Text(error).font(.caption).foregroundStyle(Theme.danger)
            }
        }
        .issueRowCard()
    }

    /// 剧集：「第 1 季缺 3 集、特别篇 1 集」；电影：「N 个文件」
    private var summary: String {
        guard item.kind == "tv" else { return "\(item.files.count) 个文件" }
        let bySeason = Dictionary(grouping: item.files, by: \.seasonNumber)
        return bySeason.keys.sorted().map { season in
            let n = bySeason[season]?.count ?? 0
            return season == 0 ? "特别篇 \(n) 集" : "第 \(season) 季缺 \(n) 集"
        }
        .joined(separator: "、")
    }

    private func redownload() async {
        await issueRun(busy: $busy, error: $error, onChanged: onChanged) {
            let result = try await api.libraryMissingRedownload(body: .init(libraryId: libraryId, mediaItemId: item.mediaItemId))
            done = "已交给订阅管线（\(result["requeued"]?.intValue ?? 0) 个工单排队）"
        }
    }

    private func clear() async {
        guard await feedback.confirm(
            "清理「\(item.title)」的 \(item.files.count) 条缺失记录？",
            message: item.subscriptionId != nil
                ? "该条目有正在追踪的订阅，只清记录的话订阅可能把它重新下回来。（只删台账，不动磁盘）"
                : "只删台账，不动磁盘。",
            confirmTitle: "清理记录",
            destructive: true
        ) else { return }
        await issueRun(busy: $busy, error: $error, onChanged: onChanged) {
            _ = try await api.libraryMissingClearRecords(body: .init(libraryId: libraryId, mediaItemId: item.mediaItemId))
        }
    }
}

// MARK: - 待识别组

/*
 待识别组：整组认领或整组忽略。一部剧几十集是同一件事，逐集处理既刷屏又折磨人。
 认领一律走详情确认面板；候选全不对时走 TMDB 搜索。TMDB 真没有条目（no_match）
 也不是死路：卡片上写明「扫描会自动重试」并引导去 TMDB 社区补录。
 */

/// 失败分类 → 短标签。配色只分两级：黄色专供「有确定解法」的一类（TMDB 不可达、放错库），
/// 其余是正常待办不是错误，用中性色——否则满屏黄字等于没有信号。
private struct IssueStatusBadge: View {
    let group: API.UnidentifiedGroupView
    @State private var showsReason = false

    private var meta: (label: String, warn: Bool) {
        switch group.code {
        case "tmdb_unreachable": ("TMDB 不可达", true)
        case "kind_mismatch": ("放错库了", true)
        case "ambiguous": ("\(group.candidates.count) 个候选待定", false)
        case "no_match": ("TMDB 无匹配", false)
        case "unparsable": ("认不出片名", false)
        // 旧数据没有 code：退回按有无候选粗分，不至于空着
        default: group.candidates.isEmpty ? ("TMDB 无匹配", false) : ("\(group.candidates.count) 个候选待定", false)
        }
    }

    var body: some View {
        // Web 悬停看完整原因；触屏上改为点一下弹出
        Button {
            if group.reason != nil { showsReason = true }
        } label: {
            Text(meta.label)
                .font(.caption)
                .foregroundStyle(meta.warn ? Theme.warning : Theme.textMuted)
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(meta.warn ? Theme.warning.opacity(0.16) : Color.white.opacity(0.07), in: .rect(cornerRadius: 6))
                .expandedHitArea(vertical: 12)
        }
        .buttonStyle(.plain)
        .popover(isPresented: $showsReason) {
            Text(group.reason ?? "")
                .font(.footnote)
                .padding()
                .frame(maxWidth: 320)
                .presentationCompactAdaptation(.popover)
        }
    }
}

private struct IssueUnidentifiedGroupRow: View {
    let group: API.UnidentifiedGroupView
    let movie: Bool
    let onChanged: () async -> Void

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @State private var busy = false
    @State private var error: String?
    @State private var expanded = false
    @State private var panel: IssueClaimPanelState?

    private var fileIds: [Int] { group.files.map(\.id) }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            // 头行：是谁 + 次要动作（⋯）
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(group.label)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Theme.text.opacity(0.85))
                    .lineLimit(2)
                    .frame(maxWidth: .infinity, alignment: .leading)
                moreMenu
            }
            // 状态、多大、主动作
            HStack(spacing: 8) {
                IssueStatusBadge(group: group)
                Text("\(group.fileCount) 个 · \(Formatters.bytes(group.totalSizeBytes))")
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
                Spacer(minLength: 0)
                // TMDB 不可达有确定解法，直接给按钮；其余给搜索入口
                if group.code == "tmdb_unreachable" {
                    Button("重新扫描") { Task { await rescan() } }
                        .buttonStyle(.glass)
                        .disabled(busy)
                } else {
                    Button("搜索") { panel = panel == .search ? nil : .search }
                        .buttonStyle(.glass)
                        .tint(panel == .search ? Theme.accentStrong : nil)
                        .disabled(busy)
                }
            }
            .controlSize(.small)

            // no_match 的两条真实出口：等扫描自动重试，或自己去 TMDB 补录
            if group.code == "no_match" {
                VStack(alignment: .leading, spacing: 2) {
                    Text("每次扫描会自动重试识别。TMDB 是社区维护的数据库，确认缺这个条目可以")
                    Link("去 TMDB 补录 ↗", destination: IssueTMDBLink.new(movie: movie))
                        .underline()
                    Text("，收录后回来搜索认领；自录、花絮这类本就不会有条目的内容用「忽略」即可。")
                }
                .font(.caption)
                .foregroundStyle(Theme.textFaint)
            }

            if !group.candidates.isEmpty {
                IssueCandidateChips(
                    candidates: group.candidates,
                    selectedId: panel?.selectedTmdbId,
                    disabled: busy
                ) { candidate in
                    panel = panel?.selectedTmdbId == candidate.tmdbId ? nil : .confirm(IssueClaimSeed(candidate: candidate))
                }
            }

            if let error {
                Text(error).font(.caption).foregroundStyle(Theme.danger)
            }

            switch panel {
            case .search:
                IssueClaimSearchPanel(movie: movie, initialQuery: IssueClaimSeed.searchText(fromLabel: group.label)) { seed in
                    panel = .confirm(seed)
                }
            case let .confirm(seed):
                IssueClaimConfirmPanel(
                    seed: seed, movie: movie, fileCount: group.fileCount, busy: busy,
                    onConfirm: { Task { await claim(seed) } },
                    onCancel: { panel = nil }
                )
                .id(seed.tmdbId)
            case nil:
                EmptyView()
            }

            if expanded {
                IssueFileList(paths: group.files.map(\.filePath))
            }
        }
        .issueRowCard()
    }

    /// 次要动作（查看文件 / 忽略）收进 ⋯，不跟主动作抢视线
    private var moreMenu: some View {
        Menu {
            if group.fileCount > 1 {
                Button(expanded ? "收起文件" : "查看文件") { expanded.toggle() }
            }
            Button("忽略") { Task { await ignoreGroup() } }
                .disabled(busy)
        } label: {
            Image(systemName: "ellipsis")
                .font(.subheadline)
                .frame(width: 30, height: 26)
                .contentShape(.rect)
        }
        .buttonStyle(.glass)
        .buttonBorderShape(.capsule)
        .controlSize(.small)
        .accessibilityLabel("更多操作")
    }

    private func rescan() async {
        await issueRun(busy: $busy, error: $error, onChanged: onChanged) {
            _ = try await api.libraryScanStart(libraryId: group.libraryId)
        }
    }

    private func claim(_ seed: IssueClaimSeed) async {
        let ref = "tmdb:\(movie ? "movie" : "tv"):\(seed.tmdbId)"
        await issueRun(busy: $busy, error: $error, onChanged: onChanged) {
            _ = try await api.libraryIdentificationAssignFilesToTitle(body: .init(fileIds: fileIds, titleRef: ref))
        }
    }

    private func ignoreGroup() async {
        if group.fileCount > 1 {
            guard await feedback.confirm(
                "忽略「\(group.label)」的全部 \(group.fileCount) 个文件？",
                message: "之后扫描不再过问（不动磁盘；可在「已忽略」里恢复）。适合自录、花絮这类 TMDB 本就不会有条目的内容——只是暂时认不出的建议先搜索认领。",
                confirmTitle: "全部忽略",
                destructive: true
            ) else { return }
        }
        let api = api
        let ids = fileIds
        await issueRun(busy: $busy, error: $error, onChanged: onChanged) {
            try await withThrowingTaskGroup(of: Void.self) { tasks in
                for id in ids {
                    tasks.addTask { _ = try await api.libraryIdentificationIgnoreFile(fileId: id) }
                }
                try await tasks.waitForAll()
            }
        }
    }
}

// MARK: - 身份复核组

/// 身份复核：现身份 vs 新识别结论并排对比，采纳或维持一键拍板（整组生效）。
private struct IssueReviewGroupRow: View {
    let group: API.ReviewGroupView
    let onChanged: () async -> Void

    @Environment(\.api) private var api
    @State private var busy = false
    @State private var error: String?

    /// 新识别结论的强调色（Web #c4b5fd）
    private static let suggestionTint = Color(red: 0xC4 / 255, green: 0xB5 / 255, blue: 0xFD / 255)

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(group.label)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Theme.text.opacity(0.85))
                    .lineLimit(2)
                Text("\(group.fileCount) 个文件 · \(Formatters.bytes(group.totalSizeBytes))")
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
            }

            // 两个身份并排：看清是谁 vs 该是谁
            HStack(spacing: 8) {
                side(group.current, suggestion: false)
                Text("→").font(.title3).foregroundStyle(Theme.textFaint)
                side(group.suggestion, suggestion: true)
            }

            HStack(spacing: 8) {
                Button {
                    Task { await resolve(accept: true) }
                } label: {
                    Text("采纳新结论").fontWeight(.semibold).foregroundStyle(Self.suggestionTint)
                }
                .buttonStyle(.glass)
                .tint(Self.suggestionTint)
                Button("维持现状") { Task { await resolve(accept: false) } }
                    .buttonStyle(.glass)
            }
            .controlSize(.small)
            .disabled(busy)

            if let error {
                Text(error).font(.caption).foregroundStyle(Theme.danger)
            }
        }
        .issueRowCard()
    }

    private func side(_ info: API.ReviewItemView, suggestion: Bool) -> some View {
        HStack(spacing: 8) {
            RemoteImage(url: api.image(info.posterUrl, .posterCard))
                .frame(width: 36, height: 54)
                .clipShape(.rect(cornerRadius: 6))
            VStack(alignment: .leading, spacing: 1) {
                Text(suggestion ? "新识别结论" : "现身份")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(Theme.textFaint)
                Text(info.title + (info.year.map { " (\($0))" } ?? ""))
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(suggestion ? Self.suggestionTint : Theme.text.opacity(0.85))
                    .lineLimit(2)
                if let tmdbId = info.tmdbId {
                    Text("tmdb \(String(tmdbId))").font(.caption).foregroundStyle(Theme.textFaint)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func resolve(accept: Bool) async {
        await issueRun(busy: $busy, error: $error, onChanged: onChanged) {
            _ = try await api.libraryIdentificationResolveReview(body: .init(
                fileIds: group.fileIds,
                decision: accept ? "accept_suggestion" : "keep_current"
            ))
        }
    }
}

// MARK: - 已忽略组

/// 已忽略组：只有一个动作「恢复」——归档视图不该再摆认领/搜索那一套。
private struct IssueIgnoredGroupRow: View {
    let group: API.UnidentifiedGroupView
    let onChanged: () async -> Void

    @Environment(\.api) private var api
    @State private var busy = false
    @State private var error: String?
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(group.label)
                .font(.subheadline)
                .foregroundStyle(Theme.text.opacity(0.7))
                .lineLimit(2)
            HStack(spacing: 10) {
                Text("\(group.fileCount) 个文件 · \(Formatters.bytes(group.totalSizeBytes))")
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
                Spacer(minLength: 0)
                if group.fileCount > 1 {
                    Button { expanded.toggle() } label: {
                        Text(expanded ? "收起文件" : "查看文件")
                            .font(.caption)
                            .foregroundStyle(Theme.textMuted)
                            .expandedHitArea(vertical: 14)
                    }
                    .buttonStyle(.plain)
                }
                Button("恢复") { Task { await restore() } }
                    .buttonStyle(.glass)
                    .controlSize(.small)
                    .disabled(busy)
            }
            if let error {
                Text(error).font(.caption).foregroundStyle(Theme.danger)
            }
            if expanded {
                IssueFileList(paths: group.files.map(\.filePath))
            }
        }
        .issueRowCard(opacity: 0.02)
    }

    private func restore() async {
        await issueRun(busy: $busy, error: $error, onChanged: onChanged) {
            _ = try await api.libraryIdentificationRestoreFiles(body: .init(fileIds: group.files.map(\.id)))
        }
    }
}
