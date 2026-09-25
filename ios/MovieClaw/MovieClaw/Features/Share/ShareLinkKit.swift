import Foundation

/// 分享链接的纯逻辑（同 Web `lib/share.ts`）：链接补全、复制文本、到期提示。
///
/// 媒体库模块的分享对话框（`LibraryShareSheet`）里有一份私有实现；这里给管理页「分享」页签与
/// 访客页用，口径逐字相同，改文案请三处一起改。
enum ShareLinkKit {
    /// 分享链接补全：后端在未配置外部访问地址时给的是相对路径 `/s/{slug}`，用服务器根地址补成绝对地址
    static func absoluteURL(_ url: String, origin: URL) -> String {
        let lower = url.lowercased()
        if lower.hasPrefix("http://") || lower.hasPrefix("https://") { return url }
        var base = origin.absoluteString
        while base.hasSuffix("/") { base.removeLast() }
        return url.hasPrefix("/") ? base + url : base + "/" + url
    }

    /// 「复制链接和密码」：一段可以直接粘进聊天框的话
    static func copyText(title: String, url: String, password: String?) -> String {
        var parts = ["《\(title)》", "链接：\(url)"]
        if let password { parts.append("密码：\(password)") }
        return parts.joined(separator: " ")
    }

    /// 到期提示：「N 天后失效」/「N 小时后失效」/「N 分钟后失效」/「已失效」
    static func expiryHint(_ expiresAt: String) -> String {
        guard let date = Formatters.date(expiresAt) else { return "已失效" }
        let remaining = date.timeIntervalSinceNow
        if remaining <= 0 { return "已失效" }
        let hours = remaining / 3600
        if hours >= 47 { return "\(Int((hours / 24).rounded())) 天后失效" }
        if hours >= 1 { return "\(Int(hours.rounded(.down))) 小时后失效" }
        return "\(max(1, Int((remaining / 60).rounded(.down)))) 分钟后失效"
    }
}
