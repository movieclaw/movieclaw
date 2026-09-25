import Foundation

// 「刮削与整理」分区的静态数据与纯函数（对应 Web `scrape-settings-section.tsx` 顶部常量、
// describeScrapeValues、命名模板渲染器与轻校验）。
//
// 为什么全部放在一个无 UI 的文件里：这些规则都是 Web 的逐字镜像（常用语种表、档位集合、
// 模板渲染三步、校验文案），Web 改规则时这里要跟着改——集中在一处比散落在视图里好对照。
// 命名模板的渲染规则本身镜像自后端 `services/library/naming.py`，是「边打字边看结果」的预览，
// 真正落盘与保存校验以后端为准。

/// 排序芯片的候选项：id 是落库值（语言标签 / 地区码 / meta·orig·null 特殊 token）
struct SettingsBScrapeChipOption: Hashable {
    let id: String
    let name: String
    /// 特殊 token 的说明（Web 悬停提示，App 直接写在芯片下方）
    var tip: String?
}

enum SettingsBScrapeCatalog {
    /// 常用语种快捷芯片（元数据语言，值为带地区的语言标签）
    static let commonMetaLangs: [SettingsBScrapeChipOption] = [
        .init(id: "zh-CN", name: "中文（简体）"),
        .init(id: "zh-TW", name: "中文（繁體）"),
        .init(id: "en-US", name: "English"),
        .init(id: "ja-JP", name: "日本語"),
        .init(id: "ko-KR", name: "한국어"),
        .init(id: "fr-FR", name: "Français"),
    ]

    /// 图片语言的特殊 token + 常用语种（值为 TMDB 图片语言码）
    static let commonImageLangs: [SettingsBScrapeChipOption] = [
        .init(id: "meta", name: "跟随元数据主语言", tip: "引用「元数据」里语言优先级的第 1 位，改语言时选图自动跟随"),
        .init(id: "orig", name: "原始语言", tip: "作品的原声语言（随条目自动解析：《寄生虫》为韩语、日本动画为日语）"),
        .init(id: "en", name: "English"),
        .init(id: "null", name: "无文字", tip: "没有烧录任何文字的干净图（TMDB 语言标记为 null）"),
        .init(id: "ja", name: "日本語"),
    ]

    static let commonCertCountries: [SettingsBScrapeChipOption] = [
        .init(id: "CN", name: "中国"),
        .init(id: "US", name: "美国"),
        .init(id: "JP", name: "日本"),
        .init(id: "GB", name: "英国"),
    ]

    /// TMDB 图床合法档位（与后端 settings/metadata.py 一致；空串 = 跟随环境变量）
    static let posterSizes = ["w342", "w500", "w780", "original"]
    static let backdropSizes = ["w780", "w1280", "original"]
    static let stillSizes = ["w185", "w300", "original"]

    /// 「更多」面板的完整候选：从后端全量表派生，剔除已在常用行里的项（同 Web useScrapeChipOptions）
    static func extraMetaLangs(_ languages: [API.LanguageOption]) -> [SettingsBScrapeChipOption] {
        languages
            .filter { l in !commonMetaLangs.contains { $0.id.hasPrefix("\(l.code)-") } }
            .map { .init(id: $0.code, name: displayName($0)) }
    }

    static func extraImageLangs(_ languages: [API.LanguageOption]) -> [SettingsBScrapeChipOption] {
        languages
            .filter { l in !commonImageLangs.contains { $0.id == l.code } }
            .map { .init(id: $0.code, name: displayName($0)) }
    }

    static func extraCountries(_ countries: [API.CountryOption]) -> [SettingsBScrapeChipOption] {
        countries
            .filter { c in !commonCertCountries.contains { $0.id == c.code } }
            .map { .init(id: $0.code, name: $0.name) }
    }

    private static func displayName(_ l: API.LanguageOption) -> String {
        if !l.name.isEmpty { return l.name }
        if !l.englishName.isEmpty { return l.englishName }
        return l.code
    }
}

// MARK: - 折叠头摘要

