import Foundation
import Security

/// 会话 Cookie 的钥匙串备份。
///
/// 为什么需要：`HTTPCookieStorage.shared` 落盘是异步的，登录后立刻被系统杀掉
/// （或用户划掉 App）时 Cookie 可能还没写到磁盘，下次启动就掉登录。
/// 这里在每次服务器下发 `Set-Cookie` 后，把该服务器的全部 Cookie 同步写进钥匙串；
/// 冷启动时再灌回 `HTTPCookieStorage`。会话令牌属于凭证，放钥匙串而不是 UserDefaults。
nonisolated enum CookieVault {
    private static let service = "io.movieclaw.app.cookies"

    /// 把某服务器当前的全部 Cookie 写入钥匙串（覆盖旧值）
    static func save(for server: ServerAddress) {
        let cookies = HTTPCookieStorage.shared.cookies(for: server.origin) ?? []
        let properties = cookies.compactMap { $0.properties }
        guard let data = try? NSKeyedArchiver.archivedData(withRootObject: properties, requiringSecureCoding: false) else { return }
        let query = baseQuery(server)
        SecItemDelete(query as CFDictionary)
        var add = query
        add[kSecValueData as String] = data
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(add as CFDictionary, nil)
    }

    /// 冷启动时把钥匙串里的 Cookie 灌回共享存储（已过期的系统会自动忽略）
    static func restore(for server: ServerAddress) {
        var query = baseQuery(server)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data,
              let list = try? NSKeyedUnarchiver.unarchivedObject(
                  ofClasses: [NSArray.self, NSDictionary.self, NSString.self, NSDate.self, NSNumber.self, NSURL.self],
                  from: data
              ) as? [[HTTPCookiePropertyKey: Any]]
        else { return }
        for properties in list {
            if let cookie = HTTPCookie(properties: properties) {
                HTTPCookieStorage.shared.setCookie(cookie)
            }
        }
    }

    /// 清空全部备份（UI 测试重置、退出全部账号）
    static func clearAll() {
        SecItemDelete([kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service] as CFDictionary)
    }

    private static func baseQuery(_ server: ServerAddress) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: server.origin.absoluteString,
        ]
    }
}
