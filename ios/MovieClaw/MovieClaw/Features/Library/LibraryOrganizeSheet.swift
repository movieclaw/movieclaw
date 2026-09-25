import SwiftUI

/// 整理文件名（对应网页 `components/library-organize-dialog.tsx`）：预览 → 确认 → 执行 → 结果，四段式。
///
/// 批量改名是半不可逆操作，交互按"手术确认单"打造：
/// - **先看后动**：打开即拉取完整预览（`POST …/file-organization-preview`，只读不动磁盘）——
///   每个文件改成什么名、哪些跳过及原因逐条可查；
/// - **风险前置**：无法一键撤销 / 做种会断 / 播放器会重识别三条风险醒目告知，
///   勾选「我已了解」后执行按钮才亮起；
/// - **执行可离场**：确认后任务在后端跑（`POST …/file-organizations`），这里轮询单库详情画进度；
///   关掉面板不影响整理；
/// - **结果可追溯**：完成页给出改名/附属/清理目录的完整账目，逐条错误原文展示。
///
/// 只收 libraryId：打开时先取一次单库详情（库名、类型、是否已在整理中、上一次整理结论），
/// 库已在整理中（比如中途关过面板）直接进执行页。
struct LibraryOrganizeSheet: View {
    let libraryId: Int
    /// 整理任务受理后回调（宿主据此刷新库状态 / 墙）
    var onStarted: () -> Void = {}

    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss

    private enum Phase { case loading, preview, running, done, failed }

