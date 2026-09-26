import SwiftUI

/*
 媒体库管理页的「回收站」页签（对应 Web components/library-recycle-bin.tsx，
 设计见 docs/design/library-recycle-bin.md）。

 设计要点（逐条对齐 Web 手机视口的形态）：
 - 跨库汇总全部待回收文件，一个条目一张卡、多文件可展开到每集；摘要行常驻回答
   "多少、多大、多急"，右侧是页面唯一的页级动作「立即清理全部」；
 - 列表只打一个接口（/libraries/trashed-files），聚合口径与「立即清理全部」的作用域是
   同一份数字；by_library / by_reason 是分面计数，切换一组胶囊另一组不归零；
 - 搜索即时回显、请求按 300ms 去抖；分页单位是条目（一部剧一行），页大小 20；
 - 展开与勾选只活在客户端，翻页 / 改筛选即重置；乱序守卫保证慢响应不覆盖新一轮结果；
 - 可见期间 30 秒轮询（同 Web useVisiblePolling）；每次成功都把 total_files 回报给页签计数。

 与 Web 的差异：Web 的批量条是底部悬浮（sticky），这里的根视图被放在外层 ScrollView 里，
 做不了悬浮，所以把「全选本页 + 批量恢复 / 清理」合成列表卡片的表头行，勾选后就地出现。

 本文件末尾还有几个与「重复文件」页签共用的小部件（ManageBin 前缀）：搜索框、胶囊、
 动作按钮、分页器、复选框。
 */

// MARK: - 页签

/// 回收站页签（Web LibraryRecycleBin）。onCountChange 回报 total_files，用于页签上的计数。
struct ManageRecycleBinTab: View {
    var onCountChange: (Int) -> Void = { _ in }

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback

    /// 分页单位是条目（一部剧一行），不是文件
    private static let pageSize = 20

    @State private var filter = ManageRecycleFilter()
    /// 搜索框即时回显，请求按 300ms 去抖
    @State private var queryDraft = ""
    @State private var offset = 0
    @State private var data: API.TrashedFilesData?
    @State private var failed = false
    // 展开的条目键与勾选的文件 id 都只活在客户端；翻页 / 改筛选即重置
    @State private var expanded: Set<String> = []
    @State private var selected: Set<Int> = []
    @State private var busy = false
    /// 乱序守卫：与管理页库列表同一套，慢响应不能覆盖新一轮结果
    @State private var reloadSeq = 0

    var body: some View {
        content
            .task(id: ManageRecycleQueryKey(filter: filter, offset: offset)) { await reload() }
            .polling(every: 30) { await reload() }
            .task(id: queryDraft) {
                try? await Task.sleep(for: .milliseconds(300))
                guard !Task.isCancelled else { return }
                if filter.q != queryDraft { filter.q = queryDraft }
            }
            // 筛选变化回到第一页；翻页与筛选都清掉展开 / 勾选态
            .onChange(of: filter) {
                offset = 0
                expanded = []
                selected = []
            }
            .onChange(of: offset) {
                expanded = []
                selected = []
            }
    }

    // MARK: 加载

    private func reload() async {
        reloadSeq += 1
        let seq = reloadSeq
        let current = filter
        let currentOffset = offset
        do {
            let next = try await api.libraryRecycleList(
                q: current.apiQuery, libraryId: current.libraryId, reason: current.reason,
                limit: Self.pageSize, offset: currentOffset
            )
            guard seq == reloadSeq else { return }
            failed = false
            if data != next { data = next }
            onCountChange(next.totalFiles)
            // 当前页在磁盘上已被清掉（如清理全部后本页为空但总数不为 0）：回退到最后一页
            if next.items.isEmpty, currentOffset > 0, next.totalItems > 0 {
                offset = max(0, (next.totalItems - 1) / Self.pageSize * Self.pageSize)
            }
        } catch {
            if manageBinIsCancellation(error) { return }
            if seq == reloadSeq { failed = true }
        }
    }

    private var filterActive: Bool {
        !filter.q.trimmingCharacters(in: .whitespaces).isEmpty || filter.libraryId != nil || filter.reason != nil
    }

    // MARK: 动作

    private func runRestore(_ ids: [Int]) async {
        guard !ids.isEmpty, !busy else { return }
        busy = true
        defer { busy = false }
        do {
            let result = try await api.libraryRecycleRestore(body: API.TrashedRestorePayload(ids: ids))
            let text = ManageRecycleText.batchResult("恢复", result)
            if result.failed.isEmpty { feedback.success(text) } else { feedback.error(text) }
            selected.subtract(ids)
            await reload()
        } catch {
            feedback.error(error)
        }
    }

    /// 清理确认要讲清的事实：标题、文件数、条目数、释放空间、原地文件数、是否作用于整个筛选
    private struct PurgeFacts {
        var title: String
        var files: Int
        var items: Int
        var bytes: Int
        var kept: Int
        var wholeFilter: Bool
    }

    /**
     清理确认：条目卡的「清理 N」、批量条的「清理」、展开行的「清理」、页级的「立即清理全部」
     复用同一个确认框，只是数字与作用域不同（同 Web runPurge）。
     */
    private func runPurge(_ payload: API.TrashedPurgePayload, _ facts: PurgeFacts) async {
        guard facts.files > 0, !busy else { return }
        var bullets = [
            "\(facts.files) 个文件" + (facts.items > 1 ? "，涉及 \(facts.items) 个条目" : ""),
            "释放空间 \(libraryBytes(facts.bytes))",
        ]
        if facts.kept > 0 { bullets.append("\(facts.kept) 个仍在原位（移入回收站失败的文件，按当前路径删除）") }
        let description = (facts.wholeFilter ? "会立即从磁盘删除当前筛选命中的全部文件，不只是本页。" : "会立即从磁盘删除。")
            + " 删除不可撤销；若其中有文件仍在下载器里做种，删除会中断做种任务。"
        let ok = await feedback.confirm(
            facts.title,
            message: manageBinMessage(description, bullets: bullets),
            confirmTitle: "清理 \(facts.files) 个文件",
            cancelTitle: "先不",
            destructive: true
        )
        guard ok else { return }
        busy = true
        defer { busy = false }
        do {
            let result = try await api.libraryRecyclePurge(body: payload)
            let text = ManageRecycleText.batchResult("清理", result)
            if result.failed.isEmpty { feedback.success(text) } else { feedback.error(text) }
            selected = []
            await reload()
        } catch {
            feedback.error(error)
        }
    }

