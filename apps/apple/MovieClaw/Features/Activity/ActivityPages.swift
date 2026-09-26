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

/// 刷流做种：页头实时汇总 → 按站点（开着 / 已暂停 / 已关闭）→ 逐种子一行 → 底部「清理刷流种子…」。
///
/// 清理（docs/design/site-protection-ratio-boost.md §2.9）：关闭刷流不会删种，残留种子会一直满速做种、
/// 占着磁盘，这里是事后清掉它们的入口。点开先取最新的在池概况，再用系统底部菜单讲清后果：
/// 删多少、还开着刷流的站点会一并关闭、保留期内的（提前删可能被记 H&R）默认到期后自动删，
/// 另给「立即全部删除」由用户自担风险。同 Safari「清除历史记录」，破坏性入口放在列表最底下的红字行。
private struct ActivityBoostPage: View {
    @Environment(ShellBadges.self) private var badges
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @State private var pool: API.BoostPoolView?
    /// 服务端支持清理（旧版服务端没有 /sites/boost-pool 时隐藏清理入口）
    @State private var supportsCleanup = false
    @State private var confirming = false
    @State private var cleaning = false

    var body: some View {
        let tasks = badges.tasks.activity.boostTasks
        let totals = ActivityBoostTotals(tasks)
        let sites = ActivityBoostSites(tasks: tasks, pool: pool)
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
            }
            if !sites.sites.isEmpty {
                Section {
                    ForEach(sites.sites) { site in siteRow(site) }
                } header: {
                    Text("按站点").textCase(nil)
                } footer: {
                    if sites.count(.off) > 0 {
                        Text("关闭刷流不会删除已有种子：它们会继续满速做种，引擎也不再自动汰换。可以在本页最下方清理。")
                    }
                }
            }
            if !tasks.isEmpty {
                Section {
                    ForEach(ActivityBoostTotals.sorted(tasks)) { task in
                        BoostTaskRow(task: task, cleanupNote: cleanupNote(sites.taskStates[task.infoHash.lowercased()]))
                    }
                } header: {
                    Text("按上行速度排序").textCase(nil)
                }
                if supportsCleanup {
                Section {
                    Button(role: .destructive) {
                        Task { await prepareCleanup() }
                    } label: {
                        HStack {
                            Spacer()
                            if cleaning { ProgressView().padding(.trailing, 6) }
                            Text(cleaning ? "正在清理…" : "清理刷流种子…")
                            Spacer()
                        }
                    }
                    .disabled(cleaning)
                    .accessibilityIdentifier("boost-cleanup")
                    // 挂在按钮上：iOS 26 的确认菜单是贴着来源弹出的气泡，箭头要指向这个按钮
                    .confirmationDialog(confirmTitle, isPresented: $confirming, titleVisibility: .visible) {
                        let plan = CleanupPlan(pool)
                        Button(plan.enabledNames.isEmpty ? "清理" : "关闭刷流并清理", role: .destructive) {
                            Task { await cleanup(force: false) }
                        }
                        if plan.protectedCount > 0 {
                            Button("立即全部删除（可能被记 H&R）", role: .destructive) {
                                Task { await cleanup(force: true) }
                            }
                        }
                        Button("取消", role: .cancel) {}
                    } message: {
                        Text(CleanupPlan(pool).message)
                    }
                } footer: {
                    Text("从下载器删除刷流种子及其数据文件，无法恢复。还没做满站点要求做种时长的，默认等到期后再自动删除，避免被记 H&R。")
                }
                }
            }
        }
        .listStyle(.insetGrouped)
        .scrollContentBackground(.hidden)
        .refreshable {
            badges.tasks.refreshDownloads()
            await loadPool()
        }
        .navigationTitle("刷流做种")
        .navigationBarTitleDisplayMode(.inline)
        .appBackground()
        .task { await loadPool() }
    }

    // MARK: 清理

    /// 清理前的汇总：删多少、哪些站还开着刷流、保留期内有多少（都取自最新的在池概况）
    private struct CleanupPlan {
        var count = 0
        var bytes = 0
        var protectedCount = 0
        var protectedBytes = 0
        var protectedUntil: String?
        var enabledNames: [String] = []

        init(_ pool: API.BoostPoolView?) {
            for site in pool?.sites ?? [] {
                count += site.taskCount
                bytes += site.sizeBytes
                protectedCount += site.protectedCount
                protectedBytes += site.protectedBytes
                if let until = site.protectedUntil, until > (protectedUntil ?? "") { protectedUntil = until }
                if site.boostEnabled { enabledNames.append(site.siteName) }
            }
        }

        var message: String {
            var lines = ["将从下载器删除 \(count) 个刷流种子及其数据文件（共 \(ActivityFormat.bytes(Double(bytes)))），无法恢复。"]
            if !enabledNames.isEmpty {
                lines.append("\(enabledNames.joined(separator: "、")) 还开着刷流，清理时会一并关闭，否则引擎几分钟内又会拉新种。")
            }
            if protectedCount > 0 {
                let until = ActivityBoostCleanupText.deadline(protectedUntil).map { "，最晚 \($0)" } ?? ""
                let rest = protectedCount == count ? "它们" : "这 \(protectedCount) 个"
                lines.append(
                    "其中 \(protectedCount) 个（\(ActivityFormat.bytes(Double(protectedBytes)))）还没做满站点要求的做种时长，现在删可能被记 H&R。"
                        + "选「清理」会先删其余的，\(rest)到期后自动删除\(until)。"
                )
            }
            return lines.joined(separator: "\n\n")
        }
    }

    private var confirmTitle: String {
        let count = CleanupPlan(pool).count
        return count > 0 ? "清理 \(count) 个刷流种子？" : "清理刷流种子？"
    }

    private func cleanupNote(_ state: API.BoostPoolTaskView?) -> String? {
        guard let state, state.cleanupScheduled else { return nil }
        if let until = ActivityBoostCleanupText.deadline(state.protectedUntil) {
            return "已请求清理 · \(until) 保留期满后自动删除"
        }
        return "已请求清理 · 下一轮巡检删除"
    }

    private func loadPool() async {
        guard let loaded = await ActivityBoostPoolLoader.load(api) else { return }
        pool = loaded.pool
        supportsCleanup = loaded.supportsCleanup
    }

    /// 先取最新概况再弹确认（刚关掉刷流 / 保留期刚过，旧数据会讲错后果）
    private func prepareCleanup() async {
        do {
            pool = try await api.siteBoostPoolShow()
            confirming = true
        } catch {
            feedback.error(error)
        }
    }

    private func cleanup(force: Bool) async {
        cleaning = true
        defer { cleaning = false }
        do {
            let result = try await api.siteBoostPoolCleanup(body: .init(siteIds: nil, disableBoost: true, force: force))
            feedback.success(ActivityBoostCleanupText.summary(result))
            badges.tasks.refreshDownloads()
            await loadPool()
        } catch {
            feedback.error(error)
        }
    }

    private func siteRow(_ site: ActivityBoostSites.Site) -> some View {
        let totals = ActivityBoostTotals(site.tasks)
        let size = site.tasks.reduce(0) { $0 + ($1.sizeBytes ?? 0) }
        let (label, color): (String, Color) = switch site.mode {
        case .running: ("刷流中", Theme.success)
        case .paused: ("已暂停", Theme.warning)
        case .off: ("已关闭", Theme.textFaint)
        }
        return HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(site.name).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text)
                Text(WatchFormat.metaLine([
                    "\(site.tasks.count) 个种子", ActivityFormat.bytes(Double(size)), "↑ \(ActivityFormat.rate(Double(totals.upSpeed)))",
                ]))
                .font(.footnote).monospacedDigit().foregroundStyle(Theme.textMuted)
                if let scheduled = site.pool?.scheduledCount, scheduled > 0 {
                    Text("\(scheduled) 个已请求清理，保留期满后自动删除")
                        .font(.footnote).foregroundStyle(Theme.warning)
                }
            }
            Spacer(minLength: 8)
            Text(label).font(.footnote.weight(.medium)).foregroundStyle(color)
        }
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
