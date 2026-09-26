import SwiftUI

/*
 媒体库管理页的「重复文件」页签（对应 Web components/library-duplicate-files.tsx，
 设计见 docs/design/library-duplicate-files.md §5 / §9）。

 页面分两层（同 Web）：
 - **落地是一张摘要**：扫过没有、上次什么时候、三档各有多少活——可以放心清理 / 建议清理 /
   需要你决定，最后一档再按取舍类型分组。摘要层请求带 limit=0，不拉明细；
 - **点进某一档才是明细**：一个条目的一季一块，电影块列文件行，剧集同构季列版本行（可「展开各集」）。
   从条目详情页带 initialItemId（Web ?item=）进来时直接落在明细层，只看这一个条目。

 数据全部来自上一轮扫描落库的结论，打开页面不会触发检测；只有扫描在跑时才每 3 秒轮询
 看进度，跑完即停。决策动作（留这个 / 整季留这个版本 / 都留着 / 整档按建议清理 / 整组都留着）
 的确认文案逐字照搬 Web；清掉的文件进回收站，7 天内可恢复。

 手机端形态取 Web max-md：文件行名字独占整行（公共前缀淡显、差异尾巴高亮，方便横向比对），
 单元内所有文件共有的规格 / 来源提到单元头上说一次；建议保留者用左侧绿色细条标出。
 */

/// 重复文件页签（Web LibraryDuplicateFiles）。libraries 用于库筛选/库名显示；initialItemId 对应 Web ?item=（只看这一个条目）。
struct ManageDuplicateFilesTab: View {
    let libraries: [API.LibraryView]?
    var initialItemId: Int? = nil
    var onCountChange: (Int) -> Void = { _ in }

    init(libraries: [API.LibraryView]?, initialItemId: Int? = nil, onCountChange: @escaping (Int) -> Void = { _ in }) {
        self.libraries = libraries
        self.initialItemId = initialItemId
        self.onCountChange = onCountChange
        _filter = State(initialValue: ManageDupFilter(itemId: initialItemId))
    }

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback

    /// 分页单位是条目（一部剧一块），不是文件
    private static let pageSize = 20
    /// 后端一次批量最多处理的文件数（与 api/routes/library_duplicates.BATCH_LIMIT 同）
    private static let batchLimit = 500

    @State private var filter: ManageDupFilter
    @State private var focus = ManageDupFocus()
    @State private var queryDraft = ""
    @State private var offset = 0
    @State private var data: API.DuplicateFilesData?
    @State private var failed = false
    /// 「展开各集」只活在客户端；翻页 / 改筛选即重置
    @State private var expanded: Set<String> = []
    @State private var busy = false
    @State private var reloadSeq = 0

    /// 明细层：选了某一档，或者从条目详情页带 ?item= 进来（只看那一个条目）
    private var detail: Bool { focus.tier != nil || filter.itemId != nil }
    private var scanning: Bool { data.map { ManageDupText.isScanning($0.scan) } ?? false }
    private var filterActive: Bool {
        !filter.q.trimmingCharacters(in: .whitespaces).isEmpty || filter.libraryId != nil || filter.itemId != nil
    }

    var body: some View {
        content
            .task(id: ManageDupQueryKey(filter: filter, focus: focus, offset: offset)) { await reload() }
            // 页面上的数字是扫描落下的结论，不会自己变——只有扫描在跑时才需要盯着看进度
            .polling(every: 3) { if scanning { await reload() } }
            .task(id: queryDraft) {
                try? await Task.sleep(for: .milliseconds(300))
                guard !Task.isCancelled else { return }
                if filter.q != queryDraft { filter.q = queryDraft }
            }
            .onChange(of: filter) {
                offset = 0
                expanded = []
            }
            .onChange(of: focus) {
                offset = 0
                expanded = []
            }
            .onChange(of: offset) { expanded = [] }
            .onChange(of: initialItemId) { _, id in filter.itemId = id }
    }

    // MARK: 加载

    private func reload() async {
        reloadSeq += 1
        let seq = reloadSeq
        let current = filter, currentFocus = focus, currentOffset = offset, isDetail = detail
        let trimmed = current.q.trimmingCharacters(in: .whitespaces)
        do {
            // 摘要层不拉明细：一条聚合查询就够，几千条重复也是一瞬间
            let next = try await api.libraryDuplicatesList(
                tier: currentFocus.tier, reviewKind: currentFocus.reviewKind,
                q: trimmed.isEmpty ? nil : trimmed,
                libraryId: current.libraryId, mediaItemId: current.itemId,
                limit: isDetail ? Self.pageSize : 0, offset: isDetail ? currentOffset : 0
            )
            guard seq == reloadSeq else { return }
            failed = false
            if data != next { data = next }
            if current.itemId == nil, current.libraryId == nil, trimmed.isEmpty {
                onCountChange(next.totalFiles)
            }
            if isDetail, next.items.isEmpty, currentOffset > 0, next.totalItems > 0 {
                offset = max(0, (next.totalItems - 1) / Self.pageSize * Self.pageSize)
            }
        } catch {
            if manageBinIsCancellation(error) { return }
            if seq == reloadSeq { failed = true }
        }
    }

    /// 当前明细层对应的一档 / 一组（给标题计数与批量按钮用）
    private func focusGroup(_ data: API.DuplicateFilesData) -> API.DuplicateGroupView? {
        guard let tier = focus.tier else { return nil }
        if let kind = focus.reviewKind { return data.reviewGroups.first { $0.key == kind } }
        return data.tiers.first { $0.key == tier }
    }

    // MARK: 动作