    private func purgeFiles(_ files: [API.TrashedFileView], title: String, itemCount: Int = 1) async {
        await runPurge(
            API.TrashedPurgePayload(ids: files.map(\.id)),
            PurgeFacts(
                title: title,
                files: files.count,
                items: itemCount,
                bytes: files.reduce(0) { $0 + $1.sizeBytes },
                kept: files.filter(\.keptInPlace).count,
                wholeFilter: false
            )
        )
    }

    private func purgeAll(_ data: API.TrashedFilesData) async {
        let libraryName = filter.libraryId.flatMap { id in data.byLibrary.first { $0.libraryId == id }?.name }
        let title: String
        if let libraryName {
            title = "清理「\(libraryName)」库的全部待回收文件？"
        } else if filterActive {
            title = "清理当前筛选下的全部待回收文件？"
        } else {
            title = "清理全部待回收文件？"
        }
        await runPurge(
            API.TrashedPurgePayload(filter: filter.apiFilter),
            PurgeFacts(
                title: title, files: data.totalFiles, items: data.totalItems, bytes: data.totalBytes,
                kept: data.keptInPlace, wholeFilter: true
            )
        )
    }

    private func purgeSelected(_ items: [API.TrashedItemView]) async {
        let files = items.flatMap { $0.files.filter { selected.contains($0.id) } }
        let itemCount = items.filter { $0.files.contains { selected.contains($0.id) } }.count
        await purgeFiles(files, title: "清理所选的 \(files.count) 个文件？", itemCount: itemCount)
    }

    private func toggleItem(_ item: API.TrashedItemView) {
        let all = ManageRecycleText.selection(item, selected) == .all
        for file in item.files {
            if all { selected.remove(file.id) } else { selected.insert(file.id) }
        }
    }

    private func toggleFile(_ id: Int) {
        if selected.contains(id) { selected.remove(id) } else { selected.insert(id) }
    }

    // MARK: 渲染

    @ViewBuilder
    private var content: some View {
        if let data {
            if data.totalFiles == 0, !filterActive {
                EmptyState(
                    systemImage: "trash",
                    title: "回收站是空的",
                    message: "洗版替换下来的旧版本会先停在这里 7 天再自动删除，期间可以恢复；文件本身放在各库根目录的 .movieclaw-trash 里。"
                )
                .padding(.top, 40)
                .accessibilityIdentifier("recycle-empty")
            } else {
                loaded(data)
            }
        } else if failed {
            VStack(spacing: 12) {
                Text("回收站加载失败").font(.subheadline).foregroundStyle(Theme.textMuted)
                Button("重试") { Task { await reload() } }
                    .buttonStyle(.glass)
                    .accessibilityIdentifier("recycle-retry")
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 64)
        } else {
            HStack(spacing: 10) {
                ProgressView()
                Text("正在加载回收站…").font(.subheadline).foregroundStyle(Theme.textMuted)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 64)
        }
    }

    private func loaded(_ data: API.TrashedFilesData) -> some View {
        let items = data.items
        let pageCount = max(1, Int((Double(data.totalItems) / Double(Self.pageSize)).rounded(.up)))
        let pageIndex = offset / Self.pageSize
        return VStack(alignment: .leading, spacing: 12) {
            if failed {
                ManageBinWarningBanner(text: "与后端通信失败，正在自动重试；下方显示的是最近一次成功加载的数据")
            }

            // 摘要行：一句话回答"多少、多大、多急"；右侧是页面唯一的页级动作
            HStack(alignment: .center, spacing: 10) {
                Text(ManageRecycleText.summary(data))
                    .font(.subheadline)
                    .monospacedDigit()
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("recycle-summary")
                ManageBinButton(title: "立即清理全部 · \(data.totalFiles)", tone: .danger) {
                    Task { await purgeAll(data) }
                }
                .disabled(busy || data.totalFiles == 0)
                .accessibilityIdentifier("recycle-purge-all")
            }

            // 筛选：搜索 / 库胶囊 / 原因胶囊（分面计数，切换一组不归零另一组）
            ManageBinSearchField(text: $queryDraft, placeholder: "按片名、剧名或文件名搜索", identifier: "recycle-search")
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ManageBinChip(title: "全部库 \(data.byLibrary.reduce(0) { $0 + $1.count })", active: filter.libraryId == nil) {
                        filter.libraryId = nil
                    }
                    .accessibilityIdentifier("recycle-library-all")
                    ForEach(data.byLibrary, id: \.libraryId) { lib in
                        ManageBinChip(title: "\(lib.name) \(lib.count)", active: filter.libraryId == lib.libraryId) {
                            filter.libraryId = filter.libraryId == lib.libraryId ? nil : lib.libraryId
                        }
                        .accessibilityIdentifier("recycle-library-\(lib.libraryId)")
                    }
                    if !data.byReason.isEmpty {
                        Rectangle().fill(Color.white.opacity(0.1)).frame(width: 1, height: 16).padding(.horizontal, 4)
                    }
                    ForEach(data.byReason, id: \.reason) { r in
                        ManageBinChip(title: "\(ManageRecycleText.reasonLabel(r.reason)) \(r.count)", active: filter.reason == r.reason) {
                            filter.reason = filter.reason == r.reason ? nil : r.reason
                        }
                        .accessibilityIdentifier("recycle-reason-\(r.reason)")
                    }
                }
                .padding(.vertical, 2)
            }
            .scrollClipDisabled()

