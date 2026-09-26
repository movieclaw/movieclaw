import SwiftUI

/// 「编辑库 → 刮削设置」（Web `library-scrape-settings.tsx`，设计见 docs/design/scrape-customization.md §14.5）。
///
/// 与全局设置页共用同一套卡片内控件（设置模块的 `SettingsBScrape*`），差别在外层：这里是手风琴，
/// 每张卡的折叠头直接写「跟随全局：…」/「自定义：…」——「我这个库到底改了哪几项」是这个页面最该一眼回答的问题。
///
/// 三态怎么表达（同 Web）：
/// - 排序芯片与数字输入没有「空值」可表达跟随，用**卡片级**「跟随全局 / 自定义」开关；切到自定义时拿全局当前值做种子，
///   跟随时控件只读（看得见全局值，改不动）；
/// - 命名模板留空即跟随；
/// - 目录写入是「跟随全局 / 开 / 关」三档。
///
/// 覆盖对象的语义与后端一致：**只存显式覆盖的字段**，空对象 = 全跟全局。
/// 实现上把全局设置编码成 JSON 字典、叠加本库覆盖再解回 `MetadataScrapeSetting` 交给共用控件渲染；
/// 控件改动后只把该卡片管的字段写回覆盖对象。
struct ManageScrapeOverrides: View {
    @Binding var overrides: [String: API.JSONValue]

    @Environment(\.api) private var api
    @State private var config: API.ScrapeConfigView?
    @State private var error: String?
    @State private var open: String?
    @State private var languages: [API.LanguageOption] = []
    @State private var countries: [API.CountryOption] = []

    /// 卡片级三态的五张卡
    private static let followCards: [SettingsBScrapeCard] = [.metaLanguage, .certCountry, .poster, .backdrop, .quality]

    /// 卡片所在分节的标题（只在该节第一张卡前出现）
    private static func groupTitle(before card: SettingsBScrapeCard) -> String? {
        switch card {
        case .metaLanguage: "元数据"
        case .poster: "图片"
        default: nil
        }
    }

    private static let namingFields: [(key: String, label: String, fallback: String)] = [
        ("naming_entry_dir", "条目目录", "{title} ({year})"),
        ("naming_movie_file", "电影文件名", "{title} ({year})"),
        ("naming_season_dir", "季目录", "Season {season:02d}"),
        ("naming_episode_file", "剧集文件名", "{title} ({year}) - S{season:02d}E{episode:02d}"),
    ]

    private static let mirrorFields: [(key: String, label: String)] = [
        ("mirror_images", "写入条目图片"),
        ("mirror_nfo", "写入 NFO 元数据"),
        ("mirror_episode_thumbs", "写入分集剧照"),
    ]

    /// 全局基线：与设置页同一套「编辑态用生效值起步」的口径
    private var base: API.MetadataScrapeSetting? {
        guard var setting = config?.setting else { return nil }
        if setting.languagePriority.isEmpty, let effective = config?.effective.languagePriority {
            setting.languagePriority = effective
        }
        return setting
    }

    private var baseDict: [String: API.JSONValue] { base.flatMap(Self.encode) ?? [:] }

    /// 交给共用控件渲染的完整设置 = 全局基线叠加本库覆盖
    private var merged: API.MetadataScrapeSetting? {
        guard base != nil else { return nil }
        return Self.decode(baseDict.merging(overrides) { _, new in new })
    }

    var body: some View {
        if let error {
            Text(error).font(.subheadline).foregroundStyle(Theme.textMuted)
        } else if let merged, let base {
            content(merged: merged, base: base)
        } else {
            HStack(spacing: 10) {
                ProgressView()
                Text("正在加载全局刮削设置…").font(.subheadline).foregroundStyle(Theme.textMuted)
            }
            .task { await load() }
        }
    }

