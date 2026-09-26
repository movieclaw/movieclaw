import Foundation
import Security

/// 只保存 Worker Token 的 Keychain 小封装。
///
/// 不把 Token 放进 UserDefaults 是有意的：UserDefaults 会落成普通 plist，
/// 而菜单栏 App 和 launchd 使用的是同一个用户 Keychain，因此重启后仍可自动连接。
///
/// ## 关于「每次都弹窗要钥匙串密码」
///
/// 钥匙串条目带着**创建它的那个程序**的访问控制表（ACL），ACL 靠代码签名认人。
/// 用 Developer ID 正式签名的 App 有稳定身份，读取不弹窗；而 ad-hoc 签名
/// （`codesign --sign -`，也就是 `package-app.sh` 在没设
/// `MOVIECLAW_SIGNING_IDENTITY` 时的默认行为）没有证书，系统只能拿二进制的
/// cdhash 当身份——**每重新构建一次身份就变一次**，新构建在旧条目眼里是另一个
/// 程序，于是又要问一次密码。
///
/// 这个模块能做的两件事都做了：
///
/// 1. 写入走「先删再建」而不是 update，让当前这份程序干净地重新拿到条目所有权，
///    而不是永远对着上一份构建留下的 ACL 要授权；
/// 2. 读取只发生在真正需要令牌去连接的时候——「有没有配过令牌」这种布尔事实
///    记在 UserDefaults 里（`ConfigurationStore`），不值得为它敲一次钥匙串。
///
/// 剩下的部分不在代码里：**发行版必须用 Developer ID 正式签名**，否则用户每次
/// 装新版本都会被问一次密码（点「始终允许」只在同一份二进制没变时有效）。
///
/// ## 让那个系统弹窗说人话
///
/// 弹窗的文字是系统写死的，只有一处由我们决定：**条目的名字**。系统会说「想要使用
/// 你存储在钥匙串中『<名字>』里的机密信息」。早先没设名字，系统用服务名顶上，弹出来
/// 就是一句「com.movieclaw.transcoder」，用户根本看不出存的是什么。现在名字是
/// 「MovieClaw 转码器连接密钥」，种类、注释写明它不是你的密码。
///
/// 另外，读之前可以先**静默探测**一次（`interactive: false`，禁止交互）：要弹窗的话
/// 系统直接返回失败而不弹，AppMain 就能先用自己的话解释清楚，再让系统弹窗。
enum KeychainStore {
    private static let service = "com.movieclaw.transcoder"
    private static let account = "worker-token"
    /// 条目名：系统授权弹窗里显示的就是它。
    /// 界面上统一叫「连接密钥」，和 App 的说明窗对得上。
    static let label = "MovieClaw 转码器连接密钥"
    /// 「钥匙串访问」里的「种类」一栏。
    private static let kind = "连接密钥"
    private static let comment = "配对时生成，用来连接 movieclaw。不是你的密码；"
        + "要停用，在 movieclaw 网页「设置 → 设备」里吊销。"

    /// 读取需要用户在系统弹窗里授权（静默探测时抛出，由调用方先解释再真读）。
    struct ApprovalRequired: Error {}

