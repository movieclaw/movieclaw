import SwiftUI

/// 待处理事项（状态化告警中心，Web `components/notice-center.tsx`；「更多」页进入，管理员）。
///
/// 产品语义：这里只列「需要用户行动才能推进的运行时故障」——正常运行时为空；问题修好后服务端自动消退，
/// 用户不需要「清理通知」。每条事项三个动作，主次分明：
/// - 去处理（主出口）：跳到能修它的页面（订阅详情 / 自动入库 / 下载器 / 站点设置）；
/// - 交给 AI 分析（第二出口，接入了模型才有）：后端带着现场自检组装工单起会话；
/// - 忽略（退路）：乐观移除，失败时下轮刷新会拉回真实状态。
///
/// 目录级根因告警（payload.group_key）存在时，被它收编的单种子告警（payload.grouped_under）折叠不显示：
/// 用户看到的是一条「这个目录 movieclaw 看不到」，而不是 16 条「《某剧》无法入库」。
/// 刷新：30 秒轮询 + 回前台立即刷新一次（`.polling` 自带）。
struct NoticeCenterView: View {
    @Environment(\.api) private var api
    @Environment(Router.self) private var router
    @State private var state: Loadable<[API.NoticeView]> = .loading

    var body: some View {
        AsyncContent(state, retry: load) { notices in
            let visible = Self.visible(notices)
            if visible.isEmpty {
                EmptyState(
                    systemImage: "checkmark.seal", title: "暂无待处理事项",
                    message: "这里只列需要你处理的运行时问题，修复后会自动消失。"
                )
                .accessibilityIdentifier("notices-empty")
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 10) {
                        ForEach(visible, id: \.id) { notice in
                            NoticeCard(notice: notice, onGoto: { goto(notice) }, onDismiss: { dismiss(notice) })
                        }
                        Text("这里只列需要你处理的运行时问题，修复后会自动消失。")
                            .font(.caption)
                            .foregroundStyle(Theme.textFaint)
                            .padding(.top, 6)
                    }
                    .padding(.horizontal, Theme.pagePadding)
                    .padding(.vertical, 12)
                }
            }
        }
        .navigationTitle("待处理事项")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if let count = state.value.map({ Self.visible($0).count }), count > 0 {
                ToolbarItem(placement: .topBarTrailing) {
                    Text("\(count)")
                        .font(.caption.weight(.semibold))
                        .monospacedDigit()
                        .foregroundStyle(Theme.textMuted)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 2)
                        .background(Color.white.opacity(0.07), in: .capsule)
                        .accessibilityIdentifier("notices-count")
                }
            }
        }
        .appBackground()
        .task { await load() }
        .polling(every: 30) { await load() }
    }

    static func visible(_ notices: [API.NoticeView]) -> [API.NoticeView] {
        let groupKeys = Set(notices.compactMap { $0.payload["group_key"]?.stringValue }.filter { !$0.isEmpty })
        return notices.filter { notice in
            guard let parent = notice.payload["grouped_under"]?.stringValue, !parent.isEmpty else { return true }
            return !groupKeys.contains(parent)
        }
    }

    /// 事项的跳转目标：按来源域给出能修它的页面（Web `noticeHref`）
    static func href(_ notice: API.NoticeView) -> String {
        switch notice.source {
        case "subscription":
            if let id = notice.payload["subscription_id"]?.intValue { return "/subscriptions/\(id)" }
            return "/subscriptions"
        case "ingest": return "/settings/import-watch"
        case "downloader": return "/settings/downloaders"
        case "site": return "/settings/sites"
        default: return "/settings"
        }
    }

    private func load() async {
        // 拉取失败（离线 / 后端重启）且已有数据时不打扰：保留上次结果，下轮自愈
        await Loadable.load(into: $state) { try await api.noticesList() }
    }

    private func goto(_ notice: API.NoticeView) {
        router.open(webPath: Self.href(notice))
    }

    private func dismiss(_ notice: API.NoticeView) {
        // 乐观移除：dismiss 幂等且失败无害
        if case let .loaded(list) = state { state = .loaded(list.filter { $0.id != notice.id }) }
        Task {
            do {
                try await api.noticesDismiss(noticeId: notice.id)
            } catch {
                await load()
            }
        }
    }
}

/// 一条待处理事项：严重度圆点自成左栏，标题与正文对齐；时间贴右；操作条与正文以发丝线分开
private struct NoticeCard: View {
    let notice: API.NoticeView
    let onGoto: () -> Void
    let onDismiss: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 10) {
                Circle()
                    .fill(notice.severity == "warning" ? Theme.warning : Theme.danger)
                    .frame(width: 8, height: 8)
                    .padding(.top, 6)
                VStack(alignment: .leading, spacing: 4) {
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text(notice.title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        Text(ActivityFormat.relative(notice.updatedAt)).font(.caption).foregroundStyle(Theme.textFaint)
                    }
                    Text(notice.message).font(.footnote).foregroundStyle(Theme.textMuted).lineSpacing(2)
                        .textSelection(.enabled)
                }
            }
            Rectangle().fill(Color.white.opacity(0.05)).frame(height: 1)
            HStack(spacing: 8) {
                Spacer(minLength: 0)
                Button(action: onDismiss) {
                    Text("忽略")
                        .font(.subheadline)
                        .foregroundStyle(Theme.textFaint)
                        .expandedHitArea(vertical: 12)
                }
                .buttonStyle(.plain)
                .padding(.horizontal, 6)
                .accessibilityIdentifier("notice-dismiss-\(notice.id)")
                ActivityHandoffButton(kind: "notice", refId: String(notice.id))
                Button(action: onGoto) {
                    HStack(spacing: 3) {
                        Text("去处理")
                        Image(systemName: "chevron.right").font(.caption.weight(.semibold))
                    }
                    .font(.subheadline.weight(.medium))
                }
                .buttonStyle(.glass)
                .accessibilityIdentifier("notice-goto-\(notice.id)")
            }
        }
        .padding(14)
        .background(Color.white.opacity(0.03), in: .rect(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.white.opacity(0.07)))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("notice-\(notice.id)")
    }
}
