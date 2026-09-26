import SwiftUI

/// 活动页（管理员）：一页总览。
///
/// 2026-09-26 用户拍板取代 Web 式的「观看 / 任务」分段 + 二级切片胶囊（Web 的 `/activity?view=` 不变，App 与之结构不同）：
/// - **不分段**：打开就是「现在有没有要我管的事、家里在发生什么」，按紧急程度自上而下——
///   需要处理 → 正在播放 → 正在下载 → 进行中（含刷流）→ 最近播放 → 观看统计 → 最近完成；没内容的分组不出现；
/// - **大标题下一行实时摘要**（「2 项需要处理 · 1 台设备在播放 · 5 个任务进行中」，需要处理标红），
///   不用 navigationSubtitle：它会把大标题压成小号，与其它标签页不一致；
/// - **历史与统计只露一小段**（3 条 / 一张 7 天摘要卡），分组标题右侧「查看全部」压栈到二级页
///   （`ActivityPageView`），成员 / 周期 / 范围筛选在二级页右上角；
/// - **系统分组列表**：行自带按压高亮、左滑操作、长按菜单、下拉刷新；「需要处理」例外，保留完整卡片——
///   每种故障的补救动作不同（换种、重试、交给 AI、删除、忽略），压成一行反而要多点一层；
/// - **浏览范围**（我的浏览范围 / 全部）不占顶栏：只在确有被隐藏的内容时以分组脚注出现、就地切换。
///
/// 实时数据来自外壳常驻的 `ShellBadges.tasks / media`（SSE + 轮询，来回切标签不打断）；
/// 最近播放与 7 天统计是页面自己取的快照，进页、切范围、有人开始 / 结束播放时重取。
struct ActivityView: View {
    @Environment(ShellBadges.self) private var badges
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(Router.self) private var router

    @State private var taskActions = TaskCenterActions()
    @State private var deviceActions = ActivityDeviceActions()
    @State private var recentPlays: [API.PlaybackLogEntryView] = []
    @State private var weekly: API.PlaybackWatchStatsView?
    /// 已配置站点（刷流行要按站点开关状态写文案）；只在有刷流种子时取
    @State private var sites: [API.ConfiguredSite]?

    /// 总览上「进行中」最多露几条，其余进二级页
    private static let activeLimit = 5
    /// 最近播放 / 最近完成各露几条
    private static let recentLimit = 3

    /// 进行中的一条：下载组或后台作业（两者在二级页的时间线里也是这个顺序）
    private enum ActiveItem: Identifiable {
        case group(TaskCenter.DownloadGroup)
        case job(API.JobView)

        var id: String {
            switch self {
            case let .group(group): "group:\(group.id)"
            case let .job(job): "job:\(job.id)"
            }
        }
    }

    /// 重取「最近播放 / 7 天统计」的时机：范围切换，或正在播放的设备数变了（有人开播 / 停播就会多一条记录）
    private struct ExtrasKey: Hashable {
        var scope: String
        var live: Int
    }

