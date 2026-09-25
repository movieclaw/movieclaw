import PhotosUI
import SwiftUI

/// 设置 → 外观（Web settings-view.tsx 的 AppearanceSection）。
///
/// 结构同 Web：置顶「主题」卡片组，下面三个胶囊页签——背景图 / 界面质感 / 导航顺序。
/// - 主题：按**当前设备语境**落字段。App 是手机端，改的是 `theme_mobile`（没单独设过时跟随通用 `theme`），
///   点卡即保存（`PUT /ui/preferences`，整体覆盖式，其余分组原样带回）；
/// - 背景图：账号图库（最多 20 张）的上传 / 点选切换 / 删除（`/appearance*`），上传前压到长边 2560 的 JPEG；
/// - 界面质感：侧栏玻璃三根 + 蒙版两根滑杆，「保存」才落库，「恢复默认」回内置默认并直接保存；
/// - 导航顺序：只影响网页桌面端左侧栏（App 底栏不读它），上/下移改序，保存时保留不可见项（mergeNavOrder）。
/// Netflix 主题是纯色平铺设计：背景图与界面质感两组置灰并说明原因（偏好字段保留不丢）。
struct AppearanceSettingsView: View {
    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Feedback.self) private var feedback

    enum Tab: Hashable { case backdrop, texture, nav }

    @State private var prefs: Loadable<API.UiPreferencesSetting> = .loading
    @State private var appearance: API.AppearanceView?
    @State private var appearanceError: String?
    @State private var tab: Tab = .backdrop

    // 主题
    @State private var themeBusy: String?
    @State private var themeError: String?
    // 背景图
    @State private var backdropItem: PhotosPickerItem?
    @State private var backdropBusy = false
    @State private var backdropError: String?
    // 界面质感草稿
    @State private var texture = TextureDraft.defaults
    @State private var textureBusy = false
    @State private var textureError: String?
    // 导航顺序草稿
    @State private var navDraft: [String] = []
    @State private var navBusy = false
    @State private var navError: String?

    var body: some View {
        AsyncContent(prefs, retry: load) { saved in
            List {
                themeSection(saved)
                Section {
                    SettingsPillTabs(
                        tabs: [(Tab.backdrop, "背景图"), (Tab.texture, "界面质感"), (Tab.nav, "导航顺序")],
                        selection: $tab,
                        identifierPrefix: "appearance-tab"
                    )
                    .listRowBackground(Color.clear)
                    .listRowInsets(EdgeInsets(top: 0, leading: 4, bottom: 0, trailing: 4))
                }
                switch tab {
                case .backdrop: backdropSection(saved)
                case .texture: textureSection(saved)
                case .nav: navSection(saved)
                }
            }
            .scrollDismissesKeyboard(.interactively)
        }
        .appBackground()
        .task { await load() }
        .onChange(of: backdropItem) { _, item in
            guard let item else { return }
            backdropItem = nil
            Task { await uploadBackdrop(item) }
        }
    }

    private func load() async {
        await Loadable.load(into: $prefs) { try await api.uiPrefsShow() }
        if let saved = prefs.value { syncDrafts(saved) }
        do {
            appearance = try await api.appearanceShow()
            appearanceError = nil
        } catch {
            appearanceError = error.localizedDescription
        }
    }

    /// 已保存值变化（首次拉取 / 保存成功）时把草稿对齐到落库值
    private func syncDrafts(_ saved: API.UiPreferencesSetting) {
        texture = TextureDraft(saved)
        navDraft = SettingsNavOrder.apply(visibleNavItems.map(\.id), saved.nav.order)
    }

    private func save(_ next: API.UiPreferencesSetting) async throws {
        let stored = try await api.uiPrefsUpdate(body: next.asInput)
        prefs = .loaded(stored)
        syncDrafts(stored)
    }

    // MARK: 主题

    /// 主题注册表（Web lib/themes.ts THEMES）
    private static let themes: [(id: String, label: String, description: String, bg: Color, accent: Color)] = [
        ("silver", "银玻璃", "液态玻璃 · 冷银高光的控制台质感（默认）",
         Color(red: 0x10 / 255, green: 0x13 / 255, blue: 0x1C / 255), Color(red: 0xCD / 255, green: 0xD6 / 255, blue: 0xE6 / 255)),
        ("netflix", "Netflix", "纯色平铺 · 品牌红 · 顶栏与横版卡片行的影院浏览形态",
         Color(red: 0x14 / 255, green: 0x14 / 255, blue: 0x14 / 255), Color(red: 0xE5 / 255, green: 0x09 / 255, blue: 0x14 / 255)),
    ]

    /// 手机端实际生效的主题：覆盖字段优先，没设过跟随通用字段；未知值兜底银玻璃（normalizeThemeId）
    private func resolvedTheme(_ saved: API.UiPreferencesSetting) -> String {
        let raw = saved.themeMobile ?? saved.theme
        return Self.themes.contains { $0.id == raw } ? raw : "silver"
    }

    private func themeSection(_ saved: API.UiPreferencesSetting) -> some View {
        let current = resolvedTheme(saved)
        return Section {
            HStack(spacing: 12) {
                ForEach(Self.themes, id: \.id) { theme in
                    let active = theme.id == current
                    Button { Task { await pickTheme(theme.id, saved) } } label: {
                        VStack(alignment: .leading, spacing: 6) {
                            RoundedRectangle(cornerRadius: 8)
                                .fill(theme.bg)
                                .frame(height: 52)
                                .overlay(alignment: .bottomLeading) {
                                    Capsule().fill(theme.accent).frame(width: 40, height: 6).padding(8)
                                }
                                .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.white.opacity(0.08)))
                            HStack(spacing: 4) {
                                Text(theme.label).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text)
                                if active { Image(systemName: "checkmark").font(.caption.weight(.bold)).foregroundStyle(Theme.accent) }
                                if themeBusy == theme.id { ProgressView().controlSize(.mini) }
                            }
                            Text(theme.description)
                                .font(.caption2).foregroundStyle(Theme.textMuted)
                                .lineLimit(3, reservesSpace: true)
                                .multilineTextAlignment(.leading)
                        }
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.white.opacity(0.03), in: .rect(cornerRadius: 12))
                        .overlay(
                            RoundedRectangle(cornerRadius: 12)
                                .strokeBorder(active ? Theme.accent : Color.white.opacity(0.12), lineWidth: active ? 2 : 1)
                        )
                        .opacity(themeBusy != nil && themeBusy != theme.id ? 0.5 : 1)
                        .contentShape(.rect)
                    }
                    .buttonStyle(.plain)
                    .disabled(themeBusy != nil)
                    .accessibilityAddTraits(active ? .isSelected : [])
                    .accessibilityIdentifier("theme-\(theme.id)")
                }
            }
            .listRowBackground(Color.clear)
            .listRowInsets(EdgeInsets(top: 4, leading: 0, bottom: 4, trailing: 0))
            if let themeError {
                Text(themeError).font(.footnote).foregroundStyle(Theme.danger)
            }
        } header: {
            Text("主题")
        } footer: {
            Text("主题跟随账号保存，所有设备同步；切换立即生效。当前设置的是移动端的主题，两端可分别设置。")
        }
    }

    /// 选定手机端主题。比较覆盖字段本身而非生效值：没设过覆盖时点当前卡也要真正落覆盖字段
    private func pickTheme(_ id: String, _ saved: API.UiPreferencesSetting) async {
        guard saved.themeMobile != id, themeBusy == nil else { return }
        themeBusy = id
        themeError = nil
        defer { themeBusy = nil }
        var next = saved
        next.themeMobile = id
        do { try await save(next) } catch { themeError = error.localizedDescription }
    }

    // MARK: 背景图

    /// Netflix 下整组置灰 + 说明（Web DisabledGlassGroup）
    @ViewBuilder
    private func disabledNote(_ label: String) -> some View {
        Text("\(label)仅「银玻璃」主题生效——Netflix 主题是纯色平铺设计，没有背景大图与玻璃质感。")
    }

    private func backdropSection(_ saved: API.UiPreferencesSetting) -> some View {
        let disabled = resolvedTheme(saved) == "netflix"
        let isCustom = appearance?.activeId != nil
        let busy = backdropBusy || appearance == nil
        return Section {
            VStack(alignment: .leading, spacing: 14) {
                // 大预览：点按即选图更换（Web 的「大预览 = 投放区」）
                PhotosPicker(selection: $backdropItem, matching: .images) {
                    ZStack(alignment: .bottomLeading) {
                        RemoteImage(url: activeBackdropURL, placeholderSymbol: "photo")
                            .aspectRatio(16 / 9, contentMode: .fill)
                            .frame(maxWidth: .infinity)
                            .clipped()
                        LinearGradient(colors: [.black.opacity(0.65), .clear], startPoint: .bottom, endPoint: .center)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(isCustom ? "自定义背景" : "默认背景 · 深色调").font(.subheadline.weight(.semibold))
                            Text("点击即可更换").font(.caption).foregroundStyle(.white.opacity(0.75))
                        }
                        .foregroundStyle(.white)
                        .padding(14)
                        if busy {
                            Rectangle().fill(.black.opacity(0.5))
                            Text(backdropBusy ? "正在应用…" : "加载中…")
                                .font(.subheadline.weight(.medium)).foregroundStyle(.white.opacity(0.9))
                                .frame(maxWidth: .infinity, maxHeight: .infinity)
                        }
                    }
                    .aspectRatio(16 / 9, contentMode: .fit)
                    .clipShape(.rect(cornerRadius: 16))
                    .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.white.opacity(0.14)))
                }
                .buttonStyle(.plain)
                .disabled(busy)
                .accessibilityLabel("点击更换首页背景")
                .accessibilityIdentifier("backdrop-preview")

                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(alignment: .top, spacing: 14) {
                        BackdropTile(url: api.server.resolve("/backdrop-default.jpg"), label: "默认",
                                     active: !isCustom, disabled: busy, onSelect: { Task { await selectBackdrop(nil) } })
                        ForEach(Array((appearance?.backdrops ?? []).enumerated()), id: \.element.id) { index, item in
                            BackdropTile(
                                url: api.image(item.url), label: "自定义 \(index + 1)",
                                active: item.id == appearance?.activeId, disabled: backdropBusy,
                                onSelect: { Task { await selectBackdrop(item.id) } },
                                onDelete: { Task { await deleteBackdrop(item.id) } }
                            )
                        }
                        PhotosPicker(selection: $backdropItem, matching: .images) {
                            VStack(spacing: 6) {
                                RoundedRectangle(cornerRadius: 8)
                                    .strokeBorder(Color.white.opacity(0.22), style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
                                    .background(Color.black.opacity(0.25), in: .rect(cornerRadius: 8))
                                    .frame(width: 120, height: 68)
                                    .overlay(Image(systemName: "plus").foregroundStyle(Theme.textMuted))
                                Text("上传").font(.caption).foregroundStyle(Theme.textMuted)
                            }
                        }
                        .buttonStyle(.plain)
                        .disabled(busy)
                        .accessibilityIdentifier("backdrop-upload")
                    }
                    .padding(4)
                }
                if let message = backdropError ?? appearanceError {
                    SettingsNotice(text: message)
                }
            }
            .padding(.vertical, 6)
            .disabled(disabled)
            .opacity(disabled ? 0.45 : 1)
        } header: {
            Text("首页背景")
        } footer: {
            if disabled {
                disabledNote("首页背景")
            } else {
                Text("建议使用 16:9、分辨率较高的横图。上传的图全部保留在服务端图库（最多 20 张），点选即切换、点缩略图角上的 × 可删除；玻璃面板的折射随生效图一并更新，跨设备访问同一实例保持一致。")
            }
        }
    }

    private var activeBackdropURL: URL? {
        if let url = appearance?.activeUrl { return api.image(url) }
        return api.server.resolve("/backdrop-default.jpg")
    }

    private func guardBackdrop(_ fallback: String, _ work: () async throws -> API.AppearanceView) async {
        backdropBusy = true
        backdropError = nil
        defer { backdropBusy = false }
        do {
            appearance = try await work()
        } catch {
            let message = error.localizedDescription
            backdropError = message.isEmpty ? fallback : message
        }
    }

    private func uploadBackdrop(_ item: PhotosPickerItem) async {
        await guardBackdrop("上传失败，请重试") {
            guard let data = try await item.loadTransferable(type: Data.self) else {
                throw APIError.network("读取图片失败")
            }
            guard let jpeg = APIClient.compressedJPEG(data, maxEdge: 2560) else {
                throw APIError.network("图片解码失败，请换一张试试")
            }
            return try await api.upload(
                "/appearance/backdrops",
                file: (name: "file", filename: "backdrop.jpg", mimeType: "image/jpeg", data: jpeg),
                as: API.AppearanceView.self
            )
        }
    }

    /// 点选切换生效图（含切回默认）：不删任何图，无需确认
    private func selectBackdrop(_ id: String?) async {
        await guardBackdrop("切换失败，请重试") {
            try await api.appearanceActiveSet(body: .init(backdropId: id))
        }
    }

    /// 删除图库中的一张：不可恢复，二次确认；删的是生效图时后端自动回退默认
    private func deleteBackdrop(_ id: String) async {
        let ok = await feedback.confirm("删除这张背景图？", message: "删除后不可恢复。", confirmTitle: "删除", destructive: true)
        guard ok else { return }
        await guardBackdrop("删除失败，请重试") {
            try await api.appearanceBackdropsDelete(backdropId: id)
        }
    }

    // MARK: 界面质感

    /// 五根滑杆的草稿（与 Web 同一换算：透明度/暗度按百分比，明暗 -1~1 映射到 0~100）
    struct TextureDraft: Equatable {
        var transparency: Double
        var brightness: Double
        var depth: Double
        var blur: Double
        var dark: Double

        /// 内置默认（Web DEFAULT_UI_PREFS，与后端模型默认一致）
        static let defaults = TextureDraft(transparency: 0.49, brightness: -0.36, depth: 28, blur: 13, dark: 0.69)

        init(transparency: Double, brightness: Double, depth: Double, blur: Double, dark: Double) {
            self.transparency = transparency
            self.brightness = brightness
            self.depth = depth
            self.blur = blur
            self.dark = dark
        }

        init(_ prefs: API.UiPreferencesSetting) {
            self.init(transparency: prefs.sidebar.transparency, brightness: prefs.sidebar.brightness,
                      depth: prefs.sidebar.depth, blur: prefs.scrim.blur, dark: prefs.scrim.dark)
        }

        /// 按滑杆显示值比较（Web 的 same() 比较的是取整后的滑杆值落回的数）
        var rounded: [Int] {
            [Int((transparency * 100).rounded()), Int((((brightness + 1) / 2) * 100).rounded()),
             Int(depth.rounded()), Int(blur.rounded()), Int((dark * 100).rounded())]
        }
    }

    private func textureSection(_ saved: API.UiPreferencesSetting) -> some View {
        let disabled = resolvedTheme(saved) == "netflix"
        let savedDraft = TextureDraft(saved)
        let dirty = texture.rounded != savedDraft.rounded
        let isDefault = texture.rounded == TextureDraft.defaults.rounded
        return Section {
            TextureSlider(label: "侧栏透明度", hint: "玻璃材质的整体浓度：0% 为标准玻璃卡片，100% 玻璃完全隐去、直接透出页面背景",
                          minLabel: "标准", maxLabel: "全透",
                          value: Binding(get: { texture.transparency * 100 }, set: { texture.transparency = $0 / 100 }))
            TextureSlider(label: "侧栏明暗", hint: "玻璃底色的亮度：向左更暗、向右更亮", minLabel: "暗", maxLabel: "亮",
                          value: Binding(get: { (texture.brightness + 1) / 2 * 100 }, set: { texture.brightness = $0 / 100 * 2 - 1 }))
            TextureSlider(label: "侧栏厚度", hint: "玻璃的边缘曲率带宽度：越大越像厚玻璃、边缘折射带越宽",
                          minLabel: "薄", maxLabel: "厚", range: 10 ... 90, unit: "", value: $texture.depth)
            TextureSlider(label: "蒙版模糊度", hint: "全站背景蒙版的模糊程度：0 背景清晰透出，越大背景越朦胧",
                          minLabel: "清晰", maxLabel: "朦胧", range: 0 ... 40, unit: "", value: $texture.blur)
            TextureSlider(label: "蒙版暗度", hint: "蒙版把背景压暗的程度：0% 完全不压暗，100% 全黑", minLabel: "透亮", maxLabel: "全黑",
                          value: Binding(get: { texture.dark * 100 }, set: { texture.dark = $0 / 100 }))
            if let textureError {
                Text(textureError).font(.footnote).foregroundStyle(Theme.danger)
            }
            HStack(spacing: 10) {
                Text(dirty ? "有未保存的调整，保存后对所有设备生效" : "设置已保存，跨设备一致")
                    .font(.caption).foregroundStyle(Theme.textFaint)
                Spacer(minLength: 4)
                Button("恢复默认") { Task { await saveTexture(.defaults, saved) } }
                    .buttonStyle(.glass)
                    .disabled(textureBusy || isDefault)
                    .accessibilityIdentifier("texture-reset")
                Button(textureBusy ? "保存中…" : "保存") { Task { await saveTexture(texture, saved) } }
                    .settingsProminentButton()
                    .disabled(textureBusy || !dirty)
                    .accessibilityIdentifier("texture-save")
            }
            .controlSize(.small)
        } header: {
            Text("界面质感")
        } footer: {
            if disabled {
                disabledNote("界面质感")
            } else {
                Text("侧栏玻璃与背景蒙版作用于网页端；App 使用系统液态玻璃材质。")
            }
        }
        .disabled(disabled || textureBusy)
    }

    private func saveTexture(_ draft: TextureDraft, _ saved: API.UiPreferencesSetting) async {
        texture = draft
        textureBusy = true
        textureError = nil
        defer { textureBusy = false }
        var next = saved
        next.sidebar = .init(transparency: draft.transparency, brightness: draft.brightness, depth: draft.depth)
        next.scrim = .init(blur: draft.blur, dark: draft.dark)
        do { try await save(next) } catch { textureError = error.localizedDescription }
    }

    // MARK: 导航顺序

    /// 网页侧栏的主导航清单（Web SIDEBAR_NAV_ITEMS 的次序即内置默认顺序），按当前权限过滤（useVisibleNavItems）
    private var visibleNavItems: [(id: String, label: String, icon: String)] {
        let all: [(id: String, label: String, icon: String)] = [
            ("new", "新会话", "plus"),
            ("library", "媒体库", "play.square.stack"),
            ("explore-movies", "发现电影", "film"),
            ("explore-tv", "发现剧集", "tv"),
            ("subscriptions", "我的订阅", "bookmark"),
            ("tasks", "活动", "waveform.path.ecg"),
        ]
        return all.filter { item in
            switch item.id {
            case "new", "tasks": permissions.isAdmin
            case "subscriptions": permissions.canSubscribe
            default: true
            }
        }
    }

    private func navSection(_ saved: API.UiPreferencesSetting) -> some View {
        let items = visibleNavItems
        let byId = Dictionary(uniqueKeysWithValues: items.map { ($0.id, $0) })
        let savedOrder = SettingsNavOrder.apply(items.map(\.id), saved.nav.order)
        let defaultOrder = items.map(\.id)
        let rows = navDraft.compactMap { byId[$0] }
        let dirty = navDraft != savedOrder
        let isDefault = navDraft == defaultOrder
        return Section {
            Text("调整网页端左侧栏主导航的排列次序（只影响桌面浏览器的侧栏，App 底栏不受影响）：用右侧的上下按钮改序。待处理与更新入口有事才出现，位置固定，不参与排序。")
                .font(.subheadline).foregroundStyle(Theme.textMuted)
            ForEach(Array(rows.enumerated()), id: \.element.id) { index, item in
                HStack(spacing: 12) {
                    Image(systemName: item.icon).foregroundStyle(Theme.textMuted).frame(width: 22)
                    Text(item.label).font(.body.weight(.medium))
                    Spacer()
                    Text("\(index + 1)").font(.caption).monospacedDigit().foregroundStyle(Theme.textFaint)
                    Button { moveNav(index, index - 1) } label: { Image(systemName: "chevron.up").frame(width: 30, height: 30) }
                        .buttonStyle(.glass)
                        .disabled(navBusy || index == 0)
                        .accessibilityLabel("把「\(item.label)」上移")
                        .accessibilityIdentifier("nav-up-\(item.id)")
                    Button { moveNav(index, index + 1) } label: { Image(systemName: "chevron.down").frame(width: 30, height: 30) }
                        .buttonStyle(.glass)
                        .disabled(navBusy || index == rows.count - 1)
                        .accessibilityLabel("把「\(item.label)」下移")
                        .accessibilityIdentifier("nav-down-\(item.id)")
                }
                .accessibilityIdentifier("nav-row-\(item.id)")
            }
            if let navError {
                Text(navError).font(.footnote).foregroundStyle(Theme.danger)
            }
            HStack(spacing: 10) {
                Text(dirty ? "有未保存的调整，保存后对所有设备生效" : "设置已保存，跨设备一致")
                    .font(.caption).foregroundStyle(Theme.textFaint)
                Spacer(minLength: 4)
                Button("恢复默认") { Task { await saveNav([], saved) } }
                    .buttonStyle(.glass)
                    .disabled(navBusy || isDefault)
                    .accessibilityIdentifier("nav-reset")
                Button(navBusy ? "保存中…" : "保存") {
                    Task { await saveNav(SettingsNavOrder.merge(navDraft, saved.nav.order), saved) }
                }
                .settingsProminentButton()
                .disabled(navBusy || !dirty)
                .accessibilityIdentifier("nav-save")
            }
            .controlSize(.small)
        } header: {
            Text("导航顺序")
        }
    }

    private func moveNav(_ from: Int, _ to: Int) {
        guard to >= 0, to < navDraft.count, from != to else { return }
        let item = navDraft.remove(at: from)
        navDraft.insert(item, at: to)
    }

    /// 恢复默认存空列表（而不是一份恰好等于默认的顺序）：将来默认顺序调整或新增入口时自动跟随
    private func saveNav(_ order: [String], _ saved: API.UiPreferencesSetting) async {
        navBusy = true
        navError = nil
        defer { navBusy = false }
        var next = saved
        next.nav = .init(order: order)
        do { try await save(next) } catch { navError = error.localizedDescription }
    }
}

