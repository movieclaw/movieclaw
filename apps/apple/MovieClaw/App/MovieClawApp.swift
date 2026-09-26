import SwiftUI

@main
struct MovieClawApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
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

/// 应用代理：目前只负责界面方向锁（见 `OrientationLock`）。
final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication, supportedInterfaceOrientationsFor window: UIWindow?) -> UIInterfaceOrientationMask {
        OrientationLock.mask
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
