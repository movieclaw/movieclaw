import Foundation

/// 海报墙筛选条件（对应网页 `lib/library-filter.ts` 的 LibraryFilter；设计见
/// docs/design/library-filtering.md 3.1）。空 = 不筛选。
///
/// 语义：**维内 OR、维间 AND**。十个维度分两级——
/// - 一级（找片的四个常用问题）：类型 / 年代 / 地区 / 观看；
/// - 二级·找片（作品是什么样的，来自刮削档案）：评分 / 片长 / 原始语言；
/// - 二级·查库（文件是什么规格，来自库存台账）：分辨率 / 动态范围 / 库存状态。
///
/// 另有 `seriesKeys`（作品系列键）：界面上**没有这一维的控件**（一个库几百个系列，
/// 合集才是它正确的呈现形态），只为系列合集与深链服务，所以不计入角标、不画成条件 chip。
///
/// 为什么筛选不持久化：上次筛的条件下次打开还在，是这类产品最经典的困惑来源
/// （"我的片怎么少了一半"）。排序是偏好该记，筛选是意图不该记——由持有它的页面
/// 用 `@State` 保存即可。
///
/// 维度在代码里统一用**后端的维度名**（genres / countries / decades / watch / rating_gte /
/// runtimes / languages / resolutions / hdr / stock）：放宽建议接口回的 `dim` 就是这套名字，
/// 条件 chip 与放宽建议因此能共用同一套增删逻辑，不必各写一份映射。
nonisolated struct LibraryFilter: Hashable, Codable, Sendable {
    /// 类型：TMDB genre id（语言无关）
    var genres: [Int] = []
    /// 地区：ISO 3166-1 二字码（大写）
    var countries: [String] = []
    /// 年代档：2020s / 2010s / 2000s / 1990s / earlier
    var decades: [String] = []
    /// 观看状态（单选）：unwatched / watching / played / favorite（seen 只给首页「最近观看」用）
    var watch: String?
    /// 评分下限（阈值不是集合：单值）
    var ratingGte: Double?
    /// 片长档：lte60 / 60to90 / 90to120 / gt120
    var runtimes: [String] = []
    /// 原始语言码（小写）
    var languages: [String] = []
    /// 分辨率：2160p / 1080p / …
    var resolutions: [String] = []
    /// true = 只看 HDR / false = 只看 SDR / nil = 都看
    var hdr: Bool?
    /// 库存状态：missing = 有文件失联 / unscraped = 没刮到档案
    var stock: [String] = []
    /// 作品系列键（tmdb:1241 / name:xxx）；无界面控件，见类型注释
    var seriesKeys: [String] = []

    /// 维度的显示顺序与叫法（条件 chip 行用）。**十个维度一个都不能少**：条件行的职责是
    /// 「收起面板之后仍看得见自己筛了什么」，漏画一维，用户就不知道那一维怎么取消。
    static let dimensions: [(key: String, label: String)] = [
        ("genres", "类型"),
        ("decades", "年代"),
        ("countries", "地区"),
        ("watch", "观看"),
        ("rating_gte", "评分"),
        ("runtimes", "片长"),
        ("languages", "语言"),
        ("resolutions", "画质"),
        ("hdr", "动态范围"),
        ("stock", "库存"),
    ]

    /// 是否为空——为空时一切按「未筛选」走（不显示条件行、不请求计数）。
    var isEmpty: Bool {
        genres.isEmpty && countries.isEmpty && decades.isEmpty && watch == nil
            && ratingGte == nil && runtimes.isEmpty && languages.isEmpty
            && resolutions.isEmpty && hdr == nil && stock.isEmpty && seriesKeys.isEmpty
    }

    /// 已选条件的条数（「筛选」按钮上的角标）。与网页 filterCount 同口径：系列键不计。
    var count: Int {
        genres.count + countries.count + decades.count + (watch == nil ? 0 : 1)
            + (ratingGte == nil ? 0 : 1) + runtimes.count + languages.count
            + resolutions.count + (hdr == nil ? 0 : 1) + stock.count
    }

    /// 条件里有没有二级维度——决定要不要取全份 facet：二级维度的展示名（"gt120" → "> 120′"）
    /// 只在 tier=all 里才有。点开的合集、深链进来的条件都可能带二级维度，那时面板是关的，
    /// 条件行也得有展示名可印。
    var hasSecondary: Bool {
        ratingGte != nil || !runtimes.isEmpty || !languages.isEmpty
            || !resolutions.isEmpty || hdr != nil || !stock.isEmpty
    }
}

