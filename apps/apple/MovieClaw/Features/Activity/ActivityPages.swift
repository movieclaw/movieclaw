import SwiftUI

/// 活动总览的二级页：从分组标题「查看全部」、行尾箭头或站内链接 `/activity?view=` 压栈进来。
enum ActivityPage: String, Hashable {
    /// 进行中的全部任务（下载过程 + 后台作业的时间线，可取消 / 删种 / 换种）
    case active
    /// 已结束的后台作业（按天分组，可撤销忽略 / 重新执行）
    case finished
    /// 刷流做种逐种子明细
    case boost
    /// 最近播放（按场、游标分页，可按成员筛选）
    case plays
    /// 观看统计（可按周期、成员筛选）
    case stats

    /// Web `/activity?view=` → 二级页；需要处理 / 正在播放 / 全部 / 缺省都落在总览本身，返回 nil
    init?(webView: String?) {
        switch webView {
        case "active": self = .active
        case "history": self = .finished
        case "plays": self = .plays
        case "stats": self = .stats
        default: return nil
        }
    }

    var title: String {
        switch self {
        case .active: "进行中"
        case .finished: "已结束"
        case .boost: "刷流做种"
        case .plays: "最近播放"
        case .stats: "观看统计"
        }
    }
}

struct ActivityPageView: View {
    let page: ActivityPage

    var body: some View {
        switch page {
        case .active: ActivityTasksPage(mode: .active)
        case .finished: ActivityTasksPage(mode: .history)
        case .boost: ActivityBoostPage()
        case .plays, .stats: ActivityWatchPage(page: page)
        }
    }
}

// MARK: - 任务

/// 进行中 / 已结束：沿用任务中心的时间线与历史分组（完整过程与全部操作都在这里）
private struct ActivityTasksPage: View {
    let mode: TaskCenterPanel.Mode

    @Environment(ShellBadges.self) private var badges
    @State private var actions = TaskCenterActions()

    var body: some View {
        let store = badges.tasks
        ScrollView {
            TaskCenterPanel(store: store, mode: mode, actions: actions)
                .padding(.horizontal, Theme.pagePadding)
                .padding(.top, 8)
                .padding(.bottom, 48)
        }
        .refreshable {
            store.refreshJobs()
            store.refreshDownloads()
        }
        .navigationTitle(mode == .history ? "已结束" : "进行中")
        .navigationBarTitleDisplayMode(.inline)
        .appBackground()
        .taskDeleteSheet(actions, store: store)
    }
}

// MARK: - 刷流

/// 刷流做种：页头实时汇总，下面逐种子一行（按上行速度倒序，正在出力的在前）
private struct ActivityBoostPage: View {
    @Environment(ShellBadges.self) private var badges

    var body: some View {
        let tasks = badges.tasks.activity.boostTasks
        let totals = ActivityBoostTotals(tasks)
        List {
            Section {
                Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 6) {
                    GridRow {
                        SpeedStat(direction: .up, bytesPerSecond: totals.upSpeed, placeholder: "↑ 0 B/s")
                        SpeedStat(direction: .down, bytesPerSecond: totals.downSpeed, placeholder: "↓ 0 B/s")
                    }
                    GridRow {
                        Text.activityJoin([Text("已上传 "), Text(ActivityFormat.bytes(Double(totals.uploaded))).fontWeight(.semibold).foregroundStyle(Theme.success)])
                        Text.activityJoin([Text("已下载 "), Text(ActivityFormat.bytes(Double(totals.downloaded))).fontWeight(.semibold).foregroundStyle(Theme.info)])
                    }
                }
                .font(.subheadline)
                .monospacedDigit()
                .foregroundStyle(Theme.textMuted)
                .padding(.vertical, 4)
            } header: {
                Text("\(totals.count) 个种子").textCase(nil)
            } footer: {
                Text("刷流种子由引擎自动汰换，这里只看不删；种子名可点开站点页面。")
            }
            if !tasks.isEmpty {
                Section {
                    ForEach(ActivityBoostTotals.sorted(tasks)) { task in
                        BoostTaskRow(task: task)
                    }
                } header: {
                    Text("按上行速度排序").textCase(nil)
                }
            }
        }
        .listStyle(.insetGrouped)
        .scrollContentBackground(.hidden)
        .refreshable { badges.tasks.refreshDownloads() }
        .navigationTitle("刷流做种")
        .navigationBarTitleDisplayMode(.inline)
        .appBackground()
    }
}

