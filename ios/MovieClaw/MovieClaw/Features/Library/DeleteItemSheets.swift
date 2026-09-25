import SwiftUI

/// 「删除影片」弹层（Web `library-item-detail-view.tsx` 的 DeleteDialog）。
///
/// 全站唯一真删磁盘的操作，所以把后果一次性讲透：列出将被清掉的目录、文件数与体积，
/// 勾选「我已明白」后才放开红色按钮；完成后展示实际删除的路径清单，让用户对
/// 「删了什么」有完整凭据。条目详情（目录、文件数、体积）由弹层自己拉，调用方只给 id。
///
/// 删除完成后弹层不可下滑关闭：只能点「回到库存页」（或左上「关闭」，行为相同），
/// 保证调用方一定收到 `onDeleted` 去离开已经不存在的条目页。
struct DeleteItemSheet: View {
    let libraryId: Int
    let mediaItemId: Int
    let title: String
    /// 用户在结果页点「回到库存页」时回调（调用方负责离开条目页并刷新库存）
    var onDeleted: () -> Void = {}

    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss

    @State private var detail: Loadable<API.LibraryItemDetailView> = .loading
    @State private var confirmed = false
    @State private var busy = false
    @State private var result: API.ItemDeleteResultView?
    @State private var error: String?

    var body: some View {
        NavigationStack {
            AsyncContent(detail, retry: load) { detail in
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        if let result {
                            DeleteResultView(
                                title: result.errors.isEmpty ? "已从磁盘彻底删除" : "部分删除完成",
                                result: result,
                                emptyText: "没有删除任何磁盘路径"
                            )
                        } else {
                            confirmStep(detail)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(Theme.pagePadding)
                }
                .safeAreaInset(edge: .bottom) { bottomBar }
            }
            .navigationTitle("删除影片")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(result == nil ? "取消" : "关闭") { result == nil ? dismiss() : finish() }
                        .disabled(busy)
                }
            }
        }
        .presentationDetents([.large])
        .interactiveDismissDisabled(busy || result != nil)
        .task { await load() }
    }

    @ViewBuilder
    private func confirmStep(_ detail: API.LibraryItemDetailView) -> some View {
        Label {
            Text("删除「\(title)」")
        } icon: {
            Image(systemName: "trash").foregroundStyle(Theme.danger)
        }
        .font(.headline)
        .foregroundStyle(Theme.text)

        Text("这不是「从列表移除」——将把下列目录从磁盘\(Text("彻底删除").bold().foregroundStyle(Theme.danger))，包括视频文件、NFO、海报、字幕等全部刮削产物，共 \(Text("\(detail.fileCount)").bold()) 个媒体文件、\(Text(Formatters.bytes(detail.totalSizeBytes)).bold())。此操作不可恢复。")
            .font(.body)
            .foregroundStyle(.white.opacity(0.8))
            .monospacedDigit()
            .padding(.top, 12)

        PathListBox(paths: detail.entryDirs.isEmpty ? detail.files.map(\.filePath) : detail.entryDirs, folderIcon: true)
            .padding(.top, 16)

        AcknowledgeToggle(isOn: $confirmed, text: "我已明白：以上目录及其中全部文件将被永久删除，无法恢复。")
            .padding(.top, 16)

        if let error {
            Text(error).font(.subheadline).foregroundStyle(Theme.danger).padding(.top, 12)
        }
    }

    private var bottomBar: some View {
        HStack {
            Spacer()
            if result != nil {
                Button("回到库存页", action: finish).buttonStyle(.glassProminent)
            } else {
                DangerButton(busy: busy, enabled: confirmed && detail.value != nil) {
                    Task { await run() }
                }
            }
        }
        .padding(.horizontal, Theme.pagePadding)
        .padding(.vertical, 12)
        .background(.bar)
    }

    private func load() async {
        await Loadable.load(into: $detail) {
            try await api.libraryItemsGet(libraryId: libraryId, mediaItemId: mediaItemId)
        }
    }

    private func run() async {
        busy = true
        error = nil
        defer { busy = false }
        do {
            result = try await api.libraryItemsDelete(libraryId: libraryId, mediaItemId: mediaItemId)
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription.isEmpty ? "删除失败，请稍后重试" : error.localizedDescription
        }
    }

    private func finish() {
        dismiss()
        onDeleted()
    }
}