// MARK: - 按维度读写（条件 chip、筛选面板、放宽建议共用）

nonisolated extension LibraryFilter {
    /// 某一维已选的取值，统一成字符串——与 facet 的 value 同一种写法：
    /// 类型是 "878"、评分是 "8"（整数不带 .0）、动态范围是 "1"/"0"。
    func values(of dim: String) -> [String] {
        switch dim {
        case "genres": genres.map(String.init)
        case "countries": countries
        case "decades": decades
        case "watch": watch.map { [$0] } ?? []
        case "rating_gte", "ratingGte": ratingGte.map { [Self.ratingString($0)] } ?? []
        case "runtimes": runtimes
        case "languages": languages
        case "resolutions": resolutions
        case "hdr": hdr.map { [$0 ? "1" : "0"] } ?? []
        case "stock": stock
        case "series_keys", "series_key": seriesKeys
        default: []
        }
    }

    /// 勾选 / 取消一个取值（筛选面板的胶囊）。
    /// - 多值维度：有则去掉、无则追加；
    /// - 观看、评分、动态范围是单值：再点一次同一个值就是取消。
    mutating func toggle(_ dim: String, _ value: String) {
        switch dim {
        case "watch":
            watch = watch == value ? nil : value
        case "rating_gte", "ratingGte":
            let next = Double(value)
            ratingGte = ratingGte == next ? nil : next
        case "hdr":
            let next = value == "1"
            hdr = hdr == next ? nil : next
        default:
            if values(of: dim).contains(value) {
                remove(dim, value)
            } else {
                setList(dim, values(of: dim) + [value])
            }
        }
    }

    /// 摘掉一个取值（条件 chip 的 ✕、放宽建议）。单值维度直接清空（放宽建议里 hdr 的取值是
    /// Python 的 "True"/"False"、评分是 "9.0"，与界面写法不同，所以单值维度不比较取值）。
    mutating func remove(_ dim: String, _ value: String) {
        switch dim {
        case "watch": watch = nil
        case "rating_gte", "ratingGte": ratingGte = nil
        case "hdr": hdr = nil
        default: setList(dim, values(of: dim).filter { $0 != value })
        }
    }

    private mutating func setList(_ dim: String, _ list: [String]) {
        switch dim {
        case "genres": genres = list.compactMap { Int($0) }
        case "countries": countries = list
        case "decades": decades = list
        case "runtimes": runtimes = list
        case "languages": languages = list
        case "resolutions": resolutions = list
        case "stock": stock = list
        case "series_keys", "series_key": seriesKeys = list
        default: break
        }
    }

    /// 评分的字符串写法：8 → "8"、8.5 → "8.5"（与 facet value、网页 String(number) 一致）
    static func ratingString(_ value: Double) -> String {
        value.rounded() == value && abs(value) < 1e9 ? String(Int(value)) : String(value)
    }
}

// MARK: - 规范化键（判断「当前这面墙是否正好等于某个合集」）

nonisolated extension LibraryFilter {
    /// 条件的规范化键：同一组条件无论勾选顺序都得到同一个串，空条件为空串（移植网页 filterKey）。
    ///
    /// 合集 chip 的选中态就是拿它**推导**出来的，不存状态：用户改动任意一条，键不再相等，
    /// 「＝ 合集」标记自然消失，不需要谁记得去清掉一个 activeCollection。
    var key: String {
        if isEmpty { return "" }
        let parts: [(String, [String])] = [
            ("c", countries), ("d", decades), ("g", genres.map(String.init)),
            ("hdr", hdr.map { [String($0)] } ?? []), ("lang", languages),
            ("r", ratingGte.map { [Self.ratingString($0)] } ?? []), ("res", resolutions),
            ("rt", runtimes), ("sk", seriesKeys), ("st", stock), ("w", watch.map { [$0] } ?? []),
        ]
        return parts
            .filter { !$0.1.isEmpty }
            .map { $0.0 + $0.1.sorted().joined(separator: "_") }
            .joined(separator: ".")
    }
}

// MARK: - 合集规则互转

