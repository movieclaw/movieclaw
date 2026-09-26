import Foundation

/// 站点资源搜索结果的纯前端规则：排序、筛选、分面计数、作品分组与各种展示标签。
/// 逐条移植自 Web `components/search-results.tsx`，全部是无副作用的纯函数（可单元测试）。
///
/// 筛选与排序都不重新发起搜索；流式期间新到的结果实时并入当前筛选/排序视图。

// MARK: - 排序

nonisolated enum TorrentSortKey: String, CaseIterable, Hashable, Sendable {
    case seeders, time, size, snatched
    /// 智能排序：全集优先 / 画质优先 / 免费优先
    case complete, quality, free

    var label: String {
        switch self {
        case .seeders: "做种数"
        case .time: "发布时间"
        case .size: "体积"
        case .snatched: "完成数"
        case .complete: "全集优先"
        case .quality: "画质优先"
        case .free: "免费优先"
        }
    }

    static let regular: [TorrentSortKey] = [.seeders, .time, .size, .snatched]
    static let smart: [TorrentSortKey] = [.complete, .quality, .free]
}

nonisolated struct TorrentSort: Hashable, Sendable {
    var key: TorrentSortKey = .seeders
    var descending = true

    /// 菜单里点新键 = 选中并降序；点当前键 = 翻转方向（同 Web SortDropdown）
    func picking(_ key: TorrentSortKey) -> TorrentSort {
        key == self.key ? TorrentSort(key: key, descending: !descending) : TorrentSort(key: key, descending: true)
    }
}

/// 结果视图：分组（按作品聚合，默认）/ 列表（平铺原始种子名）/ 图览（海报墙）
nonisolated enum TorrentResultView: String, CaseIterable, Hashable, Sendable {
    case group, list, poster

    var label: String {
        switch self {
        case .group: "分组"
        case .list: "列表"
        case .poster: "图览"
        }
    }

    var systemImage: String {
        switch self {
        case .group: "square.stack.3d.up"
        case .list: "list.bullet"
        case .poster: "photo.on.rectangle"
        }
    }
}

// MARK: - 筛选

/// 筛选维度（与 Web Filters 的键一一对应）；组内多选 = 或，组间 = 且
nonisolated enum TorrentFilterDim: String, CaseIterable, Hashable, Sendable {
    case resolution, site, year, season, episode, source, platform, codec, hdr, audio, subtitle, group

    /// 分辨率以外的筛选维度（与 Web 筛选弹层同口径，`sheetCount` 只统计这些）
    static let sheetDims: [TorrentFilterDim] = [.site, .year, .season, .episode, .source, .platform, .codec, .hdr, .audio, .subtitle, .group]

    var title: String {
        switch self {
        case .resolution: "分辨率"
        case .site: "站点"
        case .year: "年份"
        case .season: "季"
        case .episode: "集"
        case .source: "片源"
        case .platform: "流媒体平台"
        case .codec: "视频编码"
        case .hdr: "HDR"
        case .audio: "音频"
        case .subtitle: "字幕"
        case .group: "压制组"
        }
    }
}

/// 当前筛选：每个维度一组已选值（数值维度也以字符串存，展示时再格式化）
nonisolated struct TorrentFilters: Hashable, Sendable {
    var selected: [TorrentFilterDim: Set<String>] = [:]

    func values(_ dim: TorrentFilterDim) -> Set<String> { selected[dim] ?? [] }

    mutating func toggle(_ dim: TorrentFilterDim, _ value: String) {
        var set = values(dim)
        if set.contains(value) { set.remove(value) } else { set.insert(value) }
        selected[dim] = set.isEmpty ? nil : set
    }

    /// 弹层维度的已选数（筛选键角标）
    var sheetCount: Int { TorrentFilterDim.sheetDims.reduce(0) { $0 + values($1).count } }
    var isActive: Bool { !values(.resolution).isEmpty || sheetCount > 0 }
}

/// 一个分面取值及「选中它之后会看到的条数」
nonisolated struct TorrentFacetValue: Hashable, Sendable {
    var value: String
    var count: Int
}

nonisolated struct TorrentFacets: Sendable {
    var byDim: [TorrentFilterDim: [TorrentFacetValue]] = [:]
    func values(_ dim: TorrentFilterDim) -> [TorrentFacetValue] { byDim[dim] ?? [] }
}

/// 作品分组的组头元数据
nonisolated struct TorrentEntity: Hashable, Sendable {
    var key: String
    var nameZh: String?
    var nameEn: String?
    var year: Int?
    var mediaType: String?
    var contentType: String?
}

