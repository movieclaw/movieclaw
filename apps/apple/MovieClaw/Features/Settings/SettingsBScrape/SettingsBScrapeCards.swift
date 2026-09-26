import SwiftUI

// 「刮削与整理」各卡片的内容（对应 Web MetaTab / ImagesTab / NamingTab / MirrorTab 的卡片内部控件）。
// 外层折叠壳（标题、摘要、N 个库已覆盖、展开箭头）在 ScrapeSettingsView 里统一画，
// 这里只负责展开后的表单行——每个视图返回若干 Form 行，直接嵌进同一个 Section。

/// 卡片清单：顺序、标题、说明、覆盖判定所用的后端字段名都与 Web 一致
enum SettingsBScrapeCard: String, CaseIterable, Identifiable {
    case metaLanguage, certCountry, poster, backdrop, quality, naming, mirror

    var id: String { rawValue }

    var title: String {
        switch self {
        case .metaLanguage: "元数据语言"
        case .certCountry: "内容分级"
        case .poster: "海报"
        case .backdrop: "背景图（fanart）"
        case .quality: "质量与门槛"
        case .naming: "命名模板"
        case .mirror: "媒体目录写入"
        }
    }

    var desc: String {
        switch self {
        case .metaLanguage:
            "标题、简介、类型名等文本的语言。点选语言即加入优先级，第 1 位是主语言（决定向 TMDB 请求的语言），缺失的字段按顺序回落——回落基于已拉取的翻译数据，不产生额外请求。"
        case .certCountry:
            "条目分级（如 PG-13、TV-MA）按顺序取第一个有数据的地区。"
        case .poster:
            "海报和文本一样有语言：中文版、原版、无文字干净版是不同的候选图。你在条目详情页手动选定的图始终优先，不受这里影响。"
        case .backdrop:
            "铺在详情页全屏的沉浸底图。「无文字」是没有烧录任何片名文字的干净图——排第 1 位即无文字优先；想要带片名 logo 的横图，把语言排到前面。"
        case .quality:
            "分辨率门槛过滤模糊候选图；质量档位决定下载到本地的图片尺寸，调低可显著节省磁盘，改动后整库刷新会按新档位自动重下。"
        case .naming:
            "整理与入库的目录/文件命名。留空即使用默认模板；字段缺失时会连同相邻括号自动收缩。目录层级固定为「条目目录 / 季目录 / 文件」，不可自定义。"
        case .mirror:
            "把刮削成果写入媒体目录，反哺 Emby / Jellyfin / Kodi（文件名遵循播放器规范）。只增不删除；已存在的 NFO 绝不覆盖。每个媒体库还有一个总开关，关掉则该库三项都不写。"
        }
    }

    /// 本卡片管的后端字段（用于判定「N 个库已覆盖」）
    var keys: [String] {
        switch self {
        case .metaLanguage: ["language_priority"]
        case .certCountry: ["cert_country_priority"]
        case .poster: ["poster_mode", "poster_language_priority"]
        case .backdrop: ["backdrop_language_priority"]
        case .quality: ["poster_min_width", "backdrop_min_width", "poster_size", "backdrop_size", "still_size"]
        case .naming: SettingsBScrapeNaming.fields.map(\.key)
        case .mirror: SettingsBScrapeMirrorRow.all.map(\.key)
        }
    }

    func summary(_ s: API.MetadataScrapeSetting) -> String {
        switch self {
        case .metaLanguage: SettingsBScrapeSummary.metaLanguage(s)
        case .certCountry: SettingsBScrapeSummary.certCountry(s)
        case .poster: SettingsBScrapeSummary.poster(s)
        case .backdrop: SettingsBScrapeSummary.backdrop(s)
        case .quality: SettingsBScrapeSummary.quality(s)
        case .naming: SettingsBScrapeSummary.naming(s)
        case .mirror: SettingsBScrapeSummary.mirror(s)
        }
    }
}

// MARK: - 海报模式

