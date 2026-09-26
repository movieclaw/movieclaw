import SwiftUI

/// 整库元数据刷新的进度面板（对应网页 library-detail-view.tsx 的 MetadataRefreshPanel）。
///
/// 全量重刷（每部片都重拉档案 + 重下图片 + 覆盖媒体目录）在大库上要跑很久，只给一个百分比
/// 等于让用户干等：这里把**到哪几部了、每部在做什么**都摊开，外加失败计数和停止入口。
/// 后端并发 3 路，所以「正在处理」是个列表。
///
/// 分工同网页：状态由宿主（单库页）持有——进页先探一次 `GET /libraries/{id}/metadata/refresh/progress`，
/// 刷新中每 2 秒轮询，⋯ 菜单的「停止刷新 x/y」、墙上各格的刷新阶段、墙的轮询档位都读同一份；
/// 面板只负责画。「停止」失败由宿主放进页面的 notice 横幅（同 Web）。
struct MetadataRefreshPanel: View {
    let state: API.MetadataRefreshView
    let stop: () -> Void

    var body: some View {
        panel(state)
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
                Button(action: stop) {
                    Text(state.stopping ? "收尾中…" : "停止")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(.white.opacity(0.7))
                        .expandedHitArea(vertical: 12)
                }
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