    private func run(_ successText: String? = nil, _ action: () async throws -> API.TrashedBatchResultView) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        do {
            let result = try await action()
            let text = successText ?? ManageDupText.resolveResult(result)
            if result.failed.isEmpty { feedback.success(text) } else { feedback.error(text) }
            await reload()
        } catch {
            feedback.error(error)
        }
    }

    /// 「开始扫描」：后台作业算一轮，页面盯着进度
    private func startScan() async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        do {
            let started = try await api.libraryDuplicatesScan()
            feedback.success(started["created"] == .bool(true) ? "已开始扫描重复文件" : "扫描正在进行中")
            await reload()
        } catch {
            feedback.error(error)
        }
    }

    /// 「留这个」：留下这一个文件，单元里其余移入回收站
    private func keepFile(_ item: API.DuplicateItemView, _ unit: API.DuplicateUnitView, _ file: API.DuplicateFileView) async {
        let gone = unit.files.filter { $0.id != file.id }
        let ok = await feedback.confirm(
            "留下这个，其余移入回收站？",
            message: manageBinMessage(
                "留下 \(file.fileName)。移入回收站的文件 7 天内可恢复。",
                bullets: gone.map { "\($0.fileName) · \($0.qualityLabel) · \(libraryBytes($0.sizeBytes))" }
            ),
            confirmTitle: "移入回收站 · \(gone.count)",
            cancelTitle: "先不",
            destructive: true
        )
        guard ok else { return }
        await run {
            try await api.libraryDuplicatesResolve(body: API.DuplicateResolvePayload(
                mediaItemId: item.mediaItem.id, seasonNumber: unit.seasonNumber,
                episodeNumber: unit.episodeNumber, keepFileId: file.id
            ))
        }
    }

    /// 「整季留这个版本」：每集留该版本，缺该版本的集留建议保留者
    private func keepVersion(_ item: API.DuplicateItemView, _ season: API.DuplicateSeasonView, _ version: API.DuplicateVersionView) async {
        let facts = ManageDupText.keepVersionFacts(season, version)
        var bullets = facts.gone.prefix(8).map { "\($0.fileName) · \($0.qualityLabel) · \(libraryBytes($0.sizeBytes))" }
        if facts.gone.count > 8 { bullets.append("… 共 \(facts.gone.count) 个文件") }
        let missing = facts.missingEpisodes.map(ManageDupText.episodeLabel).joined(separator: "、")
        let description = "留下 \(version.qualityLabel) · \(version.originLabel)（\(version.episodes.count) 集）。"
            + (missing.isEmpty ? "" : " \(missing) 没有这个版本，这 \(facts.missingEpisodes.count) 集保留原有文件。")
            + " 移入回收站 \(facts.gone.count) 个文件 · \(libraryBytes(facts.bytes))，7 天内可恢复。"
        let ok = await feedback.confirm(
            "《\(item.mediaItem.title)》S\(ManageDupText.pad(season.seasonNumber)) 整季留下这个版本？",
            message: manageBinMessage(description, bullets: bullets),
            confirmTitle: "移入回收站 · \(facts.gone.count)",
            cancelTitle: "先不",
            destructive: true
        )
        guard ok else { return }
        await run {
            try await api.libraryDuplicatesResolve(body: API.DuplicateResolvePayload(
                mediaItemId: item.mediaItem.id, seasonNumber: season.seasonNumber, keepVersion: version.key
            ))
        }
    }

    /// 「都留着」：这些版本都是我要的，单元不再列出（直到下一轮扫描发现新文件）。可撤销，不弹确认
    private func keepAll(_ item: API.DuplicateItemView, _ season: API.DuplicateSeasonView) async {
        let isTv = item.mediaItem.kind == "tv"
        await run(isTv ? "整季都留着：不再列出，直到有新文件进来" : "都留着：不再列出，直到有新文件进来") {
            try await api.libraryDuplicatesResolve(body: API.DuplicateResolvePayload(
                mediaItemId: item.mediaItem.id, seasonNumber: season.seasonNumber,
                episodeNumber: isTv ? nil : 0, keepAll: true
            ))
        }
    }

    /// 一整档 / 一组按「建议保留」清理。除「可以放心清理」外都逐条列出会清掉的东西
    private func cleanGroup(_ tier: String, _ reviewKind: String?, _ group: API.DuplicateGroupView) async {
        guard let data else { return }
        let lines = ManageDupText.tierLines(data)
        let libraryName = filter.libraryId.flatMap { id in libraries?.first { $0.id == id }?.name }
        let scope = libraryName.map { "「\($0)」库" } ?? "全部库"
        let safe = tier == "safe"
        let description = "\(scope) · \(group.files) 个文件 · \(libraryBytes(group.bytes)) · "
            + "每个单元留下「建议保留」的那个 · 7 天内可在回收站恢复。"
            + (safe ? "" : " \(ManageDupText.bulkCleanNote(reviewKind))")
            + (group.files > Self.batchLimit ? " 一次最多处理 \(Self.batchLimit) 个，剩下的再点一次。" : "")
        let ok = await feedback.confirm(
            safe ? "清理\(scope)一模一样的文件？" : "按建议清理「\(group.label)」？",
            message: manageBinMessage(description, bullets: safe ? [] : lines),
            confirmTitle: "移入回收站 · \(min(group.files, Self.batchLimit))",
            cancelTitle: "先不",
            destructive: !safe
        )
        guard ok else { return }
        let libraryId = filter.libraryId
        await run {
            try await api.libraryDuplicatesResolveAll(body: API.DuplicateResolveAllPayload(
                tier: tier, reviewKind: reviewKind, libraryId: libraryId
            ))
        }
    }

    /// 一整组「都留着」：同一种取舍只回答一次，不动文件，可在文件区逐个撤销
    private func keepGroup(_ tier: String, _ reviewKind: String?, _ group: API.DuplicateGroupView) async {
        let ok = await feedback.confirm(
            "「\(group.label)」都留着？",
            message: "\(group.units) 个单元的文件全部保留、不再列为重复。不会动任何文件，之后可以在条目详情页逐个撤销。",
            confirmTitle: "都留着 · \(group.units)",
            cancelTitle: "先不"
        )
        guard ok else { return }
        let libraryId = filter.libraryId
        await run("已标记「都留着」：\(group.units) 个单元不再列为重复") {
            try await api.libraryDuplicatesResolveAll(body: API.DuplicateResolveAllPayload(
                tier: tier, reviewKind: reviewKind, libraryId: libraryId, keepAll: true
            ))
        }
    }

    // MARK: 渲染

    @ViewBuilder
    private var content: some View {
        if let data {
            if data.scan.status == nil, !scanning {
                // 从来没扫过：页面上只有一件事可做（扫描失败过的不算——那要让人看见失败原因）
                VStack(spacing: 0) {
                    scanBar(data.scan)
                    EmptyState(
                        systemImage: "square.on.square",
                        title: "还没有扫描过重复文件",
                        message: "重复检测要逐个文件比对指纹与规格，是一件要跑一会儿的事，所以由你来触发。"
                            + "扫完之后这里会按「可以放心清理 / 建议清理 / 需要你决定」分好，你再决定先做哪一档。",
                        actionTitle: "开始扫描",
                        action: { Task { await startScan() } }
                    )
                    .padding(.top, 24)
                    .disabled(busy)
                    .accessibilityIdentifier("dup-never-scanned")
                }
            } else if data.totalUnits == 0, !filterActive, !scanning {
                VStack(spacing: 0) {
                    scanBar(data.scan)
                    let hidden = ManageDupText.hiddenNote(data.scan)
                    EmptyState(
                        systemImage: "checkmark.seal",
                        title: "没有重复文件",
                        message: "每部电影、每一集都只有一个在位文件。" + (hidden.isEmpty ? "" : " \(hidden)。")
                    )
                    .padding(.top, 24)
                    .accessibilityIdentifier("dup-empty")
                }
            } else {
                loaded(data)
            }
        } else if failed {
            VStack(spacing: 12) {
                Text("重复文件加载失败").font(.subheadline).foregroundStyle(Theme.textMuted)
                Button("重试") { Task { await reload() } }
                    .buttonStyle(.glass)
                    .accessibilityIdentifier("dup-retry")
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 64)
        } else {
            HStack(spacing: 10) {
                ProgressView()
                Text("正在读取扫描结果…").font(.subheadline).foregroundStyle(Theme.textMuted)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 64)
        }
    }

    /**
     页面第一行永远回答同一个问题：这份结果是什么时候算的、要不要重算一次。
     重复检测是"跑一会儿的事"，所以它有自己的按钮、自己的进度。
     */
    private func scanBar(_ scan: API.DuplicateScanStateView) -> some View {
        let running = ManageDupText.isScanning(scan)
        return HStack(spacing: 10) {
            HStack(spacing: 8) {
                if running { ProgressView().controlSize(.small) }
                Text(ManageDupText.scanNote(scan))
                    .font(.subheadline)
                    .foregroundStyle(Theme.textMuted)
                    .lineLimit(2)
                    .accessibilityIdentifier("dup-scan-note")
                if running, let percent = scan.percent {
                    Text("\(Int(percent.rounded()))%")
                        .font(.caption)
                        .monospacedDigit()
                        .foregroundStyle(Theme.textFaint)
                        .accessibilityIdentifier("dup-scan-percent")
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            ManageBinButton(title: running ? "扫描中…" : scan.scannedAt != nil ? "重新扫描" : "开始扫描") {
                Task { await startScan() }
            }
            .disabled(busy || running)
            .accessibilityIdentifier("dup-scan")
        }
        .padding(.horizontal, Theme.pagePadding)
        .padding(.top, 16)
    }

    private func loaded(_ data: API.DuplicateFilesData) -> some View {
        let libs = libraries ?? []
        let libraryChips = libs.filter { $0.id == filter.libraryId || libs.count > 1 }
        let itemTitle = data.items.first { $0.mediaItem.id == filter.itemId }?.mediaItem.title
        return VStack(alignment: .leading, spacing: 0) {
            scanBar(data.scan)
            VStack(alignment: .leading, spacing: 12) {
                if failed {
                    ManageBinWarningBanner(text: "与后端通信失败，正在自动重试；下方显示的是最近一次成功加载的数据")
                }

                // 筛选：搜索 / 库胶囊 / 只看某条目（详情页带来的）
                ManageBinSearchField(text: $queryDraft, placeholder: "按片名或剧名搜索", identifier: "dup-search")
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        ManageBinChip(title: "全部库", active: filter.libraryId == nil) { filter.libraryId = nil }
                            .accessibilityIdentifier("dup-library-all")
                        ForEach(libraryChips, id: \.id) { lib in
                            ManageBinChip(title: lib.name, active: filter.libraryId == lib.id) {
                                filter.libraryId = filter.libraryId == lib.id ? nil : lib.id
                            }
                            .accessibilityIdentifier("dup-library-\(lib.id)")
                        }
                        if filter.itemId != nil {
                            ManageBinChip(
                                title: "只看" + (itemTitle.map { "《\($0)》" } ?? "这个条目"),
                                active: true, trailingSymbol: "xmark"
                            ) { filter.itemId = nil }
                            .accessibilityIdentifier("dup-item-filter")
                        }
                    }
                    .padding(.vertical, 2)
                }
                .scrollClipDisabled()

                if !detail {
                    tierSummary(data)
                } else {
                    detailSection(data, itemTitle: itemTitle)
                }
            }
            .padding(.horizontal, Theme.pagePadding)
            .padding(.top, 14)
        }
    }

    // MARK: 摘要层

    /**
     落地页只回答一句话：**先做哪一档**。三档从上到下就是建议的处理顺序；最后一档按取舍类型分组，
     同一种取舍一次回答一批。
     */
    private func tierSummary(_ data: API.DuplicateFilesData) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("一共 **\(data.totalUnits)** 个单元有重复，按建议处理可清掉 **\(data.totalFiles)** 个文件、腾出 **\(libraryBytes(data.totalBytes))**。从上往下做：")
                .font(.footnote)
                .monospacedDigit()
                .foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("dup-summary")

            ForEach(data.tiers, id: \.key) { tier in
                tierCard(tier, data: data)
            }

            let hidden = ManageDupText.hiddenNote(data.scan)
            if !hidden.isEmpty {
                Text(hidden).font(.caption).foregroundStyle(Theme.textFaint)
            }
        }
        .padding(.top, 4)
    }

    private func tierCard(_ tier: API.DuplicateGroupView, data: API.DuplicateFilesData) -> some View {
        let empty = tier.units == 0
        let tone = ManageDupText.tierTone(tier.key)
        return VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(tier.label).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text)
                Text(empty ? "没有" : ManageDupText.groupSummary(tier))
                    .font(.caption)
                    .monospacedDigit()
                    .foregroundStyle(Theme.textFaint)
                Spacer(minLength: 0)
            }
            if !empty {
                HStack(spacing: 6) {
                    ManageBinButton(title: "逐个看") { focus = ManageDupFocus(tier: tier.key) }
                        .disabled(busy)
                        .accessibilityIdentifier("dup-tier-open-\(tier.key)")
                    if ManageDupText.allowsBulkClean(tier.key, nil) {
                        // 「可以放心清理」是这一页唯一"做了不会丢东西"的批量动作，主操作；
                        // 「建议清理」动的是**有区别**的文件，依据只是机器的建议——危险档
                        ManageBinButton(
                            title: "\(ManageDupText.tierActionLabel(tier.key)) · \(tier.files)",
                            tone: tier.key == "safe" ? .primary : .dangerSolid
                        ) {
                            Task { await cleanGroup(tier.key, nil, tier) }
                        }
                        .disabled(busy)
                        .accessibilityIdentifier("dup-tier-clean-\(tier.key)")
                    }
                }
            }
            Text(tier.hint)
                .font(.caption)
                .foregroundStyle(Theme.textFaint)
                .fixedSize(horizontal: false, vertical: true)

            // 「需要你决定」再按取舍类型分组：一组两行——名字与分量一行、动作一行
            if tier.key == "review", !data.reviewGroups.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(data.reviewGroups, id: \.key) { group in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack(alignment: .firstTextBaseline, spacing: 10) {
                                Text(group.label).font(.footnote).foregroundStyle(Theme.text)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                Text(ManageDupText.compactSummary(group))
                                    .font(.caption)
                                    .monospacedDigit()
                                    .foregroundStyle(Theme.textFaint)
                            }
                            HStack(spacing: 6) {
                                // 逐个看 = 这一组的正路（玻璃按钮）；都留着 = 关掉它，不动文件（幽灵）
                                ManageBinButton(title: "逐个看") {
                                    focus = ManageDupFocus(tier: "review", reviewKind: group.key)
                                }
                                .disabled(busy)
                                .accessibilityIdentifier("dup-group-open-\(group.key)")
                                ManageBinButton(title: "都留着", tone: .ghost) {
                                    Task { await keepGroup("review", group.key, group) }
                                }
                                .disabled(busy)
                                .accessibilityIdentifier("dup-group-keep-\(group.key)")
                                if ManageDupText.allowsBulkClean("review", group.key) {
                                    ManageBinButton(title: "按建议清 · \(group.files)", tone: .dangerSolid) {
                                        Task { await cleanGroup("review", group.key, group) }
                                    }
                                    .disabled(busy)
                                    .accessibilityIdentifier("dup-group-clean-\(group.key)")
                                }
                            }
                        }
                    }
                }
                .padding(.top, 10)
                .overlay(alignment: .top) { Rectangle().fill(Color.white.opacity(0.06)).frame(height: 1) }
                .padding(.top, 2)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(empty ? Color.white.opacity(0.01) : tone.opacity(0.06), in: .rect(cornerRadius: Theme.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Theme.cardRadius).strokeBorder(empty ? Color.white.opacity(0.06) : tone.opacity(0.35)))
        .opacity(empty ? 0.6 : 1)
        .accessibilityIdentifier("dup-tier-\(tier.key)")
    }

    // MARK: 明细层

    private func detailSection(_ data: API.DuplicateFilesData, itemTitle: String?) -> some View {
        let group = focusGroup(data)
        let pageCount = max(1, Int((Double(data.totalItems) / Double(Self.pageSize)).rounded(.up)))
        return VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 8) {
                if focus.tier != nil {
                    tierNav(data, group: group)
                } else {
                    Text(itemTitle.map { "《\($0)》的重复文件" } ?? "重复文件")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.text)
                }
                if let group {
                    Text(ManageDupText.groupSummary(group))
                        .font(.caption)
                        .monospacedDigit()
                        .foregroundStyle(Theme.textFaint)
                }
                if let tier = focus.tier, let group, group.files > 0 {
                    HStack(spacing: 6) {
                        if tier == "review" {
                            ManageBinButton(title: "整组都留着", tone: .ghost) {
                                Task { await keepGroup(tier, focus.reviewKind, group) }
                            }
                            .disabled(busy)
                            .accessibilityIdentifier("dup-focus-keep-all")
                        }
                        if ManageDupText.allowsBulkClean(tier, focus.reviewKind) {
                            ManageBinButton(
                                title: "\(ManageDupText.tierActionLabel(tier)) · \(group.files)",
                                tone: tier == "safe" ? .primary : .dangerSolid,
                                fill: true
                            ) {
                                Task { await cleanGroup(tier, focus.reviewKind, group) }
                            }
                            .disabled(busy)
                            .accessibilityIdentifier("dup-focus-clean")
                        }
                    }
                }
                // 说明只在"你正要动手"的地方出现一次：三档的那句摘要卡上讲过，手机上这里不重复；
                // 取舍分组的那句摘要卡上没讲，点进某一组时要显示出来
                if focus.reviewKind != nil, let hint = group?.hint, !hint.isEmpty {
                    Text(hint)
                        .font(.caption)
                        .foregroundStyle(Theme.textFaint)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            if data.items.isEmpty {
                HStack(spacing: 8) {
                    Text("这里已经没有待处理的重复文件").foregroundStyle(Theme.textFaint)
                    if filterActive {
                        Button("清除筛选") {
                            queryDraft = ""
                            filter = ManageDupFilter()
                        }
                        .foregroundStyle(Theme.info)
                        .accessibilityIdentifier("dup-clear-filter")
                    }
                }
                .font(.subheadline)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 28)
                .padding(.horizontal, 16)
                .overlay(
                    RoundedRectangle(cornerRadius: Theme.cardRadius)
                        .strokeBorder(Color.white.opacity(0.08), style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
                )
            } else {
                LazyVStack(spacing: 10) {
                    ForEach(data.items, id: \.mediaItem.id) { item in
                        ForEach(item.seasons, id: \.self) { season in
                            let key = "\(item.mediaItem.id):\(season.seasonNumber):\(season.bucket)"
                            ManageDupSeasonBlock(
                                item: item,
                                season: season,
                                blockKey: key,
                                expanded: expanded.contains(key),
                                busy: busy,
                                onToggleExpanded: {
                                    if expanded.contains(key) { expanded.remove(key) } else { expanded.insert(key) }
                                },
                                onKeepFile: { unit, file in Task { await keepFile(item, unit, file) } },
                                onKeepVersion: { version in Task { await keepVersion(item, season, version) } },
                                onKeepAll: { Task { await keepAll(item, season) } }
                            )
                        }
                    }
                }
            }

            VStack(alignment: .leading, spacing: 8) {
                Text(
                    [ManageDupText.hiddenNote(data.scan), "清理的文件进回收站，7 天内可恢复"].filter { !$0.isEmpty }.joined(separator: " · ")
                        + (data.totalItems > Self.pageSize ? " · 第 \(offset + 1)–\(offset + data.items.count) 个条目，共 \(data.totalItems) 个" : "")
                )
                .font(.caption)
                .monospacedDigit()
                .foregroundStyle(Theme.textFaint)
                .fixedSize(horizontal: false, vertical: true)
                if pageCount > 1 {
                    ManageBinPager(page: offset / Self.pageSize, count: pageCount, numbered: false, idPrefix: "dup-page") { p in
                        offset = p * Self.pageSize
                    }
                }
            }
            .padding(.top, 4)
        }
        .padding(.top, 4)
    }

    /**
     分档导航：第一枚回摘要，其余三枚直接换档（选中的那枚同时就是标题）；聚焦到某一种取舍时
     再多一枚可点掉的组名。窄屏横滚，切换时把选中项滚进视野。
     */
    private func tierNav(_ data: API.DuplicateFilesData, group: API.DuplicateGroupView?) -> some View {
        ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ManageBinChip(title: "摘要", active: false, leadingSymbol: "chevron.left") { focus = ManageDupFocus() }
                        .accessibilityIdentifier("dup-nav-summary")
                    Rectangle().fill(Color.white.opacity(0.12)).frame(width: 1, height: 16).padding(.horizontal, 2)
                    ForEach(data.tiers, id: \.key) { t in
                        ManageBinChip(
                            title: t.label + (t.units > 0 ? " \(t.units)" : ""),
                            active: focus.tier == t.key && focus.reviewKind == nil
                        ) { focus = ManageDupFocus(tier: t.key) }
                        .id("tier-\(t.key)")
                        .accessibilityIdentifier("dup-nav-\(t.key)")
                    }
                    if focus.reviewKind != nil, let group {
                        ManageBinChip(title: group.label, active: true, trailingSymbol: "xmark") {
                            focus = ManageDupFocus(tier: "review")
                        }
                        .id("group")
                        .accessibilityIdentifier("dup-nav-group")
                    }
                }
                .padding(.vertical, 2)
            }
            .scrollClipDisabled()
            .onAppear { scrollToFocus(proxy) }
            .onChange(of: focus) { scrollToFocus(proxy) }
        }
    }

    private func scrollToFocus(_ proxy: ScrollViewProxy) {
        guard let tier = focus.tier else { return }
        withAnimation { proxy.scrollTo(focus.reviewKind != nil ? "group" : "tier-\(tier)", anchor: .center) }
    }
}