/// 海报：两种选图模式（单选）+ 按语言模式下才可编辑的语言优先级
struct SettingsBScrapePosterRows: View {
    @Binding var setting: API.MetadataScrapeSetting
    let extraImageLangs: [SettingsBScrapeChipOption]

    private let modes: [(id: String, title: String, desc: String)] = [
        ("default", "TMDB 默认", "与发现页看到的一致，订阅前后海报不跳变（默认）"),
        ("language", "按语言优先级挑选", "逐级取第一档有候选图的语言，档内按分辨率与票数排序"),
    ]

    var body: some View {
        ForEach(modes, id: \.id) { mode in
            Button {
                setting.posterMode = mode.id
            } label: {
                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(mode.title).font(.body.weight(.semibold)).foregroundStyle(Theme.text)
                        Text(mode.desc).font(.caption).foregroundStyle(Theme.textFaint)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 8)
                    Image(systemName: setting.posterMode == mode.id ? "checkmark.circle.fill" : "circle")
                        .foregroundStyle(setting.posterMode == mode.id ? Theme.accent : Theme.textFaint)
                        .font(.title3)
                }
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("scrape-poster-mode-\(mode.id)")
            .accessibilityAddTraits(setting.posterMode == mode.id ? .isSelected : [])
        }
        SettingsBScrapeOrderChips(
            options: SettingsBScrapeCatalog.commonImageLangs,
            extraOptions: extraImageLangs,
            moreLabel: "语言",
            value: $setting.posterLanguagePriority,
            max: 4,
            primaryTag: "首选",
            identifier: "scrape-poster-lang"
        )
        // 默认模式下语言优先级不生效：看得见但改不动（同 Web 的半透明 + 禁点）
        .disabled(setting.posterMode != "language")
        .opacity(setting.posterMode == "language" ? 1 : 0.4)
    }
}

// MARK: - 质量与门槛

struct SettingsBScrapeQualityRows: View {
    @Binding var setting: API.MetadataScrapeSetting
    /// 「跟随环境（当前 xxx）」里的当前生效档位
    let effective: API.ScrapeEffectiveView?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text("最低分辨率门槛").font(.body.weight(.medium))
                Text("低于门槛的候选图不选；候选全部不达标时自动放宽")
                    .font(.caption).foregroundStyle(Theme.textFaint)
            }
            widthField("海报", \.posterMinWidth, id: "scrape-poster-min-width")
            widthField("背景", \.backdropMinWidth, id: "scrape-backdrop-min-width")
        }
        .padding(.vertical, 4)

        VStack(alignment: .leading, spacing: 4) {
            Text("图片质量档位").font(.body.weight(.medium))
            sizePicker("海报", \.posterSize, SettingsBScrapeCatalog.posterSizes, effective?.posterSize, id: "scrape-poster-size")
            sizePicker("背景", \.backdropSize, SettingsBScrapeCatalog.backdropSizes, effective?.backdropSize, id: "scrape-backdrop-size")
            sizePicker("剧照", \.stillSize, SettingsBScrapeCatalog.stillSizes, effective?.stillSize, id: "scrape-still-size")
        }
        .padding(.vertical, 4)
    }

    /// 宽度门槛输入：直接绑字符串代理，边打字边写回（空 / 非数字 = 0 = 不限制）
    private func widthField(_ label: String, _ keyPath: WritableKeyPath<API.MetadataScrapeSetting, Int>, id: String) -> some View {
        HStack(spacing: 8) {
            Text("\(label) ≥").foregroundStyle(Theme.textMuted)
            TextField("0", text: Binding(
                get: { String(setting[keyPath: keyPath]) },
                set: { setting[keyPath: keyPath] = Int($0.filter(\.isNumber)) ?? 0 }
            ))
            .keyboardType(.numberPad)
            .monospacedDigit()
            .frame(width: 90)
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(Color.white.opacity(0.05), in: .rect(cornerRadius: 8))
            .accessibilityIdentifier(id)
            // 0 在输入框里看不出是「不限制」还是「没填」，补一句
            if setting[keyPath: keyPath] == 0 {
                Text("不限制").font(.caption).foregroundStyle(Theme.textFaint)
            }
            Spacer()
        }
    }

    private func sizePicker(
        _ label: String,
        _ keyPath: WritableKeyPath<API.MetadataScrapeSetting, String>,
        _ sizes: [String],
        _ fallback: String?,
        id: String
    ) -> some View {
        Picker(label, selection: $setting[dynamicMember: keyPath]) {
            Text("跟随环境（\(fallback ?? "")）").tag("")
            ForEach(sizes, id: \.self) { Text($0).tag($0) }
        }
        .pickerStyle(.menu)
        .accessibilityIdentifier(id)
    }
}

