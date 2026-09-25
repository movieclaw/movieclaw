import SwiftUI

/// 「更换图片」弹层（Web `components/artwork-picker-dialog.tsx`，设计见 docs/design/metadata.md 6.3）。
///
/// 自动选图挑不中口味时的人工通道：背景 / 海报两个页签，网格铺候选缩略图，
/// 点选即落盘 + 覆盖媒体目录 + 加锁（此后刷新元数据不再覆盖）。
/// 候选顺序与自动选图规则一致；「当前」按后端给的**实际在用路径**比对，
/// 不能用「列表第一张」推断（策略升级前刮的条目、锁定的条目都会对不上）。
/// 已锁定时给「恢复自动选图」（`file_path: null` = 解锁）。
struct ArtworkPickerSheet: View {
    let libraryId: Int
    let mediaItemId: Int
    /// 选定 / 恢复后回调：调用方重拉详情呈现新图
    var onChanged: () -> Void = {}

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(\.dismiss) private var dismiss

    @State private var tab: ArtworkTab = .backdrop
    @State private var data: API.ArtworkCandidatesView?
    @State private var failed = false
    /// 正在应用的候选 file_path（"" 表示正在恢复自动）；nil = 空闲
    @State private var applying: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    Text("选中即生效，并同步写入媒体目录；此后刷新元数据不会覆盖你选的图")
                        .font(.subheadline)
                        .foregroundStyle(Theme.textMuted)

                    Picker("图片类型", selection: $tab) {
                        Text("背景").tag(ArtworkTab.backdrop)
                        Text("海报").tag(ArtworkTab.poster)
                    }
                    .pickerStyle(.segmented)

                    if locked {
                        lockedBanner
                    }

                    content
                }
                .padding(Theme.pagePadding)
            }
            .navigationTitle("更换图片")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") { dismiss() }.disabled(applying != nil)
                }
            }
        }
        .presentationDetents([.large])
        .interactiveDismissDisabled(applying != nil)
        .task { await load() }
    }

    private var candidates: [API.ArtworkCandidateView] {
        (tab == .poster ? data?.posters : data?.backdrops) ?? []
    }

    private var locked: Bool {
        (tab == .poster ? data?.posterLocked : data?.backdropLocked) ?? false
    }

    private var current: String? {
        tab == .poster ? data?.currentPoster : data?.currentBackdrop
    }

    private var lockedBanner: some View {
        HStack(spacing: 12) {
            Text("当前\(tab == .poster ? "海报" : "背景")由你手动选定，刷新元数据不会覆盖")
                .font(.subheadline)
                .foregroundStyle(Theme.info)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button("恢复自动选图") { Task { await apply(nil) } }
                .font(.subheadline.weight(.medium))
                .disabled(applying != nil)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(Theme.info.opacity(0.08), in: .rect(cornerRadius: 12))
    }

    @ViewBuilder
    private var content: some View {
        if failed {
            VStack(spacing: 10) {
                Text("候选图加载失败（TMDB 可能不可达）")
                    .foregroundStyle(Theme.danger)
                Button("重试") { Task { await load() } }
                    .buttonStyle(.glass)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 40)
        } else if data == nil {
            HStack(spacing: 8) {
                ProgressView()
                Text("正在拉取候选图…").foregroundStyle(Theme.textMuted)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 56)
        } else if candidates.isEmpty {
            Text("TMDB 上没有这个条目的\(tab == .poster ? "海报" : "背景图")")
                .foregroundStyle(Theme.textMuted)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 56)
        } else {
            LazyVGrid(
                columns: [GridItem(.adaptive(minimum: tab == .poster ? 116 : 220), spacing: 12)],
                spacing: 12
            ) {
                ForEach(candidates, id: \.filePath) { candidate in
                    cell(candidate)
                }
            }
        }
    }

    private func cell(_ c: API.ArtworkCandidateView) -> some View {
        let isCurrent = c.filePath == current
        let size = [c.width, c.height].allSatisfy { $0 != nil } ? "\(c.width!)×\(c.height!)" : ""
        let caption = size + (c.language.map { " · \($0)" } ?? "")
        return Button {
            Task { await apply(c.filePath) }
        } label: {
            Color.clear
                .aspectRatio(tab == .poster ? 2.0 / 3.0 : 16.0 / 9.0, contentMode: .fit)
                .overlay { RemoteImage(url: api.image(c.previewUrl)) }
                .overlay(alignment: .topLeading) {
                    // 标出正在用的那张，消除「我现在用的是哪张」的疑问
                    if isCurrent {
                        badge("当前", foreground: .black.opacity(0.85), background: Theme.accent)
                    }
                }
                .overlay(alignment: .topTrailing) {
                    if tab == .backdrop, c.language == nil {
                        badge("无文字", foreground: .white.opacity(0.85), background: .black.opacity(0.7))
                    }
                }
                .overlay(alignment: .bottom) {
                    Text(caption)
                        .font(.caption2)
                        .monospacedDigit()
                        .foregroundStyle(.white.opacity(0.75))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 6)
                        .padding(.top, 14)
                        .padding(.bottom, 4)
                        .background(LinearGradient(colors: [.black.opacity(0.8), .clear], startPoint: .bottom, endPoint: .top))
                }
                .overlay {
                    if applying == c.filePath {
                        ZStack {
                            Color.black.opacity(0.55)
                            ProgressView()
                        }
                    }
                }
                .clipShape(.rect(cornerRadius: 8))
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .strokeBorder(isCurrent ? Theme.accent : Color.white.opacity(0.1), lineWidth: isCurrent ? 2 : 1)
                )
        }
        .buttonStyle(.plain)
        .disabled(applying != nil)
        .opacity(applying != nil && applying != c.filePath ? 0.6 : 1)
    }

    private func badge(_ text: String, foreground: Color, background: Color) -> some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(foreground)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(background, in: .rect(cornerRadius: 4))
            .padding(6)
    }

    /// reset=false 用于选图后的静默刷新：保留当前网格，避免整块闪回「正在拉取」
    private func load(reset: Bool = true) async {
        failed = false
        if reset { data = nil }
        do {
            data = try await api.libraryArtworkListCandidates(libraryId: libraryId, mediaItemId: mediaItemId)
        } catch is CancellationError {
        } catch {
            failed = true
        }
    }

    /// 选定一张（filePath）或恢复自动（nil）
    private func apply(_ filePath: String?) async {
        applying = filePath ?? ""
        defer { applying = nil }
        do {
            _ = try await api.libraryArtworkSelect(
                libraryId: libraryId,
                mediaItemId: mediaItemId,
                body: API.ArtworkSelectPayload(kind: tab.rawValue, filePath: filePath)
            )
            onChanged()
            await load(reset: false)
        } catch {
            failed = true
            feedback.error(error)
        }
    }
}

private enum ArtworkTab: String, Hashable {
    case poster
    case backdrop
}
