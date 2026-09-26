import SwiftUI

// 设置 → 资源站点：展示口径与纯数据模型（对应 Web `site-config-section.tsx` 顶部常量、
// `lib/format.ts`、`lib/time.ts`、`lib/categories.ts`）。
//
// 这里只放「与 Web 逐字对齐」的文案表与格式化函数，视图文件只管排版：
// 状态徽章文案、授权类型中文名、字段标签、字节/魔力/时长的格式，一处改两端一致。

// MARK: - 状态与授权类型

enum SettingsBSiteText {
    /// 站点验证状态 → 文案 + 语义色（同 Web STATUS_META）
    static func status(_ raw: String) -> (label: String, tone: SettingsBTone) {
        switch raw {
        case "active": ("已验证", .ok)
        case "verifying": ("验证中", .info)
        case "failed": ("验证失败", .danger)
        default: ("待验证", .neutral)
        }
    }

    /// 需要轮询验证进度的中间态（同 Web IN_PROGRESS）
    static func inProgress(_ status: String) -> Bool {
        status == "pending" || status == "verifying"
    }

    /// 授权类型 → 中文名（同 Web AUTH_TYPE_LABEL）
    static func authType(_ raw: String) -> String {
        switch raw {
        case "cookie": "Cookie"
        case "apikey": "API 密钥"
        case "credential": "用户名密码"
        default: raw
        }
    }

    /// 表单字段名 → 中文标签与输入形态（同 Web FIELD_META）
    enum FieldKind { case text, password, textarea }

    static func field(_ name: String) -> (label: String, kind: FieldKind) {
        switch name {
        case "cookie": ("Cookie 字符串", .textarea)
        case "api_key": ("API 密钥", .password)
        case "username": ("用户名", .text)
        case "password": ("密码", .password)
        default: (name, .text)
        }
    }
}

// MARK: - 格式化（与 Web lib/format.ts、lib/time.ts 同口径）

enum SettingsBSiteFormat {
    static let gib = 1024 * 1024 * 1024

    /// 字节 → 「1.50 GB」：1024 进制，≥100 或单位为 B 时取整，其余两位小数（同 Web formatBytes）
    static func bytes(_ value: Int) -> String {
        guard value >= 0 else { return "—" }
        let units = ["B", "KB", "MB", "GB", "TB", "PB"]
        var v = Double(value)
        var i = 0
        while v >= 1024, i < units.count - 1 {
            v /= 1024
            i += 1
        }
        let rounded = (v * 100).rounded() / 100
        if rounded >= 100 || i == 0 { return String(format: "%.0f %@", rounded, units[i]) }
        return String(format: "%.2f %@", rounded, units[i])
    }

    /// 分享率：null = 站点未提供，显示「—」（与真实的 0.00 区分）
    static func ratio(_ value: Double?) -> String {
        guard let value else { return "—" }
        return String(format: "%.2f", value)
    }

    /// 魔力等大数：中文紧凑记数（同 Web Intl compact zh-CN：1.2万 / 3.4亿，最多一位小数）
    static func compact(_ value: Double) -> String {
        func trim(_ v: Double) -> String {
            let r = (v * 10).rounded() / 10
            return r == r.rounded() ? String(format: "%.0f", r) : String(format: "%.1f", r)
        }
        let a = abs(value)
        if a >= 100_000_000 { return trim(value / 100_000_000) + "亿" }
        if a >= 10_000 { return trim(value / 10_000) + "万" }
        return trim(value)
    }

    /// 秒 → 「15 分钟」「1.5 小时」（同 Web formatDuration）
    static func duration(_ seconds: Int) -> String {
        guard seconds > 0 else { return "—" }
        if seconds < 60 { return "\(seconds) 秒" }
        if seconds < 3600 { return "\(Int((Double(seconds) / 60).rounded())) 分钟" }
        let hours = Double(seconds) / 3600
        return hours == hours.rounded() ? "\(Int(hours)) 小时" : String(format: "%.1f 小时", hours)
    }

    /// 相对时间；空值给「从未」（同 Web formatRelativeTime 的 neverLabel）
    static func relative(_ raw: String?) -> String {
        let text = Formatters.relative(raw)
        return text.isEmpty ? "从未" : text
    }

    /// 「下次同步」：null 或已过期都显示「即将开始」，避免出现「下次同步：3 分钟前」
    static func nextSync(_ raw: String?) -> String {
        guard let date = Formatters.date(raw), date > .now else { return "即将开始" }
        return Formatters.relative(raw)
    }