nonisolated enum TorrentSearchLogic {
    /// 未识别桶的聚合键（解析不出片名的种子都归这里，按原始名平铺，固定沉底）
    static let unparsedKey = "__unparsed__"

    // MARK: 排序

    /// 剧集完整度层级：4=全集、3=整季包、2=多集、1=单集、0=无季集信息
    static func completenessTier(_ a: API.TorrentAttrs?) -> Double {
        guard let a else { return 0 }
        if a.complete == true { return 4 }
        if !a.seasons.isEmpty, a.episodes.isEmpty { return 3 }
        if a.episodes.count > 1 { return 2 }
        if a.episodes.count == 1 { return 1 }
        return 0
    }

    /// 片源档位（画质排序第二级）
    static let sourceRank: [String: Double] = [
        "UHD Blu-ray": 7, "Blu-ray": 6, "WEB-DL": 5, "BDRip": 4, "WEBRip": 3, "HD-DVD": 3,
        "HDTV": 2, "HDTVRip": 2, "HDRip": 2, "TVRip": 1, "DVDRip": 1, "DVD": 1,
    ]

    static func uploadTimestamp(_ hit: API.TorrentHit) -> Double {
        guard let raw = hit.uploadTime, let date = parseDate(raw) else { return -.infinity }
        return date.timeIntervalSince1970
    }

    /// 排序向量：逐级字典序比较；智能键是「主指标 → 次指标 → 做种数决胜」
    static func sortVector(_ hit: API.TorrentHit, _ key: TorrentSortKey) -> [Double] {
        let a = hit.attrs
        switch key {
        case .time: return [uploadTimestamp(hit)]
        case .size: return [Double(hit.sizeBytes)]
        case .snatched: return [Double(hit.snatched)]
        case .complete: return [completenessTier(a), Double(a?.seasons.count ?? 0), Double(hit.seeders)]
        case .quality:
            let resolution = a?.resolution.flatMap { Double($0.prefix { $0.isNumber }) } ?? 0
            let source = (a?.mediaSource.flatMap { sourceRank[$0] } ?? 0) + (a?.remux == true ? 0.5 : 0)
            return [resolution, source, Double(hit.seeders)]
        case .free:
            let promo: Double = hit.free || hit.downloadVolumeFactor == 0 ? 2 : hit.downloadVolumeFactor < 1 ? 1 : 0
            return [promo, hit.hitAndRun == false ? 1 : 0, Double(hit.seeders)]
        case .seeders: return [Double(hit.seeders)]
        }
    }

    static func sorted(_ hits: [API.TorrentHit], by sort: TorrentSort) -> [API.TorrentHit] {
        let decorated = hits.enumerated().map { ($0.offset, $0.element, sortVector($0.element, sort.key)) }
        return decorated.sorted { x, y in
            for i in x.2.indices where x.2[i] != y.2[i] {
                // 无发布时间（-∞）升降序都垫底
                if x.2[i] == -.infinity { return false }
                if y.2[i] == -.infinity { return true }
                return sort.descending ? x.2[i] > y.2[i] : x.2[i] < y.2[i]
            }
            return x.0 < y.0 // 稳定排序
        }.map(\.1)
    }

    /// 智能排序键按结果集构成动态出现：剧集为主给「全集优先」，电影为主给「画质优先」，「免费优先」常驻
    static func smartSortKeys(_ hits: [API.TorrentHit]) -> [TorrentSortKey] {
        var tv = 0, movie = 0
        for hit in hits {
            if hit.attrs?.mediaType == "tv" { tv += 1 } else if hit.attrs?.mediaType == "movie" { movie += 1 }
        }
        let typed = tv + movie
        var keys: [TorrentSortKey] = []
        if typed > 0, Double(tv) >= Double(typed) / 2 { keys.append(.complete) }
        if typed > 0, Double(movie) > Double(typed) / 2 { keys.append(.quality) }
        keys.append(.free)
        return keys
    }

    // MARK: 筛选

    /// 单维度通过判定（维度未激活即通过）；过滤与分面计数共用
    static func passes(_ dim: TorrentFilterDim, _ hit: API.TorrentHit, _ f: TorrentFilters) -> Bool {
        let selected = f.values(dim)
        if selected.isEmpty { return true }
        let a = hit.attrs
        switch dim {
        case .resolution: return a?.resolution.map(selected.contains) ?? false
        case .site: return selected.contains(hit.siteId)
        case .year: return a?.year.map { selected.contains(String($0)) } ?? false
        case .season: return (a?.seasons ?? []).contains { selected.contains(String($0)) }
        case .episode:
            // 全集包视为包含任意一集
            return (a?.episodes ?? []).contains { selected.contains(String($0)) } || a?.complete == true
        case .source:
            let bySource = a?.mediaSource.map(selected.contains) ?? false
            return bySource || (selected.contains("Remux") && a?.remux == true)
        case .platform: return (a?.platforms ?? []).contains(where: selected.contains)
        case .codec: return a?.videoCodec.map(selected.contains) ?? false
        case .hdr: return (a?.hdr ?? []).contains(where: selected.contains)
        case .audio: return (a?.audio ?? []).contains(where: selected.contains)
        case .subtitle:
            // BCP 47 前缀语义：勾 zh 命中 zh / zh-Hans / zh-Hant；勾 zh-Hans 只命中简体
            return (a?.subtitleLanguages ?? []).contains { lang in
                selected.contains(lang) || selected.contains { lang.hasPrefix("\($0)-") }
            }
        case .group: return a?.releaseGroup.map(selected.contains) ?? false
        }
    }

    static func matches(_ hit: API.TorrentHit, _ f: TorrentFilters) -> Bool {
        TorrentFilterDim.allCases.allSatisfy { passes($0, hit, f) }
    }

    /// 遍历结果集统计各维度计数。传 filters 时做「自排除」计数：某条结果只有通过了**其他所有**维度，
    /// 才计入某维度的数字——每个标签上的数字始终等于「选中它之后会看到的条数」。
    static func facetCounts(_ hits: [API.TorrentHit], filters: TorrentFilters?) -> [TorrentFilterDim: [String: Int]] {
        var maps: [TorrentFilterDim: [String: Int]] = [:]
        func bump(_ dim: TorrentFilterDim, _ value: String) { maps[dim, default: [:]][value, default: 0] += 1 }
        for hit in hits {
            var only: TorrentFilterDim?
            if let filters {
                var failed = 0
                for dim in TorrentFilterDim.allCases where !passes(dim, hit, filters) {
                    failed += 1
                    only = dim
                    if failed > 1 { break }
                }
                if failed > 1 { continue }
                if failed == 0 { only = nil }
            }
            func want(_ dim: TorrentFilterDim) -> Bool { only == nil || only == dim }
            if want(.site) { bump(.site, hit.siteId) }
            guard let a = hit.attrs else { continue }
            if want(.year), let year = a.year { bump(.year, String(year)) }
            if want(.season) { for s in a.seasons { bump(.season, String(s)) } }
            if want(.episode) { for e in a.episodes { bump(.episode, String(e)) } }
            if want(.resolution), let r = a.resolution { bump(.resolution, r) }
            if want(.source) {
                if let s = a.mediaSource { bump(.source, s) }
                if a.remux { bump(.source, "Remux") }
            }
            if want(.platform) { for v in a.platforms { bump(.platform, v) } }
            if want(.codec), let c = a.videoCodec { bump(.codec, c) }
            if want(.hdr) { for v in a.hdr { bump(.hdr, v) } }
            if want(.audio) { for v in a.audio { bump(.audio, v) } }
            if want(.subtitle) {
                // 「中文（不限简繁）」的计数涵盖声明了 zh-Hans / zh-Hant 的资源
                var keys = Set(a.subtitleLanguages)
                if a.subtitleLanguages.contains(where: { $0.hasPrefix("zh-") }) { keys.insert("zh") }
                for v in keys { bump(.subtitle, v) }
            }
            if want(.group), let g = a.releaseGroup { bump(.group, g) }
        }
        return maps
    }

    /// 全量计数定「有哪些 chip、按什么顺序排」（筛选变化时 chip 不增删、不换位），数字随筛选变化
    static func facets(_ hits: [API.TorrentHit], filters: TorrentFilters) -> TorrentFacets {
        let full = facetCounts(hits, filters: nil)
        let counts = filters.isActive ? facetCounts(hits, filters: filters) : full
        var result = TorrentFacets()
        for dim in TorrentFilterDim.allCases {
            let entries = full[dim] ?? [:]
            let ordered: [String]
            switch dim {
            case .year:
                ordered = entries.keys.sorted { (Int($0) ?? 0) > (Int($1) ?? 0) }
            case .season, .episode:
                ordered = entries.keys.sorted { (Int($0) ?? 0) < (Int($1) ?? 0) }
            case .site:
                ordered = Array(entries.keys)
            default:
                let cap = dim == .resolution ? 8 : 50
                ordered = Array(entries.sorted { $0.value > $1.value || ($0.value == $1.value && $0.key < $1.key) }.prefix(cap).map(\.key))
            }
            result.byDim[dim] = ordered.map { TorrentFacetValue(value: $0, count: counts[dim]?[$0] ?? 0) }
        }
        return result
    }

    // MARK: 作品分组

    /// 作品聚合键：解析主名（中文优先）定作品；电影按年份区分，剧集不按年拆
    static func entityKey(_ hit: API.TorrentHit) -> String {
        guard let title = hit.attrs?.titlesZh.first ?? hit.attrs?.titlesEn.first else { return unparsedKey }
        let type = hit.attrs?.mediaType ?? "?"
        if type == "tv" { return "tv|\(title)" }
        return "\(type)|\(title)|\(hit.attrs?.year.map(String.init) ?? "?")"
    }

    static func entities(_ hits: [API.TorrentHit]) -> [String: TorrentEntity] {
        var groups: [String: TorrentEntity] = [:]
        for hit in hits {
            let key = entityKey(hit)
            if key == unparsedKey { continue }
            let a = hit.attrs
            if var g = groups[key] {
                g.nameZh = g.nameZh ?? a?.titlesZh.first
                g.nameEn = g.nameEn ?? a?.titlesEn.first
                g.year = g.year ?? a?.year
                g.contentType = g.contentType ?? a?.contentType
                groups[key] = g
            } else {
                groups[key] = TorrentEntity(key: key, nameZh: a?.titlesZh.first, nameEn: a?.titlesEn.first, year: a?.year, mediaType: a?.mediaType, contentType: a?.contentType)
            }
        }
        return groups
    }

    /// 分组顺序 = 按当前排序遍历时各作品的首次出现序；未识别桶沉底
    static func buckets(_ sortedHits: [API.TorrentHit]) -> [(key: String, rows: [API.TorrentHit])] {
        var buckets: [(key: String, rows: [API.TorrentHit])] = []
        var index: [String: Int] = [:]
        for hit in sortedHits {
            let key = entityKey(hit)
            if let i = index[key] {
                buckets[i].rows.append(hit)
            } else {
                index[key] = buckets.count
                buckets.append((key, [hit]))
            }
        }
        if let i = index[unparsedKey], i != buckets.count - 1 {
            buckets.append(buckets.remove(at: i))
        }
        return buckets
    }

    static func maxResolution(_ rows: [API.TorrentHit]) -> String? {
        rows.compactMap { $0.attrs?.resolution }
            .max { (Int($0.prefix { $0.isNumber }) ?? -1) < (Int($1.prefix { $0.isNumber }) ?? -1) }
    }

    // MARK: 展示标签

    static let mediaTypeLabels = ["movie": "电影", "tv": "剧集"]
    static let contentTypeLabels = ["anime": "动漫", "documentary": "纪录片", "variety": "综艺", "music": "音乐"]

    static let subtitleLanguageLabels = [
        "zh": "中文字幕（未标简繁）", "zh-Hans": "简体中文字幕", "zh-Hant": "繁体中文字幕",
        "en": "英文字幕", "ja": "日文字幕", "ko": "韩文字幕", "yue": "粤语字幕",
    ]

    static func subtitleLanguageLabel(_ value: String) -> String { subtitleLanguageLabels[value] ?? value }

    static let platformLabels: [String: String] = [
        "netflix": "Netflix", "amazon": "Prime Video", "disney_plus": "Disney+", "hbo_max": "HBO Max", "hulu": "Hulu",
        "apple_tv_plus": "Apple TV+", "paramount_plus": "Paramount+", "peacock": "Peacock", "showtime": "Showtime",
        "starz": "Starz", "discovery_plus": "Discovery+", "crave": "Crave", "stan": "Stan", "roku": "Roku",
        "google_tv": "Google TV", "itunes": "iTunes", "sony_core": "Sony Pictures Core", "criterion": "Criterion",
        "iqiyi": "爱奇艺", "wetv": "腾讯视频", "youku": "优酷", "mangotv": "芒果 TV", "bilibili": "哔哩哔哩",
        "viu": "Viu", "nowplayer": "Now player", "mytv_super": "myTV SUPER", "hami_video": "Hami Video",
        "line_tv": "LINE TV", "kktv": "KKTV", "tving": "TVING", "wavve": "Wavve", "coupang_play": "Coupang Play",
        "kocowa": "KOCOWA", "viki": "Viki", "unext": "U-NEXT", "tver": "TVer", "fod": "FOD", "dmm_tv": "DMM TV",
        "hotstar": "Hotstar", "crunchyroll": "Crunchyroll", "hidive": "HIDIVE", "abema": "ABEMA", "adn": "ADN",
        "funimation": "Funimation", "vrv": "VRV", "wakanim": "Wakanim", "b_global": "B-Global",
    ]

    static func platformLabel(_ id: String) -> String { platformLabels[id] ?? id }

    /// 分面值的展示文案
    static func facetLabel(_ dim: TorrentFilterDim, _ value: String, siteName: (String) -> String) -> String {
        switch dim {
        case .site: siteName(value)
        case .season: "第\(value)季"
        case .episode: "第\(value)集"
        case .subtitle: subtitleLanguageLabel(value)
        case .platform: platformLabel(value)
        default: value
        }
    }

    static let subLangShort = ["zh-Hans": "简", "zh-Hant": "繁", "zh": "中", "en": "英", "ja": "日", "ko": "韩", "yue": "粤"]
    static let audioLangShort = ["cmn": "国", "yue": "粤", "en": "英", "ja": "日", "ko": "韩"]

    /// 「字幕 简·繁·英［·硬］」；仅泛称时「中字」；无声明 nil
    static func compactSubtitleBadge(_ a: API.TorrentAttrs) -> String? {
        var langs = a.subtitleLanguages
        if langs.isEmpty { return nil }
        if langs.contains(where: { $0.hasPrefix("zh-") }) { langs.removeAll { $0 == "zh" } }
        let hard = a.subtitleCarriers.contains("hardcoded") ? "·硬" : ""
        if langs == ["zh"] { return "中字\(hard)" }
        return "字幕 \(langs.map { subLangShort[$0] ?? $0 }.joined(separator: "·"))\(hard)"
    }

    static func compactAudioBadge(_ a: API.TorrentAttrs) -> String? {
        a.audioLanguages.isEmpty ? nil : "音轨 \(a.audioLanguages.map { audioLangShort[$0] ?? $0 }.joined(separator: "·"))"
    }

    /// S01E01-E06 / S01-S03 形式的季集标签
    static func seasonEpLabel(_ a: API.TorrentAttrs) -> String? {
        func pad(_ n: Int) -> String { String(format: "%02d", n) }
        func range(_ nums: [Int], _ prefix: String) -> String {
            nums.count == 1 ? "\(prefix)\(pad(nums[0]))" : "\(prefix)\(pad(nums.first!))-\(prefix)\(pad(nums.last!))"
        }
        var parts: [String] = []
        if a.seasons.count == 1, !a.episodes.isEmpty {
            parts.append("S\(pad(a.seasons[0]))\(range(a.episodes, "E"))")
        } else {
            if !a.seasons.isEmpty { parts.append(range(a.seasons, "S")) }
            if !a.episodes.isEmpty, a.complete != true { parts.append(range(a.episodes, "E")) }
        }
        return parts.isEmpty ? nil : parts.joined(separator: " ")
    }

    static func completeLabel(_ a: API.TorrentAttrs?) -> String? {
        guard a?.complete == true else { return nil }
        return a?.episodesTotal.map { "全\($0)集" } ?? "全集"
    }

    /// 连续数字用区间（1-6），不连续用顿号（1、3）
    static func formatNumList(_ nums: [Int]) -> String {
        let sorted = nums.sorted()
        let contiguous = sorted.indices.allSatisfy { $0 == 0 || sorted[$0] == sorted[$0 - 1] + 1 }
        if sorted.count > 1, contiguous { return "\(sorted.first!)-\(sorted.last!)" }
        return sorted.map(String.init).joined(separator: "、")
    }

    /// 图览海报左下角的季集 chip；pack = 全集 / 整季包（醒目底色）
    static func seasonEpisodeChip(_ a: API.TorrentAttrs?) -> (text: String, pack: Bool)? {
        guard let a, a.mediaType != "movie" else { return nil }
        let season = a.seasons.isEmpty ? nil : "第\(formatNumList(a.seasons))季"
        let episode = completeLabel(a) ?? (a.episodes.isEmpty ? nil : "第\(formatNumList(a.episodes))集")
        if season == nil, episode == nil { return nil }
        let pack = a.complete == true || (season != nil && a.episodes.isEmpty)
        return ([season, episode].compactMap { $0 }.joined(separator: " · "), pack)
    }

    /// 分组视图组内行：用规格摘要代替重复的片名
    static func specSummary(_ a: API.TorrentAttrs) -> String? {
        var parts: [String] = []
        if let v = seasonEpLabel(a) { parts.append(v) }
        if let v = a.resolution { parts.append(v) }
        parts += a.platforms.map(platformLabel)
        if let v = a.mediaSource { parts.append(v) }
        if a.remux { parts.append("Remux") }
        if let v = a.videoCodec { parts.append(v) }
        parts += a.hdr
        if let v = compactSubtitleBadge(a) { parts.append(v) }
        if let v = compactAudioBadge(a) { parts.append(v) }
        parts += a.audio.prefix(2)
        if let v = a.releaseGroup { parts.append(v) }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// 解析片名：中文主名 + 外文副名；解析不出返回 nil（回退原始种子名）
    static func parsedName(_ hit: API.TorrentHit) -> (primary: String, secondary: String?)? {
        if let zh = hit.attrs?.titlesZh.first { return (zh, hit.attrs?.titlesEn.first) }
        if let en = hit.attrs?.titlesEn.first { return (en, nil) }
        return nil
    }

    /// 促销角标种类：免费 / 折扣 / 上传倍率 / H&R
    enum Promo: Hashable { case free, discount(String), upload(String), hitAndRun }

    static func promos(_ hit: API.TorrentHit) -> [Promo] {
        var result: [Promo] = []
        if hit.free || hit.downloadVolumeFactor == 0 {
            result.append(.free)
        } else if hit.downloadVolumeFactor < 1 {
            result.append(.discount("\(Int((hit.downloadVolumeFactor * 100).rounded()))%"))
        }
        if hit.uploadVolumeFactor > 1 {
            let factor = hit.uploadVolumeFactor == hit.uploadVolumeFactor.rounded() ? String(Int(hit.uploadVolumeFactor)) : String(hit.uploadVolumeFactor)
            result.append(.upload("\(factor)× 上传"))
        }
        if hit.hitAndRun == true { result.append(.hitAndRun) }
        return result
    }

    /// 属性徽标（最多展示 4 个，其余折叠成 +N）
    enum AttrTone: Hashable { case plain, season, content, remux, hdr, subtitle, audio, group, platform }

    static func attrBadges(_ a: API.TorrentAttrs) -> [(text: String, tone: AttrTone)] {
        var chips: [(String, AttrTone)] = []
        if let v = seasonEpLabel(a) { chips.append((v, .season)) }
        if let c = a.contentType, let label = contentTypeLabels[c] { chips.append((label, .content)) }
        if let v = a.resolution { chips.append((v, .plain)) }
        if a.remux { chips.append(("Remux", .remux)) }
        for v in a.hdr { chips.append((v, .hdr)) }
        if let v = compactSubtitleBadge(a) { chips.append((v, .subtitle)) }
        if let v = compactAudioBadge(a) { chips.append((v, .audio)) }
        if let v = a.releaseGroup { chips.append((v, .group)) }
        for v in a.platforms { chips.append((platformLabel(v), .platform)) }
        if let v = a.mediaSource { chips.append((v, .plain)) }
        if let v = a.videoCodec { chips.append((v, .plain)) }
        for v in a.audio.prefix(2) { chips.append((v, .plain)) }
        return chips
    }

    /// 体积：站点给的字符串优先，否则按字节换算
    static func sizeText(_ hit: API.TorrentHit) -> String {
        if let size = hit.size, !size.isEmpty { return size }
        guard hit.sizeBytes > 0 else { return "" }
        let units = ["B", "KB", "MB", "GB", "TB", "PB"]
        var value = Double(hit.sizeBytes)
        var i = 0
        while value >= 1024, i < units.count - 1 { value /= 1024; i += 1 }
        return String(format: value >= 100 || i == 0 ? "%.0f %@" : "%.1f %@", value, units[i])
    }

    /// 秒级耗时文案（站点状态弹层）
    static func elapsedText(_ ms: Int) -> String { String(format: "%.1f 秒", Double(ms) / 1000) }

    private nonisolated(unsafe) static let isoFormatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    private nonisolated(unsafe) static let isoFractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    static func parseDate(_ raw: String) -> Date? {
        isoFormatter.date(from: raw) ?? isoFractional.date(from: raw) ?? isoFormatter.date(from: raw + "Z")
    }
}