    var body: some View {
        let tasks = badges.tasks
        let media = badges.media
        let activity = tasks.activity
        let snapshot = media.snapshot
        List {
            Section {} header: {
                summary(activity: activity, snapshot: snapshot)
                    .font(.subheadline)
                    .foregroundStyle(Theme.textMuted)
                    .textCase(nil)
                    .accessibilityIdentifier("activity-summary")
                    // 实时通道状态给 UI 测试核对（live-事件数 / poll-事件数）
                    .accessibilityValue("\(tasks.streamConnected ? "live" : "poll")-\(tasks.streamEventCount)")
            }

            if media.error != nil || tasks.downloadsError != nil || tasks.sources.contains(where: { $0.status != "active" }) {
                Section {
                    VStack(spacing: 8) {
                        if let error = media.error { ActivityWarningBanner(message: error) }
                        TaskSourceWarning(store: tasks)
                    }
                    .listRowInsets(EdgeInsets())
                    .listRowBackground(Color.clear)
                }
            }

            if activity.attentionTotal > 0 {
                Section {
                    TaskCenterPanel(store: tasks, mode: .attention, actions: taskActions)
                        .listRowInsets(EdgeInsets())
                        .listRowBackground(Color.clear)
                } header: {
                    let canDismissAll = activity.standaloneAttentionJobs.contains { $0.status == "failed" }
                    ActivitySectionHeader(
                        title: "需要处理", count: activity.attentionTotal, tint: Theme.danger,
                        trailing: canDismissAll ? (taskActions.bulkDismissing ? "正在忽略…" : "全部忽略") : nil,
                        trailingIdentifier: "dismiss-all-failed", chevron: false,
                        action: { taskActions.dismissAllFailed(api: api, feedback: feedback, store: tasks) }
                    )
                }
            }

            if !snapshot.sessions.isEmpty || snapshot.hiddenSessionCount > 0 {
                Section {
                    ForEach(snapshot.sessions, id: \.self) { session in
                        ActivityPlaybackSessionRow(session: session, store: media, actions: deviceActions)
                    }
                } header: {
                    ActivitySectionHeader(title: "正在播放", count: snapshot.sessions.count + snapshot.hiddenSessionCount, tint: Theme.success)
                } footer: {
                    scopeFooter(hidden: snapshot.hiddenSessionCount, noun: "台设备在播放的内容")
                }
            }

            if !snapshot.downloads.isEmpty || snapshot.hiddenDownloadCount > 0 {
                Section {
                    ForEach(snapshot.downloads, id: \.self) { download in
                        ActivityFileDownloadRow(download: download, store: media, actions: deviceActions)
                    }
                } header: {
                    ActivitySectionHeader(title: "正在下载", count: snapshot.downloads.count + snapshot.hiddenDownloadCount)
                } footer: {
                    scopeFooter(hidden: snapshot.hiddenDownloadCount, noun: "条下载")
                }
            }

            if activity.activeTotal > 0 || !activity.boostTasks.isEmpty {
                Section {
                    ForEach(activeItems(activity).prefix(Self.activeLimit)) { item in
                        activeRow(item, activity: activity)
                    }
                    if !activity.boostTasks.isEmpty {
                        NavigationLink(value: AppRoute.activityPage(.boost)) {
                            ActivityBoostSummaryRow(tasks: activity.boostTasks, configured: sites)
                        }
                    }
                } header: {
                    ActivitySectionHeader(
                        title: "进行中", count: activity.activeTotal > 0 ? activity.activeTotal : nil,
                        trailing: activity.activeTotal > 0 ? "查看全部" : nil, trailingIdentifier: "activity-active-all",
                        action: { router.push(.activityPage(.active)) }
                    )
                }
            }

            if !recentPlays.isEmpty {
                Section {
                    ForEach(recentPlays, id: \.id) { entry in
                        if let route = WatchFormat.detailRoute(entry.media) {
                            NavigationLink(value: route) { ActivityRecentPlayRow(entry: entry) }
                        } else {
                            ActivityRecentPlayRow(entry: entry)
                        }
                    }
                } header: {
                    ActivitySectionHeader(
                        title: "最近播放", trailing: "查看全部", trailingIdentifier: "activity-plays-all",
                        action: { router.push(.activityPage(.plays)) }
                    )
                }
            }

            if let weekly {
                Section {
                    NavigationLink(value: AppRoute.activityPage(.stats)) { ActivityWeeklyWatchCard(stats: weekly) }
                } header: {
                    ActivitySectionHeader(
                        title: "观看统计", trailing: "查看全部", trailingIdentifier: "activity-stats-all",
                        action: { router.push(.activityPage(.stats)) }
                    )
                }
            }

            if !activity.standaloneHistoricalJobs.isEmpty {
                Section {
                    ForEach(activity.standaloneHistoricalJobs.prefix(Self.recentLimit), id: \.id) { job in
                        finishedRow(job, store: tasks)
                    }
                } header: {
                    ActivitySectionHeader(
                        title: "最近完成", trailing: "查看全部 \(activity.historyTotal) 个", trailingIdentifier: "activity-finished-all",
                        action: { router.push(.activityPage(.finished)) }
                    )
                }
            }
        }
        .listStyle(.insetGrouped)
        .headerProminence(.increased)
        .scrollContentBackground(.hidden)
        .refreshable {
            tasks.refreshJobs()
            tasks.refreshDownloads()
            media.refresh()
            await loadExtras(scope: media.scope)
        }
        .navigationTitle("活动")
        .toolbarTitleDisplayMode(.inlineLarge)
        .appBackground()
        .taskDeleteSheet(taskActions, store: tasks)
        .task(id: ExtrasKey(scope: media.scope, live: snapshot.sessions.count + snapshot.hiddenSessionCount)) {
            await loadExtras(scope: media.scope)
        }
        .task(id: activity.boostTasks.isEmpty) {
            if !activity.boostTasks.isEmpty { sites = (try? await api.siteList()) ?? sites }
        }
        .onChange(of: router.rootParameter, initial: true) { _, parameter in
            // 站内链接 /activity?view=…：点名二级页的（plays / stats / history / active）接着压栈打开，
            // 其余（需要处理、正在播放、全部、缺省）都落在总览本身
            guard let parameter, parameter.tab == .activity else { return }
            router.rootParameter = nil
            if let page = ActivityPage(webView: parameter.value) { router.push(.activityPage(page)) }
        }
    }

    // MARK: 摘要 / 脚注

