import SwiftUI

// 规则组的领域模型与「人话」翻译（对应 Web components/rule-sets-panel.tsx 的
// specSummary / upgradeTargetLabel / parseScope，以及 lib/upgrade-ladder.ts、lib/platforms.ts）。
//
// 后端把 spec 存成自由 JSON（生成模型里是 `[String: JSONValue]`），这里转成强类型的
// `RuleSetSpec` 供编辑器双向绑定；保存时只写用户真的设置过的键，未知键（经 API 写入的
// 预留字段、旧版兼容字段）原样带回，编辑一次不会把它们丢掉。
//
// 订阅弹层（选组时看摘要）、订阅详情（事实区「规则组 · 洗到 X」）、更换规则组、洗一轮版、
// 以及之后「设置 → 订阅规则」的规则组清单都复用这里，保证全 App 对同一个组只说一种话。

// MARK: - 词表（与后端 / Web 同值）

enum RuleSetVocabulary {
    /// 分辨率可选项（常用档）
    static let resolutions = ["2160p", "1080p", "720p"]

    /// 片源档（顺序即内置档序 T5…T1）
    static let mediaSources: [(value: String, label: String)] = [
        ("remux", "Remux"), ("blu-ray", "蓝光"), ("web-dl", "WEB-DL"), ("rip", "Rip 类"), ("tv", "电视录制类"),
    ]

    /// 编码按家族选择：一族的等价写法一并写进白名单
    static let codecFamilies: [(label: String, values: [String])] = [
        ("H.265", ["x265", "H.265", "HEVC"]),
        ("H.264", ["x264", "H.264", "AVC"]),
        ("AV1", ["AV1"]),
    ]

    /// HDR 值域；"SDR" 是哨兵 = 资源未标注任何 HDR 格式
    static let hdrOptions = ["DV", "HDR10+", "HDR10", "HLG", "SDR"]

    static let subtitleLanguages: [(value: String, label: String)] = [
        ("zh", "中文（不限简繁）"), ("zh-Hans", "简体中文"), ("zh-Hant", "繁体中文"), ("en", "英文"),
    ]

    static let audioLanguages: [(value: String, label: String)] = [
        ("cmn", "国语"), ("yue", "粤语"), ("ja", "日语"), ("en", "英语"),
    ]

    /// 洗版目标片源档（三个预设就是全部）
    static let upgradeOptions: [(value: String, label: String)] = [
        ("web-dl", "洗到 WEB-DL"), ("blu-ray", "洗到蓝光"), ("remux", "洗到 Remux"),
    ]
    static let upgradeSourceLabel = ["web-dl": "WEB-DL", "blu-ray": "蓝光", "remux": "Remux"]

    /// 参与洗版比较的维度（顺序即芯片顺序）
    static let ladderOptions: [(value: String, label: String)] = [
        ("resolution", "分辨率"), ("source", "片源"), ("hdr", "HDR"), ("video_codec", "编码"), ("platform", "平台"),
    ]
    static let defaultLadder = ["resolution", "source"]

    /// 未配置分辨率时的内置偏好序（后端 _DEFAULT_RESOLUTION_LADDER）
    static let defaultResolutionLadder = ["4320p", "2160p", "1440p", "1080p", "720p", "576p", "480p"]

    /// 编辑器常驻的平台选项
    static let platformOptions = [
        "netflix", "disney_plus", "amazon", "hbo_max", "apple_tv_plus", "hulu", "paramount_plus", "peacock",
        "iqiyi", "wetv", "youku", "mangotv", "bilibili", "viu", "crunchyroll", "hotstar",
    ]

