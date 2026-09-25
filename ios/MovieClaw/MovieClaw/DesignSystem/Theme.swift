import SwiftUI

/// 设计令牌：取自 Web 银玻璃主题（apps/web/app/globals.css :root），保证两端观感一致。
/// 原生控件（列表、按钮、标签栏、工具栏）一律用系统的液态玻璃材质，这里只定义颜色与尺寸。
enum Theme {
    /// 页面底色 --bg #0a0b10
    static let background = Color(red: 0x0A / 255, green: 0x0B / 255, blue: 0x10 / 255)
    /// 浮层卡片 --surface-raised
    static let surfaceRaised = Color(red: 30 / 255, green: 33 / 255, blue: 43 / 255).opacity(0.74)
    /// 内嵌输入 --surface-inset
    static let surfaceInset = Color.white.opacity(0.05)
    /// 正文 --text
    static let text = Color(red: 0xF3 / 255, green: 0xF5 / 255, blue: 0xF9 / 255)
    /// 次要文字 --text-muted
    static let textMuted = text.opacity(0.62)
    /// 弱化文字 --text-faint
    static let textFaint = text.opacity(0.36)
    /// 强调色：冷银 --accent
    static let accent = Color(red: 0xCD / 255, green: 0xD6 / 255, blue: 0xE6 / 255)
    static let accentStrong = Color(red: 0xEE / 255, green: 0xF2 / 255, blue: 0xF8 / 255)
    static let accentSoft = accent.opacity(0.14)
    static let danger = Color(red: 1, green: 0x6B / 255, blue: 0x6B / 255)
    static let success = Color(red: 0x4A / 255, green: 0xDE / 255, blue: 0x80 / 255)
    static let warning = Color(red: 0xFB / 255, green: 0xBF / 255, blue: 0x24 / 255)
    static let info = Color(red: 0x60 / 255, green: 0xA5 / 255, blue: 0xFA / 255)
    static let line = Color.white.opacity(0.08)

    /// 页面水平边距（Web 手机端 px-4 ≈ 16pt，标题区更宽）
    static let pagePadding: CGFloat = 16
    /// 海报圆角
    static let posterRadius: CGFloat = 12
    /// 卡片圆角
    static let cardRadius: CGFloat = 16
    /// 海报默认宽高比 2:3
    static let posterAspect: CGFloat = 2.0 / 3.0
}

extension View {
    /// 页面统一背景（银玻璃主题的「底」）：用户选定的背景图 + 模糊压暗蒙版，铺满安全区，
    /// 外观页换图 / 调质感后全 App 即时跟随（见 AppBackdropStore）。
    /// 同时把页内 List / Form 的系统底色隐藏，让背景透出来（Web 设置等页面的行直接铺在蒙版上）。
    /// - Parameter style: 默认 `.scrim`；影片详情类氛围页传 `.plain`，登录页传 `.sharp`
    func appBackground(_ style: AppBackdropStyle = .scrim) -> some View {
        scrollContentBackground(.hidden)
            .background { AppBackdropView(style: style) }
    }

    /// 卡片底：半透明抬升面 + 细描边
    func cardStyle(radius: CGFloat = Theme.cardRadius) -> some View {
        background(Theme.surfaceRaised, in: .rect(cornerRadius: radius))
            .overlay(RoundedRectangle(cornerRadius: radius).strokeBorder(Theme.line))
    }
}

/// 格式化工具（与 Web 端展示口径一致）
enum Formatters {
    /// 字节 → 「19.40 GB」
    static func bytes(_ value: Int?) -> String {
        guard let value else { return "—" }
        return ByteCountFormatter.string(fromByteCount: Int64(value), countStyle: .binary)
            .replacingOccurrences(of: "GiB", with: "GB").replacingOccurrences(of: "MiB", with: "MB")
            .replacingOccurrences(of: "KiB", with: "KB").replacingOccurrences(of: "TiB", with: "TB")
    }

    /// 秒 → 「1:52:18」/「53:55」
    static func clock(_ seconds: Double?) -> String {
        guard let seconds, seconds.isFinite, seconds >= 0 else { return "--:--" }
        let total = Int(seconds.rounded(.down))
        let h = total / 3600, m = (total % 3600) / 60, s = total % 60
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
    }

    /// 分钟 → 「1 小时 52 分」
    static func duration(minutes: Int?) -> String {
        guard let minutes, minutes > 0 else { return "" }
        let h = minutes / 60, m = minutes % 60
        if h == 0 { return "\(m) 分钟" }
        return m == 0 ? "\(h) 小时" : "\(h) 小时 \(m) 分"
    }

