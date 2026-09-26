import SwiftUI

/// 设置 → 更新与维护（Web settings-view.tsx 的 AppSection）：三个胶囊页签。
///
/// - 版本与更新（默认页签：这一页的高频入口是「有可用更新」，用户带着「来更新」的意图落地）→ `AppUpdatePanel`；
/// - 缓存管理 → `AppStoragePanel`；
/// - 定时任务 → `ScheduledTasksPanel`。
/// 有待更新时「版本与更新」页签上点一颗蓝点（同 Web 侧栏的更新徽标，`GET /app/update/pending`）。
/// 每个页签自带一张完整列表，页签条作为列表的第一行，切换页签时各自的轮询随视图一起启停。
struct AppMaintenanceSettingsView: View {
    @Environment(\.api) private var api

    enum Tab: String, Hashable { case update, storage, tasks }

    @State private var tab: Tab = .update
    /// 深链 `?tab=storage|tasks` 直达对应页签（Web useTabParam），只在首次出现时读一次
    @Environment(\.routeQuery) private var routeQuery
    @State private var routeQueryConsumed = false
    /// 外壳常驻的待更新快照（10 分钟轮询 + 回前台刷新，同 Web usePendingUpdate）；不在外壳里时为空
    @Environment(ShellBadges.self) private var badges: ShellBadges?

    var body: some View {
        Group {
            switch tab {
            case .update: AppUpdatePanel { tabs }
            case .storage: AppStoragePanel { tabs }
            case .tasks: ScheduledTasksPanel { tabs }
            }
        }
        .scrollDismissesKeyboard(.interactively)
        .appBackground()
        .onAppear {
            guard !routeQueryConsumed else { return }
            routeQueryConsumed = true
            if let raw = routeQuery["tab"], let value = Tab(rawValue: raw) { tab = value }
        }
        // 进页再拉一次最新快照，页签蓝点随外壳的轮询实时更新
        .task { await badges?.refreshUpdate(api: api) }
    }

    private var tabs: some View {
        SettingsPillTabs(
            tabs: [(Tab.update, "版本与更新"), (Tab.storage, "缓存管理"), (Tab.tasks, "定时任务")],
            selection: $tab,
            dotted: badges?.updatePending == true ? [.update] : [],
            identifierPrefix: "app-tab"
        )
    }
}