            // 列表：一个条目一张卡（Web 手机端形态）
            VStack(spacing: 0) {
                if items.isEmpty {
                    HStack(spacing: 8) {
                        Text("没有符合条件的待回收文件").foregroundStyle(Theme.textMuted)
                        Button("清除筛选") {
                            queryDraft = ""
                            filter = ManageRecycleFilter()
                        }
                        .foregroundStyle(Theme.info)
                        .accessibilityIdentifier("recycle-clear-filter")
                    }
                    .font(.subheadline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 36)
                    .padding(.horizontal, 16)
                } else {
                    selectionHeader(items)
                    ForEach(items, id: \.key) { item in
                        Divider().overlay(Color.white.opacity(0.06))
                        ManageRecycleItemCard(
                            item: item,
                            expanded: expanded.contains(item.key),
                            selected: selected,
                            busy: busy,
                            onToggleExpanded: {
                                if expanded.contains(item.key) { expanded.remove(item.key) } else { expanded.insert(item.key) }
                            },
                            onToggleItem: { toggleItem(item) },
                            onToggleFile: toggleFile,
                            onRestore: { files in Task { await runRestore(files.map(\.id)) } },
                            onPurge: { files, title in Task { await purgeFiles(files, title: title) } }
                        )
                    }
                }
            }
            .background(Color.white.opacity(0.02), in: .rect(cornerRadius: Theme.cardRadius))
            .overlay(RoundedRectangle(cornerRadius: Theme.cardRadius).strokeBorder(Theme.line))
            .clipShape(.rect(cornerRadius: Theme.cardRadius))

            // 页脚：范围 + 保留期说明 + 页码
            VStack(alignment: .leading, spacing: 8) {
                Text(
                    (items.isEmpty ? "" : "第 \(offset + 1)–\(offset + items.count) 个条目，共 \(data.totalItems) 个条目 · \(data.totalFiles) 个文件 · ")
                        + "回收站内文件保留 7 天后自动删除"
                )
                .font(.caption)
                .monospacedDigit()
                .foregroundStyle(Theme.textFaint)
                .fixedSize(horizontal: false, vertical: true)
                if pageCount > 1 {
                    ManageBinPager(page: pageIndex, count: pageCount, numbered: true, idPrefix: "recycle-page") { p in
                        offset = p * Self.pageSize
                    }
                }
            }
        }
        .padding(.horizontal, Theme.pagePadding)
        .padding(.top, 16)
    }

    /**
     列表表头：「全选本页」三态复选框；勾选后同一行换成已选摘要 + 恢复 / 清理 / 取消。
     Web 手机端的批量条是底部悬浮的，这里被外层 ScrollView 包着做不了悬浮，改为贴在列表头上。
     */
    private func selectionHeader(_ items: [API.TrashedItemView]) -> some View {
        let pageAll = !items.isEmpty && items.allSatisfy { ManageRecycleText.selection($0, selected) == .all }
        let pageSome = !pageAll && items.contains { ManageRecycleText.selection($0, selected) != .none }
        let summary = ManageRecycleText.selectionSummary(items, selected)
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                ManageBinCheckbox(state: pageAll ? .all : pageSome ? .some : .none, label: "全选本页") {
                    selected = pageAll ? [] : Set(items.flatMap { $0.files.map(\.id) })
                }
                .accessibilityIdentifier("recycle-select-all")
                if summary.count > 0 {
                    Text("已选 **\(summary.count)** 个文件 · \(libraryBytes(summary.bytes))")
                        .font(.subheadline)
                        .monospacedDigit()
                        .foregroundStyle(Theme.text)
                        .accessibilityIdentifier("recycle-selection-summary")
                } else {
                    Text("全选本页").font(.caption).foregroundStyle(Theme.textFaint)
                }
                Spacer(minLength: 0)
            }
            if summary.count > 0 {
                HStack(spacing: 6) {
                    ManageBinButton(title: "恢复") { Task { await runRestore(Array(selected)) } }
                        .disabled(busy)
                        .accessibilityIdentifier("recycle-restore")
                    ManageBinButton(title: "清理", tone: .danger) { Task { await purgeSelected(items) } }
                        .disabled(busy)
                        .accessibilityIdentifier("recycle-purge")
                    ManageBinButton(title: "取消", tone: .ghost) { selected = [] }
                        .accessibilityIdentifier("recycle-selection-cancel")
                }
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(summary.count > 0 ? Theme.accent.opacity(0.06) : .clear)
    }
}

// MARK: - 筛选

/// 回收站筛选：搜索词 / 库 / 原因（同 Web Filter）
struct ManageRecycleFilter: Equatable {
    var q = ""
    var libraryId: Int?
    var reason: String?

    /// 搜索词去空白，空串不传（Web `filter.q.trim() || undefined`）
    var apiQuery: String? {
        let trimmed = q.trimmingCharacters(in: .whitespaces)
        return trimmed.isEmpty ? nil : trimmed
    }

    /// 「立即清理全部」的作用域：与列表接口同一组筛选参数
    var apiFilter: API.TrashedFilter {
        API.TrashedFilter(q: apiQuery, libraryId: libraryId, reason: reason)
    }
}

/// 列表请求的依赖：筛选或页码变了就重拉一次
private struct ManageRecycleQueryKey: Equatable {
    var filter: ManageRecycleFilter
    var offset: Int
}

