import SwiftUI

/*
 「修正识别结果」（对应 Web components/reidentify-dialog.tsx，issue #107）。

 为什么是两阶段而不是"点一下重新识别"：识别错挂时机器往往是**高置信地错**——
 同样的输入重跑大概率复现同一个错答案。这里把它拆成"先出结论 → 人拍板 → 才落库"：

   打开 → 重走识别链（只读预览，一行台账都不改）
        → 按**结论**分组摆出来（一部剧几十集同一个结论只占一行；真分裂成
          「38 个归 A、2 个归 B」时也如实摊开）
        → 每组三个出口：采用新结论 / 自己搜一个条目 / 标为非独立作品
        → 关掉 = 什么都没发生

 三条刻意的交互决定（同 Web）：
 1. 搜索是一等公民，不藏在"都不对？"后面——正确答案常常压根不在候选里；
 2. 「不是独立作品」必须有——花絮/预告被错挂时用户要说的是"它根本不该是个条目"，
    没有这个出口，最接近的按钮就成了「删除影片」（那是删磁盘）；
 3. 认领一律经确认面板看过海报/简介再落地，与待识别清单同一套面板。
 */

/// 以 .sheet 呈现，自带 NavigationStack 与关闭按钮
struct ReidentifySheet: View {
    let libraryId: Int
    let mediaItemId: Int
    var onApplied: () -> Void = {}

    init(libraryId: Int, mediaItemId: Int, onApplied: @escaping () -> Void = {}) {
        self.libraryId = libraryId
        self.mediaItemId = mediaItemId
        self.onApplied = onApplied
    }

    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss

    @State private var preview: API.ReidentifyPreviewView?
    @State private var error: String?
    /// 已拍板的组 key → 结论文案。留在界面上而不是直接关窗：分裂成多组时
    /// 用户要接着处理剩下的，得看得见哪些已经处理过了
    @State private var settled: [String: String] = [:]

    private var pendingCount: Int {
        preview?.groups.filter { settled[$0.key] == nil }.count ?? 0
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    Text("已重走识别链。下面是机器给出的结论——\(Text("采纳、自己搜一个、或标为非独立作品都由你定").foregroundStyle(Theme.text.opacity(0.8)))，关掉这个窗口则不会有任何改动。")
                        .font(.caption)
                        .foregroundStyle(Theme.textMuted)

                    if let error {
                        Text(error).font(.subheadline).foregroundStyle(Theme.danger)
                    }

                    if preview == nil, error == nil {
                        HStack(spacing: 10) {
                            ProgressView()
                            Text("正在重新识别…")
                        }
                        .font(.subheadline)
                        .foregroundStyle(Theme.textMuted)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 40)
                    }

                    if let preview {
                        previewContent(preview)
                    }
                }
                .padding(.horizontal, Theme.pagePadding)
                .padding(.vertical, 12)
            }
            .scrollDismissesKeyboard(.interactively)
            .safeAreaInset(edge: .bottom) {
                Text(preview != nil && pendingCount == 0 && !(preview?.groups.isEmpty ?? true)
                    ? "全部处理完了"
                    : "改挂会记为人工身份，之后识别器升级不再自动翻案")
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, Theme.pagePadding)
                    .padding(.vertical, 10)
                    .background(.bar)
            }
            .navigationTitle("修正识别结果")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("关闭") { dismiss() }
                }
            }
            .appBackground()
        }
        .task { await load() }
    }

    @ViewBuilder
    private func previewContent(_ preview: API.ReidentifyPreviewView) -> some View {
        ReidentifyCurrentCard(current: preview.current, movie: preview.movie)

        if preview.unreachable {
            Text("有文件因为 TMDB 不可达没能得出结论——那是网络问题不是识别问题。建议先修好网络再来，此刻的结论不足以据此拍板。")
                .font(.caption)
                .foregroundStyle(Color(red: 0xF5 / 255, green: 0xD4 / 255, blue: 0x89 / 255))
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Theme.warning.opacity(0.08), in: .rect(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Theme.warning.opacity(0.3)))
        }

        if preview.pinnedIdentity {
            Text("这些文件的身份被\(Text("目录名的 tmdbid 标记或 NFO 钉死").foregroundStyle(Theme.text.opacity(0.8)))。在这里改挂会记为人工身份、扫描不会再翻案，条目 NFO 里矛盾的 tmdbid 也会被一并改正；但目录名上的标记需要你自己改，否则下次整理/重扫的命名仍会带着旧 id。")
                .font(.caption)
                .foregroundStyle(Theme.textMuted)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.white.opacity(0.03), in: .rect(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.white.opacity(0.1)))
        }

        if preview.groups.isEmpty {
            Text("这个条目在本库没有可参与识别的在位文件。")
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 24)
        }

        ForEach(preview.groups, id: \.key) { group in
            ReidentifyGroupRow(
                group: group,
                movie: preview.movie,
                searchSeed: preview.searchSeed,
                settledMessage: settled[group.key]
            ) { message in
                settled[group.key] = message
                onApplied()
            }
        }

        if preview.skippedMissing > 0 {
            Text("另有 \(preview.skippedMissing) 个文件已从磁盘消失（缺失清单里那些），没有实体可供识别，保持原身份不参与。")
                .font(.caption)
                .foregroundStyle(Theme.textFaint)
        }
    }

    private func load() async {
        preview = nil
        error = nil
        settled = [:]
        do {
            preview = try await api.libraryItemsPreviewReidentification(libraryId: libraryId, mediaItemId: mediaItemId)
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }
}