/// 「删除此文件」弹层（Web DeleteFileDialog）：条目删除的文件级姊妹（多版本洗掉一个 / 删某集重下）。
///
/// 与 DeleteItemSheet 同一套三段式：讲透后果 → 勾选确认 → 红色按钮 → 结果清单。两条必须讲清的规则：
/// 1. 条目在本库只剩这一行台账时，后端会**升级为整条目删除**（不留只剩 NFO/海报的空目录）；
/// 2. 该单元有订阅盯着时，删掉最后一份拷贝订阅会自动重新下载补齐。
/// 判断「是不是最后一个文件」与剧集的季集标签都要条目详情，由弹层自己拉。
struct DeleteFileSheet: View {
    let libraryId: Int
    let mediaItemId: Int
    let file: API.LibraryFileView
    /// 只删掉了这个文件、条目仍在时回调（调用方重拉详情）
    var onDeleted: () -> Void = {}
    /// 删的是最后一个文件、整条目随之删除时回调（调用方应离开条目页）；不传则回落到 onDeleted
    var onItemDeleted: (() -> Void)? = nil

    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss

    @State private var detail: Loadable<API.LibraryItemDetailView> = .loading
    @State private var confirmed = false
    @State private var busy = false
    @State private var result: API.ItemDeleteResultView?
    @State private var error: String?

    var body: some View {
        NavigationStack {
            AsyncContent(detail, retry: load) { detail in
                // 条目在本库只剩这一行台账（含缺失行）→ 后端会升级为整条目删除
                let isLast = detail.files.count == 1
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        if let result {
                            DeleteResultView(
                                title: result.errors.isEmpty ? "已从磁盘删除" : "删除失败",
                                result: result,
                                emptyText: "没有删除任何磁盘路径" + (file.missing ? "（文件本就缺失，仅清除了台账记录）" : "")
                            )
                        } else {
                            confirmStep(detail, isLast: isLast)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(Theme.pagePadding)
                }
                .safeAreaInset(edge: .bottom) { bottomBar(isLast: isLast) }
            }
            .navigationTitle("删除文件")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(result == nil ? "取消" : "关闭") {
                        if result == nil { dismiss() } else { finish(isLast: detail.value?.files.count == 1) }
                    }
                    .disabled(busy)
                }
            }
        }
        .presentationDetents([.large])
        .interactiveDismissDisabled(busy || result != nil)
        .task { await load() }
    }

    @ViewBuilder
    private func confirmStep(_ detail: API.LibraryItemDetailView, isLast: Bool) -> some View {
        Label {
            Text("删除文件")
        } icon: {
            Image(systemName: "trash").foregroundStyle(Theme.danger)
        }
        .font(.headline)
        .foregroundStyle(Theme.text)

        Group {
            if file.missing {
                Text("该文件在磁盘上已缺失，删除只会清掉这条台账记录。")
            } else {
                Text("将把下列文件从磁盘\(Text("彻底删除").bold().foregroundStyle(Theme.danger))，同名的 NFO/字幕/图片附属文件一并清除。此操作不可恢复。")
            }
        }
        .font(.body)
        .foregroundStyle(.white.opacity(0.8))
        .padding(.top, 12)

        VStack(alignment: .leading, spacing: 4) {
            Text("\(episodeLabel(detail))\(file.fileName)  \(Text(Formatters.bytes(file.sizeBytes)).font(.subheadline).foregroundStyle(Theme.textMuted))")
                .foregroundStyle(Theme.text)
                .monospacedDigit()
            Text(file.filePath)
                .font(.caption.monospaced())
                .foregroundStyle(.white.opacity(0.5))
                .textSelection(.enabled)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.03), in: .rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.08)))
        .padding(.top, 16)

        if isLast {
            Text("这是「\(detail.title)」在本库的最后一个文件——删除将升级为整条目删除，整个刮削目录（含 NFO/海报）一并清除，条目将从库存消失。")
                .font(.subheadline)
                .foregroundStyle(Theme.warning)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Theme.warning.opacity(0.06), in: .rect(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.warning.opacity(0.3)))
                .padding(.top, 12)
        }

        Text("若该作品有订阅且删除后此单元不再有其他拷贝，订阅会将其视为缺失并自动重新下载。")
            .font(.caption)
            .foregroundStyle(Theme.textFaint)
            .padding(.top, 12)

        AcknowledgeToggle(
            isOn: $confirmed,
            text: "我已明白：\(isLast ? "整个条目目录及其中全部文件" : "该文件及其同名附属文件")将被永久删除，无法恢复。"
        )
        .padding(.top, 16)

        if let error {
            Text(error).font(.subheadline).foregroundStyle(Theme.danger).padding(.top, 12)
        }
    }

    /// 剧集文件前的「S01E02」标签（电影不显示）
    private func episodeLabel(_ detail: API.LibraryItemDetailView) -> Text {
        guard detail.kind != "movie", file.episodeNumber > 0 || file.seasonNumber > 0 else { return Text("") }
        let label = String(format: "S%02dE%02d", file.seasonNumber, file.episodeNumber)
        return Text("\(Text(label).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.accent))  ")
    }

    private func bottomBar(isLast: Bool) -> some View {
        HStack {
            Spacer()
            if result != nil {
                Button("完成") { finish(isLast: isLast) }.buttonStyle(.glassProminent)
            } else {
                DangerButton(busy: busy, enabled: confirmed) {
                    Task { await run() }
                }
            }
        }
        .padding(.horizontal, Theme.pagePadding)
        .padding(.vertical, 12)
        .background(.bar)
    }

    private func load() async {
        await Loadable.load(into: $detail) {
            try await api.libraryItemsGet(libraryId: libraryId, mediaItemId: mediaItemId)
        }
    }

    private func run() async {
        busy = true
        error = nil
        defer { busy = false }
        do {
            result = try await api.libraryItemsDeleteFile(libraryId: libraryId, mediaItemId: mediaItemId, fileId: file.id)
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription.isEmpty ? "删除失败，请稍后重试" : error.localizedDescription
        }
    }

    /// 同 Web：最后一个文件且无错误 = 整条目已删，调用方应离开条目页；否则只重拉详情
    private func finish(isLast: Bool) {
        let deletedItem = isLast && (result?.errors.isEmpty ?? false)
        dismiss()
        if deletedItem, let onItemDeleted {
            onItemDeleted()
        } else {
            onDeleted()
        }
    }
}

