import SwiftUI

/// 设置分区 → 分区页面的派发（每个分区一个 View，由设置模块实现）。
/// 成员打开管理员分区时给出无权限提示（Web 端会重定向回设置首页）。
struct SettingsSectionView: View {
    let section: SettingsSection
    @Environment(\.permissions) private var permissions

    var body: some View {
        Group {
            if permissions.isAdmin || section.memberVisible {
                content
            } else {
                EmptyState(systemImage: "lock", title: "无权访问", message: "该设置仅超级管理员可见")
            }
        }
        .navigationTitle(section.title)
        .navigationBarTitleDisplayMode(.inline)
    }

    @ViewBuilder
    private var content: some View {
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