// MARK: - 条目卡

/// 手机端：一个条目一卡，读法与桌面同构（种子名 / 品质 / 原因 / 倒计时 + 按钮）。
private struct ManageRecycleItemCard: View {
    let item: API.TrashedItemView
    let expanded: Bool
    let selected: Set<Int>
    let busy: Bool
    let onToggleExpanded: () -> Void
    let onToggleItem: () -> Void
    let onToggleFile: (Int) -> Void
    let onRestore: ([API.TrashedFileView]) -> Void
    let onPurge: ([API.TrashedFileView], String) -> Void

    var body: some View {
        let multi = item.fileCount > 1
        let single = multi ? nil : item.files.first
        let quality = single.map(ManageRecycleText.fileQualityLine) ?? ManageRecycleText.itemQualityLine(item)
        let due = ManageRecycleText.itemCountdown(item)
        let selection = ManageRecycleText.selection(item, selected)
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 12) {
                ManageBinCheckbox(state: selection, label: "选择「\(ManageRecycleText.itemTitle(item))」", action: onToggleItem)
                    .accessibilityIdentifier("recycle-select-item-\(item.key)")
                ManageRecycleIdentity(item: item)
            }
            if let single {
                ManageRecycleFileName(file: single, clamp: true)
                    .padding(.top, 2)
            } else {
                Button(action: onToggleExpanded) {
                    HStack(spacing: 6) {
                        Image(systemName: "chevron.down")
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(Theme.textFaint)
                            .rotationEffect(.degrees(expanded ? 0 : -90))
                        Text(ManageRecycleText.itemFilesSummary(item))
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(Theme.text)
                        Text(expanded ? "收起" : "展开").font(.caption).foregroundStyle(Theme.textFaint)
                    }
                }
                .buttonStyle(.plain)
                .padding(.top, 2)
                .accessibilityIdentifier("recycle-expand-\(item.key)")
            }
            Text(ManageRecycleText.qualityText(quality))
                .font(.caption)
                .fixedSize(horizontal: false, vertical: true)
            if single?.keptInPlace == true { ManageRecycleKeptBadge() }
            if let error = single?.lastError {
                Text("上次清理失败：\(error)").font(.caption).foregroundStyle(Theme.danger)
            }
            Text(ManageRecycleText.itemReasonText(item))
                .font(.footnote)
                .foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                ManageRecycleDueText(due: due, suffix: "自动清理")
                Spacer(minLength: 0)
                ManageBinButton(title: "恢复" + (multi ? " \(item.fileCount)" : "")) { onRestore(item.files) }
                    .disabled(busy)
                    .accessibilityIdentifier("recycle-item-restore-\(item.key)")
                ManageBinButton(title: "清理" + (multi ? " \(item.fileCount)" : ""), tone: .danger) {
                    onPurge(item.files, ManageRecycleText.purgeTitle(item))
                }
                .disabled(busy)
                .accessibilityIdentifier("recycle-item-purge-\(item.key)")
            }
            .padding(.top, 4)
            if expanded, multi {
                VStack(spacing: 0) {
                    ForEach(item.files, id: \.id) { file in
                        Divider().overlay(Color.white.opacity(0.06))
                        fileRow(file)
                    }
                }
                .padding(.top, 4)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .background(selection == .all ? Theme.accent.opacity(0.07) : .clear)
    }

    /// 展开的每集：复选框 / 文件名 + 集号集名 · 品质 / 恢复 清理 / 右上角短倒计时
    private func fileRow(_ file: API.TrashedFileView) -> some View {
        let code = ManageRecycleText.episodeCode(file.seasonNumber, file.episodeNumber)
        return HStack(alignment: .top, spacing: 8) {
            ManageBinCheckbox(state: selected.contains(file.id) ? .all : .none, label: "选择「\(file.fileName)」") {
                onToggleFile(file.id)
            }
            .padding(.top, 1)
            .accessibilityIdentifier("recycle-select-file-\(file.id)")
            VStack(alignment: .leading, spacing: 3) {
                ManageRecycleFileName(file: file, muted: true, clamp: true)
                Text(episodeLine(file, code: code))
                    .font(.caption)
                    .fixedSize(horizontal: false, vertical: true)
                if let error = file.lastError {
                    Text("上次清理失败：\(error)").font(.caption).foregroundStyle(Theme.danger)
                }
                HStack(spacing: 6) {
                    ManageBinButton(title: "恢复", mini: true) { onRestore([file]) }
                        .disabled(busy)
                        .accessibilityIdentifier("recycle-file-restore-\(file.id)")
                    ManageBinButton(title: "清理", tone: .danger, mini: true) { onPurge([file], "清理「\(file.fileName)」？") }
                        .disabled(busy)
                        .accessibilityIdentifier("recycle-file-purge-\(file.id)")
                    if file.keptInPlace { ManageRecycleKeptBadge() }
                }
                .padding(.top, 2)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            ManageRecycleDueText(due: ManageRecycleText.countdown(file.purgeAfter), short: true)
        }
        .padding(.vertical, 8)
    }

    /// 「S01E03 集名 · 品质行」
    private func episodeLine(_ file: API.TrashedFileView, code: String) -> AttributedString {
        var out = AttributedString()
        if !code.isEmpty {
            out += manageBinRun(code, Theme.text, .caption.monospaced().weight(.semibold))
            if let title = file.episodeTitle { out += manageBinRun(" \(title)", Theme.textMuted) }
            out += manageBinRun(" · ", Theme.textFaint)
        }
        out += ManageRecycleText.qualityText(ManageRecycleText.fileQualityLine(file), muted: true)
        return out
    }
}

