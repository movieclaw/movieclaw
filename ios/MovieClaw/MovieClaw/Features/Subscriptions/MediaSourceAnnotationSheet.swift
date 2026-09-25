import SwiftUI

/// 整季片源标注（对应 Web `components/media-source-annotation-dialog.tsx`，
/// docs/design/media-source-annotation.md §5.2）。
///
/// 挂在「失败反馈的原地」：洗版报告的「无法确认」季与追踪明细的「无法确认档位」季（管理员）。
/// 三段式：待标注文件名列表（扫一眼确认来源）→ 档位单选（每项写明后果，标注必须是知情选择）→ 确认。
/// 只动片源未知与既有人工标注的文件，已从文件名解析出片源的不被批量覆盖。
struct MediaSourceAnnotationSheet: View {
    let mediaItemId: Int
    /// 电影传 0（后端的电影单元哨兵）
    let seasonNumber: Int
    let isMovie: Bool
    /// 标注成功后回调（父页面负责重跑体检 / 刷新与提示），参数为结果句
    let onApplied: (String) async -> Void

    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss
    @State private var candidates: [API.MediaSourceAnnotationCandidateView]?
    @State private var picked: String?
    @State private var busy = false
    @State private var error: String?

    /// 与后端片源档阶梯同源；user-lowest 是「不确定，按最低档」的显式哨兵
    static let options: [(value: String, label: String, consequence: String, destructive: Bool)] = [
        ("Remux", "Remux", "原盘无损重封装，可标注的最高档；达到任何洗版目标，停止洗版", false),
        ("Blu-ray", "蓝光重编码", "蓝光碟压制；高于 WEB-DL 目标，仅低于 Remux 目标", false),
        ("WEB-DL", "WEB-DL", "流媒体原流；洗版目标为 WEB-DL 时即达标停洗", false),
        ("WEBRip", "WEBRip", "流媒体二压；低于 WEB-DL 及以上目标，会自动洗版替换", false),
        ("HDTV", "TV 录制", "电视录制；低于洗版目标，会自动洗版替换", false),
        ("user-lowest", "不确定（按最低档）", "按最差处理：整季会重新下载，替换为可证明达标的版本", true),
    ]

    private var scopeLabel: String { isMovie ? "正片" : SubsFormat.seasonName(seasonNumber) }

    private static func sourceDisplay(_ value: String?) -> String? {
        guard let value, !value.isEmpty else { return nil }
        if value == "user-lowest" { return "最低档（人工标注）" }
        if value == "Disc" { return "原盘" }
        return value
    }

    var body: some View {
        SubsSheetScaffold(
            title: "标注\(scopeLabel)的片源",
            subtitle: "这些文件的文件名里没有可识别的片源标记，系统无法确认它们是否低于洗版目标。看一眼文件名，把你知道的来源告诉系统——标注一次，之后整季自动参与洗版判定。"
        ) {
            if let error { SubsNotice(text: error, tone: .error) }
            if let candidates {
                if candidates.isEmpty {
                    SubsNotice(text: "\(scopeLabel)没有片源未知的文件——已从文件名解析出片源的文件不会被批量覆盖。", tone: .neutral)
                } else {
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(candidates, id: \.fileId) { file in
                            HStack(spacing: 8) {
                                Text(file.fileName).font(.subheadline).foregroundStyle(Theme.text.opacity(0.8)).lineLimit(1).truncationMode(.middle)
                                Spacer(minLength: 4)
                                if file.mediaSourceManual, let current = Self.sourceDisplay(file.mediaSource) {
                                    DiscoverTag(text: "现为 \(current)")
                                }
                                Text(SubsFormat.bytes(file.sizeBytes)).font(.caption).monospacedDigit().foregroundStyle(Theme.textFaint)
                            }
                            .padding(.horizontal, 14).padding(.vertical, 8)
                        }
                        Divider().overlay(Color.white.opacity(0.06))
                        Text("共 \(candidates.count) 个文件将被统一标注").font(.caption).foregroundStyle(Theme.textFaint).padding(.horizontal, 14).padding(.vertical, 8)
                    }
                    .background(Color.white.opacity(0.02), in: .rect(cornerRadius: 14))
                    .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.white.opacity(0.07)))
                }
            } else {
                Text("正在加载文件列表…").font(.subheadline).foregroundStyle(Theme.textMuted)
            }

            VStack(spacing: 8) {
                ForEach(Self.options, id: \.value) { option in
                    SubsChoiceRow(
                        title: option.label,
                        subtitle: option.consequence,
                        selected: picked == option.value,
                        tint: option.destructive ? SubsColor.warn : SubsColor.upgrade
                    ) {
                        EmptyView()
                    } action: {
                        picked = option.value
                    }
                    .disabled(busy || (candidates?.isEmpty ?? true))
                }
            }
        } footer: {
            SubsPrimaryButton(
                title: "确认标注",
                busy: busy,
                enabled: picked != nil && !(candidates?.isEmpty ?? true),
                identifier: "annotation-confirm"
            ) { Task { await apply() } }
        }
        .accessibilityIdentifier("annotation-sheet")
        .task {
            do {
                candidates = try await api.libraryItemsListMediaSourceAnnotationCandidates(mediaItemId: mediaItemId, seasonNumber: seasonNumber)
            } catch is CancellationError {
            } catch {
                self.error = "未能加载待标注文件列表，请稍后重试"
            }
        }
    }

    private func apply() async {
        guard let picked, let option = Self.options.first(where: { $0.value == picked }) else { return }
        busy = true
        error = nil
        do {
            let result = try await api.libraryItemsAnnotateMediaSource(
                body: .init(mediaItemId: mediaItemId, seasonNumber: seasonNumber, mediaSource: option.value)
            )
            let files = result["files"]?.intValue ?? (candidates?.count ?? 0)
            await onApplied("已将 \(scopeLabel)的 \(files) 个文件片源标注为「\(option.label)」")
            dismiss()
        } catch {
            self.error = error.localizedDescription.isEmpty ? "标注失败，请稍后重试" : error.localizedDescription
            busy = false
        }
    }
}
