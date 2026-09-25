import SwiftUI

/// 整库元数据刷新的进度面板（对应网页 library-detail-view.tsx 的 MetadataRefreshPanel）。
///
/// 全量重刷（每部片都重拉档案 + 重下图片 + 覆盖媒体目录）在大库上要跑很久，只给一个百分比
/// 等于让用户干等：这里把**到哪几部了、每部在做什么**都摊开，外加失败计数和停止入口。
/// 后端并发 3 路，所以「正在处理」是个列表。
///
/// 与网页的分工差异：网页由页面持有状态、面板只负责画；这里面板**自带轮询**
/// （`GET /libraries/{id}/metadata/refresh/progress`，每 2 秒——"阶段文字跟得上"与
/// "别把接口打太密"的折中），宿主只需在刷新进行中（库详情的 `metadata_refresh.refreshing`）
/// 且当前用户能管理媒体库时挂上它。刷新结束（响应 refreshing=false）时面板自行隐藏并回调
/// `onFinished` 一次，宿主据此重拉墙（海报/档案已更新）。
///
/// 瞬时失败保留旧状态、下一轮继续：一旦清空，后台还在跑的刷新就会从界面上失踪（网页曾是线上实况）。
struct MetadataRefreshPanel: View {
    let libraryId: Int
    var onFinished: () -> Void = {}

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @State private var state: API.MetadataRefreshView?
    @State private var finishedReported = false

    var body: some View {
        // 常驻一个零高占位：没在刷新时面板收起，但轮询任务得有个宿主视图挂着
        ZStack {
            Color.clear.frame(height: 0)
            if let state, state.refreshing {
                panel(state)
            }
        }
        .frame(maxWidth: .infinity)
        .polling(every: 2, immediately: true) { await reload() }
    }

    private func reload() async {
        do {
            let next = try await api.libraryMetadataGetRefreshStatus(libraryId: libraryId)
            // 阶段没推进时不替换，别让 2 秒一次的轮询白白重绘
            if next != state { state = next }
            if !next.refreshing, !finishedReported {
                finishedReported = true
                onFinished()
            } else if next.refreshing {
                finishedReported = false
            }
        } catch {
            // 保留旧状态，下一轮再试（见类型注释）
        }
    }

    private func stop() async {
        do {
            _ = try await api.libraryMetadataStopRefresh(libraryId: libraryId)
            await reload()
        } catch {
            feedback.error(error)
        }
    }

    private func panel(_ state: API.MetadataRefreshView) -> some View {
        let fraction = state.total > 0 ? min(1, Double(state.processed) / Double(state.total)) : 0
        var headline = state.stopping ? "正在停止刷新" : "正在刷新元数据"
        if state.total > 0 { headline += " \(state.processed)/\(state.total)" }
        if state.failed > 0 { headline += " · 失败 \(state.failed)" }
        return VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                ProgressView()
                    .controlSize(.mini)
                    .tint(Theme.info)
                Text(headline)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Theme.info)
                    .lineLimit(1)
                Spacer(minLength: 8)
                Button(state.stopping ? "收尾中…" : "停止") {
                    Task { await stop() }
                }
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.white.opacity(0.7))
                .buttonStyle(.plain)
                .disabled(state.stopping)
            }
            // 进度条：全量刷新常以分钟计，一条能看出"在动"的进度很重要
            ProgressView(value: fraction)
                .tint(Theme.info)
                .animation(.easeOut(duration: 0.5), value: fraction)
            if !state.active.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(state.active, id: \.mediaItemId) { active in
                        HStack(spacing: 8) {
                            Text(active.title)
                                .foregroundStyle(.white.opacity(0.7))
                                .lineLimit(1)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            Text(active.phase)
                                .foregroundStyle(.white.opacity(0.45))
                        }
                        .font(.subheadline)
                    }
                }
            }
            Text("全量重刷：重拉 TMDB 档案、按当前尺寸重下图片、覆盖媒体目录镜像；你手动选定的图不受影响")
                .font(.caption)
                .foregroundStyle(.white.opacity(0.4))
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(Theme.info.opacity(0.07), in: .rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.info.opacity(0.25)))
    }
}