// MARK: - 状态

/// 筛选：搜索词 / 库 / 只看某条目
struct ManageDupFilter: Equatable {
    var q = ""
    var libraryId: Int?
    var itemId: Int?
}

/// 当前站在哪一层：摘要页（tier=nil），还是某一档（可能再细到某一种取舍）的明细
struct ManageDupFocus: Equatable {
    var tier: String?
    var reviewKind: String?
}

private struct ManageDupQueryKey: Equatable {
    var filter: ManageDupFilter
    var focus: ManageDupFocus
    var offset: Int
}

// MARK: - 块：一个条目的一季（电影就是条目本身）

private struct ManageDupSeasonBlock: View {
    let item: API.DuplicateItemView
    let season: API.DuplicateSeasonView
    let blockKey: String
    let expanded: Bool
    let busy: Bool
    let onToggleExpanded: () -> Void
    let onKeepFile: (API.DuplicateUnitView, API.DuplicateFileView) -> Void
    let onKeepVersion: (API.DuplicateVersionView) -> Void
    let onKeepAll: () -> Void

    @Environment(\.api) private var api
    @Environment(Router.self) private var router

    var body: some View {
        let media = item.mediaItem
        let isTv = media.kind == "tv"
        let showVersions = season.uniform && !expanded
        // 几个版本行来源相同时（同一轮扫描发现的多个包），在块头说一次，行里就不再各印一遍
        let commonOrigin = ManageDupText.sharedVersionOrigin(season.versions)
        VStack(spacing: 0) {
            header(isTv: isTv, commonOrigin: showVersions ? commonOrigin : nil)
            Divider().overlay(Color.white.opacity(0.07))
            if showVersions {
                let suggested = season.versions.first(where: \.suggested)
                ForEach(Array(season.versions.enumerated()), id: \.element.key) { index, version in
                    if index > 0 { Divider().overlay(Color.white.opacity(0.06)) }
                    versionRow(version, suggested: suggested, hideOrigin: commonOrigin != nil)
                }
            } else {
                ForEach(Array(season.units.enumerated()), id: \.offset) { _, unit in
                    unitSection(unit, isTv: isTv)
                }
            }
            HStack {
                Spacer()
                ManageBinButton(title: isTv ? "整季都留着" : "都留着", tone: .ghost, action: onKeepAll)
                    .disabled(busy)
                    .accessibilityIdentifier("dup-keep-all-\(blockKey)")
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 6)
            .background(Color.black.opacity(0.15))
            .overlay(alignment: .top) { Rectangle().fill(Color.white.opacity(0.06)).frame(height: 1) }
        }
        .background(Color.white.opacity(0.02), in: .rect(cornerRadius: Theme.cardRadius))
        .overlay(RoundedRectangle(cornerRadius: Theme.cardRadius).strokeBorder(Theme.line))
        .clipShape(.rect(cornerRadius: Theme.cardRadius))
    }

