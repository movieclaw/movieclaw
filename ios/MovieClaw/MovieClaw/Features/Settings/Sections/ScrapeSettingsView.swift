import SwiftUI

/// 设置 → 刮削与整理（对应 Web `scrape-settings-section.tsx` 的 `ScrapeSettingsSection`）。
///
/// 结构与 Web 一致：四个**并列**分节（元数据 / 图片 / 命名与整理 / 目录写入，按刮削管线先后排，
/// 但互相没有依赖所以不编号），每节若干张**手风琴卡片**——默认全收起、同时只开一张，
/// 折叠头直接摊出当前值的人话摘要，一屏扫完全站配置；被媒体库覆盖的卡片标「N 个库已覆盖」，
/// 回答「在全局改了为什么不生效」。
///
/// 保存交互照搬 Web：整页一个草稿，改任意一项即置脏，底部「保存」整体 PUT（`PUT /scrape/config`），
/// 未改动时保存键禁用。编辑态用「生效值」起步：语言优先级跟随环境变量（空列表）时显示当前生效的语言，
/// 不改就不保存，语义不变。
struct ScrapeSettingsView: View {
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback

    @State private var state: Loadable<API.ScrapeConfigView> = .loading
    @State private var draft: API.MetadataScrapeSetting?
    @State private var dirty = false
    @State private var saving = false
    /// 展开的卡片（同时只开一张；默认全收起）
    @State private var open: SettingsBScrapeCard?
    @State private var languages: [API.LanguageOption] = []
    @State private var countries: [API.CountryOption] = []
    /// 各库的覆盖情况（库名 + 覆盖了哪些字段）
    @State private var overrides: [SettingsBScrapeOverride] = []