    private static let isoWithFraction: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    private static let iso = ISO8601DateFormatter()

    /// 解析后端时间字符串（ISO8601，可能不带时区——后端库内是 UTC）
    static func date(_ raw: String?) -> Date? {
        guard let raw, !raw.isEmpty else { return nil }
        if let d = isoWithFraction.date(from: raw) ?? iso.date(from: raw) { return d }
        // 无时区标记按 UTC 解析
        let withZ = raw.hasSuffix("Z") ? raw : raw + "Z"
        return isoWithFraction.date(from: withZ) ?? iso.date(from: withZ)
    }

    /// 相对时间：「几秒前」「3 分钟前」「18 天前」「2 个月前」；空值返回空串（调用方各自给占位）。
    /// 口径即 Web `formatRelativeTime`（lib/time.ts → dayjs zh-cn `fromNow()`），见 `fromNow(_:now:)`。
    static func relative(_ raw: String?) -> String {
        guard let date = date(raw) else { return "" }
        return fromNow(date)
    }

    /// 全 App 唯一的「多久之前」算法，逐条复刻 dayjs relativeTime 插件（默认阈值 + zh-cn 措辞）：
    /// 每一档先把差值换算成该档单位并**四舍五入**，再与阈值比较——
    /// ≤44 秒「几秒」、≤89 秒「1 分钟」、≤44 分「N 分钟」、≤89 分「1 小时」、≤21 时「N 小时」、
    /// ≤35 时「1 天」、≤25 天「N 天」、≤45 天「1 个月」、≤10 月「N 个月」、≤17 月「1 年」、再往后「N 年」；
    /// 过去加「前」，将来加「内」（zh-cn 的 future 是「%s内」）。
    /// 不用系统 RelativeDateTimeFormatter：它会出「1周前」「2周前」，与网页「11 天前」对不上。
    /// 设备/令牌「最近活跃」用的是 Web 另一套分钟粒度口径（SettingsTime.deviceRelative），不走这里。
    static func fromNow(_ date: Date, now: Date = .now) -> String {
        let delta = now.timeIntervalSince(date)
        let seconds = abs(delta)
        // JS Math.round：正数 .5 进位，与 Swift 的 toNearestOrAwayFromZero 一致
        func round(_ value: Double) -> Int { Int(value.rounded()) }
        let text: String
        if round(seconds) <= 44 {
            text = "几秒"
        } else if round(seconds) <= 89 {
            text = "1 分钟"
        } else if round(seconds / 60) <= 44 {
            text = "\(max(1, round(seconds / 60))) 分钟"
        } else if round(seconds / 60) <= 89 {
            text = "1 小时"
        } else if round(seconds / 3600) <= 21 {
            text = "\(max(1, round(seconds / 3600))) 小时"
        } else if round(seconds / 3600) <= 35 {
            text = "1 天"
        } else if round(seconds / 86400) <= 25 {
            text = "\(max(1, round(seconds / 86400))) 天"
        } else if round(seconds / 86400) <= 45 {
            text = "1 个月"
        } else {
            let months = monthDiff(from: min(date, now), to: max(date, now))
            if round(months) <= 10 {
                text = "\(max(1, round(months))) 个月"
            } else if round(months) <= 17 {
                text = "1 年"
            } else {
                text = "\(max(1, round(months / 12))) 年"
            }
        }
        return delta >= 0 ? "\(text)前" : "\(text)内"
    }

    /// 两个时刻相差的月数（带小数，同 dayjs monthDiff：整月数 + 余下部分占下一个整月的比例）
    private static func monthDiff(from start: Date, to end: Date) -> Double {
        let calendar = Calendar.current
        let whole = calendar.dateComponents([.month], from: start, to: end).month ?? 0
        guard let anchor = calendar.date(byAdding: .month, value: whole, to: start),
              let next = calendar.date(byAdding: .month, value: whole + 1, to: start),
              next > anchor
        else { return Double(whole) }
        return Double(whole) + end.timeIntervalSince(anchor) / next.timeIntervalSince(anchor)
    }

    /// 「2026-09-25 17:03」
    static func dateTime(_ raw: String?) -> String {
        guard let date = date(raw) else { return raw ?? "" }
        return date.formatted(.dateTime.year().month(.twoDigits).day(.twoDigits).hour().minute().locale(Locale(identifier: "zh_CN")))
    }
}