// MARK: - 命名模板

/// 命名模板：四个模板输入 + 占位符点击插入 + 实时预览 + 恢复默认。
///
/// 占位符插入到「最后聚焦的输入框」的光标处（默认剧集文件名，同 Web），
/// 用 iOS 18 起的 `TextField(selection:)` 拿光标；插入后光标移到占位符之后并保持聚焦。
struct SettingsBScrapeNamingRows: View {
    @Binding var setting: API.MetadataScrapeSetting

    @FocusState private var focusedKey: String?
    @State private var lastFocused = "naming_episode_file"
    @State private var selections: [String: TextSelection] = [:]

    private var fields: [SettingsBScrapeNaming.Field] { SettingsBScrapeNaming.fields }
    private var errors: [String?] { fields.map { SettingsBScrapeNaming.error(for: $0, template: setting[keyPath: $0.keyPath]) } }
    private var focusedField: SettingsBScrapeNaming.Field { fields.first { $0.key == lastFocused } ?? fields[3] }

    var body: some View {
        ForEach(Array(fields.enumerated()), id: \.element.key) { index, field in
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(field.label).font(.subheadline.weight(.semibold))
                    if !field.note.isEmpty {
                        Text(field.note).font(.caption).foregroundStyle(Theme.textFaint)
                    }
                }
                TextField(field.fallback, text: $setting[dynamicMember: field.keyPath], selection: selectionBinding(field.key))
                    .font(.subheadline.monospaced())
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .focused($focusedKey, equals: field.key)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 8)
                    .background(Color.white.opacity(0.05), in: .rect(cornerRadius: 8))
                    .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(errors[index] == nil ? Color.clear : Theme.danger.opacity(0.55)))
                    .accessibilityIdentifier("scrape-\(field.key)")
                if let error = errors[index] {
                    Text(error).font(.caption).foregroundStyle(Theme.danger)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("scrape-\(field.key)-error")
                }
            }
            .padding(.vertical, 4)
        }

        VStack(alignment: .leading, spacing: 8) {
            Text("可用占位符（点击插入到「\(focusedField.label)」）")
                .font(.caption2)
                .foregroundStyle(Theme.textFaint)
            SettingsBFlow(spacing: 6, lineSpacing: 6) {
                ForEach(focusedField.tokens, id: \.self) { token in
                    Button {
                        insert(token)
                    } label: {
                        Text("{\(token)}")
                            .font(.caption.monospaced())
                            .foregroundStyle(Theme.accent)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background(Color.white.opacity(0.05), in: .rect(cornerRadius: 7))
                            .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(Color.white.opacity(0.08)))
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("scrape-token-\(token)")
                }
            }
        }
        .padding(.vertical, 4)
        // 挂在单一行上（挂在 ForEach 上会被分发成每行一份）
        .onChange(of: focusedKey) { _, key in
            if let key { lastFocused = key }
        }

        preview

        HStack(spacing: 10) {
            Button("恢复默认模板") {
                for field in fields { setting[keyPath: field.keyPath] = "" }
            }
            .buttonStyle(.glass)
            .accessibilityIdentifier("scrape-naming-reset")
            Text("同条目多版本会自动追加「 - 版本标签」后缀，无需写进模板")
                .font(.caption)
                .foregroundStyle(Theme.textFaint)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.vertical, 4)
    }

    /// 实时预览：两条样例路径（模板有误时只显示「✕ 模板有误」）
    private var preview: some View {
        let valid = errors.allSatisfy { $0 == nil }
        let tpl = { (key: String) in
            SettingsBScrapeNaming.effective(fields.first { $0.key == key }!, in: setting)
        }
        let movie = SettingsBScrapeNaming.sampleMovie
        let episode = SettingsBScrapeNaming.sampleEpisode
        let render = SettingsBScrapeNaming.render
        return VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("实时预览").font(.caption2).foregroundStyle(Theme.textFaint)
                Spacer()
                Text(valid ? "✓ 模板有效" : "✕ 模板有误")
                    .font(.caption)
                    .foregroundStyle(valid ? Theme.success : Theme.danger)
                    .accessibilityIdentifier("scrape-naming-validity")
            }
            if valid {
                previewLine(
                    caption: "电影 · 沙丘：第二部（2024）· 2160p BluRay FRDS",
                    root: "/media/电影/",
                    path: "\(render(tpl("naming_entry_dir"), movie))/\(render(tpl("naming_movie_file"), movie)).mkv",
                    id: "scrape-preview-movie"
                )
                previewLine(
                    caption: "剧集 · 风筝（2017）第 1 季第 3 集 · 1080p WEB-DL CHDWEB",
                    root: "/media/剧集/",
                    path: "\(render(tpl("naming_entry_dir"), episode))/\(render(tpl("naming_season_dir"), episode))/\(render(tpl("naming_episode_file"), episode)).mkv",
                    id: "scrape-preview-episode"
                )
            }
        }
        .padding(.vertical, 4)
    }

    private func previewLine(caption: String, root: String, path: String, id: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(caption).font(.caption).foregroundStyle(Theme.textFaint)
            ScrollView(.horizontal, showsIndicators: false) {
                Text("\(Text(root).foregroundStyle(Theme.textFaint))\(Text(path).foregroundStyle(Theme.accent))")
                    .font(.footnote.monospaced())
                    .lineLimit(1)
                    .fixedSize()
                    .textSelection(.enabled)
            }
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier(id)
        }
    }

    private func selectionBinding(_ key: String) -> Binding<TextSelection?> {
        Binding(get: { selections[key] }, set: { selections[key] = $0 })
    }

    /// 在最后聚焦的输入框光标处插入占位符；输入框为空时以默认模板为底（同 Web：`setting[key] || fallback`）
    private func insert(_ token: String) {
        let field = focusedField
        let raw = setting[keyPath: field.keyPath]
        let current = raw.isEmpty ? field.fallback : raw
        let utf16 = current.utf16.count
        var start = utf16
        var end = utf16
        if !raw.isEmpty, let selection = selections[field.key], case let .selection(range) = selection.indices {
            start = min(max(range.lowerBound.utf16Offset(in: current), 0), utf16)
            end = min(max(range.upperBound.utf16Offset(in: current), start), utf16)
        }
        let ns = current as NSString
        let inserted = "{\(token)}"
        let next = ns.replacingCharacters(in: NSRange(location: start, length: end - start), with: inserted)
        setting[keyPath: field.keyPath] = next
        let caret = String.Index(utf16Offset: start + inserted.utf16.count, in: next)
        selections[field.key] = TextSelection(insertionPoint: caret)
        focusedKey = field.key
    }
}

// MARK: - 媒体目录写入

struct SettingsBScrapeMirrorRows: View {
    @Binding var setting: API.MetadataScrapeSetting

    var body: some View {
        ForEach(SettingsBScrapeMirrorRow.all, id: \.key) { row in
            Toggle(isOn: $setting[dynamicMember: row.keyPath]) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(row.label).font(.body.weight(.medium))
                    Text(row.hint).font(.caption).foregroundStyle(Theme.textFaint)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .accessibilityIdentifier("scrape-\(row.key)")
        }
    }
}