    /// 绝对时间「2026/07/09 18:00」（同 Web formatDateTime）
    static func dateTime(_ raw: String?) -> String {
        guard let date = Formatters.date(raw) else { return "—" }
        let f = DateFormatter()
        f.locale = Locale(identifier: "zh_CN")
        f.dateFormat = "yyyy/MM/dd HH:mm"
        return f.string(from: date)
    }
}

// MARK: - 搜索分类（内置分类常量）

/// 资源分类（与后端 TorrentCategory 逐一对应，顺序即 Web CATEGORY_OPTIONS）
enum SettingsBSitePresetCategory {
    static let options: [(value: String, label: String)] = [
        ("movie", "电影"), ("tv", "剧集"), ("documentary", "纪录片"), ("anime", "动漫"),
        ("music", "音乐"), ("game", "游戏"), ("av", "成人"), ("other", "其他"),
    ]

    static func label(_ value: String) -> String {
        options.first { $0.value == value }?.label ?? value
    }

    /// 语义不自明的分类才给补充说明（同 Web CATEGORY_HINT）
    static func hint(_ value: String) -> String? {
        switch value {
        case "av": "默认隐藏；打开后搜索面板会出现「成人」分类"
        case "other": "兜底分类：站点上归不进以上类别的分区（软件、电子书、体育、综艺等，各站范围不同）"
        default: nil
        }
    }
}

/// 搜索标签栏的一项：内置分类（type=category）或自定义分类（type=preset）。
///
/// 为什么手写而不用生成模型：后端 `SearchPresetListView.presets` 是判别联合，
/// 生成器只能给出 `[JSONValue]`。这里做一层双向转换，读写都经它，
/// 保存时只写回 Web 同样的字段（整体覆盖式保存，后端负责规范化）。
struct SettingsBSitePresetTab: Hashable {
    var type: String
    var id: String
    var visible: Bool
    var name = ""
    var categories: [String] = []
    var siteIds: [String] = []
    /// 图览模式：用该分类搜索时，结果页默认以图墙展示
    var posterMode = false
    /// 无痕搜索：用该分类搜索时不写入搜索历史
    var skipHistory = false

    var isPreset: Bool { type == "preset" }
    /// 列表稳定键（内置分类与自定义分类 id 可能同名，拼上类型）
    var key: String { "\(type):\(id)" }
    /// 展示名：内置分类取中文名，预设取用户起的名字
    var label: String { isPreset ? name : SettingsBSitePresetCategory.label(id) }

    /// 预设行摘要：分类组合 · 站点组合（· 图览 · 无痕），空集显示「不限分类 / 全部站点」
    var summary: String {
        let cats = categories.isEmpty ? "不限分类" : categories.map(SettingsBSitePresetCategory.label).joined(separator: "、")
        let sites = siteIds.isEmpty ? "全部站点" : "\(siteIds.count) 个站点"
        return "\(cats) · \(sites)\(posterMode ? " · 图览" : "")\(skipHistory ? " · 无痕" : "")"
    }

    init(type: String, id: String, visible: Bool) {
        self.type = type
        self.id = id
        self.visible = visible
    }

    init?(json: API.JSONValue) {
        guard case let .object(obj) = json,
              case let .string(type)? = obj["type"],
              case let .string(id)? = obj["id"] else { return nil }
        self.type = type
        self.id = id
        if case let .bool(v)? = obj["visible"] { visible = v } else { visible = true }
        if case let .string(v)? = obj["name"] { name = v }
        categories = Self.strings(obj["categories"])
        siteIds = Self.strings(obj["site_ids"])
        if case let .bool(v)? = obj["poster_mode"] { posterMode = v }
        if case let .bool(v)? = obj["skip_history"] { skipHistory = v }
    }

    var json: API.JSONValue {
        var obj: [String: API.JSONValue] = ["type": .string(type), "id": .string(id), "visible": .bool(visible)]
        if isPreset {
            obj["name"] = .string(name)
            obj["categories"] = .array(categories.map { .string($0) })
            obj["site_ids"] = .array(siteIds.map { .string($0) })
            obj["poster_mode"] = .bool(posterMode)
            obj["skip_history"] = .bool(skipHistory)
        }
        return .object(obj)
    }

    private static func strings(_ value: API.JSONValue?) -> [String] {
        guard case let .array(items)? = value else { return [] }
        return items.compactMap { if case let .string(s) = $0 { s } else { nil } }
    }
}
