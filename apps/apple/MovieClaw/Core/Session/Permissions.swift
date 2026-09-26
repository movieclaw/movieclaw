import SwiftUI

/// 权限快照（同 Web `lib/permissions.ts`）：客户端据此裁剪入口，安全边界仍在后端 403。
struct Permissions: Equatable {
    var isAdmin: Bool
    var canSubscribe: Bool
    var canSearch: Bool
    var canDirectDownload: Bool
    var canManageLibraries: Bool { isAdmin }
    var canManageSubscriptions: Bool { isAdmin }

    init(session: API.SessionView) {
        isAdmin = session.role == "admin"
        canSubscribe = isAdmin || session.capabilities.allowSubscribe == true
        canSearch = isAdmin || session.capabilities.allowSearch == true
        canDirectDownload = isAdmin || session.capabilities.allowDirectDownload == true
    }

    static let none = Permissions()
    private init() {
        isAdmin = false
        canSubscribe = false
        canSearch = false
        canDirectDownload = false
    }

    /// 同 Web `accessiblePathFor`：成员进不了的页面落回媒体库
    func allows(_ route: AppRoute) -> Bool {
        switch route {
        case .newSession, .session, .activity, .activityPage: isAdmin
        case .subscriptions, .subscription: canSubscribe
        case .searchHome, .search: canSearch
        case let .settingsSection(section, _): isAdmin || section.memberVisible
        case .libraryManage: isAdmin
        default: true
        }
    }
}

extension API.SessionView {
    var roleLabel: String { role == "admin" ? "超级管理员" : "成员" }

    /// 头像徽标字：中文取首字，拉丁字母取前两位大写（同 Web initialsOf）
    var initials: String {
        let name = nickname.trimmingCharacters(in: .whitespaces)
        guard let first = name.first else { return "?" }
        if first.unicodeScalars.first.map({ (0x4E00 ... 0x9FFF).contains($0.value) }) == true {
            return String(first)
        }
        return String(name.prefix(2)).uppercased()
    }
}

private struct PermissionsKey: EnvironmentKey {
    static let defaultValue = Permissions.none
}

extension EnvironmentValues {
    /// 当前账号权限；由 MainTabView 注入
    var permissions: Permissions {
        get { self[PermissionsKey.self] }
        set { self[PermissionsKey.self] = newValue }
    }
}