/// 各卡片折叠头上的「人话」摘要（Web describeScrapeValues 的逐卡拆分）。
///
/// 不给原始值：折叠头要露的是「日本語 → 中文（简体）」而不是 `ja-JP → zh-CN`；
/// 未知语种/地区（从「更多」选的长尾项）回落显示代码本身，不编造名字——与 Web 同口径。
enum SettingsBScrapeSummary {
    private static func label(_ options: [SettingsBScrapeChipOption], _ id: String) -> String {
        options.first { $0.id == id }?.name ?? id
    }

    private static func join(_ options: [SettingsBScrapeChipOption], _ values: [String]) -> String {
        values.map { label(options, $0) }.joined(separator: " → ")
    }

    static func metaLanguage(_ s: API.MetadataScrapeSetting) -> String {
        join(SettingsBScrapeCatalog.commonMetaLangs, s.languagePriority)
    }

    static func certCountry(_ s: API.MetadataScrapeSetting) -> String {
        join(SettingsBScrapeCatalog.commonCertCountries, s.certCountryPriority)
    }

    /// 海报卡把「模式 + 语言优先级」并成一句：默认模式下语言优先级不生效，列出来只会误导
    static func poster(_ s: API.MetadataScrapeSetting) -> String {
        s.posterMode == "default"
            ? "TMDB 默认"
            : "按语言：\(join(SettingsBScrapeCatalog.commonImageLangs, s.posterLanguagePriority))"
    }

    static func backdrop(_ s: API.MetadataScrapeSetting) -> String {
        join(SettingsBScrapeCatalog.commonImageLangs, s.backdropLanguagePriority)
    }

    static func quality(_ s: API.MetadataScrapeSetting) -> String {
        let sizes = [s.posterSize, s.backdropSize, s.stillSize]
        return [
            s.posterMinWidth > 0 ? "海报 ≥\(s.posterMinWidth)" : "海报不限宽",
            s.backdropMinWidth > 0 ? "背景 ≥\(s.backdropMinWidth)" : "背景不限宽",
            sizes.allSatisfy(\.isEmpty) ? "档位跟随环境" : "档位 \(sizes.map { $0.isEmpty ? "环境" : $0 }.joined(separator: "/"))",
        ].joined(separator: " · ")
    }

    /// 四个模板并成一句：逐个列模板串太长，只说哪几项不是默认
    static func naming(_ s: API.MetadataScrapeSetting) -> String {
        let changed = SettingsBScrapeNaming.fields.filter { !s[keyPath: $0.keyPath].trimmingCharacters(in: .whitespaces).isEmpty }
        return changed.isEmpty ? "全部默认模板" : "已改：\(changed.map(\.label).joined(separator: "、"))"
    }

    static func mirror(_ s: API.MetadataScrapeSetting) -> String {
        let off = SettingsBScrapeMirrorRow.all.filter { !s[keyPath: $0.keyPath] }
        return off.isEmpty ? "三项全写" : "不写：\(off.map(\.label).joined(separator: "、"))"
    }
}

// MARK: - 目录写入三项

struct SettingsBScrapeMirrorRow {
    let key: String
    let label: String
    let hint: String
    let keyPath: WritableKeyPath<API.MetadataScrapeSetting, Bool>

    static let all: [SettingsBScrapeMirrorRow] = [
        .init(key: "mirror_images", label: "条目图片", hint: "poster.jpg / fanart.jpg / 季海报", keyPath: \.mirrorImages),
        .init(key: "mirror_nfo", label: "NFO 元数据", hint: "tvshow.nfo / movie.nfo / 分集 NFO，含 tmdbid 精确身份", keyPath: \.mirrorNfo),
        .init(key: "mirror_episode_thumbs", label: "分集剧照", hint: "每集一张 -thumb.jpg，长剧集写入量最大，可单独关闭", keyPath: \.mirrorEpisodeThumbs),
    ]
}

// MARK: - 命名模板

/// 命名模板：字段定义、预览样例、渲染器与轻校验。
///
/// 渲染三步与后端一一对应：① 整组丢弃占位符全空的括号组（收掉 "[tmdbid-]" 这种残留）
/// → ② 替换占位符 → ③ 收缩并清洗。改后端规则时 Web 与这里都要同步改。
enum SettingsBScrapeNaming {
    struct Field {
        /// 后端字段名（也用作 accessibilityIdentifier 后缀）
        let key: String
        let label: String
        let note: String
        let fallback: String
        let tokens: [String]
        let keyPath: WritableKeyPath<API.MetadataScrapeSetting, String>
    }