    /// 「2 项需要处理 · 1 台设备在播放 · 5 个任务进行中」；什么都没有时「一切正常 · 现在没有人在看」
    private func summary(activity: TaskCenter.Activity, snapshot: API.MediaActivityView) -> Text {
        if !badges.tasks.jobsLoaded, badges.media.loading { return Text("正在读取…") }
        let watching = snapshot.sessions.count + snapshot.hiddenSessionCount
        let downloading = snapshot.downloads.count + snapshot.hiddenDownloadCount
        var parts: [Text] = []
        if activity.attentionTotal > 0 {
            parts.append(Text("\(activity.attentionTotal) 项需要处理").foregroundColor(Theme.danger).fontWeight(.semibold))
        }
        if watching > 0 { parts.append(Text("\(watching) 台设备在播放")) }
        if downloading > 0 { parts.append(Text("\(downloading) 台设备在下载")) }
        if activity.activeTotal > 0 { parts.append(Text("\(activity.activeTotal) 个任务进行中")) }
        if parts.isEmpty { return Text("一切正常 · 现在没有人在看") }
        if watching == 0 { parts.append(Text("现在没有人在看")) }
        return parts.dropFirst().reduce(parts[0]) { $0 + Text(" · ") + $1 }
    }

    /// 浏览范围脚注：有被隐藏的内容才出现，就地切到「全部」；已是「全部」时给回退出口
    @ViewBuilder
    private func scopeFooter(hidden: Int, noun: String) -> some View {
        let media = badges.media
        if hidden > 0 || media.scope == "all" {
            HStack(spacing: 4) {
                Text(hidden > 0 ? "另有 \(hidden) \(noun)不在你的浏览范围内" : "已包含对你隐藏的库")
                Button(hidden > 0 ? "显示全部" : "只看我的范围") {
                    media.setScope(hidden > 0 ? "all" : "visible")
                }
                .fontWeight(.semibold)
                .foregroundStyle(Theme.info)
                .buttonStyle(.plain)
                .accessibilityIdentifier("activity-scope-toggle")
            }
            .font(.footnote)
            .foregroundStyle(Theme.textFaint)
        }
    }

    // MARK: 行

    private func activeItems(_ activity: TaskCenter.Activity) -> [ActiveItem] {
        activity.activeDownloadGroups.map(ActiveItem.group) + activity.standaloneActiveJobs.map(ActiveItem.job)
    }

    /// 进行中的一行：点进「进行中」二级页看完整过程；左滑删种（单资源）/ 取消作业
    @ViewBuilder
    private func activeRow(_ item: ActiveItem, activity: TaskCenter.Activity) -> some View {
        let tasks = badges.tasks
        switch item {
        case let .group(group):
            NavigationLink(value: AppRoute.activityPage(.active)) {
                ActivityActiveDownloadRow(group: group, ingestJobsByHash: activity.ingestJobsByHash)
            }
            .swipeActions(allowsFullSwipe: false) {
                if group.tasks.count == 1, let task = group.tasks.first, task.downloaderId != nil {
                    Button("删除", systemImage: "trash") { taskActions.pendingDelete = task }.tint(.red)
                }
            }
        case let .job(job):
            NavigationLink(value: AppRoute.activityPage(.active)) {
                ActivityActiveJobRow(job: job)
            }
            .swipeActions(allowsFullSwipe: false) {
                if job.status != "cancelling" {
                    Button("取消任务", systemImage: "xmark") { taskActions.cancel(job, api: api, feedback: feedback, store: tasks) }
                        .tint(.orange)
                        .disabled(taskActions.cancellingJobId != nil)
                }
            }
        }
    }

    /// 最近完成的一行：点进「已结束」；左滑撤销忽略（被忽略的失败）/ 重新执行（用户取消的）
    private func finishedRow(_ job: API.JobView, store: TaskActivityStore) -> some View {
        NavigationLink(value: AppRoute.activityPage(.finished)) {
            ActivityFinishedJobRow(job: job)
        }
        .swipeActions(allowsFullSwipe: false) {
            if job.status == "failed", TaskCenter.isDismissed(job) {
                Button("撤销忽略", systemImage: "arrow.uturn.backward") {
                    taskActions.undismiss(job, api: api, feedback: feedback, store: store)
                }
                .tint(.blue)
            } else if job.status == "cancelled", !TaskCenter.isSystemCancelled(job) {
                Button("重新执行", systemImage: "arrow.clockwise") {
                    taskActions.retry(job, api: api, feedback: feedback, store: store)
                }
                .tint(.blue)
            }
        }
    }

    // MARK: 数据

    /// 最近播放 3 条 + 最近 7 天统计（两路并行；失败时保留上次结果）
    private func loadExtras(scope: String) async {
        let offset = TimeZone.current.secondsFromGMT() / 60
        async let plays = api.playbackHistory(limit: Self.recentLimit, before: nil, memberId: nil, scope: scope)
        async let stats = api.playbackStatsWatch(days: 7, tzOffset: offset, memberId: nil, scope: scope)
        if let page = try? await plays { recentPlays = page.entries }
        if let result = try? await stats { weekly = result }
        if !badges.tasks.activity.boostTasks.isEmpty { sites = (try? await api.siteList()) ?? sites }
    }
}