    private func header(isTv: Bool, commonOrigin: String?) -> some View {
        let media = item.mediaItem
        let headline = ManageDupText.seasonHeadline(season, kind: media.kind)
        return HStack(spacing: 10) {
            RemoteImage(url: api.image(media.posterUrl, .posterCard))
                .frame(width: 26, height: 38)
                .clipShape(.rect(cornerRadius: 4))
                .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(Theme.line))
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Button {
                        router.push(.libraryItem(libraryId: item.library.id, itemId: media.id))
                    } label: {
                        Text(media.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text).lineLimit(1)
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("dup-item-title-\(blockKey)")
                    if let year = media.year {
                        Text(String(year)).font(.caption).foregroundStyle(Theme.textFaint)
                    }
                }
                Text(headerSubtitle(headline))
                    .font(.caption)
                    .lineLimit(1)
                if let commonOrigin {
                    Text(commonOrigin).font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            if season.uniform {
                ManageBinButton(title: expanded ? "收起各集" : "展开各集", tone: .ghost, action: onToggleExpanded)
                    .accessibilityIdentifier("dup-expand-\(blockKey)")
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(Color.white.opacity(0.02))
    }

    private func headerSubtitle(_ headline: String) -> AttributedString {
        var out = manageBinRun(item.library.name, Theme.textFaint)
        if !headline.isEmpty {
            out += manageBinRun(" · ", Theme.textFaint)
            out += manageBinRun(headline, Theme.text)
        }
        return out
    }

    /// 版本行：规格（与建议保留版本不同的段加亮）+ 动作一行；覆盖（集数 · 大小）与来源排在下面
    private func versionRow(_ version: API.DuplicateVersionView, suggested: API.DuplicateVersionView?, hideOrigin: Bool) -> some View {
        let refParts = suggested.map { $0.qualityLabel.split(separator: " ").map(String.init) }
        let segments = version.qualityLabel.split(separator: " ").map(String.init).enumerated().map { i, text in
            ManageDupSegment(
                text: text,
                diff: refParts != nil && !version.suggested && (i < refParts!.count ? refParts![i] : nil) != text
            )
        }
        return VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .center, spacing: 8) {
                Text(ManageDupText.segmentsText(segments))
                    .font(.footnote)
                    .lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if version.suggested { ManageDupTag(title: "建议保留", keep: true) }
                ManageBinButton(title: "整季留这个") { onKeepVersion(version) }
                    .disabled(busy)
                    .accessibilityIdentifier("dup-keep-version-\(blockKey)-\(version.key)")
            }
            Text(manageBinRun("\(version.episodes.count) 集", Theme.text, .footnote.weight(.semibold))
                + manageBinRun(" · \(libraryBytes(version.bytes))", Theme.textMuted))
                .font(.footnote)
                .monospacedDigit()
            if !hideOrigin {
                Text(version.originLabel).font(.footnote).foregroundStyle(Theme.textMuted).lineLimit(1)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }

    /// 一个单元：剧集先一条集号头（顺带共有的规格 / 来源），电影只在有共有信息时才有头
    @ViewBuilder
    private func unitSection(_ unit: API.DuplicateUnitView, isTv: Bool) -> some View {
        let shared = ManageDupText.sharedFacts(unit.files)
        let common = [shared.quality, shared.origin].compactMap { $0 }.joined(separator: " · ")
        let prefix = ManageDupText.commonNamePrefix(unit.files)
        VStack(spacing: 0) {
            if isTv || !common.isEmpty {
                Text(([isTv ? ManageDupText.episodeLabel(unit.episodeNumber) : ""] + [common]).filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(.caption2)
                    .tracking(0.3)
                    .foregroundStyle(Theme.textFaint)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 4)
                    .background(Color.white.opacity(0.012))
                Divider().overlay(Color.white.opacity(0.06))
            }
            ForEach(Array(unit.files.enumerated()), id: \.element.id) { index, file in
                if index > 0 { Divider().overlay(Color.white.opacity(0.06)) }
                ManageDupFileRow(
                    unit: unit, file: file, shared: shared, namePrefix: prefix, busy: busy,
                    onKeep: { onKeepFile(unit, file) }
                )
            }
        }
    }
}

/**
 文件行（Web FileRow 的窄屏形态）：
     三体 S01E16 - 2160p H.265 AAC ADWeb.mp4          ← 名字独占整行（公共前缀淡显，差异尾巴亮显）
     2160p · WEB-DL · AAC 2.0            1.22 GB · 8.1 Mbps   ← 规格（共有时隐藏）；体积码率钉在行尾不截断
     监听目录自动识别入库 / 建议保留 · 同档，最近入库  [留这个]
 建议保留者左侧一道绿色细条；「都留着」过的整行半透明。点名字弹出原始文件名与完整路径。
 */
private struct ManageDupFileRow: View {
    let unit: API.DuplicateUnitView
    let file: API.DuplicateFileView
    let shared: ManageDupSharedFacts
    let namePrefix: String
    let busy: Bool
    let onKeep: () -> Void

    @State private var showPath = false

    var body: some View {
        let reference = ManageDupText.suggestedOf(unit)
        let note = ManageDupText.fileNote(file)
        let live = unit.files.filter { $0.keptAt == nil }.count
        let tail = namePrefix.isEmpty ? file.fileName : String(file.fileName.dropFirst(namePrefix.count))
        VStack(alignment: .leading, spacing: 4) {
            Button { showPath = true } label: {
                Text(manageBinRun(namePrefix, Theme.textFaint) + manageBinRun(tail, Theme.text))
                    .font(.caption.monospaced())
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("查看「\(file.fileName)」的原始文件名与路径")
            .accessibilityIdentifier("dup-file-name-\(file.id)")
            .popover(isPresented: $showPath) {
                VStack(alignment: .leading, spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("原始文件名").foregroundStyle(Theme.textFaint)
                        Text(file.fileName).font(.caption.monospaced()).foregroundStyle(Theme.text)
                    }
                    VStack(alignment: .leading, spacing: 2) {
                        Text("完整路径").foregroundStyle(Theme.textFaint)
                        Text(file.filePath).font(.caption.monospaced()).foregroundStyle(Theme.textMuted)
                    }
                }
                .font(.caption)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .frame(width: 300, alignment: .leading)
                .padding(14)
                .presentationCompactAdaptation(.popover)
            }

            if shared.quality == nil {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(ManageDupText.segmentsText(ManageDupText.specSegments(file, reference: reference)))
                        .font(.footnote)
                        .lineLimit(1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Text(ManageDupText.volumeSegments(file).map(\.text).joined(separator: " · "))
                        .font(.footnote)
                        .monospacedDigit()
                        .foregroundStyle(Theme.text)
                        .lineLimit(1)
                        .fixedSize()
                }
            }

            HStack(alignment: .center, spacing: 8) {
                VStack(alignment: .leading, spacing: 1) {
                    if shared.origin == nil {
                        Text(file.origin.label).font(.footnote).foregroundStyle(Theme.textMuted).lineLimit(1)
                    }
                    if !note.isEmpty {
                        Text(note).font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                if live > 1 || unit.files.count > 1 {
                    ManageBinButton(title: "留这个", action: onKeep)
                        .disabled(busy)
                        .accessibilityIdentifier("dup-keep-file-\(file.id)")
                }
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .overlay(alignment: .leading) {
            // 窄屏不显示「建议保留」胶囊（说明行里已经写着），改用一道内嵌色条标出它
            if file.suggested {
                Rectangle().fill(Theme.success.opacity(0.45)).frame(width: 2)
            }
        }
        .opacity(file.keptAt != nil ? 0.55 : 1)
    }
}

/// 「建议保留」「你留下的」小标签
private struct ManageDupTag: View {
    let title: String
    let keep: Bool

    var body: some View {
        let color = keep ? Theme.success : Color(red: 0xE8 / 255, green: 0xC9 / 255, blue: 0x8A / 255)
        Text(title)
            .font(.caption2)
            .foregroundStyle(color)
            .lineLimit(1)
            .fixedSize()
            .padding(.horizontal, 6)
            .padding(.vertical, 1)
            .overlay(Capsule().strokeBorder(color.opacity(0.45)))
    }
}

// MARK: - 纯函数（Web lib/library-duplicates.ts 逐条移植）

/// 规格的一段：与建议保留者对应位不同的段加亮
struct ManageDupSegment {
    var text: String
    var diff: Bool
}

/// 单元内所有文件都一样的规格与来源（只有一个文件时不算共有）
struct ManageDupSharedFacts {
    var quality: String?
    var origin: String?
}

/// 「整季留这个版本」确认框的事实
struct ManageDupKeepVersionFacts {
    var gone: [API.DuplicateFileView]
    var bytes: Int
    var missingEpisodes: [Int]
}

enum ManageDupText {
    static func pad(_ n: Int) -> String { String(format: "%02d", n) }

    /// 集号 `E03`；电影为空串
    static func episodeLabel(_ episode: Int) -> String {
        episode > 0 ? "E\(pad(episode))" : ""
    }

    /// 块标题的第二段：剧集 `S01 · 3 集有重复`；电影空
    static func seasonHeadline(_ season: API.DuplicateSeasonView, kind: String) -> String {
        guard kind == "tv" else { return "" }
        return "S\(pad(season.seasonNumber)) · \(season.units.count) 集有重复"
    }

    /// `12 个单元 · 14 个文件 · 31 GB`；没有活时返回空串
    static func groupSummary(_ group: API.DuplicateGroupView) -> String {
        guard group.units > 0 else { return "" }
        var parts = ["\(group.units) 个单元", "\(group.files) 个文件"]
        if group.bytes > 0 { parts.append(libraryBytes(group.bytes)) }
        return parts.joined(separator: " · ")
    }

    /// 取舍分组行上的计数：`562 个单元 · 933 GB`（文件数写在「按建议清 · N」上）
    static func compactSummary(_ group: API.DuplicateGroupView) -> String {
        guard group.units > 0 else { return "" }
        var parts = ["\(group.units) 个单元"]
        if group.bytes > 0 { parts.append(libraryBytes(group.bytes)) }
        return parts.joined(separator: " · ")
    }

    /// 这个作用域给不给「按建议成批清理」：只有停在「需要你决定」**整档**不给
    static func allowsBulkClean(_ tier: String, _ reviewKind: String?) -> Bool {
        tier != "review" || reviewKind != nil
    }

    /// 成批清理确认框里那句"依据"：只有「规格不全」要单独说
    static func bulkCleanNote(_ reviewKind: String?) -> String {
        if reviewKind == "unknown" {
            return "这一组机器没比出档位：留下的那个只是按实测码率、来源、文件名挑的，不是画质判断。"
        }
        return "这些文件与保留者有区别；想留的请先取消，回去点那个单元的「都留着」。"
    }

    /// 一档的动作按钮文案：safe 是没风险的清理，另两档都在动"有区别"的文件
    static func tierActionLabel(_ tier: String) -> String {
        tier == "safe" ? "全部清理" : "全部按建议清理"
    }

    /// 三档的配色：放心清绿、建议清金、要你决定中性
    static func tierTone(_ tier: String) -> Color {
        switch tier {
        case "safe": Theme.success
        case "suggested": Color(red: 0xE8 / 255, green: 0xC9 / 255, blue: 0x8A / 255)
        default: Color.white.opacity(0.5)
        }
    }

    /// 页脚那句"不在这里显示的"：两段都为 0 时返回空串
    static func hiddenNote(_ scan: API.DuplicateScanStateView) -> String {
        var parts: [String] = []
        if scan.upgradingUnits > 0 { parts.append("\(scan.upgradingUnits) 个单元正在洗版验证中") }
        if scan.keepOldItems > 0 { parts.append("\(scan.keepOldItems) 个条目按规则组「保留共存」") }
        return parts.isEmpty ? "" : "\(parts.joined(separator: "、"))，不在这里显示"
    }

    static func isScanning(_ scan: API.DuplicateScanStateView) -> Bool {
        guard let status = scan.status else { return false }
        return !["succeeded", "failed", "cancelled"].contains(status)
    }

    /// 扫描状态那一行：从未扫描 / 正在跑（带进度）/ 上次没跑成 / 上次扫描于何时
    static func scanNote(_ scan: API.DuplicateScanStateView) -> String {
        if isScanning(scan) {
            if let message = scan.message, !message.isEmpty { return "正在扫描 · \(message)" }
            return "正在扫描…"
        }
        if scan.status == "failed" || scan.status == "cancelled" {
            let why = scan.message.flatMap { $0.isEmpty ? nil : "：\($0)" } ?? ""
            let had = scan.scannedAt.map { "，下面是 \(relativeTime($0))的结果" } ?? ""
            return "上次扫描\(scan.status == "failed" ? "失败" : "被取消")\(why)\(had)"
        }
        guard let scannedAt = scan.scannedAt else { return "还没有扫描过" }
        return "上次扫描：\(relativeTime(scannedAt))"
    }

    /// 「3 分钟前」「2 小时前」「3 天前」——用户只想知道"新不新鲜"
    static func relativeTime(_ iso: String, now: Date = .now) -> String {
        guard let then = Formatters.date(iso) else { return iso }
        let seconds = max(0, Int(now.timeIntervalSince(then).rounded()))
        if seconds < 60 { return "刚刚" }
        if seconds < 3600 { return "\(seconds / 60) 分钟前" }
        if seconds < 86400 { return "\(seconds / 3600) 小时前" }
        if seconds < 86400 * 30 { return "\(seconds / 86400) 天前" }
        let c = Calendar(identifier: .gregorian).dateComponents(in: .current, from: then)
        return "\(c.year ?? 0)/\(c.month ?? 0)/\(c.day ?? 0)"
    }

    /// 规格里认得出是什么的那几段：分辨率 / 片源 / HDR / 音轨（可以截断）
    static func specSegments(_ file: API.DuplicateFileView, reference: API.DuplicateFileView?) -> [ManageDupSegment] {
        let own = file.qualityLabel.split(separator: " ").map(String.init)
        let ref = reference.flatMap { $0.id == file.id ? nil : $0.qualityLabel.split(separator: " ").map(String.init) }
        var segments = own.enumerated().map { i, text in
            ManageDupSegment(text: text, diff: ref != nil && (i < ref!.count ? ref![i] : nil) != text)
        }
        if let audio = file.audioLabel, !audio.isEmpty {
            segments.append(ManageDupSegment(text: audio, diff: ref != nil && reference?.audioLabel != file.audioLabel))
        }
        return segments
    }

    /// 体积与码率：逐个清点时最硬的两个数，绝不截断
    static func volumeSegments(_ file: API.DuplicateFileView) -> [ManageDupSegment] {
        var segments = [ManageDupSegment(text: libraryBytes(file.sizeBytes), diff: false)]
        if let rate = file.bitRate, rate > 0 { segments.append(ManageDupSegment(text: formatBitRate(rate), diff: false)) }
        return segments
    }

    static func formatBitRate(_ bps: Int) -> String {
        if bps >= 1_000_000 { return String(format: "%.1f Mbps", Double(bps) / 1_000_000) }
        return "\(Int((Double(bps) / 1000).rounded())) kbps"
    }

    /// 一个文件的规格整句，用来比对"几个文件是不是一样"
    static func specText(_ file: API.DuplicateFileView) -> String {
        (specSegments(file, reference: nil) + volumeSegments(file)).map(\.text).joined(separator: " · ")
    }

    static func sharedFacts(_ files: [API.DuplicateFileView]) -> ManageDupSharedFacts {
        guard files.count >= 2 else { return ManageDupSharedFacts() }
        let specs = Set(files.map(specText))
        let origins = Set(files.map(\.origin.label))
        return ManageDupSharedFacts(
            quality: specs.count == 1 ? specText(files[0]) : nil,
            origin: origins.count == 1 ? files[0].origin.label : nil
        )
    }

    /**
     单元内几个文件名的最长公共前缀：前缀短于 8 个字符不折；任何一个文件的尾巴为空也不折；
     差异尾巴超过 28 个字符不折（尾巴不截断，太长会把行撑破）。
     */
    static func commonNamePrefix(_ files: [API.DuplicateFileView]) -> String {
        guard files.count >= 2 else { return "" }
        let names = files.map { Array($0.fileName) }
        let shortest = names.map(\.count).min() ?? 0
        var i = 0
        while i < shortest, names.allSatisfy({ $0[i] == names[0][i] }) { i += 1 }
        if i < 8 || names.contains(where: { $0.count == i }) { return "" }
        if (names.map { $0.count - i }.max() ?? 0) > 28 { return "" }
        return String(names[0][0 ..< i])
    }

    /// 同构季里几个版本行共有的来源；不同则为 nil
    static func sharedVersionOrigin(_ versions: [API.DuplicateVersionView]) -> String? {
        guard versions.count >= 2 else { return nil }
        return Set(versions.map(\.originLabel)).count == 1 ? versions[0].originLabel : nil
    }

    /// 单元里的建议保留者（没有时取第一个，后端保证一定有）
    static func suggestedOf(_ unit: API.DuplicateUnitView) -> API.DuplicateFileView? {
        unit.files.first(where: \.suggested) ?? unit.files.first
    }

    /// 文件行说明：「都留着」过的写"你留下的"；建议保留写依据；否则空
    static func fileNote(_ file: API.DuplicateFileView) -> String {
        if file.keptAt != nil { return "你留下的" }
        if file.suggested, let reason = file.suggestReason, !reason.isEmpty { return "建议保留 · \(reason)" }
        return ""
    }

    /// 「整季留这个版本」：每集留该版本，缺该版本的集留建议保留者
    static func keepVersionFacts(_ season: API.DuplicateSeasonView, _ version: API.DuplicateVersionView) -> ManageDupKeepVersionFacts {
        var gone: [API.DuplicateFileView] = []
        var missing: [Int] = []
        for unit in season.units {
            let target = unit.files.first { $0.versionKey == version.key }
            if target == nil { missing.append(unit.episodeNumber) }
            guard let keep = target ?? suggestedOf(unit) else { continue }
            gone += unit.files.filter { $0.id != keep.id && $0.keptAt == nil }
        }
        return ManageDupKeepVersionFacts(gone: gone, bytes: gone.reduce(0) { $0 + $1.sizeBytes }, missingEpisodes: missing)
    }

    /// 整档按建议清理的确认清单：本页看得到的条目逐条列出会清掉什么（只列本页）
    static func tierLines(_ data: API.DuplicateFilesData) -> [String] {
        var lines: [String] = []
        for item in data.items {
            for season in item.seasons {
                let extras = season.units.flatMap { $0.files.filter { !$0.suggested && $0.keptAt == nil } }
                if extras.isEmpty { continue }
                let head = item.mediaItem.kind == "tv" ? "\(item.mediaItem.title) S\(pad(season.seasonNumber))" : item.mediaItem.title
                var seen: [String] = []
                for label in extras.map(\.qualityLabel) where !seen.contains(label) { seen.append(label) }
                let bytes = libraryBytes(extras.reduce(0) { $0 + $1.sizeBytes })
                lines.append("\(head) · \(extras.count) 个文件 · \(bytes) · \(seen.joined(separator: " / "))")
            }
        }
        return lines
    }

    /// 批量结果：`已移入回收站 5 个文件` / `…，2 个失败：原因` / `…，还有 N 个未处理（再点一次即可）`
    static func resolveResult(_ result: API.TrashedBatchResultView) -> String {
        var text = "已移入回收站 \(result.done) 个文件"
        if let first = result.failed.first { text += "，\(result.failed.count) 个失败：\(first.error)" }
        if result.remaining > 0 { text += "，还有 \(result.remaining) 个未处理（再点一次即可）" }
        return text
    }

    /// 规格段渲染：第一段加粗、与建议保留者不同的段警示色加粗、其余次要色，以 · 相连
    static func segmentsText(_ segments: [ManageDupSegment]) -> AttributedString {
        var out = AttributedString()
        for (i, seg) in segments.enumerated() {
            if i > 0 { out += manageBinRun(" · ", Theme.textMuted) }
            if seg.diff {
                out += manageBinRun(seg.text, Theme.warning, .footnote.weight(.semibold))
            } else if i == 0 {
                out += manageBinRun(seg.text, Theme.text, .footnote.weight(.semibold))
            } else {
                out += manageBinRun(seg.text, Theme.textMuted)
            }
        }
        return out
    }
}