    static let commonTokens = ["title", "original_title", "year", "tmdb_id", "imdb_id"]
    static let fileAttrTokens = ["resolution", "media_source", "release_group"]

    static let fields: [Field] = [
        .init(key: "naming_entry_dir", label: "条目目录", note: "电影与剧集共用",
              fallback: "{title} ({year})", tokens: commonTokens, keyPath: \.namingEntryDir),
        .init(key: "naming_movie_file", label: "电影文件名", note: "",
              fallback: "{title} ({year})", tokens: commonTokens + fileAttrTokens, keyPath: \.namingMovieFile),
        .init(key: "naming_season_dir", label: "季目录", note: "必须包含 {season}",
              fallback: "Season {season:02d}", tokens: commonTokens + ["season"], keyPath: \.namingSeasonDir),
        .init(key: "naming_episode_file", label: "剧集文件名", note: "必须包含 {season} 与 {episode}",
              fallback: "{title} ({year}) - S{season:02d}E{episode:02d}",
              tokens: commonTokens + fileAttrTokens + ["season", "episode", "episode_title"], keyPath: \.namingEpisodeFile),
    ]

    /// 预览样例：一部电影 + 一集剧集，字段齐全便于看清每个占位符的效果（与 Web 同一组样例）
    static let sampleMovie: [String: String] = [
        "title": "沙丘：第二部",
        "original_title": "Dune: Part Two",
        "year": "2024",
        "tmdb_id": "693134",
        "imdb_id": "tt15239678",
        "resolution": "2160p",
        "media_source": "BluRay",
        "release_group": "FRDS",
    ]
    static let sampleEpisode: [String: String] = [
        "title": "风筝",
        "original_title": "风筝",
        "year": "2017",
        "tmdb_id": "68035",
        "imdb_id": "tt6952510",
        "season": "1",
        "episode": "3",
        "episode_title": "延安来的姑娘",
        "resolution": "1080p",
        "media_source": "WEB-DL",
        "release_group": "CHDWEB",
    ]