    @State private var phase: Phase = .loading
    @State private var library: API.LibraryView?
    @State private var preview: API.OrganizePreviewView?
    @State private var result: API.LastOrganizeView?
    @State private var error: String?
    @State private var agreed = false
    @State private var progress: API.ScanProgressView?
    /// 完成判定的两个凭据：见过 organizing=true，或 last_organize 换了新结论。
    /// 只看 organizing=false 不行——POST 受理后后台任务才起跑，首轮轮询可能落在起跑前的
    /// 空档里，那时 last_organize 还是上一次的旧结论，直接当"完成"会拿旧结果糊弄用户
    @State private var sawRunning = false
    @State private var baselineFinished: String?

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("整理文件名")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("关闭") { dismiss() }
                            .accessibilityLabel("关闭对话框")
                    }
                }
        }
        .presentationDetents([.large])
        .task { await open() }
    }

    @ViewBuilder
    private var content: some View {
        switch phase {
        case .loading:
            VStack(spacing: 12) {
                ProgressView()
                Text("正在核对台账与磁盘，生成整理预览…")
                    .font(.callout)
                    .foregroundStyle(Theme.textMuted)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed:
            VStack(spacing: 16) {
                Text(error ?? "")
                    .font(.callout)
                    .foregroundStyle(Theme.danger)
                    .multilineTextAlignment(.center)
                Button("关闭") { dismiss() }.buttonStyle(.glass)
            }
            .padding(24)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .preview:
            if let preview { previewView(preview) }
        case .running:
            runningView
                // 执行中轮询单库详情：画进度，结束后取整理结论进结果页（只在执行页存在期间轮询）
                .polling(every: 1.5, immediately: true) { await pollRunning() }
        case .done:
            doneView
        }
    }

    // MARK: 加载与执行

    private func open() async {
        guard library == nil else { return }
        do {
            let lib = try await api.libraryGet(libraryId: libraryId)
            library = lib
            baselineFinished = lib.lastOrganize?.finishedAt
            if lib.organizing {
                phase = .running
                return
            }
            preview = try await api.workflowLibraryOrganizeFilesPreview(libraryId: libraryId)
            phase = .preview
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
            phase = .failed
        }
    }

    private func start() async {
        error = nil
        sawRunning = false
        baselineFinished = library?.lastOrganize?.finishedAt
        phase = .running
        do {
            _ = try await api.workflowLibraryOrganizeFilesStart(libraryId: libraryId)
            onStarted()
        } catch {
            self.error = error.localizedDescription
            phase = .preview
        }
    }

    private func pollRunning() async {
        guard phase == .running, let lib = try? await api.libraryGet(libraryId: libraryId) else { return }
        library = lib
        if lib.organizing {
            sawRunning = true
            progress = lib.organizeProgress
            return
        }
        let finished = lib.lastOrganize?.finishedAt
        if sawRunning || finished != baselineFinished {
            progress = lib.organizeProgress
            result = lib.lastOrganize
            phase = .done
        }
        // 两个凭据都没有：任务还没起跑，继续等下一轮
    }

    // MARK: 预览

    private struct RenameGroup: Identifiable {
        let id: Int
        let title: String
        let year: Int?
        var actions: [API.OrganizeRenameView]
    }

    /// 预览按条目分组：同一部作品的文件放在一起看，比平铺清单好核对得多
    private func groups(_ preview: API.OrganizePreviewView) -> [RenameGroup] {
        var order: [Int] = []
        var byItem: [Int: RenameGroup] = [:]
        for action in preview.renames {
            if byItem[action.mediaItemId] == nil {
                order.append(action.mediaItemId)
                byItem[action.mediaItemId] = RenameGroup(id: action.mediaItemId, title: action.title, year: action.year, actions: [])
            }
            byItem[action.mediaItemId]?.actions.append(action)
        }
        let zh = Locale(identifier: "zh")
        return order.compactMap { byItem[$0] }
            .sorted { $0.title.compare($1.title, locale: zh) == .orderedAscending }
    }

    private func previewView(_ preview: API.OrganizePreviewView) -> some View {
        let renameCount = preview.renames.count
        let sidecarCount = preview.renames.reduce(0) { $0 + $1.sidecars.count }
        // 条目目录改名时跟着搬的海报/NFO：不搬走旧目录就永远非空、清不掉
        let entryAssetCount = preview.entryAssets.count
        let seasonPart = library?.kind == "tv" ? "/Season NN" : ""
        return List {
            Section {
                VStack(alignment: .leading, spacing: 10) {
                    if let name = library?.name {
                        Text(name).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.textMuted)
                    }
                    Text("按刮削结果把存量文件改名归位为「标题 (年份)\(seasonPart)/规范文件名」，Plex / Emby 零歧义识别；字幕等附属文件同步改名。")
                        .font(.subheadline)
                        .foregroundStyle(Theme.textMuted)
                    // 统计条：一眼看清这次整理的规模
                    OrganizeFlowLayout(spacing: 6) {
                        statChip("\(renameCount) 个文件将改名归位", tone: renameCount > 0 ? .white : Theme.textMuted)
                        if sidecarCount > 0 { statChip("\(sidecarCount) 个附属文件同步改名", tone: Theme.textMuted) }
                        if entryAssetCount > 0 { statChip("\(entryAssetCount) 个海报/NFO 跟随条目目录", tone: Theme.textMuted) }
                        statChip("\(preview.alreadyOk) 个已符合规范", tone: Theme.success)
                        if !preview.skips.isEmpty { statChip("\(preview.skips.count) 个跳过", tone: Theme.warning) }
                    }
                }
                .listRowBackground(Color.clear)
            }

            if renameCount == 0 {
                Section {
                    VStack(spacing: 4) {
                        Text("这个库已经很规整了，没有需要改名的文件 🎉")
                            .font(.callout)
                            .foregroundStyle(Theme.textMuted)
                        if !preview.skips.isEmpty {
                            Text("（\(preview.skips.count) 个文件因下方原因未参与整理）")
                                .font(.subheadline)
                                .foregroundStyle(Theme.textFaint)
                        }
                    }
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 20)
                }
            } else {
                // 风险告知：批量改名是半不可逆操作，三条风险必须看到
                Section {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("开始前请确认").font(.subheadline.weight(.semibold))
                        bullet("改名直接发生在磁盘上，**无法一键撤销**；下方清单就是将要发生的全部变更。")
                        bullet("**正在下载器中做种的文件，改名后做种会失败**——请确认这些文件已不在做种，或接受改名后到下载器里重新校验。")
                        bullet("Emby / Plex 会把改名视为内容变更并重新识别，观看记录可能受影响。")
                    }
                    .foregroundStyle(Theme.warning)
                    .listRowBackground(Theme.warning.opacity(0.08))
                }

                // 改名清单：按作品分组，旧名 → 新名逐条可核对
                ForEach(groups(preview)) { group in
                    Section {
                        ForEach(group.actions, id: \.fileId) { action in
                            renameRow(action)
                        }
                    } header: {
                        HStack(spacing: 8) {
                            Text(group.title + (group.year.map { " (\($0))" } ?? ""))
                                .foregroundStyle(.white.opacity(0.85))
                            Text("\(group.actions.count) 个文件")
                                .foregroundStyle(Theme.textFaint)
                        }
                        .font(.subheadline.weight(.semibold))
                        .textCase(nil)
                    }
                }
            }

            // 跳过清单：默认收起，展开逐条看原因——用户对"没动的"也心里有数
            if !preview.skips.isEmpty {
                Section {
                    DisclosureGroup("跳过 \(preview.skips.count) 个文件（点击查看原因）") {
                        ForEach(preview.skips, id: \.filePath) { skip in
                            VStack(alignment: .leading, spacing: 2) {
                                Text(skip.filePath)
                                    .font(.caption.monospaced())
                                    .foregroundStyle(Theme.textFaint)
                                    .lineLimit(2)
                                    .truncationMode(.middle)
                                Text(skip.reason)
                                    .font(.caption)
                                    .foregroundStyle(Theme.warning.opacity(0.8))
                            }
                        }
                    }
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Theme.textMuted)
                }
            }
        }
        .scrollContentBackground(.hidden)
        .safeAreaInset(edge: .bottom) { confirmBar(renameCount: renameCount) }
    }

    private func renameRow(_ action: API.OrganizeRenameView) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(action.sourceRel)
                .font(.caption.monospaced())
                .strikethrough(color: .white.opacity(0.25))
                .foregroundStyle(Theme.textFaint)
                .lineLimit(1)
                .truncationMode(.middle)
            HStack(spacing: 4) {
                Image(systemName: "chevron.right")
                    .font(.system(size: 9, weight: .bold))
                    .foregroundStyle(Theme.accent)
                Text(action.targetRel)
                    .font(.caption.monospaced())
                    .foregroundStyle(.white.opacity(0.9))
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer(minLength: 8)
                Text(Formatters.bytes(action.sizeBytes))
                    .font(.caption2)
                    .foregroundStyle(Theme.textFaint)
            }
            if !action.sidecars.isEmpty {
                Text("+\(action.sidecars.count) 个附属文件（字幕等）同步改名")
                    .font(.caption2)
                    .foregroundStyle(Theme.textFaint)
                    .padding(.leading, 13)
            }
        }
        .padding(.vertical, 2)
    }

    /// 确认区：勾选「我已核对清单并了解上述风险」后才能开始
    private func confirmBar(renameCount: Int) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if let error {
                Text(error)
                    .font(.subheadline)
                    .foregroundStyle(Theme.danger)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if renameCount > 0 {
                Toggle("我已核对清单并了解上述风险", isOn: $agreed)
                    .font(.subheadline)
                    .foregroundStyle(Theme.textMuted)
                Button {
                    Task { await start() }
                } label: {
                    Text("开始整理 \(renameCount) 个文件")
                        .fontWeight(.semibold)
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.glassProminent)
                .controlSize(.large)
                .disabled(!agreed)
            } else {
                Button {
                    dismiss()
                } label: {
                    Text("关闭").frame(maxWidth: .infinity)
                }
                .buttonStyle(.glass)
                .controlSize(.large)
            }
        }
        .padding(16)
        .glassEffect(.regular, in: .rect(cornerRadius: 24))
        .padding(.horizontal, 12)
        .padding(.bottom, 4)
    }

    // MARK: 执行中 / 结果

    private var runningView: some View {
        let fraction: Double? = progress.flatMap { $0.total > 0 ? min(1, Double($0.processed) / Double($0.total)) : nil }
        return VStack(spacing: 16) {
            ProgressView(value: fraction ?? 0.08)
                .tint(.white.opacity(0.85))
                .frame(maxWidth: 320)
            Text("正在整理…" + (progress.map { $0.total > 0 ? " \($0.processed) / \($0.total)" : "" } ?? ""))
                .font(.callout.weight(.medium))
                .foregroundStyle(.white)
            Text("每改名一个文件即同步台账，中断也不会账实不符；期间扫描自动让路。\n关闭窗口不影响整理，库卡片上可继续查看进度。")
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)
                .multilineTextAlignment(.center)
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var doneView: some View {
        ScrollView {
            VStack(spacing: 12) {
                Image(systemName: "checkmark")
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(Theme.success)
                    .frame(width: 44, height: 44)
                    .background(Theme.success.opacity(0.15), in: .circle)
                Text("整理完成")
                    .font(.headline)
                    .foregroundStyle(.white)
                Text(summary)
                    .font(.callout)
                    .foregroundStyle(Theme.textMuted)
                    .multilineTextAlignment(.center)
                if let result, !result.errors.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("\(result.errors.count) 个文件处理时遇到问题（未被改动）")
                            .font(.subheadline.weight(.semibold))
                        ForEach(Array(result.errors.enumerated()), id: \.offset) { _, message in
                            Text(message).font(.caption).textSelection(.enabled)
                        }
                    }
                    .foregroundStyle(Theme.warning)
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Theme.warning.opacity(0.08), in: .rect(cornerRadius: 12))
                    .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.warning.opacity(0.3)))
                }
                Button("完成") { dismiss() }
                    .buttonStyle(.glassProminent)
                    .padding(.top, 8)
            }
            .padding(24)
        }
    }

    private var summary: String {
        var text = "改名归位 \(result?.renamed ?? 0) 个文件"
        if let result {
            if result.sidecarsRenamed > 0 { text += "，附属文件 \(result.sidecarsRenamed) 个随迁" }
            if result.entryAssetsMoved > 0 { text += "，海报/NFO \(result.entryAssetsMoved) 个随条目目录搬迁" }
            if result.removedDirs > 0 { text += "，清理搬空目录 \(result.removedDirs) 个" }
        }
        text += "。"
        if let result, result.skipped > 0 { text += "跳过 \(result.skipped) 个（原因见预览）。" }
        return text
    }

    // MARK: 小部件

    private func bullet(_ markdown: LocalizedStringKey) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text("•")
            Text(markdown).font(.subheadline)
        }
        .opacity(0.9)
    }

    /// 统计小胶囊：预览页顶部的规模速览
    private func statChip(_ label: String, tone: Color) -> some View {
        Text(label)
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(tone)
            .padding(.horizontal, 12)
            .padding(.vertical, 4)
            .background(tone.opacity(0.1), in: .capsule)
            .overlay(Capsule().strokeBorder(tone.opacity(0.3)))
    }
}

/// 统计胶囊的换行布局（本文件私有，与筛选条那份各自独立，避免跨文件依赖）
private struct OrganizeFlowLayout: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0, widest: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > 0, x + size.width > maxWidth {
                y += rowHeight + spacing
                x = 0
                rowHeight = 0
            }
            x += size.width + spacing
            widest = max(widest, x - spacing)
            rowHeight = max(rowHeight, size.height)
        }
        return CGSize(width: proposal.width ?? widest, height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, rowHeight: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > bounds.minX, x + size.width > bounds.maxX {
                y += rowHeight + spacing
                x = bounds.minX
                rowHeight = 0
            }
            subview.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}