    var body: some View {
        Group {
            switch state {
            case .loading:
                HStack(spacing: 10) {
                    ProgressView()
                    Text("正在加载刮削配置…").foregroundStyle(Theme.textMuted)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .accessibilityIdentifier("loading")
            case let .failed(message):
                ErrorState(message: message, retry: load)
            case let .loaded(view):
                form(view)
            }
        }
        .appBackground()
        .task {
            await load()
            await loadAuxiliary()
        }
    }

    // MARK: 页面

    private func form(_ view: API.ScrapeConfigView) -> some View {
        Form {
            Section {
                // 「可按库覆盖」在分区顶部统一说一句，不逐卡贴徽标（同 Web）
                Text("全站默认的刮削口味。\(Text("任意一项").foregroundStyle(Theme.text).fontWeight(.medium))都可以在媒体库的「编辑库 → 刮削设置」里单独覆盖，没被覆盖的库跟随这里。")
                    .font(.footnote)
                    .foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Section("元数据") {
                card(.metaLanguage) {
                    SettingsBScrapeOrderChips(
                        options: SettingsBScrapeCatalog.commonMetaLangs,
                        extraOptions: SettingsBScrapeCatalog.extraMetaLangs(languages),
                        moreLabel: "语言",
                        value: binding(\.languagePriority),
                        max: 3,
                        primaryTag: "主语言",
                        identifier: "scrape-meta-lang"
                    )
                }
                card(.certCountry) {
                    SettingsBScrapeOrderChips(
                        options: SettingsBScrapeCatalog.commonCertCountries,
                        extraOptions: SettingsBScrapeCatalog.extraCountries(countries),
                        moreLabel: "地区",
                        value: binding(\.certCountryPriority),
                        max: 6,
                        primaryTag: "",
                        identifier: "scrape-cert-country"
                    )
                }
            }

            Section("图片") {
                card(.poster) {
                    SettingsBScrapePosterRows(setting: settingBinding, extraImageLangs: SettingsBScrapeCatalog.extraImageLangs(languages))
                }
                card(.backdrop) {
                    SettingsBScrapeOrderChips(
                        options: SettingsBScrapeCatalog.commonImageLangs,
                        extraOptions: SettingsBScrapeCatalog.extraImageLangs(languages),
                        moreLabel: "语言",
                        value: binding(\.backdropLanguagePriority),
                        max: 4,
                        primaryTag: "首选",
                        identifier: "scrape-backdrop-lang"
                    )
                }
                card(.quality) {
                    SettingsBScrapeQualityRows(setting: settingBinding, effective: view.effective)
                }
            }

            Section("命名与整理") {
                card(.naming) {
                    SettingsBScrapeNamingRows(setting: settingBinding)
                }
            }

            Section("目录写入") {
                card(.mirror) {
                    SettingsBScrapeMirrorRows(setting: settingBinding)
                }
            }

            Section {
                HStack(spacing: 12) {
                    Text("保存后对新刮削立即生效；存量条目在媒体库页执行「刷新元数据」后按新配置更新。")
                        .font(.caption)
                        .foregroundStyle(Theme.textFaint)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    Button {
                        Task { await save() }
                    } label: {
                        Text(saving ? "保存中…" : "保存").font(.body.weight(.semibold)).padding(.horizontal, 6)
                    }
                    .discoverProminentButton()
                    .disabled(!dirty || saving)
                    .accessibilityIdentifier("scrape-save")
                }
            }
        }
        .settingsBFormStyle()
    }

    /// 一张手风琴卡片：折叠头行 + 展开后的说明与内容行
    @ViewBuilder
    private func card<Content: View>(_ card: SettingsBScrapeCard, @ViewBuilder content: () -> Content) -> some View {
        let isOpen = open == card
        let overriddenBy = overrides.filter { !$0.keys.isDisjoint(with: card.keys) }.map(\.name)
        Button {
            withAnimation(.snappy) { open = isOpen ? nil : card }
        } label: {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 8) {
                        Text(card.title).font(.body.weight(.semibold)).foregroundStyle(Theme.text)
                        // 收起时也要看得见「这项被几个库改了」——它正是「在全局改了不生效」的答案
                        if !overriddenBy.isEmpty {
                            SettingsBBadge(text: "\(overriddenBy.count) 个库已覆盖", tone: .accent)
                        }
                    }
                    if let draft {
                        Text(card.summary(draft))
                            .font(.caption)
                            .foregroundStyle(Theme.textFaint)
                            .lineLimit(1)
                            .truncationMode(.tail)
                    }
                }
                Spacer(minLength: 8)
                Image(systemName: "chevron.down")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(Theme.textFaint)
                    .rotationEffect(.degrees(isOpen ? 180 : 0))
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("scrape-card-\(card.rawValue)")
        .accessibilityAddTraits(isOpen ? .isSelected : [])

        if isOpen {
            VStack(alignment: .leading, spacing: 6) {
                Text(card.desc)
                    .font(.footnote)
                    .foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
                if !overriddenBy.isEmpty {
                    Text("\(overriddenBy.joined(separator: "、"))不跟随此处的设置")
                        .font(.caption)
                        .foregroundStyle(Theme.accent)
                }
            }
            .padding(.vertical, 2)
            content()
        }
    }

    // MARK: 草稿绑定

    /// 整份草稿的绑定：任何写入都置脏（Web 的 patch）
    private var settingBinding: Binding<API.MetadataScrapeSetting> {
        Binding(
            get: { draft ?? Self.emptySetting },
            set: { newValue in
                guard draft != newValue else { return }
                draft = newValue
                dirty = true
            }
        )
    }

    private func binding<T>(_ keyPath: WritableKeyPath<API.MetadataScrapeSetting, T>) -> Binding<T> {
        settingBinding[dynamicMember: keyPath]
    }

    /// 仅作绑定的兜底值（加载成功前不会渲染表单）
    private static let emptySetting = API.MetadataScrapeSetting(
        languagePriority: [], certCountryPriority: [], posterMode: "default",
        posterLanguagePriority: [], backdropLanguagePriority: [], posterMinWidth: 0, backdropMinWidth: 0,
        posterSize: "", backdropSize: "", stillSize: "",
        namingEntryDir: "", namingMovieFile: "", namingSeasonDir: "", namingEpisodeFile: "",
        mirrorImages: true, mirrorNfo: true, mirrorEpisodeThumbs: true
    )

    // MARK: 数据

    private func load() async {
        do {
            let config = try await api.scrapeShow()
            var setting = config.setting
            // 编辑态用「生效值」起步：跟随环境变量的空列表在界面上就是当前生效的语言
            if setting.languagePriority.isEmpty {
                setting.languagePriority = config.effective.languagePriority
            }
            draft = setting
            dirty = false
            state = .loaded(config)
        } catch is CancellationError {
        } catch {
            state = .failed(error.localizedDescription.isEmpty ? "加载失败，请重试" : error.localizedDescription)
        }
    }

    /// 语种/地区全量表与库覆盖情况：拉不到都不阻断（面板回落只显示常用项、不标覆盖徽标）
    private func loadAuxiliary() async {
        async let langs = try? api.scrapeLanguages()
        async let ctrs = try? api.scrapeCountries()
        async let libs = try? api.libraryList()
        languages = await langs ?? []
        countries = await ctrs ?? []
        overrides = (await libs ?? [])
            .map { SettingsBScrapeOverride(name: $0.name, keys: Set($0.scrapeOverrides.keys)) }
            .filter { !$0.keys.isEmpty }
    }

    private func save() async {
        guard let s = draft else { return }
        saving = true
        defer { saving = false }
        do {
            let config = try await api.scrapeSet(body: API.MetadataScrapeSettingInput(
                languagePriority: s.languagePriority,
                certCountryPriority: s.certCountryPriority,
                posterMode: s.posterMode,
                posterLanguagePriority: s.posterLanguagePriority,
                backdropLanguagePriority: s.backdropLanguagePriority,
                posterMinWidth: s.posterMinWidth,
                backdropMinWidth: s.backdropMinWidth,
                posterSize: s.posterSize,
                backdropSize: s.backdropSize,
                stillSize: s.stillSize,
                namingEntryDir: s.namingEntryDir,
                namingMovieFile: s.namingMovieFile,
                namingSeasonDir: s.namingSeasonDir,
                namingEpisodeFile: s.namingEpisodeFile,
                mirrorImages: s.mirrorImages,
                mirrorNfo: s.mirrorNfo,
                mirrorEpisodeThumbs: s.mirrorEpisodeThumbs
            ))
            state = .loaded(config)
            draft = config.setting
            dirty = false
            feedback.success("已保存。语言与图片对存量条目生效需在媒体库执行整库刷新")
        } catch {
            feedback.error(error.localizedDescription.isEmpty ? "保存失败，请重试" : error.localizedDescription)
        }
    }
}

/// 某个媒体库覆盖了哪些刮削字段（卡片上的「N 个库已覆盖」由它判定）
struct SettingsBScrapeOverride {
    let name: String
    let keys: Set<String>
}