    // 与 Web 的正则逐字对应（JS 的 \w 只含 ASCII，这里显式写成 [A-Za-z0-9_]）
    private static let tokenRE = try! NSRegularExpression(pattern: #"\{([A-Za-z0-9_]+)(?::0(\d)d)?\}"#)
    private static let bracketGroupRE = try! NSRegularExpression(pattern: #"[(\[【][^()\[\]【】]*[)\]】]"#)
    private static let forbiddenRE = try! NSRegularExpression(pattern: #"[\\/:*?"<>|]"#)

    /// 模板里出现的占位符名（按出现顺序）
    static func tokens(in template: String) -> [String] {
        matches(tokenRE, template).compactMap { m in substring(template, m.range(at: 1)) }
    }

    /// 前端侧轻校验：与后端同口径，只为即时反馈；能否保存以后端返回为准
    static func error(for field: Field, template: String) -> String? {
        if template.trimmingCharacters(in: .whitespaces).isEmpty { return nil } // 空 = 用默认模板
        if template.contains("/") || template.contains("\\") { return "不能包含路径分隔符（目录层级是固定的）" }
        let used = tokens(in: template)
        let unknown = used.filter { !field.tokens.contains($0) }
        if !unknown.isEmpty {
            return "不可用的占位符：\(unknown.map { "{\($0)}" }.joined(separator: "、"))"
        }
        switch field.key {
        case "naming_entry_dir", "naming_movie_file":
            if !used.contains(where: { $0 == "title" || $0 == "original_title" }) {
                return "必须包含 {title} 或 {original_title}，否则不同影片会重名"
            }
        case "naming_season_dir":
            if !used.contains("season") { return "必须包含 {season}，否则不同季的同集号文件会互相覆盖" }
        case "naming_episode_file":
            if !(used.contains("season") && used.contains("episode")) {
                return "必须包含 {season} 与 {episode}，否则同一部剧的多集会互相覆盖"
            }
        default:
            break
        }
        return nil
    }

    /// 生效模板：留空时用默认模板
    static func effective(_ field: Field, in s: API.MetadataScrapeSetting) -> String {
        let value = s[keyPath: field.keyPath]
        return value.trimmingCharacters(in: .whitespaces).isEmpty ? field.fallback : value
    }

    static func render(_ template: String, _ ctx: [String: String]) -> String {
        // ① 占位符全空的括号组连同组内字面文本一起丢弃
        let dropped = replace(bracketGroupRE, in: template) { full, m in
            let group = substring(full, m.range) ?? ""
            let found = matches(tokenRE, group)
            if found.isEmpty { return group }
            let allEmpty = found.allSatisfy { m in
                tokenValue(ctx, substring(group, m.range(at: 1)) ?? "", substring(group, m.range(at: 2))).isEmpty
            }
            return allEmpty ? "" : group
        }
        // ② 替换占位符
        let filled = replace(tokenRE, in: dropped) { text, m in
            tokenValue(ctx, substring(text, m.range(at: 1)) ?? "", substring(text, m.range(at: 2)))
        }
        // ③ 收缩：括号内侧 → 重复分隔符 → 多余空白 → 首尾分隔符
        var collapsed = filled
        collapsed = regexReplace(#"([(\[【])[\s\-–]+"#, in: collapsed, with: "$1")
        collapsed = regexReplace(#"[\s\-–]+([)\]】])"#, in: collapsed, with: "$1")
        collapsed = regexReplace(#"(?:\s*-\s*){2,}"#, in: collapsed, with: " - ")
        collapsed = regexReplace(#"\s{2,}"#, in: collapsed, with: " ")
        collapsed = regexReplace(#"^[\s\-–.]+|[\s\-–.]+$"#, in: collapsed, with: "")
        let result = sanitize(collapsed)
        return result.isEmpty ? "未命名" : result
    }

    private static func sanitize(_ value: String) -> String {
        var v = forbiddenRE.stringByReplacingMatches(in: value, range: NSRange(value.startIndex..., in: value), withTemplate: " ")
        v = regexReplace(#"\s+"#, in: v, with: " ")
        return regexReplace(#"^[\s.]+|[\s.]+$"#, in: v, with: "")
    }

    private static func tokenValue(_ ctx: [String: String], _ name: String, _ pad: String?) -> String {
        guard let raw = ctx[name], !raw.isEmpty else { return "" }
        var text = sanitize(raw)
        if let pad, let width = Int(pad), !text.isEmpty, text.allSatisfy({ $0.isASCII && $0.isNumber }), text.count < width {
            text = String(repeating: "0", count: width - text.count) + text
        }
        return text
    }

    // MARK: 正则小工具

    private static func matches(_ re: NSRegularExpression, _ text: String) -> [NSTextCheckingResult] {
        re.matches(in: text, range: NSRange(text.startIndex..., in: text))
    }

    private static func substring(_ text: String, _ range: NSRange) -> String? {
        guard range.location != NSNotFound, let r = Range(range, in: text) else { return nil }
        return String(text[r])
    }

    /// 回调式替换（JS 的 `replace(re, fn)`）：倒序替换，避免前面的替换改变后面的偏移。
    /// transform 收到原串与匹配结果（捕获组区间相对原串）
    private static func replace(_ re: NSRegularExpression, in text: String, _ transform: (String, NSTextCheckingResult) -> String) -> String {
        var result = text
        for m in matches(re, text).reversed() {
            guard let r = Range(m.range, in: text) else { continue }
            let replacement = transform(text, m)
            // 在原串上取区间，再按相同 utf16 偏移写回 result（倒序保证前段偏移不变）
            if let target = Range(NSRange(r, in: text), in: result) {
                result.replaceSubrange(target, with: replacement)
            }
        }
        return result
    }

    private static func regexReplace(_ pattern: String, in text: String, with template: String) -> String {
        guard let re = try? NSRegularExpression(pattern: pattern) else { return text }
        return re.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: template)
    }
}
