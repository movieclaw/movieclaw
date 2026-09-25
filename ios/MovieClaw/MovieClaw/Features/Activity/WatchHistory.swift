import SwiftUI

/// 「最近播放」切片（Web `PlaybackHistoryList`）：每场播放一行、按本地日期分组。
///
/// 翻页用服务端游标（`before = next_cursor`）而不是 offset：记录一直往前追加，续载期间新开的一场
/// 会把 offset 整体后推，同一行就会在两页各出现一次。滚到底自动续载（列表尾部的哨兵出现即拉下一页），
/// 「加载更多」按钮兜底；翻到头时提示「已经到最早的记录了」。不轮询：日志按场记，切成员或口径时重拉。
struct PlaybackHistoryList: View {
    let scope: String
    let memberId: Int?
    let memberLabel: String?
    let onClearMember: () -> Void
    let onShowAll: () -> Void

    @Environment(\.api) private var api
    @State private var entries: [API.PlaybackLogEntryView] = []
    @State private var hiddenCount = 0
    @State private var cursor: Int?
    @State private var hasMore = false
    @State private var loading = true
    @State private var error: String?
    /// 切筛选后，仍在途的旧请求不能把旧口径的行拼进新列表
    @State private var generation = 0

    static let pageSize = 30

    private struct Filter: Hashable {
        var scope: String
        var memberId: Int?
    }

    var body: some View {
        content
            .task(id: Filter(scope: scope, memberId: memberId)) {
                generation += 1
                entries = []
                hiddenCount = 0
                cursor = nil
                hasMore = false
                await load(before: nil)
            }
    }

    @ViewBuilder private var content: some View {
        if let error {
            ActivityWarningBanner(message: error)
        } else if loading, entries.isEmpty {
            HStack(spacing: 10) {
                ProgressView()
                Text("正在读取播放记录…").font(.subheadline).foregroundStyle(Theme.textMuted)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 50)
        } else if entries.isEmpty, hiddenCount == 0 {
            if memberId != nil {
                ActivityEmptyCard(
                    systemImage: "clock.arrow.circlepath", title: "\(memberLabel ?? "这位成员")还没有播放记录",
                    message: "从这位成员下一次播放起，谁、什么时候、用什么设备、看了多久都会记在这里。"
                ) { ActivityPillButton(title: "查看全部成员", action: onClearMember) }
            } else {
                ActivityEmptyCard(
                    systemImage: "clock.arrow.circlepath", title: "还没有播放记录",
                    message: "从现在起每一场播放都会记在这里：谁、什么时候、用什么设备、看到哪、看了多久。网页播放器和 Jellyfin 客户端都算。"
                )
            }
        } else if entries.isEmpty {
            ActivityEmptyCard(
                systemImage: "lock", title: "这些播放都在你的浏览范围外",
                message: "另有 \(hiddenCount) 场播放来自你设为不可见的库；切到「全部」即可看到片名。"
            ) { ActivityPillButton(title: "显示全部", action: onShowAll) }
        } else {
            list
        }
    }

    private var groups: [(key: String, items: [API.PlaybackLogEntryView])] {
        var result: [(key: String, items: [API.PlaybackLogEntryView])] = []
        for entry in entries {
            let key = ActivityFormat.dayKey(entry.startedAt)
            if result.last?.key == key {
                result[result.count - 1].items.append(entry)
            } else {
                result.append((key, [entry]))
            }
        }
        return result
    }

    private var list: some View {
        LazyVStack(alignment: .leading, spacing: 18) {
            ForEach(groups, id: \.key) { group in
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 6) {
                        Text(ActivityFormat.dayLabel(group.items[0].startedAt)).fontWeight(.semibold)
                        Text("\(group.items.count) 场").monospacedDigit().foregroundStyle(Theme.textFaint.opacity(0.7))
                    }
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
                    VStack(spacing: 0) {
                        ForEach(Array(group.items.enumerated()), id: \.element.id) { index, entry in
                            if index > 0 { Divider().overlay(Color.white.opacity(0.06)) }
                            PlaybackHistoryRow(entry: entry)
                        }
                    }
                    .background(Color.white.opacity(0.02), in: .rect(cornerRadius: 16))
                    .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.white.opacity(0.08)))
                }
            }
            if hiddenCount > 0 {
                HiddenCountRow(count: hiddenCount, noun: "场播放", onShowAll: onShowAll).activityHiddenRowCard()
            }
            if hasMore {
                Button {
                    Task { await loadMore() }
                } label: {
                    Text(loading ? "正在加载更早的记录…" : "加载更多")
                        .font(.subheadline)
                        .foregroundStyle(Theme.textMuted)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 11)
                }
                .buttonStyle(.glass)
                .disabled(loading)
                .onScrollVisibilityChange(threshold: 0.1) { visible in
                    // 按真实可见性触发（非懒加载容器里 onAppear 会在布局时就触发，导致一口气翻完所有页）
                    if visible { Task { await loadMore() } }
                }
                .accessibilityIdentifier("history-load-more")
            } else if entries.count >= Self.pageSize {
                Text("已经到最早的记录了")
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 8)
                    .accessibilityIdentifier("history-end")
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("history-list")
    }

    private func loadMore() async {
        guard hasMore, let cursor, !loading else { return }
        await load(before: cursor)
    }

    private func load(before: Int?) async {
        let current = generation
        loading = true
        defer { if current == generation { loading = false } }
        do {
            let page = try await api.playbackHistory(limit: Self.pageSize, before: before, memberId: memberId, scope: scope)
            guard current == generation else { return }
            entries = before == nil ? page.entries : entries + page.entries.filter { new in !entries.contains { $0.id == new.id } }
            hiddenCount = before == nil ? page.hiddenCount : hiddenCount + page.hiddenCount
            cursor = page.nextCursor
            hasMore = page.hasMore
            error = nil
        } catch is CancellationError {
        } catch {
            if current == generation { self.error = error.localizedDescription.isEmpty ? "播放记录加载失败" : error.localizedDescription }
        }
    }
}

/// 一场播放：时刻 · 小海报 · 片名 / 成员 · 设备 · 右侧结果（播放中 / 看完 / 看到 N% / 播放过）与观看时长
struct PlaybackHistoryRow: View {
    let entry: API.PlaybackLogEntryView
    @Environment(\.api) private var api

    var body: some View {
        HStack(spacing: 11) {
            Text(ActivityFormat.clock(entry.startedAt))
                .font(.caption).monospacedDigit().foregroundStyle(Theme.textFaint)
                .frame(width: 40, alignment: .leading)
            ActivityPoster(media: entry.media, width: 28, height: 42, radius: 6)
            VStack(alignment: .leading, spacing: 2) {
                ActivityTitleText(media: entry.media, showYear: false)
                Text(WatchFormat.metaLine([entry.memberName, WatchFormat.metaLine([entry.client, entry.deviceName])]))
                    .font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
            }
            VStack(alignment: .trailing, spacing: 2) {
                if entry.endedAt == nil {
                    Text("播放中").foregroundStyle(Theme.success)
                } else if entry.completed {
                    Label("看完", systemImage: "checkmark").labelStyle(.titleAndIcon).foregroundStyle(Theme.success)
                } else {
                    Text(entry.progressPercent.map { "看到 \($0)%" } ?? "播放过").foregroundStyle(Theme.textMuted)
                }
                if entry.watchedMs > 0 {
                    Text(ActivityFormat.watched(entry.watchedMs)).foregroundStyle(Theme.textFaint)
                }
            }
            .font(.caption)
            .monospacedDigit()
            .fixedSize()
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }
}