/// 身份来源 → 中文短语（结论卡上说明"凭什么这么认"）
private let reidentifySourceLabels: [String: String] = [
    "path_tag": "目录名的 tmdbid 标记",
    "nfo": "NFO",
    "resolved": "名称解析 + TMDB 收敛",
]

/// 没命中的失败分类 → 一句人话（与待识别清单的标签同源，措辞按本场景调整）
private let reidentifyCodeLabels: [String: String] = [
    "tmdb_unreachable": "TMDB 不可达",
    "kind_mismatch": "类型与本库不符",
    "ambiguous": "候选之间分不出",
    "no_match": "TMDB 里没找到匹配",
    "unparsable": "从文件名认不出片名",
]

/// 现身份卡：拍板前先看清"现在挂的是谁"
private struct ReidentifyCurrentCard: View {
    let current: API.ReviewItemView
    let movie: Bool
    @Environment(\.api) private var api

    var body: some View {
        HStack(spacing: 12) {
            RemoteImage(url: api.image(current.posterUrl, .posterCard))
                .frame(width: 44, height: 64)
                .clipShape(.rect(cornerRadius: 8))
            VStack(alignment: .leading, spacing: 2) {
                Text("当前身份").font(.caption).foregroundStyle(Theme.textFaint)
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(current.title)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Theme.text.opacity(0.95))
                        .lineLimit(1)
                    if let year = current.year {
                        Text(String(year)).font(.subheadline).foregroundStyle(Theme.textMuted)
                    }
                }
                if let tmdbId = current.tmdbId {
                    Link("TMDB #\(String(tmdbId)) ↗", destination: IssueTMDBLink.title(movie: movie, id: tmdbId))
                        .font(.caption)
                        .foregroundStyle(Theme.textMuted)
                        .underline()
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(12)
        .background(Color.white.opacity(0.03), in: .rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.line))
    }
}

/// 一组结论：文件清单 + 机器结论 + 三个出口
private struct ReidentifyGroupRow: View {
    let group: API.ReidentifyGroupView
    let movie: Bool
    let searchSeed: String
    let settledMessage: String?
    let onSettled: (String) -> Void

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @State private var busy = false
    @State private var error: String?
    @State private var panel: IssueClaimPanelState?

    private var outcome: API.ReidentifyOutcomeView { group.outcome }

