import SwiftUI

/// 「转移到其他媒体库…」弹层（Web `components/library-item-detail-view.tsx` 的
/// TransferDialog / TransferPlanPreview / TransferResult）。
///
/// 分错库时的补救：把条目**磁盘上的目录**连同库存记录搬到另一个同类型库。
/// 界面上必须讲清的三件事（都在预览步骤里，与 Web 一致）：
/// 1. 搬的是磁盘目录，不是「从列表里挪一下」——源 / 目标路径并排列出；
/// 2. 跨盘要完整复制：耗时按体积走，且断开与做种目录的硬链接（占用翻倍），必须显式警示；
/// 3. 目标已有同名目录就不干（`blocked` 非空不给执行），绝不覆盖或合并。
///
/// 流程：选目标库 → 立即只读预检 `transfer-preview` → 确认转移 `POST …/transfers`
/// → 每 1 秒轮询 `item-transfer-status` 直到结论页。轮询刻意不用 `.polling`（它会随
/// 前后台暂停）：这是用户正盯着的短任务，回到前台应直接看到结果——与 Web 不用
/// useVisiblePolling 的理由相同。进行中不给「取消」（搬到一半停不下来），弹层也不可下滑关闭。
struct TransferItemSheet: View {
    let libraryId: Int
    let mediaItemId: Int
    let title: String
    /// 转移成功完成时回调（对应 Web onFinished：让调用方刷新 / 作废缓存；不要在这里做页面跳转）
    var onDone: () -> Void = {}

    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @Environment(\.dismiss) private var dismiss

    /// 当前所在库；候选目标库与文案都按它的形态 × 来源决定
    @State private var sourceLibrary: API.LibraryView?
    /// 取不到库时的兜底（条目自身的形态与来源）
    @State private var fallbackKind: String?
    @State private var fallbackSource: String?
    /// 可选目标库：同类型、同来源、非当前库；nil = 正在读取
    @State private var candidates: [API.LibraryView]?
    @State private var targetId: Int?
    @State private var preview: API.TransferPreviewView?
    @State private var loadingPreview = false
    @State private var error: String?
    /// 进行中 / 已完成的转移状态（轮询同一个接口一路走到结论）
    @State private var status: API.TransferStatusView?
    @State private var starting = false
    @State private var notifiedDone = false

    private var running: Bool { status?.running ?? false }
    private var busy: Bool { starting || running }
    private var finished: Bool { status.map { !$0.running } ?? false }
    private var canRun: Bool {
        guard let preview else { return false }
        return preview.blocked.isEmpty && !preview.moves.isEmpty && !busy
    }