    /// - Parameter interactive: false 时禁止系统弹窗——需要授权就抛 ``ApprovalRequired``。
    static func readToken(interactive: Bool = true) throws -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: CFTypeRef?
        let status = interactive
            ? SecItemCopyMatching(query as CFDictionary, &result)
            : withoutUserInteraction { SecItemCopyMatching(query as CFDictionary, &result) }
        if status == errSecItemNotFound {
            return nil
        }
        // 禁止交互时，「要弹窗授权」表现为这两个状态码（本机实测是 errSecAuthFailed）
        if !interactive, status == errSecAuthFailed || status == errSecInteractionNotAllowed {
            throw ApprovalRequired()
        }
        guard status == errSecSuccess else {
            throw KeychainError(status: status)
        }
        if interactive {
            renameLegacyItemIfPossible()
        }
        guard let data = result as? Data,
              let token = String(data: data, encoding: .utf8)
        else {
            throw ConfigurationError.message("Keychain 中的 Worker Token 格式无效")
        }
        return token
    }

    /// 写入令牌。**先删再建**，不用 SecItemUpdate。
    ///
    /// update 会命中已存在条目的 ACL：如果那条是上一份构建创建的，系统认为
    /// 现在这个程序是外人，于是弹窗要钥匙串密码——而且每次都弹，因为 ACL 永远
    /// 不会自己更新。删除不需要通过条目的读取授权，删掉再新建，ACL 就换成了
    /// 当前这份程序的。
    ///
    /// 删除失败也不放弃：万一某个系统版本上删除也要授权，还留着 update 这条
    /// 退路，行为不会比原来差。
    static func saveToken(_ token: String) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        var attributes: [String: Any] = [
            kSecValueData as String: Data(token.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]
        attributes.merge(descriptiveAttributes) { _, new in new }

        // 丢弃返回值是有意的：条目不存在（首次配对）同样走下面的新建
        _ = SecItemDelete(query as CFDictionary)
        var item = query
        item.merge(attributes) { _, new in new }
        let addStatus = SecItemAdd(item as CFDictionary, nil)
        if addStatus == errSecSuccess {
            return
        }
        guard addStatus == errSecDuplicateItem else {
            throw KeychainError(status: addStatus)
        }
        // 删除没生效（条目还在），退回原来的 update 路径
        let updateStatus = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        guard updateStatus == errSecSuccess else {
            throw KeychainError(status: updateStatus)
        }
    }

    /// 名字、种类、注释：系统弹窗和「钥匙串访问」里看到的都是这几项。
    private static var descriptiveAttributes: [String: Any] {
        [
            kSecAttrLabel as String: label,
            kSecAttrDescription as String: kind,
            kSecAttrComment as String: comment,
        ]
    }

    /// 旧版本存的条目没有名字（弹窗里只显示服务名）。刚刚读成功说明当前这份程序已被
    /// 授权，顺手补上名字，下次再弹窗就说人话了。**禁止交互**：万一补名字还要再授权
    /// 一次（用户只点了「允许」而不是「始终允许」），宁可不补，也不能再弹一个窗。
    private static func renameLegacyItemIfPossible() {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnAttributes as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let attributes = result as? [String: Any],
              attributes[kSecAttrLabel as String] as? String != label
        else { return }
        let target: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let status = withoutUserInteraction {
            SecItemUpdate(target as CFDictionary, descriptiveAttributes as CFDictionary)
        }
        if status == errSecSuccess {
            AppLogger.shared.info("已给钥匙串里的设备令牌补上说明性的名字")
        }
    }

    /// 在禁止钥匙串交互的状态下执行一次操作：需要弹窗的操作直接失败而不弹。
    ///
    /// `SecKeychainSetUserInteractionAllowed` 已标废弃，但对传统（文件型）钥匙串的
    /// ACL 授权弹窗，它是唯一确实管用的开关（本机实测：禁止后读取直接返回
    /// errSecAuthFailed，不弹窗）。进程级开关，用完立刻恢复。编译时这两行会有「已废弃」
    /// 告警，是有意保留的。
    private static func withoutUserInteraction(_ body: () -> OSStatus) -> OSStatus {
        SecKeychainSetUserInteractionAllowed(false)
        defer { SecKeychainSetUserInteractionAllowed(true) }
        return body()
    }

    static func deleteToken() throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError(status: status)
        }
    }
}

/// 必须实现 LocalizedError：界面和日志一律走 `error.localizedDescription`，
/// 只实现 CustomStringConvertible 的话，精心写好的中文提示一个字都到不了用户
/// 眼前（同 Models.swift 里 ConfigurationError 的注释）。
private struct KeychainError: Error, LocalizedError, CustomStringConvertible {
    let status: OSStatus

    var description: String {
        // SecCopyErrorMessageString 给的是可读原因（如「User interaction is not
        // allowed」），比裸状态码更能让非开发者判断该怎么办；取不到时退回状态码。
        let reason = SecCopyErrorMessageString(status, nil) as String?
        let detail = reason.flatMap { $0.isEmpty ? nil : $0 }
        let head = detail.map { "钥匙串操作失败：\($0)（状态码 \(status)）" }
            ?? "钥匙串操作失败（状态码 \(status)）"
        guard let hint = Self.hint(for: status) else { return head }
        return head + "。" + hint
    }

    var errorDescription: String? { description }

    /// 把「用户点了拒绝 / 没有界面可弹窗」这两类翻译成人能照做的下一步。
    ///
    /// 这两个状态码几乎总是同一个根因：App 没有用 Developer ID 正式签名，
    /// 系统认不出它就是当初创建这条记录的那个程序。用户看到「状态码 -25293」
    /// 只会一头雾水，看到这句话至少知道点「始终允许」，以及为什么下次装新版本
    /// 还会被问一次。
    private static func hint(for status: OSStatus) -> String? {
        switch status {
        case errSecUserCanceled, errSecAuthFailed, errSecInteractionNotAllowed:
            return "如果系统弹窗问过钥匙串密码，请选「始终允许」。"
                + "本 App 未用 Developer ID 签名时，每次装了新版本都会再问一次，这是系统的机制"
        default:
            return nil
        }
    }
}
