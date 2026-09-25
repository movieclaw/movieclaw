import SwiftUI

@main
struct MovieClawApp: App {
    @State private var model = AppModel()

    init() {
        ImagePipelineSetup.configure()
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .preferredColorScheme(.dark)
        }
    }
}

/// 按 AppModel.phase 切换顶层界面。
struct RootView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Group {
            switch model.phase {
            case .launching:
                ProgressView()
                    .task { await model.restore() }
            case .needsServer:
                ServerConnectView()
            case .needsSetup:
                LoginView(mode: .setup)
            case .needsLogin:
                LoginView(mode: .login)
            case let .ready(session):
                MainTabView()
                    .id(session.username) // 切换账号时整棵树重建，避免残留上个账号的数据
            }
        }
        .animation(.default, value: model.phase)
    }
}