    /// 一文件一条目的库（其他 / 图片）：搬的是文件本身，不是「条目目录」
    private var fileEntries: Bool { sourceLibrary.map { !$0.capabilities.scraped } ?? false }
    private var sourceKind: String? { sourceLibrary?.kind ?? fallbackKind }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    if let status, finished {
                        TransferResultView(status: status)
                    } else {
                        planStep
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(Theme.pagePadding)
            }
            .safeAreaInset(edge: .bottom) { bottomBar }
            .navigationTitle("转移到其他媒体库")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(finished ? "关闭" : "取消") { dismiss() }.disabled(busy)
                }
            }
        }
        .presentationDetents([.large])
        .interactiveDismissDisabled(busy)
        .task { await loadCandidates() }
        .task(id: targetId) { await loadPreview() }
        .task(id: running) { await pollWhileRunning() }
    }

    // MARK: - 第一步 + 第二步：选目标库、预览计划

    @ViewBuilder
    private var planStep: some View {
        Label {
            Text("把「\(title)」转移到其他媒体库")
        } icon: {
            Image(systemName: "folder").foregroundStyle(Theme.accent)
        }
        .font(.headline)
        .foregroundStyle(Theme.text)

        // 搬运单元按库的能力档案分叉，文案必须跟着分叉：本地内容库一文件一条目，
        // 照抄影视库的说法会让用户以为整个分组目录都要被搬走
        Group {
            if fileEntries {
                Text("放错库时用它补救。\(Text("这个条目的文件").foregroundStyle(Theme.text))（连同同名的 NFO、字幕）会连同库存记录一起搬到目标库；文件在库里的所在目录结构原样保留，同目录下别的条目留在原地不动。")
            } else {
                Text("分错库时用它补救（例如韩剧被判进了「大陆华语剧」）。\(Text("磁盘上的整个条目目录").foregroundStyle(Theme.text))（视频、NFO、海报、字幕）会连同库存记录一起搬到目标库；目录名原样保留，需要规范化请到目标库运行「整理文件名」。")
            }
        }
        .font(.subheadline)
        .foregroundStyle(Theme.textMuted)
        .padding(.top, 8)

        Text("转移到")
            .font(.caption.weight(.semibold))
            .tracking(2)
            .foregroundStyle(Theme.textFaint)
            .padding(.top, 20)

        Group {
            if let candidates {
                if candidates.isEmpty {
                    Text("没有其他\(transferKindLabels[sourceKind ?? ""] ?? "")库可选——请先在「媒体库」页新建一个，再回来转移。")
                        .font(.subheadline)
                        .foregroundStyle(Theme.warning)
                } else {
                    VStack(spacing: 6) {
                        ForEach(candidates, id: \.id) { lib in
                            candidateRow(lib)
                        }
                    }
                }
            } else if error == nil {
                Text("正在读取媒体库…").font(.subheadline).foregroundStyle(Theme.textMuted)
            }
        }
        .padding(.top, 8)

        if loadingPreview {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("正在计算转移计划…")
            }
            .font(.subheadline)
            .foregroundStyle(Theme.textMuted)
            .padding(.top, 16)
        }
        if let preview {
            TransferPlanPreviewView(preview: preview, sourceName: sourceLibrary?.name)
                .padding(.top, 16)
        }
        if let error {
            Text(error).font(.subheadline).foregroundStyle(Theme.danger).padding(.top, 12)
        }
    }

    private func candidateRow(_ lib: API.LibraryView) -> some View {
        let selected = targetId == lib.id
        return Button {
            targetId = lib.id
        } label: {
            HStack(spacing: 12) {
                Image(systemName: selected ? "largecircle.fill.circle" : "circle")
                    .foregroundStyle(selected ? Theme.accent : Theme.textFaint)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 8) {
                        Text(lib.name).font(.body.weight(.medium)).foregroundStyle(Theme.text)
                        if lib.isDefault {
                            Text("默认库").font(.caption).foregroundStyle(Theme.textFaint)
                        }
                    }
                    Text(lib.primaryRoot ?? "未配置根路径")
                        .font(.caption.monospaced())
                        .foregroundStyle(.white.opacity(0.45))
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(selected ? Theme.accent.opacity(0.1) : Color.white.opacity(0.03), in: .rect(cornerRadius: 12))
            .overlay(
                RoundedRectangle(cornerRadius: 12)
                    .strokeBorder(selected ? Theme.accent.opacity(0.6) : Color.white.opacity(0.08))
            )
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .disabled(busy)
        .opacity(busy ? 0.5 : 1)
    }

    // MARK: - 底栏：进行中只留进度；完成后「留在本页 / 去目标库查看」

    private var bottomBar: some View {
        VStack(alignment: .leading, spacing: 10) {
            if running {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small).tint(Theme.info)
                    Text("正在转移\(progressSuffix)；跨盘转移需要完整复制文件，请勿关闭页面以外的操作")
                        .lineLimit(2)
                }
                .font(.subheadline)
                .foregroundStyle(Theme.info)
            }
            HStack(spacing: 12) {
                Spacer()
                if let status, finished {
                    Button("留在本页") { dismiss() }
                        .buttonStyle(.glass)
                    if status.errors.isEmpty, let target = status.targetLibraryId {
                        Button("去目标库查看") { goToTarget(target) }
                            .buttonStyle(.glassProminent)
                    }
                } else {
                    Button {
                        Task { await run() }
                    } label: {
                        HStack(spacing: 6) {
                            if starting { ProgressView().controlSize(.small) }
                            Text("确认转移").font(.body.weight(.semibold))
                        }
                    }
                    .buttonStyle(.glassProminent)
                    .disabled(!canRun)
                }
            }
        }
        .padding(.horizontal, Theme.pagePadding)
        .padding(.vertical, 12)
        .background(.bar)
    }

    private var progressSuffix: String {
        guard let status, status.total > 0 else { return "" }
        return " · \(status.processed)/\(status.total)"
    }

    // MARK: - 动作

    private func loadCandidates() async {
        guard candidates == nil else { return }
        sourceLibrary = try? await api.libraryGet(libraryId: libraryId)
        if sourceLibrary == nil, let detail = try? await api.libraryItemsGet(libraryId: libraryId, mediaItemId: mediaItemId) {
            fallbackKind = detail.kind
            fallbackSource = detail.source
        }
        // 口径必须与后端 assert_transferable 一致：比的是**库**的来源，不是条目的来源
        let kind = sourceKind
        let source = sourceLibrary?.source ?? fallbackSource
        do {
            let libs = try await api.libraryList(kind: kind, scope: "all")
            candidates = libs.filter { $0.id != libraryId && $0.source == source }
        } catch is CancellationError {
        } catch {
            self.error = "读取媒体库列表失败，请稍后重试"
        }
    }

    /// 选中目标库就立刻算预览：用户要先看清「搬到哪、搬多少」才谈得上确认
    private func loadPreview() async {
        guard let targetId else { return }
        loadingPreview = true
        preview = nil
        error = nil
        do {
            let data = try await api.libraryItemsPreviewTransfer(libraryId: libraryId, mediaItemId: mediaItemId, targetLibraryId: targetId)
            guard !Task.isCancelled else { return }
            preview = data
            loadingPreview = false
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
            loadingPreview = false
        }
    }

    private func run() async {
        guard let targetId else { return }
        starting = true
        error = nil
        defer { starting = false }
        do {
            _ = try await api.libraryItemsTransfer(
                libraryId: libraryId,
                mediaItemId: mediaItemId,
                body: API.TransferPayload(targetLibraryId: targetId)
            )
            apply(try await api.libraryItemsGetTransferStatus(libraryId: libraryId))
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription.isEmpty ? "转移失败，请稍后重试" : error.localizedDescription
        }
    }

    /// 转移进行中每秒拉一次进度，跑完自动切到结论页
    private func pollWhileRunning() async {
        guard running else { return }
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(1))
            if Task.isCancelled { break }
            if let next = try? await api.libraryItemsGetTransferStatus(libraryId: libraryId) {
                apply(next)
                if !next.running { break }
            }
        }
    }

    private func apply(_ next: API.TransferStatusView) {
        status = next
        if !next.running, next.errors.isEmpty, next.targetLibraryId != nil, !notifiedDone {
            notifiedDone = true
            onDone()
        }
    }

    /// 同 Web `router.replace(/library/{target}/item/{id})`：当前条目页已不在原库，换成目标库里的同一条目
    private func goToTarget(_ target: Int) {
        dismiss()
        router.pop()
        router.push(.libraryItem(libraryId: target, itemId: mediaItemId))
    }
}