    /// 平台规范值 → 展示名（与后端 PLATFORM 词表同值；表外值原样展示）
    static let platformLabels: [String: String] = [
        "netflix": "Netflix", "amazon": "Prime Video", "disney_plus": "Disney+", "hbo_max": "HBO Max",
        "hulu": "Hulu", "apple_tv_plus": "Apple TV+", "paramount_plus": "Paramount+", "peacock": "Peacock",
        "showtime": "Showtime", "starz": "Starz", "discovery_plus": "Discovery+", "crave": "Crave", "stan": "Stan",
        "roku": "Roku", "google_tv": "Google TV", "itunes": "iTunes", "sony_core": "Sony Pictures Core",
        "criterion": "Criterion", "iqiyi": "爱奇艺", "wetv": "腾讯视频", "youku": "优酷", "mangotv": "芒果 TV",
        "bilibili": "哔哩哔哩", "viu": "Viu", "nowplayer": "Now player", "mytv_super": "myTV SUPER",
        "hami_video": "Hami Video", "line_tv": "LINE TV", "kktv": "KKTV", "tving": "TVING", "wavve": "Wavve",
        "coupang_play": "Coupang Play", "kocowa": "KOCOWA", "viki": "Viki", "unext": "U-NEXT", "tver": "TVer",
        "fod": "FOD", "dmm_tv": "DMM TV", "hotstar": "Hotstar", "crunchyroll": "Crunchyroll", "hidive": "HIDIVE",
        "abema": "ABEMA", "adn": "ADN", "funimation": "Funimation", "vrv": "VRV", "wakanim": "Wakanim",
        "b_global": "B-Global",
    ]

    static func platformLabel(_ id: String) -> String { platformLabels[id] ?? id }
    static func mediaSourceLabel(_ value: String) -> String { mediaSources.first { $0.value == value }?.label ?? value }
}

// MARK: - spec 强类型

/// 规则组过滤条件（见后端 movieclaw_matcher.RuleSetSpec）：全部键可缺省 = 不限。
struct RuleSetSpec: Equatable {
    var resolutions: [String] = []
    var mediaSources: [String] = []
    var videoCodecs: [String] = []
    var platforms: [String] = []
    var platformsBlock: [String] = []
    var releaseGroupsAllow: [String] = []
    var releaseGroupsBlock: [String] = []
    var hdrLevels: [String] = []
    var hdrBlock: [String] = []
    /// [兼容层] 旧 HDR / DV 三态：新键为空时后端才消费
    var legacyHdr: String?
    var legacyDv: String?
    var freeOnly = false
    var minSeeders: Int?
    var sizeMinMb: Int?
    var sizeMaxMb: Int?
    var excludeHr = false
    var hrUnknownPolicy: String?
    var subtitleLanguages: [String] = []
    var audioLanguages: [String] = []
    /// 洗版目标片源档；nil = 不洗版
    var upgradeSource: String?
    var cutoffResolution: String?
    var upgradeKeepOld = false
    var upgradeLadder: [String]?
    /// [预留] 站点白名单：UI 不编辑，保存时原样带回
    var sites: [String] = []

    init() {}

    init(_ raw: [String: API.JSONValue]) {
        func strings(_ key: String) -> [String] { raw[key]?.arrayValue?.compactMap(\.stringValue) ?? [] }
        func int(_ key: String) -> Int? { raw[key].flatMap { $0.isNull ? nil : $0.intValue } }
        func string(_ key: String) -> String? { raw[key].flatMap { $0.isNull ? nil : $0.stringValue } }
        resolutions = strings("resolutions")
        mediaSources = strings("media_sources")
        videoCodecs = strings("video_codecs")
        platforms = strings("platforms")
        platformsBlock = strings("platforms_block")
        releaseGroupsAllow = strings("release_groups_allow")
        releaseGroupsBlock = strings("release_groups_block")
        hdrLevels = strings("hdr_levels")
        hdrBlock = strings("hdr_block")
        legacyHdr = string("hdr")
        legacyDv = string("dv")
        freeOnly = raw["free_only"]?.boolValue ?? false
        minSeeders = int("min_seeders")
        sizeMinMb = int("size_min_mb")
        sizeMaxMb = int("size_max_mb")
        excludeHr = raw["exclude_hr"]?.boolValue ?? false
        hrUnknownPolicy = string("hr_unknown_policy")
        subtitleLanguages = strings("subtitle_languages_require")
        audioLanguages = strings("audio_languages_require")
        upgradeSource = string("upgrade_source").flatMap { $0.isEmpty ? nil : $0 }
        cutoffResolution = string("cutoff_resolution").flatMap { $0.isEmpty ? nil : $0 }
        upgradeKeepOld = raw["upgrade_keep_old"]?.boolValue ?? false
        upgradeLadder = raw["upgrade_ladder"]?.arrayValue?.compactMap(\.stringValue)
        sites = strings("sites")
    }