// MARK: - 观看

/// 最近播放 / 观看统计：筛选（周期、成员、范围）收进右上角的筛选菜单，当前条件写在标题下方
private struct ActivityWatchPage: View {
    let page: ActivityPage

    @Environment(ShellBadges.self) private var badges
    @Environment(\.api) private var api
    @State private var memberId: Int?
    @State private var days = 30
    @State private var members: [API.MemberView] = []

    private static let periods = [7, 30, 90]
    /// 成员筛选里「超级管理员」的取值（同 Web：0 表示超管自己）
    private static let adminMember = 0

    var body: some View {
        let media = badges.media
        ScrollView {
            Group {
                if page == .plays {
                    PlaybackHistoryList(
                        scope: media.scope, memberId: memberId, memberLabel: memberLabel,
                        onClearMember: { memberId = nil }, onShowAll: { media.setScope("all") }
                    )
                } else {
                    WatchStatsPanel(
                        scope: media.scope, days: days, memberId: memberId, memberLabel: memberLabel,
                        onMemberSelect: { memberId = $0 }, onDaysChange: { days = $0 },
                        onShowAll: { media.setScope("all") }
                    )
                }
            }
            .padding(.horizontal, Theme.pagePadding)
            .padding(.top, 8)
            .padding(.bottom, 48)
        }
        .navigationTitle(page.title)
        .navigationSubtitle(filterSummary)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) { filterMenu }
        }
        .appBackground()
        .task { members = (try? await api.membersList()) ?? members }
    }

    private var memberLabel: String? {
        guard let memberId else { return nil }
        if memberId == Self.adminMember { return "超级管理员" }
        return members.first { $0.id == memberId }.map { $0.nickname.isEmpty ? $0.username : $0.nickname }
    }

    /// 标题下的当前条件：「最近 30 天 · 全部成员 · 我的浏览范围」
    private var filterSummary: String {
        var parts: [String] = []
        if page == .stats { parts.append("最近 \(days) 天") }
        parts.append(memberLabel ?? "全部成员")
        parts.append(badges.media.scope == "all" ? "含隐藏的库" : "我的浏览范围")
        return parts.joined(separator: " · ")
    }

    private var filterMenu: some View {
        let media = badges.media
        let filtered = memberId != nil || media.scope == "all" || (page == .stats && days != 30)
        return Menu {
            if page == .stats {
                Picker("周期", selection: $days) {
                    ForEach(Self.periods, id: \.self) { Text("最近 \($0) 天").tag($0) }
                }
                .pickerStyle(.inline)
            }
            Picker("成员", selection: Binding(get: { memberId ?? -1 }, set: { memberId = $0 < 0 ? nil : $0 })) {
                Text("全部成员").tag(-1)
                Text("超级管理员").tag(Self.adminMember)
                ForEach(members, id: \.id) { member in
                    Text(member.nickname.isEmpty ? member.username : member.nickname).tag(member.id)
                }
            }
            .pickerStyle(.menu)
            Picker("范围", selection: Binding(get: { media.scope }, set: { media.setScope($0) })) {
                Text("我的浏览范围").tag("visible")
                Text("全部（含对你隐藏的库）").tag("all")
            }
            .pickerStyle(.inline)
        } label: {
            Image(systemName: filtered ? "line.3.horizontal.decrease.circle.fill" : "line.3.horizontal.decrease")
        }
        .accessibilityLabel("筛选")
        .accessibilityIdentifier("activity-filter")
    }
}
