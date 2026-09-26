import Foundation
import SwiftUI
import Testing
@testable import MovieClaw

/// 外壳与通用口径的纯逻辑（对等修补 R-3 / R-4 / S-4 / SA-3 / SBB-11）：
/// 相对时间与 dayjs `fromNow()` 逐档一致、整数插值不加千分位、路由守卫改道、
/// 下载器映射建议的覆盖判定、设置分区深链的查询参数透传。
@MainActor
struct ShellLogicTests {
    private let now = Date(timeIntervalSince1970: 1_790_000_000)

    private func ago(_ seconds: Double) -> String {
        Formatters.fromNow(now.addingTimeInterval(-seconds), now: now)
    }

    /// dayjs relativeTime 默认阈值：每档先四舍五入再比较（数值取自 dayjs 1.11 源码的阈值表）
    @Test func relativeTimeMatchesDayjsThresholds() {
        #expect(ago(10) == "几秒前")
        #expect(ago(44) == "几秒前")
        #expect(ago(45) == "1 分钟前")
        #expect(ago(89) == "1 分钟前")
        #expect(ago(90) == "2 分钟前")
        #expect(ago(44 * 60) == "44 分钟前")
        #expect(ago(45 * 60) == "1 小时前")
        #expect(ago(89 * 60) == "1 小时前")
        #expect(ago(107 * 60) == "2 小时前") // 1h47m：dayjs 四舍五入是 2 小时
        #expect(ago(21 * 3600) == "21 小时前")
        #expect(ago(22 * 3600) == "1 天前")
        #expect(ago(35 * 3600) == "1 天前")
        #expect(ago(36 * 3600) == "2 天前")
        #expect(ago(11 * 86400) == "11 天前") // 系统格式器会写成「1周前」
        #expect(ago(25 * 86400) == "25 天前")
        #expect(ago(26 * 86400) == "1 个月前")
        #expect(ago(45 * 86400) == "1 个月前")
        #expect(ago(120 * 86400) == "4 个月前")
        #expect(ago(400 * 86400) == "1 年前")
        #expect(ago(800 * 86400) == "2 年前")
        // 将来时刻用 zh-cn 的「%s内」
        #expect(Formatters.fromNow(now.addingTimeInterval(3 * 86400), now: now) == "3 天内")
    }

    @Test func legacyRelativeHelpersShareOneAlgorithm() {
        #expect(libraryFromNow(nil) == "从未")
        #expect(SubsFormat.relative(nil) == "从未")
        #expect(Formatters.relative(nil) == "")
    }

    /// Text 字面量里的整数与网页一样原样显示（「10481 个文件」而不是「10,481 个文件」）
    @Test func integerInterpolationHasNoGrouping() {
        var interpolation = LocalizedStringKey.StringInterpolation(literalCapacity: 8, interpolationCount: 1)
        interpolation.appendLiteral("共 ")
        interpolation.appendInterpolation(10481)
        interpolation.appendLiteral(" 个文件")
        let key = LocalizedStringKey(stringInterpolation: interpolation)
        // LocalizedStringKey 不能直接取字符串，借 Mirror 看它记下的格式与参数：整数应当以字符串参数（%@）进入
        let described = String(describing: key)
        #expect(!described.contains("%lld"))
        #expect(described.contains("10481"))
    }

    /// 路由守卫：成员越权的设置分区改去个人信息，其余越权页落媒体库
    @Test func routerGuardRedirectsMembers() {
        let router = Router()
        // 权限未同步前不拦（启动深链抢在权限同步之前）
        #expect(router.guarded(.settingsSection(.sites)) == .settingsSection(.sites))
        router.permissions = Permissions.none
        #expect(router.guarded(.settingsSection(.sites)) == .settingsSection(.profile))
        #expect(router.guarded(.settingsSection(.appearance)) == .settingsSection(.appearance))
        #expect(router.guarded(.session(id: "x")) == .libraryHome)
        #expect(router.guarded(.activity()) == .libraryHome)
        #expect(router.guarded(.discover()) == .discover())
    }

    /// 映射建议：已被某条映射的本机侧覆盖（相同或子目录）就不再追加（Web withSuggestedMapping）
    @Test func suggestedMappingSkipsCoveredPaths() {
        let existing = [SettingsBDlMappingDraft(local: "/data/downloads/", remote: "/downloads")]
        #expect(SettingsBDlEditorSheet.withSuggestedMapping(existing, "/data/downloads/movies").count == 1)
        #expect(SettingsBDlEditorSheet.withSuggestedMapping(existing, "/data/downloads").count == 1)
        let added = SettingsBDlEditorSheet.withSuggestedMapping(existing, "/volume1/media")
        #expect(added.count == 2)
        #expect(added.last?.local == "/volume1/media")
        #expect(added.last?.remote == "")
        // 根目录映射不算覆盖
        let root = [SettingsBDlMappingDraft(local: "/", remote: "/")]
        #expect(SettingsBDlEditorSheet.withSuggestedMapping(root, "/x").count == 2)
    }

    /// 设置分区深链：查询参数原样透传（分区页经 routeQuery 读取），旧地址按 Web 重定向
    @Test func settingsDeepLinksKeepQuery() {
        #expect(AppRoute(webPath: "/settings/downloaders?limits=3") == .settingsSection(.downloaders, query: ["limits": "3"]))
        #expect(AppRoute(webPath: "/settings/import-watch?suggest=auto&kinds=movie,tv")
            == .settingsSection(.importWatch, query: ["suggest": "auto", "kinds": "movie,tv"]))
        #expect(AppRoute(webPath: "/settings/app?tab=storage") == .settingsSection(.app, query: ["tab": "storage"]))
        #expect(AppRoute(webPath: "/settings/app?tab=remote") == .settingsSection(.playback))
        // Web 把旧「搜索」分区重定向到 /settings/sites（不带页签）
        #expect(AppRoute(webPath: "/settings/search") == .settingsSection(.sites))
    }
}