    /// 保存用的 JSON：只写设置过的键（与 Web 编辑器 draft.spec 同一口径）
    var json: [String: API.JSONValue] {
        var out: [String: API.JSONValue] = [:]
        func put(_ key: String, _ values: [String]) { if !values.isEmpty { out[key] = .array(values.map { .string($0) }) } }
        put("resolutions", resolutions)
        put("media_sources", mediaSources)
        put("video_codecs", videoCodecs)
        if let upgradeLadder { put("upgrade_ladder", upgradeLadder) }
        put("platforms", platforms)
        put("platforms_block", platformsBlock)
        put("hdr_levels", hdrLevels)
        put("hdr_block", hdrBlock)
        put("subtitle_languages_require", subtitleLanguages)
        put("audio_languages_require", audioLanguages)
        if freeOnly { out["free_only"] = .bool(true) }
        if excludeHr {
            out["exclude_hr"] = .bool(true)
            if hrUnknownPolicy == "strict" { out["hr_unknown_policy"] = .string("strict") }
        }
        if let minSeeders { out["min_seeders"] = .int(minSeeders) }
        if let sizeMinMb { out["size_min_mb"] = .int(sizeMinMb) }
        if let sizeMaxMb { out["size_max_mb"] = .int(sizeMaxMb) }
        put("release_groups_allow", releaseGroupsAllow)
        put("release_groups_block", releaseGroupsBlock)
        if let upgradeSource {
            out["upgrade_source"] = .string(upgradeSource)
            if let cutoffResolution { out["cutoff_resolution"] = .string(cutoffResolution) }
            if upgradeKeepOld { out["upgrade_keep_old"] = .bool(true) }
        }
        put("sites", sites)
        return out
    }

    /// 旧三态 → (白名单, 黑名单)，与后端 _hdr_from_legacy 同一张表
    var effectiveHdr: (levels: [String], block: [String]) {
        if !hdrLevels.isEmpty || !hdrBlock.isEmpty { return (hdrLevels, hdrBlock) }
        let family = RuleSetVocabulary.hdrOptions.filter { $0 != "SDR" }
        if legacyHdr == "forbid" { return (["SDR"], []) }
        if legacyDv == "require" { return (["DV"], []) }
        if legacyHdr == "require" {
            return legacyDv == "forbid" ? (family.filter { $0 != "DV" }, ["DV"]) : (family, [])
        }
        if legacyDv == "forbid" { return ([], ["DV"]) }
        return ([], [])
    }
}

extension API.RuleSetView {
    var typedSpec: RuleSetSpec { RuleSetSpec(spec) }
    /// 洗版目标人话标签；nil = 本组不洗版
    var upgradeTarget: String? { RuleSetText.upgradeTargetLabel(typedSpec) }
}

// MARK: - spec → 人话

enum RuleSetText {
    static func hdrChipText(_ levels: [String]) -> String {
        let family = RuleSetVocabulary.hdrOptions.filter { $0 != "SDR" }
        if levels.count == family.count, family.allSatisfy(levels.contains) { return "必须 HDR" }
        if levels == ["SDR"] { return "排除 HDR" }
        return "HDR \(levels.joined(separator: "/"))"
    }

    /// 洗版目标的人话标签（「1080p WEB-DL · DV」）；未开洗版返回 nil
    static func upgradeTargetLabel(_ spec: RuleSetSpec) -> String? {
        guard let source = spec.upgradeSource else { return nil }
        let resolution = spec.cutoffResolution ?? spec.resolutions.first ?? "1080p"
        var parts = ["\(resolution) \(RuleSetVocabulary.upgradeSourceLabel[source] ?? source)"]
        let ladder = spec.upgradeLadder ?? RuleSetVocabulary.defaultLadder
        if ladder.contains("hdr"), let first = spec.hdrLevels.first { parts.append(first) }
        if ladder.contains("video_codec"), let first = spec.videoCodecs.first { parts.append(first) }
        if ladder.contains("platform"), let first = spec.platforms.first { parts.append(RuleSetVocabulary.platformLabel(first)) }
        return parts.joined(separator: " · ")
    }

