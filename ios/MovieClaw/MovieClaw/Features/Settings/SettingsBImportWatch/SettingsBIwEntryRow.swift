import SwiftUI

/// 台账条目一行（对应 Web `EntryRow`）：条目名 + 处理结论 + 认领 / 忽略 / 恢复处理。
///
/// - 待处理 / 失败的条目可「认领」（搜索 TMDB 选作品 → 看详情确认 → 立即整理入库）或「忽略」；
///   已忽略的条目可「恢复处理」。
/// - 电影合集（issue #438）：已认出的几部已入库，剩下识别不出的视频逐个认领；整条认领会把
///   整个合集钉成同一部片，所以此时不给条目级「认领」按钮，改为每个视频一个。
/// - 认领面板直接复用媒体库「待识别」的共享面板（`IssueClaimSearchPanel` / `IssueClaimConfirmPanel`），
///   与 Web 两处共用 `claim-panels.tsx` 同构，认领体验一处改两处生效。
struct SettingsBIwEntryRow: View {
    let entry: API.IngestEntryView
    let movie: Bool
    let onChanged: () -> Void

    /// 内联面板：搜索或确认；file 为电影合集里正在认领的那个视频（整条认领时为 nil）
    private enum Panel: Equatable {
        case search(file: String?)
        case confirm(seed: IssueClaimSeed, file: String?)

        var file: String? {
            switch self {
            case let .search(file), let .confirm(_, file): file
            }
        }
    }

    @Environment(\.api) private var api
    @State private var busy = false
    @State private var error: String?
    @State private var panel: Panel?

    private var actionable: Bool { entry.status == "pending" || entry.status == "failed" }
    private var unresolved: [String] { actionable ? entry.unresolvedFiles : [] }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text(entry.name)
                    .font(.footnote.monospaced())
                    .foregroundStyle(Theme.text.opacity(0.9))
                    .lineLimit(2)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if actionable && unresolved.isEmpty {
                    actionButton("认领", active: panel == .search(file: nil), id: "claim") { toggleSearch(nil) }
                }
                if actionable {
                    actionButton("忽略", id: "ignore") { act { _ = try await api.watchEntriesIgnore(entryId: entry.id) } }
                }
                if entry.status == "ignored" {
                    actionButton("恢复处理", id: "restore") { act { _ = try await api.watchEntriesRestore(entryId: entry.id) } }
                }
            }
            if let message = entry.message, !message.isEmpty {
                Text(message)
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
                    .lineLimit(2)
            }
            ForEach(unresolved, id: \.self) { file in
                HStack(spacing: 8) {
                    Text(file)
                        .font(.caption.monospaced())
                        .foregroundStyle(Theme.text.opacity(0.7))
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    actionButton("认领", active: panel?.file == file, id: "claim-file") { toggleSearch(file) }
                }
            }
            if let error {
                Text(error).font(.caption).foregroundStyle(Theme.danger)
            }
            panelView
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private var panelView: some View {
        switch panel {
        case let .search(file):
            IssueClaimSearchPanel(movie: movie, initialQuery: IssueClaimSeed.searchText(fromLabel: searchLabel(file))) { seed in
                panel = .confirm(seed: seed, file: file)
            }
            .id(file ?? "")
        case let .confirm(seed, file):
            IssueClaimConfirmPanel(seed: seed, movie: movie, fileCount: 1, busy: busy, onConfirm: {
                act { _ = try await api.watchEntriesClaim(entryId: entry.id, body: .init(tmdbId: seed.tmdbId, entryFile: file)) }
            }, onCancel: {
                panel = nil
            })
            .id(seed.tmdbId)
        case nil:
            EmptyView()
        }
    }

    /// 搜索预填：合集视频取文件名去扩展名，否则取条目名（再由共享面板剥掉标记块与年份）
    private func searchLabel(_ file: String?) -> String {
        guard let file else { return entry.name }
        let base = file.split(separator: "/").last.map(String.init) ?? file
        return base.replacingOccurrences(of: #"\.[^.]+$"#, with: "", options: .regularExpression)
    }

    private func toggleSearch(_ file: String?) {
        if panel == .search(file: file) {
            panel = nil
        } else {
            panel = .search(file: file)
        }
    }

    private func actionButton(_ title: String, active: Bool = false, id: String, action: @escaping () -> Void) -> some View {
        Button(title, action: action)
            .font(.footnote.weight(.medium))
            .buttonStyle(.glass)
            .tint(active ? Theme.text : nil)
            .disabled(busy)
            .accessibilityIdentifier("import-watch-entry-\(id)-\(entry.id)")
    }

    /// 执行条目动作：成功后收起面板并通知父级刷新清单与计数；失败原因显示在行内（同 Web）
    private func act(_ work: @escaping () async throws -> Void) {
        busy = true
        error = nil
        Task {
            do {
                try await work()
                panel = nil
                onChanged()
            } catch is CancellationError {
            } catch {
                self.error = error.localizedDescription
            }
            busy = false
        }
    }
}