/// 海报 + 片名（点开条目详情页）+ 年份，第二行库名 · 季。
private struct ManageRecycleIdentity: View {
    let item: API.TrashedItemView
    @Environment(\.api) private var api
    @Environment(Router.self) private var router

    var body: some View {
        let media = item.mediaItem
        let seasons = ManageRecycleText.seasonsLabel(item.seasons)
        HStack(spacing: 10) {
            RemoteImage(url: api.image(media?.posterUrl, .posterCard))
                .frame(width: 30, height: 44)
                .clipShape(.rect(cornerRadius: 5))
                .overlay(RoundedRectangle(cornerRadius: 5).strokeBorder(Theme.line))
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    if let media {
                        Button {
                            router.push(.libraryItem(libraryId: item.library.id, itemId: media.id))
                        } label: {
                            Text(media.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text).lineLimit(1)
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("recycle-item-title-\(item.key)")
                        if let year = media.year {
                            Text(String(year)).font(.subheadline).foregroundStyle(Theme.textFaint)
                        }
                    } else {
                        Text(ManageRecycleText.itemTitle(item)).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text).lineLimit(1)
                    }
                }
                Text(item.library.name + (seasons.isEmpty ? "" : " · \(seasons)"))
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
                    .lineLimit(1)
            }
        }
    }
}

/**
 文件名：等宽。点它弹出完整存放路径——「原路径」是恢复回去的位置，「现在的位置」是回收站内的
 当前路径（Web 用 openOnClick 的 Tooltip，这里用紧凑弹出框）。
 */
private struct ManageRecycleFileName: View {
    let file: API.TrashedFileView
    var muted = false
    /// 手机端：允许折两行不截断
    var clamp = false
    @State private var showPath = false

    var body: some View {
        Button { showPath = true } label: {
            Text(file.fileName)
                .font(.caption.monospaced())
                .foregroundStyle(muted ? Theme.textMuted : Theme.text)
                .lineLimit(clamp ? 2 : 1)
                .multilineTextAlignment(.leading)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("查看「\(file.fileName)」的存放路径")
        .accessibilityIdentifier("recycle-file-name-\(file.id)")
        .popover(isPresented: $showPath) {
            VStack(alignment: .leading, spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("原路径（恢复回这里）").foregroundStyle(Theme.textFaint)
                    Text(file.trashOriginalPath ?? file.filePath).font(.caption.monospaced()).foregroundStyle(Theme.text)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text("现在的位置").foregroundStyle(Theme.textFaint)
                    Text(file.keptInPlace ? "仍在原路径（移入回收站失败，清理时按这个路径删除）" : file.filePath)
                        .font(.caption.monospaced())
                        .foregroundStyle(Theme.text)
                }
            }
            .font(.caption)
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .frame(width: 300, alignment: .leading)
            .padding(14)
            .presentationCompactAdaptation(.popover)
        }
    }
}

/// 「原地」徽标：移入回收站失败，文件仍在原路径；清理按当前路径删除
private struct ManageRecycleKeptBadge: View {
    var body: some View {
        Text("原地")
            .font(.caption2)
            .foregroundStyle(Theme.warning)
            .padding(.horizontal, 6)
            .overlay(Capsule().strokeBorder(Theme.warning.opacity(0.4)))
            .accessibilityHint("移入回收站失败，文件仍在原路径；清理按当前路径删除")
    }
}

/// 倒计时文字：24 小时内警示色加粗、不自动清理弱化；short 去掉「后」与「最早」
private struct ManageRecycleDueText: View {
    let due: ManageRecycleCountdown
    var suffix: String?
    var short = false

    var body: some View {
        var text = due.text
        if short {
            if text.hasSuffix("后") { text.removeLast() }
            if text.hasPrefix("最早 ") { text.removeFirst(3) }
        }
        if let suffix, due.tone != .never { text += suffix }
        return Text(text)
            .font(due.tone == .soon ? .caption.weight(.semibold) : .caption)
            .monospacedDigit()
            .foregroundStyle(due.tone == .soon ? Theme.warning : due.tone == .never ? Theme.textFaint : Theme.textMuted)
            .lineLimit(1)
            .fixedSize()
    }
}

// MARK: - 纯函数（Web lib/library-recycle.ts 逐条移植）

/// 倒计时的语气：soon = 24 小时内（警示色）；never = 不自动清理（弱化）
struct ManageRecycleCountdown: Equatable {
    enum Tone { case soon, normal, never }
    var text: String
    var tone: Tone
}

/// 品质行：第一段是加粗的档位（可带计数），HDR 徽标单列，其余弱化以 · 相连
struct ManageRecycleQualityLine {
    var tiers: [(label: String, count: Int?)]
    var hdr: [String]
    var rest: [String]
}

enum ManageRecycleText {
    /// 单个到期时间 → 倒计时文案（直读 purge_after）
    static func countdown(_ purgeAfter: String?, now: Date = .now) -> ManageRecycleCountdown {
        guard let purgeAfter, let date = Formatters.date(purgeAfter) else { return .init(text: "不自动清理", tone: .never) }
        let ms = date.timeIntervalSince(now) * 1000
        if ms <= 0 { return .init(text: "即将清理", tone: .soon) }
        let hour = 3_600_000.0, day = 24 * hour
        let days = Int(ms / day)
        let hours = Int(ms.truncatingRemainder(dividingBy: day) / hour)
        let tone: ManageRecycleCountdown.Tone = ms <= day ? .soon : .normal
        if days > 0 { return .init(text: hours > 0 ? "\(days) 天 \(hours) 小时后" : "\(days) 天后", tone: tone) }
        if hours > 0 { return .init(text: "\(hours) 小时后", tone: tone) }
        return .init(text: "1 小时内", tone: tone)
    }