    /// spec → 人话芯片（空 = 全不限）
    static func summary(_ spec: RuleSetSpec, withoutUpgrade: Bool = false) -> [String] {
        var chips: [String] = []
        if !spec.resolutions.isEmpty { chips.append(spec.resolutions.joined(separator: " > ")) }
        if !spec.mediaSources.isEmpty {
            chips.append(spec.mediaSources.map(RuleSetVocabulary.mediaSourceLabel).joined(separator: " > "))
        }
        if !withoutUpgrade, let target = upgradeTargetLabel(spec) {
            chips.append("洗到 \(target)\(spec.upgradeKeepOld ? "（保留旧版）" : "")")
        }
        if !spec.videoCodecs.isEmpty {
            var rest = spec.videoCodecs
            var labels: [String] = []
            for family in RuleSetVocabulary.codecFamilies where family.values.contains(where: rest.contains) {
                labels.append(family.label)
                rest.removeAll { family.values.contains($0) }
            }
            labels.append(contentsOf: rest)
            chips.append(labels.joined(separator: "/"))
        }
        if !spec.platforms.isEmpty {
            chips.append("平台: \(spec.platforms.map(RuleSetVocabulary.platformLabel).joined(separator: "/"))")
        }
        if !spec.platformsBlock.isEmpty {
            chips.append("排除平台: \(spec.platformsBlock.map(RuleSetVocabulary.platformLabel).joined(separator: "/"))")
        }
        let hdr = spec.effectiveHdr
        if !hdr.levels.isEmpty { chips.append(hdrChipText(hdr.levels)) }
        if !hdr.block.isEmpty { chips.append("排除 \(hdr.block.joined(separator: "/"))") }
        if !spec.subtitleLanguages.isEmpty {
            chips.append("字幕: " + spec.subtitleLanguages.map { v in RuleSetVocabulary.subtitleLanguages.first { $0.value == v }?.label ?? v }.joined(separator: "/"))
        }
        if !spec.audioLanguages.isEmpty {
            chips.append("音轨: " + spec.audioLanguages.map { v in RuleSetVocabulary.audioLanguages.first { $0.value == v }?.label ?? v }.joined(separator: "/"))
        }
        if spec.freeOnly { chips.append("仅免费") }
        if let seeders = spec.minSeeders { chips.append("做种 ≥ \(seeders)") }
        if spec.sizeMinMb != nil || spec.sizeMaxMb != nil {
            let min = spec.sizeMinMb.map(String.init) ?? ""
            let max = spec.sizeMaxMb.map(String.init) ?? ""
            chips.append(!min.isEmpty && !max.isEmpty ? "单集 \(min)–\(max)MB" : !min.isEmpty ? "单集 ≥ \(min)MB" : "单集 ≤ \(max)MB")
        }
        if spec.excludeHr { chips.append(spec.hrUnknownPolicy == "strict" ? "排除 H&R（未知也排）" : "排除 H&R") }
        if !spec.releaseGroupsAllow.isEmpty { chips.append("制作组白名单 \(spec.releaseGroupsAllow.count) 个") }
        if !spec.releaseGroupsBlock.isEmpty { chips.append("制作组黑名单 \(spec.releaseGroupsBlock.count) 个") }
        return chips
    }
}

// MARK: - 洗版阶梯预览（同 Web lib/upgrade-ladder.ts）

/// 把洗版配置摊成「从终点往下数」的档位清单：终点、更高也算达标的顶档、终点之下的过渡档
struct UpgradeLadderPreview: Equatable {
    var dimensions: [String]
    var target: String
    var ceiling: String?
    var below: [String]
    var moreBelow: Int

    private static let maxRungsBelow = 5