nonisolated extension LibraryFilter {
    /// 合集规则 → 筛选条件（移植网页 rulesToFilter，字段名与后端 collections.rules_to_filter 一一对上）。
    /// 未知字段保守忽略：与后端同一条降级策略（宁可少收窄也不误收窄）。
    init(rules: [API.JSONValue]) {
        self.init()
        for rule in rules {
            guard let field = rule["field"]?.stringValue else { continue }
            let values = rule["values"]?.arrayValue ?? []
            let strings = values.compactMap(\.stringValue)
            switch field {
            case "genres": genres = values.compactMap(\.intValue)
            case "origin_countries", "countries": countries = strings.map { $0.uppercased() }
            case "decades": decades = strings
            case "watch": watch = strings.first
            case "rating_gte": ratingGte = values.first?.doubleValue
            case "runtimes": runtimes = strings
            case "languages": languages = strings.map { $0.lowercased() }
            case "resolutions": resolutions = strings
            case "hdr": hdr = values.first.map { $0.boolValue ?? ($0.stringValue == "true") }
            case "stock": stock = strings
            case "series_key": seriesKeys = strings
            default: break
            }
        }
    }

    /// 筛选条件 → 合集规则（「筛完存为合集」是一次纯粹的形状转换，不是另一套语义）。
    /// 字段名对不上时后端会保守忽略，表现是"存下来的合集比刚才筛出来的多"且没有报错，
    /// 所以字段名必须与网页 filterToRules 完全一致。
    var rules: [API.JSONValue] {
        var rules: [API.JSONValue] = []
        func push(_ field: String, _ values: [API.JSONValue]) {
            guard !values.isEmpty else { return }
            rules.append(.object(["field": .string(field), "op": .string("any_of"), "values": .array(values)]))
        }
        push("genres", genres.map { .int($0) })
        push("origin_countries", countries.map { .string($0) })
        push("decades", decades.map { .string($0) })
        push("watch", watch.map { [.string($0)] } ?? [])
        push("rating_gte", ratingGte.map { [.double($0)] } ?? [])
        push("runtimes", runtimes.map { .string($0) })
        push("languages", languages.map { .string($0) })
        push("resolutions", resolutions.map { .string($0) })
        push("hdr", hdr.map { [.bool($0)] } ?? [])
        push("stock", stock.map { .string($0) })
        push("series_key", seriesKeys.map { .string($0) })
        return rules
    }
}

// MARK: - 查询串（深链 / 与网页 URL 同一套参数名）

nonisolated extension LibraryFilter {
    /// 从查询参数还原（参数名同网页 URL 与后端：g / c / d / w / rating_gte / rt / lang / res / hdr / stock / series_keys）。
    /// 一个筛选参数都没有时返回 nil。解析不出的取值静默丢弃（与后端宽容语义一致，老链接不报错）。
    init?(query: [String: String]) {
        func list(_ key: String) -> [String] {
            (query[key] ?? "").split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        }
        self.init(
            genres: list("g").compactMap { Int($0) },
            countries: list("c").map { $0.uppercased() },
            decades: list("d"),
            watch: query["w"].flatMap { $0.isEmpty ? nil : $0 },
            ratingGte: query["rating_gte"].flatMap { Double($0) },
            runtimes: list("rt"),
            languages: list("lang").map { $0.lowercased() },
            resolutions: list("res"),
            hdr: query["hdr"].flatMap { $0 == "true" ? true : $0 == "false" ? false : nil },
            stock: list("stock"),
            seriesKeys: list("series_keys")
        )
        if isEmpty { return nil }
    }

    /// 条件 → 查询参数（深链用；接口请求走下面的 APIClient 扩展）。
    var queryItems: [URLQueryItem] {
        let pairs: [(String, String?)] = [
            ("g", qG), ("c", qC), ("d", qD), ("w", watch),
            ("rating_gte", ratingGte.map(Self.ratingString)), ("rt", qRt), ("lang", qLang),
            ("res", qRes), ("hdr", hdr.map { String($0) }), ("stock", qStock), ("series_keys", qSeries),
        ]
        return pairs.compactMap { name, value in value.map { URLQueryItem(name: name, value: $0) } }
    }

    // 逗号拼接的查询参数；空维度给 nil（生成的接口函数对 nil 不带该参数）
    fileprivate var qG: String? { Self.join(genres.map(String.init)) }
    fileprivate var qC: String? { Self.join(countries) }
    fileprivate var qD: String? { Self.join(decades) }
    fileprivate var qRt: String? { Self.join(runtimes) }
    fileprivate var qLang: String? { Self.join(languages) }
    fileprivate var qRes: String? { Self.join(resolutions) }
    fileprivate var qStock: String? { Self.join(stock) }
    fileprivate var qSeries: String? { Self.join(seriesKeys) }

    private static func join(_ values: [String]) -> String? {
        values.isEmpty ? nil : values.joined(separator: ",")
    }
}

