import SwiftUI

/// 登录后的主界面。标签栏按 Web 手机端底栏的入口组织（待功能清单确定后补齐）。
struct MainTabView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        TabView {
            Tab("首页", systemImage: "house") {
                NavigationStack {
                    List {
                        if let session = model.session {
                            LabeledContent("账号", value: session.nickname)
                            LabeledContent("角色", value: session.isAdmin ? "超级管理员" : "成员")
                        }
                        if let server = model.server {
                            LabeledContent("服务器", value: server.displayString)
                        }
                        Button("退出登录", role: .destructive) {
                            Task { await model.logout() }
                        }
                    }
                    .navigationTitle("MovieClaw")
                }
            }
        }
    }
}