    @ViewBuilder
    private func content(merged: API.MetadataScrapeSetting, base: API.MetadataScrapeSetting) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("本库单独的刮削口味，未显式修改的项跟随「设置 → 刮削与整理」。")
                .font(.footnote)
                .foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
            if !overrides.isEmpty {
                Text("已覆盖 \(overrides.count) 项")
                    .font(.caption2)
                    .foregroundStyle(Theme.accent)
                    .padding(.horizontal, 7).padding(.vertical, 2)
                    .background(Theme.accentSoft, in: .capsule)
                    .fixedSize()
            }
        }
        // 一键回到全跟随：否则想撤掉本库的全部个性化得逐张卡切「跟随全局」+ 逐个模板清空
        if !overrides.isEmpty {
            Button("全部恢复跟随") { overrides = [:] }
                .font(.footnote)
                .accessibilityIdentifier("form-scrape-reset")
        }

        // 四个分节同 Web ScrapeSection：元数据 / 图片 / 命名与整理 / 目录写入
        ForEach(Self.followCards) { card in
            if let group = Self.groupTitle(before: card) { ManageScrapeGroupTitle(text: group) }
            followCard(card, merged: merged, base: base)
        }
        ManageScrapeGroupTitle(text: "命名与整理")
        namingCard(base: base)
        ManageScrapeGroupTitle(text: "目录写入")
        mirrorCard(base: base)

        Text("语言与选图的产物挂在条目上（一部片一份档案、一张海报），所以它们按条目的\(Text("刮削归属库").foregroundStyle(Theme.textMuted).fontWeight(.medium))生效——归属本库的条目才跟这里的设置。存量条目需在本库执行「刷新元数据」后按新设置重刮。")
            .font(.caption)
            .foregroundStyle(Theme.textFaint)
            .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: 卡片级三态

    @ViewBuilder
    private func followCard(_ card: SettingsBScrapeCard, merged: API.MetadataScrapeSetting, base: API.MetadataScrapeSetting) -> some View {
        let custom = card.keys.contains { overrides[$0] != nil }
        cardHeader(id: card.rawValue, title: card.title, customized: custom,
                   status: custom ? "自定义：\(card.summary(merged))" : "跟随全局：\(card.summary(base))")
        if open == card.rawValue {
            Text(card.desc).font(.footnote).foregroundStyle(Theme.textMuted).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 10) {
                Text("全局：\(card.summary(base))").font(.caption).foregroundStyle(Theme.textFaint).lineLimit(2)
                Spacer(minLength: 4)
                Picker("跟随", selection: Binding(
                    get: { custom },
                    set: { next in
                        var patched = overrides
                        for key in card.keys {
                            if next { patched[key] = baseDict[key] } else { patched.removeValue(forKey: key) }
                        }
                        overrides = patched
                    }
                )) {
                    Text("跟随全局").tag(false)
                    Text("自定义").tag(true)
                }
                .pickerStyle(.segmented)
                .fixedSize()
                .accessibilityIdentifier("form-scrape-follow-\(card.rawValue)")
            }
            // 跟随全局时控件只读：看得见全局值长什么样，但改不动
            Group {
                switch card {
                case .metaLanguage:
                    SettingsBScrapeOrderChips(
                        options: SettingsBScrapeCatalog.commonMetaLangs,
                        extraOptions: SettingsBScrapeCatalog.extraMetaLangs(languages),
                        moreLabel: "语言", value: binding(card)[dynamicMember: \.languagePriority],
                        max: 3, primaryTag: "主语言", identifier: "form-scrape-meta-lang"
                    )
                case .certCountry:
                    SettingsBScrapeOrderChips(
                        options: SettingsBScrapeCatalog.commonCertCountries,
                        extraOptions: SettingsBScrapeCatalog.extraCountries(countries),
                        moreLabel: "地区", value: binding(card)[dynamicMember: \.certCountryPriority],
                        max: 6, primaryTag: "", identifier: "form-scrape-cert-country"
                    )
                case .poster:
                    SettingsBScrapePosterRows(setting: binding(card), extraImageLangs: SettingsBScrapeCatalog.extraImageLangs(languages))
                case .backdrop:
                    SettingsBScrapeOrderChips(
                        options: SettingsBScrapeCatalog.commonImageLangs,
                        extraOptions: SettingsBScrapeCatalog.extraImageLangs(languages),
                        moreLabel: "语言", value: binding(card)[dynamicMember: \.backdropLanguagePriority],
                        max: 4, primaryTag: "首选", identifier: "form-scrape-backdrop-lang"
                    )
                case .quality:
                    SettingsBScrapeQualityRows(setting: binding(card), effective: config?.effective)
                default:
                    EmptyView()
                }
            }
            .disabled(!custom)
            .opacity(custom ? 1 : 0.45)
        }
    }

    /// 一张卡的设置绑定：读合成值；写时只把这张卡管的字段写回覆盖对象
    private func binding(_ card: SettingsBScrapeCard) -> Binding<API.MetadataScrapeSetting> {
        Binding(
            get: { merged ?? base ?? Self.emptySetting },
            set: { next in
                guard let dict = Self.encode(next) else { return }
                var patched = overrides
                for key in card.keys { patched[key] = dict[key] }
                overrides = patched
            }
        )
    }

    // MARK: 字段级三态

    @ViewBuilder
    private func namingCard(base: API.MetadataScrapeSetting) -> some View {
        let hit = Self.namingFields.filter { overrides[$0.key] != nil }
        cardHeader(id: "naming", title: "命名模板", customized: !hit.isEmpty,
                   status: hit.isEmpty ? "跟随全局" : "自定义：\(hit.map(\.label).joined(separator: "、"))")
        if open == "naming" {
            Text("留空即跟随全局模板。命名的产物是本库目录树里的路径，所以每个库可以各用一套。")
                .font(.footnote).foregroundStyle(Theme.textMuted).fixedSize(horizontal: false, vertical: true)
            ForEach(Self.namingFields, id: \.key) { field in
                let globalValue = baseDict[field.key]?.stringValue ?? ""
                VStack(alignment: .leading, spacing: 4) {
                    Text(field.label).font(.caption).foregroundStyle(Theme.textMuted)
                    TextField("跟随全局（\(globalValue.isEmpty ? field.fallback : globalValue)）", text: Binding(
                        get: { overrides[field.key]?.stringValue ?? "" },
                        set: { value in
                            if value.trimmingCharacters(in: .whitespaces).isEmpty {
                                overrides.removeValue(forKey: field.key)
                            } else {
                                overrides[field.key] = .string(value)
                            }
                        }
                    ))
                    .font(.footnote.monospaced())
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .accessibilityIdentifier("form-scrape-\(field.key)")
                }
            }
        }
    }

    @ViewBuilder
    private func mirrorCard(base: API.MetadataScrapeSetting) -> some View {
        let hit = Self.mirrorFields.filter { overrides[$0.key] != nil }
        cardHeader(id: "mirror", title: "媒体目录写入", customized: !hit.isEmpty,
                   status: hit.isEmpty ? "跟随全局" : "自定义：\(hit.map(\.label).joined(separator: "、"))")
        if open == "mirror" {
            Text("把刮削成果写入本库的媒体目录。基本信息里的「刮削图片/NFO 写入媒体目录」是总闸，关掉则这三项全不写。")
                .font(.footnote).foregroundStyle(Theme.textMuted).fixedSize(horizontal: false, vertical: true)
            ForEach(Self.mirrorFields, id: \.key) { field in
                let globalOn = baseDict[field.key]?.boolValue ?? true
                VStack(alignment: .leading, spacing: 6) {
                    Text(field.label).font(.subheadline)
                    Picker(field.label, selection: Binding<Int>(
                        get: {
                            guard let value = overrides[field.key]?.boolValue else { return 0 }
                            return value ? 1 : 2
                        },
                        set: { choice in
                            switch choice {
                            case 1: overrides[field.key] = .bool(true)
                            case 2: overrides[field.key] = .bool(false)
                            default: overrides.removeValue(forKey: field.key)
                            }
                        }
                    )) {
                        Text("跟随全局（\(globalOn ? "开" : "关")）").tag(0)
                        Text("开").tag(1)
                        Text("关").tag(2)
                    }
                    .pickerStyle(.segmented)
                    .accessibilityIdentifier("form-scrape-\(field.key)")
                }
            }
        }
    }

    // MARK: 折叠头

    private func cardHeader(id: String, title: String, customized: Bool, status: String) -> some View {
        Button {
            withAnimation(.snappy) { open = open == id ? nil : id }
        } label: {
            HStack(spacing: 10) {
                Circle().fill(customized ? Theme.accent : Color.white.opacity(0.2)).frame(width: 6, height: 6)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text)
                    Text(status)
                        .font(.caption)
                        .foregroundStyle(customized ? Theme.text : Theme.textFaint)
                        .lineLimit(1)
                }
                Spacer(minLength: 6)
                Image(systemName: "chevron.down")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Theme.textFaint)
                    .rotationEffect(.degrees(open == id ? 180 : 0))
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("form-scrape-card-\(id)")
    }

    // MARK: 数据

    private func load() async {
        do {
            config = try await api.scrapeShow()
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription.isEmpty ? "全局刮削设置加载失败" : error.localizedDescription
        }
        // 语种 / 地区全量表拉不到不阻断（「更多」面板回落只显示常用项）
        languages = (try? await api.scrapeLanguages()) ?? []
        countries = (try? await api.scrapeCountries()) ?? []
    }

    /// 仅作绑定兜底（加载成功前不会渲染卡片）
    private static let emptySetting = API.MetadataScrapeSetting(
        languagePriority: [], certCountryPriority: [], posterMode: "default",
        posterLanguagePriority: [], backdropLanguagePriority: [], posterMinWidth: 0, backdropMinWidth: 0,
        posterSize: "", backdropSize: "", stillSize: "",
        namingEntryDir: "", namingMovieFile: "", namingSeasonDir: "", namingEpisodeFile: "",
        mirrorImages: true, mirrorNfo: true, mirrorEpisodeThumbs: true
    )

    static func encode(_ setting: API.MetadataScrapeSetting) -> [String: API.JSONValue]? {
        guard let data = try? JSONEncoder().encode(setting) else { return nil }
        return try? JSONDecoder().decode([String: API.JSONValue].self, from: data)
    }

    static func decode(_ dict: [String: API.JSONValue]) -> API.MetadataScrapeSetting? {
        guard let data = try? JSONEncoder().encode(dict) else { return nil }
        return try? JSONDecoder().decode(API.MetadataScrapeSetting.self, from: data)
    }
}

/// 刮削覆盖里的分节标题（同 Web ScrapeSection 的小标题）
struct ManageScrapeGroupTitle: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.caption.weight(.semibold))
            .foregroundStyle(Theme.textFaint)
            .padding(.top, 6)
            .accessibilityAddTraits(.isHeader)
    }
}