    init?(_ spec: RuleSetSpec) {
        guard let upgradeSource = spec.upgradeSource else { return nil }
        let known = Set(RuleSetVocabulary.ladderOptions.map(\.value))
        var dims = (spec.upgradeLadder ?? RuleSetVocabulary.defaultLadder).filter { dim in
            known.contains(dim)
                && !(dim == "hdr" && spec.hdrLevels.isEmpty)
                && !(dim == "video_codec" && spec.videoCodecs.isEmpty)
                && !(dim == "platform" && spec.platforms.isEmpty)
        }
        if dims.isEmpty { dims = RuleSetVocabulary.defaultLadder }

        let sourceLabel = Dictionary(uniqueKeysWithValues: RuleSetVocabulary.mediaSources.map { ($0.value, $0.label) })
        func axis(_ dim: String) -> [String] {
            switch dim {
            case "resolution": return spec.resolutions.isEmpty ? RuleSetVocabulary.defaultResolutionLadder : spec.resolutions
            case "source":
                let chosen = spec.mediaSources.isEmpty ? RuleSetVocabulary.mediaSources.map(\.value) : spec.mediaSources
                return chosen.map { sourceLabel[$0] ?? $0 }
            case "hdr": return spec.hdrLevels
            case "video_codec":
                var result: [String] = []
                for value in spec.videoCodecs {
                    let family = RuleSetVocabulary.codecFamilies.first { $0.values.contains { $0.lowercased() == value.lowercased() } }?.label ?? value
                    if !result.contains(family) { result.append(family) }
                }
                return result
            default: return spec.platforms.map(RuleSetVocabulary.platformLabel)
            }
        }
        let axes = dims.map(axis)
        let digits: [Int] = dims.enumerated().map { index, dim in
            switch dim {
            case "resolution": return axes[index].firstIndex(of: spec.cutoffResolution ?? spec.resolutions.first ?? "1080p") ?? -1
            case "source": return axes[index].firstIndex(of: sourceLabel[upgradeSource] ?? "") ?? -1
            default: return 0
            }
        }
        guard !digits.contains(where: { $0 < 0 }), !axes.contains(where: \.isEmpty) else { return nil }

        func label(_ combo: [Int]) -> String { combo.enumerated().map { axes[$0.offset][$0.element] }.joined(separator: " · ") }
        let total = axes.reduce(1) { $0 * $1.count }
        let ordinal = digits.enumerated().reduce(0) { $0 * axes[$1.offset].count + $1.element }
        func decode(_ value: Int) -> [Int] {
            var combo = Array(repeating: 0, count: axes.count)
            var rest = value
            for index in stride(from: axes.count - 1, through: 0, by: -1) {
                combo[index] = rest % axes[index].count
                rest /= axes[index].count
            }
            return combo
        }
        let belowCount = total - 1 - ordinal
        let shown = min(belowCount, Self.maxRungsBelow)
        dimensions = dims.map { dim in RuleSetVocabulary.ladderOptions.first { $0.value == dim }?.label ?? dim }
        target = label(digits)
        ceiling = ordinal > 0 ? label(Array(repeating: 0, count: axes.count)) : nil
        below = shown > 0 ? (1 ... shown).map { label(decode(ordinal + $0)) } : []
        moreBelow = belowCount - shown
    }
}

// MARK: - 适用范围（match_rules）

/// 规则组适用范围的表单态：kind nil = 不限电影/剧集
struct RuleSetScope: Equatable {
    var kind: String?
    var regions: [String] = []
    var genres: [Int] = []

    init(kind: String? = nil, regions: [String] = [], genres: [Int] = []) {
        self.kind = kind
        self.regions = regions
        self.genres = genres
    }

    init(_ rules: [[String: API.JSONValue]]) {
        func values(_ field: String) -> [API.JSONValue] {
            rules.first { $0["field"]?.stringValue == field }?["values"]?.arrayValue ?? []
        }
        let kinds = values("kind").compactMap(\.stringValue)
        kind = kinds.count == 1 && (kinds[0] == "movie" || kinds[0] == "tv") ? kinds[0] : nil
        regions = values("origin_countries").compactMap { if case let .string(s) = $0 { s } else { nil } }
        genres = values("genres").compactMap { if case let .int(i) = $0 { i } else { nil } }
    }

