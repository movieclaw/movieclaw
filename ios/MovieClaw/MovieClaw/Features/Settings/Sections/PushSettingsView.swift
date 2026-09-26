import SwiftUI

/// 设置 → 消息推送（对应 Web `im-push-section.tsx` 的 `ImPushSection`）。
///
/// 两类设定按分段标签分开（与 Web 的胶囊标签一致），因为它们回答的是两个完全不同的问题：
/// - **接入通道**：我接了哪些账号？——跨平台统一列表 +「新增通道」菜单，绑定流程收进弹层；
/// - **推送内容**：什么事件会推给我？——事件开关（逐项即时保存）+ 一键测试推送。
/// 切换标签时另一侧卸载，回来重新拉取（同 Web）。
struct PushSettingsView: View {
    @State private var tab: SettingsBPushTab = .channels
    /// 深链 `?tab=content` 直达推送内容（Web useTabParam），只在首次出现时读一次
    @Environment(\.routeQuery) private var routeQuery
    @State private var routeQueryConsumed = false

    var body: some View {
        Form {
            Section {
                Picker("分类", selection: $tab) {
                    ForEach(SettingsBPushTab.allCases, id: \.self) { Text($0.title).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .accessibilityIdentifier("push-tab")
            }
            .listRowBackground(Color.clear)
            .listRowInsets(EdgeInsets())

            switch tab {
            case .channels: SettingsBPushChannelsSections()
            case .content: SettingsBPushContentSections()
            }
        }
        .settingsBFormStyle()
        .onAppear {
            guard !routeQueryConsumed else { return }
            routeQueryConsumed = true
            if let raw = routeQuery["tab"], let value = SettingsBPushTab(rawValue: raw) { tab = value }
        }
    }
}

enum SettingsBPushTab: String, CaseIterable {
    case channels, content

    var title: String {
        switch self {
        case .channels: "接入通道"
        case .content: "推送内容"
        }
    }
}