    var body: some View {
        if let settledMessage {
            Text("✓ \(settledMessage)")
                .font(.subheadline)
                .foregroundStyle(Theme.text.opacity(0.85))
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Theme.accent.opacity(0.08), in: .rect(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.accent.opacity(0.25)))
        } else {
            card
        }
    }

    private var card: some View {
        VStack(alignment: .leading, spacing: 8) {
            // 是哪些文件——分裂成多组时这行是唯一的辨认依据
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("\(group.fileCount) 个文件")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(Theme.text.opacity(0.85))
                    Text(Formatters.bytes(group.totalSizeBytes))
                        .font(.caption)
                        .foregroundStyle(Theme.textFaint)
                }
                Text(group.sampleNames.joined(separator: "、") + (group.fileCount > group.sampleNames.count ? " …" : ""))
                    .font(.caption.monospaced())
                    .foregroundStyle(Theme.textFaint)
                    .lineLimit(2)
                    .truncationMode(.middle)
            }

            Divider().overlay(Theme.line)
            outcomeView

            // 候选：机器拿不定主意时留下的可能匹配，点一下先看详情
            if !outcome.candidates.isEmpty {
                IssueCandidateChips(
                    candidates: outcome.candidates,
                    selectedId: panel?.selectedTmdbId,
                    showsEpisodeCount: false,
                    disabled: busy
                ) { candidate in
                    panel = panel?.selectedTmdbId == candidate.tmdbId ? nil : .confirm(IssueClaimSeed(candidate: candidate))
                }
            }

            // 两个常驻出口。搜索不藏在二级：机器高置信错挂时正确答案常常压根不在候选里
            HStack(spacing: 8) {
                Button("都不对，自己搜…") { panel = panel == .search ? nil : .search }
                    .buttonStyle(.glass)
                    .tint(panel == .search ? Theme.accentStrong : nil)
                Spacer(minLength: 0)
                Button {
                    Task { await detach() }
                } label: {
                    Text("不是独立作品").foregroundStyle(Color(red: 1, green: 0xB4 / 255, blue: 0xB4 / 255))
                }
                .buttonStyle(.glass)
            }
            .controlSize(.small)
            .disabled(busy)

            if let error {
                Text(error).font(.caption).foregroundStyle(Theme.danger)
            }

            switch panel {
            case .search:
                IssueClaimSearchPanel(movie: movie, initialQuery: searchSeed) { seed in
                    panel = .confirm(seed)
                }
            case let .confirm(seed):
                IssueClaimConfirmPanel(
                    seed: seed, movie: movie, fileCount: group.fileCount, busy: busy,
                    onConfirm: { Task { await assign(seed) } },
                    onCancel: { panel = nil }
                )
                .id(seed.tmdbId)
            case nil:
                EmptyView()
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.03), in: .rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.line))
    }

    /// 机器结论：命中给海报片名与依据（不同于现身份时给「改挂到这里」），没命中给原因
    @ViewBuilder
    private var outcomeView: some View {
        if outcome.mediaItemId != nil {
            HStack(spacing: 12) {
                RemoteImage(url: api.image(outcome.posterUrl, .posterCard))
                    .frame(width: 40, height: 56)
                    .clipShape(.rect(cornerRadius: 4))
                VStack(alignment: .leading, spacing: 2) {
                    Text(outcome.sameAsCurrent ? "重新识别的结论：与当前一致" : "重新识别的结论")
                        .font(.caption)
                        .foregroundStyle(Theme.textFaint)
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(outcome.title ?? "")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(Theme.text.opacity(0.95))
                            .lineLimit(1)
                        if let year = outcome.year {
                            Text(String(year)).font(.subheadline).foregroundStyle(Theme.textMuted)
                        }
                    }
                    if let source = outcome.source {
                        Text("依据：\(reidentifySourceLabels[source] ?? source)")
                            .font(.caption)
                            .foregroundStyle(Theme.textFaint)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                if !outcome.sameAsCurrent, let tmdbId = outcome.tmdbId {
                    Button("改挂到这里") {
                        panel = .confirm(IssueClaimSeed(
                            tmdbId: tmdbId, title: outcome.title ?? "", year: outcome.year, posterUrl: outcome.posterUrl
                        ))
                    }
                    .buttonStyle(.glassProminent)
                    .fontWeight(.semibold)
                    .controlSize(.small)
                    .disabled(busy)
                }
            }
        } else {
            let code = outcome.code.map { "（\(reidentifyCodeLabels[$0] ?? $0)）" } ?? ""
            let reason = outcome.reason.map { "：\($0)" } ?? "。"
            Text("没能认出来\(code)\(reason)")
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)
        }
    }

    private func act(_ message: String, _ work: () async throws -> Void) async {
        busy = true
        error = nil
        defer { busy = false }
        do {
            try await work()
            panel = nil
            onSettled(message)
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func assign(_ seed: IssueClaimSeed) async {
        let ref = "tmdb:\(movie ? "movie" : "tv"):\(seed.tmdbId)"
        await act("\(group.fileCount) 个文件已改挂为《\(seed.title)》") {
            _ = try await api.libraryIdentificationAssignFilesToTitle(body: .init(fileIds: group.fileIds, titleRef: ref))
        }
    }

    private func detach() async {
        guard await feedback.confirm(
            "把这 \(group.fileCount) 个文件标为「非独立作品」？",
            message: "它们会摘掉身份、不再占用库存，之后扫描也不再过问。适合花絮、预告、片段这类本就不该单独成条目的内容。磁盘文件一个字节都不会动，随时可以在「已忽略」清单里恢复。",
            confirmTitle: "标为非独立作品",
            destructive: true
        ) else { return }
        await act("\(group.fileCount) 个文件已标为非独立作品") {
            _ = try await api.libraryIdentificationMarkFilesAsExtras(body: .init(fileIds: group.fileIds))
        }
    }
}