    /// 表单态 → 条件（空维度不生成条件；全空 = 不声明）
    var rules: [[String: API.JSONValue]] {
        var out: [[String: API.JSONValue]] = []
        if let kind { out.append(["field": .string("kind"), "op": .string("any_of"), "values": .array([.string(kind)])]) }
        if !regions.isEmpty { out.append(["field": .string("origin_countries"), "op": .string("any_of"), "values": .array(regions.map { .string($0) })]) }
        if !genres.isEmpty { out.append(["field": .string("genres"), "op": .string("any_of"), "values": .array(genres.map { .int($0) })]) }
        return out
    }

    /// 一句话摘要（「剧集 · 日韩 · 动画」）；未声明返回 nil
    func summary(_ options: LibraryRoutingOptions?) -> String? {
        guard kind != nil || !regions.isEmpty || !genres.isEmpty else { return nil }
        var parts: [String] = []
        if let kind { parts.append(kind == "movie" ? "电影" : "剧集") }
        if !regions.isEmpty {
            parts.append(options.map { $0.regionLabels(regions).joined(separator: "/") } ?? "\(regions.count) 个区域")
        }
        if !genres.isEmpty {
            parts.append(options.map { $0.genres(for: kind).filter { genres.contains($0.id) }.map(\.label).joined(separator: "/") } ?? "\(genres.count) 个类型")
        }
        return parts.joined(separator: " · ")
    }
}

/// 收藏范围可选项（`GET /libraries/routing-options`，生成模型是自由 JSON，这里解码成强类型）
struct LibraryRoutingOptions: Decodable, Equatable {
    struct Genre: Decodable, Equatable, Identifiable {
        var id: Int
        var label: String
    }

    struct RegionPreset: Decodable, Equatable, Identifiable {
        var key: String
        var label: String
        var countries: [String]
        var id: String { key }
    }

    var movieGenres: [Genre]
    var tvGenres: [Genre]
    var regionPresets: [RegionPreset]
    var countryNames: [String: String]

    enum CodingKeys: String, CodingKey {
        case movieGenres = "movie_genres"
        case tvGenres = "tv_genres"
        case regionPresets = "region_presets"
        case countryNames = "country_names"
    }

    /// kind nil = 电影与剧集类型合并去重
    func genres(for kind: String?) -> [Genre] {
        if kind == "movie" { return movieGenres }
        if kind != nil { return tvGenres }
        var seen = Set<Int>()
        return (movieGenres + tvGenres).filter { seen.insert($0.id).inserted }
    }

    /// 区域国家码折叠成展示名：整组命中折叠成预设组名（如「日韩」）
    func regionLabels(_ regions: [String]) -> [String] {
        var parts: [String] = []
        var rest = regions
        for preset in regionPresets where preset.countries.allSatisfy(rest.contains) {
            parts.append(preset.label)
            rest.removeAll { preset.countries.contains($0) }
        }
        parts.append(contentsOf: rest.map { countryNames[$0] ?? $0 })
        return parts
    }

    /// 国家列表：JSON 对象解码后丢了服务端顺序，按预设组顺序（大陆、港台、日韩…）排在前面，
    /// 预设外的国家按中文名排在后面，与网页的视觉顺序基本一致且稳定
    var sortedCountries: [(code: String, name: String)] {
        var ordered: [String] = []
        for preset in regionPresets {
            for code in preset.countries where countryNames[code] != nil && !ordered.contains(code) { ordered.append(code) }
        }
        let rest = countryNames.keys.filter { !ordered.contains($0) }
            .sorted { (countryNames[$0] ?? $0).localizedStandardCompare(countryNames[$1] ?? $1) == .orderedAscending }
        return (ordered + rest).map { ($0, countryNames[$0] ?? $0) }
    }
}

extension APIClient {
    /// 收藏范围可选项（静态常量，进程内缓存一次）
    func routingOptions() async throws -> LibraryRoutingOptions {
        if let cached = RoutingOptionsCache.value { return cached }
        let raw = try await libraryListRoutingOptions()
        let decoded = try API.JSONValue.object(raw).decode(as: LibraryRoutingOptions.self)
        RoutingOptionsCache.value = decoded
        return decoded
    }
}

@MainActor
private enum RoutingOptionsCache {
    static var value: LibraryRoutingOptions?
}