    /// 条目行的倒计时：取组内最早到期；多文件时带「最早」前缀
    static func itemCountdown(_ item: API.TrashedItemView) -> ManageRecycleCountdown {
        var base = countdown(item.earliestPurgeAfter)
        if item.fileCount > 1, base.tone != .never { base.text = "最早 \(base.text)" }
        return base
    }

    /// 摘要行：`N 个文件 · M 个条目 · 总大小 · K 个将在 24 小时内自动清理 · J 个仍在原位`
    static func summary(_ data: API.TrashedFilesData) -> AttributedString {
        var segments: [(String, Color)] = [
            ("\(data.totalFiles) 个文件", Theme.text),
            ("\(data.totalItems) 个条目", Theme.text),
            (libraryBytes(data.totalBytes), Theme.text),
        ]
        if data.dueWithin24h > 0 { segments.append(("\(data.dueWithin24h) 个将在 24 小时内自动清理", Theme.warning)) }
        if data.keptInPlace > 0 { segments.append(("\(data.keptInPlace) 个仍在原位", Theme.textFaint)) }
        var out = AttributedString()
        for (i, seg) in segments.enumerated() {
            if i > 0 { out += manageBinRun(" · ", Theme.textMuted) }
            out += manageBinRun(seg.0, seg.1)
        }
        return out
    }

    /// 审计快照 reason 词表 → 胶囊 / 原因文案；未知值原样返回
    static func reasonLabel(_ reason: String) -> String {
        switch reason {
        case "upgrade_replaced": "洗版替换"
        case "upgrade_refuted": "洗版证伪"
        case "manual": "手动删除"
        case "duplicate_cleanup": "重复清理"
        case "unknown": "其他"
        default: reason
        }
    }

    /// 条目的原因：组内 note 一致时写整句；混合时写计数 `洗版证伪 4 · 洗版替换 2`
    static func itemReasonText(_ item: API.TrashedItemView) -> String {
        if let note = item.note, !note.isEmpty { return note }
        let entries = item.reasons.sorted { $0.value != $1.value ? $0.value > $1.value : $0.key < $1.key }
        if entries.isEmpty { return "" }
        if entries.count == 1 { return reasonLabel(entries[0].key) }
        return entries.map { "\(reasonLabel($0.key)) \($0.value)" }.joined(separator: " · ")
    }

    /// 探测层的编码名 → 惯用写法（与条目详情页同一张表）
    static func videoCodecLabel(_ codec: String?) -> String? {
        guard let codec, !codec.isEmpty else { return nil }
        switch codec.lowercased() {
        case "hevc", "h265": return "HEVC"
        case "h264": return "H.264"
        case "av1": return "AV1"
        case "vc1": return "VC-1"
        case "mpeg2video": return "MPEG-2"
        case "vp9": return "VP9"
        default: return codec.uppercased()
        }
    }