// MARK: - 导航顺序合并规则（Web lib/sidebar-nav.ts）

/// 存下来的顺序是提示不是契约：排过的在前，没排过的（版本升级新增）按内置默认追加在后；
/// 保存时把当前不可见项的 id 追加保留，权限恢复后不会变成「没排过的」。
enum SettingsNavOrder {
    static func apply(_ items: [String], _ order: [String]) -> [String] {
        var remaining = items
        var ordered: [String] = []
        for id in order {
            if let index = remaining.firstIndex(of: id) {
                ordered.append(id)
                remaining.remove(at: index)
            }
        }
        return ordered + remaining
    }

    static func merge(_ visible: [String], _ previous: [String]) -> [String] {
        let set = Set(visible)
        return visible + previous.filter { !set.contains($0) }
    }
}

// MARK: - 小组件

/// 背景画廊瓷砖：选中项高亮环 + 对勾；自定义图左上角常驻 × 删除（触屏没有 hover）
private struct BackdropTile: View {
    let url: URL?
    let label: String
    let active: Bool
    let disabled: Bool
    var onSelect: () -> Void
    var onDelete: (() -> Void)?

    var body: some View {
        VStack(spacing: 6) {
            Button(action: onSelect) {
                RemoteImage(url: url, placeholderSymbol: "photo")
                    .frame(width: 120, height: 68)
                    .clipShape(.rect(cornerRadius: 8))
                    .overlay(
                        RoundedRectangle(cornerRadius: 8)
                            .strokeBorder(active ? Theme.accent : Color.white.opacity(0.14), lineWidth: active ? 2 : 1)
                    )
                    .overlay(alignment: .topTrailing) {
                        if active {
                            Image(systemName: "checkmark")
                                .font(.system(size: 9, weight: .bold))
                                .foregroundStyle(Color(red: 0x14 / 255, green: 0x18 / 255, blue: 0x21 / 255))
                                .frame(width: 18, height: 18)
                                .background(Theme.accentStrong, in: .circle)
                                .padding(5)
                        }
                    }
            }
            .buttonStyle(.plain)
            .disabled(active || disabled)
            .accessibilityLabel("使用\(label)背景")
            .accessibilityAddTraits(active ? .isSelected : [])
            .accessibilityIdentifier("backdrop-tile-\(label)")
            .overlay(alignment: .topLeading) {
                if let onDelete, !disabled {
                    Button(action: onDelete) {
                        Image(systemName: "xmark")
                            .font(.system(size: 9, weight: .bold))
                            .foregroundStyle(.white.opacity(0.9))
                            .frame(width: 20, height: 20)
                            .background(.black.opacity(0.6), in: .circle)
                    }
                    .buttonStyle(.plain)
                    .padding(4)
                    .accessibilityLabel("删除\(label)背景图")
                    .accessibilityIdentifier("backdrop-delete-\(label)")
                }
            }
            Text(label)
                .font(.caption.weight(active ? .semibold : .regular))
                .foregroundStyle(active ? Theme.text : Theme.textMuted)
        }
        .opacity(disabled && !active ? 0.5 : 1)
    }
}

/// 一行滑杆：标题 + 当前值 + 说明 + 两端刻度（Web SliderRow）
private struct TextureSlider: View {
    let label: String
    let hint: String
    let minLabel: String
    let maxLabel: String
    var range: ClosedRange<Double> = 0 ... 100
    var unit = "%"
    @Binding var value: Double

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(label).font(.body.weight(.medium))
                Spacer()
                Text("\(Int(value.rounded()))\(unit)").font(.subheadline).monospacedDigit().foregroundStyle(Theme.textMuted)
            }
            Text(hint).font(.caption).foregroundStyle(Theme.textFaint).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 10) {
                Text(minLabel).font(.caption).foregroundStyle(Theme.textFaint).frame(width: 34, alignment: .trailing)
                Slider(value: Binding(get: { value }, set: { value = $0.rounded() }), in: range, step: 1)
                    .accessibilityLabel(label)
                    .accessibilityIdentifier("slider-\(label)")
                Text(maxLabel).font(.caption).foregroundStyle(Theme.textFaint).frame(width: 34, alignment: .leading)
            }
        }
        .padding(.vertical, 4)
    }
}
