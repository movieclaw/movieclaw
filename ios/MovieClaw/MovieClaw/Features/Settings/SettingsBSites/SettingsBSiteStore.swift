import SwiftUI

/// 「站点接入」页签的数据源（对应 Web `SiteConfigSection` 里的四份状态与 upsert 逻辑）。
///
/// 设计要点：
/// - 首载并行拉「目录 / 已接入 / 同步统计 / 刷流统计」四份（同 Web `load`），任何一份失败整页报错；
/// - 轮询分两档由页面驱动：有站点在验证中时 2.5 秒只刷已接入列表；有站点开着刷流时 30 秒刷两份统计
///   （都是后端本地聚合查询，不触达站点）；
/// - 所有写操作（启停/保护/刷流/重验/编辑授权）后端都回传最新的站点对象，**原地替换**不重新排序，
///   避免被操作的站点在列表里跳位；新增的站点追加到末尾；
/// - 行级 busy 用 `busySites` 记录：操作进行中该站的菜单整体禁用，防止连点发出重复写请求。
@Observable
final class SettingsBSiteStore {
    private(set) var catalog: [API.CatalogItem] = []
    private(set) var configured: [API.ConfiguredSite] = []
    private(set) var syncStats: [String: API.SiteSyncStatsView] = [:]
    private(set) var boostStats: [String: API.SiteBoostStatsView] = [:]
    private(set) var loading = true
    private(set) var loadError: String?
    private(set) var busySites: Set<String> = []

    /// 目录里还没接入的站点（「添加站点」只列这些）
    var available: [API.CatalogItem] {
        let ids = Set(configured.map(\.siteId))
        return catalog.filter { !ids.contains($0.siteId) }
    }

    /// 异常站点置顶（稳定排序：失败之间、正常之间都保持后端顺序）
    var ordered: [API.ConfiguredSite] {
        configured.filter { $0.status == "failed" } + configured.filter { $0.status != "failed" }
    }

    var hasInProgress: Bool { configured.contains { SettingsBSiteText.inProgress($0.status) } }
    var anyBoosting: Bool { configured.contains(where: \.boostEnabled) }
    var failedCount: Int { configured.filter { $0.status == "failed" }.count }

    /// 目录里查展示信息；目录缺失（站点已下架但仍有历史配置）时兜底用 site_id
    func item(for siteId: String) -> API.CatalogItem {
        catalog.first { $0.siteId == siteId }
            ?? API.CatalogItem(siteId: siteId, displayName: siteId, baseUrl: "", supportedAuthTypes: [])
    }

    func load(_ api: APIClient) async {
        do {
            async let cat = api.siteCatalog()
            async let cfg = api.siteList()
            async let sync = api.siteStats()
            async let boost = api.siteBoostStats()
            let (c, s, st, b) = try await (cat, cfg, sync, boost)
            catalog = c
            configured = s
            syncStats = st
            boostStats = b
            loadError = nil
        } catch is CancellationError {
        } catch {
            loadError = error.localizedDescription
        }
        loading = false
    }

    /// 验证进度轮询：只刷已接入列表，失败静默（下一轮重试）
    func refreshConfigured(_ api: APIClient) async {
        if let list = try? await api.siteList() { configured = list }
    }

    /// 刷流运行期轮询：刷流统计 + 索引同步节奏，失败静默
    func refreshStats(_ api: APIClient) async {
        async let boost = try? api.siteBoostStats()
        async let sync = try? api.siteStats()
        if let b = await boost { boostStats = b }
        if let s = await sync { syncStats = s }
    }

    func upsert(_ site: API.ConfiguredSite) {
        if let idx = configured.firstIndex(where: { $0.siteId == site.siteId }) {
            configured[idx] = site
        } else {
            configured.append(site)
        }
    }

    func remove(_ siteId: String) {
        configured.removeAll { $0.siteId == siteId }
    }

    /// 行级写操作的统一包装：标记 busy → 执行 → 回写最新站点对象；失败抛给调用方弹 Toast
    func mutate(_ siteId: String, _ op: () async throws -> API.ConfiguredSite) async throws {
        busySites.insert(siteId)
        defer { busySites.remove(siteId) }
        upsert(try await op())
    }

    func withBusy(_ siteId: String, _ op: () async throws -> Void) async throws {
        busySites.insert(siteId)
        defer { busySites.remove(siteId) }
        try await op()
    }
}