    /// 单个文件的品质档位「分辨率 片源」，两者都没探到时写「未知规格」
    static func fileTier(_ file: API.TrashedFileView) -> String {
        let tier = [file.resolution, file.mediaSource].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " ")
        return tier.isEmpty ? "未知规格" : tier
    }

    /// 单个文件的品质行（电影单文件、展开的每集）
    static func fileQualityLine(_ file: API.TrashedFileView) -> ManageRecycleQualityLine {
        let codec = videoCodecLabel(file.videoCodec)
        let codecText = codec.map { c in (file.bitDepth ?? 0) > 8 ? "\(c) \(file.bitDepth ?? 0)bit" : c }
        return ManageRecycleQualityLine(
            tiers: [(fileTier(file), nil)],
            hdr: file.hdr.map { [$0] } ?? [],
            rest: [libraryBytes(file.sizeBytes), codecText, file.audioLabel, file.releaseGroup]
                .compactMap { $0 }.filter { !$0.isEmpty }
        )
    }

    /// 条目的品质汇总：档位一致写一种，混合写带计数的并列；大小为合计，其余去重后用 / 并列
    static func itemQualityLine(_ item: API.TrashedItemView) -> ManageRecycleQualityLine {
        let tiers = item.quality.tiers.sorted { $0.value != $1.value ? $0.value > $1.value : $0.key < $1.key }
        let mixed = tiers.count > 1
        let codecs = item.quality.videoCodecs.compactMap(videoCodecLabel).joined(separator: " / ")
        let audios = item.quality.audioLabels.joined(separator: " / ")
        let groups = item.quality.releaseGroups.joined(separator: " / ")
        return ManageRecycleQualityLine(
            tiers: tiers.map { ($0.key, mixed ? $0.value : nil) },
            hdr: item.quality.hdr,
            rest: [libraryBytes(item.totalBytes), codecs, audios, groups].filter { !$0.isEmpty }
        )
    }

    /// 品质行渲染：档位加粗（混合带计数）· HDR 金色 · 大小 · 编码 · 音轨 · 制作组
    static func qualityText(_ line: ManageRecycleQualityLine, muted: Bool = false) -> AttributedString {
        var out = AttributedString()
        for (i, tier) in line.tiers.enumerated() {
            if i > 0 { out += manageBinRun(" · ", Theme.textFaint) }
            out += manageBinRun(tier.label, muted ? Theme.textMuted : Theme.text, .caption.weight(muted ? .medium : .semibold))
            if let count = tier.count { out += manageBinRun(" \(count)", Theme.textFaint, .caption2) }
        }
        for hdr in line.hdr {
            out += manageBinRun(" \(hdr)", Color(red: 0xE8 / 255, green: 0xC9 / 255, blue: 0x8A / 255), .caption2.weight(.semibold))
        }
        for (i, part) in line.rest.enumerated() {
            out += manageBinRun(" · ", Theme.textFaint)
            out += manageBinRun(part, i == 0 ? Theme.textMuted : Theme.textFaint)
        }
        return out
    }

    /// S01E03 形态的集号；电影（0,0）返回空串
    static func episodeCode(_ season: Int, _ episode: Int) -> String {
        if season == 0, episode == 0 { return "" }
        return String(format: "S%02dE%02d", season, episode)
    }

    /// 涉及的季：连续写区间 `S01–S03`，不连续写列表 `S01, S03`；电影为空串
    static func seasonsLabel(_ seasons: [Int]) -> String {
        guard !seasons.isEmpty else { return "" }
        let sorted = seasons.sorted()
        func pad(_ n: Int) -> String { String(format: "S%02d", n) }
        if sorted.count == 1 { return pad(sorted[0]) }
        let contiguous = zip(sorted, sorted.dropFirst()).allSatisfy { $1 == $0 + 1 }
        return contiguous ? "\(pad(sorted.first!))–\(pad(sorted.last!))" : sorted.map(pad).joined(separator: ", ")
    }

    /// 多文件条目的文件摘要：剧集 `24 集 · 3 季`，其余 `2 个版本`
    static func itemFilesSummary(_ item: API.TrashedItemView) -> String {
        if item.mediaItem?.kind == "tv" {
            return item.seasons.isEmpty ? "\(item.fileCount) 集" : "\(item.fileCount) 集 · \(item.seasons.count) 季"
        }
        return "\(item.fileCount) 个版本"
    }

    static func itemTitle(_ item: API.TrashedItemView) -> String {
        item.mediaItem?.title ?? item.files.first?.fileName ?? "未识别文件"
    }

    static func purgeTitle(_ item: API.TrashedItemView) -> String {
        let title = itemTitle(item)
        return item.fileCount > 1 ? "清理「\(title)」的 \(item.fileCount) 个待回收文件？" : "清理「\(title)」的待回收文件？"
    }

    /// 条目复选框的三态：整组选中 / 部分选中 / 未选
    static func selection(_ item: API.TrashedItemView, _ selected: Set<Int>) -> ManageBinCheckState {
        let picked = item.files.filter { selected.contains($0.id) }.count
        if picked == 0 { return .none }
        return picked == item.files.count ? .all : .some
    }

    /// 勾选集合的摘要：文件数与合计大小（批量条用）
    static func selectionSummary(_ items: [API.TrashedItemView], _ selected: Set<Int>) -> (count: Int, bytes: Int) {
        var count = 0, bytes = 0
        for item in items {
            for file in item.files where selected.contains(file.id) {
                count += 1
                bytes += file.sizeBytes
            }
        }
        return (count, bytes)
    }

    /// 批量结果 → 一句回执：「已清理 55 个文件，2 个失败，还有 N 个未处理，再点一次即可」
    static func batchResult(_ verb: String, _ result: API.TrashedBatchResultView) -> String {
        var text = "已\(verb) \(result.done) 个文件"
        if !result.failed.isEmpty { text += "，\(result.failed.count) 个失败" }
        if result.remaining > 0 { text += "，还有 \(result.remaining) 个未处理，再点一次即可" }
        return text
    }
}

// MARK: - 两个页签共用的小部件（ManageBin 前缀）

/// 一段带颜色 / 字体的富文本
func manageBinRun(_ text: String, _ color: Color, _ font: Font? = nil) -> AttributedString {
    var run = AttributedString(text)
    run.foregroundColor = color
    if let font { run.font = font }
    return run
}

/// 确认框正文：说明 + 逐条事实（Web confirm 的 bullets，原生 alert 没有列表，折成「• 」行）
func manageBinMessage(_ description: String, bullets: [String]) -> String {
    guard !bullets.isEmpty else { return description }
    return description + "\n\n" + bullets.map { "• \($0)" }.joined(separator: "\n")
}

/// 请求被取消（页面离开 / 新请求替换）不算失败
func manageBinIsCancellation(_ error: Error) -> Bool {
    if Task.isCancelled || error is CancellationError { return true }
    return (error as? URLError)?.code == .cancelled
}

/// 与后端通信失败时的琥珀色提示条（保留最近一次成功的数据）
struct ManageBinWarningBanner: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.footnote)
            .foregroundStyle(Color(red: 0.99, green: 0.85, blue: 0.55))
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(Color.orange.opacity(0.1), in: .rect(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.orange.opacity(0.25)))
    }
}

/// 搜索框：放大镜 + 输入 + 清除（液态玻璃胶囊）
struct ManageBinSearchField: View {
    @Binding var text: String
    let placeholder: String
    let identifier: String

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass").font(.subheadline).foregroundStyle(Theme.textMuted)
            TextField(placeholder, text: $text)
                .font(.subheadline)
                .foregroundStyle(Theme.text)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .submitLabel(.search)
                .accessibilityIdentifier(identifier)
            if !text.isEmpty {
                Button { text = "" } label: {
                    Image(systemName: "xmark.circle.fill").foregroundStyle(Theme.textFaint)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("清除搜索")
                .accessibilityIdentifier("\(identifier)-clear")
            }
        }
        .padding(.horizontal, 14)
        .frame(height: 40)
        .glassEffect(.regular.interactive(), in: .capsule)
    }
}

