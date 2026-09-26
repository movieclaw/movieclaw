import SwiftUI

/// 设置首页：分区列表（Web 手机端 /settings，components/settings-index.tsx）。
/// 成员只看到「个人信息」；空标题的组（概览）不渲染组头。
struct SettingsIndexView: View {
    @Environment(\.permissions) private var permissions

    var body: some View {
        List {
            ForEach(SettingsSection.groups, id: \.title) { group in
                let items = group.items.filter { permissions.isAdmin || $0.memberVisible }
                if !items.isEmpty {
                    Section {
                        ForEach(items) { section in
                            NavigationLink(value: AppRoute.settingsSection(section)) {
                                Label {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(section.title)
                                        Text(section.subtitle)
                                            .font(.caption)
                                            .foregroundStyle(Theme.textMuted)
                                            .lineLimit(2)
                                    }
                                } icon: {
                                    Image(systemName: section.systemImage)
                                }
                            }
                            .accessibilityIdentifier("settings-\(section.rawValue)")
                        }
                    } header: {
                        if !group.title.isEmpty { Text(group.title) }
                    }
                }
            }
        }
        .navigationTitle("设置")
        .appBackground()
    }
}