// MARK: - 接口：把 filter 展开成查询参数

/// 墙、索引、图廊、计数、放宽建议**五个接口共用同一份条件展开**：
/// 「面板上显示多少部、点下去墙上就是多少部」由此在结构上得到保证（网页 filterQuery 同理）。
nonisolated extension APIClient {
    /// 海报墙条目（`GET /libraries/{id}/items`）。identity：confirmed（默认）/ provisional。
    func libraryItemsFiltered(
        libraryId: Int, filter: LibraryFilter, sort: String?, order: String?,
        limit: Int?, offset: Int?, identity: String? = nil
    ) async throws -> [API.LibraryItemView] {
        try await libraryItemsList(
            libraryId: libraryId, sort: sort, order: order, limit: limit, offset: offset, identity: identity,
            g: filter.qG, c: filter.qC, d: filter.qD, w: filter.watch, ratingGte: filter.ratingGte,
            rt: filter.qRt, lang: filter.qLang, res: filter.qRes, hdr: filter.hdr, stock: filter.qStock,
            seriesKeys: filter.qSeries
        )
    }

    /// 海报墙跳转索引（`GET /libraries/{id}/item-index`）；order 必须与取墙时同一个值，offset 才对得上。
    func libraryIndexFiltered(
        libraryId: Int, filter: LibraryFilter, sort: String?, order: String?
    ) async throws -> [API.LibraryIndexEntryView] {
        try await uiLibraryItemsIndex(
            libraryId: libraryId, sort: sort, order: order,
            g: filter.qG, c: filter.qC, d: filter.qD, w: filter.watch, ratingGte: filter.ratingGte,
            rt: filter.qRt, lang: filter.qLang, res: filter.qRes, hdr: filter.hdr, stock: filter.qStock,
            seriesKeys: filter.qSeries
        )
    }

    /// 图廊（`GET /libraries/{id}/gallery`）。
    func libraryGalleryFiltered(
        libraryId: Int, filter: LibraryFilter, sort: String?, order: String?, limit: Int?, offset: Int?
    ) async throws -> [API.LibraryGalleryGroupView] {
        try await uiLibraryGallery(
            libraryId: libraryId, limit: limit, offset: offset, sort: sort, order: order,
            g: filter.qG, c: filter.qC, d: filter.qD, w: filter.watch, ratingGte: filter.ratingGte,
            rt: filter.qRt, lang: filter.qLang, res: filter.qRes, hdr: filter.hdr, stock: filter.qStock,
            seriesKeys: filter.qSeries
        )
    }

    /// 筛选面板候选值与计数（`GET /libraries/{id}/facets`）。
    /// allTiers=false 只算一级四维；二级维度要多算十几条 COUNT，所以按需才取全份。
    func libraryFacetsFiltered(libraryId: Int, filter: LibraryFilter, allTiers: Bool) async throws -> API.LibraryFacetsView {
        try await libraryItemsFacets(
            libraryId: libraryId, tier: allTiers ? "all" : nil,
            g: filter.qG, c: filter.qC, d: filter.qD, w: filter.watch, ratingGte: filter.ratingGte,
            rt: filter.qRt, lang: filter.qLang, res: filter.qRes, hdr: filter.hdr, stock: filter.qStock,
            seriesKeys: filter.qSeries
        )
    }

    /// 筛空时的放宽建议（`GET /libraries/{id}/relax`）：服务端只返回救得回内容的条件。
    func libraryRelaxFiltered(libraryId: Int, filter: LibraryFilter) async throws -> API.LibraryRelaxView {
        try await libraryItemsRelax(
            libraryId: libraryId,
            g: filter.qG, c: filter.qC, d: filter.qD, w: filter.watch, ratingGte: filter.ratingGte,
            rt: filter.qRt, lang: filter.qLang, res: filter.qRes, hdr: filter.hdr, stock: filter.qStock,
            seriesKeys: filter.qSeries
        )
    }
}