// MARK: - 共用小件（仅本文件）

/// 删除结果：实际删除的路径清单 + 错误 + 「已清理 N 条台账，释放 X」
private struct DeleteResultView: View {
    let title: String
    let result: API.ItemDeleteResultView
    let emptyText: String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title).font(.headline).foregroundStyle(Theme.text)
            if result.removedPaths.isEmpty {
                Text(emptyText)
                    .font(.subheadline)
                    .foregroundStyle(Theme.textMuted)
                    .padding(14)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.white.opacity(0.03), in: .rect(cornerRadius: 12))
                    .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.08)))
            } else {
                PathListBox(paths: result.removedPaths, folderIcon: false)
            }
            if !result.errors.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(result.errors, id: \.self) { Text($0) }
                }
                .font(.subheadline)
                .foregroundStyle(Theme.danger)
            }
            Text("已清理 \(result.rowsDeleted) 条台账，释放 \(Formatters.bytes(result.freedBytes))。")
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)
        }
    }
}

/// 等宽路径清单框
private struct PathListBox: View {
    let paths: [String]
    let folderIcon: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(paths, id: \.self) { path in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    if folderIcon {
                        Image(systemName: "folder").foregroundStyle(.white.opacity(0.4))
                    }
                    Text(path).foregroundStyle(.white.opacity(0.7))
                }
                .font(.caption.monospaced())
            }
        }
        .textSelection(.enabled)
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.03), in: .rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.08)))
    }
}

/// 「我已明白…」勾选（对应 Web 的 checkbox）
private struct AcknowledgeToggle: View {
    @Binding var isOn: Bool
    let text: String

    var body: some View {
        Button {
            isOn.toggle()
        } label: {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Image(systemName: isOn ? "checkmark.square.fill" : "square")
                    .foregroundStyle(isOn ? Theme.danger : Theme.textMuted)
                Text(text)
                    .font(.subheadline)
                    .foregroundStyle(.white.opacity(0.8))
                    .multilineTextAlignment(.leading)
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
    }
}

/// 红色「彻底删除 / 正在删除…」按钮
private struct DangerButton: View {
    let busy: Bool
    let enabled: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                if busy { ProgressView().controlSize(.small).tint(.white) }
                Text(busy ? "正在删除…" : "彻底删除").font(.body.weight(.semibold))
            }
        }
        .buttonStyle(.glassProminent)
        .tint(Theme.danger)
        .disabled(!enabled || busy)
    }
}
