import SwiftUI

/// 设置分区 → 分区页面的派发（每个分区一个 View，由设置模块实现）。
/// 成员打开管理员分区时改为显示「个人信息」（Web settings-view 把越权分区 replace 到 /settings/profile；
/// 路由层 Router.guarded 已先改道，这里兜住直接构造本页的情况）。
struct SettingsSectionView: View {
    let section: SettingsSection
    @Environment(\.permissions) private var permissions

    var body: some View {
        let effective = permissions.isAdmin || section.memberVisible ? section : .profile
        content(effective)
            .navigationTitle(effective.title)
            .navigationBarTitleDisplayMode(.inline)
    }

    @ViewBuilder
    private func content(_ section: SettingsSection) -> some View {
        switch section {
        case .overview: OverviewSettingsView()
        case .profile: ProfileSettingsView()
        case .appearance: AppearanceSettingsView()
        case .members: MembersSettingsView()
        case .devices: DevicesSettingsView()
        case .subscription: SubscriptionRulesSettingsView()
        case .sites: SitesSettingsView()
        case .downloaders: DownloadersSettingsView()
        case .importWatch: ImportWatchSettingsView()
        case .scrape: ScrapeSettingsView()
        case .playback: PlaybackSettingsView()
        case .imPush: PushSettingsView()
        case .webhook: WebhookSettingsView()
        case .llm: LLMSettingsView()
        case .mcp: MCPSettingsView()
        case .ai: AIDefaultsSettingsView()
        case .app: AppMaintenanceSettingsView()
        case .network: NetworkSettingsView()
        case .logs: LogsSettingsView()
        }
    }
}