/// 筛选胶囊：选中实心亮银，未选淡底描边（与 LibraryChip 同一观感，额外支持前后图标）
struct ManageBinChip: View {
    let title: String
    let active: Bool
    var leadingSymbol: String?
    var trailingSymbol: String?
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                if let leadingSymbol { Image(systemName: leadingSymbol).font(.caption2.weight(.semibold)) }
                Text(title).font(.caption.weight(active ? .semibold : .medium)).monospacedDigit()
                if let trailingSymbol { Image(systemName: trailingSymbol).font(.caption2.weight(.semibold)) }
            }
            .lineLimit(1)
            .foregroundStyle(active ? Color.black.opacity(0.85) : Theme.textMuted)
            .padding(.horizontal, 11)
            .padding(.vertical, 6)
            .background(active ? AnyShapeStyle(Theme.accentStrong) : AnyShapeStyle(Theme.surfaceInset), in: .capsule)
            .overlay(Capsule().strokeBorder(active ? .clear : Theme.line))
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(active ? .isSelected : [])
    }
}

/// 动作按钮的轻重：普通（玻璃）/ 主操作（亮银实心）/ 幽灵（淡描边）/ 危险描边（红字）/ 危险实心（红底白字）
enum ManageBinButtonTone {
    case normal, primary, ghost, danger, dangerSolid
}

/// 页签内的动作按钮：系统液态玻璃 + 小号字，轻重由 tone 区分
struct ManageBinButton: View {
    let title: String
    var tone: ManageBinButtonTone = .normal
    var mini = false
    /// 占满可用宽度（Web max-md:flex-1）
    var fill = false
    let action: () -> Void

    @Environment(\.isEnabled) private var isEnabled

    var body: some View {
        let label = Text(title)
            .font(mini ? .caption2.weight(.medium) : .caption.weight(.medium))
            .monospacedDigit()
            .lineLimit(1)
            .frame(maxWidth: fill ? .infinity : nil)
        switch tone {
        case .normal:
            Button(action: action) { label.foregroundStyle(Theme.text) }
                .buttonStyle(.glass)
                .controlSize(mini ? .mini : .small)
        case .danger:
            Button(action: action) { label.foregroundStyle(Theme.danger) }
                .buttonStyle(.glass)
                .controlSize(mini ? .mini : .small)
        case .primary:
            Button(action: action) { label.foregroundStyle(Color.black.opacity(0.85)) }
                .buttonStyle(.glassProminent)
                .tint(Theme.accentStrong)
                .controlSize(mini ? .mini : .small)
        case .dangerSolid:
            Button(action: action) { label.foregroundStyle(.white) }
                .buttonStyle(.glassProminent)
                .tint(Color(red: 0.84, green: 0.25, blue: 0.27))
                .controlSize(mini ? .mini : .small)
        case .ghost:
            Button(action: action) {
                label
                    .foregroundStyle(Theme.textMuted)
                    .padding(.horizontal, 12)
                    .padding(.vertical, mini ? 3 : 6)
                    .overlay(Capsule().strokeBorder(Color.white.opacity(0.12)))
                    .contentShape(.capsule)
            }
            .buttonStyle(.plain)
            .opacity(isEnabled ? 1 : 0.4)
        }
    }
}

/// 三态复选框的状态：整组 / 半选 / 未选
enum ManageBinCheckState {
    case all, some, none
}

/// 三态复选框（Web TriCheckbox）
struct ManageBinCheckbox: View {
    let state: ManageBinCheckState
    let label: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: state == .all ? "checkmark.square.fill" : state == .some ? "minus.square.fill" : "square")
                .font(.body)
                .foregroundStyle(state == .none ? Theme.textMuted : Theme.accentStrong)
                .frame(width: 24, height: 24)
                .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityValue(state == .all ? "已选" : state == .some ? "部分选中" : "未选")
    }
}

/// 分页器。numbered：‹ 1 2 … 7 ›（当前页前后各两页，其余折成省略号，回收站用）；
/// 否则 ‹ 1 / N ›（重复文件用）。
struct ManageBinPager: View {
    let page: Int
    let count: Int
    var numbered = true
    let idPrefix: String
    let onChange: (Int) -> Void

    var body: some View {
        HStack(spacing: 4) {
            arrow("chevron.left", disabled: page == 0, id: "\(idPrefix)-prev") { onChange(page - 1) }
                .accessibilityLabel("上一页")
            if numbered {
                ForEach(Array(pages.enumerated()), id: \.offset) { _, p in
                    if let p {
                        Button { onChange(p) } label: {
                            Text("\(p + 1)")
                                .font(.caption.weight(p == page ? .semibold : .regular))
                                .monospacedDigit()
                                .foregroundStyle(p == page ? Theme.text : Theme.textMuted)
                                .frame(minWidth: 28, minHeight: 28)
                                .background(p == page ? Color.white.opacity(0.12) : .clear, in: .rect(cornerRadius: 8))
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("\(idPrefix)-\(p + 1)")
                    } else {
                        Text("…").font(.caption).foregroundStyle(Theme.textFaint).padding(.horizontal, 2)
                    }
                }
            } else {
                Text("\(page + 1) / \(count)")
                    .font(.caption)
                    .monospacedDigit()
                    .foregroundStyle(Theme.textMuted)
                    .padding(.horizontal, 6)
                    .accessibilityIdentifier("\(idPrefix)-label")
            }
            arrow("chevron.right", disabled: page >= count - 1, id: "\(idPrefix)-next") { onChange(page + 1) }
                .accessibilityLabel("下一页")
        }
    }

    /// 页码序列：nil 表示省略号
    private var pages: [Int?] {
        var out: [Int?] = []
        for i in 0 ..< count {
            if i == 0 || i == count - 1 || abs(i - page) <= 2 {
                out.append(i)
            } else if let last = out.last, last != nil {
                out.append(nil)
            }
        }
        return out
    }

    private func arrow(_ symbol: String, disabled: Bool, id: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.caption.weight(.semibold))
                .foregroundStyle(Theme.textMuted)
                .frame(width: 28, height: 28)
                .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .disabled(disabled)
        .opacity(disabled ? 0.4 : 1)
        .accessibilityIdentifier(id)
    }
}
