import Foundation

/// 发现模块的纯数据规则（与 Web `lib/discovery-filters.ts`、`lib/api/discover.ts` 逐条对应）。

/// 组合发现的六维筛选（TMDB 专属）
struct DiscoveryFilters: Hashable {
    var genreIds: [Int] = []
    var originCountry: String?
    var year: Int?
    var ratingGte: Double?
    var runtimeLte: Int?
    var sort: String = "popular"

    static let empty = DiscoveryFilters()

    /// 已启用的维度数（顶栏筛选键的角标）
    var activeCount: Int {
        (genreIds.isEmpty ? 0 : 1) + (originCountry == nil ? 0 : 1) + (year == nil ? 0 : 1)
            + (ratingGte == nil ? 0 : 1) + (runtimeLte == nil ? 0 : 1) + (sort == "popular" ? 0 : 1)
    }

    /// 当前筛选的可读标签（结果页 chips）；类型名由后端本地化清单补齐
    func labels(genreNames: [Int: String]) -> [String] {
        var labels = genreIds.compactMap { genreNames[$0] }
        if !genreIds.isEmpty, labels.isEmpty { labels.append("\(genreIds.count) 个类型") }
        if let originCountry { labels.append(Self.countryLabel(originCountry)) }
        if let year { labels.append("\(year) 年") }
        if let ratingGte { labels.append("\(Self.format(ratingGte)) 分以上") }
        if let runtimeLte { labels.append("\(runtimeLte) 分钟以内") }
        if sort != "popular" { labels.append(Self.sortLabel(sort)) }
        return labels
    }

    static let countries: [(code: String, name: String)] = [
        ("CN", "中国大陆"), ("US", "美国"), ("JP", "日本"), ("KR", "韩国"), ("GB", "英国"),
        ("FR", "法国"), ("HK", "中国香港"), ("TW", "中国台湾"), ("IN", "印度"),
    ]

    static let sorts: [(value: String, label: String)] = [
        ("popular", "热门优先"), ("rating", "评分优先"), ("newest", "最新优先"), ("most-rated", "最多评分"),
    ]

    static func countryLabel(_ code: String) -> String {
        countries.first { $0.code == code }?.name ?? code
    }

    static func sortLabel(_ value: String) -> String {
        sorts.first { $0.value == value }?.label ?? value
    }

    static func format(_ value: Double) -> String {
        value == value.rounded() ? String(Int(value)) : String(value)
    }
}

extension DiscoveryFilters {
    /// 从站内链接的查询参数恢复筛选（同 Web `parseDiscoveryFilters`）：
    /// 无效或过期的值安全忽略，不让分享链接破坏页面。
    init(query: [String: String]) {
        self.init()
        if let sort = query["sort"], Self.sorts.contains(where: { $0.value == sort }) { self.sort = sort }
        var seen = Set<Int>()
        genreIds = (query["genres"] ?? "").split(separator: ",").compactMap { Int($0) }.filter { $0 > 0 && seen.insert($0).inserted }
        if let country = query["country"]?.uppercased(), country.count == 2, country.allSatisfy({ $0.isASCII && $0.isLetter }) {
            originCountry = country
        }
        func number(_ key: String, _ range: ClosedRange<Double>) -> Double? {
            guard let raw = query[key], let value = Double(raw), value.isFinite, range.contains(value) else { return nil }
            return value
        }
        if let year = number("year", 1874 ... 2100), year == year.rounded() { self.year = Int(year) }
        ratingGte = number("rating", 0 ... 10)
        if let runtime = number("runtime", 1 ... 600), runtime == runtime.rounded() { runtimeLte = Int(runtime) }
    }
}

/// 发现页视角：类型 × 数据源 × 筛选，与 Web 地址 `/discover/{type}?source=&genres=…` 一一对应。
///
/// Web 上地址是视角的唯一状态源：切类型、切数据源都跳到不带筛选的新地址（筛选随之清空），
/// 站内链接也能完整恢复视角。原生没有地址栏，由路由切到发现标签时把参数交给 `DiscoverView`：
/// 参数可以只是类型（`tv`），也可以带上查询串（`tv?source=douban&genres=18`）。
struct DiscoverViewpoint: Equatable {
    var mediaType: String
    var source: String
    var filters: DiscoveryFilters

    init(parameter: String) {
        let parts = parameter.split(separator: "?", maxSplits: 1).map(String.init)
        mediaType = parts.first == "tv" ? "tv" : "movie"
        var query: [String: String] = [:]
        if parts.count > 1 {
            for item in URLComponents(string: "?\(parts[1])")?.queryItems ?? [] { query[item.name] = item.value ?? "" }
        }
        // 未知数据源安全回退到默认 TMDB 视角；筛选只对 TMDB 生效
        source = query["source"] == "douban" ? "douban" : "tmdb"
        filters = source == "tmdb" ? DiscoveryFilters(query: query) : .empty
    }
}

/// 院线地区（ISO 3166-1，与后端 TMDB region 参数一致）
enum DiscoverRegions {
    static let all: [(code: String, name: String)] = [
        ("CN", "中国大陆"), ("HK", "香港"), ("TW", "台湾"), ("US", "美国"),
        ("JP", "日本"), ("KR", "韩国"), ("GB", "英国"),
    ]

    static func name(_ code: String) -> String {
        all.first { $0.code == code }?.name ?? code
    }
}

/// 片单引用 `provider:type:id` 的拆解
struct CollectionRef {
    let provider: String
    let mediaType: String
    let collectionId: String

    init?(_ raw: String) {
        let parts = raw.split(separator: ":", omittingEmptySubsequences: false).map(String.init)
        guard parts.count >= 3, ["tmdb", "douban"].contains(parts[0]), ["movie", "tv"].contains(parts[1]) else { return nil }
        let id = parts[2...].joined(separator: ":")
        guard !id.isEmpty else { return nil }
        provider = parts[0]
        mediaType = parts[1]
        collectionId = id
    }

    /// 「查看完整榜单」的路由
    var route: AppRoute { .discoverCollection(kind: mediaType, provider: provider, collectionId: collectionId) }
}

/// 影视引用 `tmdb:movie:550` / `douban:1292052` 的拆解（详情页订阅状态查询与外链用）
struct TitleRefParts {
    let source: String
    let mediaType: String?
    let externalId: String

    init(_ raw: String) {
        let parts = raw.split(separator: ":").map(String.init)
        if parts.first == "douban" {
            source = "douban"
            // douban:tv:123 与 douban:123 两种写法都认
            mediaType = parts.count >= 3 ? parts[1] : nil
            externalId = parts.last ?? ""
        } else {
            source = "tmdb"
            mediaType = parts.count >= 3 ? parts[1] : nil
            externalId = parts.last ?? ""
        }
    }
}

extension APIError {
    /// 后端统一错误码（UPSTREAM_UNREACHABLE = 网络级不可达，发现页据此给「前往网络设置」）
    var discoverErrorCode: String? {
        if case let .http(_, _, code) = self { return code }
        return nil
    }
}

extension Error {
    /// 上游数据源（TMDB/豆瓣）不可达：发现页错误态给出网络设置入口
    var isUpstreamUnreachable: Bool {
        (self as? APIError)?.discoverErrorCode == "UPSTREAM_UNREACHABLE"
    }

    var isDiscoverNotFound: Bool {
        (self as? APIError)?.status == 404
    }
}
