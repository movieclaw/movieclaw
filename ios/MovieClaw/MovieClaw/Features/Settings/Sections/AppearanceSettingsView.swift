import SwiftUI

/// 设置 → 外观（Web settings-view.tsx 的 AppearanceSection，去掉背景图设定）。
///
/// 置顶「主题」卡片组，下面两个胶囊页签——界面质感 / 导航顺序。
/// **App 不做背景图设定**（用户决定，已接受的平台差异）：App 底色固定纯黑（同 Apple Music），
/// 网页外观页的「背景图」页签、质感里的两根蒙版滑杆（只作用于背景图）、详情页剧照的「设为背景」都不提供；
/// 账号在网页设的背景图与蒙版参数原样保留，保存其它偏好时整份带回、不会被 App 冲掉。
/// - 主题：按**当前设备语境**落字段。App 是手机端，改的是 `theme_mobile`（没单独设过时跟随通用 `theme`），
///   点卡即保存（`PUT /ui/preferences`，整体覆盖式，其余分组原样带回）。
///   **App 固定使用银玻璃外观**（产品决定，已接受的平台差异）：Netflix 卡片置灰不可选并写明只在网页生效；
///   账号若在网页把手机端设成了 Netflix，卡片仍如实标出，但 App 外观不变；
/// - 界面质感：侧栏玻璃三根滑杆，只作用于网页桌面端的侧栏玻璃（手机端没有侧栏）；「恢复默认」回内置默认并直接保存；
/// - 导航顺序：只影响网页桌面端左侧栏（App 底栏不读它），上/下移改序，保存时保留不可见项（mergeNavOrder）。
struct AppearanceSettingsView: View {
    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(Feedback.self) private var feedback

    enum Tab: String, Hashable { case texture, nav }
    /// 深链 `?tab=` 直达页签（Web useTabParam），只在首次出现时读一次
    @Environment(\.routeQuery) private var routeQuery
    @State private var routeQueryConsumed = false

    @State private var prefs: Loadable<API.UiPreferencesSetting> = .loading
    @State private var tab: Tab = .texture

    // 主题
    @State private var themeBusy: String?
    @State private var themeError: String?
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
                        tabs: [(Tab.texture, "界面质感"), (Tab.nav, "导航顺序")],
                        selection: $tab,
                        identifierPrefix: "appearance-tab"
                    )
                    .listRowBackground(Color.clear)
                    .listRowInsets(EdgeInsets(top: 0, leading: 4, bottom: 0, trailing: 4))
                }
                switch tab {
                case .texture: textureSection(saved)
                case .nav: navSection(saved)
                }
            }
            .scrollDismissesKeyboard(.interactively)
        }
        .appBackground()
        .task { await load() }
        .onAppear {
            guard !routeQueryConsumed else { return }
            routeQueryConsumed = true
            // 网页的 `?tab=backdrop` 在 App 里没有对应页签，停在默认的界面质感
            if let raw = routeQuery["tab"], let value = Tab(rawValue: raw) { tab = value }
        }
        // 离开质感页签：未保存的草稿回到已保存值（同 Web 质感组卸载）
        .onChange(of: tab) { old, _ in
            guard old == .texture, let saved = prefs.value else { return }
            texture = TextureDraft(saved)
        }
    }

    private func load() async {
        await Loadable.load(into: $prefs) { try await api.uiPrefsShow() }
        if let saved = prefs.value { syncDrafts(saved) }
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
                    // App 只有银玻璃外观：Netflix 卡片置灰不可选（点了也不会改变 App 的样子）
                    let unavailable = theme.id != "silver"
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
                        .opacity(unavailable ? 0.4 : themeBusy != nil && themeBusy != theme.id ? 0.5 : 1)
                        .contentShape(.rect)
                    }
                    .buttonStyle(.plain)
                    .disabled(themeBusy != nil || unavailable)
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
            Text(current == "netflix"
                ? "Netflix 主题仅在网页生效，App 固定使用银玻璃。你的账号在网页手机端当前是 Netflix 主题；选「银玻璃」会把网页手机端也改回银玻璃。"
                : "Netflix 主题仅在网页生效，App 固定使用银玻璃。主题跟随账号保存，当前设置的是移动端的主题，网页桌面端与移动端可分别设置。")
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

    // MARK: 界面质感

    /// 侧栏三根滑杆的草稿（与 Web 同一换算：透明度按百分比，明暗 -1~1 映射到 0~100）。
    /// 网页的另两根蒙版滑杆只作用于背景图，App 不提供；保存时 `scrim` 按已保存值原样带回
    struct TextureDraft: Equatable {
        var transparency: Double
        var brightness: Double
        var depth: Double

        /// 内置默认（Web DEFAULT_UI_PREFS，与后端模型默认一致）
        static let defaults = TextureDraft(transparency: 0.49, brightness: -0.36, depth: 28)

        init(transparency: Double, brightness: Double, depth: Double) {
            self.transparency = transparency
            self.brightness = brightness
            self.depth = depth
        }

        init(_ prefs: API.UiPreferencesSetting) {
            self.init(transparency: prefs.sidebar.transparency, brightness: prefs.sidebar.brightness, depth: prefs.sidebar.depth)
        }

        /// 按滑杆显示值比较（Web 的 same() 比较的是取整后的滑杆值落回的数）
        var rounded: [Int] {
            [Int((transparency * 100).rounded()), Int((((brightness + 1) / 2) * 100).rounded()), Int(depth.rounded())]
        }
    }

    private func textureSection(_ saved: API.UiPreferencesSetting) -> some View {
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
            Text("侧栏透明度、明暗、厚度只作用于网页桌面端的侧栏玻璃。")
        }
        .disabled(textureBusy)
    }

    private func saveTexture(_ draft: TextureDraft, _ saved: API.UiPreferencesSetting) async {
        texture = draft
        textureBusy = true
        textureError = nil
        defer { textureBusy = false }
        var next = saved
        next.sidebar = .init(transparency: draft.transparency, brightness: draft.brightness, depth: draft.depth)
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
                .accessibilityElement(children: .contain)
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