private let transferKindLabels = ["movie": "电影", "tv": "剧集", "video": "其他", "photo": "图片"]

/// 转移计划预览：阻断原因、跨盘警示、搬运清单（源 → 目标）、跳过说明
private struct TransferPlanPreviewView: View {
    let preview: API.TransferPreviewView
    let sourceName: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if !preview.blocked.isEmpty {
                box(tint: Theme.danger) {
                    ForEach(preview.blocked, id: \.self) { Text($0) }
                }
                .foregroundStyle(Theme.danger)
            }

            if preview.crossDevice, !preview.moves.isEmpty {
                box(tint: Theme.warning) {
                    Text("目标库与当前位置\(Text("不在同一块盘").bold())：文件需要完整复制（\(Formatters.bytes(preview.totalBytes))），耗时取决于体积与盘速；复制会产生新文件，与下载器做种目录的硬链接关系将断开，两边各占一份空间。")
                }
                .foregroundStyle(Theme.warning)
            }

            if !preview.moves.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text(summary).font(.caption).foregroundStyle(Theme.textFaint)
                    ForEach(preview.moves, id: \.sourcePath) { move in
                        VStack(alignment: .leading, spacing: 2) {
                            Text((sourceName.map { "\($0) · " } ?? "") + move.sourcePath)
                                .foregroundStyle(.white.opacity(0.45))
                            Text("↳ \(preview.targetLibraryName) · \(move.targetPath)")
                                .foregroundStyle(.white.opacity(0.8))
                        }
                        .font(.caption.monospaced())
                        .textSelection(.enabled)
                    }
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.white.opacity(0.03), in: .rect(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.08)))
            }

            if !preview.skips.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(preview.skips, id: \.filePath) { skip in
                        Text("\(Text(skip.filePath).font(.caption.monospaced()))：\(skip.reason)")
                    }
                }
                .font(.caption)
                .foregroundStyle(.white.opacity(0.6))
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.white.opacity(0.02), in: .rect(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.08)))
            }
        }
    }

    private var summary: String {
        var text = "\(preview.moves.count) 个路径 · \(Formatters.bytes(preview.totalBytes))"
        if preview.missingCount > 0 { text += " · \(preview.missingCount) 个缺失记录随迁" }
        return text
    }

    private func box<Content: View>(tint: Color, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 4, content: content)
            .font(.subheadline)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(tint.opacity(0.08), in: .rect(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(tint.opacity(0.3)))
    }
}

/// 转移结论页：搬了哪些路径、随迁多少台账、订阅是否跟着改挂
private struct TransferResultView: View {
    let status: API.TransferStatusView

    var body: some View {
        let failed = !status.errors.isEmpty
        VStack(alignment: .leading, spacing: 12) {
            Text(failed ? "部分转移完成" : "「\(status.title ?? "")」已转移到「\(status.targetLibraryName ?? "")」")
                .font(.headline)
                .foregroundStyle(Theme.text)

            VStack(alignment: .leading, spacing: 2) {
                if status.movedPaths.isEmpty {
                    Text("没有搬运任何磁盘路径").font(.subheadline).foregroundStyle(Theme.textMuted)
                } else {
                    ForEach(status.movedPaths, id: \.self) { path in
                        Text(path).font(.caption.monospaced()).foregroundStyle(.white.opacity(0.7))
                    }
                }
            }
            .textSelection(.enabled)
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.white.opacity(0.03), in: .rect(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.08)))

            if failed {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(status.errors, id: \.self) { Text($0) }
                }
                .font(.subheadline)
                .foregroundStyle(Theme.danger)
            }

            Text(conclusion).font(.subheadline).foregroundStyle(Theme.textMuted)
        }
    }

    private var conclusion: String {
        var text = "已随迁 \(status.filesRelocated) 条库存记录（\(Formatters.bytes(status.bytesMoved))）"
        if status.removedDirs > 0 { text += "，清理空目录 \(status.removedDirs) 个" }
        text += "。"
        if status.subscriptionMoved { text += "该片的订阅已一并改挂到目标库，后续剧集直接入新库。" }
        return text
    }
}
